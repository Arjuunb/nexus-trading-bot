"""The setup-review renderer.

A review page is read, not asserted on, so the tests guard the things that
would quietly make it misleading rather than its appearance: that a run on
manufactured candles says so, that a rejection's cause survives into the page,
and that the escaping holds.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest

from bot.types import Bar
from services.pa_rulebook_v01 import CONFIRM_TF, CONTEXT_TF, SETUP_TF
from tests.test_pa_rulebook_engine import (
    ATR15, _confirm_series, _confirmation_for, _context_bars, _flat, _rejection_for,
)

REVIEW = Path(__file__).resolve().parents[1] / "scripts" / "pa_rulebook_review.py"
REPLAY = Path(__file__).resolve().parents[1] / "scripts" / "pa_rulebook_replay.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def review():
    return _load(REVIEW, "pa_rulebook_review")


@pytest.fixture(scope="module")
def audit(tmp_path_factory):
    """A real replay audit, produced by the real replay loop."""
    from services.pa_rulebook_v01 import RulebookConfig, build_zones

    replay = _load(REPLAY, "pa_rulebook_replay")
    config = RulebookConfig(symbol="BTCUSDT")
    context = _context_bars()
    support = [z for z in build_zones(context, config) if z.kind == "support"][-1]
    setup_bars = _flat(support.created_at + timedelta(hours=2), 40,
                       support.upper + 400.0, ATR15, minutes=15)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    filler, window = _confirm_series(rejection)
    confirms = filler + [_confirmation_for(rejection, window)]
    data = {CONTEXT_TF: context, SETUP_TF: setup_bars, CONFIRM_TF: confirms}

    out = tmp_path_factory.mktemp("audit") / "audit.json"
    replay.replay("BTCUSDT", bars=len(confirms), strategy="rejection",
                  equity=100_000.0, verbose=False,
                  loader=lambda s, tf, n, **k: (list(data[tf]), "synthetic fixture"),
                  audit_path=str(out))
    return json.loads(out.read_text())


def test_the_audit_carries_the_setup_onto_a_chart(audit):
    """Candles, the zone and the plan all have to survive the round trip."""
    record = audit["confirmations"][0]
    assert record["candles"][SETUP_TF], "no setup candles to draw"
    assert record["zone"]["lower"] < record["zone"]["upper"]
    assert record["plan"]["stop"] < record["plan"]["entry_bound"]
    assert record["plan"]["evidence"]["target_zone_id"], "target level not named"
    assert record["confirm_slot"] in (1, 2, 3)


def test_a_synthetic_run_says_so_on_the_page(review, audit):
    """The one claim a standalone HTML file cannot make quietly.

    Once the page leaves this machine nothing else on it says the candles were
    manufactured, and a review that measures nothing looks exactly like one
    that measures something.
    """
    page = review.render(audit)
    assert "Synthetic candles" in page
    assert "these numbers measure nothing" in page

    real = {**audit, "meta": {**audit["meta"], "sources": {"1h": "live (ccxt)"}}}
    assert "Synthetic candles" not in review.render(real)


def test_a_partial_replay_cannot_pass_for_the_whole_window(review, audit):
    """A checkpointed audit is honest about a shorter period; the page has to
    say so, or 25 setups "in 2025" may really be 25 setups in five weeks."""
    partial = {**audit, "meta": {**audit["meta"], "complete": False,
                                 "progress": {"candles_judged": 4100,
                                              "candles_total": 105_000,
                                              "through": "2025-02-12T00:00:00+00:00"}}}
    page = review.render(partial)
    assert "Partial replay" in page
    assert "4100 of 105000 candles" in page
    assert "shorter period" in page

    assert "Partial replay" not in review.render(audit)     # no key: written at the end
    finished = {**audit, "meta": {**audit["meta"], "complete": True}}
    assert "Partial replay" not in review.render(finished)


def test_the_rejection_reason_reaches_the_page(review, audit):
    """A refused setup must carry its cause, not just a red badge."""
    refused = json.loads(json.dumps(audit))
    plan = refused["confirmations"][0]["plan"]
    plan["blocker"] = "NET_RR_TOO_LOW"
    plan["net_rr"] = 2.1
    plan["evidence"].update({"target_room": 900.0, "required_room_for_min_rr": 1400.0,
                             "target_zone_id": "zone-res-180", "cost_share_of_risk": 0.22})
    refused["confirmations"][0]["verdict"] = "NET_RR_TOO_LOW"

    page = review.render(refused)
    assert "NET_RR_TOO_LOW" in page
    assert "zone-res-180" in page                 # which level capped the target
    assert "room the 2.5R gate required" in page  # the comparison, not just the ratio
    assert "Room to the next opposing level" in page


def test_every_chart_is_closed_and_the_page_has_a_table_view(review, audit):
    page = review.render(audit)
    assert page.count("<svg") == page.count("</svg>") > 0
    assert "<table>" in page and "</table>" in page
    assert 'name="viewport"' in page
    # Dark mode is declared under both scopes, so an OS setting and an explicit
    # theme stamp both resolve.
    assert "prefers-color-scheme:dark" in page and '[data-theme="dark"]' in page


def test_a_hostile_zone_id_cannot_inject_markup(review, audit):
    """Ids reach the page as text. They are engine-generated today, which is
    exactly the assumption that quietly stops holding."""
    poisoned = json.loads(json.dumps(audit))
    poisoned["confirmations"][0]["zone"]["id"] = '<script>alert(1)</script>'
    page = review.render(poisoned)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# ------------------------------------------------- the evidence checker

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "pa_rulebook_audit_check.py"


@pytest.fixture(scope="module")
def checker():
    return _load(CHECKER, "pa_rulebook_audit_check")


def test_a_complete_audit_passes_the_field_check(checker, audit, tmp_path):
    """The replay's own output is the schema, so it must validate clean.

    If this ever fails, the replay and the review have drifted apart and the
    checker is reporting a real gap rather than a false alarm.
    """
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(audit))
    assert checker.check(path) in (0, 1)       # 1 only for the outcome tally

    missing = []
    for record in audit["confirmations"]:
        has_target, _ = checker._dig(record, "plan.target")
        required = checker.REQUIRED + (checker.REQUIRED_WITH_TARGET
                                       if has_target is not None else [])
        for field, _why in required:
            value, present = checker._dig(record, field)
            if not present or (value is None and field not in checker.NULLABLE):
                missing.append(field)
    assert missing == [], f"the replay does not emit {sorted(set(missing))}"


def test_a_null_blocker_on_an_accepted_setup_is_not_reported_missing(checker, audit):
    """The most complete records must not be the ones flagged.

    ``plan.blocker`` is null exactly when a setup was accepted, and treating
    null as absent would report every winning trade as broken evidence.
    """
    accepted = [r for r in audit["confirmations"] if r["verdict"] == "ACCEPTED"]
    assert accepted, "fixture produced no accepted setup"
    value, present = checker._dig(accepted[0], "plan.blocker")
    assert present and value is None
    assert "plan.blocker" in checker.NULLABLE


def test_a_missing_field_is_named_rather_than_guessed(checker, audit, tmp_path, capsys):
    """The point of the checker: say which field, not "malformed"."""
    broken = json.loads(json.dumps(audit))
    broken["confirmations"][0]["candles"]["15m"] = []
    del broken["confirmations"][0]["plan"]["stop"]
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken))

    checker.check(path)
    out = capsys.readouterr().out
    assert "candles.15m" in out and "plan.stop" in out
    assert "the historical candles the chart is drawn from" in out


def test_an_archive_without_a_payload_says_what_it_did_contain(checker, tmp_path, capsys):
    """A bare "could not read it" costs a round trip; listing the entries does not."""
    import zipfile

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("trades.csv", "a,b\n1,2\n")
        zf.writestr("notes.txt", "hello")

    assert checker.check(archive) == 2
    out = capsys.readouterr().out
    assert "trades.csv" in out and "notes.txt" in out


def test_the_gate_sensitivity_says_whether_the_threshold_is_the_problem(review, audit):
    """32 refusals at 2.5R reads like a strict parameter until you see that the
    median is 0.2R. The count per candidate gate is what tells them apart."""
    records = [
        {"plan": {"net_rr": 0.2}}, {"plan": {"net_rr": 0.4}},
        {"plan": {"net_rr": 1.2}}, {"plan": {"net_rr": 2.7}},
        {"plan": None}, {},                      # no plan: not counted either way
    ]
    rows = dict((level, (passing, total))
                for level, passing, total in review.threshold_sensitivity(records))
    assert rows[0.5] == (2, 4)   # 0.2 and 0.4 are both below 0.5
    assert rows[1.0] == (2, 4)
    assert rows[2.5] == (1, 4)
    assert rows[3.0] == (0, 4)
    assert review.threshold_sensitivity([]) == []

    page = review.render(audit)
    assert "clearing each net-RR gate" in page
    assert "not a re-run" in page


def test_the_text_reconciliation_carries_the_formulas_and_the_counts(review, audit):
    import io
    out = io.StringIO()
    review.summarise(audit, out=out)
    text = out.getvalue()

    assert "Funnel" in text and "reached CONFIRMED" in text
    assert "(|T-E| - costs_win) / (|E-S| + costs_loss), n=" in text
    assert "|E-S| / ATR15, n=" in text
    assert "Confirmations clearing each net-RR gate" in text
    assert "Every confirmation" in text
    for record in audit["confirmations"]:
        assert record["verdict"] in text


def test_the_text_reconciliation_admits_a_partial_run(review, audit):
    import io
    partial = {**audit, "meta": {**audit["meta"], "complete": False,
                                 "progress": {"candles_judged": 10, "candles_total": 99,
                                              "through": "2025-02-01T00:00:00+00:00"}}}
    out = io.StringIO()
    review.summarise(partial, out=out)
    assert "PARTIAL" in out.getvalue()


def test_the_text_reconciliation_contains_no_markup(review, audit):
    """It shipped once printing the literal text "&mdash;" in a column of
    numbers, because the page's formatter was reused without changing the
    placeholder. Plain text output must be plain text."""
    import io
    refused = copy.deepcopy(audit)
    for record in refused["confirmations"]:
        record["plan"] = {**(record.get("plan") or {}), "net_rr": None,
                          "stop_distance_atr": None, "entry_drift_atr": None}
    out = io.StringIO()
    review.summarise(refused, out=out)
    text = out.getvalue()

    assert "&mdash;" not in text
    assert "&" not in text.replace("&&", "")      # no entity of any kind
    assert "<" not in text and ">" not in text.replace("->", "")
    assert " -- " in text                          # the plain-text stand-in
