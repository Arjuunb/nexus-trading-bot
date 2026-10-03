"""Paper ledger risk is scoped by instance and never inferred as live exposure."""
from __future__ import annotations

import pytest

from tradexa.guardian.instance_ledger_integrity import reconcile_instance_paper_ledger


def _position(owner="instance-1", position_id="position-1", **changes):
    return {"id": position_id, "instance_id": owner, "simulation_session_id": "session-1",
            "status": "open", "symbol": "BTCUSDT", "side": "long", "size": 2,
            "entry": 100, "stop": 95, **changes}


def _trade(owner="instance-1", trade_id="trade-1", **changes):
    return {"id": trade_id, "instance_id": owner, "simulation_session_id": "session-1",
            "status": "open", "source": "paper", "symbol": "BTCUSDT", "side": "long",
            "size": 2, "entry": 100, **changes}


def _link(owner="instance-1", position_id="position-1", trade_id="trade-1"):
    return {"execution_id": "execution-1", "action": "OPEN", "instance_id": owner,
            "position_id": position_id, "trade_id": trade_id}


def test_matched_pair_reports_paper_risk_without_claiming_a_broker_fill():
    result = reconcile_instance_paper_ledger(
        [_position()], [_trade()], [_link()], atomic_snapshot=True)
    assert result["scope"] == "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY"
    assert result["instances"] == [{"instance_id": "instance-1", "open_positions": 1,
                                    "open_trades": 1, "risk_amount": 10.0,
                                    "risk_complete": True}]
    assert result["findings"][0]["pairing_state"] == "OBSERVED_MATCH"
    assert result["broker_fill_verified"] is False
    assert result["live_exposure_verified"] is False
    assert result["source_coverage_verified"] is False


def test_nonatomic_remote_pair_never_claims_complete_risk():
    result = reconcile_instance_paper_ledger(
        [_position()], [_trade()], [_link()], atomic_snapshot=False)
    assert result["instances"][0]["risk_amount"] is None
    assert result["instances"][0]["risk_complete"] is False
    assert result["findings"][0]["pairing_state"] == "UNVERIFIED"


def test_source_row_order_does_not_change_material_projection():
    positions = [_position(position_id="p2"), _position(position_id="p1")]
    trades = [_trade(trade_id="t2"), _trade(trade_id="t1")]
    links = [_link(position_id="p2", trade_id="t2"),
             {**_link(position_id="p1", trade_id="t1"),
              "execution_id": "execution-2"}]
    forward = reconcile_instance_paper_ledger(
        positions, trades, links, atomic_snapshot=True)
    reverse = reconcile_instance_paper_ledger(
        positions[::-1], trades[::-1], links[::-1], atomic_snapshot=True)
    assert forward == reverse


def test_two_positions_cannot_claim_the_same_open_trade_as_matched():
    links = [_link(position_id="p1"),
             {**_link(position_id="p2"), "execution_id": "execution-2"}]
    result = reconcile_instance_paper_ledger(
        [_position(position_id="p1"), _position(position_id="p2")],
        [_trade()], links, atomic_snapshot=True)
    assert all(row["pairing_state"] == "UNVERIFIED" for row in result["findings"])
    assert "MULTIPLE_POSITION_LINKS" in result["findings"][0]["codes"]
    assert result["instances"][0]["risk_amount"] is None


def test_missing_trade_and_stop_stay_unverified_with_unknown_risk():
    result = reconcile_instance_paper_ledger(
        [_position(stop=None)], [], [_link()], atomic_snapshot=False)
    assert result["findings"][0]["codes"] == [
        "MISSING_STOP", "OPEN_POSITION_TRADE_UNVERIFIED"]
    assert result["instances"][0]["risk_amount"] is None
    assert result["instances"][0]["risk_complete"] is False
    assert result["atomic_snapshot"] is False


def test_unlinked_legacy_pair_is_not_called_a_confirmed_mismatch():
    result = reconcile_instance_paper_ledger(
        [_position()], [_trade()], [], atomic_snapshot=True)
    assert result["findings"][0]["codes"] == ["EXECUTION_LINK_UNVERIFIED"]
    assert result["findings"][1]["codes"] == ["OPEN_TRADE_POSITION_UNVERIFIED"]
    assert all(row["pairing_state"] == "UNVERIFIED" for row in result["findings"])


def test_instances_and_paper_mode_cannot_be_silently_mixed():
    result = reconcile_instance_paper_ledger(
        [_position()], [_trade(owner="instance-2", source="live")], [_link()],
        atomic_snapshot=False)
    codes = [code for row in result["findings"] for code in row["codes"]]
    assert "TRADE_OWNER_MISMATCH" in codes
    assert "PAPER_SOURCE_UNVERIFIED" in codes
    assert {row["instance_id"] for row in result["instances"]} == {
        "instance-1", "instance-2"}
    assert all(row["risk_amount"] is None for row in result["instances"])


def test_bad_geometry_and_mismatched_size_prevent_complete_risk_claim():
    result = reconcile_instance_paper_ledger(
        [_position(stop=105)], [_trade(size=3)], [_link()], atomic_snapshot=True)
    assert set(result["findings"][0]["codes"]) == {
        "SIZE_MISMATCH", "STOP_GEOMETRY_INVALID"}
    assert result["instances"][0]["risk_amount"] is None


@pytest.mark.parametrize("positions,trades,links", [
    ([_position(owner="")], [], []),
    ([_position(status="closed")], [], []),
    ([_position()] * 65, [], []),
    ([_position()], [_trade()], [{**_link(), "action": "CLOSE"}]),
    ([_position(), _position()], [], []),
])
def test_incomplete_or_malformed_source_rows_fail_closed(positions, trades, links):
    with pytest.raises(ValueError):
        reconcile_instance_paper_ledger(positions, trades, links, atomic_snapshot=True)
