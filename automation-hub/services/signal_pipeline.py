"""Signal Pipeline (Phase 1) — the brain that turns a TradingView alert into a
paper trade, safely and transparently.

    alert -> [controls] -> [dedup] -> [risk + sizing] -> [paper execution]
          -> ledger (webhook_events, positions, paper_trades, bot_logs, alerts)

Every stage records a decision step (passed/failed + reason) so the Logs page
shows exactly why a trade executed or was rejected. No real broker is touched.
"""
from __future__ import annotations

import threading

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from data.ledger import DuplicateOrderIntent, Ledger
from execution.paper_engine import PaperExecutionEngine, _dir
from tradexa.risk.position_sizing import (
    FIXED_QUANTITY, PositionSizingRequest, PositionSizingService,
    normalize_sizing_mode,
)

# Position-sizing arithmetic (architecture phase 3). Gathering the factors is
# still this object's job — they come from stateful collaborators that live
# here — but composing them into an effective risk is now a pure function with
# its own differential test. Guarded so a deployment shipping only
# automation-hub still starts; the fallback is the identical expression this
# replaced, kept byte-for-byte so the two cannot diverge.
try:
    from tradexa.risk.sizing import (
        RiskFactors as _RiskFactors,
        clamp_context as _clamp_context,
        describe_factors as _describe_factors,
        effective_risk as _effective_risk,
    )
