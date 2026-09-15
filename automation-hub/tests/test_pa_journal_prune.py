"""The one sanctioned exception to an append-only table.

pa_journal_revisions carries BEFORE UPDATE and BEFORE DELETE triggers so that
nothing can quietly rewrite what the bot did. This script lifts them on
purpose. The tests below are the price of that: it must archive before it
deletes, it must never touch a revision that records a real event, and it must
leave the guard exactly as it found it.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pa_journal_prune.py"


@pytest.fixture(scope="module")
def prune():
    spec = importlib.util.spec_from_file_location("pa_journal_prune", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def journal(tmp_path, prune):
    """A journal shaped like production: real events plus duplicate noise."""
    path = tmp_path / "price_action_paper.db"
    db = sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE pa_journal_entries(id TEXT PRIMARY KEY, session_id TEXT);
      CREATE TABLE pa_journal_revisions(
        id TEXT PRIMARY KEY, journal_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
        reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
        initiated_by TEXT NOT NULL, payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE(journal_id,revision_no), UNIQUE(journal_id,payload_hash));
    """)
    db.execute("INSERT INTO pa_journal_entries VALUES ('setup-1','s1')")
    db.execute("INSERT INTO pa_journal_entries VALUES ('setup-2','s1')")

    rows = []
    # setup-1: created, 5 duplicates, a fill, 3 more duplicates, closed.
    plan = (["SETUP_CREATED"] + ["MATERIAL_EVIDENCE_CHANGED"] * 5 + ["EXECUTION_FILL"]
            + ["MATERIAL_EVIDENCE_CHANGED"] * 3 + ["OUTCOME_CLOSED"])
    for n, reason in enumerate(plan, start=1):
        rows.append((f"r1-{n}", "setup-1", n, reason, f"t{n}", "runtime", f"h1-{n}", "{}"))
    # setup-2: still open, its newest revision is duplicate noise.
    plan2 = ["SETUP_CREATED", "LIFECYCLE_TRANSITION"] + ["MATERIAL_EVIDENCE_CHANGED"] * 4
    for n, reason in enumerate(plan2, start=1):
        rows.append((f"r2-{n}", "setup-2", n, reason, f"t{n}", "runtime", f"h2-{n}", "{}"))
    db.executemany("INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.executescript(prune.TRIGGERS)      # the guard, exactly as production has it
    db.commit()
    db.close()
    return path


def _run(prune, journal, tmp_path, *extra):
    import sys
    argv = sys.argv
    sys.argv = ["prune", "--db", str(journal), *extra]
    try:
        return prune.main()
    finally:
        sys.argv = argv


def _reasons(journal) -> dict:
    db = sqlite3.connect(journal)
    out = dict(db.execute("SELECT reason_code, COUNT(*) FROM pa_journal_revisions "
                          "GROUP BY reason_code"))
    db.close()
    return out


def test_a_dry_run_changes_nothing(prune, journal, tmp_path, capsys):
    before = _reasons(journal)
    assert _run(prune, journal, tmp_path) == 0
    assert _reasons(journal) == before
    assert "DRY RUN" in capsys.readouterr().out


def test_it_refuses_to_delete_without_an_archive(prune, journal, tmp_path, capsys):
    before = _reasons(journal)
    assert _run(prune, journal, tmp_path, "--confirm") == 2
    assert _reasons(journal) == before
    assert "refusing to delete without --archive" in capsys.readouterr().err


def test_it_refuses_to_overwrite_an_existing_archive(prune, journal, tmp_path):
    archive = tmp_path / "already-here.db"
    archive.write_text("not empty")
    before = _reasons(journal)
    assert _run(prune, journal, tmp_path, "--archive", str(archive), "--confirm") == 2
    assert _reasons(journal) == before


def test_every_real_event_survives(prune, journal, tmp_path):
    """The whole point: lifecycle evidence is not what is being removed."""
    archive = tmp_path / "pruned.db"
    assert _run(prune, journal, tmp_path, "--archive", str(archive), "--confirm") == 0

    after = _reasons(journal)
    assert after.get("SETUP_CREATED") == 2
    assert after.get("EXECUTION_FILL") == 1
    assert after.get("OUTCOME_CLOSED") == 1
    assert after.get("LIFECYCLE_TRANSITION") == 1
    # setup-2's newest revision is noise, but it is the current state, so it stays.
    assert after.get("MATERIAL_EVIDENCE_CHANGED") == 1


def test_the_newest_revision_of_an_open_setup_is_never_removed(prune, journal, tmp_path):
    """Deleting it would leave a live setup showing a state it has moved past."""
    archive = tmp_path / "pruned.db"
    _run(prune, journal, tmp_path, "--archive", str(archive), "--confirm")

    db = sqlite3.connect(journal)
    for setup in ("setup-1", "setup-2"):
        newest = db.execute("SELECT MAX(revision_no) FROM pa_journal_revisions "
                            "WHERE journal_id=?", (setup,)).fetchone()[0]
        assert newest is not None, f"{setup} lost every revision"
    assert db.execute("SELECT MAX(revision_no) FROM pa_journal_revisions "
                      "WHERE journal_id='setup-2'").fetchone()[0] == 6
    db.close()


def test_nothing_is_deleted_that_was_not_archived(prune, journal, tmp_path):
    before = sqlite3.connect(journal).execute(
        "SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]
    archive = tmp_path / "pruned.db"
    _run(prune, journal, tmp_path, "--archive", str(archive), "--confirm")

    remaining = sqlite3.connect(journal).execute(
        "SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]
    archived = sqlite3.connect(archive).execute(
        "SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]
    assert remaining + archived == before, "rows vanished without being archived"
    # setup-1 has 8 duplicates behind its closing revision, setup-2 has 3 behind
    # its newest; the newest of each setup is kept whatever its reason code.
    assert archived == 11
    assert remaining == 6


def test_the_immutability_guard_is_put_back(prune, journal, tmp_path):
    """Leaving the triggers off would silently downgrade an immutable table."""
    archive = tmp_path / "pruned.db"
    assert _run(prune, journal, tmp_path, "--archive", str(archive), "--confirm") == 0

    db = sqlite3.connect(journal)
    present = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert set(prune.REQUIRED_TRIGGERS) <= present

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM pa_journal_revisions WHERE journal_id='setup-1'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE pa_journal_revisions SET reason_code='x'")
    db.close()


def test_the_archive_holds_the_full_payload_of_what_was_removed(prune, journal, tmp_path):
    """An archive that dropped the payload would be a receipt, not a backup."""
    archive = tmp_path / "pruned.db"
    _run(prune, journal, tmp_path, "--archive", str(archive), "--confirm")

    db = sqlite3.connect(archive)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM pa_journal_revisions").fetchall()
    assert rows and all(set(r.keys()) >= {"id", "journal_id", "revision_no",
                                          "reason_code", "payload_hash",
                                          "payload_json"} for r in rows)
    assert all(r["reason_code"] == "MATERIAL_EVIDENCE_CHANGED" for r in rows)
    db.close()
