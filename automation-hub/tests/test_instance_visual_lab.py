"""The Instance Visual Lab API.

One page for every strategy, and it must be observability only. The properties
worth pinning are not the JSON shape: that the Lab reads the runtime's own
evidence rather than recomputing it, that it cannot write anything, that a
strategy is never described by conditions it does not consume, and that a
blocked instance is never drawn as a clean one.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROUTER = Path(__file__).resolve().parents[1] / "routers" / "instance_visual_lab.py"


def _instance(*, instance_id="inst-1", strategy_key="pa_rulebook", state="running",
              blocker="GATE_REJECTED: NET_RR_TOO_LOW", position=None,
              market_status="SYNCHRONIZED", symbol="BTCUSDT"):
    return {
        "id": instance_id, "name": f"{strategy_key} #1", "symbol": symbol,
        "timeframe": "5m", "state": state, "mode": "trading",
        "execution_mode": "FORWARD_PAPER", "exchange": "Binance USDⓈ-M",
        "strategy_key": strategy_key, "strategy_label": strategy_key,
        "strategy_version": "0.1.0",
        "current_position": position,
        "market": {"market_data_status": market_status,
                   "market_data_mode": "paper_forward",
                   "last_market_data_timestamp": "2026-09-16T03:00:00+00:00",
                   "data_source": "binance_usdm"},
        "engine": {"last_blocker": blocker,
                   "last_blocker_timestamp": "2026-09-16T03:00:05+00:00",
                   "mtf_policy": {"label": "HTF 1H", "evidence": {"primary_htf": "1h"}}},
    }


class _Manager:
    def __init__(self, rows):
        self._rows = {row["id"]: row for row in rows}

    def snapshot(self):
        return list(self._rows.values()), [], []

    def status(self, instance_id, **_kw):
        return self._rows[instance_id]


class _Decisions:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def list(self, *, instance_id=None, limit=50, decision=None):
        rows = [r for r in self._rows
                if instance_id is None or r.get("instance_id") == instance_id]
        if decision:
            rows = [r for r in rows if r.get("decision") == decision]
        return rows[:limit]


@pytest.fixture()
def lab(monkeypatch):
    """The router on a bare app, with the runtime's singletons stubbed."""
    import routers.instance_visual_lab as module

    app = FastAPI()
    app.include_router(module.router)

    def _build(rows, decisions=()):
        manager, store = _Manager(rows), _Decisions(decisions)

        class _Proxy:
            instance_manager = manager
            decision_store = store

        monkeypatch.setattr(module, "_wa", _Proxy())
        return TestClient(app)
    return _build


# ------------------------------------------------------------------- one page

def test_three_different_strategies_use_the_same_page(lab):
    """Acceptance 1: one Lab, many strategies, no per-strategy endpoint."""
    client = lab([_instance(instance_id="a", strategy_key="pa_rulebook"),
                  _instance(instance_id="b", strategy_key="donchian"),
                  _instance(instance_id="c", strategy_key="smc", symbol="ETHUSDT")])
    for instance_id in ("a", "b", "c"):
        body = client.get(f"/research/instance-visual/state?instance_id={instance_id}").json()
        assert body["instance"]["instance_id"] == instance_id
        assert body["strategy"]["strategy_id"]
        assert body["gates"]


def test_switching_instances_changes_the_overlays(lab):
    """Acceptance 2 and 11: the chart draws what this strategy consumes."""
    client = lab([_instance(instance_id="a", strategy_key="pa_rulebook"),
                  _instance(instance_id="b", strategy_key="donchian"),
                  _instance(instance_id="c", strategy_key="smc")])

    rulebook = client.get("/research/instance-visual/state?instance_id=a").json()
    donchian = client.get("/research/instance-visual/state?instance_id=b").json()
    smc = client.get("/research/instance-visual/state?instance_id=c").json()

    assert "support" in rulebook["strategy"]["overlays"]
    assert "donchian_channel" in donchian["strategy"]["overlays"]
    assert "choch" in smc["strategy"]["overlays"]

    # The negative is the point: a renderer that can draw CHoCH must not draw
    # it for a 30-bar channel breakout.
    assert "choch" not in donchian["strategy"]["overlays"]
    assert "fvg" not in donchian["strategy"]["overlays"]
    assert "ema" not in donchian["strategy"]["overlays"]
    assert "donchian_channel" not in smc["strategy"]["overlays"]


def test_a_strategy_without_an_adapter_is_refused_rather_than_guessed(lab):
    """Better a named gap than a page that invents a strategy's reasoning."""
    client = lab([_instance(instance_id="x", strategy_key="rsi")])
    response = client.get("/research/instance-visual/state?instance_id=x")
    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "NO_VISUAL_ADAPTER"
    # ...but it is still listed, so an operator can see it exists.
    listed = client.get("/research/instance-visual/instances").json()["instances"]
    assert listed[0]["has_visual_adapter"] is False


# -------------------------------------------------------------------- blockers

