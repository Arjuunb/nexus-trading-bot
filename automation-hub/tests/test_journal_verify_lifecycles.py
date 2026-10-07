"""Verification: canonical trade integrity (section 1) and partial exits (section 2).

Every scenario drives the production path end to end:

    SignalPipeline -> PaperExecutionEngine / ForwardPaperExecutionEngine
                   -> SqliteLedger (InstanceLedger for instance scope)
                   -> TradeJournalRecorder -> TradeJournalStore

with a fill model that charges half-spread + slippage on every fill and a taker
commission on both sides, so slippage and fees are always exercised. Stop moves
and scale-outs come from the real AutoStrategyEngine._check_exit driven by a
bot.tradecore TradeManager over synthetic bars.

Expected values are derived here from first principles: requested prices, the
fill model's configured rates, the sizing rule (risk % of equity / stop
distance), and timestamps (sessions via zoneinfo). They are compared to both the
ledger rows (paper_trades) and the canonical journal. The journal and analytics
functions under test are never used to produce an expectation.
"""
from __future__ import annotations

import copy
import gc
import threading
import types
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.types import Bar
from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from data.trade_journal_store import TradeJournalStore
from execution.paper_engine import ForwardPaperExecutionEngine, PaperExecutionEngine
from services import journal_analytics
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.decision_journal import DecisionJournal
from services.fill_model import RealisticFill
from services.signal_pipeline import SignalPipeline
from services.trade_journal import TradeJournalRecorder
from services.trade_manager import TradeManager
from services.trading_instances import InstanceLedger

# ---------------------------------------------------------------- fill model
SPREAD, SLIPPAGE, LATENCY, FEE = 0.0004, 0.0003, 0.0, 0.0004
#: per-side price impact of a taker fill: half the spread plus slippage (+ latency)
COST = SPREAD / 2 + SLIPPAGE + LATENCY          # 0.0005
EQUITY = 10_000.0
RISK_PCT = 0.01                                 # pipeline risk per trade
EXPOSURE_CAP = 0.5                              # pipeline per-trade notional cap

PROVENANCE = {
    "instance_id": None, "strategy_id": "smc_breaker", "strategy_name": "SMC Lab",
    "strategy_version": "2.3.1", "market_data_mode": "forward_paper", "execution_mode": "paper",
    "exchange": "binance_usdm", "instrument_type": "perpetual",
}
STRATEGY = ("smc_breaker", "SMC Lab", "2.3.1")
TABLES = ("journal_trades", "journal_trade_links", "journal_executions", "journal_fees",
          "journal_modifications", "journal_snapshots", "journal_events", "journal_reviews",
          "journal_corrections")


def _fill_model(**kw) -> RealisticFill:
    params = dict(spread_pct=SPREAD, slippage_pct=SLIPPAGE, latency_pct=LATENCY,
                  taker_fee_pct=FEE, partial_fill_prob=0.0, reject_prob=0.0)
    params.update(kw)
    return RealisticFill(**params)


def _buy(price: float) -> float:
    """A taker buy fills above the reference by half-spread + slippage."""
    return price * (1 + COST)


def _sell(price: float) -> float:
    """A taker sell fills below the reference by half-spread + slippage."""
    return price * (1 - COST)


def _ts(value: str) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _session(value: str) -> str:
    """Independent session classification in each centre's own local time."""
    utc = _ts(value)

    def is_open(zone: str, start: int, end: int) -> bool:
        local = utc.astimezone(ZoneInfo(zone))
        minutes = local.hour * 60 + local.minute
        return start * 60 <= minutes < end * 60

    london = is_open("Europe/London", 8, 17)
    new_york = is_open("America/New_York", 8, 17)
    asia = is_open("Asia/Tokyo", 9, 18)
    if london and new_york:
        return "LONDON_NY_OVERLAP"
    if london:
        return "LONDON"
    if new_york:
        return "NEW_YORK"
    if asia:
        return "ASIA"
    return "OFF_HOURS"


def _expected_result(net: float, risk: float, leg_gross: list[float]) -> str:
    """Documented rule (services/trade_journal.classify_result docstring and
    BREAKEVEN_R_BAND = 0.05R): |net| <= 0.05R is break-even; exit legs that
    disagree in sign make a PARTIAL_WIN / PARTIAL_LOSS by the sign of net."""
    band = 0.05 * risk
    n = len(leg_gross)
    mixed = n > 1 and any(g > band / n for g in leg_gross) and any(g < -band / n for g in leg_gross)
    if abs(net) <= band:
        return "BREAK_EVEN"
    if mixed:
        return "PARTIAL_WIN" if net > 0 else "PARTIAL_LOSS"
    return "WIN" if net > 0 else "LOSS"


# ---------------------------------------------------------------- harness
class _Spy:
    """Transparent proxy for the engine's journal: records every hook event so
    a test can replay it and prove the replay is a no-op."""

    def __init__(self, recorder):
        self._recorder = recorder
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        target = getattr(self._recorder, name)
        if not name.startswith("on_"):
            return target

        def hook(event):
            self.calls.append((name, copy.deepcopy(event)))
            return target(event)
        return hook


def _rig(*, ledger=None, scope="", forward=False, store=None, legacy_store=None, fill_model=None,
         context=None, initial_intents=None, intents_listener=None):
    ledger = ledger or SqliteLedger(":memory:")
    led = InstanceLedger(ledger, scope, "session-1") if scope else ledger
    if forward:
        paper = ForwardPaperExecutionEngine(led, EQUITY, fill_model=fill_model or _fill_model(),
                                            initial_intents=initial_intents,
                                            intents_listener=intents_listener)
    else:
        paper = PaperExecutionEngine(led, EQUITY, fill_model=fill_model or _fill_model())
    pipe = SignalPipeline(led, paper, TradingControl(), equity=EQUITY, risk_per_trade_pct=RISK_PCT,
                          exposure_limit_pct=EXPOSURE_CAP, max_total_exposure_pct=1.0,
                          adaptive_risk=False, equity_throttle=False)
    pipe.journal = DecisionJournal(legacy_store or JournalStore(":memory:"))
    store = store or TradeJournalStore(":memory:")
    rec = TradeJournalRecorder(store, legacy_journal=pipe.journal)
    spy = _Spy(rec)
    pipe.trade_journal = rec
    paper.journal = spy
    pipe.journal_context = dict(context or {**PROVENANCE, "instance_id": scope or None})
    paper.journal_provenance = pipe.journal_context
    return types.SimpleNamespace(ledger=ledger, led=led, paper=paper, pipe=pipe, store=store,
                                 rec=rec, spy=spy, scope=scope)


def _engine(rig, manager: TradeManager) -> AutoStrategyEngine:
    return AutoStrategyEngine(rig.pipe, rig.paper, rig.led, symbols=["BTCUSDT"], timeframe="5m",
                              strategy_factory=lambda _s: types.SimpleNamespace(label="SMC Lab"),
                              trade_manager=manager, entry_mode="market")


