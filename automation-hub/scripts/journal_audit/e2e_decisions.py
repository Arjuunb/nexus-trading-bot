"""AUDIT: material non-trade decisions from the real engine path.

Every case runs the 3-Candle Rejection strategy through
AutoStrategyEngine._process_bar on its own instance id; the operating mode,
the controls or the data are what differ. After a recorder pass the case
reports what the canonical store holds for that instance: decision records,
trade records, and whether anything carries P&L.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import e2e_real as h  # noqa: E402
from data.trade_record_store import TradeRecordStore  # noqa: E402
from services.approvals import ApprovalStore  # noqa: E402
from services.journal_recorder import JournalRecorder, LedgerSource  # noqa: E402
from test_three_candle_rejection import _history, _long_pattern  # noqa: E402

h.BYPASS = True
STORE = TradeRecordStore(h.settings.trade_records_db)
out: dict = {}


def reconcile():
    rec = JournalRecorder(STORE)
    rec.add_ledger(LedgerSource("MAIN", h.ledger, decision_store=h.decisions))
    return rec.reconcile()


def series():
    rows, i = _history()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return h.shifted(rows + _long_pattern(i), now - h.TF)


def run_case(inst, configure):
    _, paper, pipe, engine, _ = h.build(instance_id=inst, session=f"sess-{inst}")
    configure(engine, pipe)
    bars = series()
    strategy = engine.strategy_factory(h.SYM)
    strategy.bars.extend(bars[:-3])
    blockers = [engine._process_bar(h.SYM, b, strategy) for b in bars[-3:]]
    # a later quote: a signals-only / approval / paused case must not fill
    paper.process_quote({"bid": 102.2, "ask": 102.22, "mark": 102.21,
                         "received_at": datetime.now(timezone.utc).isoformat()})
    return blockers, paper


def report(inst, blockers, paper):
    reconcile()
    decs = STORE.query_decisions(where="instance_id=?", params=(inst,), limit=50)
    trades = STORE.query_trades(where="instance_id=?", params=(inst,), limit=50)
    res = {"engine_blockers": blockers,
           "decision_records": [{k: d.get(k) for k in ("decision_record_id", "decision_type", "status",
                                                       "blocker", "reason", "journal_record_id")} for d in decs],
           "trade_records": len(trades), "ledger_trades": len(h.ledger.get_paper_trades(instance_id=inst)),
           "positions": len(paper.positions()) if paper else None,
           "parked_intents": len(paper.pending_intents()) if paper else None}
    out[inst] = res
    print(f"\n[{inst}]\n" + json.dumps(res, indent=2, default=str))


# 1) SIGNALS_ONLY
b, p = run_case("inst-dec-signals", lambda e, _p: (setattr(e, "trading_mode", "signal"),
                                                   setattr(e, "approvals", ApprovalStore())))
report("inst-dec-signals", b, p)
# 2) APPROVAL_REQUIRED (semi-auto)
b, p = run_case("inst-dec-semi", lambda e, _p: (setattr(e, "trading_mode", "semi"),
                                                setattr(e, "approvals", ApprovalStore())))
report("inst-dec-semi", b, p)
# 3) paused by the operator -> blocked at the controls gate
b, p = run_case("inst-dec-paused", lambda _e, pipe: pipe.controls.pause_all())
report("inst-dec-paused", b, p)
# 4) Decision Brain quality block (no bypass)
b, p = run_case("inst-dec-brain", lambda e, _p: setattr(e, "quality_gate_bypass", lambda: False))
report("inst-dec-brain", b, p)

# 5) stale market data: a real Trading Instance whose shared feed serves
#    3-hour-old closed candles, then recovers. The engine refuses the stale
#    candles; the outage reaches the journal through the lifecycle events the
#    instance manager writes (the manager shares the audit ledger).
from services.forward_paper_hub import ForwardPaperMarketDataHub  # noqa: E402
from services.trading_instances import TradingInstanceManager  # noqa: E402
from strategies.brain_strategy import DecisionBrain  # noqa: E402
from test_journal_integrity import _StaleThenFresh, _wait_for  # noqa: E402

_StaleThenFresh.fresh = False
hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_StaleThenFresh)
hub.synchronous_delivery = True
manager = TradingInstanceManager(h.ledger, strategy_factory=lambda _k, s: DecisionBrain(s),
                                 live=True, live_poll_s=1.0)
manager.market_hub = hub
manager.symbol_rules_provider = lambda _s: {"symbol": "X", "tick_size": 0.01, "step_size": 0.001,
                                            "min_qty": 0.001, "min_notional": 5.0}
manager.configure(paper_account_capital=100_000)
stale_inst = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                            strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                            capital_allocation=1_000)


def _events():
    return [row["message"] for row in manager.store.engine_logs(stale_inst.id, limit=500)]


manager.start(stale_inst.id)
_wait_for(lambda: sum("MARKET_DISCONNECTED" in m for m in _events()) >= 2)
_StaleThenFresh.fresh = True
_wait_for(lambda: any("MARKET_CONNECTED" in m for m in _events()))
manager.shutdown()
_StaleThenFresh.fresh = False
report(stale_inst.id, ["lifecycle: MARKET_STALE ... MARKET_CONNECTED"], None)
out["inst-dec-stale"] = out.pop(stale_inst.id)

# 6) NO_SETUP flood: 60 quiet candles
inst = "inst-dec-quiet"
_, paper, pipe, engine, _ = h.build(instance_id=inst, session=f"sess-{inst}")
rows, _i = _history()
bars = h.shifted(rows, datetime.now(timezone.utc).replace(second=0, microsecond=0) - h.TF)
strategy = engine.strategy_factory(h.SYM)
strategy.bars.extend(bars[:-60])
blockers = [engine._process_bar(h.SYM, b, strategy) for b in bars[-60:]]
cycles = h.cycles._c.execute("SELECT COUNT(*) FROM cycle_reports WHERE instance_id=?", (inst,)).fetchone()[0]
report(inst, sorted(set(blockers)), paper)
out[inst]["cycle_reports"] = cycles
out[inst]["blocker_counts"] = {b: blockers.count(b) for b in set(blockers)}
print(f"  cycle reports written: {cycles}; blocker counts: {out[inst]['blocker_counts']}")

json.dump(out, open(os.path.join(os.environ["HUB_DATA_DIR"], "e2e_decisions.json"), "w"), indent=2, default=str)
