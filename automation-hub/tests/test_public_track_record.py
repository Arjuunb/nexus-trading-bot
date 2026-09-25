"""Public paper track record (services/public_track_record.py).

Pins the promises the switch makes: nothing is public until the owner turns
it on for one instance; what goes out is percentages of the paper account
and never an amount; research replays cannot be published; the public route
needs no sign-in while the switch needs the owner and the control secret.
"""
import json

import pytest

from data.ledger import SqliteLedger
from services.public_track_record import MIN_SAMPLE, PublicTrackRecord, _thin, public_id
from services.tenancy import OWNER_TENANT
from services.trading_instances import InstanceLedger, TradingInstanceManager

MONEY_KEYS = {"realized_pnl", "balance", "starting_balance", "capital_allocation", "pnl",
              "gross_win", "gross_loss", "avg_win", "avg_loss", "best", "worst", "size",
              "equity", "fees", "expectancy", "max_drawdown_abs", "starting_equity"}


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


def _manager(record=None):
    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    manager.track_record = record
    return manager


def _create(manager, *, owner_id=OWNER_TENANT, mode="trading", capital=1_000):
    return manager.create(symbol="BTCUSDT", strategy_key="three_candle_rejection",
                          strategy_label="3-Candle Rejection · EMA 9/33", strategy_version="1.0.0",
                          timeframe="15m", risk_per_trade_pct=0.005, capital_allocation=capital,
                          owner_id=owner_id, mode=mode)


def _trade(manager, inst, pnl, rr):
    scoped = InstanceLedger(manager.ledger, inst.id, inst.simulation_session_id)
    trade_id = scoped.record_paper_trade({"symbol": "BTCUSDT", "side": "long", "size": 0.1,
                                          "entry": 100, "stop": 95, "target": 110})
    scoped.close_paper_trade(trade_id, exit_price=100 + pnl * 10, pnl=pnl, rr=rr, fees=0.2)


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


# ─────────────────────────── the record ───────────────────────────
def test_nothing_is_public_until_the_owner_publishes_one_instance():
    record = PublicTrackRecord(None)
    manager = _manager(record)
    first, second = _create(manager), _create(manager)
    _trade(manager, first, 20, 2.0)
    assert record.build(manager)["instances"] == []

    record.set(first.id, True, by="owner")
    record._cache = None
    view = record.build(manager)
    assert [row["id"] for row in view["instances"]] == [public_id(first.id)]
    assert first.id not in json.dumps(view) and second.id not in json.dumps(view)
    assert "no real money" in view["note"] and "owner chooses" in view["note"]


def test_percentages_only_and_they_match_the_ledger():
    record = PublicTrackRecord(None)
    manager = _manager(record)
    inst = _create(manager, capital=2_000)
    for pnl, rr in ((40, 2.0), (-20, -1.0), (60, 2.0), (-20, -1.0)):
        _trade(manager, inst, pnl, rr)
    record.set(inst.id, True)
    row = record.entry(manager, inst.id)

    assert row["closed_trades"] == 4 and (row["wins"], row["losses"]) == (2, 2)
    assert row["win_rate_pct"] == 50.0
    assert row["return_pct"] == pytest.approx(60 / 2_000 * 100)
    assert row["profit_factor"] == pytest.approx(100 / 40)
    assert row["equity_index"][0]["index"] == 100.0
    assert row["equity_index"][-1]["index"] == pytest.approx(103.0)
    assert row["max_drawdown_pct"] > 0
    assert row["execution"] == "paper" and row["risk_per_trade_pct"] == 0.5
    assert row["sample_note"] and str(MIN_SAMPLE) in row["sample_note"]
    leaked = MONEY_KEYS & set(_keys(row))
    assert not leaked, leaked


def test_no_losing_trade_yet_is_no_ratio_rather_than_ninety_nine():
    record = PublicTrackRecord(None)
    manager = _manager(record)
    inst = _create(manager)
    _trade(manager, inst, 10, 1.0)
    assert record.entry(manager, inst.id)["profit_factor"] is None