_BAR_T0 = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def _bar(i: int, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(_BAR_T0 + timedelta(minutes=5 * i), o, h, low, c, 10.0)


def _signal(rig, *, side="BUY", entry=100.0, stop=95.0, target=115.0, ts=None, alert=None,
            symbol="BTCUSDT"):
    ts = ts or datetime.now(timezone.utc).isoformat()
    alert = alert or f"auto:{rig.scope or 'legacy'}:{symbol}:5m:{ts}:{side.lower()}"
    return rig.pipe.process({
        "alert_id": alert, "symbol": symbol, "side": side, "entry": entry, "stop": stop,
        "target": target, "confidence": 1.0, "regime": "Trending", "strategy": "SMC Lab",
        "timeframe": "5m", "timestamp": ts, "mode": "paper",
        "reason": "HTF bullish + liquidity sweep + demand POI + rejection",
        "brain_checklist": [{"name": "HTF bullish", "status": "Passed"},
                            {"name": "Liquidity sweep", "status": "Passed"}]})


def _close(rig, price, *, reason, alert=None, symbol="BTCUSDT"):
    return rig.pipe.process({"alert_id": alert or f"close-{uuid.uuid4().hex[:10]}", "symbol": symbol,
                             "side": "CLOSE", "entry": price, "exit_reason": reason})


def _qty(entry: float, stop: float, equity: float = EQUITY, risk_pct: float = RISK_PCT,
         cap: float = EXPOSURE_CAP) -> float:
    """Sizing rule: risk % of equity over the stop distance, capped by the
    per-trade notional limit (both on the requested price)."""
    return min(risk_pct * equity / abs(entry - stop), cap * equity / entry)


def _count(store, table: str, trade_id: str | None = None) -> int:
    if trade_id is None:
        return int(store._c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return int(store._c.execute(f"SELECT COUNT(*) FROM {table} WHERE trade_id=?",
                                (trade_id,)).fetchone()[0])


def _counts(store) -> dict:
    return {t: _count(store, t) for t in TABLES}


def _only(store) -> dict:
    assert _count(store, "journal_trades") == 1
    rows = store.list_trades()
    assert len(rows) == 1
    return rows[0]


def _ledger_ids(store, trade_id) -> set:
    return {l["ref"] for l in store.links(trade_id) if l["link_type"] == "LEDGER_TRADE"}


def _assert_replay_and_reconcile_add_nothing(rig, *, ledger=None, mode_resolver=None) -> dict:
    """Replaying every engine hook and reconciling twice must add no row and
    change no fact."""
    ledger = ledger or rig.ledger
    before = _counts(rig.store)
    facts = {t["trade_id"]: {k: v for k, v in t.items() if k != "updated_at"}
             for t in rig.store.list_trades()}
    assert rig.spy.calls, "the engine reported nothing to the journal"
    for method, event in list(rig.spy.calls):
        getattr(rig.rec, method)(copy.deepcopy(event))
    first = rig.rec.reconcile_ledger(ledger, mode_resolver=mode_resolver, grace_s=0)
    second = rig.rec.reconcile_ledger(ledger, mode_resolver=mode_resolver, grace_s=0)
    zero = {"created": 0, "linked_remainders": 0, "closed": 0, "uncertain": 0, "skipped_recent": 0}
    assert first == zero and second == zero
    assert _counts(rig.store) == before
    after = {t["trade_id"]: {k: v for k, v in t.items() if k != "updated_at"}
             for t in rig.store.list_trades()}
    assert after == facts
    return before


def _verify_closed(store, ledger_rows, trade, *, qty, entry, stop, target, legs, direction,
                   exit_reason, result, trade_source, instance_id, trading_mode,
                   strategy=STRATEGY, order_link=True, requested_exits=None, extra_links=None,
                   fee_rates=(FEE,)):
    """Every section-1 integrity assertion for one closed canonical trade.

    ``legs`` is [(fill_price, quantity)] in execution order, computed by the
    caller from the requested price and the fill model's configured rates."""
    tid = trade["trade_id"]
    sign = 1.0 if direction == "LONG" else -1.0
    n_partials = len(legs) - 1
    assert trade["trade_ref"].startswith("TRD-")
    assert trade["status"] == "CLOSED" and trade["finalised_at"]
    assert trade["direction"] == direction
    # ---- the ledger rows of this trade, and only those
    assert _ledger_ids(store, tid) == {r["id"] for r in ledger_rows}
    assert len(ledger_rows) == len(legs)
    assert all(r["status"] == "closed" for r in ledger_rows)
    # ---- quantity / position size
    ledger_qty = sum(float(r["size"]) for r in ledger_rows)
    assert ledger_qty == pytest.approx(qty, rel=1e-9)
    assert trade["quantity"] == pytest.approx(qty, rel=1e-9)
    assert trade["closed_quantity"] == pytest.approx(trade["quantity"], rel=1e-12)
    assert trade["notional_value"] == pytest.approx(entry * qty, rel=1e-9)
    assert trade["margin_used"] == pytest.approx(entry * qty, rel=1e-9)   # 1x: margin == notional
    # ---- entry == actual fill
    for row in ledger_rows:
        assert float(row["entry"]) == pytest.approx(entry, rel=1e-10)
    assert trade["entry_price"] == pytest.approx(entry, rel=1e-10)
    assert trade["entry_price"] == pytest.approx(float(ledger_rows[0]["entry"]), rel=1e-12)
    # ---- exits == actual fills, quantity-weighted
    rows_by_close = sorted(ledger_rows, key=lambda r: (r["closed_at"], r["opened_at"]))
    for (price, q), row in zip(legs, rows_by_close):
        assert float(row["exit"]) == pytest.approx(price, rel=1e-10)
        assert float(row["size"]) == pytest.approx(q, rel=1e-9)
    vwap = sum(p * q for p, q in legs) / sum(q for _, q in legs)
    ledger_vwap = sum(float(r["exit"]) * float(r["size"]) for r in ledger_rows) / ledger_qty
    assert trade["exit_price"] == pytest.approx(vwap, rel=1e-10)
    assert trade["exit_price"] == pytest.approx(ledger_vwap, rel=1e-10)
    # ---- P&L and fees
    leg_gross = [(p - entry) * q * sign for p, q in legs]
    gross = sum(leg_gross)
    fees = sum(FEE * q * (entry + p) for p, q in legs)
    net = gross - fees
    assert trade["gross_pnl"] == pytest.approx(gross, rel=1e-9, abs=1e-9)
    assert sum(float(r["fees"]) for r in ledger_rows) == pytest.approx(fees, rel=1e-9)
    assert trade["fees_total"] == pytest.approx(fees, rel=1e-9)
    fee_rows = store.fees(tid)
    assert len(fee_rows) == 2 * len(legs)
    entry_comm = sum(f["amount"] for f in fee_rows if f["fee_type"] == "ENTRY_COMMISSION")
    exit_comm = sum(f["amount"] for f in fee_rows if f["fee_type"] == "EXIT_COMMISSION")
    assert {f["fee_type"] for f in fee_rows} == {"ENTRY_COMMISSION", "EXIT_COMMISSION"}
    assert entry_comm == pytest.approx(FEE * entry * qty, rel=1e-9)
    assert exit_comm == pytest.approx(sum(FEE * p * q for p, q in legs), rel=1e-9)
    assert entry_comm + exit_comm == pytest.approx(trade["fees_total"], rel=1e-12)
    assert all(f["rate"] in fee_rates for f in fee_rows)
    assert trade["net_pnl"] == pytest.approx(net, rel=1e-9, abs=1e-9)
    assert trade["net_pnl"] == pytest.approx(sum(float(r["pnl"]) for r in ledger_rows), rel=1e-9, abs=1e-9)
    assert trade["funding_total"] is None   # not modelled by the paper engine: unknown, not zero
    # ---- risk
    risk = abs(entry - stop) * qty
    assert trade["initial_stop"] == stop and trade["initial_target"] == target
    assert trade["risk_amount"] == pytest.approx(risk, rel=1e-9)
    assert trade["planned_rr"] == pytest.approx(abs(target - entry) / abs(entry - stop), rel=1e-9)
    assert trade["realised_r"] == pytest.approx(net / risk, rel=1e-9, abs=1e-12)
    assert trade["gross_r"] == pytest.approx(gross / risk, rel=1e-9, abs=1e-12)
    # ---- leverage
    assert trade["leverage"] == 1.0
    assert trade["leverage_source"] == "UNLEVERAGED_CASH_MODEL"
    # ---- session and London time from the entry fill time
    entry_at = _ts(trade["entry_filled_at"])
    assert trade["entry_session"] == _session(trade["entry_filled_at"])
    london = entry_at.astimezone(ZoneInfo("Europe/London"))
    assert trade["entry_hour_london"] == london.hour
    assert trade["entry_weekday"] == london.strftime("%A")
    assert _ts(trade["entry_at_london"]) == entry_at
    assert _ts(trade["entry_at_london"]).utcoffset() == london.utcoffset()
    # ---- provenance
    assert (trade["strategy_id"], trade["strategy_name"], trade["strategy_version"]) == strategy
    assert trade["trade_source"] == trade_source
    assert trade["instance_id"] == instance_id
    assert trade["trading_mode"] == trading_mode
    # ---- exit reason, result
    assert trade["exit_reason"] == exit_reason
    assert _expected_result(net, risk, leg_gross) == result
    assert trade["result"] == result
    assert trade["counts_in_stats"] is True and trade["is_operational"] is False
    # ---- executions: one entry, one per partial, one final exit
    execs = store.executions(tid)
    kinds = [e["kind"] for e in execs]
    assert sorted(kinds) == sorted(["ENTRY"] + ["PARTIAL_EXIT"] * n_partials + ["EXIT"])
    exits = [e for e in execs if e["kind"] in ("PARTIAL_EXIT", "EXIT")]
    assert sorted((round(e["price"], 8), round(e["quantity"], 8)) for e in exits) == \
        sorted((round(p, 8), round(q, 8)) for p, q in legs)
    entry_exec = next(e for e in execs if e["kind"] == "ENTRY")
    assert entry_exec["price"] == pytest.approx(entry, rel=1e-12)
    assert entry_exec["quantity"] == pytest.approx(qty, rel=1e-12)
    if requested_exits is not None:
        # exit slippage is the move from the requested price to the fill
        for e, requested in zip(sorted(exits, key=lambda e: e["executed_at"]), requested_exits):
            assert e["requested_price"] == requested
            assert e["slippage"] == pytest.approx(sign * (requested - e["price"]), rel=1e-9, abs=1e-12)
    # ---- duration == exit_at - entry_filled_at
    assert trade["exit_at"] == max(e["executed_at"] for e in exits)
    duration = (_ts(trade["exit_at"]) - entry_at).total_seconds()
    assert trade["duration_s"] == pytest.approx(duration, abs=1e-6)
    exit_london = _ts(trade["exit_at"]).astimezone(ZoneInfo("Europe/London"))
    assert _ts(trade["exit_at_london"]).utcoffset() == exit_london.utcoffset()
    # ---- links: no duplicates, exactly what the trade owns
    links = store.links(tid)
    by_type: dict[str, int] = {}
    for link in links:
        by_type[link["link_type"]] = by_type.get(link["link_type"], 0) + 1
    expected_links = {"LEDGER_TRADE": len(legs), "LEDGER_POSITION": len(legs)}
    if order_link:
        expected_links["ORDER"] = 1
    expected_links.update(extra_links or {})
    assert by_type == expected_links
    # ---- timeline: one of each terminal event
    kinds = [e["kind"] for e in store.events(tid)]
    assert kinds.count("exit-filled") == 1 and kinds.count("partial-exit") == n_partials
    assert kinds.count("trade-closed") == 1 and kinds.count("journal-finalised") == 1
    assert kinds.count("order-filled") == 1
    return {"gross": gross, "fees": fees, "net": net, "risk": risk, "legs": leg_gross}


def _evidence(tag: str, trade: dict, **extra) -> None:
    keys = ("trade_ref", "status", "result", "exit_reason", "quantity", "entry_price", "exit_price",
            "gross_pnl", "fees_total", "net_pnl", "risk_amount", "planned_rr", "realised_r",
            "leverage", "entry_session", "duration_s")
    print(f"EVIDENCE {tag} " + " ".join(f"{k}={trade.get(k)!r}" for k in keys)
          + "".join(f" {k}={v!r}" for k, v in extra.items()))


# ============================================================ SECTION 1
def test_s1_entry_then_take_profit_from_the_engine():
    rig = _rig()
    eng = _engine(rig, TradeManager())
    assert _signal(rig).accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)          # 20 units @ 100.05
    pending = _only(rig.store)
    assert pending["status"] == "OPEN"
    assert pending["requested_entry_price"] == 100.0
    assert pending["entry_slippage"] == pytest.approx(E - 100.0, rel=1e-9)
    # one bar that trades through the 115 target
    assert eng._check_exit("BTCUSDT", _bar(1, 101.0, 116.0, 100.5, 115.5)) is True
    X = _sell(115.0)
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(X, q)], direction="LONG", exit_reason="TAKE_PROFIT", result="WIN",
                         trade_source="AUTO_ENGINE", instance_id=None, trading_mode="FORWARD_PAPER",
                         requested_exits=[115.0])
    # excursion from the engine's tracked extremes, in price and currency
    assert trade["mfe_price"] == 116.0 and trade["mfe_amount"] == pytest.approx((116.0 - E) * q)
    assert trade["exit_reason_source"] == "EXECUTION_ENGINE"
    legacy = rig.pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S1", trade, expected_net=out["net"], ledger_rows=len(rows))


