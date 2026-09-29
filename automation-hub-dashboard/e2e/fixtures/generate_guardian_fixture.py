"""Build e2e/fixtures/guardian.json, the mocked /guardian/* responses.

Not written by hand: the real GuardianService, store and bus run over a
fixed scene -- one instance whose candles went stale and then recovered, the
SMC lab running on a synchronised feed, the Price Action lab idle, the ledger
healthy, and a journal recorder that skipped a Supabase ledger.

Strategy telemetry (Phase 2) is real strategy output: the 3-Candle Rejection
strategy through AutoStrategyEngine (a filtered rejection and a taken trade),
the frozen SMC strategy through the SMC agent, and the frozen Price Action
engine through the PA lab with its pin-bar experiment on. Regenerate after
changing the Guardian API:

    cd automation-hub && python ../automation-hub-dashboard/e2e/fixtures/generate_guardian_fixture.py
"""
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "automation-hub"))
from services.guardian.bus import EventBus  # noqa: E402
from services.guardian.service import GuardianService  # noqa: E402
from services.guardian.store import GuardianStore  # noqa: E402
from services.guardian.strategy import StrategyTelemetry  # noqa: E402

store = GuardianStore()
bus = EventBus(store)
bus.start()
instance = {"id": "a3f9c2d1e8b74c0f", "symbol": "BTCUSDT", "timeframe": "5m",
            "strategy_id": "three_candle_rejection", "strategy_label": "3-Candle Rejection · EMA 9/33",
            "mode": "trading", "live_feed": True, "state": "running", "paused": False, "alive": True,
            "lifecycle_state": "data_stale", "market_data_status": "stale"}
labs = {
    "smc": {"id": "smc", "label": "SMC Lab", "thread_alive": True, "session_active": True,
            "stream": {"state": "SYNCHRONIZED", "reliable": True, "failing_dependency": None,
                       "symbol": "BTCUSDT", "timeframe": "5m"}},
    "pa": {"id": "pa", "label": "Price Action Lab", "thread_alive": True, "session_active": False,
           "stream": None},
}
journal = {"running": True, "passes": 12, "last_error": None, "last_pass_at": "2026-09-28T22:00:00+00:00",
           "skipped": ["MAIN: ledger is not a local SQLite ledger"]}

def strategy_scene(tmp: Path) -> list:
    """Real strategies, real engine, real labs. Returns the lab readers."""
    from dataclasses import replace

    from bot.types import Bar
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services import guardian as g
    from services.auto_engine import AutoStrategyEngine
    from services.controls import TradingControl
    from services.guardian.strategy import PAEvaluationReader, SMCDecisionReader
    from services.native_price_action import NativePriceActionEngine, PriceActionConfig
    from services.native_smc import PriceAction
    from services.signal_pipeline import SignalPipeline
    from services.smc_agent import SMCAgent
    from services.smc_agent_journal import SMCAgentJournal
    from services.smc_strategy_v1 import evaluate
    from services.strategy_factory import make_builtin_strategy
    from services.trading_instances import InstanceLedger
    from tests.test_journal_pa_decisions import _account, _candles
    from tests.test_smc_strategy_ladder import seeded_engine
    from tests.test_three_candle_rejection import _history, _long_pattern

    g.install(bus)
    for k, (trend, open_) in enumerate((("down", 101.6), ("up", 110.0))):
        ledger = SqliteLedger(str(tmp / f"ledger{k}.db"))
        scoped = InstanceLedger(ledger, instance["id"], "sess-1")
        paper = PaperExecutionEngine(scoped, 10_000)
        pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
        engine = AutoStrategyEngine(
            pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=False,
            strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
            fetcher=lambda *a, **k: ([], "replay"), entry_mode="market", instance_id=instance["id"])
        engine.strategy_key, engine.strategy_version = "three_candle_rejection", "v1"
        engine.quality_gate_bypass = lambda: True
        engine.guardian_trace = True
        rows, i = _history(trend=trend)
        series = rows + _long_pattern(i, open_=open_)
        t0 = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5 * len(series))
        bars = [Bar(t0 + timedelta(minutes=5 * n), r.open, r.high, r.low, r.close, r.volume)
                for n, r in enumerate(series)]
        strategy = engine.strategy_factory("BTCUSDT")
        strategy.bars.extend(bars[:-3])
        for bar in bars[-3:]:
            engine._process_bar("BTCUSDT", bar, strategy)
    g.uninstall()

    journal = SMCAgentJournal(str(tmp / "agent.db"))
    agent = SMCAgent(journal, equity=10_000.0)
    one_short = seeded_engine()
    now = list(one_short.snapshots)[-1]
    one_short.snapshots[now] = replace(one_short.snapshots[now],
                                       price_action=PriceAction(False, False, .5, .2, 2.0))
    one_short.latest_snapshot = one_short.snapshots[now]
    agent.observe(evaluate(one_short), candle_time="2026-09-28T21:55:00+00:00")
    agent.observe(evaluate(seeded_engine()), candle_time="2026-09-28T22:00:00+00:00")

    account = _account(tmp)
    pa = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m",
                                                   trigger_filter="pin_bar_only"))
    candles = _candles(200)
    pa.ingest_closed_bars(candles[:150])
    for bar in candles[150:]:
        pa.process_closed_bar(bar, market_data_health="SYNCHRONIZED")
        account.record_evaluation(pa.visual_state(candle_window=3000), bar, {"state": "SYNCHRONIZED"})
    return [SMCDecisionReader(str(tmp / "agent.db")), PAEvaluationReader(account.path)]


