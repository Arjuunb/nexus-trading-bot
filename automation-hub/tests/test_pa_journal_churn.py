"""The churn analyser has to convict the right field, or it is worse than useless.

Two earlier diagnoses of this journal's growth were wrong because they were
reasoned rather than measured. These tests pin the measurement itself: a field
the deployed projection already excludes must be reported as *suppressed*, a
field that still differs must be named, and a field that only ever changes
alongside another must not be convicted on its own.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pa_journal_churn.py"


@pytest.fixture(scope="module")
def churn():
    spec = importlib.util.spec_from_file_location("pa_journal_churn", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = {
    "identity": {"journal_entry_id": "j", "symbol": "BTCUSDT"},
    "market_context": {"zone_id": "z-1", "data_health_reason": "ok"},
    "setup": {"state": "FILLED"},
    "order_risk": {"stop": 99740.0, "target": 103000.0,
                   "bid_ask_decision": {"bid": 1.0, "ask": 2.0}, "spread": 0.5},
    "outcome": {"status": "OPEN", "result": "open", "bars_in_trade": 10,
                "bars_to_entry": 2, "maximum_adverse_excursion": -0.10},
    "review": {"tags": []},
    "chart_state": {"entry": 100860.0},
}


def _journal(tmp_path, revisions):
    """revisions: list of (journal_id, reason_code, mutate) applied cumulatively."""
    path = tmp_path / "price_action_paper.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE pa_journal_revisions(
        id TEXT PRIMARY KEY, journal_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
        reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
        initiated_by TEXT NOT NULL, payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL, UNIQUE(journal_id,revision_no))""")
    numbers: dict = {}
    current: dict = {}
    for journal_id, reason, mutate in revisions:
        record = copy.deepcopy(current.get(journal_id) or BASE)
        record["identity"]["journal_entry_id"] = journal_id
        mutate(record)
        current[journal_id] = record
        numbers[journal_id] = numbers.get(journal_id, 0) + 1
        db.execute("INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex, journal_id, numbers[journal_id], reason,
                    f"2026-09-15T00:{numbers[journal_id]:02d}:00+00:00", "runtime",
                    uuid.uuid4().hex, json.dumps(record, sort_keys=True)))
    db.commit()
    db.close()
    return path


def _run(churn, path, **kwargs):
    out = io.StringIO()
    options = {"reason": churn.CHURN_REASON, "since": None, "top": 15, "samples": 2}
    options.update(kwargs)
    totals = churn.analyse(str(path), out=out, **options)
    return totals, out.getvalue()


def _nothing(record):
    return None


def test_a_field_the_deployed_projection_drops_is_reported_as_suppressed(churn, tmp_path):
    def tick(record):
        record["outcome"]["bars_in_trade"] += 1

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", tick)] * 4)
    totals, text = _run(churn, path)

    assert totals["pairs"] == 4
    assert totals["suppressed"] == 4          # the build already writes none of these
    assert totals["analysed"] == 0
    assert totals["sole_cause"] == {}
    assert "CLEAN" in text and "would write none of them" in text


def test_a_field_that_still_drifts_is_named_with_its_count_and_bytes(churn, tmp_path):
    def drift(record):
        record["outcome"]["maximum_adverse_excursion"] -= 0.01
        record["outcome"]["bars_in_trade"] += 1      # excluded: must not mask the real one

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", drift)] * 6)
    totals, text = _run(churn, path)

    assert totals["analysed"] == 6
    assert totals["suppressed"] == 0
    assert totals["sole_cause"] == {"outcome.maximum_adverse_excursion": 6}
    assert "outcome.maximum_adverse_excursion" in text
    assert "-0.1" in text and "->" in text          # the sample shows the drift