def test_s2_short_entry_then_stop_loss_from_the_engine():
    rig = _rig()
    eng = _engine(rig, TradeManager())
    assert _signal(rig, side="SELL", stop=105.0, target=85.0).accepted
    q, E = _qty(100.0, 105.0), _sell(100.0)        # short 20 @ 99.95
    # the bar's high runs through the 105 stop (open below it: no gap)
    assert eng._check_exit("BTCUSDT", _bar(1, 101.0, 105.5, 100.5, 105.2)) is True
    X = _buy(105.0)                                # a short exits with a buy
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=105.0, target=85.0,
                         legs=[(X, q)], direction="SHORT", exit_reason="STOP_LOSS", result="LOSS",
                         trade_source="AUTO_ENGINE", instance_id=None, trading_mode="FORWARD_PAPER",
                         requested_exits=[105.0])
    assert out["net"] < -abs(E - 105.0) * q        # a full stop plus costs
    assert trade["mae_price"] == 105.5 and trade["mae_amount"] == pytest.approx(-(105.5 - E) * q)
    assert rig.store.modifications(trade["trade_id"]) == []
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S2", trade, expected_net=out["net"])


def test_s3_entry_partial_exit_final_exit_from_engine_scale_out():
    rig = _rig()
    eng = _engine(rig, TradeManager(scale_at_r=1.0, scale_frac=0.5))
    assert _signal(rig).accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)
    level = E + 1.0 * (E - 95.0)                   # +1R on the filled entry
    # bar 1 reaches +1R: half the position is scaled out at the 1R level
    assert eng._check_exit("BTCUSDT", _bar(1, 101.0, 106.0, 100.5, 105.5)) is True
    mid = _only(rig.store)
    assert mid["status"] == "PARTIALLY_CLOSED" and mid["partial_exit_count"] == 1
    # bar 2 reaches the target with the remainder
    assert eng._check_exit("BTCUSDT", _bar(2, 106.0, 115.5, 105.5, 115.2)) is True
    legs = [(_sell(level), q * 0.5), (_sell(115.0), q * 0.5)]
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    assert len(rows) == 2                         # parent + remainder ledger rows
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0, legs=legs,
                         direction="LONG", exit_reason="TAKE_PROFIT", result="WIN",
                         trade_source="AUTO_ENGINE", instance_id=None, trading_mode="FORWARD_PAPER",
                         requested_exits=[level, 115.0])
    size_mod = [m for m in rig.store.modifications(trade["trade_id"]) if m["field"] == "SIZE"]
    assert len(size_mod) == 1 and size_mod[0]["reason"] == "PARTIAL_EXIT"
    assert (size_mod[0]["old_value"], size_mod[0]["new_value"]) == (pytest.approx(q), pytest.approx(q / 2))
    legacy = rig.pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S3", trade, expected_net=out["net"], legs=legs)


