"""Build e2e/fixtures/guardian.json, the mocked /guardian/* responses.

Not written by hand: the real GuardianService, store and bus run over a
fixed scene -- one instance whose candles went stale and then recovered, the
SMC lab running on a synchronised feed, the Price Action lab idle, the ledger
healthy, and a journal recorder that skipped a Supabase ledger. Regenerate
after changing the Guardian API:

    cd automation-hub && python ../automation-hub-dashboard/e2e/fixtures/generate_guardian_fixture.py
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "automation-hub"))
from services.guardian.bus import EventBus  # noqa: E402
from services.guardian.service import GuardianService  # noqa: E402
from services.guardian.store import GuardianStore  # noqa: E402

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
svc = GuardianService(store, bus, instances={"main": lambda: [instance]},
                      labs={k: (lambda v=v: v) for k, v in labs.items()},
                      database=lambda: {"components": {"database": {"state": "operational",
                                                                    "detail": "Recording decisions and fills"}},
                                        "last_sample_at": time.time(), "interval_s": 60},
                      journal=lambda: journal, interval_s=15)
svc.cycle()                                   # the instance's feed is stale: instance BLOCKED
instance.update(lifecycle_state="running", market_data_status="healthy")
svc.cycle()                                   # the feed caught up
instance.update(lifecycle_state="data_stale", market_data_status="stale")
svc.cycle()                                   # and fell behind again
bus.flush()
store.set_meta("heartbeat", {"at": time.time(), "cycles": svc.cycles, "interval_s": 15, "last_cycle_ms": 2.1})
svc._thread = type("Alive", (), {"is_alive": lambda self: True})()   # snapshot reads it as running
status = svc.snapshot()
bus.stop()
out = {"status": status, "events": {"events": store.events(limit=200), "next_before": None},
       "actions": {"actions": store.actions()}}
(HERE / "guardian.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=str) + "\n")
print("wrote", HERE / "guardian.json", "state:", status["summary"]["state"],
      "events:", len(out["events"]["events"]))
