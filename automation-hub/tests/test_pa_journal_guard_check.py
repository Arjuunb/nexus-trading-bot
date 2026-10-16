"""The check that says whether the journal is still append-only.

This check exists because a previous one lied. It probed with
``DELETE ... WHERE 1=0``, which matches no rows, so SQLite never fired the
row-level trigger and the probe reported the guard was off while all four
triggers were present and working. A verifier that can pass a broken database
or fail a sound one is worse than none, so both directions are pinned here.
"""
from __future__ import annotations

import importlib.util
import io
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pa_journal_guard_check.py"
PRUNE = Path(__file__).resolve().parents[1] / "scripts" / "pa_journal_prune.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load(SCRIPT, "pa_journal_guard_check")


@pytest.fixture(scope="module")
def triggers():
    return _load(PRUNE, "pa_journal_prune").TRIGGERS


def _journal(tmp_path, triggers, *, guarded=True, rows=3):
    path = tmp_path / "price_action_paper.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE pa_journal_entries(id TEXT PRIMARY KEY, created_at TEXT);
      CREATE TABLE pa_journal_revisions(
        id TEXT PRIMARY KEY, journal_id TEXT, created_at TEXT);
    """)
    for n in range(rows):
        db.execute("INSERT INTO pa_journal_entries VALUES (?,?)", (f"e{n}", "t"))
        db.execute("INSERT INTO pa_journal_revisions VALUES (?,?,?)", (f"r{n}", "e0", "t"))
    if guarded:
        db.executescript(triggers)
    db.commit()
    db.close()
    return path


def _run(guard, path):
    out = io.StringIO()
    return guard.check(str(path), out=out), out.getvalue()


def test_a_guarded_journal_passes(guard, triggers, tmp_path):
    code, text = _run(guard, _journal(tmp_path, triggers))
    assert code == 0, text
    assert "GUARD HOLDS" in text
    assert text.count("refused") == 4          # delete and update, both tables
    assert "none missing" in text


def test_the_probe_targets_a_real_row_so_a_dropped_trigger_is_caught(
        guard, triggers, tmp_path):
    """The exact failure the old probe missed: the trigger is gone, a delete of
    an existing row would really succeed, and the check has to say so."""
    path = _journal(tmp_path, triggers)
    db = sqlite3.connect(path)
    db.executescript("DROP TRIGGER pa_journal_revisions_no_delete")
    db.commit()
    db.close()

    code, text = _run(guard, path)
    assert code == 1
    assert "GUARD IS OFF" in text
    assert "NOT GUARDED  DELETE pa_journal_revisions" in text
    assert "pa_journal_revisions_no_delete" in text
    assert "row counts unchanged" in text      # it proved it without destroying one


def test_an_accepted_probe_is_still_rolled_back(guard, triggers, tmp_path):
    path = _journal(tmp_path, triggers, guarded=False, rows=4)
    before = sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]

    code, text = _run(guard, path)

    assert code == 1 and "GUARD IS OFF" in text
    after = sqlite3.connect(path).execute(
        "SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]
    assert after == before == 4                # nothing was lost proving the point


def test_an_empty_table_is_not_reported_as_a_passing_guard(guard, triggers, tmp_path):
    """With no rows there is nothing to test against, and 'no error' must not be
    read as 'guard holds' -- that is precisely the old probe's mistake."""
    path = _journal(tmp_path, triggers, rows=0)
    code, text = _run(guard, path)
    assert code == 1
    assert "nothing real to test the guard against" in text