def test_s4_break_even_stop_from_the_engine_trade_manager():
    rig = _rig()
    eng = _engine(rig, TradeManager(be_at_r=1.0))
    assert _signal(rig).accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)
    # bar 1 runs +1R (106 - 100.05 >= 5.05): the manager moves the stop to entry
    assert eng._check_exit("BTCUSDT", _bar(1, 101.0, 106.0, 100.5, 105.5)) is False
    mods = rig.store.modifications(_only(rig.store)["trade_id"])
    assert [(m["field"], m["reason"]) for m in mods] == [("STOP_LOSS", "BREAK_EVEN")]
    assert mods[0]["old_value"] == 95.0 and mods[0]["new_value"] == pytest.approx(E, rel=1e-12)
    # bar 2 falls back through entry: stopped at break-even
    assert eng._check_exit("BTCUSDT", _bar(2, 101.0, 101.5, 99.5, 100.0)) is True
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(_sell(E), q)], direction="LONG", exit_reason="BREAK_EVEN_STOP",
                         result="BREAK_EVEN", trade_source="AUTO_ENGINE", instance_id=None,
                         trading_mode="FORWARD_PAPER", requested_exits=[E])
    assert trade["initial_stop"] == 95.0 and trade["current_stop"] == pytest.approx(E, rel=1e-12)
    # the risk stays the ORIGINAL risk: R is not re-based on the moved stop
    assert trade["risk_amount"] == pytest.approx((E - 95.0) * q)
    assert out["net"] < 0 and abs(out["net"]) <= 0.05 * out["risk"]
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S4", trade, expected_net=out["net"], modifications=len(mods))


def test_s5_trailing_stop_from_the_engine_trade_manager():
    rig = _rig()
    eng = _engine(rig, TradeManager(trail_r=1.0))
    assert _signal(rig).accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)
    r = E - 95.0
    assert eng._check_exit("BTCUSDT", _bar(1, 101.0, 108.0, 100.5, 107.5)) is False
    assert eng._check_exit("BTCUSDT", _bar(2, 107.5, 110.0, 107.0, 109.5)) is False
    trail1, trail2 = 108.0 - r, 110.0 - r
    mods = rig.store.modifications(_only(rig.store)["trade_id"])
    assert [(m["field"], m["reason"]) for m in mods] == [("STOP_LOSS", "TRAILING"), ("STOP_LOSS", "TRAILING")]
    assert mods[0]["old_value"] == 95.0
    assert mods[0]["new_value"] == pytest.approx(trail1) and mods[1]["old_value"] == pytest.approx(trail1)
    assert mods[1]["new_value"] == pytest.approx(trail2)
    # bar 3 pulls back through the trailed stop
    assert eng._check_exit("BTCUSDT", _bar(3, 109.0, 109.2, 104.0, 104.5)) is True
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(_sell(trail2), q)], direction="LONG", exit_reason="TRAILING_STOP",
                         result="WIN", trade_source="AUTO_ENGINE", instance_id=None,
                         trading_mode="FORWARD_PAPER", requested_exits=[trail2])
    assert trade["current_stop"] == pytest.approx(trail2)
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S5", trade, expected_net=out["net"], stops=[95.0, trail1, trail2])


def test_s6_manual_close_through_the_paper_close_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    import app as appmod
    import data.market_data as market_data
    import webhook_api as wa
    from config import settings

    rig = _rig()
    assert _signal(rig, alert=f"tv-{uuid.uuid4().hex[:8]}").accepted   # a webhook entry
    q, E = _qty(100.0, 95.0), _buy(100.0)
    monkeypatch.setattr(wa, "paper", rig.paper)
    monkeypatch.setattr(wa, "pipeline", rig.pipe)
    monkeypatch.setattr(wa, "ledger", rig.ledger)
    monkeypatch.setattr(wa, "trade_journal", rig.rec)
    monkeypatch.setattr(wa, "trade_journal_store", rig.store)
    mark = 108.0
    monkeypatch.setattr(market_data, "get_bars", lambda *a, **k: (
        [Bar(datetime.now(timezone.utc), mark, mark, mark, mark, 1.0)], "test-feed"))
    client = TestClient(appmod.app)
    assert client.post("/paper/close", json={"symbol": "BTCUSDT"}).status_code == 401
    resp = client.post("/paper/close", json={"symbol": "BTCUSDT"},
                       headers={"x-webhook-secret": settings.admin_key})
    assert resp.status_code == 200, resp.text
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    X = _sell(mark)
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(X, q)], direction="LONG", exit_reason="MANUAL_CLOSE", result="WIN",
                         trade_source="WEBHOOK", instance_id=None, trading_mode="FORWARD_PAPER",
                         order_link=False, requested_exits=[mark])
    assert trade["exit_reason_source"] == "OPERATOR"
    assert resp.json()["pnl"] == pytest.approx(round(out["net"], 2))
    # the journal stores the executed price, not the requested mark
    assert trade["exit_price"] != mark and trade["exit_price"] == pytest.approx(X)
    _assert_replay_and_reconcile_add_nothing(rig)
    # the endpoint is idempotent at the HTTP level too: nothing left to close
    again = client.post("/paper/close", json={"symbol": "BTCUSDT"},
                        headers={"x-webhook-secret": settings.admin_key})
    assert again.status_code == 404 and _count(rig.store, "journal_trades") == 1
    _evidence("S6", trade, expected_net=out["net"], http_status=resp.status_code)


