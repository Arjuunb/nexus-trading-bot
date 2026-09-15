"""Autonomous strategy engine (real logic, paper execution).

This is the "trading bot brain": it runs a real strategy over real market data
and routes every signal through the SAME Phase 1 pipeline a TradingView webhook
uses (controls -> dedup -> risk -> sizing -> exposure -> paper execution ->
ledger). Nothing here is decorative — positions, P&L, stops/targets and the
decision log are all produced by live strategy + risk logic and persisted to
the ledger (the source of truth the dashboard reads).

Flow per closed bar (on a background worker):
    1. mark open positions -> close on stop-loss / take-profit hit
    2. feed the bar to the strategy -> on a Signal, open via the risk pipeline

Trading Instances use provider REST history only to warm indicators, then
consume new closed exchange candles through the WebSocket/forward feed. The
legacy research path may replay local data, but it is never used by a forward
paper instance. Opposite-direction crossovers also flatten via the pipeline's
close path, so the engine and webhooks share identical execution semantics.
"""
from __future__ import annotations

import itertools
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from bot.types import Signal, SignalType
from data.ledger import Ledger
from execution.paper_engine import PaperExecutionEngine
from services.market_data_freshness import assess_timeframe
from services.signal_pipeline import SignalPipeline, gate_blocker


class EngineFeedError(RuntimeError):
    """A transient market-data failure eligible for bounded reconnect attempts."""

    recovery_reason = "Market data connection lost"


class EnginePersistenceError(EngineFeedError):
    """A recoverable durable-state failure; trading remains fail-closed."""

    recovery_reason = "State persistence unavailable"


class StrategyExecutionError(RuntimeError):
    """A strategy implementation failed while evaluating a closed candle."""


class MarketDataStaleError(EngineFeedError):
    """The provider is reachable but its latest closed candle is not current."""


def _default_strategy_factory(symbol: str):
    # The DecisionBrain is the default: a multi-signal, regime-aware decision
    # engine (imported lazily so the module has no hard strategy dependency).
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


def _default_fetcher(symbol: str, timeframe: str, limit: int):
    """Return (bars, source). Real candles when HUB_USE_LIVE_DATA=1, else local."""
    from data.market_data import get_bars
    return get_bars(symbol, n=limit, timeframe=timeframe)


