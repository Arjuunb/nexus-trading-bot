"""The main paper engine sits out the first 15 minutes after a release.

It used to stop blocking the moment a release's timestamp passed, which is
when the violent part of the move starts. It now uses the same window as
Trading Instances and labs that opt in, and the Safety Center reports the
window the engine actually enforces.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.controls import TradingControl
from services.instance_event_guard import AFTER_MIN
from services.signal_pipeline import SignalPipeline


def _at(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def test_the_server_gives_the_main_engine_the_after_window(tmp_path):
    """Checked in a fresh interpreter: a few older tests replace
    ``webhook_api.pipeline`` and ``webhook_api.engine`` and never put them
    back, so the in-process objects may not be the ones the server built."""
    pytest.importorskip("fastapi")
    code = ("import webhook_api as w; "
            "print(w.pipeline.econ_after_min, w.engine.pipeline is w.pipeline, "
            "w.pipeline.econ_events == w.econ_calendar.events)")
    env = {**os.environ, "HUB_DATA_DIR": str(tmp_path), "HUB_ECON_FEED": "off",
           "HUB_ADAPTIVE_LAB_AUTOSTART": "0"}
    run = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                         env=env, capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout.split()[-3:] == [str(AFTER_MIN), "True", "True"] and AFTER_MIN == 15


def test_an_entry_five_minutes_after_a_release_is_refused():
    led = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(led)
    pipeline = SignalPipeline(led, paper, TradingControl(), equity=10_000)
    pipeline.econ_events = lambda: [{"name": "CPI m/m", "impact": "high", "time": _at(-5)}]
    pipeline.econ_after_min = AFTER_MIN
    signal = {"alert_id": "after-1", "symbol": "BTCUSDT", "side": "BUY",
              "entry": 100.0, "stop": 95.0, "confidence": 1.0}
    result = pipeline.process(signal)
    assert not result.accepted and result.stage == "event_risk"
    assert "CPI m/m released 5m ago" in result.reason

    pipeline.econ_events = lambda: [{"name": "CPI m/m", "impact": "high", "time": _at(-20)}]
    assert pipeline.process({**signal, "alert_id": "after-2"}).stage != "event_risk"


def test_the_safety_center_reports_the_window_the_engine_enforces(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import webhook_api
    from routers import settings as settings_api
    from services.econ_guard import EconCalendar

    cal = EconCalendar(str(tmp_path / "econ_events.json"))
    cal.set_events([{"name": "Non-Farm Employment Change", "impact": "high", "time": _at(-5)}])
    monkeypatch.setattr(webhook_api, "econ_calendar", cal)
    monkeypatch.setattr(webhook_api, "pipeline", SimpleNamespace(econ_after_min=AFTER_MIN))
    out = settings_api.econ_protection()
    assert out["blackout_after_min"] == 15
    assert out["mode"] == "blackout" and out["halt_new_entries"] is True
    assert out["minutes_to_event"] < 0 and "released" in out["actions"][0]