# ---------------------------------------------------------------- instances
def _pin_instance_fill_model(monkeypatch):
    for name, value in (("HUB_FILL_SPREAD_PCT", SPREAD), ("HUB_FILL_SLIPPAGE_PCT", SLIPPAGE),
                        ("HUB_FILL_LATENCY_PCT", LATENCY), ("HUB_FILL_TAKER_FEE_PCT", FEE),
                        ("HUB_FILL_PARTIAL_PROB", 0.0), ("HUB_FILL_REJECT_PROB", 0.0)):
        monkeypatch.setenv(name, str(value))


def _instance_rig(tmp_path, monkeypatch, symbol="BTCUSDT"):
    from tests.test_instance_failure_modes import _create, _manager
    _pin_instance_fill_model(monkeypatch)
    ledger, _hub, manager = _manager(tmp_path)
    store = TradeJournalStore(str(tmp_path / "journal.db"))
    rec = TradeJournalRecorder(store)
    manager.trade_journal = rec
    instance = _create(manager, symbol)
    manager.start(instance.id)
    engine, paper = manager._runtime[instance.id][0], manager._runtime[instance.id][1]
    assert isinstance(paper, ForwardPaperExecutionEngine)
    spy = _Spy(rec)
    paper.journal = spy
    engine.pipeline.symbol_rules_provider = None   # the shared harness stubs venue rules as a dict
    rig = types.SimpleNamespace(ledger=ledger, led=paper.ledger, paper=paper, pipe=engine.pipeline,
                                store=store, rec=rec, spy=spy, scope=instance.id)
    return rig, manager, instance, engine


def _instance_entry(rig, instance, engine):
    decided = datetime.now(timezone.utc) - timedelta(seconds=2)
    result = engine.pipeline.process({
        "alert_id": f"auto:{instance.id}:BTCUSDT:5m:{decided.isoformat()}:buy", "symbol": "BTCUSDT",
        "side": "BUY", "entry": 100.0, "stop": 95.0, "target": 115.0, "confidence": 1.0,
        "strategy": "Decision Brain", "timeframe": "5m", "timestamp": decided.isoformat(),
        "reason": "fixture decision", "regime": "Trending"})
    assert result.accepted, result.reason
    pending = _only(rig.store)
    assert pending["status"] == "PENDING"
    stamp = datetime.now(timezone.utc).isoformat()
    fills = rig.paper.process_quote({"symbol": "BTCUSDT", "last": 100.0, "bid": 99.9, "ask": 100.1,
                                     "mark": 100.0, "sequence": 1, "received_at": stamp,
                                     "event_timestamp": stamp, "quote_event_id": "q1"})
    assert len(fills) == 1
    trade = _only(rig.store)
    assert trade["trade_id"] == pending["trade_id"] and trade["entry_filled_at"] == stamp
    equity = float(instance.starting_equity)
    q = _qty(100.0, 95.0, equity=equity, risk_pct=instance.risk_per_trade_pct, cap=0.05)
    return trade, q, _buy(100.1), equity


def test_s7_instance_disposal_closes_the_open_trade_in_the_journal(tmp_path, monkeypatch):
    rig, manager, instance, engine = _instance_rig(tmp_path, monkeypatch)
    try:
        opened, q, E, equity = _instance_entry(rig, instance, engine)
        assert opened["account_balance_before"] == equity
        mark = 104.0
        engine.last_prices["BTCUSDT"] = mark
        engine.last_activity = datetime.now(timezone.utc).isoformat()
        idle = threading.Event()
        engine._thread = threading.Thread(target=idle.wait, daemon=True)
        engine._thread.start()
        outcome = manager.close_open_positions(instance.id)
        idle.set()
        assert [c["symbol"] for c in outcome["closed"]] == ["BTCUSDT"] and outcome["remaining"] == []
        trade = _only(rig.store)
        assert trade["trade_id"] == opened["trade_id"]
        rows = rig.ledger.get_paper_trades(instance_id=instance.id)
        out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                             legs=[(_sell(mark), q)], direction="LONG", exit_reason="MANUAL_CLOSE",
                             result="WIN", trade_source="TRADING_INSTANCE", instance_id=instance.id,
                             trading_mode="FORWARD_PAPER", strategy=("brain", "Decision Brain", "v1"),
                             requested_exits=[mark])
        assert trade["exit_reason_source"] == "OPERATOR_DISPOSAL"
        assert outcome["closed"][0]["pnl"] == pytest.approx(out["net"])
        _assert_replay_and_reconcile_add_nothing(rig, mode_resolver=manager.journal_identity)
        _evidence("S7", trade, expected_net=out["net"], instance=instance.id)
    finally:
        manager.shutdown()


def test_s8b_forward_paper_through_an_instance_worker_exit_from_its_engine(tmp_path, monkeypatch):
    rig, manager, instance, engine = _instance_rig(tmp_path, monkeypatch)
    try:
        opened, q, E, _equity = _instance_entry(rig, instance, engine)
        assert opened["exchange"] == "Binance USD-M Futures" and opened["market_type"] == "PERPETUAL_FUTURES"
        # the worker's own exit check sees a bar through the 115 target
        with engine._cycle_lock:
            assert engine._check_exit("BTCUSDT", _bar(1, 101.0, 115.5, 100.5, 115.2)) is True
        trade = _only(rig.store)
        rows = rig.ledger.get_paper_trades(instance_id=instance.id)
        out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                             legs=[(_sell(115.0), q)], direction="LONG", exit_reason="TAKE_PROFIT",
                             result="WIN", trade_source="TRADING_INSTANCE", instance_id=instance.id,
                             trading_mode="FORWARD_PAPER", strategy=("brain", "Decision Brain", "v1"),
                             requested_exits=[115.0])
        _assert_replay_and_reconcile_add_nothing(rig, mode_resolver=manager.journal_identity)
        _evidence("S8b", trade, expected_net=out["net"], instance=instance.id)
    finally:
        manager.shutdown()