def test_a_field_is_not_convicted_when_it_never_changes_alone(churn, tmp_path):
    def pair(record):
        record["outcome"]["maximum_adverse_excursion"] -= 0.01
        record["order_risk"]["stop"] += 1.0

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", pair)] * 3)
    totals, text = _run(churn, path)

    assert totals["sole_cause"] == {}
    assert totals["changed_in"] == {"outcome.maximum_adverse_excursion": 3,
                                    "order_risk.stop": 3}
    assert "no single field can be convicted" in text


def test_lifecycle_revisions_are_not_counted_as_churn(churn, tmp_path):
    def close(record):
        record["outcome"]["status"] = "CLOSED"

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing),
                               ("j1", "OUTCOME_CLOSED", close)])
    totals, _ = _run(churn, path)
    assert totals["pairs"] == 0 and totals["analysed"] == 0

    totals_all, _ = _run(churn, path, reason=None)
    assert totals_all["pairs"] == 1
    assert totals_all["sole_cause"] == {"outcome.status": 1}


def test_each_setup_is_walked_separately(churn, tmp_path):
    """A pair must never straddle two setups: that would diff unrelated records."""
    def drift(record):
        record["outcome"]["maximum_adverse_excursion"] -= 0.01

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing),
                               ("j2", "SETUP_CREATED", _nothing),
                               ("j1", "MATERIAL_EVIDENCE_CHANGED", drift),
                               ("j2", "MATERIAL_EVIDENCE_CHANGED", drift)])
    totals, _ = _run(churn, path)
    assert totals["pairs"] == 2
    assert totals["sole_cause"] == {"outcome.maximum_adverse_excursion": 2}


def test_the_analyser_cannot_write_to_the_journal(churn, tmp_path):
    """It is meant to be safe against a live journal, so it opens read-only."""
    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)])
    before = path.read_bytes()
    _run(churn, path)
    assert path.read_bytes() == before

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM pa_journal_revisions")
    db.close()


def test_a_list_field_says_whether_it_grew_or_was_rewritten(churn):
    """For state_transitions this is the only question that matters.

    An appended row is a real event and earns its revision. The same row
    written again with a different feed state is the past being restamped with
    the present, which is the defect. Truncated JSON shows neither.
    """
    recorded = {"id": "t-1", "to_phase": "REJECTED", "market_data_health": "SYNCHRONIZED"}
    later = {"id": "t-2", "to_phase": "CLOSED", "market_data_health": "SYNCHRONIZED"}

    grew = churn._describe_list_change(json.dumps([recorded]),
                                       json.dumps([recorded, later]))
    assert "1 -> 2 rows" in grew and "appended 1" in grew and "a real change" in grew

    rewritten = churn._describe_list_change(
        json.dumps([recorded]),
        json.dumps([{**recorded, "market_data_health": "DEGRADED"}]))
    assert "SAME LENGTH" in rewritten and "rewritten in place" in rewritten
    assert "row 0.market_data_health" in rewritten
    assert "SYNCHRONIZED" in rewritten and "DEGRADED" in rewritten

    assert "identical" in churn._describe_list_change(json.dumps([recorded]),
                                                      json.dumps([recorded]))


def test_a_scalar_field_is_left_to_the_plain_before_and_after(churn):
    """The list description must not swallow the numbers it cannot explain."""
    assert churn._describe_list_change("1242", "235") is None
    assert churn._describe_list_change('"a"', '"b"') is None
    assert churn._describe_list_change("not json", "also not") is None
    assert churn._describe_list_change('{"a": 1}', '{"a": 2}') is None


def test_the_description_reaches_the_report(churn, tmp_path):
    def restamp(record):
        rows = record["setup"].setdefault("state_transitions",
                                          [{"id": "t-1", "market_data_health": "SYNCHRONIZED"}])
        rows[0]["market_data_health"] = "DEGRADED" if rows[0]["market_data_health"] == "SYNCHRONIZED" else "SYNCHRONIZED"

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", restamp)] * 3)
    totals, text = _run(churn, path)
    assert totals["sole_cause"] == {"setup.state_transitions": 3}
    assert "rewritten in place" in text
    assert "row 0.market_data_health" in text