def test_a_rejected_decision_shows_the_exact_blocker(lab):
    """Acceptance 4. The code, the sentence, and the gate it failed at."""
    client = lab([_instance(blocker="GATE_REJECTED: NET_RR_TOO_LOW")])
    body = client.get("/research/instance-visual/state?instance_id=inst-1").json()

    assert body["blocker"] == "NET_RR_TOO_LOW"
    assert "does not pay after costs" in body["blocker_explanation"]
    assert body["decision_state"] == "SIGNAL_REJECTED"
    failing = [g for g in body["gates"] if g["state"] == "FAIL"]
    assert len(failing) == 1
    assert failing[0]["id"] == "net_rr"
    assert failing[0]["blocker"] == "NET_RR_TOO_LOW"


def test_earlier_gates_pass_and_later_gates_wait(lab):
    """A setup that never reached the reward test has not failed it."""
    client = lab([_instance(blocker="NO_ELIGIBLE_ZONE")])
    gates = {g["id"]: g["state"] for g in
             client.get("/research/instance-visual/state?instance_id=inst-1").json()["gates"]}
    assert gates["feed_synchronized"] == "PASS"
    assert gates["zone"] == "FAIL"
    assert gates["net_rr"] == "WAITING"


@pytest.mark.parametrize("blocker,expected", [
    ("STALE_MARKET_DATA", "DATA_BLOCKED"),
    ("STALE_HTF_CANDLE", "WAITING_FOR_HTF"),
    ("HTF_NOT_READY", "WAITING_FOR_HTF"),
    ("SIGNALS_ONLY", "SIGNALS_ONLY"),
    ("DAILY_LOSS_LIMIT", "RISK_BLOCKED"),
    ("NO_SETUP", "SCANNING"),
])
def test_a_blocked_instance_is_never_shown_as_merely_active(lab, blocker, expected):
    """Acceptance 7, 8, 9 at the presentation layer: stale base data, stale HTF
    and missing HTF each have their own visible state rather than a green
    badge. The engine fails those closed; this proves the Lab shows it."""
    client = lab([_instance(blocker=blocker)])
    body = client.get("/research/instance-visual/state?instance_id=inst-1").json()
    assert body["decision_state"] == expected
    assert body["decision_state"] != "ACTIVE"


def test_a_stopped_instance_reports_stopped(lab):
    client = lab([_instance(state="stopped", blocker=None)])
    body = client.get("/research/instance-visual/state?instance_id=inst-1").json()
    assert body["decision_state"] == "STOPPED"


def test_the_page_says_what_is_needed_next(lab):
    """Requirement 8: show exactly what remains before an order can be made."""
    client = lab([_instance(strategy_key="pa_rulebook", blocker="NO_ELIGIBLE_ZONE")])
    body = client.get("/research/instance-visual/state?instance_id=inst-1").json()
    assert body["required_next"]
    assert body["current_stage"] == "SETUP"


# ------------------------------------------------------------------- timeline

def _decision_row(**kw):
    row = {"id": 1, "ts": "2026-09-16T03:00:00+00:00", "symbol": "BTCUSDT",
           "timeframe": "5m", "strategy": "pa_rulebook", "side": "long",
           "regime": "BULL", "htf_bias": "bullish", "decision": "rejected",
           "reason": "net RR 0.21", "executed": False, "final_state": "GATE_REJECTED",
           "gate_stage": "strategy", "blocker": "GATE_REJECTED: NET_RR_TOO_LOW",
           "instance_id": "inst-1", "decision_identity": "BTCUSDT|2026-09-16T03:00:00",
           "passed_rules": ["zone", "rejection"], "failed_rules": ["net_rr"],
           "components": {"net_rr": 0.21}}
    row.update(kw)
    return row


def test_the_timeline_uses_the_runtime_candle_identity(lab):
    """Acceptance 3: the Lab shows the same candle identity the runtime acted
    on, because it is the same row -- not a recomputed one."""
    client = lab([_instance()], [_decision_row()])
    body = client.get("/research/instance-visual/timeline?instance_id=inst-1").json()
    event = body["events"][0]
    assert event["candle_identity"] == "BTCUSDT|2026-09-16T03:00:00"
    assert event["timestamp"] == "2026-09-16T03:00:00+00:00"
    assert event["blocker"] == "NET_RR_TOO_LOW"
    assert "does not pay after costs" in event["blocker_explanation"]
    assert event["failed_rules"] == ["net_rr"]


def test_an_accepted_decision_traces_through_to_the_fill(lab):
    """Acceptance 5: strategy -> risk -> order intent -> broker -> fill."""
    client = lab([_instance()], [
        _decision_row(id=2, decision="accepted", executed=True, final_state="FILLED",
                      gate_stage="execution", blocker="", failed_rules=[],
                      passed_rules=["zone", "rejection", "net_rr", "risk"])])
    body = client.get("/research/instance-visual/timeline?instance_id=inst-1").json()
    event = body["events"][0]
    assert event["decision"] == "accepted"
    assert event["final_state"] == "FILLED"
    assert event["executed"] is True
    assert event["blocker"] is None
    assert body["focus"]["last_trade"]["id"] == 2