# ---------------------------------------------------------------- forward paper
@pytest.mark.parametrize("fill_at,expected_session", [
    ("2026-10-05T02:00:00+00:00", "ASIA"),               # Tokyo 11:00, London 03:00 BST
    ("2026-10-05T08:42:00+00:00", "LONDON"),             # London 09:42 BST, NY 04:42 EDT
    ("2026-10-05T12:30:00+00:00", "LONDON_NY_OVERLAP"),  # 13:30 BST and 08:30 EDT
    ("2026-10-05T16:30:00+00:00", "NEW_YORK"),           # London 17:30 closed, NY 12:30
    ("2026-10-05T20:30:00+00:00", "OFF_HOURS"),          # London 21:30, NY 16:30... see below
    ("2025-03-20T16:30:00+00:00", "LONDON_NY_OVERLAP"),  # US on DST (9 Mar), UK not (30 Mar)
    ("2025-10-27T16:30:00+00:00", "LONDON_NY_OVERLAP"),  # UK back on GMT (26 Oct), US not (2 Nov)
    ("2025-03-31T07:30:00+00:00", "LONDON"),             # first BST Monday: London 08:30
    ("2025-11-03T07:45:00+00:00", "ASIA"),               # London 07:45 GMT closed, Tokyo 16:45
])
def test_s8_forward_paper_intent_quote_fill_then_exit(fill_at, expected_session):
    if expected_session == "OFF_HOURS":
        # 20:30Z on 5 Oct: London 21:30 BST, New York 16:30 EDT -> NY is still open.
        # Use 22:30Z instead (NY 18:30, Tokyo 07:30): no centre open.
        fill_at = "2026-10-05T22:30:00+00:00"
    assert _session(fill_at) == expected_session
    scope = "inst-fwd"
    rig = _rig(forward=True, scope=scope,
               context={**PROVENANCE, "instance_id": scope, "instance_name": "BTCUSDT SMC 5m #INSTFW"})
    filled = _ts(fill_at)
    decided = filled - timedelta(minutes=5)
    result = _signal(rig, ts=decided.isoformat())
    assert result.accepted and result.reason.startswith("paper order intent")
    pending = _only(rig.store)
    assert pending["status"] == "PENDING" and pending["entry_price"] is None
    assert rig.ledger.get_paper_trades() == []
    quote = {"bid": 99.9, "ask": 100.1, "mark": 100.0}
    # a quote AT decision time is not "later": nothing fills
    assert rig.paper.process_quote({**quote, "received_at": decided.isoformat()}) == []
    fills = rig.paper.process_quote({**quote, "received_at": filled.isoformat()})
    assert len(fills) == 1
    q, E = _qty(100.0, 95.0), _buy(100.1)          # a buy references the ask, then pays the fill cost
    opened = _only(rig.store)
    assert opened["trade_id"] == pending["trade_id"] and opened["status"] == "OPEN"
    assert opened["entry_filled_at"] == filled.isoformat()
    assert opened["requested_entry_price"] == 100.0
    assert opened["entry_slippage"] == pytest.approx(E - 100.0, rel=1e-9)
    assert opened["entry_session"] == expected_session
    # a repeat of the same quote fills nothing more
    assert rig.paper.process_quote({**quote, "received_at": (filled + timedelta(seconds=1)).isoformat()}) == []
    assert _close(rig, 113.0, reason="take-profit").accepted
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(_sell(113.0), q)], direction="LONG", exit_reason="TAKE_PROFIT",
                         result="WIN", trade_source="TRADING_INSTANCE", instance_id=scope,
                         trading_mode="FORWARD_PAPER", requested_exits=[113.0])
    assert trade["entry_session"] == expected_session
    legacy = rig.pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S8", trade, expected_session=expected_session, expected_net=out["net"])


# ---------------------------------------------------------------- restart
def _close_connections(*objects) -> None:
    for obj in objects:
        conn = getattr(obj, "_c", None)
        if conn is not None:
            conn.close()


def test_s9_restart_while_open_then_reconcile_then_final_exit(tmp_path):
    paths = {k: str(tmp_path / f"{k}.db") for k in ("ledger", "journal", "legacy")}
    ledger, store, legacy = (SqliteLedger(paths["ledger"]), TradeJournalStore(paths["journal"]),
                             JournalStore(paths["legacy"]))
    rig = _rig(ledger=ledger, store=store, legacy_store=legacy)
    assert _signal(rig).accepted
    before = _only(store)
    assert before["status"] == "OPEN"
    trade_id, trade_ref = before["trade_id"], before["trade_ref"]
    pre_counts = _counts(store)
    # process dies: drop every object and close every connection
    _close_connections(ledger, store, legacy)
    del rig, ledger, store, legacy
    gc.collect()
    # boot: rebuild everything from the same files
    ledger, store, legacy = (SqliteLedger(paths["ledger"]), TradeJournalStore(paths["journal"]),
                             JournalStore(paths["legacy"]))
    rig = _rig(ledger=ledger, store=store, legacy_store=legacy)
    assert _counts(store) == pre_counts
    summary = rig.rec.reconcile_ledger(ledger, grace_s=0)
    assert summary == {"created": 0, "linked_remainders": 0, "closed": 0, "uncertain": 0,
                       "skipped_recent": 0}
    assert rig.rec.migrate_legacy(legacy, ledger) == {"migrated": 0, "already": 1}
    assert _count(store, "journal_trades") == 1
    assert _close(rig, 112.0, reason="take-profit").accepted
    trade = _only(store)
    assert (trade["trade_id"], trade["trade_ref"]) == (trade_id, trade_ref)   # the ORIGINAL TRD
    q, E = _qty(100.0, 95.0), _buy(100.0)
    rows = ledger.get_paper_trades()
    out = _verify_closed(store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(_sell(112.0), q)], direction="LONG", exit_reason="TAKE_PROFIT",
                         result="WIN", trade_source="AUTO_ENGINE", instance_id=None,
                         trading_mode="FORWARD_PAPER", requested_exits=[112.0],
                         extra_links={"LEGACY_JOURNAL": 1})
    assert store.snapshot(trade_id)["decision"] == "ENTER_LONG"          # decision survived the restart
    assert trade["data_completeness"] == "LIVE_CAPTURE"
    assert legacy.get(rows[0]["id"])["status"] == "closed"
    # migrate_legacy linked the legacy row to the live trade (a link, never a second trade)
    assert {l["link_type"] for l in store.links(trade_id)} >= {"LEGACY_JOURNAL"}
    rig.store.links  # noqa: B018 - keep the store referenced
    before_replay = _counts(store)
    for method, event in list(rig.spy.calls):
        getattr(rig.rec, method)(copy.deepcopy(event))
    assert rig.rec.reconcile_ledger(ledger, grace_s=0)["closed"] == 0
    assert rig.rec.migrate_legacy(legacy, ledger)["migrated"] == 0
    assert _counts(store) == before_replay
    _evidence("S9", trade, expected_net=out["net"], reconcile=summary)