def test_the_report_says_what_it_cannot_measure(churn, tmp_path):
    """It replays the projection over stored rows, so a capture-time fix is
    invisible to it. Reading the unchanged number as "the fix did not work"
    already cost one round trip; the report now says so itself."""
    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)])
    _, text = _run(churn, path)
    assert "cannot show here" in text
    assert "--since" in text


def test_an_empty_window_is_not_reported_as_a_clean_one(churn, tmp_path):
    """Three different situations print zero and only one is good news.

    Asking for a window that has not started yet returned no rows, and the
    report announced that every revision in it had been suppressed by the
    current build. A conclusion drawn from no data is worse than no
    conclusion, because it stops the next question being asked.
    """
    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", _nothing)] * 3)

    totals, text = _run(churn, path, since="2099-01-01T00:00:00")
    assert totals["scanned"] == 0 and totals["pairs"] == 0
    assert "NOTHING MEASURED" in text
    assert "in the future" in text
    assert "CLEAN" not in text


def test_a_window_with_no_second_capture_says_so(churn, tmp_path):
    """Revisions present but never two of the same setup: nothing to compare,
    which is not the same as nothing to fix."""
    path = _journal(tmp_path, [("j1", "MATERIAL_EVIDENCE_CHANGED", _nothing),
                               ("j2", "MATERIAL_EVIDENCE_CHANGED", _nothing)])
    totals, text = _run(churn, path)
    assert totals["scanned"] == 2 and totals["pairs"] == 0
    assert "NOTHING COMPARED" in text
    assert "Leave it running longer" in text
    assert "CLEAN" not in text


def test_a_past_since_bound_is_not_called_future(churn):
    assert churn._is_future("2099-01-01T00:00:00") is True
    assert churn._is_future("2020-01-01T00:00:00") is False
    assert churn._is_future("2020-01-01T00:00:00+00:00") is False
    assert churn._is_future("not a timestamp") is False


def test_a_window_diffs_against_the_revision_before_it(churn, tmp_path):
    """The first revision after a deploy is the one worth measuring.

    Its predecessor was written by the old build and is the baseline, not a
    measurement. Filtering it out with the window meant that revision could
    never be analysed, so --since under-measured exactly the window it exists
    for: the run that prompted this found 5 revisions and 0 pairs.
    """
    def drift(record):
        record["outcome"]["maximum_adverse_excursion"] -= 0.01

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", drift)] * 3)
    # The fixture stamps 00:01, 00:02, 00:03, 00:04.
    totals, text = _run(churn, path, since="2026-09-15T00:04:00")

    assert totals["scanned"] == 1, "only the last revision is inside the window"
    assert totals["pairs"] == 1, "and it is still comparable to the one before it"
    assert totals["sole_cause"] == {"outcome.maximum_adverse_excursion": 1}
    assert "1 against a baseline from before the window" in text


def test_a_window_does_not_measure_pairs_that_predate_it(churn, tmp_path):
    """The baseline is read, not counted. Otherwise --since would quietly
    re-measure the history it was asked to exclude."""
    def drift(record):
        record["outcome"]["maximum_adverse_excursion"] -= 0.01

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", drift)] * 5)
    everything, _ = _run(churn, path)
    windowed, _ = _run(churn, path, since="2026-09-15T00:05:00")

    assert everything["pairs"] == 5
    assert windowed["pairs"] == 2, "00:05 and 00:06 only"
    assert windowed["sole_cause"] == {"outcome.maximum_adverse_excursion": 2}


def test_a_baseline_inside_the_window_is_not_reported_as_carried(churn, tmp_path):
    def drift(record):
        record["outcome"]["maximum_adverse_excursion"] -= 0.01

    path = _journal(tmp_path, [("j1", "SETUP_CREATED", _nothing)]
                    + [("j1", "MATERIAL_EVIDENCE_CHANGED", drift)] * 3)
    _, text = _run(churn, path, since="2020-01-01T00:00:00")
    assert "baseline from before the window" not in text
