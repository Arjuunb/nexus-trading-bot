"""Trade Journal recorder — the single write path into the canonical journal.

Every executed trade reaches the journal automatically, from the component
that actually knows each fact:

    strategy / decision engine   -> frozen decision snapshot     (pipeline)
    risk manager / sizing        -> risk, sizing, account state  (pipeline)
    execution engine             -> fills, fees, slippage        (engine hooks)
    position manager             -> stop/target modifications    (engine hooks)
    market / session data        -> session, London time         (derived)
    account ledger               -> reconciliation and recovery  (reconcile_ledger)

The pipeline registers the decision before submitting the order, which creates
a PENDING journal trade carrying the immutable snapshot. The execution engine
reports the fill (immediately, or later from a forward-paper quote), every
partial exit, the final exit and every protection change. Nothing here can
block trading: each hook is wrapped by its caller and reconciliation repairs
any record a crash interrupted, from the ledger's own rows.

Nothing is fabricated. A value the source did not capture is stored as NULL
and the trade's ``data_completeness`` says so.
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from data.trade_journal_store import TradeJournalStore
from services import journal_review
from services.journal_sessions import parse_ts, timing_fields

#: |net R| at or below this is a break-even trade.
BREAKEVEN_R_BAND = 0.05
#: Without a stop (no R), |net| at or below this fraction of notional is break-even.
BREAKEVEN_NOTIONAL_BAND = 0.0005
#: Reconciliation leaves very recent ledger rows to the live hooks.
RECONCILE_GRACE_S = 120
#: A PENDING order older than this with no fill becomes EXECUTION_UNCERTAIN.
PENDING_UNCERTAIN_AFTER_S = 24 * 3600
_EPS = 1e-9
#: journal sources whose positions live in the paper execution engine ledger
ENGINE_SOURCES = ("PIPELINE", "ENGINE", "LEDGER_RECONCILIATION", "LEGACY_DECISION_JOURNAL")
#: sources whose ledger books funding (PaperBrokerV2 funding events)
FUNDING_MODELLED_SOURCES = ("PRICE_ACTION_LAB", "SMC_LAB")

EXIT_REASONS = ("STOP_LOSS", "TAKE_PROFIT", "PARTIAL_TAKE_PROFIT", "MANUAL_CLOSE",
                "STRATEGY_CLOSE", "TRAILING_STOP", "BREAK_EVEN_STOP", "SAFETY_EXIT",
                "LIQUIDATION", "TIME_EXIT", "SIMULATION_RESET", "UNKNOWN")
_EXIT_ALIASES = {
    "stop": "STOP_LOSS", "stop-loss": "STOP_LOSS", "stop_loss": "STOP_LOSS", "sl": "STOP_LOSS",
    "stopped": "STOP_LOSS", "lost": "STOP_LOSS",
    "take-profit": "TAKE_PROFIT", "take_profit": "TAKE_PROFIT", "target": "TAKE_PROFIT",
    "tp": "TAKE_PROFIT", "target_hit": "TAKE_PROFIT", "won": "TAKE_PROFIT",
    "manual-close": "MANUAL_CLOSE", "manual": "MANUAL_CLOSE", "manual_close": "MANUAL_CLOSE",
    "opposite-signal": "STRATEGY_CLOSE", "strategy": "STRATEGY_CLOSE", "strategy_close": "STRATEGY_CLOSE",
    "signal": "STRATEGY_CLOSE",
    "trailing-stop": "TRAILING_STOP", "trailing_stop": "TRAILING_STOP", "trail": "TRAILING_STOP",
    "breakeven": "BREAK_EVEN_STOP", "break-even": "BREAK_EVEN_STOP", "break_even_stop": "BREAK_EVEN_STOP",
    "time": "TIME_EXIT", "time-stop": "TIME_EXIT", "time_exit": "TIME_EXIT",
    "safety": "SAFETY_EXIT", "risk": "SAFETY_EXIT", "safety_exit": "SAFETY_EXIT",
    "kill-switch": "SAFETY_EXIT", "emergency": "SAFETY_EXIT",
    "liquidation": "LIQUIDATION", "liquidated": "LIQUIDATION", "liquidated_paper": "LIQUIDATION",
    "account_restart": "SIMULATION_RESET", "simulation_reset": "SIMULATION_RESET",
}
_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "USD", "EUR", "GBP", "JPY", "BTC", "ETH")
_EXCHANGES = {
    "binance_usdm": ("Binance USD-M Futures", "PERPETUAL_FUTURES"),
    "binanceusdm": ("Binance USD-M Futures", "PERPETUAL_FUTURES"),
    "binance": ("Binance", None), "bybit": ("Bybit", None), "okx": ("OKX", None),
    "alpaca": ("Alpaca", "EQUITY"), "oanda": ("OANDA", "FX_CFD"), "paper": ("Paper venue", None),
}
_TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800, "12h": 43200,
               "1d": 86400, "1w": 604800}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if out == out else None   # NaN -> None
    except (TypeError, ValueError):
        return None


def _q(value) -> str:
    """Compact number for timeline text; never raises on a missing value."""
    number = _num(value)
    return "?" if number is None else f"{number:.8g}"


def _iso(value) -> Optional[str]:
    stamp = parse_ts(value)
    return stamp.isoformat() if stamp else None


# --------------------------------------------------------------- identity helpers
def split_symbol(symbol: str) -> tuple[str, Optional[str]]:
    clean = str(symbol or "").upper().replace("/", "").replace("-", "").replace(":", "")
    for quote in _QUOTES:
        if clean.endswith(quote) and len(clean) > len(quote):
            return clean[: -len(quote)], quote
    return clean, None


def exchange_identity(exchange: Optional[str], instrument_type: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    key = str(exchange or "").strip().lower()
    name, market = _EXCHANGES.get(key, (exchange if key and key not in ("unknown", "inherit") else None, None))
    kind = str(instrument_type or "").strip().lower()
    if kind in ("perpetual", "perp", "swap", "futures", "future"):
        market = "PERPETUAL_FUTURES"
    elif kind == "spot":
        market = "SPOT"
    elif kind in ("equity", "stock"):
        market = "EQUITY"
    return name, market


def trading_mode_for(provenance: dict) -> str:
    """Map captured execution provenance to one trading mode. Unknown stays
    UNKNOWN — it is never guessed into a mode that would blend datasets."""
    execution = str(provenance.get("execution_mode") or "").lower()
    data = str(provenance.get("market_data_mode") or "").lower()
    if execution == "live":
        return "LIVE"
    if execution in ("backtest",) or data in ("backtest",):
        return "BACKTEST"
    if data in ("replay", "legacy_replay", "historical", "simulation"):
        return "SIMULATION"
    if data in ("forward_paper", "live", "legacy_live", "paper_forward"):
        return "FORWARD_PAPER"
    return "UNKNOWN"


def strategy_family(strategy_id: Optional[str], strategy_name: Optional[str]) -> str:
    text = f"{strategy_id or ''} {strategy_name or ''}".lower()
    if "smc" in text or "smart money" in text:
        return "SMC"
    if "price_action" in text or "price action" in text or text.strip().startswith("pa"):
        return "PRICE_ACTION"
    if "brain" in text or "decision" in text:
        return "DECISION_BRAIN"
    if "ema" in text:
        return "EMA"
    return (strategy_id or strategy_name or "UNKNOWN").upper().replace(" ", "_")[:40]


def normalize_exit_reason(reason: Optional[str]) -> str:
    if not reason:
        return "UNKNOWN"
    raw = str(reason).strip()
    if raw.upper() in EXIT_REASONS:
        return raw.upper()
    return _EXIT_ALIASES.get(raw.lower(), _EXIT_ALIASES.get(raw.lower().replace(" ", "-"), "UNKNOWN"))


def direction_of(side: Optional[str]) -> str:
    text = str(side or "").strip().lower()
    return "LONG" if text in ("buy", "long", "bullish") else "SHORT"


# --------------------------------------------------------------- risk / result math
def risk_fields(*, direction: str, entry: Optional[float], stop: Optional[float],
                target: Optional[float], quantity: Optional[float],
                equity_before: Optional[float], max_allowed_risk_pct: Optional[float] = None,
                max_allowed_risk_amount: Optional[float] = None,
                leverage: Optional[float] = None) -> dict:
    """Everything derivable from the entry plan. Missing inputs give NULLs."""
    out: dict = {}
    entry, stop, target, quantity = _num(entry), _num(stop), _num(target), _num(quantity)
    if entry is not None and quantity is not None:
        out["notional_value"] = abs(entry * quantity)
        if leverage:
            out["margin_used"] = out["notional_value"] / float(leverage)
    if entry is not None and stop is not None:
        out["stop_distance"] = abs(entry - stop)
        if quantity is not None:
            out["risk_amount"] = out["stop_distance"] * abs(quantity)
    if entry is not None and target is not None:
        out["target_distance"] = abs(target - entry)
        if quantity is not None:
            out["planned_reward"] = out["target_distance"] * abs(quantity)
        if out.get("stop_distance"):
            out["planned_rr"] = out["target_distance"] / out["stop_distance"]
    if out.get("risk_amount") is not None and equity_before:
        out["risk_pct"] = out["risk_amount"] / float(equity_before) * 100
    if max_allowed_risk_pct is not None:
        out["max_allowed_risk_pct"] = float(max_allowed_risk_pct)
    if max_allowed_risk_amount is not None:
        out["max_allowed_risk_amount"] = float(max_allowed_risk_amount)
    status = "UNKNOWN"
    if out.get("risk_pct") is not None and (max_allowed_risk_pct is not None
                                            or max_allowed_risk_amount is not None):
        ok = True
        if max_allowed_risk_pct is not None:
            ok = ok and out["risk_pct"] <= float(max_allowed_risk_pct) * 1.005 + 1e-9
        if max_allowed_risk_amount is not None and out.get("risk_amount") is not None:
            ok = ok and out["risk_amount"] <= float(max_allowed_risk_amount) * 1.005 + 1e-9
        status = "PASSED" if ok else "FAILED"
    elif stop is None and entry is not None:
        status = "FAILED"   # no stop means the risk is undefined, not acceptable
    out["risk_rule_status"] = status
    return out


def classify_result(*, net_pnl: Optional[float], risk_amount: Optional[float],
                    notional: Optional[float], leg_pnls: Iterable[float] = ()) -> Optional[str]:
    """WIN / LOSS / BREAK_EVEN, or PARTIAL_* when exit legs disagree in sign."""
    net = _num(net_pnl)
    if net is None:
        return None
    risk = _num(risk_amount)
    if risk and risk > 0:
        band = BREAKEVEN_R_BAND * risk
    elif notional:
        band = BREAKEVEN_NOTIONAL_BAND * abs(float(notional))
    else:
        band = _EPS
    legs = [float(p) for p in leg_pnls if p is not None]
    mixed = len(legs) > 1 and any(p > band / len(legs) for p in legs) and any(p < -band / len(legs) for p in legs)
    if abs(net) <= band:
        return "BREAK_EVEN"
    if mixed:
        return "PARTIAL_WIN" if net > 0 else "PARTIAL_LOSS"
    return "WIN" if net > 0 else "LOSS"


def excursion_fields(*, direction: str, entry: Optional[float], quantity: Optional[float],
                     risk_amount: Optional[float], mfe_price: Optional[float] = None,
                     mae_price: Optional[float] = None, mfe_r: Optional[float] = None,
                     mae_r: Optional[float] = None, source: str) -> dict:
    """MFE/MAE in price, currency and R. MAE is stored as a negative amount."""
    entry, quantity, risk = _num(entry), _num(quantity), _num(risk_amount)
    sign = 1.0 if direction == "LONG" else -1.0
    stop_distance = (risk / abs(quantity)) if (risk and quantity) else None
    out: dict = {}
    if mfe_price is None and mfe_r is not None and entry is not None and stop_distance:
        mfe_price = entry + sign * float(mfe_r) * stop_distance
    if mae_price is None and mae_r is not None and entry is not None and stop_distance:
        mae_price = entry - sign * abs(float(mae_r)) * stop_distance
    if mfe_price is not None and entry is not None:
        favourable = max(0.0, sign * (float(mfe_price) - entry))
        out["mfe_price"] = float(mfe_price)
        if quantity is not None:
            out["mfe_amount"] = favourable * abs(quantity)
        if stop_distance:
            out["mfe_r"] = favourable / stop_distance
    elif mfe_r is not None:
        out["mfe_r"] = max(0.0, float(mfe_r))
    if mae_price is not None and entry is not None:
        adverse = max(0.0, sign * (entry - float(mae_price)))
        out["mae_price"] = float(mae_price)
        if quantity is not None:
            out["mae_amount"] = -adverse * abs(quantity)
        if stop_distance:
            out["mae_r"] = -adverse / stop_distance
    elif mae_r is not None:
        out["mae_r"] = -abs(float(mae_r))
    if out:
        out["excursion_source"] = source
    return out


# --------------------------------------------------------------- snapshot builders
def _status_of(value) -> str:
    text = str(value or "").strip().upper()
    if text in ("PASS", "PASSED", "TRUE", "OK", "YES"):
        return "PASSED"
    if text in ("FAIL", "FAILED", "FALSE", "BLOCKED"):
        return "FAILED"
    if text in ("NOT CHECKED", "NOT_CHECKED", "N/A", "NOT_EVALUATED"):
        return "NOT_EVALUATED"
    if text in ("MISSING",):
        return "MISSING"
    if text in ("NEUTRAL",):
        return "NEUTRAL"
    return text or "UNKNOWN"


def split_conditions(rows: Iterable[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(passed, failed, missing/not-evaluated) from heterogeneous checklists."""
    passed, failed, missing = [], [], []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("label") or row.get("rule") or row.get("key") or "condition"
        item = {"name": str(name), "detail": row.get("detail"),
                "key": row.get("key") or row.get("rule")}
        if row.get("required"):
            item["required"] = True
        status = _status_of(row.get("status") if "status" in row else row.get("ok"))
        item["status"] = status
        if status == "PASSED":
            passed.append(item)
        elif status == "FAILED":
            failed.append(item)
        else:
            missing.append(item)
    return passed, failed, missing