class AutoStrategyEngine:
    def __init__(
        self,
        pipeline: SignalPipeline,
        paper: PaperExecutionEngine,
        ledger: Ledger,
        *,
        symbols: Optional[list[str]] = None,
        timeframe: str = "1h",
        interval: float = 2.0,
        warmup: int = 150,
        live_bars: int = 250,
        strategy_factory: Callable[[str], object] = _default_strategy_factory,
        live: bool = False,
        live_poll_s: float = 60.0,
        fetcher: Optional[Callable[[str, str, int], tuple]] = None,
        initial_last_processed_candle: Optional[str] = None,
        candle_checkpoint: Optional[Callable[[str], None]] = None,
        initial_pending_orders: Optional[dict] = None,
        pending_orders_checkpoint: Optional[Callable[[dict], None]] = None,
        trade_manager=None,
        entry_mode: Optional[str] = None,
        limit_ttl_bars: int = 3,
        instance_id: Optional[str] = None,
        lifecycle_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.pipeline = pipeline
        self.paper = paper
        self.ledger = ledger
        self.symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        # Pair-selection state is separate from the active scan list. Manual
        # mode reduces ``symbols`` to exactly one selected market; auto mode
        # restores the saved watchlist. Both survive dashboard refreshes.
        self.symbol_selection_mode = "auto"
        self.auto_symbols = list(self.symbols)
        self.manual_symbol = self.symbols[0] if self.symbols else "BTCUSDT"
        self.timeframe = timeframe
        self.interval = interval
        self.warmup = warmup
        self.warmup_required_runtime = warmup
        self.live_bars = live_bars
        self.strategy_factory = strategy_factory
        # The rule spec behind the running strategy, when there is one. Set by
        # reconfigure() at deploy time so the monitoring agent can re-derive a
        # baseline for what is ACTUALLY running rather than whatever the user
        # happens to have open in the editor.
        self.deployed_spec = None
        # Live forward mode: poll for NEW closed candles and trade only those
        # (vs. replaying a historical batch). Real forward paper-trading.
        self.live = live
        self.live_poll_s = live_poll_s
        self._fetcher = fetcher or _default_fetcher
        # A Trading Instance supplies these to make forward processing survive
        # a worker/container restart.  The legacy engine leaves them unset.
        self.last_processed_candle = initial_last_processed_candle
        self._candle_checkpoint = candle_checkpoint
        self._pending_orders_checkpoint = pending_orders_checkpoint
        self.last_received_candle: Optional[str] = None
        self.last_closed_candle: Optional[str] = None
        self.warmup_bars = 0
        self.warmup_evidence: dict[str, object] = {}
        self.market_data_status = "replay" if not live else "warming_up"
        self.duplicate_candles_ignored = 0
        self.missing_candles = 0
        self.out_of_order_candles = 0
        # Human-readable label for the active strategy (shown on the Bots page).
        try:
            probe = strategy_factory(self.symbols[0]) if self.symbols else None
            self.strategy_label = getattr(probe, "label", None) or "Strategy"
        except Exception:  # noqa: BLE001 — never let label probing break construction
            self.strategy_label = "Strategy"

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Set by notify_new_candle() when the market feed pushes a closed
        # candle. The forward loop waits on this instead of sleeping out its
        # whole poll interval, so a 5m candle is acted on when it closes rather
        # than up to live_poll_s later. The poll stays as the safety net for a
        # feed that cannot push (REST) or a notification that went missing.
        self._candle_ready = threading.Event()
        self._lock = threading.Lock()
        # Serializes one complete closed-candle decision. Pause acknowledgement
        # acquires this after closing the entry gate, proving no earlier cycle
        # can still create exposure when the API reports success.
        self._cycle_lock = threading.Lock()
        self.running = False
        self.started_at: Optional[str] = None
        # Lifecycle state is server-owned.  A page refresh must never infer it
        # from a stale client timer or a missing websocket connection.
        self.lifecycle_state = "stopped"  # bootstrapping|warming|syncing|ready|running|data_stale|recovering|error|stopped
        self.stop_reason = "Not started"
        self.last_error: Optional[str] = None
        self.last_blocker: Optional[str] = "GATE_REJECTED: WARMUP" if live else None
        self.last_blocker_timestamp: Optional[str] = None
        self.last_heartbeat: Optional[str] = None
        self.last_transition: Optional[str] = None
        self.reconnect_attempt = 0
        self.max_reconnect_attempts = 5
        self.reconnect_next_at: Optional[str] = None
        self.autostart_enabled = True  # persisted by the control API
        self.last_trade: Optional[dict] = None
        self.stats = {"bars": 0, "signals": 0, "accepted_signals": 0,
                      "trades": 0, "rejections": 0}
        self._targets: dict[str, float] = {}
        # Mid-trade management (break-even / scale-out / trailing). The default
        # TradeManager has everything DISABLED — see services/trade_manager.py
        # for the out-of-sample evidence — so behavior is identical to plain
        # stop/target exits until a config is explicitly enabled.
        from services.trade_manager import TradeManager
        self.trade_manager = trade_manager or TradeManager()
        self._managed: dict[str, object] = {}   # symbol -> ManagedTrade
        # Guards the per-symbol managed state (_managed / _targets) so a manual
        # on-chart stop/target adjust from a request thread can never interleave
        # with the engine thread's exit check (a torn read of stop+target+risk).
        self._adjust_lock = threading.Lock()
        # Maker entries (measured better on all validation suites): a signal
        # parks a resting limit at its price; it fills only when price trades
        # through it (no spread/slippage) and expires unfilled after
        # ``limit_ttl_bars`` bars. HUB_ENTRY_MODE=market restores taker entries.
        import os as _os
        self.entry_mode = (entry_mode or _os.environ.get("HUB_ENTRY_MODE", "limit")).lower()
        self.limit_ttl_bars = max(1, int(limit_ttl_bars))
        self.instance_id = instance_id
        self.strategy_version = ""
        self._lifecycle_callback = lifecycle_callback
        self.bootstrap_status = "not_started"
        self.last_recovery_attempt: Optional[str] = None
        self.next_expected_candle: Optional[str] = None
        self.worker_task_identifier: Optional[str] = None
        self.rejection_counts: dict[str, int] = {}
        # PARITY: every backtest/simulation filters entries through the
        # TradeBrain quality scorer (min score 60) — the live engine must
        # apply the IDENTICAL gate or live results run worse than the promise.
        # 0 disables; the gate stands down below 60 bars of history (warmup).
        self.min_quality_score = int(_os.environ.get("HUB_MIN_SCORE", "60"))
        from strategies.brain import TradeBrain
        self._quality_brain = TradeBrain()
        # Context-aware modifiers (cross-asset gate / funding / sentiment) —
        # ALL OFF by default; enable validated ones via HUB_CONTEXT after
        # POST /research/validate-context proves them on real candles.
        from services.context_brain import ContextModifiers
        self.context = ContextModifiers(
            leader_bars_fn=lambda: self._fetcher("BTCUSDT", self.timeframe, 80)[0])
        self._pending: dict[str, dict] = dict(initial_pending_orders or {})
        # Independently fetched completed candles for context-aware strategies.
        # The engine filters each series again at every decision timestamp, so
        # recovery cannot leak a newer 1H/4H close into an older 5M decision.
        self._multi_timeframe_context: dict[str, dict[str, list]] = {}
        self._mtf_evidence: dict[str, dict] = {}
        self.stats_missed_entries = 0
        # Shadow A/B: an optional services.shadow.ShadowRun fed the SAME bars
        # the live strategy trades — a candidate audition with zero capital.
        self.shadow = None
        # Counterfactual tracker (shared with the pipeline): resolves vetoed
        # and missed entries against the same bars the engine trades.
        self.counterfactual = None
        # Unified decision store: EVERY evaluated signal is persisted as a
        # decision object (accepted or rejected) BEFORE any trade is placed.
        self.decisions = None
        # Explainable Trading: per-cycle Decision Report store (data.cycle_store)
        self.reports = None
        # V2 observer is attached only when HUB_CORE_V2_MODE=shadow. It is
        # evidence-only and must never influence legacy routing.
        self.core_v2_observer = None
        # Trading mode: 'full' (auto-execute), 'semi' (queue entries for human
        # approval), 'signal' (alert only). Persisted via runtime settings.
        self.trading_mode = "full"
        # Measured deterioration can only reduce paper entry risk. It never
        # modifies exits or sizes up a trade, and can be disabled explicitly
        # for A/B research with HUB_STRATEGY_HEALTH_GUARD=0.
        self.strategy_health_guard = _os.environ.get("HUB_STRATEGY_HEALTH_GUARD", "1").lower() not in ("0", "false", "off")
        self._strategy_health: dict[str, dict] = {}
        # services.approvals.ApprovalStore — the semi-auto approval queue.
        self.approvals = None
        self._seq = itertools.count(1)
        # Activity tracking — used to explain *why* no trades are happening
        # (e.g. a stalled live feed that never delivers a new candle).
        self.last_bar_ts: Optional[str] = None      # timestamp of the last bar acted on
        self.last_activity: Optional[str] = None    # wall-clock of the last processed bar
        self.current_symbol: Optional[str] = None   # most recently processed market
        # Last closed-candle price per symbol. It is telemetry only: execution
        # still uses the bar passed through the pipeline, while dashboard
        # consumers can calculate a clearly labelled unrealised paper P&L.
        self.last_prices: dict[str, float] = {}
        self.last_source: Optional[str] = None       # data source ("live (ccxt)" / "bundled sample" / …)
        self._warned_fallback: set = set()           # symbols already warned (no per-poll spam)
        # Event bus (architecture phase 2). The engine PUBLISHES; it never
        # subscribes and never reads a result back, so no control flow depends
        # on whether anyone is listening. Publication is additive telemetry
        # today — the trading path is byte-for-byte what it was.
        self._bus = _event_bus()

    # ----------------------------------------------------------- lifecycle
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self.running = True
            self.autostart_enabled = True
            self.lifecycle_state = "starting"
            self.stop_reason = None
            self.last_error = None
            self.reconnect_attempt = 0
            self.reconnect_next_at = None
            self.last_heartbeat = self._utc_now()
            self.last_transition = self.last_heartbeat
            from data.ledger import _now
            self.started_at = _now()
            worker_name = f"paper-instance-{self.instance_id or 'legacy'}"
            self.worker_task_identifier = worker_name
            self._thread = threading.Thread(target=self._run, name=worker_name, daemon=True)
            self._emit_lifecycle("starting", "Worker thread created")
            self._thread.start()
            self.ledger.log(level="info", stage="engine",
                            message=f"Autonomous engine started — {', '.join(self.symbols)} ({self.timeframe})")
            return True

    def notify_new_candle(self) -> None:
        """Tell the forward loop a closed candle is available now.

        Called from the market feed's delivery worker, never from the loop
        itself, so it must stay this cheap: setting an Event cannot block the
        caller and cannot fail. Which candle it was does not matter — the loop
        re-reads the feed and applies everything past its durable cursor, with
        the same continuity checks as a polled pass.
        """
        self._candle_ready.set()

    def stop(self, reason: str = "Stopped by operator") -> bool:
        with self._lock:
            if not self.running:
                self.autostart_enabled = False
                self.lifecycle_state = "stopped"
                self.stop_reason = reason
                self.last_transition = self._utc_now()
                return False
            self.autostart_enabled = False
            self._stop.set()
            # The forward loop parks on _candle_ready between candles, so
            # stopping has to release that wait too or the join below would
            # time out and report a thread stopped while it was still parked.
            self._candle_ready.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5)
        with self._lock:
            self.running = False
            self.lifecycle_state = "stopped"
            self.stop_reason = reason
            self.reconnect_next_at = None
            self.last_transition = self._utc_now()
        self._emit_lifecycle("stopped", reason)
        self.ledger.log(level="info", stage="engine", message=f"Autonomous engine stopped: {reason}")
        return True

    def flush_runtime_state(self) -> dict:
        """Persist every recoverable execution checkpoint before replacement.

        Transient worker state is intentionally not serialised: a full reboot
        must rebuild caches, candle buffers, timers and error counters from a
        fresh engine object. Pending orders, the candle cursor and managed
        position protection are durable execution state and therefore must be
        flushed before that object is discarded.
        """
        self._checkpoint_pending_orders()
        if self.last_processed_candle and self._candle_checkpoint is not None:
            self._checkpoint_candle(self.last_processed_candle)
        with self._adjust_lock:
            managed = list(self._managed.items())
        for symbol, trade in managed:
            self._checkpoint_managed(symbol, trade)
        return {
            "pending_orders": len(self._pending),
            "managed_positions": len(managed),
            "last_processed_candle_timestamp": self.last_processed_candle,
        }

    def acknowledge_entry_pause(self, timeout_s: float = 5.0) -> dict:
        """Wait for the current candle cycle, then durably checkpoint state."""
        acquired = self._cycle_lock.acquire(timeout=max(0.01, float(timeout_s)))
        if not acquired:
            raise TimeoutError("worker did not acknowledge the entry pause before timeout")
        try:
            checkpoint = self.flush_runtime_state()
            return {**checkpoint, "acknowledged_at": self._utc_now()}
        finally:
            self._cycle_lock.release()

    def restart(self) -> bool:
        """Restart the worker without changing its strategy/risk configuration."""
        self.stop("Restart requested by operator")
        return self.start()

    def reconfigure(self, *, symbols, timeframe, strategy_factory, label,
                    spec=None) -> dict:
        """Swap the running strategy / symbols / timeframe and restart (paper).
        Used to deploy a user-built custom strategy from the Strategy Builder.

        ``spec`` is the rule spec behind the deployment, when there is one. It is
        recorded so the monitoring agent can re-derive a backtest baseline for
        whatever is ACTUALLY running. It defaults to None and is cleared on every
        reconfigure, because a built-in engine strategy has no rule spec to
        compare against and a stale one would silently monitor the wrong thing."""
        self.stop("Reconfiguring engine")
        self.symbols = list(symbols)
        self.timeframe = timeframe
        self.strategy_factory = strategy_factory
        self.strategy_label = label
        self.deployed_spec = spec
        self._targets.clear()
        self._managed.clear()
        self._pending.clear()
        self._checkpoint_pending_orders()
        self.stats = {"bars": 0, "signals": 0, "accepted_signals": 0,
                      "trades": 0, "rejections": 0}
        self.start()
        return self.status()

    def feed_status(self) -> tuple[str, Optional[str]]:
        """(status, error) describing the data feed honestly:
        connected | waiting-for-candle | fallback | failed | replay."""
        err = None
        try:
            from data.live_data import last_error
            err = last_error()
        except Exception:  # noqa: BLE001
            pass
        if not self.live:
            return "replay", err
        if self.market_data_status == "stale":
            return "stale", self.last_error or err
        src = self.last_source or ""
        if src.startswith("live") or src.startswith("multi-timeframe"):
            return ("connected" if self.stats["bars"] > 0 else "waiting-for-candle"), None
        if src:
            return "fallback", err          # live wanted, static source delivered
        return ("failed" if err else "waiting-for-candle"), err

    def status(self) -> dict:
        from services.mtf_policy import display_contract
        feed, feed_err = self.feed_status()
        ws_feed = getattr(self, "ws_feed", None)
        uptime_s = None
        if self.started_at:
            try:
                started = datetime.fromisoformat(str(self.started_at).replace("Z", "+00:00"))
                uptime_s = max(0, round((datetime.now(timezone.utc) - started).total_seconds()))
            except (TypeError, ValueError):
                # A malformed legacy timestamp is telemetry damage, not a
                # reason to stop an otherwise healthy trading worker.
                uptime_s = None
        displayed_symbol = self.current_symbol or (self.symbols[0] if self.symbols else "")
        current_evidence = self._mtf_evidence.get(displayed_symbol, {})
        try:
            mtf_policy = display_contract(self.timeframe, current_evidence)
        except ValueError as exc:
            mtf_policy = {"entry_timeframe": self.timeframe, "label": str(exc),
                          "evidence": current_evidence}
        return {
            "running": self.running,
            "symbols": self.symbols,
            "symbol_selection_mode": self.symbol_selection_mode,
            "auto_symbols": self.auto_symbols,
            "manual_symbol": self.manual_symbol,
            "timeframe": self.timeframe,
            "mtf_policy": mtf_policy,
            "interval": self.interval,
            "mode": "live" if self.live else "replay",
            "strategy": self.strategy_label,
            "strategy_key": getattr(self, "strategy_key", ""),
            "started_at": self.started_at,
            "uptime_s": uptime_s,
            "lifecycle_state": self.lifecycle_state,
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
            "last_blocker": self.last_blocker,
            "last_blocker_timestamp": self.last_blocker_timestamp,
            "last_heartbeat": self.last_heartbeat,
            "last_transition": self.last_transition,
            "instance_id": self.instance_id,
            # Which configuration revision this worker was actually built from,
            # and which process owns it. Both are set by the manager at start.
            "config_revision": getattr(self, "config_revision", None),
            "worker_id": getattr(self, "worker_id", None),
            "worker_task_identifier": self.worker_task_identifier,
            "bootstrap_status": self.bootstrap_status,
            "last_recovery_attempt": self.last_recovery_attempt,
            "next_expected_candle": self.next_expected_candle,
            "reconnect_attempt": self.reconnect_attempt,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "reconnect_next_at": self.reconnect_next_at,
            "autostart_enabled": self.autostart_enabled,
            "last_trade": self.last_trade,
            "current_symbol": self.current_symbol,
            "last_prices": self.last_prices.copy(),
            "last_bar_ts": self.last_bar_ts,
            "last_activity": self.last_activity,
            "data_source": self.last_source,
            "feed_status": feed,             # connected | waiting-for-candle | fallback | failed | replay
            "feed_error": feed_err,          # last real fetch error, if any
            "websocket": ws_feed.status() if ws_feed is not None else None,
            "market_data_status": self.market_data_status,
            "last_received_candle": self.last_received_candle,
            "last_closed_candle": self.last_closed_candle,
            "last_processed_candle_timestamp": self.last_processed_candle,
            "warmup_bars": self.warmup_bars,
            "warmup_required": self.warmup_required_runtime,
            "warmup_evidence": dict(self.warmup_evidence),
            "duplicate_candles_ignored": self.duplicate_candles_ignored,
            "missing_candles": self.missing_candles,
            "out_of_order_candles": self.out_of_order_candles,
            "entry_mode": self.entry_mode,
            "pending_orders": len(self._pending),
            "missed_entries": self.stats_missed_entries,
            "strategy_health_guard": self.strategy_health_guard,
            "strategy_health": self._strategy_health,
            "learning": (self.pipeline.learning.report()
                         if getattr(self.pipeline, "learning", None) is not None else None),
            "rejection_counts": dict(self.rejection_counts),
            **self.stats,
        }

    # ----------------------------------------------------------- worker
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._heartbeat()
                try:
                    if self.live:
                        self._run_live()
                    else:
                        self._mark_running()
                        self._run_replay()
                        if not self._stop.is_set():
                            self._mark_stopped("Replay data completed — start a new paper run or enable live market data.")
                    return
                except EngineFeedError as exc:
                    # A provider timeout/disconnect is stale market data even
                    # before an age threshold can be calculated.  Persist the
                    # degraded state first so operators see the real outage,
                    # then enter the bounded recovery loop.
                    self._transition("data_stale", str(exc), error=exc)
                    delay = self._schedule_reconnect(exc)
                    if delay is None:
                        self._mark_error(getattr(exc, "recovery_reason", "Market data connection lost"), exc)
                        return
                    self._stop.wait(delay)
                except StrategyExecutionError as exc:
                    self._mark_error("Strategy execution failed", exc)
                    return
                except Exception as exc:  # strategy/config/runtime errors are not blindly retried
                    self._mark_error("Unknown internal error", exc)
                    return
        finally:
            with self._lock:
                self.running = False
                if self._stop.is_set() and self.lifecycle_state != "error":
                    self.lifecycle_state = "stopped"
                    self.stop_reason = self.stop_reason or "Stopped by operator"
                    self.reconnect_next_at = None
                    self.last_transition = self._utc_now()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _heartbeat(self) -> None:
        self.last_heartbeat = self._utc_now()

    def _emit_lifecycle(self, state: str, reason: str) -> None:
        if self._lifecycle_callback is None:
            return
        try:
            self._lifecycle_callback({
                "instance_id": self.instance_id, "state": state, "reason": reason,
                "timestamp": self.last_transition or self._utc_now(),
                "symbol": self.symbols[0] if self.symbols else None,
                "timeframe": self.timeframe,
                "last_error": self.last_error,
            })
        except Exception as exc:  # observability must not kill execution
            self.ledger.log(level="warning", stage="engine",
                            message=f"Lifecycle persistence unavailable: {type(exc).__name__}: {exc}")

    def _transition(self, state: str, reason: str, *, error: Optional[Exception] = None) -> None:
        with self._lock:
            self.lifecycle_state = state
            self.stop_reason = reason or None
            if error is not None:
                self.last_error = f"{type(error).__name__}: {error}"[:500]
            self.last_transition = self._utc_now()
            self.last_heartbeat = self.last_transition
        self._emit_lifecycle(state, reason)

    def _mark_running(self) -> None:
        changed = self.lifecycle_state != "running"
        with self._lock:
            self.lifecycle_state = "running"
            self.stop_reason = None
            self.last_error = None
            self.reconnect_attempt = 0
            self.reconnect_next_at = None
            self.last_transition = self._utc_now()
            self.last_heartbeat = self.last_transition
        if changed:
            self._emit_lifecycle("running", "Fresh closed market data confirmed")

    def _mark_stopped(self, reason: str) -> None:
        with self._lock:
            self.lifecycle_state = "stopped"
            self.stop_reason = reason
            self.last_transition = self._utc_now()
        self._emit_lifecycle("stopped", reason)

    def _schedule_reconnect(self, exc: Exception) -> Optional[float]:
        reason = getattr(exc, "recovery_reason", "Market data connection lost")
        with self._lock:
            self.reconnect_attempt += 1
            attempt = self.reconnect_attempt
            safe_error = f"{type(exc).__name__}: {exc}"[:500]
            self.last_error = safe_error
            self.last_heartbeat = self._utc_now()
            self.last_recovery_attempt = self.last_heartbeat
            if attempt > self.max_reconnect_attempts:
                return None
            delay = min(2.0 ** attempt, 30.0)
            self.lifecycle_state = "recovering"
            self.stop_reason = reason
            self.reconnect_next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            self.last_transition = self.last_heartbeat
        self._emit_lifecycle("recovering", f"Automatic recovery attempt {attempt}/{self.max_reconnect_attempts}")
        self.ledger.log(level="warning", stage="engine",
                        message=(f"{reason} — reconnecting "
                                 f"(attempt {attempt}/{self.max_reconnect_attempts} in {delay:.0f}s): {safe_error}"))
        return delay

    def _mark_error(self, reason: str, exc: Exception) -> None:
        detail = f"{type(exc).__name__}: {exc}"[:500]
        stack = traceback.format_exc(limit=5).strip()[-1200:]
        with self._lock:
            self.lifecycle_state = "error"
            self.stop_reason = reason
            self.last_error = detail
            self.reconnect_next_at = None
            self.last_heartbeat = self._utc_now()
            self.last_transition = self.last_heartbeat
        self._emit_lifecycle("error", reason)
        self.ledger.log(level="error", stage="engine",
                        message=f"Engine stopped — {reason}: {detail}" + (f"\n{stack}" if stack else ""))

    def _run_replay(self) -> None:
        from data.market_data import get_bars

        strategies: dict[str, object] = {}
        live: dict[str, list] = {}
        seeds: dict[str, int] = {}

        for sym in self.symbols:
            seeds[sym] = 1
            self._load_batch(sym, strategies, live, seeds, get_bars)

        while not self._stop.is_set():
            advanced = False
            for sym in self.symbols:
                if self._stop.is_set():
                    break
                if not live[sym]:
                    self._load_batch(sym, strategies, live, seeds, get_bars)
                if not live[sym]:
                    continue
                bar = live[sym].pop(0)
                with self._cycle_lock:
                    self._process_bar(sym, bar, strategies[sym])
                self.stats["bars"] += 1
                advanced = True
            if not advanced:
                break
            self._stop.wait(self.interval)

    # ------------------------------------------------------ live forward mode
    def _run_live(self) -> None:
        """Warm up on history WITHOUT trading, then act only on NEW closed
        candles as they arrive — genuine forward paper-trading."""
        strategies: dict[str, object] = {}
        last_ts: dict[str, object] = {}

        for sym in self.symbols:
            self.bootstrap_status = "fetching_rest_history"
            self._transition("bootstrapping", "Fetching provider history for indicator warm-up")
            strat = self.strategy_factory(sym)
            required_warmup = self._required_warmup(strat)
            self.warmup_required_runtime = required_warmup
            persisted = self._parse_timestamp(self.last_processed_candle)
            try:
                bars, src = self._fetch_bootstrap_history(sym, required_warmup, persisted)
            except Exception as exc:  # initial warmup previously killed the thread silently
                raise EngineFeedError(f"{sym} warmup failed: {exc}") from exc
            if not (src or "").startswith("live"):
                raise EngineFeedError(f"{sym} live feed unavailable (source: {src or 'none'})")
            self.last_source = src
            self._record_received_candle(bars)
            closed = self._closed_bars(bars, self.timeframe)
            unique_timestamps = {
                getattr(bar, "timestamp", None) for bar in bars
                if getattr(bar, "timestamp", None) is not None
            }
            from data.forward_market_data import valid_closed_bars
            structurally_valid = valid_closed_bars(
                bars, _TF_SECONDS[self.timeframe],
                now=datetime.max.replace(tzinfo=timezone.utc),
            )
            valid_timestamps = {bar.timestamp for bar in closed}
            self.warmup_evidence = {
                "required": required_warmup,
                "requested": max(required_warmup * 2, required_warmup + 50),
                "fetched": len(bars),
                "valid_closed": len(closed),
                "duplicates_removed": max(0, len(bars) - len(unique_timestamps)),
                "malformed_rejected": max(0, len(unique_timestamps) - len(structurally_valid)),
                "incomplete_excluded": max(0, len(structurally_valid) - len(valid_timestamps)),
                "oldest": closed[0].timestamp.isoformat() if closed else None,
                "newest": closed[-1].timestamp.isoformat() if closed else None,
            }
            if not closed:
                raise EngineFeedError(f"{sym} live feed returned no closed candles")
            self._record_market_snapshot(sym, closed)
            # On recovery indicators are warmed only through the persisted
            # cursor.  Newer bars are then processed chronologically below;
            # warming through them first would leak future information.
            warm = [b for b in closed if persisted is None or b.timestamp <= persisted]
            if len(warm) < required_warmup:
                # The provider's regular window is not long enough to rebuild
                # state safely from the cursor.  Do not replay/guess; fail
                # closed and let the bounded reconnect/recovery path retry.
                raise EngineFeedError(
                    f"{sym} cannot rebuild {required_warmup}-bar warm-up before "
                    f"{'persisted candle' if persisted is not None else 'live start'} "
                    f"(provider returned {len(warm)} valid closed candles)")
            if persisted is not None and warm[-1].timestamp != persisted:
                raise EngineFeedError(
                    f"{sym} provider backfill does not contain persisted candle {persisted.isoformat()}")
            self._require_continuity(warm[-required_warmup:], self.timeframe)
            self.bootstrap_status = "warming_indicators"
            self._transition("warming", f"Loading {required_warmup} completed candles into strategy")
            self._refresh_multi_timeframe_context(sym, strat, entry_bars=closed)
            if self._is_multi_timeframe_strategy(strat):
                self._apply_multi_timeframe_context(strat, closed[-1].timestamp)
            else:
                for b in warm[-required_warmup:]:
                    strat.bars.append(b)
                self._apply_multi_timeframe_context(strat, closed[-1].timestamp)
            strategies[sym] = strat
            self.warmup_bars = len(strat.bars)
            if persisted is None:
                # First forward start: historical bars initialise indicators
                # only.  They can never create a paper trade.
                last_ts[sym] = closed[-1].timestamp
                self.last_processed_candle = last_ts[sym].isoformat()
                self._checkpoint_candle(self.last_processed_candle)
                self.bootstrap_status = "syncing_checkpoint"
                self._transition("syncing", "Initial durable cursor committed; no warm-up candle traded")
            else:
                self.bootstrap_status = "syncing_checkpoint"
                self._transition("syncing", "Processing unseen closed candles after durable cursor")
                last_ts[sym] = persisted
                # Recover missed *closed* candles once and in chronological
                # order. _ingest ignores the persisted candle itself.
                unseen = [bar for bar in closed if bar.timestamp > persisted]
                self._require_continuity([warm[-1], *unseen], self.timeframe)
                if unseen:
                    last_ts[sym] = self._ingest(sym, strat, unseen, last_ts[sym])
            self.ledger.log(level="info", stage="engine", symbol=sym,
                            message=f"market_data_connected symbol={sym} timeframe={self.timeframe} source={src}")
            self.ledger.log(level="info", stage="engine", symbol=sym,
                            message=f"warmup_complete bars={self.warmup_bars} last_closed={self.last_closed_candle}")

        self.bootstrap_status = "ready"
        self._transition("ready", "Warm-up and durable cursor synchronization complete")
        self._mark_running()

        while not self._stop.is_set():
            for sym in self.symbols:
                if self._stop.is_set():
                    break
                try:
                    bars, src = self._forward_fetch(sym, max(self.warmup + 5, 60))
                except Exception as exc:
                    raise EngineFeedError(f"{sym} fetch failed: {exc}") from exc
                self.last_source = src
                self._record_received_candle(bars)
                if not (src or "").startswith("live"):
                    raise EngineFeedError(f"{sym} live feed unavailable (source: {src or 'none'})")
                closed = self._closed_bars(bars, self.timeframe)
                if not closed:
                    raise EngineFeedError(f"{sym} provider returned no closed candles")
                self._record_market_snapshot(sym, closed)
                unseen = [item for item in closed
                          if last_ts[sym] is None or item.timestamp > last_ts[sym]]
                has_new_candle = bool(unseen)
                if unseen and last_ts[sym] is not None:
                    expected = _TF_SECONDS.get(self.timeframe, 3600)
                    if (unseen[0].timestamp - last_ts[sym]).total_seconds() != expected:
                        raise EngineFeedError(
                            f"{sym} missing candle after {last_ts[sym].isoformat()}; "
                            "entering REST cursor recovery before any decision")
                    self._require_continuity(unseen, self.timeframe)
                if has_new_candle:
                    self._refresh_multi_timeframe_context(sym, strategies[sym], entry_bars=closed)
                if unseen:
                    last_ts[sym] = self._ingest(
                        sym, strategies[sym], unseen, last_ts[sym])
                self._mark_running()
                self._heartbeat()
            # Wake on the next closed candle, or on the poll deadline,
            # whichever comes first. Clearing before the fetch would lose a
            # candle that closed while the fetch above was running; clearing
            # after the wait means at worst one extra fetch that finds nothing
            # new, which _ingest already treats as a no-op.
            if not self._stop.is_set():
                self._candle_ready.wait(self.live_poll_s)
                self._candle_ready.clear()

    def _ingest(self, sym, strat, bars, last_ts):
        """Process closed forward bars once; caller already removed forming bar."""
        previous = last_ts
        for b in sorted(bars, key=lambda item: item.timestamp):
            if last_ts is not None and b.timestamp <= last_ts:
                self.duplicate_candles_ignored += 1
                continue
            if last_ts is not None and b.timestamp < previous:
                self.out_of_order_candles += 1
                continue
            if last_ts is not None:
                gap_s = (b.timestamp - last_ts).total_seconds()
                expected = _TF_SECONDS.get(self.timeframe, 3600)
                if gap_s > expected * 1.5:
                    self.missing_candles += max(0, int(gap_s // expected) - 1)
            if last_ts is None or b.timestamp > last_ts:
                self._apply_multi_timeframe_context(strat, b.timestamp)
                with self._cycle_lock:
                    self._process_bar(sym, b, strat)
                self.stats["bars"] += 1
                last_ts = b.timestamp
                self.last_processed_candle = last_ts.isoformat()
                self._checkpoint_candle(self.last_processed_candle)
                self.ledger.log(level="info", stage="engine", symbol=sym,
                                message=f"candle_processed timestamp={self.last_processed_candle}")
        return last_ts

    @staticmethod
    def _is_multi_timeframe_strategy(strategy) -> bool:
        return bool(getattr(strategy, "required_timeframes", ())) and callable(
            getattr(strategy, "set_timeframe_context", None))

    def _refresh_multi_timeframe_context(self, symbol: str, strategy, *, entry_bars: list) -> None:
        """Fetch independent live series for every declared timeframe.

        The strict forward fetcher has no historical/synthetic fallback. Its
        final candle is conservatively removed on every timeframe because it
        may still be forming.
        """
        from services.mtf_policy import native_timeframes, policy_for
        try:
            policy_required = native_timeframes(self.timeframe)
            primary_tf, secondary_tf = policy_for(self.timeframe)
        except ValueError as exc:
            raise EngineFeedError(str(exc)) from exc
        strategy_required = tuple(getattr(strategy, "required_timeframes", ()))
        required = tuple(dict.fromkeys((*policy_required, *strategy_required)))
        decision_tf = str(getattr(strategy, "decision_timeframe", self.timeframe))
        if strategy_required and self.timeframe != decision_tf:
            raise EngineFeedError(
                f"{getattr(strategy, 'label', 'Multi-timeframe strategy')} requires "
                f"a {decision_tf} Trading Instance decision timeframe")
        policy_for(self.timeframe)  # explicit fail-closed validation
        minimums = getattr(getattr(strategy, "config", None), "minimum_bars", {})
        context: dict[str, list] = {decision_tf: list(entry_bars)}
        previous_context = self._multi_timeframe_context.get(symbol, {})
        decision_duration = _TF_SECONDS.get(decision_tf)
        decision_close = (entry_bars[-1].timestamp + timedelta(seconds=decision_duration)
                          if entry_bars and decision_duration else None)
        sources = []
        for timeframe in required:
            if timeframe == decision_tf:
                continue
            # A policy secondary is context only. Failure to load it must be
            # visible as missing bias, but it must not silently become another
            # entry gate. A strategy that explicitly declares the same clock
            # in required_timeframes still makes it mandatory.
            mandatory = timeframe == primary_tf or timeframe in strategy_required
            # Primary/secondary bias needs at least the two native candles
            # required to identify direction. Strategy-owned MTF indicators
            # may require a larger independent history.
            required_bars = int(minimums.get(timeframe, 2))
            limit = max(2, required_bars * 2, required_bars + 50)
            duration = _TF_SECONDS.get(timeframe)
            cached = list(previous_context.get(timeframe, ()))
            next_close = (cached[-1].timestamp + timedelta(seconds=duration * 2)
                          if cached and duration else None)
            if cached and decision_close is not None and next_close is not None and decision_close < next_close:
                closed, source = cached, "live (cached closed context)"
            else:
                try:
                    bars, source = self._forward_fetch_for_timeframe(symbol, timeframe, limit)
                except (EngineFeedError, RuntimeError):
                    if mandatory:
                        raise
                    context[timeframe] = []
                    sources.append(f"{timeframe}:unavailable optional bias")
                    continue
                if not str(source or "").startswith("live"):
                    raise EngineFeedError(f"{symbol} {timeframe} live context unavailable")
                closed = self._closed_bars(bars, timeframe)
            if len(closed) < required_bars:
                if mandatory:
                    raise EngineFeedError(
                        f"{symbol} {timeframe} returned {len(closed)} completed candles; "
                        f"requires {required_bars}")
                context[timeframe] = []
                sources.append(f"{timeframe}:insufficient optional bias")
                continue
            # Each required timeframe is verified independently against the one
            # platform-wide authority, so a fresh 5m entry candle can never
            # carry a stale 1h bias into an entry. A mandatory clock that is
            # stale fails closed; a policy secondary is bias only and degrades
            # to "no bias" rather than becoming a second entry gate.
            htf = assess_timeframe(symbol, timeframe, closed[-1].timestamp) \
                if duration is not None else None
            age = htf.age_seconds if htf and htf.age_seconds is not None else 0.0
            if htf is None or not htf.fresh:
                if mandatory:
                    raise EngineFeedError(
                        f"{symbol} {timeframe} context stale: age={max(0.0, age):.0f}s")
                context[timeframe] = []
                sources.append(f"{timeframe}:stale optional bias")
                continue
            context[timeframe] = closed
            sources.append(f"{timeframe}:{source}")
        self._multi_timeframe_context[symbol] = context
        if sources:
            self.last_source = "multi-timeframe " + ", ".join(sources)

    def _forward_fetch_for_timeframe(self, symbol: str, timeframe: str, limit: int):
        bars, source = self._fetcher(symbol, timeframe, limit)
        if not str(source or "").startswith("live"):
            raise EngineFeedError(
                f"forward paper requires live {timeframe} provider data, got {source or 'none'}")
        return bars, source

    def _apply_multi_timeframe_context(self, strategy, decision_timestamp) -> None:
        from bot.data.resample import TF_SECONDS
        from services.mtf_policy import evidence_at, native_timeframes
        policy_required = native_timeframes(self.timeframe)
        strategy_required = tuple(getattr(strategy, "required_timeframes", ()))
        required = tuple(dict.fromkeys((*policy_required, *strategy_required)))
        decision_tf = str(getattr(strategy, "decision_timeframe", self.timeframe))
        # The decision bar timestamp is its open. Every strategy votes at that
        # bar's close, never at its open.
        decision_close = decision_timestamp + timedelta(seconds=TF_SECONDS[self.timeframe])
        symbol = getattr(strategy, "symbol", None)
        if not symbol:
            return
        source = self._multi_timeframe_context.get(symbol, {})
        causal: dict[str, list] = {}
        for timeframe in required:
            duration = TF_SECONDS.get(timeframe)
            if duration is None:
                raise EngineFeedError(f"Unsupported strategy context timeframe {timeframe}")
            causal[timeframe] = [bar for bar in source.get(timeframe, ())
                                 if bar.timestamp + timedelta(seconds=duration) <= decision_close]
        evidence = evidence_at(
            symbol, self.timeframe, causal, decision_close,
        )
        if evidence.get("primary") is None:
            raise EngineFeedError(
                f"{strategy.symbol} {self.timeframe} primary native HTF has no candle "
                f"closed by {decision_close.isoformat()}")
        self._mtf_evidence[symbol] = evidence
        setter = getattr(strategy, "set_native_mtf_context", None)
        if callable(setter):
            setter({tf: causal.get(tf, []) for tf in policy_required}, evidence)
        if self._is_multi_timeframe_strategy(strategy):
            strategy.set_timeframe_context(causal)

    def _forward_fetch(self, symbol: str, limit: int):
        """Call a strict forward fetcher; historical sources are rejected."""
        bars, source = self._fetcher(symbol, self.timeframe, limit)
        if not str(source or "").startswith("live"):
            raise EngineFeedError(f"forward paper requires live provider data, got {source or 'none'}")
        return bars, source

    @staticmethod
    def _closed_bars(bars, timeframe: str):
        from data.forward_market_data import valid_closed_bars
        duration = _TF_SECONDS.get(timeframe)
        if duration is None:
            raise EngineFeedError(f"Unsupported forward timeframe {timeframe}")
        return valid_closed_bars(bars, duration)

    def _required_warmup(self, strategy) -> int:
        required = int(self.warmup)
        configured = getattr(strategy, "warmup_required", None)
        if configured is not None:
            required = max(required, int(configured))
        minimums = getattr(getattr(strategy, "config", None), "minimum_bars", {})
        decision_tf = str(getattr(strategy, "decision_timeframe", self.timeframe))
        if isinstance(minimums, dict):
            required = max(required, int(minimums.get(decision_tf, 0) or 0))
        return required

    def _require_continuity(self, bars, timeframe: str) -> None:
        expected = _TF_SECONDS.get(timeframe)
        if expected is None or len(bars) < 2:
            return
        ordered = sorted(bars, key=lambda bar: bar.timestamp)
        for previous, current in zip(ordered, ordered[1:]):
            gap = int((current.timestamp - previous.timestamp).total_seconds())
            if gap != expected:
                missing = max(0, gap // expected - 1)
                self.missing_candles += missing
                raise EngineFeedError(
                    f"{self.symbols[0]} {timeframe} candle continuity failed between "
                    f"{previous.timestamp.isoformat()} and {current.timestamp.isoformat()} "
                    f"({missing} missing); REST repair incomplete")

    def _fetch_with_since(self, symbol: str, timeframe: str, limit: int,
                          since_ms: Optional[int] = None):
        try:
            return self._fetcher(symbol, timeframe, limit, since_ms=since_ms)
        except TypeError as exc:
            if since_ms is not None:
                raise EngineFeedError("forward fetcher does not support cursor backfill") from exc
            return self._fetcher(symbol, timeframe, limit)

    def _fetch_bootstrap_history(self, symbol: str, required: int,
                                 persisted: Optional[datetime]):
        """Fetch warm-up plus buffer and page across downtime when required."""
        duration = _TF_SECONDS.get(self.timeframe)
        if duration is None:
            raise EngineFeedError(f"Unsupported forward timeframe {self.timeframe}")
        target = max(required * 2, required + 50)
        if persisted is None:
            return self._fetch_with_since(symbol, self.timeframe, min(1000, target))

        since = persisted - timedelta(seconds=duration * (required + 10))
        since_ms = int(since.timestamp() * 1000)
        merged: dict[datetime, object] = {}
        source = ""
        for _page in range(20):
            page, source = self._fetch_with_since(symbol, self.timeframe, 1000, since_ms)
            if not str(source or "").startswith("live"):
                raise EngineFeedError(f"forward paper requires live provider data, got {source or 'none'}")
            valid = self._closed_bars(page, self.timeframe)
            before = len(merged)
            for bar in valid:
                merged[bar.timestamp] = bar
            if not valid or len(merged) == before:
                break
            newest = valid[-1].timestamp
            since_ms = int((newest + timedelta(seconds=duration)).timestamp() * 1000)
            age_after_close = max(0.0, (datetime.now(timezone.utc) - newest).total_seconds() - duration)
            # Some venues cap OHLCV below the requested 1000 rows. A short page
            # is therefore not proof that we reached the live edge; page until
            # freshness or until the provider makes no forward progress.
            if age_after_close <= duration * 1.5:
                break
        return [merged[key] for key in sorted(merged)], source

    @staticmethod
    def _parse_timestamp(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _checkpoint_candle(self, timestamp: str) -> None:
        if self._candle_checkpoint is None:
            return
        try:
            self._candle_checkpoint(timestamp)
        except Exception as exc:  # persistence is critical for exactly-once
            raise EnginePersistenceError(f"candle cursor persistence failed: {exc}") from exc

    def _checkpoint_pending_orders(self) -> None:
        if self._pending_orders_checkpoint is None:
            return
        try:
            snapshot = {symbol: dict(order) for symbol, order in self._pending.items()}
            self._pending_orders_checkpoint(snapshot)
        except Exception as exc:  # order persistence is part of instance recovery
            raise EnginePersistenceError(f"pending order persistence failed: {exc}") from exc

    def _record_market_snapshot(self, symbol, closed) -> None:
        newest = closed[-1]
        self.last_closed_candle = newest.timestamp.isoformat()
        # OHLCV providers timestamp a candle at its *open*.  ``newest`` is
        # already known to be closed (the caller removed the forming bar), so
        # freshness begins when that interval closes, not at its open.  Using
        # the raw timestamp made an otherwise current 5m candle appear five
        # minutes older than it is and could exhaust reconnects before the
        # next provider candle arrived.
        interval = _TF_SECONDS.get(self.timeframe, 3600)
        # One rule, shared with both Strategy Labs and the dashboard. This path
        # already measured from the close (see above); what it did not share was
        # how much lateness to allow, so an instance would keep trading on a
        # candle a lab had already called stale. The shared authority also
        # reports the numbers it judged on, which land in the blocker text.
        verdict = assess_timeframe(symbol, self.timeframe, newest.timestamp)
        age = verdict.age_seconds if verdict.age_seconds is not None else 0.0
        allowed_age = verdict.allowed_age_seconds
        if not verdict.fresh:
            self.market_data_status = "stale"
            self.last_blocker = f"GATE_REJECTED: {verdict.blocker or 'STALE_CANDLES'}"
            self.last_blocker_timestamp = newest.timestamp.isoformat()
            raise MarketDataStaleError(
                f"{symbol} market data stale: age={age:.0f}s allowed={allowed_age:.0f}s")
        self.market_data_status = "healthy"
        self.next_expected_candle = (
            newest.timestamp + timedelta(seconds=interval * 2)).isoformat()

    def _record_received_candle(self, bars) -> None:
        """Expose the newest provider event separately from the closed cursor."""
        timestamps = [getattr(bar, "timestamp", None) for bar in bars]
        valid = [stamp for stamp in timestamps if isinstance(stamp, datetime)]
        if not valid:
            return
        newest = max(stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None
                     else stamp.astimezone(timezone.utc) for stamp in valid)
        self.last_received_candle = newest.isoformat()

    def _load_batch(self, sym, strategies, live, seeds, get_bars) -> None:
        bars, src = get_bars(sym, n=self.warmup + self.live_bars,
                             timeframe=self.timeframe, seed=seeds[sym])
        self.last_source = src
        seeds[sym] += 1
        if sym not in strategies:
            strategies[sym] = self.strategy_factory(sym)
            # Warm up indicators with history WITHOUT trading it.
            for b in bars[:self.warmup]:
                strategies[sym].bars.append(b)
            live[sym] = list(bars[self.warmup:])
            self.ledger.log(level="info", stage="engine", symbol=sym,
                            message=f"{sym}: {src} data — warmed {self.warmup}, replaying {len(live[sym])} bars")
        else:
            live[sym] = list(bars)

    def _process_bar(self, sym: str, bar, strategy) -> None:
        from datetime import datetime, timezone
        decision_identity = self._decision_identity(sym, bar.timestamp)
        # record activity so diagnostics can tell a live trade from a stalled feed
        try:
            self.last_bar_ts = bar.timestamp.isoformat()
        except Exception:  # noqa: BLE001
            self.last_bar_ts = str(getattr(bar, "timestamp", ""))
        self.last_activity = datetime.now(timezone.utc).isoformat()
        self.current_symbol = sym
        try:
            self.last_prices[sym] = float(bar.close)
        except (TypeError, ValueError):
            pass
        # Announce the closed bar. Only CLOSED bars reach this method, which is
        # the contract MarketDataReceived states. The bus isolates subscriber
        # errors, and this guard covers the remaining case — constructing the
        # event itself failing on an unexpected bar shape. Telemetry must never
        # be able to stop a bar being traded.
        try:
            self._bus.publish(_events.MarketDataReceived(
                symbol=sym, timeframe=self.timeframe, bar=bar,
                # `data_source` is the provenance of the DATA; the envelope's
                # `source` is the module that published the event. They were
                # one field before the envelope existed.
                data_source=self.last_source or "", source="auto_engine"))
        except Exception:  # noqa: BLE001 — publication is never load-bearing
            pass
        # 0. shadow candidate sees the same bar (virtual, zero capital).
        if self.shadow is not None:
            try:
                self.shadow.on_bar(sym, bar)
            except Exception:  # noqa: BLE001 — the shadow must never affect live
                pass
        # 0b. resolve counterfactual (vetoed/missed) virtual trades.
        if self.counterfactual is not None:
            try:
                self.counterfactual.on_bar(sym, bar)
            except Exception:  # noqa: BLE001 — grading must never affect live
                pass
        # 0c. expire un-approved trade ideas past their TTL and grade each as a
        # missed entry (an approval the user let lapse is a real decision).
        if self.approvals is not None:
            try:
                for _idea in self.approvals.expire():
                    if self.counterfactual is not None and _idea.get("status") == "expired":
                        self.counterfactual.record_veto(
                            symbol=_idea.get("symbol"),
                            side="long" if _idea.get("side") == "BUY" else "short",
                            entry=_idea.get("entry"), stop=_idea.get("stop"),
                            target=_idea.get("target"), rule="approval-ttl",
                            detail="approval window expired unactioned")
            except Exception:  # noqa: BLE001 — never let approvals break the loop
                pass
        # 1. resting limit entry: fill if this bar traded through it. A candle
        # does not reveal whether its high happened before or after that fill,
        # so a newly filled entry may be stopped on this candle but can never be
        # credited with a same-candle target/scale-out (conservative ordering).
        filled_this_bar = self._check_pending(sym, bar)
        # 2. stop-loss / take-profit exits against this bar's range.
        position_before_exit = self.paper.open_position(sym)
        exit_taken = self._check_exit(sym, bar, strategy, entry_bar=filled_this_bar)
        position_after_exit = self.paper.open_position(sym)
        # 3. strategy decision on the new bar.
        # A lifecycle-aware strategy must not keep proposing entries while its
        # authoritative instance-scoped broker already owns a position. The
        # ordinary strategies retain their historical pipeline-rejection
        # behaviour for backward compatibility.
        lifecycle_aware = callable(getattr(strategy, "mark_position_managing", None))
        skip_entry_scan = False
        if lifecycle_aware and position_after_exit is not None:
            if filled_this_bar:
                strategy.mark_position_open("Resting paper order filled on the completed candle")
            else:
                strategy.mark_position_managing()
            skip_entry_scan = True
        elif lifecycle_aware and position_before_exit is not None and position_after_exit is None:
            strategy.mark_position_closed("Stop, target, or managed exit completed")
            # Never close and reopen on the same OHLC candle: its intrabar path
            # is unknowable and doing so would fabricate execution ordering.
            skip_entry_scan = True
        try:
            signal: Optional[Signal] = None if skip_entry_scan else strategy.on_bar(bar)
        except Exception as exc:
            label = getattr(strategy, "label", type(strategy).__name__)
            raise StrategyExecutionError(
                f"{label} failed for {sym} {self.timeframe}: "
                f"{type(exc).__name__}: {exc}") from exc
        outcome: Optional[dict] = None
        strategy_decision: Optional[dict] = None
        if signal is not None:
            outcome = self._on_signal(
                sym, signal, strategy, decision_identity=decision_identity)
            if hasattr(strategy, "lifecycle_state"):
                kind = (outcome or {}).get("kind")
                try:
                    from strategies.adaptive_trend_pullback.models import SetupState
                    if kind == "opened":
                        marker = getattr(strategy, "mark_position_open", None)
                        marker() if callable(marker) else setattr(strategy, "lifecycle_state", SetupState.POSITION_OPEN)
                    elif kind in ("pending", "queued"):
                        strategy.lifecycle_state = SetupState.ORDER_PENDING
                    elif kind in ("rejected", "error"):
                        strategy.lifecycle_state = SetupState.BLOCKED
                except Exception:  # noqa: BLE001 — strategy telemetry is non-load-bearing
                    pass
        elif skip_entry_scan and callable(getattr(strategy, "decision_report", None)):
            strategy_decision = strategy.decision_report()
            outcome = {"kind": "strategy_decision", "strategy_decision": strategy_decision}
        elif callable(getattr(strategy, "decision_report", None)):
            try:
                strategy_decision = strategy.decision_report()
                outcome = {"kind": "strategy_decision", "strategy_decision": strategy_decision}
                self.ledger.log(
                    level="info", stage="strategy_decision", symbol=sym,
                    message=(f"{getattr(strategy, 'label', 'Strategy')} "
                             f"{strategy_decision.get('decision')}: "
                             f"{strategy_decision.get('reason')}")[:500])
            except Exception:  # noqa: BLE001 — decision telemetry never blocks the bar
                pass
        trade_taken = bool(
            filled_this_bar
            or exit_taken
            or (position_before_exit is not None and position_after_exit is None)
            or (outcome or {}).get("kind") in {"opened", "closed"}
        )
        if trade_taken:
            blocker = None
        elif outcome is not None and outcome.get("blocker"):
            blocker = str(outcome["blocker"])
        else:
            kind = str((outcome or {}).get("kind") or "")
            reason = str((outcome or {}).get("reason") or "")
            if kind == "rejected":
                blocker = gate_blocker(str(outcome.get("stage") or "unknown"), reason)
            elif kind == "pending":
                blocker = "GATE_REJECTED: ORDER_PENDING"
            elif kind == "queued":
                blocker = "GATE_REJECTED: APPROVAL_REQUIRED"
            elif kind == "signal":
                blocker = "GATE_REJECTED: SIGNALS_ONLY"
            elif kind == "hold":
                blocker = "GATE_REJECTED: POSITION_ALREADY_ALIGNED"
            elif kind == "error":
                blocker = "GATE_REJECTED: PIPELINE_ERROR"
            elif skip_entry_scan:
                blocker = "GATE_REJECTED: POSITION_MANAGED"
            else:
                decision_reason = str((strategy_decision or {}).get("reason") or "")
                blocker = (gate_blocker("strategy", decision_reason)
                           if any(word in decision_reason.upper()
                                  for word in ("WARM", "STALE", "R:R", "RR ", "REWARD"))
                           else "GATE_REJECTED: NO_SETUP")
        self.last_blocker = blocker
        self.last_blocker_timestamp = bar.timestamp.isoformat()
        if outcome is None:
            outcome = {"kind": "no_trade", "blocker": blocker}
        else:
            outcome["blocker"] = blocker
        if self.core_v2_observer is not None:
            try:
                self.core_v2_observer.observe(
                    symbol=sym, timeframe=self.timeframe,
                    bars=list(getattr(strategy, "bars", []) or []),
                    as_of=bar.timestamp, source=self.last_source or "paper-engine",
                    signal=signal, strategy_id=self.strategy_label,
                    risk_context=(self.pipeline._risk_context({
                        "symbol": sym, "side": "BUY" if signal and signal.type == SignalType.LONG else "SELL",
                        "entry": signal.entry if signal else bar.close,
                        "stop": signal.stop_loss if signal else None,
                        "target": signal.take_profit if signal else None,
                        "confidence": getattr(signal, "confidence", 1.0) if signal else 1.0,
                        "timestamp": bar.timestamp.isoformat(),
                    }) if signal is not None else None),
                    risk_engine=self.pipeline.risk_engine)
            except Exception as exc:  # noqa: BLE001 — shadow never blocks paper trading
                self.ledger.log(level="warning", stage="core_v2", symbol=sym,
                                message=f"V2 shadow observation failed: {type(exc).__name__}: {exc}")
        # 4. Explainable Trading: one complete Decision Report per cycle —
        #    including WAIT candles — so the bot never acts (or holds back)
        #    silently. Reporting must never affect trading.
        if self.reports is not None:
            try:
                from services.explain import build_cycle_report
                report = build_cycle_report(
                    symbol=sym, timeframe=self.timeframe,
                    bars=list(getattr(strategy, "bars", []) or []),
                    signal=signal, outcome=outcome,
                    position=self.paper.open_position(sym),
                    session=(self.pipeline.session_start, self.pipeline.session_end,
                             self.pipeline.trading_days_mask))
                report.update({
                    "instance_id": self.instance_id or "",
                    "strategy_version": self.strategy_version or "",
                    "decision_identity": decision_identity,
                })
                from services.mtf_policy import material_evidence
                report["mtf_evidence"] = material_evidence(
                    getattr(strategy, "_native_mtf_evidence", {}) or {})
                self.reports.record(report)
            except Exception as e:  # noqa: BLE001 — never block the engine
                print(f"[explain] cycle report failed for {sym}: {type(e).__name__}: {e}")
        return blocker

    def _check_pending(self, sym: str, bar) -> bool:
        # A paused/rebooting worker may keep consuming market data so open
        # positions remain observable, but a persisted pending order must not
        # age, disappear or fill while the execution gate is closed. It will be
        # reconciled and monitored again after the gate is explicitly resumed.
        controls = getattr(self.pipeline, "controls", None)
        if controls is not None and not controls.trading_allowed():
            return False
        po = self._pending.get(sym)
        if po is None:
            return False
        if self.paper.open_position(sym) is not None:
            self._pending.pop(sym, None)
            self._checkpoint_pending_orders()
            return False
        long = po["side"] == "BUY"
        fill = None
        if long and bar.open <= po["price"]:
            fill = bar.open                       # gapped through: better fill
        elif long and bar.low <= po["price"]:
            fill = po["price"]
        elif not long and bar.open >= po["price"]:
            fill = bar.open
        elif not long and bar.high >= po["price"]:
            fill = po["price"]
        if fill is None:
            po["ttl"] -= 1
            if po["ttl"] <= 0:
                self._pending.pop(sym, None)
                self.stats_missed_entries += 1
                self.stats["rejections"] += 1
                self.rejection_counts["limit"] = self.rejection_counts.get("limit", 0) + 1
                self._finalize_decision(
                    po.get("decision_id"), final_state="GATE_REJECTED", stage="limit",
                    reason="Limit entry expired unfilled",
                    blocker="GATE_REJECTED: LIMIT_EXPIRED")
                self.ledger.log(level="info", stage="engine", symbol=sym,
                                message=f"{sym}: limit entry expired unfilled @ {po['price']}")
                # grade the miss: did the trade we refused to chase work out?
                if self.counterfactual is not None:
                    try:
                        self.counterfactual.record_veto(
                            symbol=sym, side="long" if long else "short",
                            entry=po["price"], stop=po["payload"].get("stop"),
                            target=po.get("target"), rule="limit-ttl",
                            detail="limit entry expired unfilled",
                            time=bar.timestamp.isoformat())
                    except Exception:  # noqa: BLE001
                        pass
            self._checkpoint_pending_orders()
            return False
        self._pending.pop(sym, None)
        self._checkpoint_pending_orders()
        res = self._route({**po["payload"], "entry": fill, "maker": True,
                           "timestamp": bar.timestamp.isoformat()})
        if res is None:
            return False
        f = res.fill or {}
        if res.accepted and f.get("action") == "opened":
            self.stats["accepted_signals"] += 1
            if po.get("decision_id") is not None and self.decisions is not None:
                try:
                    self.decisions.mark_executed(po["decision_id"])
                except Exception:  # noqa: BLE001
                    pass
            self.stats["trades"] += 1
            self._targets[sym] = po["target"]
            entry, stop = f.get("price") or fill, po["payload"].get("stop")
            if stop and entry and stop != entry:
                from services.trade_manager import ManagedTrade
                self._managed[sym] = ManagedTrade(
                    side="long" if long else "short", entry=entry, stop=stop,
                    target=po["target"], risk=abs(entry - stop))
                self._checkpoint_managed(sym, self._managed[sym])
            return True
        elif not res.accepted:
            self.stats["rejections"] += 1
            stage = str(res.stage or "unknown")
            self.rejection_counts[stage] = self.rejection_counts.get(stage, 0) + 1
            self._finalize_decision(
                po.get("decision_id"), final_state="GATE_REJECTED", stage=stage,
                reason=res.reason, blocker=res.blocker or gate_blocker(res.stage, res.reason))
        return False

    @staticmethod
    def _mfe_mae_r(mt) -> tuple:
        """Lifecycle telemetry in R for a managed trade (None when untracked)."""
        if mt is None or not getattr(mt, "risk", 0):
            return None, None
        sign = 1.0 if mt.side == "long" else -1.0
        mfe_r = round((mt.mfe - mt.entry) * sign / mt.risk, 3)
        mae_r = round((mt.entry - mt.mae) * sign / mt.risk, 3)
        return mfe_r, mae_r

    @staticmethod
    def _managed_dict(mt) -> dict:
        """Stable JSON-safe representation of every stateful management field."""
        return {key: getattr(mt, key) for key in (
            "side", "entry", "stop", "target", "risk", "be", "scaled",
            "best", "age", "mfe", "mae",
        )}

    def _checkpoint_managed(self, sym: str, mt) -> None:
        """Persist protection + lifecycle after each mutation, scoped per instance."""
        if not hasattr(self, "paper"):  # permits pure-state unit construction
            return
        self.paper.update_management(
            sym, stop=mt.stop, target=mt.target,
            management=self._managed_dict(mt))

    def _auto_execution_id(self, sym: str, timestamp, action: str) -> str:
        """Return a permanent key in forward mode and a run key in research.

        Research replay is intentionally repeatable, so replaying the same
        candle in a new experiment must not be rejected by the durable ledger.
        Forward paper execution, however, must never execute a recovered candle
        twice, including after the normal webhook retry window has elapsed.
        """
        if not self.live:
            return f"auto-{sym}-{action}-{next(self._seq)}"
        stamp = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
        scope = getattr(self.ledger, "instance_id", "") or "legacy"
        return f"auto:{scope}:{sym}:{self.timeframe}:{stamp}:{action}"

    def _decision_identity(self, sym: str, timestamp) -> str:
        """Stable identity for one instance strategy evaluation on one candle."""
        stamp = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
        scope = self.instance_id or getattr(self.ledger, "instance_id", "") or "legacy"
        version = self.strategy_version or self.strategy_label or "unversioned"
        return f"{scope}:{version}:{sym}:{self.timeframe}:{stamp}"

    def _check_exit(self, sym: str, bar, strategy=None, *, entry_bar: bool = False) -> bool:
        pos = self.paper.open_position(sym)
        if pos is None:
            self._managed.pop(sym, None)
            return False
        exit_price = why = None
        act = None
        trade_taken = False
        # Hold the adjust lock only over the fast, pure management computation so
        # a manual level change can't tear stop/target/risk mid-read. All IO
        # (scale-out fill, close routing) happens AFTER the lock is released.
        with self._adjust_lock:
            mt = self._managed.get(sym)
            if mt is None:
                mt = self._adopt(sym, pos)
            if mt is not None:
                if entry_bar:
                    # OHLC has no path information. After an intrabar limit fill,
                    # an adverse stop is provably reachable (price crossed entry
                    # on its way there), but a favorable high/low may have happened
                    # before the fill. Never manufacture a same-candle winner.
                    from services.trade_manager import Action
                    adverse = bar.low if mt.side == "long" else bar.high
                    stop_hit = (adverse <= mt.stop if mt.side == "long"
                                else adverse >= mt.stop)
                    act = Action()
                    if stop_hit:
                        gap_through = (bar.open <= mt.stop if mt.side == "long"
                                       else bar.open >= mt.stop)
                        act.exit_price = bar.open if gap_through else mt.stop
                        act.exit_reason = "stop"
                        mt.mae = min(mt.mae, adverse) if mt.side == "long" else max(mt.mae, adverse)
                else:
                    # shared TradeManager: stop/target exits + break-even /
                    # scale-out / trailing when enabled.
                    act = self.trade_manager.on_bar(mt, bar.high, bar.low, bar.close)
                    if act.exit_reason == "stop":
                        gap_through = (bar.open <= mt.stop if mt.side == "long"
                                       else bar.open >= mt.stop)
                        if gap_through:
                            act.exit_price = bar.open
            else:  # no stop on record (e.g. external webhook trade) — legacy
                legacy_stop, legacy_target = pos.get("stop"), self._targets.get(sym)
        if mt is not None and act is not None:
            if act.partial_price is not None:
                fill = self.paper.reduce(symbol=sym, exit_price=act.partial_price,
                                         fraction=self.trade_manager.scale_frac)
                if fill.action == "reduced":
                    trade_taken = True
                    self.ledger.log(level="info", stage="execution", symbol=sym,
                                    message=f"{sym} scale-out {fill.size:.6f} @ {fill.price}"
                                            f" (PnL {fill.pnl:+.2f})")
            if act.exit_price is None:
                self._checkpoint_managed(sym, mt)
            if act.exit_price is not None:
                exit_price = act.exit_price
                why = ({"stop": "stop-loss", "target": "take-profit",
                        "time": "time-stop"}.get(act.exit_reason, act.exit_reason))
        elif mt is None:  # no stop on record (e.g. external webhook trade) — legacy
            stop = legacy_stop
            target = None if entry_bar else legacy_target
            if pos["side"] == "long":
                if stop and bar.low <= stop:
                    exit_price, why = stop, "stop-loss"
                elif target and bar.high >= target:
                    exit_price, why = target, "take-profit"
            else:
                if stop and bar.high >= stop:
                    exit_price, why = stop, "stop-loss"
                elif target and bar.low <= target:
                    exit_price, why = target, "take-profit"
        # strategy-defined exit conditions (custom builder: indicator / AI exit).
        # Only strategies that implement should_exit participate — built-ins don't.
        if exit_price is None and strategy is not None:
            should = getattr(strategy, "should_exit", None)
            if callable(should):
                try:
                    reason = should(bar, pos["side"])
                    if reason:
                        exit_price, why = bar.close, reason
                except Exception as exc:
                    raise StrategyExecutionError(
                        f"Strategy exit evaluation failed closed for {sym}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
        if exit_price is not None:
            mfe_r, mae_r = self._mfe_mae_r(mt)
            res = self._route({
                "alert_id": self._auto_execution_id(sym, bar.timestamp, "close"), "symbol": sym,
                "side": "CLOSE", "entry": exit_price, "stop": None,
                "exit_reason": why,          # real exit cause for the journal
                "mfe_r": mfe_r, "mae_r": mae_r,   # lifecycle telemetry
                "timestamp": bar.timestamp.isoformat(),
            })
            if res is not None and res.accepted:
                trade_taken = True
                self.stats["trades"] += 1
                with self._adjust_lock:
                    self._targets.pop(sym, None)
                    self._managed.pop(sym, None)
        return trade_taken

    def _adopt(self, sym: str, pos: dict):
        """Rebuild management state for a position opened before a restart (or
        by a webhook). Needs a stop to define R; returns None without one."""
        from services.trade_manager import ManagedTrade
        raw = pos.get("management_json") or {}
        if isinstance(raw, str):
            try:
                import json
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        stop = pos.get("stop")
        entry = pos.get("entry")
        if stop is None or entry is None:
            return None
        # A break-even stop legitimately equals entry after management has
        # progressed. Its persisted original risk is what makes reconstruction
        # possible; only legacy rows with neither distance nor saved risk remain
        # unmanaged.
        risk = float(raw.get("risk") or abs(entry - stop))
        if risk <= 0:
            return None
        sign = 1.0 if pos["side"] == "long" else -1.0
        target = pos.get("target") or self._targets.get(sym)
        if not target:
            # Compatibility only for positions opened before target persistence
            # existed. Every new position records its exact strategy target.
            target = entry + sign * 3.0 * risk
        allowed = {key: raw[key] for key in (
            "side", "entry", "stop", "target", "risk", "be", "scaled",
            "best", "age", "mfe", "mae",
        ) if key in raw}
        allowed.update({"side": pos["side"], "entry": entry, "stop": stop,
                        "target": target, "risk": risk})
        mt = ManagedTrade(**allowed)
        self._targets[sym] = target
        self._managed[sym] = mt
        return mt

    # ---------------------------------------------------------- manual levels
    def apply_manual_levels(self, symbol: str, *, stop=None,
                            target=None) -> dict:
        """Apply a manual on-chart stop/target change to the LIVE managed
        position for ``symbol`` — the same in-memory state the exit check reads,
        so the engine enforces the new levels on the very next bar. Runs under
        the adjust lock so it can never tear a concurrent on_bar read.

        A manually moved stop redefines the trade's risk (the new stop is now the
        real distance being risked) and clears the auto break-even flag so future
        management, if enabled, references the operator's stop. Both levels and
        the mutable lifecycle state are persisted before this method returns.
        Returns the levels now in force (``{stop, target, side, entry}``)."""
        with self._adjust_lock:
            mt = self._managed.get(symbol)
            if mt is not None:
                if stop is not None:
                    mt.stop = float(stop)
                    new_risk = abs(mt.entry - mt.stop)
                    if new_risk > 0:
                        mt.risk = new_risk
                    mt.be = False
                if target is not None:
                    mt.target = float(target)
                    self._targets[symbol] = float(target)
                self._checkpoint_managed(symbol, mt)
                return {"stop": mt.stop, "target": mt.target,
                        "side": mt.side, "entry": mt.entry}
            # No managed state (adopt needs a stop to define R). Still honour the
            # request: remember the target and report the requested stop, which
            # the caller persists to the ledger for the legacy exit path.
            if target is not None:
                self._targets[symbol] = float(target)
            if hasattr(self, "paper"):
                self.paper.update_management(symbol, stop=stop, target=target)
            return {"stop": (float(stop) if stop is not None else None),
                    "target": self._targets.get(symbol),
                    "side": None, "entry": None}

    def managed_snapshot(self) -> dict:
        """Thread-safe read of the live managed levels per symbol. Used to expose
        the take-profit (memory-only) alongside the persisted stop so the chart
        can draw the REAL levels the engine is enforcing — never invented."""
        with self._adjust_lock:
            out: dict[str, dict] = {}
            for sym, mt in self._managed.items():
                out[sym] = {"stop": getattr(mt, "stop", None),
                            "target": getattr(mt, "target", None),
                            "side": getattr(mt, "side", None),
                            "entry": getattr(mt, "entry", None)}
            for sym, tg in self._targets.items():
                out.setdefault(sym, {"stop": None, "target": None,
                                     "side": None, "entry": None})["target"] = tg
            return out

    def _on_signal(self, sym: str, signal: Signal, strategy=None, *,
                   decision_identity: str | None = None) -> Optional[dict]:
        # The brain re-asserts its view every bar; only act when it CHANGES the
        # position (open from flat, or flip/close an opposite). Holding the same
        # direction is a no-op, so the decision log stays signal — not spam.
        pos = self.paper.open_position(sym)
        if signal.type == SignalType.FLAT and pos is None:
            return {"kind": "rejected", "stage": "execution",
                    "reason": "Flatten signal with no open position",
                    "blocker": "GATE_REJECTED: NO_OPEN_POSITION"}
        desired = ("long" if signal.type == SignalType.LONG else
                   "short" if signal.type == SignalType.SHORT else None)
        if pos is not None and pos["side"] == desired:
            return {"kind": "hold", "blocker": "GATE_REJECTED: POSITION_ALREADY_ALIGNED"}
        self.stats["signals"] += 1
        side = ("BUY" if signal.type == SignalType.LONG else
                "SELL" if signal.type == SignalType.SHORT else "FLATTEN")
        health_factor = self._health_factor(sym) if pos is None else 1.0
        # Preserve the exact per-symbol context that guarded this new entry.
        # It is written into the decision journal only after a fill, never used
        # as a second sizing input and never applied to exits.
        recent_losses = self._symbol_loss_streak(sym) if pos is None else 0
        health_summary = dict(self._strategy_health.get(sym) or {}) if pos is None else {}
        # Decision Brain gate (parity with every backtest): score the setup with
        # the same TradeBrain the simulators use, build the unified decision
        # object, and persist it — accepted OR rejected — BEFORE any trade.
        bars = list(getattr(strategy, "bars", []) or [])
        v = None
        if (self.min_quality_score > 0 and signal.stop_loss and pos is None
                and len(bars) >= 60):
            # TradeBrain already contains a well-defined per-symbol
            # losing-streak penalty/cooldown. Feed it closed paper results so
            # the forward engine follows the same protection as simulations;
            # never use another symbol's results to block this setup.
            v = self._quality_brain.evaluate(
                bars, len(bars) - 1,
                side="long" if signal.type == SignalType.LONG else "short",
                entry=signal.entry, stop=signal.stop_loss,
                target=signal.take_profit, recent_losses=recent_losses,
                native_htf_bars=(getattr(strategy, "_native_mtf_context", {}) or {}).get(
                    ((getattr(strategy, "_native_mtf_evidence", {}) or {}).get("primary") or {}).get(
                        "htf_timeframe"), ()),
                require_native_htf=self.live)
        decision = None
        decision_id = None
        if pos is None:                      # entries only; flips/closes pass through
            from services.decision_gate import build_decision
            decision = build_decision(
                symbol=sym, timeframe=self.timeframe, strategy=self.strategy_label,
                side="long" if signal.type == SignalType.LONG else "short",
                confidence=getattr(signal, "confidence", None), verdict=v,
                min_score=self.min_quality_score,
                regime_hint=getattr(signal, "regime", ""))
            # InstanceLedger carries the persisted ownership boundary.  Keep
            # decision history in that same boundary so the UI never credits a
            # BTC decision from another strategy/instance to this worker.
            instance_id = getattr(self.ledger, "instance_id", "")
            if instance_id:
                decision["instance_id"] = instance_id
            decision["ts"] = signal.timestamp.isoformat()
            decision["decision_identity"] = (
                decision_identity or self._decision_identity(sym, signal.timestamp))
            if self.decisions is not None:
                try:
                    decision_id = self.decisions.record(decision)
                except Exception:  # noqa: BLE001 — persistence must never block trading
                    pass
        if decision is not None and decision["decision"] == "rejected":
            self.stats["rejections"] += 1
            self.rejection_counts["quality"] = self.rejection_counts.get("quality", 0) + 1
            why = decision["reason"]
            self._finalize_decision(decision_id, final_state="GATE_REJECTED",
                                    stage="brain", reason=why,
                                    blocker=gate_blocker("brain", why))
            self.ledger.log(level="info", stage="brain", symbol=sym,
                            message=f"{sym} {side} blocked by decision gate: {why}")
            self._record_skipped_decision(
                sym=sym, side=side, signal=signal, stage="brain", reason=why,
                decision_identity=decision["decision_identity"])
            if self.counterfactual is not None:
                try:
                    self.counterfactual.record_veto(
                        symbol=sym,
                        side="long" if signal.type == SignalType.LONG else "short",
                        entry=signal.entry, stop=signal.stop_loss,
                        target=signal.take_profit, rule="quality-score",
                        detail=why, time=signal.timestamp.isoformat())
                except Exception:  # noqa: BLE001
                    pass
            return {"kind": "rejected", "stage": "brain", "reason": why,
                    "blocker": gate_blocker("brain", why),
                    "decision": decision, "verdict": v}
        # context gate: never long an altcoin against BTC's own trend
        # (cross-asset modifier; OFF unless validated + enabled)
        ctx_why = self.context.gate(sym, "long" if signal.type == SignalType.LONG else "short")
        if ctx_why and pos is None:
            self.stats["rejections"] += 1
            self.rejection_counts["context"] = self.rejection_counts.get("context", 0) + 1
            self._finalize_decision(decision_id, final_state="GATE_REJECTED",
                                    stage="context", reason=ctx_why,
                                    blocker=gate_blocker("context", ctx_why))
            self.ledger.log(level="info", stage="context", symbol=sym,
                            message=f"{sym} {side} blocked: {ctx_why}")
            self._record_skipped_decision(
                sym=sym, side=side, signal=signal, stage="context", reason=ctx_why,
                decision_identity=(decision_identity
                                   or self._decision_identity(sym, signal.timestamp)))
            if self.counterfactual is not None:
                try:
                    self.counterfactual.record_veto(
                        symbol=sym,
                        side="long" if signal.type == SignalType.LONG else "short",
                        entry=signal.entry, stop=signal.stop_loss,
                        target=signal.take_profit, rule="context:btc-trend",
                        detail=ctx_why, time=signal.timestamp.isoformat())
                except Exception:  # noqa: BLE001
                    pass
            return {"kind": "rejected", "stage": "context", "reason": ctx_why,
                    "blocker": gate_blocker("context", ctx_why),
                    "decision": decision, "verdict": v}
        payload = {
            "alert_id": self._auto_execution_id(sym, signal.timestamp, side.lower()),
            "symbol": sym, "side": side,
            "entry": signal.entry, "stop": signal.stop_loss,
            "target": signal.take_profit,
            "confidence": getattr(signal, "confidence", 1.0),
            "regime": getattr(signal, "regime", ""),
            "reason": getattr(signal, "reason", ""),
            "context_size_factor": self.context.size_factor(
                sym, "long" if signal.type == SignalType.LONG else "short"),
            "health_size_factor": health_factor,
            "journal_engine": {
                "closed_symbol_loss_streak": recent_losses,
                "strategy_health": health_summary.get("status", "Not evaluated"),
                "strategy_health_sample": health_summary.get("sample", 0),
            },
            # real decision context for the trade journal
            "brain_score": getattr(signal, "brain_score", None),
            "snapshot": getattr(signal, "snapshot", None),
            "brain_checklist": getattr(signal, "checklist", None),
            # The separately-scored TradeBrain verdict is the final quality
            # gate used for this entry. Preserve it with the fill so the trade
            # journal can explain the accepted setup without re-computing from
            # later market data.
            "journal_quality_gate": v.to_dict() if v is not None else None,
            "journal_decision_id": decision_id,
            "strategy": self.strategy_label,
            "timeframe": self.timeframe,
            "mode": "live" if self.live else "paper",
            "open_trades": len(self.paper.positions()),
            "timestamp": signal.timestamp.isoformat(),
            "instance_id": self.instance_id or getattr(self.ledger, "instance_id", "") or "",
            "market_data_source": self.last_source or "",
            "decision_identity": decision_identity or self._decision_identity(sym, signal.timestamp),
        }
        # Maker entry: when FLAT, park a resting limit instead of paying the
        # spread. Flips/closes (opposite side of an open position) stay
        # immediate — exits are never left resting.
        if pos is not None:   # opposite side of an open position -> this routes as a close
            _mfe, _mae = self._mfe_mae_r(self._managed.get(sym))
            payload["mfe_r"], payload["mae_r"] = _mfe, _mae
        # Trading-mode gate: FULL AUTO executes immediately; SEMI-AUTO queues a
        # NEW ENTRY for human approval; SIGNAL records the idea as an alert
        # only. Exits/flips (pos is not None) ALWAYS execute automatically — a
        # human must never have to approve getting OUT of a position.
        if pos is None and self.trading_mode in ("semi", "signal") and self.approvals is not None:
            # dedup: one ongoing setup shouldn't queue a fresh idea every bar
            if self.trading_mode == "semi" and self.approvals.has_pending(sym, side):
                return {"kind": "queued", "idea": None, "duplicate": True}
            idea = self.approvals.create(payload, decision=decision, verdict=v,
                                         mode=self.trading_mode)
            terminal = "APPROVAL_REQUIRED" if self.trading_mode == "semi" else "SIGNALS_ONLY"
            self._finalize_decision(
                decision_id, final_state=terminal, stage="operating_mode",
                reason=("Waiting for explicit operator approval" if self.trading_mode == "semi"
                        else "Signals-only mode does not create orders"),
                blocker=("GATE_REJECTED: APPROVAL_REQUIRED" if self.trading_mode == "semi"
                         else "GATE_REJECTED: SIGNALS_ONLY"))
            self.ledger.log(
                level="info", stage="approval", symbol=sym,
                message=(f"{sym} {side} setup "
                         + ("queued for approval" if self.trading_mode == "semi"
                            else "signalled (alert only)")
                         + f" — idea #{idea['id']}"))
            return {"kind": "queued" if self.trading_mode == "semi" else "signal",
                    "idea": idea, "decision": decision, "verdict": v}
        controls = getattr(self.pipeline, "controls", None)
        if pos is None and controls is not None and not controls.trading_allowed():
            # Do not create a fresh resting order behind a paused/reboot gate.
            # Route once through the pipeline so the blocked decision remains
            # explainable, but keep both durable and in-memory order books
            # unchanged until an operator/health check reopens execution.
            result = self._route(payload)
            self._finalize_decision(
                decision_id, final_state="GATE_REJECTED", stage="controls",
                reason=getattr(result, "reason", "Trading paused — entry blocked"),
                blocker=getattr(result, "blocker", "GATE_REJECTED: PAUSED"))
            return {"kind": "rejected", "stage": "controls",
                    "reason": getattr(result, "reason", "Trading paused — entry blocked"),
                    "blocker": getattr(result, "blocker", "GATE_REJECTED: PAUSED"),
                    "decision": decision, "verdict": v}
        if self.entry_mode == "limit" and pos is None and signal.stop_loss:
            if getattr(self.paper, "deferred_entries", False):
                res = self._route({**payload, "maker": True})
                fill = (res.fill or {}) if res is not None else {}
                if res is not None and res.accepted and fill.get("action") == "intent":
                    self._finalize_decision(
                        decision_id, final_state="PENDING_INTENT", stage="execution",
                        reason="Limit intent awaits a post-decision Binance USD-M quote cross",
                        blocker="GATE_REJECTED: ORDER_PENDING",
                    )
                    return {"kind": "pending", "fill": fill,
                            "blocker": "GATE_REJECTED: ORDER_PENDING",
                            "decision": decision, "verdict": v}
                if res is not None and not res.accepted:
                    self.stats["rejections"] += 1
                    stage = str(res.stage or "execution")
                    self.rejection_counts[stage] = self.rejection_counts.get(stage, 0) + 1
                    self._finalize_decision(
                        decision_id, final_state="GATE_REJECTED", stage=stage,
                        reason=res.reason, blocker=getattr(res, "blocker", None),
                    )
                    return {"kind": "rejected", "stage": stage,
                            "reason": res.reason, "decision": decision, "verdict": v}
                return {"kind": "error", "reason": "forward-paper intent routing failed"}
            self._pending[sym] = {"side": side, "price": signal.entry,
                                  "target": signal.take_profit,
                                  "ttl": self.limit_ttl_bars, "payload": payload,
                                  "decision_id": decision_id}
            self._checkpoint_pending_orders()
            self._finalize_decision(
                decision_id, final_state="PENDING_INTENT", stage="limit",
                reason=f"Resting limit awaiting fill for at most {self.limit_ttl_bars} closed candles",
                blocker="GATE_REJECTED: ORDER_PENDING")
            return {"kind": "pending", "blocker": "GATE_REJECTED: ORDER_PENDING",
                    "decision": decision, "verdict": v}
        res = self._route(payload)
        if res is None:
            self._finalize_decision(decision_id, final_state="GATE_REJECTED",
                                    stage="pipeline", reason="pipeline error (see engine log)",
                                    blocker="GATE_REJECTED: PIPELINE_ERROR")
            return {"kind": "error", "reason": "pipeline error (see engine log)"}
        fill = res.fill or {}
        if res.accepted and fill.get("action") == "intent":
            self._finalize_decision(
                decision_id, final_state="PENDING_INTENT", stage="execution",
                reason="Market intent awaits the next Binance USD-M public quote",
                blocker="GATE_REJECTED: ORDER_PENDING",
            )
            return {"kind": "pending", "decision": decision, "verdict": v,
                    "fill": fill, "blocker": "GATE_REJECTED: ORDER_PENDING"}
        if res.accepted and fill.get("action") == "opened":
            self.stats["accepted_signals"] += 1
            if decision_id is not None and self.decisions is not None:
                try:
                    self.decisions.mark_executed(decision_id)
                except Exception:  # noqa: BLE001
                    pass
            self.stats["trades"] += 1
            self._targets[sym] = signal.take_profit
            entry, stop = fill.get("price") or signal.entry, signal.stop_loss
            if stop and entry and stop != entry:
                from services.trade_manager import ManagedTrade
                self._managed[sym] = ManagedTrade(
                    side="long" if signal.type == SignalType.LONG else "short",
                    entry=entry, stop=stop, target=signal.take_profit,
                    risk=abs(entry - stop))
                self._checkpoint_managed(sym, self._managed[sym])
            return {"kind": "opened", "decision": decision, "verdict": v, "fill": fill}
        elif res.accepted and fill.get("action") == "closed":
            self.stats["trades"] += 1
            self._targets.pop(sym, None)
            self._managed.pop(sym, None)
            return {"kind": "closed", "fill": fill}
        elif not res.accepted:
            self.stats["rejections"] += 1
            stage = str(res.stage or "unknown")
            self.rejection_counts[stage] = self.rejection_counts.get(stage, 0) + 1
            self._finalize_decision(
                decision_id, final_state="GATE_REJECTED", stage=stage,
                reason=res.reason, blocker=res.blocker or gate_blocker(res.stage, res.reason))
            return {"kind": "rejected", "stage": res.stage, "reason": res.reason,
                    "blocker": res.blocker or gate_blocker(res.stage, res.reason),
                    "decision": decision, "verdict": v}
        return {"kind": "noop", "decision": decision, "verdict": v}

    def _finalize_decision(self, decision_id: int | None, *, final_state: str,
                           stage: str, reason: str, blocker: str = "") -> None:
        if decision_id is None or self.decisions is None:
            return
        try:
            self.decisions.finalize(
                decision_id, final_state=final_state, gate_stage=stage,
                reason=reason, blocker=blocker)
        except Exception as exc:  # noqa: BLE001 -- telemetry cannot change execution
            self.ledger.log(
                level="warning", stage="decision_store",
                message=f"Could not finalize decision {decision_id}: {type(exc).__name__}: {exc}")

    def _record_skipped_decision(self, *, sym: str, side: str, signal,
                                 stage: str, reason: str,
                                 decision_identity: str) -> None:
        """Persist one strategy-level veto without allowing telemetry to trade."""
        store = getattr(self.pipeline, "skipped", None)
        if store is None:
            return
        try:
            store.record(
                symbol=sym, side=side, stage=stage, reason=reason,
                entry=signal.entry, stop=signal.stop_loss,
                target=signal.take_profit, strategy=self.strategy_label,
                timeframe=self.timeframe,
                snapshot=getattr(signal, "snapshot", None) or {},
                instance_id=self.instance_id or "",
                decision_identity=decision_identity,
            )
        except Exception:  # noqa: BLE001 — evidence must never change execution
            pass

    def _health_factor(self, sym: str) -> float:
        """Return a bounded, evidence-based new-entry risk multiplier.

        Health is assessed per symbol from closed paper trades. The monitor has
        its own minimum-sample protection, so a short record leaves risk
        unchanged. This is telemetry plus a defensive throttle, not a strategy
        optimiser and never a source of leverage.
        """
        if not self.strategy_health_guard:
            return 1.0
        try:
            from services.strategy_health import StrategyHealthMonitor, entry_risk_factor
            trades = [{**t, "r": (t.get("rr") if t.get("rr") is not None else 0.0)}
                      for t in self.paper.history() if t.get("symbol") == sym]
            health = StrategyHealthMonitor().evaluate(trades)
            factor = entry_risk_factor(health)
            summary = {"status": health.status, "factor": factor,
                       "sample": health.recent.n, "warnings": [w.detail for w in health.warnings]}
            prior = self._strategy_health.get(sym)
            self._strategy_health[sym] = summary
            if prior != summary and factor < 1.0:
                self.ledger.log(level="warning", stage="strategy_health", symbol=sym,
                                message=(f"{sym}: {health.status} paper record — new-entry risk "
                                         f"reduced to {factor * 100:.0f}% (sample {health.recent.n})"))
            return factor
        except Exception as exc:  # noqa: BLE001 — health telemetry cannot stop paper trading
            self.ledger.log(level="warning", stage="strategy_health", symbol=sym,
                            message=f"{sym}: health guard unavailable ({type(exc).__name__}); no risk change")
            return 1.0

    def _symbol_loss_streak(self, sym: str) -> int:
        """Return the current closed-paper loss streak for one symbol.

        The quality gate is intentionally scoped to the instrument that
        produced the losses. A BTC sequence must not suppress an independent
        ETH setup, and open/unrealised positions do not count. On a transient
        ledger read failure we fail open here; the existing risk pipeline
        remains responsible for its separate account-wide kill switches.
        """
        try:
            from risk.daily_limits import consecutive_losses
            trades = [t for t in self.paper.history() if t.get("symbol") == sym]
            return consecutive_losses(trades)
        except Exception as exc:  # noqa: BLE001 — telemetry must not break paper execution
            self.ledger.log(level="warning", stage="brain", symbol=sym,
                            message=(f"{sym}: closed-trade streak unavailable "
                                     f"({type(exc).__name__}); quality gate has no streak input"))
            return 0

    def execute_approved(self, idea: dict) -> dict:
        """Route a HUMAN-APPROVED trade idea through the SAME risk pipeline a
        full-auto entry uses, then set up trade management. All risk gates still
        apply — approval bypasses nothing."""
        payload = dict(idea.get("_payload") or {})
        sym = payload.get("symbol")
        if not sym:
            return {"ok": False, "reason": "idea has no symbol"}
        payload["alert_id"] = f"approved-{sym}-{next(self._seq)}"  # fresh id (dedup)
        payload["approved"] = True
        res = self._route(payload)
        if res is None:
            return {"ok": False, "reason": "pipeline error (see engine log)"}
        fill = res.fill or {}
        if res.accepted and fill.get("action") == "opened":
            self.stats["trades"] += 1
            entry = fill.get("price") or payload.get("entry")
            stop = payload.get("stop")
            self._targets[sym] = payload.get("target")
            if stop and entry and stop != entry:
                from services.trade_manager import ManagedTrade
                self._managed[sym] = ManagedTrade(
                    side="long" if payload.get("side") == "BUY" else "short",
                    entry=entry, stop=stop, target=payload.get("target"),
                    risk=abs(float(entry) - float(stop)))
                self._checkpoint_managed(sym, self._managed[sym])
            self.ledger.log(level="info", stage="approval", symbol=sym,
                            message=f"Approved {sym} {payload.get('side')} entry executed")
            return {"ok": True, "action": "opened", "fill": fill}
        if not res.accepted:
            self.stats["rejections"] += 1
            return {"ok": False, "reason": res.reason, "stage": getattr(res, "stage", "risk")}
        return {"ok": False, "reason": "no fill", "fill": fill}

    def _route(self, payload: dict):
        try:
            result = self.pipeline.process(payload)
            fill = result.fill or {}
            if result.accepted and fill.get("action") in ("opened", "closed", "reduced"):
                self.last_trade = {
                    "action": fill.get("action"), "symbol": payload.get("symbol"),
                    "side": payload.get("side"), "price": fill.get("price") or payload.get("entry"),
                    "timestamp": payload.get("timestamp"),
                }
            return result
        except Exception as e:
            self.ledger.log(level="error", stage="engine",
                            message=f"Pipeline error on {payload.get('symbol')}: {e}",
                            symbol=payload.get("symbol", ""))
            raise StrategyExecutionError(
                f"Pipeline failed closed for {payload.get('symbol')}: {type(e).__name__}: {e}"
            ) from e


# Approx seconds per candle, to judge whether a live feed has stalled.
# Candle durations come from bot.data.resample — the one definition. This used
# to be a local copy, and six copies of the same fact had already drifted apart.
from bot.data.resample import TF_SECONDS as _TF_SECONDS  # noqa: E402

# Architecture phase 2: the event bus and the event vocabulary. Imported at
# module scope with a guard because `tradexa` sits at the repository root — a
# deployment that ships only `automation-hub` must still start, with events
# simply going nowhere rather than the engine failing to import.
try:
    from tradexa.core import events as _events
    from tradexa.infrastructure.events import default_bus as _event_bus
except Exception:  # noqa: BLE001 - telemetry is optional, the engine is not
    class _events:                      # type: ignore[no-redef]
        class MarketDataReceived:       # noqa: D106
            def __init__(self, **kw): pass
    def _event_bus():                   # type: ignore[misc]
        from types import SimpleNamespace
        return SimpleNamespace(publish=lambda e: None)

def explain_inactivity(*, running: bool, trading_state: str, mode: str, timeframe: str,
                       bars: int, signals: int, trades: int, rejections: int,
                       data_source: Optional[str], last_activity_age_s: Optional[float],
                       feed_error: Optional[str] = None) -> dict:
    """Plain-English answer to 'why isn't the bot trading?'. Pure + testable.

    Returns {status, headline, detail, severity}. ``status`` is a stable code the
    UI can switch on; ``severity`` is info|warning|critical.
    """
    def out(status, headline, detail, severity="info"):
        return {"status": status, "headline": headline, "detail": detail, "severity": severity}

    if not running:
        return out("stopped", "The engine is not running.",
                   "Start it from the Paper Trading page to begin scanning the market.", "warning")
    if trading_state and trading_state.lower() != "active":
        return out("halted", f"Trading is {trading_state}.",
                   "New entries are blocked. Resume from Risk Manager / Safety Center.", "warning")

    tf_s = _TF_SECONDS.get(timeframe, 3600)
    # The precise feed diagnosis must come BEFORE the generic bars==0 message —
    # a fallen-back live feed keeps bars at 0 forever, and hiding that behind
    # "just started warming up" is exactly how it goes unnoticed.
    if mode == "live" and data_source and not str(data_source).startswith("live"):
        err = f" Last fetch error: {feed_error}." if feed_error else ""
        return out("stale_feed", "Running — data feed FAILED (static fallback active).",
                   (f"Live data is enabled but every fetch failed, so it fell back to "
                    f"'{data_source}' (static historical data).{err} Exchanges like Binance "
                    f"block many cloud/datacenter IPs (HTTP 451), so no NEW {timeframe} candle "
                    f"will ever arrive — bars stay 0 and no trades can fire. Fix: set "
                    f"HUB_EXCHANGE=kraken (or coinbase) and redeploy, or unset "
                    f"HUB_USE_LIVE_DATA to run replay mode instead."), "critical")
    if bars == 0:
        if mode == "live" and str(data_source or "").startswith("live"):
            hrs = tf_s / 3600
            wait = (f"up to {hrs:.0f} hours" if tf_s >= 3600
                    else f"up to {tf_s // 60} minutes")
            return out("waiting_first_candle", "Running — data feed connected, waiting for the first candle.",
                       (f"The live feed is healthy; the engine acts only when a {timeframe} candle "
                        f"CLOSES, which can take {wait} from now. For faster activity during "
                        f"testing, set HUB_AUTO_TIMEFRAME=5m or 15m and redeploy."), "info")
        return out("no_data", "No market data has been processed yet.",
                   "The data feed may be unavailable, or the engine just started warming up.", "warning")
    if mode == "live" and last_activity_age_s is not None and last_activity_age_s > 1.5 * tf_s:
        hrs = last_activity_age_s / 3600
        return out("waiting_candles", f"Waiting for the next {timeframe} candle.",
                   (f"The last new candle was ~{hrs:.1f}h ago. On {timeframe} there are only a few "
                    f"candles per day, so trades are infrequent by design. Use a lower timeframe "
                    f"(e.g. 15m/1h) for more activity."), "info")
    if signals == 0 and bars > 0:
        return out("no_setup", f"Scanned {bars} bars — no valid setup yet.",
                   "The strategy and quality filter are waiting for a high-quality entry. Fewer, "
                   "better trades is the intended behaviour.", "info")
    if trades == 0 and rejections > 0:
        return out("all_blocked", f"Found setups, but all {rejections} were blocked.",
                   "Risk/quality filters rejected every candidate. Check the Logs page for the exact "
                   "reasons (e.g. against higher-timeframe trend, choppy regime, weak reward:risk).", "warning")
    if trades == 0:
        return out("warming_up", "Engine active — no trades yet.",
                   "Still warming up or waiting for the first qualifying setup.", "info")
    return out("active", f"Engine healthy — {trades} trades taken.",
               f"{signals} signals, {rejections} blocked by filters.", "info")