except Exception:  # noqa: BLE001 - pragma: no cover
    from dataclasses import dataclass as _dc

    @_dc(frozen=True)
    class _RiskFactors:  # type: ignore[no-redef]
        confidence: float = 1.0
        kelly: float = 1.0
        equity_curve: float = 1.0
        learned: float = 1.0
        allocator: float = 1.0
        event: float = 1.0
        boost: float = 1.0
        context: float = 1.0
        side: float = 1.0
        streak: float = 1.0

        @property
        def throttling(self):
            return min(self.kelly, self.equity_curve, self.learned, self.event)

    def _clamp_context(v):  # type: ignore[misc]
        try:
            return max(0.5, min(1.0, float(v if v is not None else 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def _effective_risk(base, f):  # type: ignore[misc]
        r = base * (0.5 + 0.5 * f.confidence)
        r *= (f.kelly * f.equity_curve * f.learned * f.allocator * f.event
              * f.boost * f.context * f.side * f.streak)
        return r

    def _describe_factors(f):  # type: ignore[misc]
        parts = [f"conf {f.confidence:.2f}"]
        for attr, label, ok in (("kelly", "kelly", lambda v: v < 1.0),
                                ("equity_curve", "curve", lambda v: v < 1.0),
                                ("learned", "learned", lambda v: v < 1.0),
                                ("allocator", "alloc", lambda v: v != 1.0),
                                ("event", "event", lambda v: v < 1.0),
                                ("boost", "edge", lambda v: v > 1.0),
                                ("context", "context", lambda v: v < 1.0),
                                ("side", "side", lambda v: v < 1.0),
                                ("streak", "streak", lambda v: v < 1.0)):
            v = getattr(f, attr)
            if ok(v):
                parts.append(f"× {label} {v:.2f}")
        return " ".join(parts)

# The standalone Risk Engine is a MANDATORY VETO. The guarded import preserves
# a diagnostic sentinel, but SignalPipeline construction fails closed when the
# package is absent; execution is never allowed to continue without it.
try:
    from tradexa.risk import (
        AccountState as _AccountState,
        Direction as _RiskDirection,
        MarketConditions as _MarketConditions,
        OpenPosition as _OpenPosition,
        PIPELINE_PARITY as _PIPELINE_PARITY,
        RiskContext as _RiskContext,
        RiskEngine as _RiskEngine,
        TradeProposal as _TradeProposal,
    )
except Exception:  # noqa: BLE001 - pragma: no cover
    _RiskEngine = None  # type: ignore[assignment]

from services.controls import TradingControl
from services.dedup import DuplicateGuard
from services.market_quality import MarketQualityGate

# 2024-01-01 is a Monday. The session and trading-day rules need a datetime,
# but the pipeline decides those from the ALERT's timestamp, not the wall
# clock — so the instant handed to the engine is synthesised from the same
# weekday and hour the pipeline's own gates used. Handing it `now()` instead
# would let the two disagree about what day it is on a replayed alert.
_MONDAY = datetime(2024, 1, 1, tzinfo=timezone.utc)


@dataclass
class Step:
    rule: str
    passed: bool
    detail: str = ""


@dataclass
class PipelineResult:
    accepted: bool
    stage: str
    reason: str
    steps: list[Step] = field(default_factory=list)
    fill: Optional[dict] = None
    blocker: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted, "stage": self.stage, "reason": self.reason,
            "steps": [s.__dict__ for s in self.steps],
            "fill": self.fill,
            "blocker": self.blocker,
        }


_CLOSE_SIDES = {"REDUCE", "CLOSE", "EXIT", "FLAT", "FLATTEN"}


def gate_blocker(stage: str, reason: str = "") -> str:
    """Stable operator-facing rejection code for every pipeline veto."""
    text = str(reason or "").upper()
    if "REWARD" in text or "R:R" in text or "RR " in text or "RR_" in text:
        code = "INSUFFICIENT_RR"
    elif "WARM" in text or "INSUFFICIENT HISTORY" in text:
        code = "WARMUP"
    elif "STALE" in text:
        code = "STALE_CANDLE"
    else:
        code = {
            "controls": "PAUSED",
            "market_quality": "MARKET_QUALITY",
            "dedup": "DUPLICATE_SIGNAL",
            "execution": "EXECUTION",
            "risk": "INVALID_RISK",
            "risk_guard": "RISK_LIMIT",
            "session": "OUTSIDE_SESSION",
            "trading_day": "TRADING_DAY_DISABLED",
            "daily_loss": "DAILY_LOSS_LIMIT",
            "weekly_loss": "WEEKLY_LOSS_LIMIT",
            "cooldown": "LOSS_COOLDOWN",
            "max_trades": "TRADE_LIMIT",
            "correlation": "CORRELATED_EXPOSURE",
            "portfolio_exposure": "PORTFOLIO_EXPOSURE",
            "event_risk": "EVENT_BLACKOUT",
        }.get(str(stage or "").lower(), str(stage or "UNKNOWN").upper())
    return f"GATE_REJECTED: {code}"

# Correlation clusters: assets that move together. Crypto majors are treated as
# ONE cluster — three simultaneous longs on BTC/ETH/SOL are not diversification,
# they are one 3x-sized bet on the same market. Extend the map as new asset
# classes are added.
_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH")


def _cluster(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "").replace("-", "")
    if s.endswith(_CRYPTO_QUOTES):
        return "crypto"
    return "other"


class SignalPipeline:
    def __init__(
        self,
        ledger: Ledger,
        paper: PaperExecutionEngine,
        controls: TradingControl,
        *,
        equity: float = 10_000.0,
        risk_per_trade_pct: float = 0.01,
        exposure_limit_pct: float = 0.05,
        dedup_window_s: int = 300,
        quality: Optional[MarketQualityGate] = None,
        max_drawdown_pct: float = 0.20,
        max_open_positions: int = 3,
        max_daily_loss_pct: float = 0.0,
        session_start: int = 0,
        session_end: int = 24,
        max_weekly_loss_pct: float = 0.0,
        max_trades_per_day: int = 0,
        max_consecutive_losses: int = 0,
        cooldown_after_loss_min: int = 0,
        trading_days_mask: int = 127,
        adaptive_risk: bool = True,
        max_correlated_positions: int = 2,
        max_total_exposure_pct: float = 0.10,
        equity_throttle: bool = True,
        position_sizing_mode: str = "auto",
        fixed_position_size: float = 0.0,
        equity_provider: Optional[Callable[[], float]] = None,
        profit_reinvestment: bool = False,
        maximum_risk_amount: Optional[float] = None,
        minimum_equity: Optional[float] = None,
    ):
        self.ledger = ledger
        self.paper = paper
        self.controls = controls
        self.equity = equity
        self.starting_equity = float(equity)
        self.equity_provider = equity_provider
        self.risk_per_trade_pct = risk_per_trade_pct
        self.exposure_limit_pct = exposure_limit_pct
        # Optional account-wide gate supplied by TradingInstanceManager. It is
        # evaluated after instance sizing/caps but before paper execution.
        self.global_entry_guard = None
        # Optional venue rule resolver supplied by TradingInstanceManager.
        # It returns bot.brokers.symbol_rules.SymbolRules and is evaluated only
        # for a real entry candidate, after account caps but before execution.
        self.symbol_rules_provider = None
        raw_sizing_mode = str(position_sizing_mode or "auto")
        # Keep the legacy global settings API's public auto/fixed values stable.
        # Trading Instances pass and persist canonical Stage 3 values.
        self.position_sizing_mode = (raw_sizing_mode if raw_sizing_mode in ("auto", "fixed")
                                     else normalize_sizing_mode(raw_sizing_mode))
        self.fixed_position_size = max(0.0, float(fixed_position_size))
        self.profit_reinvestment = bool(profit_reinvestment)
        self.maximum_risk_amount = (float(maximum_risk_amount)
                                    if maximum_risk_amount is not None else None)
        self.minimum_equity = float(minimum_equity) if minimum_equity is not None else None
        self.dedup = DuplicateGuard(ledger, dedup_window_s)
        # Fail-closed pre-trade safety gate (default = strong defaults).
        self.quality = quality or MarketQualityGate()
        # Automatic capital protection: a drawdown circuit breaker (halts new
        # entries, never exits) + a cap on concurrent positions.
        self.max_drawdown_pct = max_drawdown_pct
        self.max_open_positions = max_open_positions
        # Daily-loss kill switch (resets each UTC day) + trading-session window.
        self.max_daily_loss_pct = max_daily_loss_pct
        self.session_start = session_start
        self.session_end = session_end
        self.max_weekly_loss_pct = max_weekly_loss_pct
        self.max_trades_per_day = max_trades_per_day
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_after_loss_min = cooldown_after_loss_min
        self.trading_days_mask = trading_days_mask
        # Kelly-capped adaptive sizing: risk less when the recent record is weak.
        self.adaptive_risk = adaptive_risk
        # Anti-martingale streak scaling: half risk after 2 consecutive losses,
        # quarter risk after 4; the next win restores full size. Reacts
        # immediately (the Kelly guard needs a window of trades to move) and
        # only ever REDUCES risk — it can never size up.
        self.streak_risk_scaling = True
        # H-4: serialize the whole signal->risk->open path. The engine
        # thread and concurrent webhook POSTs both call process(); the
        # position-cap / existing-position checks are separated from the
        # open, so without this two signals for one symbol could both pass
        # the checks and double-open past the cap.
        self._proc_lock = threading.RLock()
        # Portfolio-level risk: correlated same-direction positions look like
        # diversification but are one oversized bet; total notional is capped
        # across ALL open positions; the equity-curve throttle halves risk while
        # the bot trades below its own recent equity average.
        self.max_correlated_positions = max_correlated_positions
        self.max_total_exposure_pct = max_total_exposure_pct
        self.equity_throttle = equity_throttle
        # Optional notification hook: callable(kind, title, detail). Best-effort.
        self.notifier = None
        # Self-learning loop (services/learning.LearningBook). When attached,
        # learned corrections gate entries + scale risk, and every close
        # triggers a re-learn from the bot's own record.
        self.learning = None
        self._alert_info: dict[str, dict] = {}   # alert_id -> confidence/regime
        self._alert_info_hydrated = False        # rebuilt from the ledger once
        # Event-risk gate: callable returning upcoming econ events. Blackout
        # halts new entries; caution halves size (exits are never blocked).
        self.econ_events = None
        # Allocator tilt: callable(symbol) -> size multiplier from the live
        # per-symbol record (evidence-only, capped — see services/allocator.py).
        self.allocator = None
        # Counterfactual tracker: every meaningful veto is followed as a
        # virtual trade so each rule gets graded by what it actually blocked.
        self.counterfactual = None
        # Decision journal: the full explainable record of every trade.
        self.journal = None
        # Skipped-trade log: every rejected setup with its failed gate + snapshot.
        self.skipped = None
        # Permanent trade memory: composes the closed trade into a forever record.
        self.trade_memory = None
        # Server-owned execution provenance captured with every journal entry.
        # Instance workers populate this; clients cannot override it.
        self.journal_context: dict[str, object] = {}
        self._halted = False
        self._halt_reason = ""
        # Drawdown is measured from this baseline; a manual Resume rebaselines to
        # the current equity so the same loss doesn't immediately re-halt.
        self._dd_base_balance = paper.starting_balance
        self._dd_base_count = 0
        # Every trade passes through the Risk Engine before execution. Built
        # from THIS pipeline's own configured limits, so an operator who
        # tightens a cap tightens both paths at once and they cannot drift.
        self.risk_engine = self._build_risk_engine()

    # ------------------------------------------------------------ risk engine
    def _build_risk_engine(self):
        """Build the mandatory engine that vetoes every entry.

        Limits are derived from the pipeline's own settings on top of
        ``PIPELINE_PARITY``, which disables the rules this pipeline has never
        enforced (account risk, leverage, margin, news, volatility ceiling).
        That is what makes the veto safe to add to a live path: it is a SUBSET
        of the checks already applied, so it cannot refuse a trade that used to
        pass. Turning the extra rules on is a separate, deliberate decision —
        see ``tradexa.risk.STRICT``.
        """
        if _RiskEngine is None:
            raise RuntimeError(
                "tradexa.risk is unavailable; refusing to construct an execution "
                "pipeline without its mandatory risk veto"
            )
        limits = _PIPELINE_PARITY.with_(
            risk_per_trade_pct=self.risk_per_trade_pct,
            max_risk_per_trade_pct=max(self.risk_per_trade_pct,
                                       _PIPELINE_PARITY.max_risk_per_trade_pct),
            max_drawdown_pct=self.max_drawdown_pct,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_weekly_loss_pct=self.max_weekly_loss_pct,
            max_open_positions=self.max_open_positions,
            max_correlated_positions=self.max_correlated_positions,
            max_position_exposure_pct=self.exposure_limit_pct,
            max_total_exposure_pct=self.max_total_exposure_pct,
            session_start_hour=self.session_start,
            session_end_hour=self.session_end,
            trading_days_mask=self.trading_days_mask,
        )
        return _RiskEngine(limits)

    def _risk_context(self, *, symbol: str, side: str, entry: float, stop: float,
                      confidence: float, payload: dict, equity: Optional[float] = None):
        """Assemble what the engine reads. Every figure comes from the same
        source the corresponding pipeline gate uses — the daily P&L from
        ``_today_pnl``, the exposure from ``paper.positions()`` — so the two
        cannot disagree about the state they are judging.
        """
        positions = tuple(
            _OpenPosition(
                symbol=p["symbol"],
                direction=(_RiskDirection.LONG if p["side"] == "long"
                           else _RiskDirection.SHORT),
                qty=float(p.get("size") or 0.0),
                entry=float(p.get("entry") or 0.0),
                stop=(float(p["stop"]) if p.get("stop") is not None else None),
                cluster=_cluster(p["symbol"]),
            )
            for p in self.paper.positions())
        wd = self._entry_weekday(payload.get("timestamp"))
        hour = self._entry_hour(payload.get("timestamp"))
        return _RiskContext(
            proposal=_TradeProposal(
                symbol=symbol,
                direction=(_RiskDirection.LONG if _dir(side) == "long"
                           else _RiskDirection.SHORT),
                entry=entry, stop=stop, target=payload.get("target"),
                confidence=confidence,
                strategy_id=str(payload.get("strategy", "") or "")),
            account=_AccountState(
                equity=float(self._current_realized_equity() if equity is None else equity),
                # The loss windows are measured against the SAME base the
                # pipeline's own gates use (paper.starting_balance), not
                # against current equity — otherwise the same P&L would clear
                # one gate and trip the other.
                starting_equity=self.paper.starting_balance,
                peak_equity=max(self._dd_base_balance, self.paper.balance()),
                cash=self.paper.balance(),
                realized_pnl_today=self._today_pnl(),
                realized_pnl_week=self._week_pnl()),
            positions=positions,
            market=_MarketConditions(cluster=_cluster(symbol)),
            now=_MONDAY + timedelta(days=wd, hours=hour),
            # The pipeline's pause and auto-halt have already rejected by the
            # time this runs; passing them anyway keeps the context truthful
            # rather than describing a bot that is running when it is not.
            kill_switch_engaged=bool(self._halted) or not self.controls.trading_allowed(),
            kill_switch_reason=self._halt_reason or "trading halted")

    def _current_realized_equity(self) -> float:
        """Fresh authoritative realized equity; unrealized P&L is excluded."""
        if self.equity_provider is not None:
            return float(self.equity_provider())
        return float(self.paper.balance())

    def _release_order_claim(self, alert_id: str) -> None:
        """Drop a claim that never became an order.

        A claim is a lock, not a record of something that happened. If the
        entry does not become an order after it, leaving the row would make a
        later legitimate retry of the same candle look like a duplicate and
        suppress a trade that was never placed. The dedup constraint has no
        time component, so nothing ages it out on its own.
        """
        try:
            self.ledger.release_webhook_claim(alert_id)
        except Exception:  # noqa: BLE001 — a stuck claim must not break the cycle
            pass

    def process(self, payload: dict) -> PipelineResult:
        with self._proc_lock:
            return self._process(payload)

    def _process(self, payload: dict) -> PipelineResult:
        # Copy before adding server-owned evidence so one worker cannot mutate a
        # payload later reused by another instance or caller.
        payload = dict(payload)
        payload["journal_execution"] = dict(self.journal_context)
        if payload.get("market_data_source"):
            payload["journal_execution"]["market_data_source"] = payload["market_data_source"]
        symbol = payload["symbol"]
        side = str(payload["side"]).upper()
        entry = float(payload["entry"])
        stop = payload.get("stop")
        stop = float(stop) if stop is not None else None
        alert_id = payload.get("alert_id", "")
        # Optional brain inputs: conviction (scales risk) + human-readable rationale.
        confidence = float(payload.get("confidence", 1.0) or 1.0)
        confidence = max(0.0, min(1.0, confidence))
        brain_reason = str(payload.get("reason", "") or "")
        steps: list[Step] = []

        # Vetoes from these stages are judgment calls worth grading — the
        # counterfactual tracker follows what the blocked trade would have done.
        _GRADED_STAGES = ("learning", "event_risk", "correlation", "risk_guard",
                          "daily_loss", "weekly_loss", "session", "trading_day",
                          "cooldown", "max_trades", "portfolio_exposure")

        def reject(stage: str, reason: str, status: str = "rejected") -> PipelineResult:
            steps.append(Step(stage, False, reason))
            self.ledger.insert_webhook_event(alert_id=alert_id, symbol=symbol, side=side,
                                              entry=entry, stop=stop, payload=payload,
                                              status=status, reason=reason)
            self.ledger.log(level="warning", stage=stage, message=f"{symbol} {side} rejected: {reason}", symbol=symbol)
            self.ledger.add_alert(severity="warning", category="trade",
                                  title=f"Trade rejected — {symbol}", detail=reason)
            # First-class skipped-trade record: the gate that failed + the real
            # market snapshot, so every "no" is explainable and searchable.
            if self.skipped is not None:
                try:
                    self.skipped.record(
                        symbol=symbol, side=side, stage=stage, reason=reason,
                        status=status, entry=entry, stop=stop, target=payload.get("target"),
                        strategy=payload.get("strategy", ""), timeframe=payload.get("timeframe", ""),
                        snapshot=payload.get("snapshot") or {},
                        instance_id=str(payload.get("instance_id") or ""),
                        decision_identity=str(payload.get("decision_identity") or ""))
                except Exception:  # noqa: BLE001 — logging must never block trading
                    pass
            if (self.counterfactual is not None and stage in _GRADED_STAGES
                    and stop and entry and side not in _CLOSE_SIDES):
                rule = stage
                if stage == "learning" and getattr(self.learning, "last_gate_key", None):
                    rule = f"learning:{self.learning.last_gate_key}"
                try:
                    self.counterfactual.record_veto(
                        symbol=symbol, side=_dir(side), entry=entry, stop=stop,
                        target=payload.get("target"), rule=rule, detail=reason,
                        time=payload.get("timestamp") or "")
                except Exception:  # noqa: BLE001 — grading must never block trading
                    pass
            return PipelineResult(False, stage, reason, steps, blocker=gate_blocker(stage, reason))

        # Classify exposure direction before applying entry-only controls. Pause
        # is a soft entry gate, never a veto on a protective/risk-reducing exit.
        existing = self.paper.open_position(symbol)
        is_close = side in _CLOSE_SIDES or (
            existing is not None and _dir(side) != existing["side"]
        )

        # 1. emergency controls
        if not self.controls.trading_allowed() and not is_close:
            return reject("controls", f"Trading {self.controls.state.lower()} — entry blocked")
        steps.append(Step(
            "controls", True,
            (f"risk-reducing exit allowed while trading is {self.controls.state.lower()}"
             if is_close and not self.controls.trading_allowed() else "trading active"),
        ))

        # 1.5 market-quality gate (fail-closed: bad data / untradeable market -> veto)
        if is_close:
            # An entry may wait for ideal data; an existing position may not.
            # Exit pricing/fill validation belongs to the execution adapter.
            steps.append(Step("market_quality", True, "protective exit bypass"))
        else:
            q = self.quality.check(
                entry=entry, stop=stop, timestamp=payload.get("timestamp"),
                bid=payload.get("bid"), ask=payload.get("ask"),
                spread_bps=payload.get("spread_bps"),
            )
            if not q.ok:
                return reject("market_quality", q.reason)
            steps.append(Step("market_quality", True, "data + microstructure ok"))

        # 2. duplicate protection
        if not is_close and self.dedup.is_duplicate(alert_id):
            return reject("dedup", f"Duplicate alert_id within {self.dedup.window_seconds}s", status="duplicate")
        steps.append(Step("dedup", True, "protective exit bypass" if is_close else "no duplicate"))

        # 3a. CLOSE signal (explicit, or opposite side of an open position)
        if is_close:
            if existing is None:
                return reject("execution", "Close signal with no open position")
            # link the closing trade to its open journal before the ledger row closes
            _open_tid = next((t["id"] for t in self.ledger.get_paper_trades()
                              if t["symbol"] == symbol and t["status"] == "open"), None)
            fill = self.paper.close(symbol=symbol, exit_price=entry,
                                    execution_id=alert_id)
            if self.journal is not None and _open_tid:
                try:
                    self.journal.record_exit(
                        # The fill model may move an exit through spread and
                        # slippage. Journal the executed price, never the
                        # requested trigger price.
                        trade_id=_open_tid, exit_price=fill.price, pnl=fill.pnl,
                        instance_id=str(self.journal_context.get("instance_id") or ""),
                        exit_reason=payload.get("exit_reason")
                        or ("opposite-signal" if side not in _CLOSE_SIDES else "manual-close"),
                        mfe_r=payload.get("mfe_r"), mae_r=payload.get("mae_r"))
                except Exception:  # noqa: BLE001 — journaling must never block trading
                    pass
                # commit the now-closed trade to permanent memory (never blocks)
                if self.trade_memory is not None:
                    try:
                        self.trade_memory.remember(_open_tid)
                    except Exception:  # noqa: BLE001 — memory must never block trading
                        pass
            self.ledger.insert_webhook_event(alert_id=alert_id, symbol=symbol, side=side,
                                              entry=entry, stop=stop, payload=payload, status="accepted")
            self.ledger.log(level="info", stage="execution",
                            message=f"{symbol} closed @ {fill.price} (PnL {fill.pnl:+.2f})", symbol=symbol)
            self.ledger.add_alert(severity="info", category="trade",
                                  title=f"Position closed — {symbol}", detail=f"PnL {fill.pnl:+.2f}")
            self._notify("trade", f"📉 {symbol} closed", f"PnL {fill.pnl:+.2f}")
            steps.append(Step("execution", True, f"closed PnL {fill.pnl:+.2f}"))
            # A losing close may breach drawdown -> halt future entries (not exits).
            if not self._halted:
                dd = self._drawdown_trip()
                if dd is not None:
                    self._engage_halt(dd)
            # every closed trade is a datapoint: re-learn from the full record,
            # with counterfactual evidence falsifying rules that block winners
            if self.learning is not None:
                try:
                    costing = (self.counterfactual.costing_rules()
                               if self.counterfactual is not None else None)
                    self.learning.update(self.paper.history(), self.alert_context(),
                                         costing_rules=costing)
                except Exception:  # noqa: BLE001 — learning must never block trading
                    pass
            return PipelineResult(True, "execution", "position closed", steps, fill.__dict__)

        # 3b. OPEN — no pyramiding in Phase 1
        if existing is not None:
            return reject("execution", f"Position already open on {symbol} (no pyramiding)")

        # 3c. portfolio cap — limit concurrent open positions
        if len(self.paper.positions()) >= self.max_open_positions:
            return reject("risk_guard", f"Max open positions ({self.max_open_positions}) reached")

        # 3c2. correlation guard — same-direction positions in the same asset
        # cluster compound into one oversized bet (crypto majors move together)
        if self.max_correlated_positions > 0:
            cluster, direction = _cluster(symbol), _dir(side)
            same = sum(1 for p in self.paper.positions()
                       if p["side"] == direction and _cluster(p["symbol"]) == cluster)
            if same >= self.max_correlated_positions:
                return reject("correlation",
                              f"{same} open {direction} positions in the {cluster} cluster "
                              f"(max {self.max_correlated_positions}) — correlated exposure")
            steps.append(Step("correlation", True,
                              f"{same}/{self.max_correlated_positions} {direction} in {cluster}"))

        # 3c2b. event-risk gate — high-impact macro events (CPI/FOMC/NFP) spike
        # volatility and gap stops; inside the blackout window no NEW entries.
        econ_risk = 1.0
        if self.econ_events is not None:
            from services.econ_guard import evaluate as _econ_eval
            ev = _econ_eval(self.econ_events())
            if ev["halt_new_entries"]:
                return reject("event_risk",
                              f"Event blackout: {ev['next_event']['name']} in "
                              f"{ev['minutes_to_event']:.0f}m — no new entries")
            econ_risk = ev.get("risk_multiplier", 1.0) or 1.0
            if econ_risk < 1.0:
                steps.append(Step("event_risk", True,
                                  f"caution: {ev['next_event']['name']} in "
                                  f"{ev['minutes_to_event']:.0f}m → risk ×{econ_risk:.2f}"))

        # 3c3. Learning is a soft, observable sizing input. Statistical lessons
        # must not become a self-sealing hard veto; safety hard-stops remain in
        # the risk, drawdown, exposure and market-data gates.
        learning_soft = {"multiplier": 1.0, "active_rules": []}
        if self.learning is not None:
            secs = self._since_last_loss()
            learning_soft = self.learning.soft_adjustment(
                symbol=symbol, regime=payload.get("regime", ""), confidence=confidence,
                minutes_since_loss=None if secs is None else secs / 60)
            steps.append(Step(
                "learning", True,
                f"soft risk ×{learning_soft['multiplier']:.2f}; "
                f"rules={','.join(learning_soft['active_rules']) or 'none'}"))
        else:
            steps.append(Step("learning", True, "soft learning not configured; risk ×1.00"))

        # 3d. drawdown circuit breaker — auto-halt NEW ENTRIES until manual resume
        #     (exits are never blocked, so open positions can always stop out).
        if not self._halted:
            dd = self._drawdown_trip()
            if dd is not None:
                self._engage_halt(dd)
        if self._halted:
            return reject("risk_guard", f"Auto-halt: {self._halt_reason}")
        steps.append(Step("risk_guard", True, "within risk limits"))

        # 3d2. allowed trading days (UTC weekday) — blocks entries on disabled days
        if self.trading_days_mask != 127:
            wd = self._entry_weekday(payload.get("timestamp"))
            if not (self.trading_days_mask >> wd) & 1:
                return reject("trading_day", "Today is not an allowed trading day")
            steps.append(Step("trading_day", True, "allowed day"))

        # 3e. trading-session window (UTC hours) — blocks entries outside hours
        if self.session_start != 0 or self.session_end != 24:
            hour = self._entry_hour(payload.get("timestamp"))
            if not (self.session_start <= hour < self.session_end):
                return reject("session", f"Outside session {self.session_start:02d}:00–{self.session_end:02d}:00 UTC")
            steps.append(Step("session", True, "within trading hours"))

        # 3f. daily-loss kill switch — blocks NEW entries once today's loss exceeds
        #     the limit; resets automatically at the next UTC day.
        if self.max_daily_loss_pct > 0:
            today = self._today_pnl()
            limit = self.max_daily_loss_pct * self.paper.starting_balance
            if today <= -limit:
                return reject("daily_loss", f"Daily loss limit hit ({today:+.2f} ≤ -{limit:.2f})")
            steps.append(Step("daily_loss", True, f"today {today:+.2f} / -{limit:.2f}"))

        # 3g. weekly-loss limit (resets each ISO week)
        if self.max_weekly_loss_pct > 0:
            wk = self._week_pnl()
            wlimit = self.max_weekly_loss_pct * self.paper.starting_balance
            if wk <= -wlimit:
                return reject("weekly_loss", f"Weekly loss limit hit ({wk:+.2f} ≤ -{wlimit:.2f})")
            steps.append(Step("weekly_loss", True, f"week {wk:+.2f} / -{wlimit:.2f}"))

        # 3h. stop after N consecutive losses -> auto-halt new entries until Resume
        if self.max_consecutive_losses > 0 and not self._halted:
            streak = self._consecutive_losses()
            if streak >= self.max_consecutive_losses:
                self._engage_halt(f"{streak} consecutive losses (limit {self.max_consecutive_losses})")
                return reject("risk_guard", f"Auto-halt: {self._halt_reason}")

        # 3i. cooldown after a losing trade
        if self.cooldown_after_loss_min > 0:
            secs = self._since_last_loss()
            if secs is not None and secs < self.cooldown_after_loss_min * 60:
                left = int((self.cooldown_after_loss_min * 60 - secs) / 60) + 1
                return reject("cooldown", f"Cooldown after loss — ~{left}m left")

        # 3j. max trades per UTC day
        if self.max_trades_per_day > 0 and self._opens_today() >= self.max_trades_per_day:
            return reject("max_trades", f"Max {self.max_trades_per_day} trades/day reached")

        # 4. risk: position sizing from stop distance, scaled by conviction
        #    (confidence 1.0 -> full risk; 0.5 -> 75% risk; floors at 50%),
        #    then by the Kelly guard (risk less while the recent record is weak).
        if stop is None or stop == entry:
            return reject("risk", "Invalid stop (missing or equal to entry)")
        # The stop must be on the LOSING side of entry. Found by the differential
        # harness in tests/test_risk_engine_parity.py: this check tested only for
        # a missing or equal stop, so a long with its stop ABOVE entry was
        # accepted, sized from abs(entry - stop) like any other trade, and opened
        # as a position whose stop was already through. A guaranteed immediate
        # loss, and the decision log recorded it as a normal entry.
        _long = _dir(side) == "long"
        if (_long and stop > entry) or (not _long and stop < entry):
            _side_word = "below" if _long else "above"
            return reject("risk",
                          f"Stop {stop} is on the wrong side of entry {entry} — "
                          f"a {_dir(side)} stop must be {_side_word} entry")
        # Each collaborator's opinion on how large to be. Gathering them stays
        # here (they are stateful and live in this object); the arithmetic that
        # combines them moved to tradexa.risk.sizing, where it is a pure
        # function with its own tests. A differential test asserts the extracted
        # composition is bit-identical to the expression this replaced.
        kf = self._kelly_factor() if self.adaptive_risk else 1.0
        ef = self._equity_curve_factor() if self.equity_throttle else 1.0
        lf = float(learning_soft["multiplier"])
        af = float(self.allocator(symbol)) if self.allocator is not None else 1.0
        xf = _clamp_context(payload.get("context_size_factor", 1.0))
        # The autonomous engine may reduce new-entry risk when the current
        # symbol's *measured* paper record deteriorates. It shares the bounded
        # context slot rather than adding a second independent sizing path;
        # 0.5 is the existing global floor, so this can never size to zero or
        # create a surprise leverage change. Webhooks that omit the field are
        # completely backward compatible.
        hf = _clamp_context(payload.get("health_size_factor", 1.0))
        xf = _clamp_context(xf * hf)
        if hf < 1.0:
            steps.append(Step("strategy_health", True,
                              f"measured strategy-health throttle {hf:.2f}× on new-entry risk"))
        sfm = (self.learning.side_multiplier(_dir(side))
               if self.learning is not None else 1.0)
        # edge boost: size up ONLY a proven winning pattern, and never while any
        # other factor is throttling down — defense always outranks offense.
        # `throttling` is min(kelly, curve, learned, event), the same four the
        # inline guard used.
        defensive = _RiskFactors(kelly=kf, equity_curve=ef, learned=lf, event=econ_risk)
        bf = 1.0
        if self.learning is not None and defensive.throttling >= 1.0:
            bf = self.learning.boost_multiplier(regime=payload.get("regime", ""),
                                                confidence=confidence)
        factors = _RiskFactors(
            confidence=confidence, kelly=kf, equity_curve=ef, learned=lf,
            allocator=af, event=econ_risk, boost=bf, context=xf, side=sfm,
            streak=self._streak_factor())
        eff_risk = _effective_risk(self.risk_per_trade_pct, factors)
        realized_equity = self._current_realized_equity()
        active_sizing_mode = normalize_sizing_mode(self.position_sizing_mode)
        sizing = PositionSizingService.calculate(PositionSizingRequest(
            mode=active_sizing_mode,
            entry_price=entry,
            stop_price=stop,
            starting_equity=self.starting_equity,
            current_realized_equity=realized_equity,
            risk_per_trade_pct=eff_risk,
            fixed_quantity=self.fixed_position_size,
            profit_reinvestment=self.profit_reinvestment,
            maximum_risk_amount=self.maximum_risk_amount,
            minimum_equity=self.minimum_equity,
        ))
        manual_sizing = sizing.mode == FIXED_QUANTITY
        size = sizing.quantity
        if not sizing.approved:
            if sizing.reason == "instance equity floor reached":
                self._halted = True
                self._halt_reason = sizing.reason
            return reject("sizing", sizing.reason)
        # Immutable-at-entry evidence for the decision journal. This is a
        # receipt of the factors already used above, not a second sizing path.
        # Values are overwritten server-side so a webhook cannot fabricate the
        # explanation later shown to the operator.
        payload["journal_sizing"] = {
            "base_risk_pct": round(self.risk_per_trade_pct * 100, 4),
            "effective_risk_pct": round(eff_risk * 100, 4),
            "mode": sizing.mode,
            "sizing_engine_version": sizing.sizing_engine_version,
            "risk_basis_at_entry": round(sizing.risk_basis, 10),
            "risk_amount_at_entry": round(sizing.risk_amount, 10),
            "equity_before_trade": round(realized_equity, 10),
            "configured_fixed_size": round(self.fixed_position_size, 10) if manual_sizing else None,
            "computed_size": round(size, 10),
            "risk_model_size": round(size, 10),
            "modifiers": {
                "confidence": round(factors.confidence, 4),
                "kelly": round(factors.kelly, 4),
                "equity_curve": round(factors.equity_curve, 4),
                "learning": round(factors.learned, 4),
                "allocator": round(factors.allocator, 4),
                "event": round(factors.event, 4),
                "edge": round(factors.boost, 4),
                "context_and_health": round(factors.context, 4),
                "side": round(factors.side, 4),
                "account_loss_streak": round(factors.streak, 4),
            },
        }
        if manual_sizing:
            steps.append(Step("risk", True,
                              f"manual fixed quantity {size:.6f}; risk {sizing.risk_amount:.2f}; "
                              "exposure and portfolio caps still apply"))
        else:
            steps.append(Step("risk", True,
                              f"{_describe_factors(factors)}"
                              f" → risk {eff_risk*100:.2f}% sized {size:.6f}"))

        # 5. exposure limit (cap notional to the per-trade limit)
        max_size = (self.exposure_limit_pct * realized_equity) / entry if entry > 0 else 0.0
        if size > max_size:
            size = max_size
            steps.append(Step("exposure", True, f"capped to {self.exposure_limit_pct*100:.0f}% exposure"))
        else:
            steps.append(Step("exposure", True, f"within {self.exposure_limit_pct*100:.0f}% exposure"))
        if size <= 0:
            return reject("exposure", "Exposure limit leaves zero size")

        # 5b. portfolio exposure cap — TOTAL open notional across all positions.
        # Per-trade limits alone still allow the book to stack up; this is the
        # portfolio-level ceiling every production bot enforces.
        if self.max_total_exposure_pct > 0:
            open_notional = sum(p["size"] * p["entry"] for p in self.paper.positions())
            budget = self.max_total_exposure_pct * realized_equity - open_notional
            if budget <= 0:
                return reject("portfolio_exposure",
                              f"Portfolio exposure {open_notional:.0f} already at the "
                              f"{self.max_total_exposure_pct*100:.0f}% cap")
            if size * entry > budget:
                size = budget / entry
                steps.append(Step("portfolio_exposure", True,
                                  f"capped to remaining {budget:.0f} notional budget"))
            else:
                steps.append(Step("portfolio_exposure", True,
                                  f"total within {self.max_total_exposure_pct*100:.0f}%"))
        if self.symbol_rules_provider is not None:
            try:
                rules = self.symbol_rules_provider(symbol)
                executable_size, rule_error = rules.clamp(size, entry)
            except Exception as exc:
                return reject("venue_rules",
                              f"Venue order rules unavailable: {type(exc).__name__}: {exc}")
            if executable_size <= 0:
                return reject("venue_rules", rule_error or "Quantity rejected by venue rules")
            if executable_size != size:
                steps.append(Step(
                    "venue_rules", True,
                    f"quantity floored {size:.10g} → {executable_size:.10g} "
                    f"(step {rules.step_size:g})"))
                size = executable_size
            else:
                steps.append(Step("venue_rules", True, "quantity satisfies venue filters"))
            payload["venue_rules"] = {
                "symbol": rules.symbol, "step_size": rules.step_size,
                "tick_size": rules.tick_size, "min_qty": rules.min_qty,
                "min_notional": rules.min_notional,
            }
        payload["journal_sizing"]["accepted_size"] = round(size, 10)
        payload["journal_sizing"]["accepted_notional"] = round(size * entry, 2)

        # 5c. THE RISK ENGINE VETO — the last thing between a signal and a fill.
        # Every trade passes through it before execution; there is no path to
        # the open below that does not come through here.
        #
        # A veto, not a second sizer. The pipeline keeps sizing because it holds
        # nine strategy-derived modifiers (Kelly, learning store, allocator,
        # streak) the standalone engine deliberately cannot see — knowing about
        # them would make it depend on strategies, which is the one thing it is
        # not allowed to do. Two sizers would be duplication, and the second one
        # would silently overrule risk decisions the first had already made.
        #
        # It runs under PIPELINE_PARITY limits, so its rule set is a SUBSET of
        # the gates above and it cannot refuse anything they let through. If it
        # ever does, that is a genuine divergence between two implementations of
        # the same rule and the trade is refused: the engine is the authority on
        # risk, and a disagreement it loses is a bypass.
        #
        # Last rather than first, and that placement is load-bearing. Run before
        # the pipeline's own exposure gates, it reached the same verdicts a beat
        # earlier and reported them under its own name — turning
        # "portfolio_exposure" into "risk_engine" in the decision log and
        # dropping the veto out of the counterfactual tracker's graded stages.
        # Same decision, worse explanation. Here it can only add refusals, never
        # rename existing ones.
        if self.risk_engine is None:
            raise RuntimeError("Mandatory risk engine is unavailable; entry failed closed")
        decision = self.risk_engine.evaluate(self._risk_context(
            symbol=symbol, side=side, entry=entry, stop=stop,
            confidence=confidence, payload=payload, equity=realized_equity))
        if not decision.approved:
            return reject("risk_engine", decision.explain())
        steps.append(Step("risk_engine", True,
                          f"approved by {decision.limits_name} "
                          f"({len(decision.checks)} rules, "
                          f"{decision.evaluated_ms:.1f}ms)"))

        if self.global_entry_guard is not None:
            try:
                allowed, reason = self.global_entry_guard(symbol=symbol, entry=entry, stop=stop, size=size)
            except Exception as exc:  # fail closed: cross-instance risk must not silently disappear
                return reject("global_risk", f"Global risk manager unavailable: {type(exc).__name__}")
            if not allowed:
                return reject("global_risk", reason)
            steps.append(Step("global_risk", True, reason))

        # 6. paper execution (routed through the fill model)
        #
        # Claim the idempotency key BEFORE the fill. Checking afterwards meant a
        # replayed candle opened a real second position and only then hit the
        # unique constraint, leaving an open position with no webhook row, no
        # journal entry and a caller told the cycle produced nothing -- which
        # instance reconciliation would later report as position_without_trade.
        # The claim is the same durable constraint, used as a lock: whoever
        # inserts the row owns this entry, and the loser never reaches open().
        try:
            self.ledger.insert_webhook_event(
                alert_id=alert_id, symbol=symbol, side=side, entry=entry,
                stop=stop, payload=payload, status="claimed",
            )
        except DuplicateOrderIntent as exc:
            steps.append(Step("dedup", True, "idempotency key already claimed"))
            return PipelineResult(
                False, "dedup", f"order already claimed for this candle: {exc}",
                steps, {})
        try:
            fill = self.paper.open(symbol=symbol, side=side, size=size, entry=entry,
                                   stop=stop, target=payload.get("target"),
                                   alert_id=alert_id, maker=bool(payload.get("maker")),
                                   # Empty for an inbound webhook alert, which
                                   # belongs to no deployed spec and must stay
                                   # unattributed rather than borrow one.
                                   strategy_id=str(payload.get("strategy_id") or ""),
                                   sizing_context={
                                       "sizing_mode": sizing.mode,
                                       "sizing_engine_version": sizing.sizing_engine_version,
                                       "risk_basis_at_entry": sizing.risk_basis,
                                       "risk_pct_at_entry": eff_risk,
                                       "risk_amount_at_entry": abs(entry - stop) * size,
                                       "equity_before_trade": realized_equity,
                                       "signal_timestamp": payload.get("timestamp"),
                                       "decision_timestamp": payload.get("timestamp"),
                                       "signal_price": entry,
                                       "strategy": payload.get("strategy"),
                                       "strategy_version": self.journal_context.get("strategy_version"),
                                       "timeframe": payload.get("timeframe"),
                                       "market_data_source": payload.get("market_data_source"),
                                       "candle_id": payload.get("decision_identity"),
                                       "account_id": (
                                           f"instance:{self.journal_context.get('instance_id')}:"
                                           f"{self.journal_context.get('simulation_session_id')}"),
                                       "execution_engine": "INSTANCE",
                                   })
        except Exception:
            # Anything escaping the fill strands the claim, and a stranded
            # claim bars this candle permanently -- the dedup constraint has no
            # time component. Release it before the error propagates, so a
            # retry of a trade that never happened is still possible.
            self._release_order_claim(alert_id)
            raise
        if fill.action == "rejected":
            # The claim did not become an order. Release it so a later,
            # legitimate retry of this candle is not mistaken for a duplicate
            # and a trade that never happened silently suppressed.
            self._release_order_claim(alert_id)
            # The reason travels on the result. Reading it off the engine let
            # the hub's quote thread overwrite it between the rejection and
            # this line, hiding a systematic capital stop behind a
            # random-rejection reason.
            return reject("execution",
                          getattr(fill, "reason", "")
                          or "Order rejected at fill (execution model)")
        if fill.action == "intent":
            try:
                # The claim becomes this order's one row. Inserting a second
                # would put every trade in the event log twice, once as its
                # claim and once as itself.
                if not self.ledger.promote_webhook_event(
                        alert_id=alert_id, status="pending", payload=payload):
                    self.ledger.insert_webhook_event(
                        alert_id=alert_id, symbol=symbol, side=side,
                        entry=entry, stop=stop, payload=payload, status="pending",
                    )
            except DuplicateOrderIntent as exc:
                # The durable idempotency constraint caught a repeat of a key
                # this instance has already parked -- the same candle replayed
                # after a reconnect, or two threads racing the guard's
                # read-then-insert. The intent already exists, so this is a
                # no-op success, not a second order and not an error.
                #
                # The claim still has to go: the final row already exists under
                # this key, so a surviving claim would bar the candle forever
                # with nothing to age it out.
                self._release_order_claim(alert_id)
                steps.append(Step("execution", True, "duplicate intent suppressed by idempotency key"))
                return PipelineResult(
                    False, "dedup", f"order intent already recorded: {exc}",
                    steps, {},
                )
            self.ledger.log(
                level="info", stage="execution", symbol=symbol,
                message=(f"{symbol} {side} paper intent accepted; waiting for "
                         "the first Binance USD-M quote after decision time"),
            )
            steps.append(Step("execution", True, "forward-paper intent awaiting next quote"))
            return PipelineResult(
                True, "execution", "paper order intent awaiting next quote",
                steps, fill.__dict__,
            )
        entry, size = fill.price, fill.size          # actual filled price / size
        payload["journal_sizing"].update({
            "filled_entry": round(entry, 10),
            "filled_size": round(size, 10),
            "filled_notional": round(size * entry, 2),
        })
        try:
            if not self.ledger.promote_webhook_event(
                    alert_id=alert_id, status="accepted", entry=entry, payload=payload):
                self.ledger.insert_webhook_event(alert_id=alert_id, symbol=symbol, side=side,
                                                 entry=entry, stop=stop, payload=payload,
                                                 status="accepted")
        except DuplicateOrderIntent as exc:
            # Reaching here means the fill already happened and was recorded
            # under this key. Reporting success would double-count it in the
            # caller's statistics, so report the duplicate plainly -- and drop
            # the claim, which would otherwise bar this candle permanently.
            self._release_order_claim(alert_id)
            steps.append(Step("execution", True, "duplicate fill suppressed by idempotency key"))
            return PipelineResult(False, "dedup", f"order already recorded: {exc}", steps, {})
        open_msg = f"{symbol} {side} opened {size:.6f} @ {entry}"
        if brain_reason:
            open_msg += f" | {brain_reason}"
        self.ledger.log(level="info", stage="execution", message=open_msg, symbol=symbol)
        self.ledger.add_alert(severity="info", category="trade",
                              title=f"Paper trade opened — {symbol}",
                              detail=(brain_reason or f"{side} {size:.6f} @ {entry}"))
        self._notify("trade", f"📈 {symbol} {side} opened", f"{size:.6f} @ {entry}")
        steps.append(Step("execution", True, f"opened {size:.6f} @ {entry}"))
        # full explainable decision journal for this trade (real data only)
        if self.journal is not None and fill.trade_id:
            try:
                self.journal.record_entry(
                    trade_id=fill.trade_id, mode=payload.get("mode", "paper"),
                    symbol=symbol, side=_dir(side),
                    strategy=payload.get("strategy", brain_reason.split(" ")[0] or "Strategy"),
                    timeframe=payload.get("timeframe", ""), entry=entry, stop=stop,
                    target=payload.get("target"), size=size, equity=realized_equity,
                    confidence=confidence, brain_score=payload.get("brain_score"),
                    regime=payload.get("regime", ""), steps=steps, payload=payload,
                    position_id=fill.position_id)
            except Exception:  # noqa: BLE001 — journaling must never block trading
                pass
        # remember entry context so the learning loop can study this trade later
        if alert_id:
            self._alert_info[alert_id] = {"confidence": confidence,
                                          "regime": payload.get("regime", "")}
            if len(self._alert_info) > 500:
                self._alert_info.pop(next(iter(self._alert_info)))
        return PipelineResult(True, "execution", "paper trade opened", steps, fill.__dict__)

    # ----------------------------------------------------- auto risk guard
    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def resume(self) -> None:
        """Clear an auto-halt and rebaseline drawdown to the current equity
        (called by the manual Resume control)."""
        self._halted = False
        self._halt_reason = ""
        self._dd_base_balance = self.paper.balance()
        self._dd_base_count = len(self.paper.history())

    @staticmethod
    def _entry_hour(ts: Optional[str]) -> int:
        try:
            if ts:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).hour
        except Exception:  # noqa: BLE001 — unparseable -> use now
            pass
        return datetime.now(timezone.utc).hour

    @staticmethod
    def _entry_weekday(ts: Optional[str]) -> int:
        try:
            if ts:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).weekday()
        except Exception:  # noqa: BLE001
            pass
        return datetime.now(timezone.utc).weekday()

    @staticmethod
    def _pnl_on_day(trades, day: str) -> float:
        return sum((t.get("pnl") or 0.0) for t in trades if (t.get("closed_at") or "")[:10] == day)

    def _today_pnl(self) -> float:
        """Net realized P&L for the current UTC day (resets at the day boundary)."""
        day = datetime.now(timezone.utc).date().isoformat()
        return self._pnl_on_day(self.paper.history(), day)

    def _week_pnl(self) -> float:
        """Net realized P&L for the current ISO week (resets each week)."""
        y, w, _ = datetime.now(timezone.utc).isocalendar()
        total = 0.0
        for t in self.paper.history():
            try:
                d = datetime.fromisoformat((t.get("closed_at") or "").replace("Z", "+00:00"))
                ty, tw, _ = d.isocalendar()
                if ty == y and tw == w:
                    total += t.get("pnl") or 0.0
            except Exception:  # noqa: BLE001
                continue
        return total

    def _consecutive_losses(self) -> int:
        n = 0
        for t in sorted(self.paper.history(), key=lambda x: x.get("closed_at") or "", reverse=True):
            if (t.get("pnl") or 0.0) < 0:
                n += 1
            else:
                break
        return n

    def _streak_factor(self) -> float:
        """Anti-martingale risk scaling: ×0.5 after 2 consecutive losses,
        ×0.25 after 4. The next winning trade restores full size. This can
        only REDUCE risk — never increase it."""
        if not self.streak_risk_scaling:
            return 1.0
        streak = self._consecutive_losses()
        if streak >= 4:
            return 0.25
        if streak >= 2:
            return 0.5
        return 1.0

    def _since_last_loss(self) -> Optional[float]:
        """Seconds since the most recent losing trade closed, or None."""
        losses = [t for t in self.paper.history() if (t.get("pnl") or 0.0) < 0 and t.get("closed_at")]
        if not losses:
            return None
        last = max(losses, key=lambda t: t["closed_at"])
        try:
            d = datetime.fromisoformat(last["closed_at"].replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - d).total_seconds()
        except Exception:  # noqa: BLE001
            return None

    def _opens_today(self) -> int:
        day = datetime.now(timezone.utc).date().isoformat()
        return sum(1 for t in self.ledger.get_paper_trades()
                   if (t.get("opened_at") or "")[:10] == day)

    def _drawdown_trip(self) -> Optional[str]:
        """Return a reason if realized-equity drawdown (since the last baseline)
        breaches the limit."""
        ordered = sorted(self.paper.history(), key=lambda t: t.get("closed_at") or "")
        ordered = ordered[self._dd_base_count:]
        if not ordered:
            return None
        base = self._dd_base_balance
        eq = [base]
        run = base
        for t in ordered:
            run += (t.get("pnl") or 0.0)
            eq.append(run)
        from bot.metrics import max_drawdown
        from risk.drawdown_guard import breached
        if breached(eq, self.max_drawdown_pct):
            return (f"Max drawdown breached "
                    f"({max_drawdown(eq) * 100:.1f}% > {self.max_drawdown_pct * 100:.0f}%)")
        return None

    def alert_context(self) -> dict:
        """alert_id -> {confidence, regime} for the learning loop. Restarts
        used to wipe this (it was memory-only), starving the regime and
        conviction lessons; it now rehydrates once from the ledger's webhook
        events, where every accepted entry's payload is already persisted."""
        if not self._alert_info_hydrated:
            self._alert_info_hydrated = True
            try:
                for ev in self.ledger.get_webhook_events(limit=500):
                    if ev.get("status") != "accepted":
                        continue
                    p = ev.get("payload") or {}
                    aid = ev.get("alert_id") or ""
                    if aid and aid not in self._alert_info and (
                            "confidence" in p or "regime" in p):
                        self._alert_info[aid] = {
                            "confidence": float(p.get("confidence", 1.0) or 1.0),
                            "regime": p.get("regime", "")}
            except Exception:  # noqa: BLE001 — hydration is best-effort
                pass
        return self._alert_info

    def _kelly_factor(self, min_trades: int = 20, lookback: int = 40) -> float:
        """Kelly-capped risk multiplier from the bot's own recent closed trades.

        Professional sizing: the per-trade risk should never exceed a fraction
        of the Kelly optimum implied by the recent win rate and payoff ratio.
        With a healthy record the factor is 1.0 (no change). As the recent edge
        deteriorates it scales risk down smoothly, floored at 0.25 — the bot
        digs shallower holes when it is trading badly, exactly when equity
        needs protecting. With fewer than ``min_trades`` closed trades there is
        no evidence either way, so sizing is untouched.
        """
        # history() is newest-first — the first `lookback` entries are the recent ones
        closed = [t for t in self.paper.history() if t.get("rr") is not None]
        recent = [float(t["rr"]) for t in closed[:lookback]]
        if len(recent) < min_trades:
            return 1.0
        wins = [r for r in recent if r > 0]
        losses = [-r for r in recent if r < 0]
        if not losses:
            return 1.0
        if not wins:
            return 0.25
        w = len(wins) / len(recent)
        payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
        kelly = w - (1.0 - w) / payoff if payoff > 0 else 0.0
        if kelly <= 0:          # negative edge lately — trade at quarter risk
            return 0.25
        # quarter-Kelly cap: full risk only when 0.25*kelly covers the base risk
        return max(0.25, min(1.0, 0.25 * kelly / self.risk_per_trade_pct))

    def _equity_curve_factor(self, lookback: int = 10) -> float:
        """Equity-curve throttle: trade half size while the bot's own equity
        curve is below its recent average (prop-desk practice — the system's
        equity curve is itself a signal about whether the edge is working in
        current conditions). Full size resumes as soon as the curve recovers.
        """
        closed = self.paper.history()   # newest-first
        if len(closed) < lookback:
            return 1.0
        balance = self.paper.starting_balance
        curve = []
        for t in reversed(closed):      # chronological equity curve
            balance += t.get("pnl") or 0.0
            curve.append(balance)
        sma = sum(curve[-lookback:]) / lookback
        return 0.5 if curve[-1] < sma else 1.0

    def _notify(self, kind: str, title: str, detail: str = "") -> None:
        if self.notifier:
            try:
                self.notifier(kind, title, detail)
            except Exception:  # noqa: BLE001 — notifications never break trading
                pass

    def _engage_halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        self.ledger.log(level="error", stage="risk_guard",
                        message=f"AUTO-HALT — {reason}; new entries blocked until Resume")
        self.ledger.add_alert(severity="critical", category="risk",
                              title="Auto-halt — drawdown circuit breaker", detail=reason)
        self._notify("risk", "🛑 Auto-halt", reason)