tmpdir = Path(tempfile.mkdtemp())
readers = strategy_scene(tmpdir)
second = {"id": "7e1d4b09c2a35f86", "symbol": "ETHUSDT", "timeframe": "15m",
          "strategy_id": "three_candle_rejection", "strategy_label": "3-Candle Rejection · EMA 9/33",
          "mode": "trading", "live_feed": True, "state": "running", "paused": False, "alive": True,
          "lifecycle_state": "running", "market_data_status": "healthy"}
svc = GuardianService(store, bus, instances={"main": lambda: [instance, second]},
                      labs={k: (lambda v=v: v) for k, v in labs.items()},
                      database=lambda: {"components": {"database": {"state": "operational",
                                                                    "detail": "Recording decisions and fills"}},
                                        "last_sample_at": time.time(), "interval_s": 60},
                      journal=lambda: journal, telemetry=StrategyTelemetry(store, readers),
                      interval_s=15)
# Phase 3 scene. A Binance outage stalls both instances and the SMC lab at
# once: one incident, recovered and verified. Then one instance's own feed
# goes stale, recovers, and fails again before its recovery is verified.
healthy = dict(lifecycle_state="running", market_data_status="healthy")
stale = dict(lifecycle_state="data_stale", market_data_status="stale")
instance.update(healthy)
second.update(healthy)
svc.cycle()                                   # all healthy
instance.update(stale)
second.update(stale)
labs["smc"]["stream"]["state"] = "DISCONNECTED"
svc.cycle()
bus.flush()
svc.cycle()                                   # one incident, five components
instance.update(healthy)
second.update(healthy)
labs["smc"]["stream"]["state"] = "SYNCHRONIZED"
svc.cycle()                                   # recovered
svc.incidents.verify_s = 0
svc.cycle()                                   # verified and closed
svc.incidents.verify_s = 3600
instance.update(stale)
svc.cycle()                                   # the instance's feed is stale: instance BLOCKED
instance.update(healthy)
svc.cycle()                                   # the feed caught up
instance.update(stale)
svc.cycle()                                   # and fell behind again: the same incident reopens
bus.flush()
store.set_meta("heartbeat", {"at": time.time(), "cycles": svc.cycles, "interval_s": 15, "last_cycle_ms": 2.1})
svc._thread = type("Alive", (), {"is_alive": lambda self: True})()   # snapshot reads it as running
status = svc.snapshot()
strategies = svc.strategies(days=7)
bus.stop()
almost = store.almost_trades(limit=50)
strategy_events = store.events(category="strategy", limit=20)
wanted = {a["first_event_id"] for a in almost} | {e["event_id"] for e in strategy_events}
out = {"status": status,
       # the activity stream as the Activity tab asks for it: platform events
       "events": {"events": [e for e in store.events(limit=1000) if e["category"] != "strategy"][:200],
                  "next_before": None},
       "actions": {"actions": store.actions()},
       "strategies": strategies,
       "almost_trades": {"almost_trades": almost},
       "strategy_events": {"events": strategy_events, "next_before": None},
       "traces": {event_id: store.event(event_id) for event_id in sorted(wanted)},
       "incidents": {"incidents": svc.incidents.list(), "counts": svc.incidents.counts()},
       "incident_details": {str(i["id"]): svc.incidents.get(i["id"]) for i in svc.incidents.list()},
       "anomalies": {"active": svc.anomalies.active(),
                     "recent": store.events(event_type="anomaly_detected", limit=50)}}
shutil.rmtree(tmpdir, ignore_errors=True)
(HERE / "guardian.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=str) + "\n")
print("wrote", HERE / "guardian.json", "state:", status["summary"]["state"],
      "events:", len(out["events"]["events"]), "strategies:", len(strategies["strategies"]),
      "almost-trades:", len(almost), "incidents:", svc.incidents.counts())