_SMC_FIELDS = (
    ("htf_bias", r"htf|higher.?time"), ("market_structure", r"structure|trend"),
    ("bos", r"\bbos\b|break.?of.?structure"), ("choch", r"choch|change.?of.?character"),
    ("order_block", r"order.?block|\bob\b"), ("fair_value_gap", r"fvg|fair.?value"),
    ("liquidity_sweep", r"sweep|liquidity"), ("eqh_eql", r"eqh|eql|equal.?(high|low)"),
    ("premium_discount", r"premium|discount|location|dealing"),
    ("poi", r"\bpoi\b|point.?of.?interest|zone"), ("rejection_confirmation", r"rejection|reject"),
    ("entry_confirmation_candle", r"confirm|trigger|displacement"),
    ("volume_confirmation", r"volume"),
)
_PA_FIELDS = (
    ("support_resistance_level", r"zone|support|resistance|level"),
    ("rejection_level", r"rejection|reject|reclaim"), ("flip_retest", r"flip|retest"),
    ("trend_direction", r"trend|structure|bias"), ("liquidity_sweep", r"sweep|liquidity"),
    ("rejection_candle", r"pin|engulf|rejection.?candle|wick"),
    ("dominance_candle", r"dominan"), ("ema_relationship", r"\bema"),
    ("volume_confirmation", r"volume"), ("entry_trigger", r"trigger|entry|confirm"),
)


def _match_fields(spec, rows: list[dict], extra: dict) -> dict:
    out = {}
    for field, pattern in spec:
        regex = re.compile(pattern, re.I)
        hit = next((row for row in rows if regex.search(
            f"{row.get('key') or ''} {row.get('name') or row.get('label') or ''}")), None)
        if field in extra and extra[field] is not None:
            out[field] = extra[field]
        elif hit is not None:
            out[field] = {"status": _status_of(hit.get("status") if "status" in hit else hit.get("ok")),
                          "detail": hit.get("detail")}
        else:
            out[field] = {"status": "NOT_EVALUATED", "detail": "not part of this strategy's decision"}
    return out


def strategy_setup(family: str, conditions: list[dict], extra: Optional[dict] = None) -> dict:
    """Strategy-aware setup section. Values come from the strategy's own
    recorded conditions; anything the strategy did not evaluate says so."""
    extra = extra or {}
    if family == "SMC":
        return {"family": "SMC", **_match_fields(_SMC_FIELDS, conditions, extra)}
    if family == "PRICE_ACTION":
        return {"family": "PRICE_ACTION", **_match_fields(_PA_FIELDS, conditions, extra)}
    return {"family": family, "conditions": conditions, **extra}


def _primary_mtf(*sources) -> dict:
    for source in sources:
        if isinstance(source, dict):
            mtf = source.get("mtf_evidence") if "mtf_evidence" in source else source
            primary = (mtf or {}).get("primary") if isinstance(mtf, dict) else None
            if primary:
                return primary
    return {}


def _freshness(reference: Optional[str], decided_at: Optional[str], timeframe: Optional[str]) -> Optional[str]:
    ref, decided = parse_ts(reference), parse_ts(decided_at)
    seconds = _TF_SECONDS.get(str(timeframe or ""))
    if ref is None or decided is None or not seconds:
        return None
    age = (decided - ref).total_seconds()
    label = "FRESH" if age <= 2 * seconds else "STALE"
    return f"{label} ({int(age)}s after candle open, timeframe {timeframe})"


# --------------------------------------------------------------- the recorder
class _HooksFirstLock:
    """The recorder lock. Python locks are not fair: a thread that releases a
    lock and takes it straight back usually beats a thread already waiting,
    so an import releasing per item could still starve a fill hook. Imports
    therefore take it through ``TradeJournalRecorder.bulk_item``, which lets
    every waiting caller through first."""

    def __init__(self):
        self._lock = threading.RLock()
        self._state = threading.Condition()
        self._waiting = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        if not blocking:
            return False
        with self._state:
            self._waiting += 1
        try:
            return self._lock.acquire(timeout=timeout)
        finally:
            with self._state:
                self._waiting -= 1
                self._state.notify_all()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def let_waiters_through(self, timeout: float = 1.0) -> None:
        """Wait until no caller is queued for the lock (bounded, so an import
        always progresses). A no-op for a thread that already holds it."""
        if self._lock._is_owned():
            return
        with self._state:
            self._state.wait_for(lambda: not self._waiting, timeout)