def test_research_replays_are_never_published(tmp_path):
    record = PublicTrackRecord(str(tmp_path / "public_track_record.json"))
    manager = _manager(record)
    research = _create(manager, mode="research")
    record.set(research.id, True)
    assert record.entry(manager, research.id) is None
    assert record.build(manager)["instances"] == []


def test_the_switch_survives_a_restart_and_is_cleared_on_delete(tmp_path):
    path = str(tmp_path / "public_track_record.json")
    record = PublicTrackRecord(path)
    manager = _manager(record)
    inst = _create(manager)
    row = record.set(inst.id, True, by="owner")
    assert row["since"] and PublicTrackRecord(path).published(inst.id)
    # Switching it on again keeps the original publication date.
    assert record.set(inst.id, True)["since"] == row["since"]
    manager.delete(inst.id)
    assert PublicTrackRecord(path).published(inst.id) is False


def test_a_long_curve_is_thinned_but_keeps_its_ends_and_its_low():
    points = [{"t": str(i), "index": 100 + (i % 17) - (50 if i == 777 else 0)} for i in range(2_000)]
    thin = _thin(points)
    assert len(thin) <= 201
    assert thin[0] is points[0] and thin[-1] is points[-1]
    assert min(p["index"] for p in thin) == min(p["index"] for p in points)


# ─────────────────────────── the API ───────────────────────────
def _patch_api(monkeypatch, manager):
    from routers import instances as instance_api
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _s: None)
    monkeypatch.setattr(instance_api, "_owner", lambda _request: OWNER_TENANT)
    monkeypatch.setattr(instance_api, "_initiated_by", lambda _request: "tester")
    return instance_api


def test_the_owner_switches_it_and_another_owner_cannot(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    record = PublicTrackRecord(None)
    manager = _manager(record)
    mine, theirs = _create(manager), _create(manager, owner_id="someone-else")
    api = _patch_api(monkeypatch, manager)

    assert api.instance_public_record(mine.id)["published"] is False
    state = api.update_instance_public_record(mine.id, api.PublicRecordUpdate(published=True))
    assert state["published"] and state["preview"]["id"] == public_id(mine.id)
    assert any("Paper record published" in row["message"] for row in manager.store.engine_logs(mine.id))

    body = api.PublicRecordUpdate(published=True)
    for call in (lambda: api.instance_public_record(theirs.id),
                 lambda: api.update_instance_public_record(theirs.id, body)):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 404
    assert record.published(theirs.id) is False


def test_the_switch_requires_the_control_secret(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    record = PublicTrackRecord(None)
    manager = _manager(record)
    inst = _create(manager)
    api = _patch_api(monkeypatch, manager)

    def refuse(_secret):
        raise HTTPException(401, "bad secret")
    monkeypatch.setattr(api._wa, "_check_secret", refuse)
    with pytest.raises(HTTPException) as refused:
        api.update_instance_public_record(inst.id, api.PublicRecordUpdate(published=True))
    assert refused.value.status_code == 401 and record.published(inst.id) is False


def test_a_research_replay_cannot_be_published_through_the_api(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    record = PublicTrackRecord(None)
    manager = _manager(record)
    research = _create(manager, mode="research")
    api = _patch_api(monkeypatch, manager)
    with pytest.raises(HTTPException) as refused:
        api.update_instance_public_record(research.id, api.PublicRecordUpdate(published=True))
    assert refused.value.status_code == 409 and record.published(research.id) is False


def test_the_public_route_needs_no_sign_in_and_shows_only_published(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    import webhook_api

    record = PublicTrackRecord(None)
    manager = _manager(record)
    shown, hidden = _create(manager), _create(manager)
    _trade(manager, shown, 15, 1.5)
    _trade(manager, hidden, 30, 3.0)
    record.set(shown.id, True)
    monkeypatch.setattr(webhook_api, "instance_manager", manager)

    response = TestClient(hub_app.app).get("/public/track-record")
    assert response.status_code == 200
    assert "max-age=60" in response.headers["cache-control"]
    body = response.json()
    assert [row["id"] for row in body["instances"]] == [public_id(shown.id)]
    assert not MONEY_KEYS & set(_keys(body))
    # Everything else on the instance API still needs a session.
    assert TestClient(hub_app.app).get(f"/instances/{shown.id}/public-record").status_code == 401
