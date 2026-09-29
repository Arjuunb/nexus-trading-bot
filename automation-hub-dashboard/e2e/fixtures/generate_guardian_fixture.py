"""Build e2e/fixtures/guardian.json, the mocked /guardian/* responses.

Not written by hand: the real GuardianService, store and bus run over a
fixed scene -- one instance whose candles went stale and then recovered, the
SMC lab running on a synchronised feed, the Price Action lab idle, the ledger
healthy, and a journal recorder that skipped a Supabase ledger.

Strategy telemetry (Phase 2) is real strategy output: the 3-Candle Rejection
strategy through AutoStrategyEngine (a filtered rejection and a taken trade),
the frozen SMC strategy through the SMC agent, and the frozen Price Action
engine through the PA lab with its pin-bar experiment on.

Phases 5-8: research runs the real ResearchEngine over journal records seeded
through the journal's own store (as the research tests do) on a simulated
clock -- one idea that passes every stage, one that fails out of sample, one
waiting for forward trades. Reports are the real Reporter over a simulated
18 hours of observation yesterday; notifications go to a channel that is not
configured. The reasoning answer comes from the real ask() with a stub model
reply (no request is sent). Regenerate after changing the Guardian API:

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
from services.guardian.integrity import IntegrityMonitor, Source  # noqa: E402
from services.guardian.recovery import RecoveryController  # noqa: E402
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
    ledgers = []
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
        ledgers.append(ledger)
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
    return [SMCDecisionReader(str(tmp / "agent.db")), PAEvaluationReader(account.path)], ledgers


def integrity_scene(tmp: Path, ledgers) -> IntegrityMonitor:
    """The taken trade stays open (paper risk to its stop), an SMC-lab paper
    position, and the real journal recorder over the instance ledger."""
    from data.trade_record_store import TradeRecordStore
    from services.journal_recorder import JournalRecorder, LedgerSource
    from tests.test_guardian_integrity import _broker_position
    recorder = JournalRecorder(TradeRecordStore(str(tmp / "records.db")))
    recorder.add_ledger(LedgerSource("MAIN", ledgers[1]))
    recorder.reconcile()
    _broker_position(tmp / "smc_lab.db", "SMC_LAB")
    return IntegrityMonitor(store, [Source("MAIN", ledgers[1].path, kind="ledger"),
                                    Source("SMC_LAB", str(tmp / "smc_lab.db"), kind="lab_broker")],
                            journal_path=str(tmp / "records.db"), live_status=lambda: {"locked": True},
                            every=1)


def broken_integrity(tmp: Path, ledgers) -> dict:
    """The same monitor over a copy of the instance ledger whose position row
    was deleted: what a fill without its position looks like."""
    import sqlite3
    copy = tmp / "ledger_broken.db"
    shutil.copy(ledgers[1].path, copy)
    conn = sqlite3.connect(copy)
    conn.execute("DELETE FROM positions")
    conn.commit()
    conn.close()
    return IntegrityMonitor(GuardianStore(), [Source("MAIN", str(copy), kind="ledger")],
                            journal_path=str(tmp / "records.db"), live_status=lambda: {"locked": True},
                            every=1).run(now=time.time())


def research_scene(tmp: Path) -> dict:
    """Three ideas: one passes every stage, one fails out of sample, one waits
    for forward trades. Returns the simulated clock."""
    from data.trade_record_store import TradeRecordStore
    from tests.test_guardian_research import _WINNING, _seed
    journal = TradeRecordStore(str(tmp / "research_records.db"))
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=30)
    _seed(journal, start=base, n=150)                                   # Asia loses throughout
    _seed(journal, start=base, n=90, strategy="ema_pullback", key="ema")
    _seed(journal, start=base + timedelta(hours=90), n=60, strategy="ema_pullback", key="ema2", asia=_WINNING)
    _seed(journal, start=base, n=150, strategy="pin_bar", key="pin", version="v2")
    clock = {"t": (base + timedelta(days=10)).timestamp(), "base": base, "journal": journal,
             "path": str(tmp / "research_records.db")}
    return clock


def _never_restart(instance_id):
    raise RuntimeError("the fixture never restarts anything")


tmpdir = Path(tempfile.mkdtemp())
readers, scene_ledgers = strategy_scene(tmpdir)
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
                      integrity=integrity_scene(tmpdir, scene_ledgers),
                      recovery=RecoveryController(store, restart_instance=_never_restart),
                      notify=lambda text: None,          # no Telegram channel configured
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
# Phase 5: research on a simulated clock through Guardian's own research step.
from services.guardian.research import ResearchEngine  # noqa: E402
rclock = research_scene(tmpdir)
svc.research = svc.reports.research = ResearchEngine(store, journal_path=rclock["path"], clock=lambda: rclock["t"])
svc._research_step(None, now=rclock["t"], publish=svc._publish)
from tests.test_guardian_research import _seed  # noqa: E402
_seed(rclock["journal"], start=rclock["base"] + timedelta(days=12), n=60, key="fwd")   # forward trades
rclock["t"] = (rclock["base"] + timedelta(days=16)).timestamp()
svc._research_step(None, now=rclock["t"], publish=svc._publish)
# Phase 8: Guardian observed 18 hours of yesterday; the daily report is issued.
yesterday = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
for minute in range(18 * 60 + 1):
    svc.reports.observed((yesterday + timedelta(minutes=minute)).timestamp())
svc._reports_step(None, now=time.time(), publish=svc._publish)
bus.flush()
# Phase 6: the real ask() over the real evidence pack, with a stub model reply.
import os  # noqa: E402
from types import SimpleNamespace  # noqa: E402
import services.guardian_reasoning as reasoning  # noqa: E402
first_incident = f"incident:{svc.incidents.list()[-1]['id']}"
reply = {"answer": "Both instances and the SMC lab lost their candles at the same moment while nothing else "
                   "changed, which points at the shared upstream (Binance USD-M) rather than any one worker. "
                   "It recovered and was verified; nothing in the evidence suggests a strategy problem.",
         "confidence": "HIGH CONFIDENCE", "citations": [first_incident, "incident:9999"],
         "limitations": ["One consumer's link and Binance itself cannot be told apart from one feed alone."]}
stub = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
    stop_reason="end_turn", model=kw["model"], stop_details=None,
    content=[SimpleNamespace(type="text", text=json.dumps(reply))]))))
os.environ["HUB_LLM_API_KEY"] = "fixture-only-not-a-key"
answer = reasoning.ask(svc, "Why did every feed stop at once this morning?", research=svc.research,
                       client_factory=lambda: stub)
reasoning_on = reasoning.status(svc)
del os.environ["HUB_LLM_API_KEY"]
reasoning_off = reasoning.status(svc)
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
       "integrity": svc.integrity.last,
       "integrity_with_findings": broken_integrity(tmpdir, scene_ledgers),
       "anomalies": {"active": svc.anomalies.active(),
                     "recent": store.events(event_type="anomaly_detected", limit=50)},
       "research": svc.research.view(),
       "research_analyst": {"strategies": svc.research.analyst(), "error": None},
       "recovery": svc.recovery_view(),
       "reports": svc.reports.view(),
       "reasoning": reasoning_off,
       "reasoning_on": reasoning_on,
       "reasoning_answer": answer}
shutil.rmtree(tmpdir, ignore_errors=True)
(HERE / "guardian.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=str) + "\n")
print("wrote", HERE / "guardian.json", "state:", status["summary"]["state"],
      "events:", len(out["events"]["events"]), "strategies:", len(strategies["strategies"]),
      "almost-trades:", len(almost), "incidents:", svc.incidents.counts(),
      "hypotheses:", [(h["strategy_id"], h["status"]) for h in out["research"]["hypotheses"]],
      "reports:", [r["kind"] for r in out["reports"]["reports"]], "answer:", answer["outcome"], answer["confidence"])
