"""A journal revision must record something that happened.

On the production Price Action lab, 59,560 of 72,443 revisions carried the
reason code MATERIAL_EVIDENCE_CHANGED while recording no lifecycle event at
all. Each was a ~9.5 KB copy of the previous one, appended roughly once a
second, growing the database about 860 MB a day until journal writes started
timing out and the lab correctly refused to place orders it could not durably
record.

The cause was the dedupe hash, not the journal: it covered two bar counters
that move on their own. These tests pin the difference between a change that
justifies a revision and one that does not.
"""
from __future__ import annotations

import copy

import pytest

from services.price_action_governance import PriceActionJournalStore


def _record() -> dict:
    """A minimally shaped journal record; only the hashed fields matter here."""
    return {
        "identity": {"journal_entry_id": "j-1", "session_id": "s-1", "symbol": "BTCUSDT"},
        "market_context": {"regime": "BULL", "data_health_reason": "candles fresh"},
        "setup": {"id": "setup-1", "direction": "long", "invalidation_price": 100.0},
        "order_risk": {"order_id": "o-1", "actual_simulated_fill": 101.0,
                       "spread": 0.0999999, "bid_ask_decision": {"bid": 78478.8, "ask": 78478.9}},
        "outcome": {"status": "OPEN", "result": None, "bars_in_trade": 1254,
                    "bars_to_entry": 12, "maximum_adverse_excursion": -0.4},
        "chart_state": {"candles": 500},
        "review": {"researcher_notes": "", "tags": []},
    }


def _hash(record: dict) -> str:
    return PriceActionJournalStore._material_hash(record)


def test_a_drifting_bar_counter_does_not_justify_a_revision():
    """The measured cause, in isolation.

    bars_in_trade is ``index - filled_index`` over a rolling window, so it
    moves as the window slides -- it was observed decreasing, 1254 to 1253,
    between two consecutive revisions of an untouched trade.
    """
    before = _record()
    after = copy.deepcopy(before)
    after["outcome"]["bars_in_trade"] = 1253        # went DOWN, on its own
    after["outcome"]["bars_to_entry"] = 11
    assert _hash(before) == _hash(after)


def test_a_moving_quote_does_not_justify_a_revision():
    """Already true before this fix; locked so it stays true."""
    before = _record()
    after = copy.deepcopy(before)
    after["order_risk"]["bid_ask_decision"] = {"bid": 78479.2, "ask": 78479.3}
    after["order_risk"]["spread"] = 0.1000001
    after["market_context"]["data_health_reason"] = "candles fresh, 1s ago"
    assert _hash(before) == _hash(after)


@pytest.mark.parametrize("path,value", [
    (("outcome", "status"), "CLOSED"),
    (("outcome", "result"), "win"),
    (("order_risk", "actual_simulated_fill"), 123.45),
    (("order_risk", "order_id"), "o-2"),
    (("setup", "invalidation_price"), 99.0),
    (("market_context", "regime"), "BEAR"),
    (("outcome", "maximum_adverse_excursion"), -1.2),
])
def test_real_evidence_still_writes_a_revision(path, value):
    """The fix must not buy quiet by suppressing what matters.

    Excursions are deliberately still hashed: unlike the bar counters they are
    research evidence in their own right, and if they turn out to drive their
    own revision storm that is a separate decision with its own measurement.
    """
    before = _record()
    after = copy.deepcopy(before)
    node = after
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert _hash(before) != _hash(after), f"{'.'.join(path)} stopped being evidence"


def test_the_counters_are_still_recorded_even_though_they_are_not_hashed():
    """Excluding a field from the dedupe hash must not remove it from the
    payload -- the stored evidence keeps its counters, the hash just stops
    treating them as a reason to append another copy."""
    record = _record()
    projection = PriceActionJournalStore._material_projection(record)
    assert "bars_in_trade" not in projection["outcome"]
    assert record["outcome"]["bars_in_trade"] == 1254, "the caller's record was mutated"
