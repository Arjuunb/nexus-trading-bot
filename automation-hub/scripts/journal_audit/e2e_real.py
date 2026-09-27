"""AUDIT harness: a real strategy signal, end to end, on deterministic bars.

Nothing here builds a payload by hand. The 3-Candle Rejection strategy reads
the candles, AutoStrategyEngine._process_bar turns its signal into the
decision + pipeline payload exactly as a Trading Instance worker does, the
forward-paper engine parks the intent, a later quote fills it, and the
engine's own stop/target check on the following candles closes it. Only then
does the journal recorder run.

Run through run_all.sh, which creates the scratch HUB_DATA_DIR.
Writes <HUB_DATA_DIR>/e2e_<mode>_<instance>.json with every identifier it saw.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

HUB = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, HUB)
sys.path.insert(0, os.path.join(HUB, "tests"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import require_scratch  # noqa: E402

require_scratch()
from config import settings  # noqa: E402
from bot.types import Bar  # noqa: E402
from data.cycle_store import CycleStore  # noqa: E402
from data.decision_store import DecisionStore  # noqa: E402
from data.journal_store import JournalStore  # noqa: E402
from data.ledger import SqliteLedger  # noqa: E402
from data.trade_record_store import TradeRecordStore  # noqa: E402
from execution.paper_engine import ForwardPaperExecutionEngine  # noqa: E402
from services.auto_engine import AutoStrategyEngine  # noqa: E402
from services.controls import TradingControl  # noqa: E402
from services.decision_journal import DecisionJournal  # noqa: E402
from services.journal_recorder import JournalRecorder, LedgerSource  # noqa: E402
from services.signal_pipeline import SignalPipeline  # noqa: E402
from services.strategy_factory import make_builtin_strategy  # noqa: E402
from services.trading_instances import InstanceLedger  # noqa: E402
from test_three_candle_rejection import _history, _long_pattern, _mirror  # noqa: E402

INST = os.environ.get("AUDIT_INST", "inst-audit-3cr")
SESS = os.environ.get("AUDIT_SESS", "sess-audit-1")
SYM = "BTCUSDT"
TF = timedelta(minutes=5)
MODE = sys.argv[1] if len(sys.argv) > 1 else "long_tp"
BYPASS = os.environ.get("AUDIT_QUALITY_BYPASS") == "1"

ledger = SqliteLedger(settings.ledger_path)
decisions = DecisionStore(settings.decisions_db)
cycles = CycleStore(settings.cycles_db)
journal = DecisionJournal(JournalStore(settings.journal_db))


def build(instance_id=INST, session=SESS, equity=10_000.0, intents=None):
    scoped = InstanceLedger(ledger, instance_id, session)
    saved: dict = {}
    paper = ForwardPaperExecutionEngine(scoped, equity, initial_intents=intents,
                                        intents_listener=saved.update)
    paper.strategy_id = "three_candle_rejection:1.0.0"
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=equity,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05,
                          equity_provider=paper.current_realized_equity)
    pipe.journal = journal
    pipe.journal_context = {
        "instance_id": instance_id, "simulation_session_id": session,
        "instance_name": f"{SYM} 3-Candle Rejection 5m #{instance_id[:6].upper()}",
        "strategy_id": "three_candle_rejection", "strategy_name": "3-Candle Rejection · EMA 9/33",
        "strategy_version": "1.0.0", "market_data_mode": "forward_paper",
        "market_data_source": "Binance USD-M public WebSocket", "fill_model": "next_quote",
        "execution_mode": "paper", "exchange": "binance_usdm", "instrument_type": "perpetual"}
    engine = AutoStrategyEngine(
        pipe, paper, scoped, symbols=[SYM], timeframe="5m", live=True,
        strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
        fetcher=lambda *a, **k: ([], "live (audit)"), entry_mode="market", instance_id=instance_id)
    engine.decisions, engine.reports = decisions, cycles
    engine.strategy_label = "3-Candle Rejection · EMA 9/33 1.0.0"
    engine.strategy_key, engine.strategy_version = "three_candle_rejection", "1.0.0"
    engine.last_source = "live (audit)"
    if BYPASS:
        engine.quality_gate_bypass = lambda: True
    return scoped, paper, pipe, engine, saved


def shifted(rows, newest):
    return [Bar(newest - TF * (len(rows) - 1 - k), r.open, r.high, r.low, r.close, r.volume)
            for k, r in enumerate(rows)]


def main() -> None:
    rows, i = _history()
    pattern = _long_pattern(i)
    if MODE == "short_sl":
        rows, pattern = _mirror(rows), _mirror(pattern)
    # the confirmation candle has just closed; the quote and exits follow it
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    c3_time = now - TF
    series = shifted(rows + pattern, c3_time)
    history, live = series[:-3], series[-3:]
    scoped, paper, pipe, engine, saved = build()
    strategy = engine.strategy_factory(SYM)
    strategy.bars.extend(history)                 # warm-up the way _load_batch does

    out: dict = {"mode": MODE, "bypass": BYPASS, "bars": []}
    for bar in live:
        blocker = engine._process_bar(SYM, bar, strategy)
        out["bars"].append({"t": bar.timestamp.isoformat(), "close": bar.close, "blocker": blocker})
    signal_bar = live[-1]
    intents = paper.pending_intents()
    out["intent"] = intents.get(SYM)
    out["checkpointed_intent"] = saved.get(SYM)
    if not intents:
        out["result"] = "no intent parked"
        out["decisions"] = decisions.recent(20) if hasattr(decisions, "recent") else None
        json.dump(out, open(os.path.join(os.environ["HUB_DATA_DIR"], f"e2e_{MODE}_{INST}.json"), "w"),
                  indent=2, default=str)
        print(json.dumps(out, indent=2, default=str))
        return

    intent = intents[SYM]
    entry, stop, target = intent["entry"], float(intent["stop"]), float(intent["target"])
    long_ = intent["side"] == "BUY"
    # 1) a quote AFTER the decision fills the parked intent (spread 0.02)
    q_at = datetime.now(timezone.utc)            # the first quote after the order
    fill_px = entry + (0.01 if long_ else -0.01)
    fills = paper.process_quote({"bid": fill_px - 0.01, "ask": fill_px + 0.01, "mark": fill_px,
                                 "received_at": q_at.isoformat(), "candle_id": f"BINANCE_USDM:{SYM}:5m:audit",
                                 "market_data_source": "Binance USD-M public WebSocket"})
    out["fills"] = [f.__dict__ for f in fills]
    # 2) the following candles: the engine's own stop/target check closes it
    t = signal_bar.timestamp + TF
    exit_bars = []
    if MODE == "long_tp":            # drift up, then trade through the 2R target
        path = [(entry, entry + 0.4, entry - 0.2, entry + 0.3),
                (entry + 0.3, target + 0.2, entry + 0.1, target)]
    else:                            # short: rally through the stop
        path = [(entry, entry + 0.2, entry - 0.3, entry + 0.1),
                (entry + 0.1, stop + 0.3, entry, stop + 0.1)]
    for o, h, lo, c in path:
        t = t + TF
        exit_bars.append(Bar(t, o, h, lo, c, 1.0))
    for bar in exit_bars:
        blocker = engine._process_bar(SYM, bar, strategy)
        out["bars"].append({"t": bar.timestamp.isoformat(), "close": bar.close, "blocker": blocker})

    # 3) the recorder, as webhook_api wires it
    store = TradeRecordStore(settings.trade_records_db)
    rec = JournalRecorder(store)
    rec.add_ledger(LedgerSource("MAIN", ledger, decision_store=decisions))
    out["reconcile"] = rec.reconcile()
    out["trades_ledger"] = ledger.get_paper_trades(instance_id=INST)
    json.dump(out, open(os.path.join(os.environ["HUB_DATA_DIR"], f"e2e_{MODE}_{INST}.json"), "w"),
              indent=2, default=str)
    print(json.dumps({k: out[k] for k in ("mode", "bypass", "bars", "intent", "fills")}, indent=2,
                     default=str))


if __name__ == "__main__":
    main()