def test_s9b_restart_with_a_pending_forward_intent_fills_into_the_original_trade(tmp_path):
    paths = {k: str(tmp_path / f"{k}.db") for k in ("ledger", "journal", "legacy")}
    scope = "inst-restart"
    context = {**PROVENANCE, "instance_id": scope}
    holder: dict = {}
    ledger, store, legacy = (SqliteLedger(paths["ledger"]), TradeJournalStore(paths["journal"]),
                             JournalStore(paths["legacy"]))
    rig = _rig(ledger=ledger, store=store, legacy_store=legacy, forward=True, scope=scope, context=context,
               intents_listener=lambda intents: holder.__setitem__("intents", intents))
    decided = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert _signal(rig, ts=decided.isoformat()).accepted
    pending = _only(store)
    assert pending["status"] == "PENDING" and "BTCUSDT" in holder["intents"]
    _close_connections(ledger, store, legacy)
    del rig, ledger, store, legacy
    gc.collect()
    ledger, store, legacy = (SqliteLedger(paths["ledger"]), TradeJournalStore(paths["journal"]),
                             JournalStore(paths["legacy"]))
    rig = _rig(ledger=ledger, store=store, legacy_store=legacy, forward=True, scope=scope, context=context,
               initial_intents=holder["intents"])
    assert rig.rec.reconcile_ledger(ledger, grace_s=0)["created"] == 0
    filled = datetime.now(timezone.utc)
    assert len(rig.paper.process_quote({"bid": 99.9, "ask": 100.1, "mark": 100.0,
                                        "received_at": filled.isoformat()})) == 1
    opened = _only(store)
    assert opened["trade_id"] == pending["trade_id"] and opened["status"] == "OPEN"
    assert opened["data_completeness"] == "LIVE_CAPTURE"
    assert store.snapshot(opened["trade_id"])["decision"] == "ENTER_LONG"
    assert _close(rig, 96.0, reason="stop").accepted
    trade = _only(store)
    q, E = _qty(100.0, 95.0), _buy(100.1)
    rows = ledger.get_paper_trades()
    out = _verify_closed(store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                         legs=[(_sell(96.0), q)], direction="LONG", exit_reason="STOP_LOSS",
                         result="LOSS", trade_source="TRADING_INSTANCE", instance_id=scope,
                         trading_mode="FORWARD_PAPER", requested_exits=[96.0])
    # the legacy decision journal was back-filled from the frozen snapshot and closed
    assert legacy.get(rows[0]["id"])["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("S9b", trade, expected_net=out["net"])


# ---------------------------------------------------------------- operational outcomes
def test_s10_operational_outcomes_are_never_losses():
    store = TradeJournalStore(":memory:")
    # (a) EXECUTION_FAILED: the engine raises at submission
    failed = _rig(store=store, scope="inst-fail")

    def boom(**_kw):
        raise RuntimeError("venue unavailable")
    failed.paper.open = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="venue unavailable"):
        _signal(failed)
    # (b) REJECTED: every gate passes, the execution model refuses the order
    rejected = _rig(store=store, scope="inst-reject", fill_model=_fill_model(reject_prob=1.0))
    assert not _signal(rejected).accepted
    # (c) EXECUTION_UNCERTAIN: a pending forward order that never fills or cancels
    uncertain = _rig(store=store, scope="inst-uncertain", forward=True)
    assert _signal(uncertain, ts=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()).accepted
    assert uncertain.rec.reconcile_ledger(uncertain.ledger, pending_uncertain_after_s=-1)["uncertain"] == 1
    # (d) CANCELLED: a second forward decision while the first intent is still pending
    dup = _rig(store=store, scope="inst-dup", forward=True)
    t1 = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    t2 = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    assert _signal(dup, ts=t1).accepted
    assert _signal(dup, ts=t2).accepted            # the pipeline parks it; the engine keeps the first
    # the first intent fills and closes as a normal trade (WIN)
    assert len(dup.paper.process_quote({"bid": 99.9, "ask": 100.1, "mark": 100.0,
                                        "received_at": datetime.now(timezone.utc).isoformat()})) == 1
    assert _close(dup, 113.0, reason="take-profit").accepted
    # (e) CANCELLED by a simulation reset: an open position ends without a fabricated fill
    reset = _rig(store=store, scope="inst-reset")
    assert _signal(reset).accepted
    assert reset.rec.cancel_open_for_instance("inst-reset", reason="account restart") == 1
    # (f) a real loss, for contrast
    loser = _rig(store=store, scope="inst-loss")
    assert _signal(loser).accepted
    assert _close(loser, 96.0, reason="stop").accepted

    by_scope = {t["instance_id"]: [] for t in store.list_trades()}
    for t in store.list_trades():
        by_scope[t["instance_id"]].append(t)
    assert _count(store, "journal_trades") == 7
    expect = {
        "inst-fail": ("FAILED", "EXECUTION_FAILED"),
        "inst-reject": ("REJECTED", "REJECTED"),
        "inst-uncertain": ("UNCERTAIN", "EXECUTION_UNCERTAIN"),
        "inst-reset": ("CANCELLED", "CANCELLED"),
    }
    for scope, (status, result) in expect.items():
        (t,) = by_scope[scope]
        assert (t["status"], t["result"]) == (status, result), scope
        assert t["is_operational"] is True and t["counts_in_stats"] is False, scope
        assert t["net_pnl"] is None and t["exit_price"] is None, scope
    assert "venue unavailable" in by_scope["inst-fail"][0]["result_reason"]
    assert by_scope["inst-reset"][0]["exit_reason"] == "SIMULATION_RESET"
    # no ledger trade exists for the orders that never executed
    assert failed.ledger.get_paper_trades() == [] and rejected.ledger.get_paper_trades() == []
    assert uncertain.ledger.get_paper_trades() == []
    # duplicate intent: one CANCELLED decision, one executed trade linked to the only ledger row
    dup_trades = sorted(by_scope["inst-dup"], key=lambda t: t["order_created_at"])
    assert len(dup_trades) == 2
    first, second = dup_trades
    assert (second["status"], second["result"]) == ("CANCELLED", "CANCELLED")
    assert second["is_operational"] is True and second["counts_in_stats"] is False
    assert "still pending" in second["result_reason"]
    assert _count(store, "journal_executions", second["trade_id"]) == 0
    assert _count(store, "journal_fees", second["trade_id"]) == 0
    assert _ledger_ids(store, second["trade_id"]) == set()
    dup_rows = dup.ledger.get_paper_trades()
    assert len(dup_rows) == 1 and _ledger_ids(store, first["trade_id"]) == {dup_rows[0]["id"]}
    _verify_closed(store, dup_rows, first, qty=_qty(100.0, 95.0), entry=_buy(100.1), stop=95.0,
                   target=115.0, legs=[(_sell(113.0), _qty(100.0, 95.0))], direction="LONG",
                   exit_reason="TAKE_PROFIT", result="WIN", trade_source="TRADING_INSTANCE",
                   instance_id="inst-dup", trading_mode="FORWARD_PAPER")
    (loss,) = by_scope["inst-loss"]
    loss_out = _verify_closed(store, loser.ledger.get_paper_trades(), loss, qty=_qty(100.0, 95.0),
                              entry=_buy(100.0), stop=95.0, target=115.0,
                              legs=[(_sell(96.0), _qty(100.0, 95.0))], direction="LONG",
                              exit_reason="STOP_LOSS", result="LOSS", trade_source="TRADING_INSTANCE",
                              instance_id="inst-loss", trading_mode="FORWARD_PAPER")
    # analytics: operational events are counted, never as losses or trades
    m = journal_analytics.metrics(store.list_trades())
    assert (m["total_trades"], m["wins"], m["losses"], m["break_even"]) == (2, 1, 1, 0)
    assert m["operational_events"] == 5
    assert m["operational_breakdown"] == {"EXECUTION_FAILED": 1, "REJECTED": 1,
                                          "EXECUTION_UNCERTAIN": 1, "CANCELLED": 2}
    assert m["net_pnl"] == pytest.approx(first["net_pnl"] + loss_out["net"], abs=1e-6)
    assert m["win_rate"] == pytest.approx(50.0)
    assert len(store.list_trades(only_stats=True)) == 2
    print(f"EVIDENCE S10 metrics total={m['total_trades']} wins={m['wins']} losses={m['losses']} "
          f"operational={m['operational_events']} breakdown={m['operational_breakdown']} "
          f"net={m['net_pnl']}")