class TradeJournalRecorder:
    """Writes canonical journal records. Thread-safe."""

    def __init__(self, store: TradeJournalStore, *, legacy_journal=None,
                 review_on_close: bool = True, logger: Optional[Callable[..., None]] = None,
                 preferred_window: Optional[tuple] = None):
        self.store = store
        self.legacy_journal = legacy_journal
        self.review_on_close = review_on_close
        self.logger = logger
        self.preferred_window = preferred_window
        self._lock = _HooksFirstLock()
        # current stop/target per open canonical trade, so a per-bar
        # management checkpoint only touches the database when a level moves.
        self._levels: dict[str, tuple[Optional[float], Optional[float]]] = {}

    @contextlib.contextmanager
    def bulk_item(self):
        """Hold the lock for one item of an import (a lab trip, a ledger or
        legacy row, a replay entry), after any waiting live hook has gone."""
        self._lock.let_waiters_through()
        with self._lock:
            yield

    def _log(self, message: str) -> None:
        if self.logger is not None:
            try:
                self.logger(message)
            except Exception:  # noqa: BLE001 — logging must never raise
                pass

    # ------------------------------------------------------------ decision
    def register_decision(self, *, order_id: str, scope: str, symbol: str, side: str,
                          timeframe: str, payload: dict, steps: list, sizing: dict,
                          provenance: dict, requested_entry: float, stop: Optional[float],
                          target: Optional[float], quantity: float,
                          equity_before: Optional[float], available_before: Optional[float],
                          open_positions: int, max_allowed_risk_pct: Optional[float],
                          max_allowed_risk_amount: Optional[float],
                          leverage: Optional[float] = None, leverage_source: Optional[str] = None,
                          preferred_window: Optional[tuple] = None) -> Optional[str]:
        """Create the PENDING journal trade and freeze the decision snapshot."""
        with self._lock:
            now = _now()
            direction = direction_of(side)
            strategy_name = provenance.get("strategy_name") or payload.get("strategy") or "Strategy"
            strategy_id = provenance.get("strategy_id") or payload.get("strategy") or strategy_name
            family = strategy_family(strategy_id, strategy_name)
            trading_mode = trading_mode_for(provenance)
            if str(payload.get("mode") or "").lower() == "backtest":
                trading_mode = "BACKTEST"
            exchange, market_type = exchange_identity(provenance.get("exchange"),
                                                     provenance.get("instrument_type"))
            base, quote = split_symbol(symbol)
            instance_id = provenance.get("instance_id") or (scope or None)
            alert = str(order_id or "")
            if instance_id:
                source = "TRADING_INSTANCE"
            elif alert.startswith("auto"):
                source = "AUTO_ENGINE"
            elif str(payload.get("source") or "").lower() == "manual":
                source = "MANUAL"
            else:
                source = "WEBHOOK"
            # Deterministic forward keys make re-registration of one candle's
            # order idempotent; research replay keys repeat across runs, so
            # those get a fresh key and rely on the fill link for uniqueness.
            key = (f"order:{scope or 'legacy'}:{alert}" if alert.startswith("auto:")
                   else f"order:{uuid.uuid4().hex}")
            snapshot_ctx = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
            gate = payload.get("journal_quality_gate") or {}
            mtf = _primary_mtf(snapshot_ctx, payload.get("journal_setup") or {})
            risk = risk_fields(direction=direction, entry=requested_entry, stop=stop, target=target,
                               quantity=quantity, equity_before=equity_before,
                               max_allowed_risk_pct=max_allowed_risk_pct,
                               max_allowed_risk_amount=max_allowed_risk_amount, leverage=leverage)
            signal_at = _iso(payload.get("timestamp"))
            fields = {
                "source_system": "PIPELINE", "source_trade_key": key, "order_id": alert or None,
                "instance_id": instance_id, "instance_name": provenance.get("instance_name"),
                "bot_id": instance_id or ("legacy-auto-engine" if source == "AUTO_ENGINE" else source.lower()),
                "simulation_session_id": provenance.get("simulation_session_id"),
                "strategy_id": strategy_id, "strategy_name": strategy_name,
                "strategy_family": family,
                "strategy_version": provenance.get("strategy_version"),
                "trade_source": source, "trading_mode": trading_mode,
                "exchange": exchange, "market_type": market_type, "symbol": symbol.upper(),
                "base_asset": base, "quote_asset": quote, "direction": direction,
                "timeframe": timeframe or payload.get("timeframe"),
                "htf_timeframe": mtf.get("htf_timeframe"), "status": "PENDING",
                "signal_at": signal_at, "order_created_at": now,
                "requested_entry_price": _num(requested_entry),
                "initial_stop": _num(stop), "initial_target": _num(target),
                "current_stop": _num(stop), "current_target": _num(target),
                "account_balance_before": _num(equity_before),
                # Equity equals balance only when nothing else is open; with
                # open positions the unrealised P&L at decision time is unknown.
                "account_equity_before": _num(equity_before) if open_positions == 0 else None,
                "available_margin_before": _num(available_before),
                "leverage": _num(leverage), "leverage_source": leverage_source,
                "max_allowed_risk_pct": risk.get("max_allowed_risk_pct"),
                "max_allowed_risk_amount": risk.get("max_allowed_risk_amount"),
                "decision": f"ENTER_{direction}",
                "setup_score": _num(gate.get("score") if gate else payload.get("brain_score")),
                "confidence": _num(payload.get("confidence")),
                "htf_bias": (mtf.get("htf_bias") or (gate or {}).get("htf_bias")
                             or snapshot_ctx.get("htf_trend")),
                "market_regime": payload.get("regime") or (gate or {}).get("regime"),
                "data_completeness": "LIVE_CAPTURE",
                "provenance": json.dumps({k: v for k, v in provenance.items() if v is not None},
                                         sort_keys=True, default=str),
            }
            fields.update(timing_fields(signal_at, preferred_window=preferred_window or self.preferred_window))
            # entry session is re-derived from the fill; keep only the model now
            for name in ("entry_session", "entry_weekday", "entry_hour_london", "entry_at_london",
                         "in_preferred_session"):
                fields.pop(name, None)
            trade_id, created = self.store.create_trade(fields, links=[("ORDER", f"{scope or 'legacy'}:{alert}")]
                                                        if alert.startswith("auto:") else [])
            if not created:
                return trade_id
            snapshot = self._pipeline_snapshot(
                payload=payload, steps=steps, sizing=sizing, family=family, direction=direction,
                timeframe=fields["timeframe"], mtf=mtf, decided_at=now, risk=risk,
                provenance=provenance)
            self.store.save_snapshot(trade_id, snapshot)
            ts = signal_at or now
            self.store.add_event(trade_id, "setup-detected",
                                 f"{strategy_name} setup on {symbol} {fields['timeframe'] or ''}".strip(),
                                 ts=ts, actor="strategy-engine")
            self.store.add_event(trade_id, "decision", f"ENTER_{direction}: {snapshot['decision_reason']}",
                                 ts=now, actor="decision-engine")
            self.store.add_event(trade_id, "risk-check-passed",
                                 f"{len([s for s in snapshot['risk']['gates'] if s.get('status') == 'PASSED'])} "
                                 "risk gates passed", ts=now, actor="risk-manager")
            self.store.add_event(trade_id, "order-submitted",
                                 f"{direction} {_q(quantity)} {symbol} @ ~{_q(requested_entry)} "
                                 f"(stop {stop}, target {target})", ts=now, actor="execution-engine",
                                 payload={"order_id": alert})
            return trade_id

    def _pipeline_snapshot(self, *, payload, steps, sizing, family, direction, timeframe, mtf,
                           decided_at, risk, provenance) -> dict:
        gate = payload.get("journal_quality_gate") or {}
        reads = list(payload.get("brain_checklist") or [])
        setup_meta = payload.get("journal_setup") or {}
        reads.extend(setup_meta.get("conditions") or [])
        for item in gate.get("passed") or []:
            reads.append({"name": f"Quality gate: {item}", "status": "Passed"})
        for item in (gate.get("failed") or []):
            reads.append({"name": f"Quality gate: {item}", "status": "Failed"})
        passed, failed, missing = split_conditions(reads)
        gates = []
        for step in steps or []:
            rule = step.get("rule") if isinstance(step, dict) else getattr(step, "rule", "")
            ok = step.get("passed") if isinstance(step, dict) else getattr(step, "passed", True)
            detail = step.get("detail") if isinstance(step, dict) else getattr(step, "detail", "")
            gates.append({"rule": rule, "status": "PASSED" if ok else "FAILED", "detail": detail})
        market_quality = next((g for g in gates if g["rule"] == "market_quality"), None)
        snapshot_ctx = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        return {
            "captured_at": decided_at, "decision": f"ENTER_{direction}",
            "decision_reason": payload.get("reason") or "Strategy signal fired.",
            "conditions_passed": passed, "conditions_failed": failed, "conditions_missing": missing,
            "confidence": _num(payload.get("confidence")),
            "setup_score": _num(gate.get("score") if gate else payload.get("brain_score")),
            "market_bias": payload.get("regime") or gate.get("regime"),
            "htf_bias": mtf.get("htf_bias") or gate.get("htf_bias") or snapshot_ctx.get("htf_trend"),
            "risk_decision": "ALLOWED — every risk gate passed",
            "feed_health": ("HEALTHY — " + (market_quality.get("detail") or "market-quality gate passed")
                            if market_quality and market_quality["status"] == "PASSED" else "NOT RECORDED"),
            "candle_freshness": _freshness(payload.get("timestamp"), decided_at, timeframe),
            "htf_freshness": _freshness(mtf.get("htf_close_timestamp"), decided_at, mtf.get("htf_timeframe"))
            if mtf.get("htf_close_timestamp") else None,
            "strategy_state": setup_meta.get("strategy_state") or "SIGNAL_EMITTED",
            "execution_state": "ORDER_SUBMITTED",
            "strategy_family": family,
            "setup": strategy_setup(family, reads, setup_meta.get("fields")),
            "market_context": {
                **({k: v for k, v in snapshot_ctx.items()} if snapshot_ctx else {}),
                "market_data_source": payload.get("market_data_source") or provenance.get("market_data_source"),
                "decision_identity": payload.get("decision_identity"),
                "decision_reference": payload.get("journal_decision_id"),
            },
            "risk": {
                "gates": gates, "sizing": sizing, "planned": risk,
                "engine_guardrails": payload.get("journal_engine") or {},
                "quality_gate": gate or None,
            },
            "raw": {"payload_keys": sorted(payload.keys())},
            "source": "SIGNAL_PIPELINE",
        }

    def mark_order_outcome(self, trade_id: Optional[str], outcome: str, reason: str = "") -> None:
        """REJECTED / EXECUTION_FAILED / CANCELLED for an order that never filled.
        These are operational events and never count as trading losses."""
        if not trade_id:
            return
        with self._lock:
            trade = self.store.get_trade(trade_id)
            if trade is None or trade["status"] not in ("PENDING", "UNCERTAIN"):
                return
            status = {"REJECTED": "REJECTED", "EXECUTION_FAILED": "FAILED",
                      "CANCELLED": "CANCELLED", "EXECUTION_UNCERTAIN": "UNCERTAIN"}[outcome]
            now = _now()
            self.store.update_trade(trade_id, {
                "status": status, "result": outcome, "result_reason": reason[:500] or None,
                "is_operational": 1, "counts_in_stats": 0,
                **({"finalised_at": now, "entry_locked": 1} if status != "UNCERTAIN" else {}),
            })
            self.store.add_event(trade_id, {"REJECTED": "order-rejected", "EXECUTION_FAILED": "execution-failed",
                                            "CANCELLED": "order-cancelled",
                                            "EXECUTION_UNCERTAIN": "execution-uncertain"}[outcome],
                                 reason or outcome, ts=now, actor="execution-engine")

    # ------------------------------------------------------------ engine hooks
    def on_entry_fill(self, ev: dict) -> Optional[str]:
        """An entry filled. ``ev`` carries the ledger ids and the fill facts."""
        with self._lock:
            ledger_trade_id = str(ev.get("ledger_trade_id") or "")
            existing = self.store.resolve_link("LEDGER_TRADE", ledger_trade_id) if ledger_trade_id else None
            if existing:
                return existing                       # replayed hook: one trade, one record
            context = dict(ev.get("context") or {})
            trade_id = context.get("journal_trade_id")
            trade = self.store.get_trade(trade_id) if trade_id else None
            direction = direction_of(ev.get("side"))
            filled_at = _iso(context.get("fill_timestamp") or ev.get("filled_at")) or _now()
            price, quantity = _num(ev.get("price")), _num(ev.get("quantity"))
            requested = _num(context.get("requested_price") or context.get("signal_price")
                             or ev.get("requested_price"))
            if trade is not None and trade.get("finalised_at"):
                # A fill for an order the journal already closed out (for
                # example cancelled by a reset). The fill is real, so it gets
                # its own record; the old one keeps its history.
                self.store.add_event(trade_id, "late-fill-received",
                                     f"A fill arrived after this order was {trade['status'].lower()}; "
                                     "it is journaled as a separate trade.", ts=filled_at, actor="journal")
                trade = None
            if trade is None:
                trade_id = self._create_from_fill(ev, context, direction, filled_at)
                trade = self.store.get_trade(trade_id)
            was_uncertain = trade["status"] == "UNCERTAIN"
            stop = _num(ev.get("stop")) if ev.get("stop") is not None else trade.get("initial_stop")
            target = _num(ev.get("target")) if ev.get("target") is not None else trade.get("initial_target")
            equity_before = _num(context.get("equity_before_trade")) or trade.get("account_balance_before")
            leverage = trade.get("leverage") or _num(ev.get("leverage"))
            risk = risk_fields(direction=direction, entry=price, stop=stop, target=target,
                               quantity=quantity, equity_before=equity_before,
                               max_allowed_risk_pct=trade.get("max_allowed_risk_pct"),
                               max_allowed_risk_amount=trade.get("max_allowed_risk_amount"),
                               leverage=leverage)
            slippage = None
            if requested is not None and price is not None:
                slippage = (price - requested) if direction == "LONG" else (requested - price)
            update = {
                "status": "OPEN", "position_id": ev.get("position_id"),
                "execution_id": ev.get("execution_id"), "entry_filled_at": filled_at,
                "entry_price": price, "quantity": quantity,
                "requested_entry_price": trade.get("requested_entry_price") or requested,
                "entry_slippage": slippage,
                "entry_slippage_cost": (slippage * quantity) if (slippage is not None and quantity) else None,
                "initial_stop": stop, "initial_target": target,
                "current_stop": stop, "current_target": target,
                "account_balance_before": equity_before,
                "leverage": leverage, "leverage_source": trade.get("leverage_source") or ev.get("leverage_source"),
                **{k: v for k, v in risk.items() if k not in ("max_allowed_risk_pct", "max_allowed_risk_amount")},
                **{k: v for k, v in timing_fields(filled_at, preferred_window=self.preferred_window).items()
                   if k != "duration_s"},
                "entry_locked": 1,
            }
            update = {k: v for k, v in update.items() if v is not None}
            if was_uncertain:
                update.update({"result": None, "result_reason": None, "is_operational": 0})
            self.store.update_trade(trade_id, update)
            self.store.add_link(trade_id, "LEDGER_TRADE", ledger_trade_id)
            self.store.add_link(trade_id, "LEDGER_POSITION", ev.get("position_id") or "")
            fee_rate = _num(ev.get("fee_rate"))
            self.store.add_execution(trade_id, {
                "execution_id": ev.get("execution_id") or f"entry:{ledger_trade_id or trade_id}",
                "kind": "ENTRY", "side": "BUY" if direction == "LONG" else "SELL",
                "quantity": quantity, "requested_price": requested, "price": price,
                "fee": None, "slippage": slippage,
                "slippage_cost": (slippage * quantity) if (slippage is not None and quantity) else None,
                "liquidity": "MAKER" if ev.get("maker") else "TAKER", "executed_at": filled_at,
                "source_ref": ledger_trade_id or None,
            })
            self._levels[trade_id] = (stop, target)
            self.store.add_event(trade_id, "order-filled",
                                 f"{direction} {_q(quantity)} @ {_q(price)}"
                                 + (f" (requested {requested}, slippage {slippage:+.8g})"
                                    if slippage is not None else ""),
                                 ts=filled_at, actor="execution-engine",
                                 payload={"execution_id": ev.get("execution_id"),
                                          "fee_rate": fee_rate,
                                          "quote": {k: context.get(k) for k in ("fill_bid", "fill_ask", "fill_mark")
                                                    if context.get(k) is not None} or None})
            self.store.add_event(trade_id, "trade-opened", f"Position {ev.get('position_id') or ''} open",
                                 ts=filled_at, actor="position-manager")
            if context.get("fill_timestamp"):
                # Only a forward-paper fill (a later quote) needs the legacy
                # entry recreated. On an immediate fill the pipeline writes its
                # own entry as soon as this hook returns; writing it here first
                # made that insert fail.
                self._backfill_legacy_entry(trade_id, ledger_trade_id)
            return trade_id

    def _create_from_fill(self, ev: dict, context: dict, direction: str, filled_at: str) -> str:
        """An engine fill with no registered decision (direct engine use, or a
        decision recorded by an older build). The trade is real, so it is
        journaled; the decision snapshot honestly says it was not captured."""
        provenance = dict(ev.get("provenance") or {})
        strategy = context.get("strategy") or ev.get("strategy_id") or "Unattributed"
        exchange, market_type = exchange_identity(provenance.get("exchange"), provenance.get("instrument_type"))
        base, quote = split_symbol(ev.get("symbol") or "")
        scope = ev.get("scope") or ""
        fields = {
            "source_system": "ENGINE", "source_trade_key": f"ledger:{ev.get('ledger_trade_id') or uuid.uuid4().hex}",
            "order_id": ev.get("order_id") or None, "instance_id": scope or None,
            "instance_name": provenance.get("instance_name"),
            "bot_id": scope or "legacy-paper-engine",
            "simulation_session_id": ev.get("simulation_session_id"),
            "strategy_id": provenance.get("strategy_id") or strategy,
            "strategy_name": provenance.get("strategy_name") or strategy,
            "strategy_family": strategy_family(provenance.get("strategy_id") or strategy, strategy),
            "strategy_version": context.get("strategy_version") or provenance.get("strategy_version"),
            "trade_source": "TRADING_INSTANCE" if scope else "UNATTRIBUTED",
            "trading_mode": trading_mode_for(provenance) if provenance else "UNKNOWN",
            "exchange": exchange, "market_type": market_type,
            "symbol": str(ev.get("symbol") or "").upper(), "base_asset": base, "quote_asset": quote,
            "direction": direction, "timeframe": context.get("timeframe"), "status": "PENDING",
            "signal_at": _iso(context.get("signal_timestamp")),
            "order_created_at": _iso(context.get("order_timestamp")) or filled_at,
            "requested_entry_price": _num(context.get("requested_price") or context.get("signal_price")),
            "account_balance_before": _num(context.get("equity_before_trade")),
            "leverage": _num(ev.get("leverage")), "leverage_source": ev.get("leverage_source"),
            "data_completeness": "FILL_ONLY_NO_DECISION_SNAPSHOT",
        }
        trade_id, _ = self.store.create_trade(fields)
        self.store.add_event(trade_id, "decision-not-captured",
                             "The fill arrived without a registered decision; entry reasoning was not captured.",
                             ts=filled_at, actor="journal")
        return trade_id

    def _backfill_legacy_entry(self, trade_id: str, ledger_trade_id: str) -> None:
        """Forward-paper fills arrive after the pipeline returned, so the
        legacy decision journal (which feeds trade memory) never saw them.
        Recreate its entry from the frozen snapshot, once."""
        legacy = self.legacy_journal
        if legacy is None or not ledger_trade_id:
            return
        try:
            if legacy.store.get(ledger_trade_id) is not None:
                return
            trade = self.store.get_trade(trade_id) or {}
            snap = self.store.snapshot(trade_id) or {}
            if not snap or snap.get("source") != "SIGNAL_PIPELINE":
                return
            risk = snap.get("risk") or {}
            steps = [{"rule": g.get("rule"), "passed": g.get("status") == "PASSED", "detail": g.get("detail")}
                     for g in risk.get("gates") or []]
            reads = [{"name": c.get("name"), "status": "Passed", "detail": c.get("detail")}
                     for c in snap.get("conditions_passed") or []]
            reads += [{"name": c.get("name"), "status": "Failed", "detail": c.get("detail")}
                      for c in snap.get("conditions_failed") or []]
            provenance = trade.get("provenance") if isinstance(trade.get("provenance"), dict) else {}
            legacy.record_entry(
                trade_id=ledger_trade_id, mode="paper", symbol=trade["symbol"],
                side=trade["direction"].lower(), strategy=trade.get("strategy_name") or "Strategy",
                timeframe=trade.get("timeframe") or "", entry=trade["entry_price"],
                stop=trade.get("initial_stop"), target=trade.get("initial_target"),
                size=trade["quantity"], equity=trade.get("account_balance_before") or 0.0,
                confidence=trade.get("confidence") or 0.0, brain_score=trade.get("setup_score"),
                regime=trade.get("market_regime") or "", steps=steps,
                payload={"reason": snap.get("decision_reason"), "brain_checklist": reads,
                         "snapshot": snap.get("market_context") or {},
                         "journal_sizing": risk.get("sizing") or {},
                         "journal_quality_gate": risk.get("quality_gate"),
                         "journal_execution": provenance},
                position_id=trade.get("position_id") or "")
        except Exception as exc:  # noqa: BLE001 — legacy view only
            self._log(f"legacy journal backfill failed: {type(exc).__name__}: {exc}")

    def canonical_for_ledger_trade(self, ledger_trade_id: str) -> Optional[str]:
        return self.store.resolve_link("LEDGER_TRADE", ledger_trade_id)

    def original_ledger_trade_id(self, ledger_trade_id: str) -> str:
        """After a partial exit the ledger opens a remainder row with a new id.
        Return the id of the first ledger row of the same canonical trade, which
        is the id the legacy decision journal knows."""
        trade_id = self.canonical_for_ledger_trade(ledger_trade_id)
        if not trade_id:
            return ledger_trade_id
        for link in self.store.links(trade_id):
            if link["link_type"] == "LEDGER_TRADE":
                return link["ref"]
        return ledger_trade_id

    def on_partial_exit(self, ev: dict) -> Optional[str]:
        with self._lock:
            trade_id = self.canonical_for_ledger_trade(str(ev.get("ledger_trade_id") or ""))
            if trade_id is None:
                self._log(f"partial exit for unjournaled ledger trade {ev.get('ledger_trade_id')}")
                return None
            self.store.add_link(trade_id, "LEDGER_TRADE", ev.get("remainder_trade_id") or "")
            self.store.add_link(trade_id, "LEDGER_POSITION", ev.get("remainder_position_id") or "")
            added = self._record_exit_leg(trade_id, ev, kind="PARTIAL_EXIT")
            if added:
                trade = self.store.get_trade(trade_id)
                self.store.update_trade(trade_id, {"partial_exit_count": int(trade["partial_exit_count"] or 0) + 1,
                                                   "status": "PARTIALLY_CLOSED"})
                self.store.add_modification(
                    trade_id, field="SIZE", old_value=_num(ev.get("size_before")),
                    new_value=_num(ev.get("remaining_size")), reason="PARTIAL_EXIT",
                    actor="position-manager", detail=f"scaled out {ev.get('quantity')} @ {ev.get('price')}",
                    modified_at=_iso(ev.get("executed_at")))
                self._aggregate(trade_id, final=False)
            return trade_id

    def on_exit_fill(self, ev: dict) -> Optional[str]:
        with self._lock:
            trade_id = self.canonical_for_ledger_trade(str(ev.get("ledger_trade_id") or ""))
            if trade_id is None:
                self._log(f"exit for unjournaled ledger trade {ev.get('ledger_trade_id')}")
                return None
            context = dict(ev.get("exit_context") or {})
            self._record_exit_leg(trade_id, ev, kind="EXIT")
            self._finalise(trade_id, context, executed_at=_iso(ev.get("executed_at")))
            return trade_id

    def _record_exit_leg(self, trade_id: str, ev: dict, *, kind: str) -> bool:
        trade = self.store.get_trade(trade_id)
        direction = trade["direction"]
        quantity, price = _num(ev.get("quantity")), _num(ev.get("price"))
        requested = _num(ev.get("requested_price"))
        slippage = None
        if requested is not None and price is not None:
            # an exit sells a long / buys a short: worse = lower / higher
            slippage = (requested - price) if direction == "LONG" else (price - requested)
        executed_at = _iso(ev.get("executed_at")) or _now()
        execution_id = ev.get("execution_id") or f"{kind.lower()}:{ev.get('ledger_trade_id')}:{executed_at}"
        added = self.store.add_execution(trade_id, {
            "execution_id": execution_id, "kind": kind,
            "side": "SELL" if direction == "LONG" else "BUY", "quantity": quantity,
            "requested_price": requested, "price": price, "fee": _num(ev.get("fee")),
            "slippage": slippage,
            "slippage_cost": (slippage * quantity) if (slippage is not None and quantity) else None,
            "realized_gross_pnl": _num(ev.get("gross_pnl")), "liquidity": "TAKER",
            "executed_at": executed_at, "source_ref": ev.get("ledger_trade_id"),
        })
        if not added:
            return False
        fee, fee_rate = _num(ev.get("fee")), _num(ev.get("fee_rate"))
        entry = trade.get("entry_price")
        if fee is not None and fee > 0:
            if ev.get("fee_split") == "round_trip" and entry and price and quantity:
                # The paper engine books entry and exit commission together at
                # the exit; split it exactly by notional so both are visible.
                entry_part = fee * abs(entry) / (abs(entry) + abs(price))
                self.store.add_fee(trade_id, fee_type="ENTRY_COMMISSION", amount=entry_part,
                                   source_ref=execution_id, rate=fee_rate, basis=abs(entry * quantity))
                self.store.add_fee(trade_id, fee_type="EXIT_COMMISSION", amount=fee - entry_part,
                                   source_ref=execution_id, rate=fee_rate, basis=abs(price * quantity))
            else:
                self.store.add_fee(trade_id, fee_type=ev.get("fee_type") or "EXIT_COMMISSION",
                                   amount=fee, source_ref=execution_id, rate=fee_rate,
                                   basis=abs((price or 0) * (quantity or 0)) or None)
        self.store.add_event(trade_id, "partial-exit" if kind == "PARTIAL_EXIT" else "exit-filled",
                             f"{kind.replace('_', ' ').lower()} {_q(quantity)} @ {_q(price)}"
                             + (f" (gross {float(ev['gross_pnl']):+.2f}, fee {fee:.4f})"
                                if ev.get("gross_pnl") is not None and fee is not None else ""),
                             ts=executed_at, actor="execution-engine",
                             payload={"execution_id": execution_id})
        return True

    def add_funding(self, trade_id: str, *, amount: float, source_ref: str, at: Optional[str] = None,
                    rate: Optional[float] = None) -> None:
        if self.store.add_fee(trade_id, fee_type="FUNDING", amount=float(amount), source_ref=source_ref, rate=rate):
            self.store.add_event(trade_id, "funding", f"funding {float(amount):+.6f}", ts=_iso(at) or _now(),
                                 actor="account-ledger")

    def _aggregate(self, trade_id: str, *, final: bool) -> dict:
        trade = self.store.get_trade(trade_id)
        execs = self.store.executions(trade_id)
        fees = self.store.fees(trade_id)
        exits = [e for e in execs if e["kind"] in ("PARTIAL_EXIT", "EXIT")]
        closed_qty = sum(float(e["quantity"] or 0) for e in exits)
        exit_price = (sum(float(e["quantity"] or 0) * float(e["price"] or 0) for e in exits) / closed_qty
                      if closed_qty > _EPS else None)
        gross = sum(float(e["realized_gross_pnl"] or 0) for e in exits) if exits else None
        commissions = sum(float(f["amount"]) for f in fees if f["fee_type"] != "FUNDING")
        funding = sum(float(f["amount"]) for f in fees if f["fee_type"] == "FUNDING")
        has_funding = any(f["fee_type"] == "FUNDING" for f in fees)
        slip = [e["slippage_cost"] for e in execs if e.get("slippage_cost") is not None]
        net = (gross - commissions - funding) if gross is not None else None
        risk = _num(trade.get("risk_amount"))
        update = {
            "closed_quantity": closed_qty or None, "exit_price": exit_price, "gross_pnl": gross,
            "fees_total": commissions if (fees or gross is not None) else None,
            # A lab broker's funding ledger is the source of truth: nothing
            # booked means this trade paid 0. The instance engine does not
            # model funding at all, so there it stays unknown.
            "funding_total": funding if has_funding else (
                0.0 if trade.get("source_system") in FUNDING_MODELLED_SOURCES else None),
            "slippage_cost_total": sum(slip) if slip else None, "net_pnl": net,
            "gross_r": (gross / risk) if (gross is not None and risk) else None,
            "realised_r": (net / risk) if (net is not None and risk) else None,
            "pnl_pct": (net / trade["account_balance_before"] * 100)
            if (net is not None and trade.get("account_balance_before")) else None,
            "return_on_margin_pct": (net / trade["margin_used"] * 100)
            if (net is not None and trade.get("margin_used")) else None,
        }
        if exits:
            update["exit_at"] = max(e["executed_at"] for e in exits if e.get("executed_at"))
        self.store.update_trade(trade_id, {k: v for k, v in update.items() if v is not None or not final})
        return {"exits": exits, **update}

    def _finalise(self, trade_id: str, context: dict, *, executed_at: Optional[str] = None,
                  status: str = "CLOSED") -> None:
        trade = self.store.get_trade(trade_id)
        if trade is None or trade.get("finalised_at"):
            return
        agg = self._aggregate(trade_id, final=True)
        trade = self.store.get_trade(trade_id)
        mods = self.store.modifications(trade_id)
        reason = normalize_exit_reason(context.get("exit_reason"))
        reason_source = context.get("exit_reason_source") or ("EXECUTION_ENGINE" if context.get("exit_reason")
                                                             else "NOT_RECORDED")
        if reason == "STOP_LOSS":
            # the engine reports "stop"; the journal knows whether that stop had
            # been moved to break-even or trailed when it was hit.
            stops = [m for m in mods if m["field"] == "STOP_LOSS"]
            if stops:
                last = stops[-1]
                if last["reason"] == "BREAK_EVEN":
                    reason = "BREAK_EVEN_STOP"
                elif last["reason"] == "TRAILING":
                    reason = "TRAILING_STOP"
        legs = [float(e["realized_gross_pnl"] or 0) for e in agg["exits"]]
        result = classify_result(net_pnl=trade.get("net_pnl"), risk_amount=trade.get("risk_amount"),
                                 notional=trade.get("notional_value"),
                                 leg_pnls=legs if trade.get("partial_exit_count") else ())
        exit_at = trade.get("exit_at") or executed_at or _now()
        excursions = excursion_fields(
            direction=trade["direction"], entry=trade.get("entry_price"), quantity=trade.get("quantity"),
            risk_amount=trade.get("risk_amount"), mfe_price=_num(context.get("mfe_price")),
            mae_price=_num(context.get("mae_price")), mfe_r=_num(context.get("mfe_r")),
            mae_r=_num(context.get("mae_r")),
            source=context.get("excursion_source") or "ENGINE_TRACKED_CLOSED_BARS")
        timing = timing_fields(trade.get("entry_filled_at"), exit_at)
        snapshot = self.store.snapshot(trade_id)
        provisional = {**trade, "exit_reason": reason, "result": result, **excursions}
        violations = journal_review.rule_violations(provisional, snapshot, mods)
        self.store.update_trade(trade_id, {
            "status": status, "exit_reason": reason, "exit_reason_source": reason_source,
            "exit_at": exit_at, "exit_at_london": timing.get("exit_at_london"),
            "duration_s": timing.get("duration_s"), "result": result,
            "counts_in_stats": 1 if result else 0, "is_operational": 0,
            "rule_violation": 1 if violations else 0, "rule_violation_count": len(violations),
            **{k: v for k, v in excursions.items() if trade.get(k) is None},
            "entry_locked": 1, "finalised_at": _now(),
        })
        final = self.store.get_trade(trade_id)
        net = final.get("net_pnl")
        self.store.add_event(trade_id, "trade-closed",
                             f"{result or 'UNCLASSIFIED'} · {reason}"
                             + (f" · net {net:+.2f}" if net is not None else "")
                             + (f" · {final['realised_r']:+.2f}R" if final.get("realised_r") is not None else ""),
                             ts=exit_at, actor="position-manager")
        self.store.add_event(trade_id, "journal-finalised", "Trade facts locked; corrections require an audit entry.",
                             ts=_now(), actor="journal")
        self._levels.pop(trade_id, None)
        if self.review_on_close and result:
            self.review(trade_id)

    def review(self, trade_id: str) -> Optional[dict]:
        """Run the trade review agent and store its review separately."""
        trade = self.store.get_trade(trade_id)
        if trade is None:
            return None
        review = journal_review.build_review(trade, self.store.snapshot(trade_id),
                                             self.store.modifications(trade_id))
        stored = self.store.add_review(trade_id, review)
        self.store.add_event(trade_id, "review-generated",
                             f"{review['reviewer']} grade {review['grade']}", ts=_now(), actor=review["reviewer"])
        return stored

    def on_protection_change(self, ev: dict) -> None:
        """Stop/target moved after entry. The original levels stay in
        initial_*; each change is its own modification row."""
        with self._lock:
            trade_id = None
            if ev.get("ledger_trade_id"):
                trade_id = self.canonical_for_ledger_trade(str(ev["ledger_trade_id"]))
            if trade_id is None:
                # only trades this execution engine owns; a lab position on the
                # same symbol (labs carry no instance id) is never matched
                open_rows = self.store.open_trades(instance_id=ev.get("scope") or "",
                                                   symbol=str(ev.get("symbol") or "").upper(),
                                                   source_systems=ENGINE_SOURCES)
                trade_id = open_rows[0]["trade_id"] if open_rows else None
            if trade_id is None:
                return
            if trade_id not in self._levels:
                trade = self.store.get_trade(trade_id)
                self._levels[trade_id] = (trade.get("current_stop"), trade.get("current_target"))
            old_stop, old_target = self._levels[trade_id]
            new_stop, new_target = _num(ev.get("stop")), _num(ev.get("target"))
            changes = {}
            at = _iso(ev.get("at")) or _now()
            management = ev.get("management") or {}
            trade = None
            if new_stop is not None and (old_stop is None or abs(new_stop - old_stop) > 1e-12):
                trade = self.store.get_trade(trade_id)
                reason = ev.get("reason")
                if not reason:
                    entry = trade.get("entry_price")
                    if management.get("be") and entry is not None and abs(new_stop - entry) <= max(1e-9, abs(entry) * 1e-6):
                        reason = "BREAK_EVEN"
                    elif old_stop is not None and ((trade["direction"] == "LONG" and new_stop > old_stop) or
                                                   (trade["direction"] == "SHORT" and new_stop < old_stop)):
                        reason = "TRAILING" if not management.get("be") or new_stop != entry else "BREAK_EVEN"
                    else:
                        reason = "STRATEGY"
                self.store.add_modification(trade_id, field="STOP_LOSS", old_value=old_stop, new_value=new_stop,
                                            reason=reason, actor=ev.get("actor") or "position-manager",
                                            modified_at=at)
                label = {"BREAK_EVEN": "Stop moved to break-even", "TRAILING": "Trailing stop advanced",
                         "MANUAL": "Stop changed manually"}.get(reason, "Stop changed")
                self.store.add_event(trade_id, "stop-modified", f"{label}: {old_stop} → {new_stop}", ts=at,
                                     actor=ev.get("actor") or "position-manager")
                changes["current_stop"] = new_stop
            if new_target is not None and (old_target is None or abs(new_target - old_target) > 1e-12):
                reason = ev.get("reason") or "STRATEGY"
                self.store.add_modification(trade_id, field="TAKE_PROFIT", old_value=old_target,
                                            new_value=new_target, reason=reason,
                                            actor=ev.get("actor") or "position-manager", modified_at=at)
                self.store.add_event(trade_id, "target-modified", f"Target changed: {old_target} → {new_target}",
                                     ts=at, actor=ev.get("actor") or "position-manager")
                changes["current_target"] = new_target
            if changes:
                self.store.update_trade(trade_id, changes)
                self._levels[trade_id] = (changes.get("current_stop", old_stop),
                                          changes.get("current_target", old_target))

    # ------------------------------------------------------------ operator actions
    def cancel_open_for_instance(self, instance_id: str, *, reason: str) -> int:
        """Simulation account restart: open trades end without a fabricated fill."""
        return self._cancel_open(
            ("PENDING", "OPEN", "PARTIALLY_CLOSED", "UNCERTAIN"),
            lambda trade: (trade.get("instance_id") or "") == instance_id,
            reason=reason, event="simulation-account-restarted")

    def cancel_open_engine_trades(self, *, reason: str) -> int:
        """Paper account reset (initial-capital change): the paper ledger lost
        every trade and position, across instances, so each open paper-engine
        trade ends without a fabricated fill. Lab trades are untouched; they
        live in their own ledgers. Pending orders keep their own lifecycle."""
        return self._cancel_open(
            ("OPEN", "PARTIALLY_CLOSED"),
            lambda trade: trade.get("source_system") in ENGINE_SOURCES,
            reason=reason, event="paper-account-reset")

    def _cancel_open(self, statuses, include, *, reason: str, event: str) -> int:
        with self._lock:
            count = 0
            for trade in self.store.trades_with_status(statuses):
                if not include(trade):
                    continue
                now = _now()
                partial = trade["status"] == "PARTIALLY_CLOSED"
                self._aggregate(trade["trade_id"], final=False)
                self.store.update_trade(trade["trade_id"], {
                    "status": "CANCELLED", "result": "CANCELLED", "exit_reason": "SIMULATION_RESET",
                    "exit_reason_source": "OPERATOR", "result_reason": reason[:500],
                    "is_operational": 1, "counts_in_stats": 0, "entry_locked": 1, "finalised_at": now,
                })
                self.store.add_event(trade["trade_id"], event,
                                     reason + (" Realised partial exits are kept." if partial else ""),
                                     ts=now, actor="operator")
                count += 1
            return count

    # ------------------------------------------------------------ reconciliation
    def reconcile_ledger(self, ledger, *, mode_resolver: Optional[Callable[[str], dict]] = None,
                         grace_s: int = RECONCILE_GRACE_S,
                         pending_uncertain_after_s: int = PENDING_UNCERTAIN_AFTER_S) -> dict:
        """Restore missing journal information from the ledger, safely.

        * a ledger trade with no journal record gets one (RECONCILED_FROM_LEDGER)
        * a remainder row left by a partial exit is linked to its parent trade
        * an OPEN journal trade whose ledger row closed gets that close applied
        * a PENDING order that never filled becomes EXECUTION_UNCERTAIN

        It only fills gaps: facts already recorded are never overwritten, and
        running it twice (or after a restart) changes nothing the second time.
        """
        summary = {"created": 0, "linked_remainders": 0, "closed": 0, "uncertain": 0, "skipped_recent": 0}
        # Read the ledger before taking the recorder lock, so live fill hooks
        # never wait on a history read. Anything a hook records in between is
        # seen as linked/closed below; the grace window covers hooks in flight.
        rows = sorted(ledger.get_paper_trades(), key=lambda r: (r.get("opened_at") or "", r.get("id") or ""))
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_s)
        linked = self.store.linked_refs("LEDGER_TRADE")
        for row in rows:
            if row["id"] in linked:
                continue
            opened = parse_ts(row.get("opened_at"))
            if opened is not None and opened > cutoff:
                summary["skipped_recent"] += 1
                continue
            # One row per lock hold, so a live fill hook waits for at most one
            # row's writes. Re-checked under the lock: a hook may have linked
            # the row since the read. Rows run in opening order, so a parent
            # is in ``linked`` before its remainder is reached.
            with self.bulk_item():
                if self.store.resolve_link("LEDGER_TRADE", row["id"]):
                    linked.add(row["id"])
                    continue
                parent = self._remainder_parent(row, rows, linked)
                if parent is not None:
                    trade_id = self.store.resolve_link("LEDGER_TRADE", parent["id"])
                    self.store.add_link(trade_id, "LEDGER_TRADE", row["id"])
                    linked.add(row["id"])
                    self._reconcile_partial(trade_id, parent)
                    summary["linked_remainders"] += 1
                    continue
                self._create_from_ledger(row, mode_resolver)
                linked.add(row["id"])
                summary["created"] += 1
        # apply ledger closes the live hook never delivered
        by_id = {r["id"]: r for r in rows}
        for listed in self.store.trades_with_status(("OPEN", "PARTIALLY_CLOSED")):
            # one trade per lock hold, re-read under the lock: a live hook may
            # have closed the trade or linked a new leg since the list was read
            with self.bulk_item():
                trade = self.store.get_trade(listed["trade_id"])
                if trade is None or trade["status"] not in ("OPEN", "PARTIALLY_CLOSED"):
                    continue
                refs = [l["ref"] for l in self.store.links(trade["trade_id"]) if l["link_type"] == "LEDGER_TRADE"]
                ledger_rows = [by_id[r] for r in refs if r in by_id]
                if not ledger_rows:
                    continue
                last = max(ledger_rows, key=lambda r: r.get("opened_at") or "")
                if last.get("status") != "closed":
                    continue
                closed = parse_ts(last.get("closed_at"))
                if closed is not None and closed > cutoff:
                    summary["skipped_recent"] += 1
                    continue
                for leg in sorted(ledger_rows, key=lambda r: r.get("opened_at") or ""):
                    if leg.get("status") == "closed":
                        self._ledger_exit_leg(trade["trade_id"], leg, final=leg is last)
                self._finalise(trade["trade_id"], {"exit_reason": None,
                                                   "exit_reason_source": "RECONCILED_FROM_LEDGER"},
                               executed_at=_iso(last.get("closed_at")))
                self.store.add_event(trade["trade_id"], "reconciled-close",
                                     "Close restored from the ledger after the live hook did not record it.",
                                     ts=_now(), actor="reconciliation")
                summary["closed"] += 1
        summary["uncertain"] = self.mark_stale_pending(pending_uncertain_after_s)
        return summary

    def mark_stale_pending(self, pending_uncertain_after_s: int = PENDING_UNCERTAIN_AFTER_S) -> int:
        """A PENDING order that has neither filled nor been cancelled within
        the window becomes EXECUTION_UNCERTAIN. Reads only PENDING trades."""
        marked = 0
        with self._lock:
            stale = datetime.now(timezone.utc) - timedelta(seconds=pending_uncertain_after_s)
            for trade in self.store.trades_with_status(("PENDING",)):
                created = parse_ts(trade.get("order_created_at") or trade.get("created_at"))
                if created is not None and created < stale:
                    self.mark_order_outcome(trade["trade_id"], "EXECUTION_UNCERTAIN",
                                            f"Order submitted {trade.get('order_created_at')} has neither filled "
                                            "nor been cancelled; execution state is unknown.")
                    marked += 1
        return marked

    @staticmethod
    def _remainder_parent(row: dict, rows: list[dict], linked: set) -> Optional[dict]:
        """A reduce closes the parent row and opens the remainder at the same
        instant with the same instance, symbol, side and entry."""
        if row.get("alert_id"):
            return None
        for other in rows:
            if (other is not row and other.get("status") == "closed" and other["id"] in linked
                    and other.get("closed_at") == row.get("opened_at")
                    and (other.get("instance_id") or "") == (row.get("instance_id") or "")
                    and other.get("symbol") == row.get("symbol") and other.get("side") == row.get("side")
                    and abs(float(other.get("entry") or 0) - float(row.get("entry") or 0)) < 1e-12):
                return other
        return None

    def _reconcile_partial(self, trade_id: str, parent: dict) -> None:
        """The parent row of a remainder closed as a partial exit."""
        self._ledger_exit_leg(trade_id, parent, final=False)

    def _ledger_exit_leg(self, trade_id: str, row: dict, *, final: bool) -> None:
        trade = self.store.get_trade(trade_id)
        size, exit_price, pnl = _num(row.get("size")), _num(row.get("exit")), _num(row.get("pnl"))
        fees = _num(row.get("fees")) or 0.0
        if size is None or exit_price is None or pnl is None:
            return
        gross = pnl + fees
        execution_id = f"ledger:{row['id']}:close"
        if self.store.has_execution(execution_id):
            return
        if any(e["kind"] in ("EXIT", "PARTIAL_EXIT") and e.get("source_ref") == row["id"]
               for e in self.store.executions(trade_id)):
            return
        self._record_exit_leg(trade_id, {
            "ledger_trade_id": row["id"], "execution_id": execution_id, "quantity": size,
            "price": exit_price, "gross_pnl": gross, "fee": fees, "fee_split": "round_trip",
            "executed_at": row.get("closed_at"),
        }, kind="EXIT" if final else "PARTIAL_EXIT")
        if not final:
            self.store.update_trade(trade_id, {
                "partial_exit_count": int(trade.get("partial_exit_count") or 0) + 1,
                "status": "PARTIALLY_CLOSED"})
            self._aggregate(trade_id, final=False)

    def _create_from_ledger(self, row: dict, mode_resolver) -> str:
        instance_id = row.get("instance_id") or ""
        context = (mode_resolver(instance_id) if (mode_resolver and instance_id) else None) or {}
        direction = direction_of(row.get("side"))
        opened = _iso(row.get("opened_at"))
        base, quote = split_symbol(row.get("symbol") or "")
        exchange, market_type = exchange_identity(context.get("exchange"), context.get("instrument_type"))
        strategy = context.get("strategy_name") or (row.get("strategy_id") or "").split(":")[0] or None
        risk = risk_fields(direction=direction, entry=row.get("entry"), stop=row.get("stop"),
                           target=row.get("target"), quantity=row.get("size"),
                           equity_before=row.get("equity_before_trade"))
        recorded_risk = _num(row.get("risk_amount_at_entry"))
        fields = {
            "source_system": "LEDGER_RECONCILIATION", "source_trade_key": f"ledger:{row['id']}",
            "order_id": row.get("alert_id") or None, "instance_id": instance_id or None,
            "instance_name": context.get("instance_name"), "bot_id": instance_id or "legacy-paper-account",
            "simulation_session_id": row.get("simulation_session_id") or None,
            "strategy_id": context.get("strategy_id") or row.get("strategy_id") or None,
            "strategy_name": strategy, "strategy_family": strategy_family(row.get("strategy_id"), strategy)
            if strategy else None,
            "strategy_version": context.get("strategy_version"),
            "trade_source": "TRADING_INSTANCE" if instance_id else "UNATTRIBUTED",
            "trading_mode": trading_mode_for(context) if context else "UNKNOWN",
            "exchange": exchange, "market_type": market_type, "symbol": str(row.get("symbol") or "").upper(),
            "base_asset": base, "quote_asset": quote, "direction": direction,
            "timeframe": context.get("timeframe"), "status": "OPEN",
            "entry_filled_at": opened, "entry_price": _num(row.get("entry")), "quantity": _num(row.get("size")),
            "initial_stop": _num(row.get("stop")), "initial_target": _num(row.get("target")),
            "current_stop": _num(row.get("stop")), "current_target": _num(row.get("target")),
            "account_balance_before": _num(row.get("equity_before_trade")),
            "risk_pct": (_num(row.get("risk_pct_at_entry")) * 100) if _num(row.get("risk_pct_at_entry")) is not None
            else risk.get("risk_pct"),
            **{k: v for k, v in risk.items() if k not in ("risk_pct", "risk_rule_status")},
            "risk_rule_status": "UNKNOWN",
            "data_completeness": "RECONCILED_FROM_LEDGER",
            "entry_locked": 1,
            **{k: v for k, v in timing_fields(opened).items() if k != "duration_s"},
        }
        if recorded_risk is not None:
            fields["risk_amount"] = recorded_risk
        trade_id, _ = self.store.create_trade(fields, links=[("LEDGER_TRADE", row["id"])])
        self.store.add_execution(trade_id, {
            "execution_id": f"ledger:{row['id']}:open", "kind": "ENTRY",
            "side": "BUY" if direction == "LONG" else "SELL", "quantity": _num(row.get("size")),
            "price": _num(row.get("entry")), "executed_at": opened, "source_ref": row["id"],
        })
        self.store.add_event(trade_id, "reconciled-from-ledger",
                             "Journal record restored from the ledger; the decision snapshot was not captured "
                             "and is not reconstructed.", ts=opened or _now(), actor="reconciliation")
        return trade_id

    # ------------------------------------------------------------ legacy migration
    def migrate_legacy(self, legacy_store, ledger=None) -> dict:
        """Map every ``trade_decision_journal`` row into the canonical journal,
        once. Fields the legacy row did not record stay NULL."""
        summary = {"migrated": 0, "already": 0}
        # Both histories are read before the recorder lock is taken: an explicit
        # sync can run at any time, and live fill hooks must never wait on a
        # history read. The lock covers one row that still needs linking.
        ledger_rows = {}
        if ledger is not None:
            try:
                ledger_rows = {r["id"]: r for r in ledger.get_paper_trades()}
            except Exception:  # noqa: BLE001 — enrichment is optional
                ledger_rows = {}
        legacy_rows = legacy_store.list(limit=1_000_000)
        known = self.store.linked_refs("LEGACY_JOURNAL")
        pending = [row for row in legacy_rows if row["trade_id"] not in known]
        summary["already"] = len(legacy_rows) - len(pending)
        for legacy in pending:
            full = legacy_store.get(legacy["trade_id"]) or legacy
            # one row per lock hold, re-checked under the lock
            with self.bulk_item():
                if self.store.resolve_link("LEGACY_JOURNAL", legacy["trade_id"]):
                    summary["already"] += 1
                    continue
                owner = self.store.resolve_link("LEDGER_TRADE", legacy["trade_id"])
                if owner:
                    # the live recorder already owns this trade; just link it
                    self.store.add_link(owner, "LEGACY_JOURNAL", legacy["trade_id"])
                    summary["already"] += 1
                    continue
                self._migrate_one(full, legacy, ledger_rows.get(legacy["trade_id"]))
                summary["migrated"] += 1
        return summary

    def _migrate_one(self, full: dict, row: dict, ledger_row: Optional[dict]) -> str:
        sections = full.get("sections") or {}
        provenance = sections.get("provenance") or {}
        if not full.get("instance_id") and not provenance:
            mode = "UNKNOWN"
        else:
            mode = trading_mode_for({"execution_mode": full.get("execution_mode") if full.get("execution_mode")
                                     != "LEGACY / UNVERIFIED" else None,
                                     "market_data_mode": full.get("market_data_mode")})
        direction = direction_of(full.get("side"))
        sizing = (sections.get("risk_check") or {}).get("entry_sizing") or {}
        equity = _num(sizing.get("equity_before_trade")) or _num((ledger_row or {}).get("equity_before_trade"))
        quantity = _num(full.get("size"))
        risk = risk_fields(direction=direction, entry=full.get("entry"), stop=full.get("stop"),
                           target=full.get("target"), quantity=quantity, equity_before=equity)
        events = full.get("events") or []
        signal_at = _iso(events[0]["ts"]) if events else None
        entry_at = _iso((ledger_row or {}).get("opened_at")) or _iso(full.get("created_at"))
        exchange, market_type = exchange_identity(full.get("exchange") or provenance.get("exchange"),
                                                  provenance.get("instrument_type"))
        base, quote = split_symbol(full.get("symbol") or "")
        strategy_name = full.get("strategy_name") or full.get("strategy")
        status = {"open": "OPEN", "closed": "CLOSED", "cancelled": "CANCELLED"}.get(full.get("status"), "UNCERTAIN")
        recorded_risk = _num(full.get("risk_amount"))
        fields = {
            "source_system": "LEGACY_DECISION_JOURNAL", "source_trade_key": row["trade_id"],
            "instance_id": full.get("instance_id"), "instance_name": full.get("instance_name"),
            "bot_id": full.get("instance_id") or "legacy-auto-engine",
            "simulation_session_id": full.get("simulation_session_id"),
            "position_id": full.get("position_id"), "strategy_id": full.get("strategy_id") or strategy_name,
            "strategy_name": strategy_name,
            "strategy_family": strategy_family(full.get("strategy_id"), strategy_name),
            "strategy_version": full.get("strategy_version"),
            "trade_source": "TRADING_INSTANCE" if full.get("instance_id") else "AUTO_ENGINE",
            "trading_mode": mode, "exchange": exchange, "market_type": market_type,
            "symbol": str(full.get("symbol") or "").upper(), "base_asset": base, "quote_asset": quote,
            "direction": direction, "timeframe": full.get("timeframe") or None,
            "status": "OPEN" if status == "OPEN" else "PENDING",
            "signal_at": signal_at, "entry_filled_at": entry_at, "entry_price": _num(full.get("entry")),
            "quantity": quantity, "initial_stop": _num(full.get("stop")), "initial_target": _num(full.get("target")),
            "current_stop": _num(full.get("stop")), "current_target": _num(full.get("target")),
            "account_balance_before": equity,
            "risk_pct": _num(sizing.get("effective_risk_pct")) if sizing.get("effective_risk_pct") is not None
            else risk.get("risk_pct"),
            "confidence": _num(full.get("confidence")), "setup_score": _num(full.get("brain_score")),
            "market_regime": full.get("regime") or None, "decision": f"ENTER_{direction}",
            "data_completeness": "MIGRATED_FROM_LEGACY_JOURNAL", "entry_locked": 1,
            **{k: v for k, v in risk.items() if k not in ("risk_pct", "risk_rule_status")},
            "risk_rule_status": "UNKNOWN",
            **{k: v for k, v in timing_fields(entry_at).items() if k != "duration_s"},
        }
        if recorded_risk:
            fields["risk_amount"] = recorded_risk
        trade_id, _ = self.store.create_trade(fields, links=[("LEGACY_JOURNAL", row["trade_id"]),
                                                             ("LEDGER_TRADE", row["trade_id"])])
        entry_decision = sections.get("entry_decision") or {}
        checklist = sections.get("checklist") or {}
        passed, failed, missing = split_conditions(checklist.get("entry_reads") or [])
        family = fields["strategy_family"]
        self.store.save_snapshot(trade_id, {
            "captured_at": full.get("created_at"), "decision": f"ENTER_{direction}",
            "decision_reason": entry_decision.get("main_reason"),
            "conditions_passed": passed, "conditions_failed": failed, "conditions_missing": missing,
            "confidence": _num(full.get("confidence")), "setup_score": _num(full.get("brain_score")),
            "market_bias": full.get("regime"), "htf_bias": entry_decision.get("higher_timeframe_trend"),
            "risk_decision": (sections.get("risk_check") or {}).get("final_risk_decision"),
            "strategy_family": family, "setup": strategy_setup(family, checklist.get("entry_reads") or []),
            "market_context": sections.get("market_snapshot"),
            "risk": {"gates": checklist.get("risk_gates") or [], "sizing": sizing},
            "raw": {"legacy_sections": {k: v for k, v in sections.items()
                                        if k in ("entry_decision", "checklist", "market_snapshot", "risk_check")}},
            "source": "LEGACY_DECISION_JOURNAL",
        })
        for i, event in enumerate(events):
            self.store.add_event(trade_id, event.get("kind") or "event", event.get("detail") or "",
                                 ts=_iso(event.get("ts")) or full.get("created_at"), actor="legacy-journal",
                                 event_key=f"legacy:{row['trade_id']}:{i}")
        self.store.add_execution(trade_id, {
            "execution_id": f"legacy:{row['trade_id']}:entry", "kind": "ENTRY",
            "side": "BUY" if direction == "LONG" else "SELL", "quantity": quantity,
            "price": _num(full.get("entry")), "executed_at": entry_at, "source_ref": row["trade_id"],
        })
        if full.get("status") == "closed":
            exit_decision = sections.get("exit_decision") or {}
            fees = _num((ledger_row or {}).get("fees")) or 0.0
            net = _num(full.get("pnl"))
            exit_price = _num(full.get("exit"))
            exit_at = _iso((ledger_row or {}).get("closed_at")) or _iso(full.get("closed_at"))
            if net is not None and exit_price is not None and quantity:
                self._record_exit_leg(trade_id, {
                    "ledger_trade_id": row["trade_id"], "execution_id": f"legacy:{row['trade_id']}:exit",
                    "quantity": quantity, "price": exit_price, "gross_pnl": net + fees, "fee": fees,
                    "fee_split": "round_trip", "executed_at": exit_at,
                }, kind="EXIT")
            mfe = exit_decision.get("max_profit_r")
            mae = exit_decision.get("max_drawdown_r")
            self._finalise(trade_id, {
                "exit_reason": exit_decision.get("exit_reason"), "exit_reason_source": "LEGACY_JOURNAL",
                "mfe_r": mfe if isinstance(mfe, (int, float)) else None,
                "mae_r": mae if isinstance(mae, (int, float)) else None,
                "excursion_source": "LEGACY_JOURNAL_R",
            }, executed_at=exit_at)
            if exit_price is None and net is None:
                self.store.update_trade(trade_id, {"data_completeness": "MIGRATED_PARTIAL_NO_EXIT_FACTS"})
            legacy_review = sections.get("review")
            if legacy_review:
                self.store.add_review(trade_id, {
                    "reviewer": "legacy-post-trade-review", "review_version": "legacy",
                    "grade": legacy_review.get("grade"), "outcome": exit_decision.get("result"),
                    "mistakes": [legacy_review["mistake"]] if legacy_review.get("mistake") else [],
                    "improvement": legacy_review.get("improvement"),
                    "summary": "Migrated from the legacy decision journal review.",
                })
        elif full.get("status") == "cancelled":
            self.store.update_trade(trade_id, {"status": "CANCELLED", "result": "CANCELLED",
                                               "exit_reason": "SIMULATION_RESET", "is_operational": 1,
                                               "counts_in_stats": 0, "finalised_at": _now()})
        return trade_id
