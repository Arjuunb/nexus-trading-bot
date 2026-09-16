"""A journal revision must record something that happened.

On the production Price Action lab, 59,560 of 72,443 revisions carried the
reason code MATERIAL_EVIDENCE_CHANGED while recording no lifecycle event at
all. Each was a ~9.5 KB copy of the previous one, and together they grew the
database until journal writes started timing out and the lab correctly refused
to place orders it could not durably record.

The cause was the dedupe hash, not the journal: it covered three numbers that
are positions in the runtime's rolling candle buffer rather than facts about
the setup -- two bar counters and the entry expiry index. All three move as the
window slides and jump when it is re-seeded. Excluding the counters alone left
7,833 revisions still being written, and scripts/pa_journal_churn.py convicted
the expiry index of 5,212 of them as the sole differing field.

These tests pin the difference between a change that justifies a revision and
one that does not.
"""
from __future__ import annotations

import copy
import json

import pytest

from services.price_action_governance import PriceActionJournalStore


def _record() -> dict:
    """A minimally shaped journal record; only the hashed fields matter here."""
    return {
        "identity": {"journal_entry_id": "j-1", "session_id": "s-1", "symbol": "BTCUSDT"},
        "market_context": {"regime": "BULL", "data_health_reason": "candles fresh"},
        "setup": {"id": "setup-1", "direction": "long", "invalidation_price": 100.0},
        "order_risk": {"order_id": "o-1", "actual_simulated_fill": 101.0,
                       "expiry_index": 1242,
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


def test_a_drifting_entry_expiry_index_does_not_justify_a_revision():
    """The field the churn measurement convicted.

    expiry_index is ``len(bars) - 1 + entry_expiry_bars``, so it is wherever
    the rolling buffer happens to end, not when the proposal expires. Re-seed
    the buffer with fewer bars and it drops: 1242 to 235, observed between two
    consecutive revisions of a setup nothing had happened to.
    """
    before = _record()
    after = copy.deepcopy(before)
    after["order_risk"]["expiry_index"] = 235       # the buffer was re-seeded
    assert _hash(before) == _hash(after)


def test_the_three_unstable_numbers_do_not_justify_a_revision_together():
    """They were observed moving in the same capture; no combination of them
    is a lifecycle event, so no combination may append a copy of the record."""
    before = _record()
    after = copy.deepcopy(before)
    after["order_risk"]["expiry_index"] = 380
    after["outcome"]["bars_in_trade"] = 1253
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
    assert "bars_to_entry" not in projection["outcome"]
    assert "expiry_index" not in projection["order_risk"]
    assert record["outcome"]["bars_in_trade"] == 1254, "the caller's record was mutated"
    assert record["order_risk"]["expiry_index"] == 1242, "the caller's record was mutated"


def test_a_recorded_transition_keeps_the_values_it_was_recorded_with():
    """A transition is a record of something that happened.

    The runtime rebuilt it on every capture, stamping the feed state and the
    proposal prices of *now* onto a past event. The event had not changed, so
    the revision recorded nothing -- 2,211 of the 7,833 still being written on
    the counters-only build carried a changed state_transitions.
    """
    from services.price_action_governance import _transition_key

    recorded = {"id": "t-1", "setup_id": "setup-1", "from_phase": "TRIGGERED",
                "to_phase": "REJECTED", "timestamp": "2026-09-13T17:00:00+00:00",
                "market_data_health": "SYNCHRONIZED",
                "relevant_prices": {"entry": 100.0, "stop": 99.0, "target": 103.0}}
    rebuilt = {**recorded, "market_data_health": "DEGRADED",
               "relevant_prices": {"entry": 100.4, "stop": 99.0, "target": 103.0}}

    # Same event, so the same key -- which is what makes it a rewrite and not
    # an append.
    assert _transition_key(recorded) == _transition_key(rebuilt)

    later = {**recorded, "id": "t-2", "to_phase": "FILLED"}
    assert _transition_key(later) != _transition_key(recorded)


def test_a_legacy_transition_without_an_id_still_matches_itself():
    """Otherwise the fix would append a copy of it on every single capture --
    turning a dedupe bug into an unbounded one."""
    from services.price_action_governance import _transition_key

    row = {"setup_id": "setup-1", "from_phase": "A", "to_phase": "B",
           "timestamp": "2026-09-13T17:00:00+00:00", "market_data_health": "SYNCHRONIZED"}
    drifted = {**row, "market_data_health": "DEGRADED"}
    assert _transition_key(row) == _transition_key(drifted)
    assert _transition_key({**row, "to_phase": "C"}) != _transition_key(row)


def test_a_flapping_feed_does_not_reclassify_a_finished_setup():
    """The 367 review.* revisions, and a correctness bug behind them.

    _classification reads market_data_health out of the recorded transitions.
    While those were rebuilt from the live feed, a setup decided on healthy
    data was relabelled DATA_QUALITY_FAILURE minutes later because the feed
    happened to be degraded at capture time -- and relabelled back when it
    recovered. Preserving the recorded transition fixes the churn and the
    misclassification together.
    """
    healthy = {
        "market_context": {"data_health_state": "SYNCHRONIZED"},
        "review": {"rule_compliance": True},
        "outcome": {"status": "CLOSED", "result": "win", "net_r": 2.4, "gross_r": 2.6},
        "setup": {"state_transitions": [
            {"id": "t-1", "market_data_health": "SYNCHRONIZED"}]},
    }
    classification, _ = PriceActionJournalStore._classification(healthy)
    assert classification != "DATA_QUALITY_FAILURE"

    # The same setup, with the transition rebuilt from a degraded live feed.
    degraded = copy.deepcopy(healthy)
    degraded["setup"]["state_transitions"][0]["market_data_health"] = "DEGRADED"
    assert PriceActionJournalStore._classification(degraded)[0] == "DATA_QUALITY_FAILURE"


def test_a_rewritten_transition_does_not_write_a_revision(tmp_path):
    """End to end, through the real store: the same event re-reported with a
    different feed state must not append another copy of the record, and a
    genuinely new transition still must."""
    from tests.test_price_action_governance import evidence

    state, session, paper, feed, partition = evidence()
    state["setups"][0]["transitions"] = [
        {"setup_id": "setup-1", "from_phase": "ORDER_PENDING", "to_phase": "LOST",
         "timestamp": "2026-08-24T10:05:00+00:00",
         "market_data_health": "SYNCHRONIZED"}]

    store = PriceActionJournalStore(tmp_path / "pa.db")

    def _capture(visual):
        # An empty list means nothing was appended, which is the whole point.
        return store.capture(visual_state=visual, session=session, paper_state=paper,
                             feed_status=feed, partition_label=partition)

    def _revisions(journal_id):
        return store._db.execute(
            "SELECT COUNT(*) FROM pa_journal_revisions WHERE journal_id=?",
            (journal_id,)).fetchone()[0]

    journal_id = _capture(state)[0]
    assert _revisions(journal_id) == 1

    flapped = copy.deepcopy(state)
    flapped["setups"][0]["transitions"][0]["market_data_health"] = "DEGRADED"
    assert _capture(flapped) == [], "a rewritten transition wrote a revision"
    assert _revisions(journal_id) == 1

    extended = copy.deepcopy(state)
    extended["setups"][0]["transitions"].append(
        {"setup_id": "setup-1", "from_phase": "LOST", "to_phase": "CLOSED",
         "timestamp": "2026-08-24T10:20:00+00:00",
         "market_data_health": "SYNCHRONIZED"})
    assert _capture(extended) == [journal_id], "a real new transition was swallowed"
    assert _revisions(journal_id) == 2

    stored = json.loads(store._db.execute(
        "SELECT payload_json FROM pa_journal_revisions WHERE journal_id=? "
        "ORDER BY revision_no DESC LIMIT 1", (journal_id,)).fetchone()[0])
    rows = stored["setup"]["state_transitions"]
    assert len(rows) == 2, rows
    assert rows[0]["market_data_health"] == "SYNCHRONIZED", "the record was rewritten"


def test_the_decision_fingerprint_does_not_move_with_the_candle_buffer(tmp_path):
    """dataset_fingerprint identifies the data a decision was made on, so two
    runs over identical candles must fingerprint it identically. It covered
    valid_until_index, which is a position in the rolling candle buffer."""
    from tests.test_price_action_governance import evidence

    def fingerprint(name, **proposal):
        state, session, paper, feed, partition = evidence()
        state["proposals"][0].update(proposal)
        store = PriceActionJournalStore(tmp_path / name)
        journal_id = store.capture(
            visual_state=state, session=session, paper_state=paper,
            feed_status=feed, partition_label=partition)[0]
        return store._db.execute(
            "SELECT dataset_fingerprint FROM pa_journal_entries WHERE id=?",
            (journal_id,)).fetchone()[0]

    assert (fingerprint("a.db", valid_until_index=3)
            == fingerprint("b.db", valid_until_index=99_999))
    # It must still discriminate the evidence that actually defines the decision.
    assert (fingerprint("c.db", valid_until_index=3)
            != fingerprint("d.db", valid_until_index=3, entry=123.45))
