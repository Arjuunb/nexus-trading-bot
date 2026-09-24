"""Adaptive MTF Trend Pullback Lab: one private paper bot, laid out like the SMC lab.

The lab exists so an operator can watch this one strategy place paper orders
without reading them out of the Trading Instances list. It deliberately does
not reimplement anything that trades:

* the strategy is the unmodified ``adaptive_trend_pullback`` built by the same
  factory Trading Instances use;
* candles, the 1h/15m/5m context, the fail-closed freshness checks, sizing,
  forward-paper fills, stops and targets all come from the same
  ``TradingInstanceManager`` / ``AutoStrategyEngine`` / ``SignalPipeline``
  path the XRP instance runs on.

What makes it separate is where it keeps its state. The lab owns a private
manager on its OWN ledger database, so its positions, trades, balance, leases
and decisions never mix with Trading Instances, it never appears in their
list, and it never takes one of their slots. It shares only the Binance
USD-M market-data hub, which deduplicates channels, so watching XRPUSDT here
adds no second Binance connection.

Each symbol gets its own bot (and therefore its own paper account and
history). Switching symbol stops the current bot and starts or resumes the
one for the new symbol; it is refused while the current bot holds a position
or a working order, so an open trade is never orphaned.

Modes are what this execution path can honestly do:

* ``automatic``    -- entries armed; the bot places paper orders itself.
* ``signals_only`` -- the worker keeps running and records every decision,
                      but its entry gate is closed, so no order is placed.
* ``off``          -- stopped.

There is no manual-approval mode: the instance engine has no approval queue,
and a mode that looked like one without being one would be worse than none.

The lab can also MIRROR a Trading Instance that runs this strategy: the same
chart, orders, trades and journal, read from that instance's own manager and
ledger. A mirror is view only -- it never starts, stops or configures the
instance -- and the instance's per-candle journal rows are written by a tee
on its existing report hook (InstanceReportTee), after the instances' own
store has recorded the candle, so nothing about how it trades changes.

Paper only. Nothing here can reach an exchange order endpoint.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from services.mtf_policy import TIMEFRAME_SECONDS
from services.trading_instances import TradingInstance, TradingInstanceManager

STRATEGY_KEY = "adaptive_trend_pullback"
STRATEGY_LABEL = "Adaptive MTF Trend Pullback"
TIMEFRAME = "5m"                      # the strategy's own decision timeframe
DEFAULT_SYMBOL = "XRPUSDT"
STARTING_EQUITY = 10_000.0
DEFAULT_RISK_PCT = 0.5                # percent of equity per trade
MAX_RISK_PCT = 1.0                    # same ceiling as the SMC lab
MAX_SYMBOL_BOTS = 50
MODES = ("automatic", "signals_only", "off")
MODE_LABELS = {"automatic": "Automatic paper", "signals_only": "Signals only", "off": "Off"}
_EXPOSED_ORDER_KEYS = ("forward_paper_intents", "strategy_limit_intents", "quarantined_intents")
LAB_SOURCE = "lab"


class AdaptiveLabError(ValueError):
    """A lab request the lab refuses, with the reason an operator can act on."""


class AdaptiveJournal:
    """Append-only journal: one row per closed candle the bot judged.

    Plugged in where the engine records its per-candle Decision Report
    (``engine.reports``). The engine calls ``record`` once per closed candle,
    after the strategy has decided, so the row keeps the engine's report AND
    the strategy's own ``decision_report()`` for that same candle -- the 1h
    regime, 15m pullback and 5m confirmation reasons the generic report does
    not carry. It reads; it never changes what the engine or strategy does,
    and a failure here is swallowed by the engine, never raised into it.
    """

    def __init__(self, path: str = ":memory:"):
        self._lock = threading.Lock()
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript("""
            CREATE TABLE IF NOT EXISTS adaptive_journal(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT,
                candle_time TEXT NOT NULL, decision_identity TEXT NOT NULL,
                engine_decision TEXT, price REAL,
                strategy_state TEXT, strategy_decision TEXT, direction TEXT,
                reason TEXT, quality REAL, entry REAL, stop REAL, target REAL, rr REAL,
                stages_json TEXT, engine_reasons_json TEXT, recorded_at TEXT NOT NULL,
                UNIQUE(instance_id, decision_identity));
            CREATE INDEX IF NOT EXISTS ix_adaptive_journal ON adaptive_journal(instance_id, id);
            CREATE TRIGGER IF NOT EXISTS adaptive_journal_no_update BEFORE UPDATE ON adaptive_journal
              BEGIN SELECT RAISE(ABORT, 'adaptive journal is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS adaptive_journal_no_delete BEFORE DELETE ON adaptive_journal
              BEGIN SELECT RAISE(ABORT, 'adaptive journal is append-only'); END;
        """)
        self._c.commit()
        self._managers: list = []

    def bind(self, manager) -> None:
        """Read strategy reports from this manager's workers (lab or instances)."""
        if all(bound is not manager for bound in self._managers):
            self._managers.append(manager)

    def _strategy_report(self, instance_id: str, symbol: str) -> dict:
        runtime = next((m._runtime.get(instance_id) for m in self._managers
                        if instance_id in getattr(m, "_runtime", {})), None)
        live = getattr(runtime[0], "_live_strategies", {}) if runtime else {}
        strategy = live.get(symbol) or live.get(str(symbol).upper())
        report = getattr(strategy, "decision_report", None)
        return report() if callable(report) else {}

    def record(self, report: dict) -> None:
        instance_id = str(report.get("instance_id") or "")
        symbol = str(report.get("symbol") or "")
        candle = str(report.get("ts") or "")
        if not instance_id or not candle:
            return
        decided = self._strategy_report(instance_id, symbol)
        stages = {key: decided.get(key) for key in ("regime", "trend", "pullback", "confirmation")
                  if decided.get(key)}
        with self._lock:
            self._c.execute(
                "INSERT OR IGNORE INTO adaptive_journal(instance_id,symbol,timeframe,candle_time,"
                "decision_identity,engine_decision,price,strategy_state,strategy_decision,direction,"
                "reason,quality,entry,stop,target,rr,stages_json,engine_reasons_json,recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (instance_id, symbol, report.get("timeframe"), candle,
                 str(report.get("decision_identity") or candle), report.get("decision"),
                 report.get("price"), decided.get("state"), decided.get("decision"),
                 decided.get("direction"), decided.get("reason"), decided.get("quality_score"),
                 decided.get("entry"), decided.get("stop"), decided.get("target"), decided.get("rr"),
                 json.dumps(stages, default=str), json.dumps(report.get("reasons") or [], default=str),
                 datetime.now(timezone.utc).isoformat()))
            self._c.commit()

    def entries(self, instance_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._c.execute(
                "SELECT * FROM adaptive_journal WHERE instance_id=? ORDER BY id DESC LIMIT ?",
                (instance_id, int(limit))).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["stages"] = json.loads(item.pop("stages_json") or "{}")
            item["engine_reasons"] = json.loads(item.pop("engine_reasons_json") or "[]")
            out.append(item)
        return out


class InstanceReportTee:
    """The Trading Instances' per-candle report hook, with the lab journal beside it.

    Every report goes to the instances' own store first, exactly as before.
    Only then, and only for an instance running this strategy, the same
    report is journaled with the strategy's own decision for that candle. A
    journal failure is contained here: it can neither reach the engine nor
    undo the instances' own record. Anything else asked of the hook is the
    original store's.
    """

    def __init__(self, primary, journal: "AdaptiveJournal", manager):
        self.primary = primary
        self.journal = journal
        self.manager = manager

    def record(self, report: dict):
        result = self.primary.record(report) if self.primary is not None else None
        try:
            inst = self.manager._instances.get(str(report.get("instance_id") or ""))
            if inst is not None and inst.strategy_key == STRATEGY_KEY:
                self.journal.record(report)
        except Exception as exc:  # noqa: BLE001 -- the journal never blocks the instance
            print(f"[adaptive-lab] instance journal row skipped: {type(exc).__name__}: {exc}")
        return result

    def __getattr__(self, name):
        return getattr(self.primary, name)


class AdaptiveLab:
    def __init__(self, ledger, *, strategy_factory: Callable[[str, str], object],
                 strategy_version: str, live_poll_s: float = 5.0,
                 decision_store=None, market_hub=None, symbol_rules_provider=None,
                 supported_symbols: Optional[tuple[str, ...]] = None,
                 journal: Optional[AdaptiveJournal] = None):
        self.ledger = ledger
        self.decisions = decision_store
        self.journal = journal or AdaptiveJournal()
        self.strategy_version = strategy_version
        self.supported_symbols = tuple(s.upper() for s in (supported_symbols or ()))
        # The manager treats its instances as slices of ONE paper account and
        # refuses allocations beyond it. Here every symbol's bot is its own
        # 10,000 USDT account and only one of them trades at a time, so the
        # capacity is one full allocation per symbol that can ever have a bot.
        # The manager's global risk guard sums those allocations, so with
        # several symbol bots it is looser than one 10,000 account would be;
        # the per-bot pipeline limits (risk per trade, open positions,
        # drawdown, daily loss) are the ones that bind.
        capacity = STARTING_EQUITY * (len(self.supported_symbols) or MAX_SYMBOL_BOTS)
        self.manager = TradingInstanceManager(
            ledger, strategy_factory=strategy_factory, live=True,
            live_poll_s=live_poll_s, max_slots=1, decision_store=decision_store,
            market_hub=market_hub, symbol_rules_provider=symbol_rules_provider,
            paper_account_capital=capacity, cycle_store=self.journal)
        self.journal.bind(self.manager)
        self.instances: Optional[TradingInstanceManager] = None
        self._lock = threading.RLock()
        # One bot trades at a time; switching symbol stops the old one first.
        # A lab that has never had a bot starts on XRPUSDT. The selection is
        # then simply the saved default symbol of the lab's own manager.
        first_use = not self._bots()
        self.manager.configure(
            max_active_slots=1, paper_account_capital=capacity,
            defaults={"default_symbol": DEFAULT_SYMBOL} if first_use else None)

    # ------------------------------------------------- Trading Instance mirror
    def attach_instances(self, manager: TradingInstanceManager) -> None:
        """Mirror the Trading Instances that run this strategy (view only).

        Their per-candle reports keep going to their own store; the tee adds
        a lab journal row beside each one for this strategy's instances.
        Workers started later pick the tee up from the manager; workers
        already running are handed it too, so attach order cannot matter.
        """
        self.instances = manager
        self.journal.bind(manager)
        if isinstance(manager.cycle_store, InstanceReportTee):
            return
        primary = manager.cycle_store
        tee = InstanceReportTee(primary, self.journal, manager)
        manager.cycle_store = tee
        for runtime in list(manager._runtime.values()):
            engine = runtime[0] if runtime else None
            if engine is not None and getattr(engine, "reports", None) is primary:
                engine.reports = tee

    def _mirrored(self) -> list[TradingInstance]:
        if self.instances is None:
            return []
        return sorted((inst for inst in self.instances._instances.values()
                       if inst.strategy_key == STRATEGY_KEY and inst.mode == "trading"),
                      key=lambda inst: inst.created_at)

    @staticmethod
    def _armed(manager: TradingInstanceManager, inst: TradingInstance) -> bool:
        runtime = manager._runtime.get(inst.id)
        return bool(runtime and manager.worker_alive(inst.id) and runtime[3].trading_allowed())

    def _view(self, source: Optional[str]):
        """(kind, manager, ledger, instance) for the lab bot or a mirrored instance."""
        if not source or source == LAB_SOURCE:
            return "lab", self.manager, self.ledger, self.current()
        inst = next((row for row in self._mirrored() if row.id == source), None)
        if inst is None:
            raise AdaptiveLabError(
                f"no Trading Instance running {STRATEGY_LABEL} has id {source}")
        return "instance", self.instances, self.instances.ledger, inst

    def sources(self) -> list[dict]:
        lab_bot = self.current()
        rows = [{"id": LAB_SOURCE, "kind": "lab", "symbol": self.selected_symbol(),
                 "timeframe": TIMEFRAME, "running": bool(lab_bot and self.manager.worker_alive(lab_bot.id)),
                 "label": f"Lab bot · {self.selected_symbol()} {TIMEFRAME}"}]
        for inst in self._mirrored():
            running = self.instances.worker_alive(inst.id)
            rows.append({"id": inst.id, "kind": "instance", "symbol": inst.symbol,
                         "timeframe": inst.timeframe, "running": running,
                         "label": (f"Trading Instance · {inst.symbol} {inst.timeframe} · "
                                   f"{'running' if running else 'stopped'}")})
        return rows

    def view(self, source: Optional[str] = None) -> dict:
        """What the page shows for the chosen source: its bot row and whether it can trade."""
        kind, manager, _ledger, inst = self._view(source)
        if inst is None:
            return {"source": LAB_SOURCE, "kind": kind, "bot": None, "bot_id": None,
                    "symbol": self.selected_symbol(), "timeframe": TIMEFRAME,
                    "running": False, "armed": False, "state_label": "No bot yet",
                    "risk_pct": DEFAULT_RISK_PCT, "capital_allocation": STARTING_EQUITY,
                    "controlled_from": "this lab"}
        running = manager.worker_alive(inst.id)
        armed = self._armed(manager, inst)
        if kind == "lab":
            label = MODE_LABELS[self.mode_of(inst)]
        else:
            label = ("Running · entries armed" if armed else
                     "Running · entries paused" if running else "Stopped")
        return {"source": LAB_SOURCE if kind == "lab" else inst.id, "kind": kind,
                "bot": manager.status(inst.id), "bot_id": inst.id,
                "symbol": inst.symbol, "timeframe": inst.timeframe,
                "running": running, "armed": armed, "state_label": label,
                "risk_pct": round(float(inst.risk_per_trade_pct) * 100, 6),
                "capital_allocation": inst.capital_allocation,
                "controlled_from": "this lab" if kind == "lab" else "Trading Instances"}

    # ------------------------------------------------------------ the bots
    def _bots(self) -> list[TradingInstance]:
        return [inst for inst in self.manager._instances.values()
                if inst.strategy_key == STRATEGY_KEY and inst.mode == "trading"]

    def _bot_for(self, symbol: str) -> Optional[TradingInstance]:
        symbol = symbol.upper()
        return next((inst for inst in self._bots() if inst.symbol == symbol), None)

    def selected_symbol(self) -> str:
        return str(self.manager.instance_defaults.get("default_symbol") or DEFAULT_SYMBOL).upper()

    def current(self) -> Optional[TradingInstance]:
        return self._bot_for(self.selected_symbol())

    def _create(self, symbol: str) -> TradingInstance:
        return self.manager.create(
            symbol=symbol, strategy_key=STRATEGY_KEY, strategy_label=STRATEGY_LABEL,
            strategy_version=self.strategy_version, timeframe=TIMEFRAME,
            risk_per_trade_pct=DEFAULT_RISK_PCT / 100, capital_allocation=STARTING_EQUITY)

    @staticmethod
    def mode_of(inst: Optional[TradingInstance]) -> str:
        if inst is None or not inst.desired_running:
            return "off"
        return "signals_only" if inst.state == "paused" else "automatic"

    def _exposure(self, inst: TradingInstance) -> str:
        positions = self.ledger.get_positions(
            "open", instance_id=inst.id, simulation_session_id=inst.simulation_session_id)
        if positions:
            return f"{inst.symbol} has an open paper position"
        pending = self.manager.store.market_state(inst.id).get("pending_orders_json") or {}
        if any(pending.get(key) for key in _EXPOSED_ORDER_KEYS):
            return f"{inst.symbol} has a working paper order"
        return ""

    def _apply_mode(self, inst: TradingInstance, mode: str) -> None:
        running = self.manager.worker_alive(inst.id)
        if mode == "off":
            if inst.desired_running or running:
                self.manager.stop(inst.id)
        elif mode == "signals_only":
            if running:
                if inst.state != "paused":
                    self.manager.pause(inst.id)
            else:
                self.manager.start(inst.id, entry_gate_closed=True)
        else:  # automatic
            if running:
                if inst.state == "paused":
                    self.manager.resume(inst.id)
            else:
                self.manager.start(inst.id)

    # --------------------------------------------------------- operations
    def ensure_started(self) -> TradingInstance:
        """First use: the XRPUSDT bot, armed, so the lab has something to show.

        Only when the lab has never had a bot. After that, the saved mode of
        every bot is restored by restore(), and an operator's Off stays off.
        """
        with self._lock:
            if self._bots():
                return self.current() or self._bots()[0]
            inst = self._create(self.selected_symbol())
            self._apply_mode(inst, "automatic")
            return self.manager._instances[inst.id]

    def restore(self) -> list[str]:
        """Bring back whatever was running before a restart, in its saved mode."""
        return self.manager.restore_desired_instances()

    def configure(self, *, symbol: Optional[str] = None, mode: Optional[str] = None,
                  risk_pct: Optional[float] = None) -> dict:
        with self._lock:
            if mode is not None and mode not in MODES:
                raise AdaptiveLabError(f"mode must be one of {', '.join(MODES)}")
            if risk_pct is not None and not 0 < float(risk_pct) <= MAX_RISK_PCT:
                raise AdaptiveLabError(
                    f"risk per trade must be above 0% and no more than {MAX_RISK_PCT:g}%")
            current = self.current()
            target_symbol = (symbol or self.selected_symbol()).upper()
            if self.supported_symbols and target_symbol not in self.supported_symbols:
                raise AdaptiveLabError(f"unsupported symbol {target_symbol}")
            next_mode = mode or self.mode_of(current)
            if current is None and next_mode == "off" and mode is None:
                next_mode = "automatic"

            if current is not None and target_symbol != current.symbol:
                exposed = self._exposure(current)
                if exposed:
                    raise AdaptiveLabError(
                        f"{exposed}. Close it before switching symbol, so the trade "
                        "is not left without the bot that manages it.")
                if current.desired_running or self.manager.worker_alive(current.id):
                    self.manager.stop(current.id)

            bot = self._bot_for(target_symbol) or self._create(target_symbol)
            if risk_pct is not None and abs(bot.risk_per_trade_pct - float(risk_pct) / 100) > 1e-12:
                exposed = self._exposure(bot)
                if exposed:
                    raise AdaptiveLabError(f"{exposed}. Risk changes apply to the next trade "
                                           "only after it is closed.")
                self.manager.update_configuration(bot.id, risk_per_trade_pct=float(risk_pct) / 100)
                bot = self.manager._instances[bot.id]
            self._apply_mode(bot, next_mode)
            self.manager.configure(defaults={"default_symbol": target_symbol})
            return self.status()

    # -------------------------------------------------------------- reads
    def status(self, source: Optional[str] = None) -> dict:
        inst = self.current()
        view = self.view(source)
        base = {
            "lab": "ADAPTIVE_MTF_TREND_PULLBACK",
            "strategy": {"key": STRATEGY_KEY, "label": STRATEGY_LABEL,
                         "version": self.strategy_version, "timeframe": TIMEFRAME},
            "symbol": self.selected_symbol(),
            "modes": [{"id": mode, "label": MODE_LABELS[mode]} for mode in MODES],
            "max_risk_pct": MAX_RISK_PCT,
            "supported_symbols": list(self.supported_symbols) or [DEFAULT_SYMBOL],
            "bots": [{"symbol": b.symbol, "id": b.id, "mode": self.mode_of(b)}
                     for b in sorted(self._bots(), key=lambda b: b.symbol)],
            "sources": self.sources(),
            "view": view,
            "paper_only": True, "real_execution_allowed": False,
        }
        if inst is None:
            return {**base, "bot": None, "mode": "off",
                    "risk_pct": DEFAULT_RISK_PCT}
        row = view["bot"] if view["kind"] == "lab" else self.manager.status(inst.id)
        return {**base, "bot": row, "bot_id": inst.id, "mode": self.mode_of(inst),
                "risk_pct": round(float(inst.risk_per_trade_pct) * 100, 6)}

    def paper(self, source: Optional[str] = None) -> dict:
        _kind, manager, ledger, inst = self._view(source)
        if inst is None:
            return {"positions": [], "orders": {}, "trades": [], "logs": [],
                    "paper_only": True, "real_execution_allowed": False}
        scope = {"instance_id": inst.id, "simulation_session_id": inst.simulation_session_id}
        pending = manager.store.market_state(inst.id).get("pending_orders_json") or {}
        return {
            "bot_id": inst.id, "symbol": inst.symbol,
            "positions": ledger.get_positions("open", **scope),
            "orders": {key: pending.get(key) or {} for key in _EXPOSED_ORDER_KEYS},
            "trades": ledger.get_paper_trades(**scope),
            "logs": manager.store.engine_logs(inst.id, 100),
            "paper_only": True, "real_execution_allowed": False,
        }

    def _engine_report_rows(self, inst: TradingInstance, limit: int,
                            journaled: set[str]) -> list[dict]:
        """A mirrored instance's candles from before the lab journal saw it.

        The instances' own per-candle store has been recording every candle
        all along; the lab journal only from the moment the tee was attached.
        Those earlier candles are shown as what they are -- the engine's
        report, with its decision and reasons -- and never given a strategy
        state, quality or R:R the store does not hold.
        """
        store = getattr(self.instances.cycle_store, "primary", self.instances.cycle_store)
        if store is None or not hasattr(store, "list"):
            return []
        rows = []
        for row in store.list(limit=limit, instance_id=inst.id, full=True):
            identity = str(row.get("decision_identity") or row.get("ts") or "")
            if identity in journaled:
                continue
            report = row.get("report") or {}
            reasons = [str(r) for r in report.get("reasons") or []]
            rows.append({
                "id": f"report-{row['id']}", "instance_id": inst.id, "symbol": inst.symbol,
                "timeframe": row.get("timeframe"), "candle_time": row.get("ts"),
                "decision_identity": identity, "engine_decision": row.get("decision"),
                "price": row.get("price"), "strategy_state": None, "strategy_decision": None,
                "direction": report.get("side"), "reason": reasons[0] if reasons else None,
                "quality": None, "entry": None, "stop": None, "target": None, "rr": None,
                "stages": {}, "engine_reasons": reasons, "evidence": "engine_report"})
        return rows

    def journal_entries(self, limit: int = 200, source: Optional[str] = None) -> dict:
        kind, _manager, _ledger, inst = self._view(source)
        rows = [{**row, "evidence": "strategy_journal"}
                for row in (self.journal.entries(inst.id, limit) if inst else [])]
        if kind == "instance" and len(rows) < limit:
            journaled = {str(row.get("decision_identity") or "") for row in rows}
            rows = sorted(rows + self._engine_report_rows(inst, limit, journaled),
                          key=lambda row: str(row.get("candle_time") or ""), reverse=True)[:limit]
        tally: dict[str, int] = {}
        for row in rows:
            key = (str(row.get("strategy_state") or "UNKNOWN")
                   if row["evidence"] == "strategy_journal" else "ENGINE_REPORT_ONLY")
            tally[key] = tally.get(key, 0) + 1
        return {"bot_id": inst.id if inst else None,
                "symbol": inst.symbol if inst else self.selected_symbol(),
                "entries": rows, "state_counts": tally,
                "note": ("One row per closed candle the bot judged, written as it decided. "
                         "Append-only. Rows marked engine report predate the lab journal: "
                         "the instance's own report for that candle, without strategy state."),
                "paper_only": True, "real_execution_allowed": False}

    def live_chart(self, window: int = 400, source: Optional[str] = None) -> dict:
        """The bot's own Binance feed, in the shape the SMC lab's chart draws.

        Read from the bot's hub subscription -- the same closed candles its
        strategy decides on, plus the forming candle and the bid/ask/mark the
        hub holds. The forming candle is display only and is labelled so.
        """
        kind, manager, ledger, inst = self._view(source)
        if inst is None:
            raise AdaptiveLabError("the lab has no bot yet; choose a mode to start one")
        runtime = manager._runtime.get(inst.id)
        feed = getattr(runtime[0], "ws_feed", None) if runtime else None
        who = f"the {inst.symbol} bot" if kind == "lab" else f"the {inst.symbol} Trading Instance"
        if feed is None or not manager.worker_alive(inst.id):
            raise AdaptiveLabError(f"{who} is off, so it has no live feed to show")
        if not callable(getattr(feed, "snapshot", None)):
            raise AdaptiveLabError(f"{who} is not on the Binance USD-M hub feed, so there is "
                                   "no forming candle or quote to show")
        snapshot = feed.snapshot()
        status = snapshot.get("connection") or {}
        quote = snapshot.get("quote") or {}
        closed = list(snapshot.get("closed_bars") or [])[-max(20, int(window)):]
        forming = snapshot.get("forming")
        step = timedelta(seconds=TIMEFRAME_SECONDS.get(inst.timeframe, 300))

        def candle(bar) -> dict:
            return {"timestamp": bar.timestamp.isoformat(), "open": float(bar.open),
                    "high": float(bar.high), "low": float(bar.low), "close": float(bar.close),
                    "volume": float(getattr(bar, "volume", 0) or 0)}

        last_price = (float(forming.close) if forming is not None else
                      float(closed[-1].close) if closed else None)
        reliable = bool(status.get("reliable"))
        scope = {"instance_id": inst.id, "simulation_session_id": inst.simulation_session_id}
        positions = ledger.get_positions("open", **scope)
        trades = ledger.get_paper_trades(**scope)
        fills = []
        for trade in trades:
            if trade.get("opened_at") and trade.get("entry") is not None:
                fills.append({"timestamp": trade["opened_at"], "price": float(trade["entry"]),
                              "side": "buy" if trade.get("side") == "long" else "sell"})
            if trade.get("closed_at") and trade.get("exit") is not None:
                fills.append({"timestamp": trade["closed_at"], "price": float(trade["exit"]),
                              "side": "sell" if trade.get("side") == "long" else "buy",
                              "realized_pnl": trade.get("realized_pnl") or trade.get("pnl")})
        plan = None
        if positions and positions[0].get("stop") is not None and positions[0].get("target") is not None:
            p = positions[0]
            plan = {"entry": float(p["entry"]), "stop": float(p["stop"]),
                    "target_1": float(p["target"]), "target_2": float(p["target"])}
        return {
            "symbol": inst.symbol, "timeframe": inst.timeframe, "bot_id": inst.id,
            "source": LAB_SOURCE if kind == "lab" else inst.id,
            "candles": [candle(bar) for bar in closed],
            "forming_candle": candle(forming) if forming is not None else None,
            "live_display": {
                "is_forming": forming is not None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "refresh_interval_seconds": 2.5,
                "candle_closes_at": (forming.timestamp + step).isoformat() if forming is not None else None,
                "last_price": last_price,
                "bid": quote.get("bid"), "ask": quote.get("ask"), "mark": quote.get("mark"),
                "funding_rate": quote.get("funding_rate"),
                "next_funding_time": quote.get("next_funding_time"),
                "connection_state": status.get("state"), "reliable": reliable,
                "new_entries_paused": not reliable,
                "health_reason": status.get("health_reason"),
                "quote_source": "BINANCE_USDM_PUBLIC_WEBSOCKET",
                "execution_uses_closed_bars_only": True,
            },
            "data_provenance": {
                "last_closed_candle": closed[-1].timestamp.isoformat() if closed else None,
                "closed_candles_loaded": len(closed),
                "exchange": "Binance USDⓈ-M Futures",
                "market_data_source": (f"{who}'s own subscription to the shared Binance hub"),
            },
            "trade_plan": plan, "fills": fills,
            "paper_only": True, "real_execution_allowed": False,
        }

    def shutdown(self) -> dict:
        return self.manager.shutdown()
