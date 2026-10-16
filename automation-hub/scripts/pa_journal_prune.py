#!/usr/bin/env python3
"""Archive and remove the duplicate revisions from the Price Action journal.

pa_journal_revisions is append-only by design: it carries BEFORE UPDATE and
BEFORE DELETE triggers so nothing can quietly rewrite what the bot did. This
script is the deliberate, auditable exception -- it drops those triggers,
removes rows, and puts the triggers back. It refuses to finish if it cannot
restore them.

What it removes, and nothing else: revisions whose reason_code is
MATERIAL_EVIDENCE_CHANGED and which are NOT the newest revision of their setup.
Those are the copies written because a drifting bar counter changed the dedupe
hash; they record no lifecycle event. Every SETUP_CREATED, LIFECYCLE_TRANSITION,
EXECUTION_FILL and OUTCOME_CLOSED revision is kept, and so is the latest
revision of every setup, so each one keeps its creation, its transitions, its
fills, its outcome and its current state.

Everything removed is written to an archive database first, and the archive is
counted and compared before a single row is deleted. Nothing is destroyed that
has not already been copied.

    python scripts/pa_journal_prune.py                      # dry run, the default
    python scripts/pa_journal_prune.py --archive /var/lib/tradexa/backups/pa_pruned.db --confirm

Stop the app first. VACUUM needs exclusive access, and reclaiming the space is
the point -- deleting rows alone leaves the file the same size.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DB = "/var/lib/tradexa/price_action_paper.db"

#: Recreated verbatim from PriceActionJournalStore._create_immutability_triggers.
#: Restoring them is not optional: leaving them off would silently downgrade an
#: immutable evidence table to an ordinary one.
TRIGGERS = """
  CREATE TRIGGER IF NOT EXISTS pa_journal_entries_no_update
  BEFORE UPDATE ON pa_journal_entries BEGIN
    SELECT RAISE(ABORT,'Price Action journal entries are immutable');
  END;
  CREATE TRIGGER IF NOT EXISTS pa_journal_entries_no_delete
  BEFORE DELETE ON pa_journal_entries BEGIN
    SELECT RAISE(ABORT,'Price Action journal entries are immutable');
  END;
  CREATE TRIGGER IF NOT EXISTS pa_journal_revisions_no_update
  BEFORE UPDATE ON pa_journal_revisions BEGIN
    SELECT RAISE(ABORT,'Price Action journal revisions are immutable');
  END;
  CREATE TRIGGER IF NOT EXISTS pa_journal_revisions_no_delete
  BEFORE DELETE ON pa_journal_revisions BEGIN
    SELECT RAISE(ABORT,'Price Action journal revisions are immutable');
  END;
"""

REQUIRED_TRIGGERS = ("pa_journal_entries_no_update", "pa_journal_entries_no_delete",
                     "pa_journal_revisions_no_update", "pa_journal_revisions_no_delete")

#: A revision is removable only if it records no lifecycle event AND is not the
#: newest one for its setup. The second half is what keeps the current state of
#: every open setup intact.
SELECT_REMOVABLE = """
  SELECT r.* FROM pa_journal_revisions r
  WHERE r.reason_code = 'MATERIAL_EVIDENCE_CHANGED'
    AND r.revision_no <> (SELECT MAX(revision_no) FROM pa_journal_revisions
                          WHERE journal_id = r.journal_id)
"""


def _size(path: str) -> float:
    return os.path.getsize(path) / 1e6 if os.path.exists(path) else 0.0


def _report(db: sqlite3.Connection) -> None:
    total = db.execute("SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]
    print(f"  revisions            {total:>9,}")
    for row in db.execute("""SELECT reason_code, COUNT(*) n FROM pa_journal_revisions
                             GROUP BY reason_code ORDER BY n DESC"""):
        print(f"    {row[1]:>9,}  {row[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DB)
    parser.add_argument("--archive", help="where the removed rows are written first")
    parser.add_argument("--confirm", action="store_true",
                        help="actually delete; without it this is a dry run")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    print(f"database {args.db}  ({_size(args.db):,.1f} MB)\n")
    print("BEFORE")
    _report(db)

    removable = db.execute(
        f"SELECT COUNT(*), COALESCE(SUM(LENGTH(payload_json)),0) FROM ({SELECT_REMOVABLE})"
    ).fetchone()
    count, payload_bytes = removable[0], removable[1]
    print(f"\n  removable            {count:>9,}  (~{payload_bytes/1e6:,.1f} MB of payload)")
    print(f"  kept                 {db.execute('SELECT COUNT(*) FROM pa_journal_revisions').fetchone()[0] - count:>9,}")

    if not args.confirm:
        print("\nDRY RUN -- nothing changed. Re-run with --archive PATH --confirm to apply.")
        return 0
    if not args.archive:
        print("\nrefusing to delete without --archive: the rows must be copied first.",
              file=sys.stderr)
        return 2
    if os.path.exists(args.archive):
        print(f"\nrefusing to overwrite an existing archive: {args.archive}", file=sys.stderr)
        return 2
    if count == 0:
        print("\nnothing to remove.")
        return 0

    # 1. Copy every removable row out, and prove the copy is complete.
    print(f"\narchiving {count:,} rows to {args.archive}")
    archive = sqlite3.connect(args.archive)
    archive.execute("""CREATE TABLE pa_journal_revisions(
        id TEXT PRIMARY KEY, journal_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
        reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
        initiated_by TEXT NOT NULL, payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL)""")
    rows = db.execute(SELECT_REMOVABLE).fetchall()
    archive.executemany(
        "INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)",
        [tuple(r) for r in rows])
    archive.commit()
    archived = archive.execute("SELECT COUNT(*) FROM pa_journal_revisions").fetchone()[0]
    archive.close()
    if archived != count:
        print(f"archive holds {archived:,} of {count:,} rows -- deleting nothing.",
              file=sys.stderr)
        return 3
    print(f"  archive verified     {archived:>9,} rows  ({_size(args.archive):,.1f} MB)")

    # 2. Lift the guard, delete, put the guard back. The restore is checked.
    try:
        for name in ("pa_journal_revisions_no_delete", "pa_journal_revisions_no_update"):
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        deleted = db.execute(
            f"DELETE FROM pa_journal_revisions WHERE id IN "
            f"(SELECT id FROM ({SELECT_REMOVABLE}))").rowcount
        db.commit()
        print(f"  deleted              {deleted:>9,} rows")
    finally:
        db.executescript(TRIGGERS)
        db.commit()

    present = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    missing = [t for t in REQUIRED_TRIGGERS if t not in present]
    if missing:
        print(f"\nIMMUTABILITY TRIGGERS NOT RESTORED: {missing}", file=sys.stderr)
        return 4
    print("  immutability triggers restored and verified")

    # 3. Reclaim the space. Deleting rows alone only frees pages inside the file.
    print("\nvacuuming (this rewrites the file; it takes a while)")
    db.execute("VACUUM")
    db.close()

    print(f"\nAFTER  ({_size(args.db):,.1f} MB)")
    after = sqlite3.connect(args.db)
    _report(after)
    after.close()
    print(f"\narchive kept at {args.archive} -- delete it yourself once you are satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
