"""The read-only rulebook lab API.

Two properties matter here and neither is about JSON shape. The endpoints must
never acquire a write path -- they exist alongside a lab that owns real
sessions and a paper account -- and the manifest's purity claim must be checked
against the bytes it describes rather than asserted.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.pa_rulebook_v01 import FLIP_RETEST_ID, RULEBOOK_VERSION, SR_REJECTION_ID

ENGINE = Path(__file__).resolve().parents[1] / "services" / "pa_rulebook_v01.py"


@pytest.fixture(scope="module")
def client():
    """The router on a bare app.

    The production app puts every research route behind sign-in, so driving it
    here would test the auth middleware rather than these endpoints. That the
    router is actually mounted in production is asserted separately below,
    because mounting it on a bare app proves nothing about that.
    """
    from fastapi import FastAPI

    from routers.pa_rulebook import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_router_is_mounted_on_the_production_api():
    """A router nobody included is a file, not an endpoint."""
    import routers.pa_rulebook
    import webhook_api

    # This FastAPI version keeps includes lazily as wrappers around the
    # original router, so the mounted paths are not flattened here; older
    # versions flatten them. Accept either rather than pinning the internals.
    mounted = [getattr(route, "original_router", None)
               for route in webhook_api.router.routes]
    paths = [getattr(route, "path", "") for route in webhook_api.router.routes]
    assert (any(candidate is routers.pa_rulebook.router for candidate in mounted)
            or any(path.startswith("/research/pa-rulebook") for path in paths))


def test_the_manifest_attests_the_engine_bytes_it_describes(client):
    """A purity claim with no hash beside it is a comment, not evidence."""
    body = client.get("/research/pa-rulebook/manifest").json()
    assert body["engine_sha256"] == hashlib.sha256(ENGINE.read_bytes()).hexdigest()
    assert body["version"] == RULEBOOK_VERSION
    assert body["status"] == "RESEARCH_ONLY"
    assert body["pure_engine"] is True and body["reads_clock"] is False
    assert body["real_order_path"] is False and body["live_execution_allowed"] is False
    assert set(body["strategies"]) == {SR_REJECTION_ID, FLIP_RETEST_ID}
    assert "not a proven edge" in body["status_note"]


def test_the_config_endpoint_persists_every_default_explicitly(client):
    """Chapter 18: an omitted default is how a software upgrade changes
    behaviour invisibly."""
    body = client.get("/research/pa-rulebook/config?symbol=ETHUSDT").json()
    config = body["config"]
    assert config["symbol"] == "ETHUSDT"
    assert config["min_net_rr"] == 2.5
    assert config["risk_per_trade"] == 0.0025
    assert config["confirmation_bars"] == 3 and config["retest_window_bars"] == 4
    assert config["atr_period"] == 14 and config["warmup_bars"] == 200
    assert body["real_execution_allowed"] is False


def test_the_state_endpoint_says_why_rather_than_failing_silently(client, monkeypatch):
    """With no cached candles it must report that, not an empty analysis.

    An endpoint that answers "no zones, no setup" when the real cause is an
    empty candle store is the kind of green screen that costs an afternoon.
    The loader is stubbed rather than pointed at an unknown symbol, because
    opening the store for one creates an empty database file as a side effect.
    """
    import routers.pa_rulebook as module

    monkeypatch.setattr(module, "_bars", lambda symbol, timeframe, limit: [])
    response = client.get("/research/pa-rulebook/state?symbol=BTCUSDT")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "NO_CANDLES"


def test_the_state_endpoint_reports_warm_up_separately_from_no_data(client, monkeypatch):
    """Short history and no history are different answers and must stay so."""
    import routers.pa_rulebook as module
    from tests.test_pa_rulebook_engine import _context_bars

    short = _context_bars()[:20]
    monkeypatch.setattr(module, "_bars", lambda symbol, timeframe, limit: list(short))
    body = client.get("/research/pa-rulebook/state?symbol=BTCUSDT").json()
    assert body["state"] == "WARMING_UP"
    assert body["available_context_bars"] < body["required_context_bars"]


def test_the_router_has_no_write_path():
    """It sits beside a lab that owns sessions, a journal and a paper account.

    The guard is structural rather than behavioural: every route is a GET, and
    the module imports nothing that could open a broker, a session or a
    writable store. A later 'just one POST to start a run' has to change this
    test, which is the point.
    """
    import ast
    import inspect

    import routers.pa_rulebook as module

    tree = ast.parse(inspect.getsource(module))
    methods = set()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            imported.update(name.split(".")[0] for name in names)
        for decorator in getattr(node, "decorator_list", []):
            func = getattr(decorator, "func", None)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id == "router":
                methods.add(func.attr)

    assert methods == {"get"}, f"a non-GET route appeared: {sorted(methods - {'get'})}"
    for forbidden in ("sqlite3", "execution", "broker", "paper_trading"):
        assert forbidden not in imported, f"the read-only router imported {forbidden}"