def test_the_focus_controls_find_the_last_accepted_and_rejected(lab):
    """Requirement 11: Current / Last Rejected / Last Accepted / Last Trade."""
    client = lab([_instance()], [
        _decision_row(id=3, decision="rejected"),
        _decision_row(id=2, decision="accepted", executed=True, final_state="FILLED"),
        _decision_row(id=1, decision="rejected")])
    focus = client.get("/research/instance-visual/timeline?instance_id=inst-1").json()["focus"]
    assert focus["last_rejected"]["id"] == 3
    assert focus["last_accepted"]["id"] == 2
    assert focus["last_trade"]["id"] == 2


def test_the_timeline_is_scoped_to_the_instance(lab):
    client = lab([_instance()], [_decision_row(instance_id="other")])
    body = client.get("/research/instance-visual/timeline?instance_id=inst-1").json()
    assert body["events"] == []
    # An empty list must not read as "nothing happened".
    assert "no setup" in body["coverage"] or "not as a row each" in body["coverage"]


def test_an_unknown_instance_is_a_404_not_an_empty_page(lab):
    client = lab([_instance()])
    assert client.get("/research/instance-visual/state?instance_id=nope").status_code == 404


# --------------------------------------------------------------------- safety

def test_every_route_is_a_get_and_nothing_can_write():
    """Requirement 18. The Lab is observability only.

    Structural rather than behavioural: every route decorator is a GET, and the
    module imports nothing that could place an order or mutate an instance. A
    later "just one POST to arm it" has to change this test, which is the point.
    """
    tree = ast.parse(ROUTER.read_text())
    methods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                func = getattr(decorator, "func", decorator)
                if isinstance(func, ast.Attribute):
                    methods.add(func.attr)
    assert methods == {"get"}, f"non-GET routes: {sorted(methods - {'get'})}"

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = {"services.broker", "bot.broker", "services.paper_broker",
                 "services.signal_pipeline", "services.auto_engine"}
    assert not (imported & forbidden), f"execution imports: {imported & forbidden}"

    source = ROUTER.read_text()
    for banned in ("place_order", "submit_order", ".start(", ".stop(",
                   "set_mode", "live_trading"):
        assert banned not in source, f"the Visual Lab must not reference {banned}"


def test_the_router_declares_execution_is_not_allowed(lab):
    """Every payload says so, because a page that can be screenshotted should
    carry its own disclaimer rather than relying on the surrounding chrome."""
    client = lab([_instance()])
    for path in ("/research/instance-visual/instances",
                 "/research/instance-visual/strategies",
                 "/research/instance-visual/state?instance_id=inst-1",
                 "/research/instance-visual/timeline?instance_id=inst-1"):
        assert client.get(path).json()["real_execution_allowed"] is False


def test_the_router_is_mounted_on_the_production_api():
    """A router nobody included is a file, not an endpoint."""
    source = (Path(__file__).resolve().parents[1] / "webhook_api.py").read_text()
    assert "import routers.instance_visual_lab" in source
    assert "router.include_router(routers.instance_visual_lab.router)" in source


# --------------------------------------------------------------- market data

def test_synthetic_candles_cannot_reach_the_lab(lab, monkeypatch):
    """Acceptance 10. The sample series and the generator are for fixtures.

    A chart that quietly falls back to manufactured candles while explaining a
    real refusal is the worst of both: it looks authoritative and measures
    nothing.
    """
    import data.market_data as market_data

    def _refuse(symbol, n=0, timeframe="", require_real=False):
        assert require_real is True, "the Lab must demand real data"
        raise RuntimeError("no verified real candles; refusing sample fallback")

    monkeypatch.setattr(market_data, "get_bars", _refuse)
    client = lab([_instance()])
    response = client.get("/research/instance-visual/candles?instance_id=inst-1")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "NO_REAL_MARKET_DATA"
    assert "will not substitute" in detail["note"]


def test_an_empty_real_series_fails_closed_rather_than_rendering_nothing(lab, monkeypatch):
    """Acceptance 7: stale/absent base candles must fail closed, because an
    empty chart and a healthy quiet market look identical."""
    import data.market_data as market_data

    monkeypatch.setattr(market_data, "get_bars",
                        lambda *a, **k: ([], "local store (real)"))
    client = lab([_instance()])
    response = client.get("/research/instance-visual/candles?instance_id=inst-1")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "NO_REAL_MARKET_DATA"


def test_real_candles_are_returned_with_their_source(lab, monkeypatch):
    """Provenance travels with the data: an operator can see which series the
    chart is drawing without leaving the page."""
    import data.market_data as market_data
    from datetime import datetime, timezone

    from bot.types import Bar

    bars = [Bar(datetime(2026, 9, 16, 3, 0, tzinfo=timezone.utc),
                100.0, 101.0, 99.0, 100.5, 12.0)]
    monkeypatch.setattr(market_data, "get_bars",
                        lambda *a, **k: (bars, "local store (real)"))
    client = lab([_instance()])
    body = client.get("/research/instance-visual/candles?instance_id=inst-1").json()
    assert body["source"] == "local store (real)"
    assert body["candles"][0]["c"] == 100.5
    assert body["symbol"] == "BTCUSDT" and body["timeframe"] == "5m"