# ============================================================ SECTION 2
def test_p_two_successive_partial_exits_then_final_close():
    rig = _rig()
    assert _signal(rig).accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)
    trade_id = _only(rig.store)["trade_id"]
    first = rig.paper.reduce(symbol="BTCUSDT", exit_price=104.0, fraction=0.25)
    second = rig.paper.reduce(symbol="BTCUSDT", exit_price=108.0, fraction=0.5)
    assert (first.action, second.action) == ("reduced", "reduced")
    mid = _only(rig.store)
    assert mid["trade_id"] == trade_id
    assert mid["status"] == "PARTIALLY_CLOSED" and mid["partial_exit_count"] == 2
    assert mid["closed_quantity"] == pytest.approx(q * 0.25 + q * 0.75 * 0.5)
    assert _close(rig, 112.0, reason="take-profit").accepted
    legs = [(_sell(104.0), q * 0.25), (_sell(108.0), q * 0.75 * 0.5), (_sell(112.0), q * 0.75 * 0.5)]
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    assert len(rows) == 3
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0, legs=legs,
                         direction="LONG", exit_reason="TAKE_PROFIT", result="WIN",
                         trade_source="AUTO_ENGINE", instance_id=None, trading_mode="FORWARD_PAPER",
                         requested_exits=[104.0, 108.0, 112.0])
    assert trade["status"] == "CLOSED" and trade["partial_exit_count"] == 2
    size_mods = [(m["old_value"], m["new_value"]) for m in rig.store.modifications(trade_id)
                 if m["field"] == "SIZE"]
    assert size_mods == [(pytest.approx(q), pytest.approx(q * 0.75)),
                         (pytest.approx(q * 0.75), pytest.approx(q * 0.375))]
    legacy = rig.pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    assert legacy[0]["trade_id"] == min(rows, key=lambda r: r["opened_at"])["id"]
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("P-two-partials", trade, expected_net=out["net"], ledger_rows=len(rows))


@pytest.mark.parametrize("side,stop,target,partial_px,final_px,expected", [
    ("BUY", 95.0, 115.0, 110.0, 97.0, "PARTIAL_WIN"),     # +half at 110, rest stopped at 97
    ("SELL", 105.0, 85.0, 98.0, 104.5, "PARTIAL_LOSS"),   # short: +half at 98, rest out at 104.5
])
def test_p_partial_win_then_final_loss_is_a_partial_result(side, stop, target, partial_px, final_px,
                                                           expected):
    rig = _rig()
    assert _signal(rig, side=side, stop=stop, target=target).accepted
    long = side == "BUY"
    q = _qty(100.0, stop)
    E = _buy(100.0) if long else _sell(100.0)
    exit_fill = _sell if long else _buy
    assert rig.paper.reduce(symbol="BTCUSDT", exit_price=partial_px, fraction=0.5).action == "reduced"
    assert _close(rig, final_px, reason="stop").accepted
    legs = [(exit_fill(partial_px), q * 0.5), (exit_fill(final_px), q * 0.5)]
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    out = _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=stop, target=target, legs=legs,
                         direction="LONG" if long else "SHORT", exit_reason="STOP_LOSS", result=expected,
                         trade_source="AUTO_ENGINE", instance_id=None, trading_mode="FORWARD_PAPER",
                         requested_exits=[partial_px, final_px])
    assert out["legs"][0] > 0 > out["legs"][1]
    assert (out["net"] > 0) == (expected == "PARTIAL_WIN")
    legacy = rig.pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence(f"P-{expected}", trade, expected_net=out["net"], legs=out["legs"])


def test_p_restart_between_partial_and_final_exit_keeps_one_trade(tmp_path):
    paths = {k: str(tmp_path / f"{k}.db") for k in ("ledger", "journal", "legacy")}
    ledger, store, legacy = (SqliteLedger(paths["ledger"]), TradeJournalStore(paths["journal"]),
                             JournalStore(paths["legacy"]))
    rig = _rig(ledger=ledger, store=store, legacy_store=legacy)
    assert _signal(rig).accepted
    trade_id = _only(store)["trade_id"]
    assert rig.paper.reduce(symbol="BTCUSDT", exit_price=106.0, fraction=0.5).action == "reduced"
    _close_connections(ledger, store, legacy)
    del rig, ledger, store, legacy
    gc.collect()
    ledger, store, legacy = (SqliteLedger(paths["ledger"]), TradeJournalStore(paths["journal"]),
                             JournalStore(paths["legacy"]))
    rig = _rig(ledger=ledger, store=store, legacy_store=legacy)
    assert rig.rec.reconcile_ledger(ledger, grace_s=0) == {
        "created": 0, "linked_remainders": 0, "closed": 0, "uncertain": 0, "skipped_recent": 0}
    assert _close(rig, 99.0, reason="stop").accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)
    trade = _only(store)
    assert trade["trade_id"] == trade_id
    rows = ledger.get_paper_trades()
    _verify_closed(store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                   legs=[(_sell(106.0), q / 2), (_sell(99.0), q / 2)], direction="LONG",
                   exit_reason="STOP_LOSS", result="PARTIAL_WIN", trade_source="AUTO_ENGINE",
                   instance_id=None, trading_mode="FORWARD_PAPER", requested_exits=[106.0, 99.0])
    original = min(rows, key=lambda r: r["opened_at"])["id"]
    assert legacy.get(original)["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("P-restart", trade)


def test_p_missed_partial_hook_is_repaired_by_reconciliation_into_the_same_trade():
    rig = _rig()
    assert _signal(rig).accepted
    trade_id = _only(rig.store)["trade_id"]
    rig.paper.journal = None                       # the journal hook is lost for the scale-out
    assert rig.paper.reduce(symbol="BTCUSDT", exit_price=107.0, fraction=0.5).action == "reduced"
    rig.paper.journal = rig.spy
    summary = rig.rec.reconcile_ledger(rig.ledger, grace_s=0)
    assert summary["linked_remainders"] == 1 and summary["created"] == 0
    mid = _only(rig.store)
    assert mid["trade_id"] == trade_id and mid["status"] == "PARTIALLY_CLOSED"
    assert _close(rig, 111.0, reason="take-profit").accepted
    q, E = _qty(100.0, 95.0), _buy(100.0)
    trade = _only(rig.store)
    rows = rig.ledger.get_paper_trades()
    # the ledger row keeps the fee amount but not the commission rate, so the
    # repaired leg records the rate as unknown rather than assuming one; and a
    # paper_trades row carries no position id, so only the entry position is
    # cross-referenced (the remainder is linked by its ledger trade id)
    _verify_closed(rig.store, rows, trade, qty=q, entry=E, stop=95.0, target=115.0,
                   legs=[(_sell(107.0), q / 2), (_sell(111.0), q / 2)], direction="LONG",
                   exit_reason="TAKE_PROFIT", result="WIN", trade_source="AUTO_ENGINE",
                   instance_id=None, trading_mode="FORWARD_PAPER", fee_rates=(FEE, None),
                   extra_links={"LEDGER_POSITION": 1})
    repaired = [e for e in rig.store.executions(trade_id) if e["kind"] == "PARTIAL_EXIT"]
    assert len(repaired) == 1
    by_ref = {}
    for f in rig.store.fees(trade_id):
        by_ref.setdefault(f["source_ref"], []).append(f["rate"])
    assert by_ref[repaired[0]["execution_id"]] == [None, None]
    assert all(rates == [FEE, FEE] for ref, rates in by_ref.items() if ref != repaired[0]["execution_id"])
    legacy = rig.pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    _assert_replay_and_reconcile_add_nothing(rig)
    _evidence("P-missed-hook", trade)
