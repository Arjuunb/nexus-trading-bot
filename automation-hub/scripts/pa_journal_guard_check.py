#!/usr/bin/env python3
"""Prove the Price Action journal is still append-only, by trying to break it.

pa_journal_revisions and pa_journal_entries carry BEFORE UPDATE and BEFORE
DELETE triggers so nothing can quietly rewrite what the bot did. After
pa_journal_prune.py lifts those triggers and puts them back, something has to
confirm they are actually back.

Listing them in sqlite_master is necessary and not sufficient: a trigger can be
present and still not fire if it was created against the wrong table or event.
So this script performs the real operation inside a transaction it always rolls
back, and requires it to be refused.

The refusal has to come from a statement that would otherwise have done
something. An earlier version of this check used ``DELETE ... WHERE 1=0``,
which matches no rows -- SQLite never fires a row-level BEFORE DELETE trigger
when no row is deleted, so the delete "succeeded", and the check reported the
guard was off while it was perfectly intact. Every probe below therefore
targets a row that exists, and the script fails if it cannot find one to target.

Nothing is destroyed: each probe runs in its own transaction, the transaction is
rolled back whether it raised or not, and the row counts are compared at the end.

    python scripts/pa_journal_guard_check.py
    python scripts/pa_journal_guard_check.py --db /var/lib/tradexa/price_action_paper.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB = "/var/lib/tradexa/price_action_paper.db"

#: Every (table, operation) pair the immutability triggers are meant to cover.
GUARDED = (
    ("pa_journal_revisions", "DELETE"),
    ("pa_journal_revisions", "UPDATE"),
    ("pa_journal_entries", "DELETE"),
    ("pa_journal_entries", "UPDATE"),
)

REQUIRED_TRIGGERS = ("pa_journal_entries_no_update", "pa_journal_entries_no_delete",
                     "pa_journal_revisions_no_update", "pa_journal_revisions_no_delete")


def _probe(db: sqlite3.Connection, table: str, operation: str) -> tuple[bool, str]:
    """Attempt the forbidden operation on a real row, then roll it back."""
    row = db.execute(f"SELECT id FROM {table} LIMIT 1").fetchone()
    if row is None:
        return False, f"{table} is empty -- nothing real to test the guard against"
    statement = (f"DELETE FROM {table} WHERE id=?" if operation == "DELETE"
                 else f"UPDATE {table} SET created_at='probe' WHERE id=?")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(statement, (row[0],))
    except sqlite3.IntegrityError as exc:
        db.execute("ROLLBACK")
        return True, str(exc)
    except sqlite3.OperationalError as exc:
        db.execute("ROLLBACK")
        # A read-only database or a busy journal is not a verdict either way.
        return False, f"could not test: {exc}"
    db.execute("ROLLBACK")
    return False, f"{operation} on a real row was ACCEPTED"


def check(db_path: str, out=sys.stdout) -> int:
    db = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    try:
        present = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'pa_journal_%'")}
        missing = [name for name in REQUIRED_TRIGGERS if name not in present]
        before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in {t for t, _ in GUARDED}}

        print(f"database   {db_path}", file=out)
        print(f"triggers   {len(present)} present"
              + (f", MISSING {missing}" if missing else ", none missing"), file=out)
        for table, count in sorted(before.items()):
            print(f"  {table:<22} {count} rows", file=out)
        print(file=out)

        failures = []
        for table, operation in GUARDED:
            held, detail = _probe(db, table, operation)
            mark = "refused " if held else "NOT GUARDED"
            print(f"  {mark}  {operation:<6} {table:<22} {detail}", file=out)
            if not held:
                failures.append(f"{operation} {table}: {detail}")

        after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in before}
        print(file=out)
        if after != before:
            print(f"ROW COUNTS CHANGED {before} -> {after}", file=out)
            return 3
        print("row counts unchanged -- every probe was rolled back", file=out)
        if missing or failures:
            print("\nGUARD IS OFF. The journal is no longer append-only:", file=out)
            for line in [f"trigger missing: {m}" for m in missing] + failures:
                print(f"  {line}", file=out)
            return 1
        print("GUARD HOLDS -- the journal refuses every update and delete", file=out)
        return 0
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DB)
    args = parser.parse_args(argv)
    if not Path(args.db).exists():
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2
    return check(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
