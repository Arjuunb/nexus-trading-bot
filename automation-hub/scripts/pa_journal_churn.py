#!/usr/bin/env python3
"""Name the fields that are still writing Price Action journal revisions.

The journal appends a revision whenever the material projection of a setup's
record changes. That is correct behaviour for a fill or an exit and pathological
for a number that drifts on its own: each revision is a full ~9.5 KB snapshot,
so one restless float can write hundreds of megabytes of nothing.

Excluding ``bars_in_trade``/``bars_to_entry`` removed one such field. It did not
remove all of them -- the table kept growing afterwards -- and guessing which
one is next is how two earlier diagnoses in this repo turned out to be wrong.
So this script does not guess. It walks every consecutive pair of revisions in
the journal, applies the *current* material projection to both, and reports:

  * pairs the deployed code would no longer write at all (the earlier fix,
    measured rather than assumed), and
  * for every pair it would still write, exactly which field paths differ --
    ranked by how often each one is the ONLY thing that changed, which is the
    only evidence that convicts a field rather than implicating it.

Read-only. The database is opened ``mode=ro``; this script cannot modify,
prune or vacuum anything, and it is safe to run against a live journal.

    python scripts/pa_journal_churn.py
    python scripts/pa_journal_churn.py --db /var/lib/tradexa/price_action_paper.db --top 20
    python scripts/pa_journal_churn.py --since 2026-09-01 --samples 3
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = "/var/lib/tradexa/price_action_paper.db"

#: Revisions that record a lifecycle event are supposed to exist. Only the
#: catch-all reason is churn, so that is what is analysed by default.
CHURN_REASON = "MATERIAL_EVIDENCE_CHANGED"


def _flatten(value: object, prefix: str = "") -> dict:
    """Dotted leaf paths. Lists are leaves: a changed list is one fact, not n."""
    if isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: json.dumps(value, sort_keys=True, default=str)}


def _describe_list_change(before: str, after: str) -> str | None:
    """Say whether a list-valued field grew or was rewritten in place.

    This is the whole question for a field like state_transitions. A row
    appended is a real event and belongs in a new revision; the same row
    written again with different values is a record of the past being restamped
    with the present, which is a defect. Truncating the JSON shows neither, and
    the two look identical at 34 characters.
    """
    try:
        old_rows, new_rows = json.loads(before), json.loads(after)
    except (TypeError, ValueError):
        return None
    if not isinstance(old_rows, list) or not isinstance(new_rows, list):
        return None
    if len(new_rows) != len(old_rows):
        return (f"{len(old_rows)} -> {len(new_rows)} rows "
                f"({'appended' if len(new_rows) > len(old_rows) else 'removed'} "
                f"{abs(len(new_rows) - len(old_rows))}) -- a real change")
    rewrites = []
    for index, (old_row, new_row) in enumerate(zip(old_rows, new_rows)):
        if not isinstance(old_row, dict) or not isinstance(new_row, dict):
            if old_row != new_row:
                rewrites.append(f"row {index} replaced")
            continue
        for key in sorted(set(old_row) | set(new_row)):
            if old_row.get(key) != new_row.get(key):
                rewrites.append(
                    f"row {index}.{key} {_short(json.dumps(old_row.get(key), default=str), 22)}"
                    f" -> {_short(json.dumps(new_row.get(key), default=str), 22)}")
    if not rewrites:
        return f"{len(old_rows)} rows, identical"
    return (f"{len(old_rows)} rows, SAME LENGTH -- rewritten in place: "
            + "; ".join(rewrites[:4])
            + (f" (+{len(rewrites) - 4} more)" if len(rewrites) > 4 else ""))


def _short(text: str, width: int = 34) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[:width - 1] + "…"


def analyse(db_path: str, *, reason: str | None, since: str | None,
            top: int, samples: int, out) -> dict:
    from services.price_action_governance import PriceActionJournalStore

    project = PriceActionJournalStore._material_projection
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    where, params = [], []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.execute(
        "SELECT journal_id,revision_no,reason_code,created_at,payload_json "
        f"FROM pa_journal_revisions{clause} ORDER BY journal_id, revision_no",
        params)

    reasons: Counter = Counter()
    changed_in: Counter = Counter()          # pairs in which this path differs
    sole_cause: Counter = Counter()          # pairs in which it is the ONLY one
    sole_bytes: Counter = Counter()
    examples: dict = defaultdict(list)
    combos: Counter = Counter()

    scanned = pairs = suppressed = analysed = 0
    suppressed_bytes = still_bytes = 0
    current_journal = None
    previous = None

    for row in rows:
        scanned += 1
        reasons[row["reason_code"]] += 1
        payload = json.loads(row["payload_json"])
        size = len(row["payload_json"])
        if row["journal_id"] != current_journal:
            current_journal, previous = row["journal_id"], payload
            continue
        prior, previous = previous, payload
        if reason is not None and row["reason_code"] != reason:
            continue
        pairs += 1

        before, after = _flatten(project(prior)), _flatten(project(payload))
        diff = sorted(k for k in set(before) | set(after)
                      if before.get(k) != after.get(k))
        if not diff:
            # The deployed projection makes these two revisions identical, so
            # this pair is a revision the current code would never write.
            suppressed += 1
            suppressed_bytes += size
            continue
        analysed += 1
        still_bytes += size
        combos[" + ".join(diff)] += 1
        for key in diff:
            changed_in[key] += 1
        if len(diff) == 1:
            key = diff[0]
            sole_cause[key] += 1
            sole_bytes[key] += size
            if len(examples[key]) < samples:
                examples[key].append(
                    (row["created_at"], before.get(key, "<absent>"),
                     after.get(key, "<absent>")))

    db.close()

    def line(text: str = "") -> None:
        print(text, file=out)

    line(f"Price Action journal churn -- READ ONLY")
    line(f"  database   {db_path}")
    line(f"  revisions  {scanned}"
         + (f"  (created_at >= {since})" if since else ""))
    line(f"  reason     {reason or 'ALL'}")
    line()
    line("Revisions by reason code")
    for code, count in reasons.most_common():
        line(f"  {count:>8}  {code}")
    line()
    line(f"Consecutive pairs examined          {pairs}")
    line(f"  already suppressed by this build  {suppressed:>8}"
         f"   {suppressed_bytes / 1e6:>9.1f} MB")
    line(f"  still written by this build       {analysed:>8}"
         f"   {still_bytes / 1e6:>9.1f} MB")
    line()
    # Read once, misread once: these two lines replay the *current material
    # projection* over rows already in the table. A fix that changes what
    # capture() writes -- preserving a recorded transition instead of
    # rebuilding it, say -- never appears here, because the stored payloads
    # still differ and the projection does not touch that field. Those are
    # measured forward, on revisions written after the deploy.
    line("  Both lines replay the current projection over stored rows, so a fix")
    line("  to what capture() writes cannot show here. Measure those forward:")
    line("    pa_journal_churn.py --since <deploy timestamp>")
    line()

    if not analysed:
        line("No pair survives the current material projection: every"
             f" {reason or 'analysed'} revision in this window was written by a"
             " field the deployed code now excludes.")
        return {"scanned": scanned, "pairs": pairs, "suppressed": suppressed,
                "analysed": analysed, "sole_cause": dict(sole_cause),
                "changed_in": dict(changed_in)}

    line("Sole cause -- the only field that differed in the pair")
    line(f"  {'revisions':>9} {'bytes':>10}  field")
    for key, count in sole_cause.most_common(top):
        line(f"  {count:>9} {sole_bytes[key] / 1e6:>8.1f}MB  {key}")
    if not sole_cause:
        line("  (none: every remaining revision changed two or more fields"
             " at once, so no single field can be convicted)")
    line()
    line("Appeared in a differing pair, alone or with others")
    line(f"  {'revisions':>9}  {'share':>6}  field")
    for key, count in changed_in.most_common(top):
        line(f"  {count:>9}  {100 * count / analysed:>5.1f}%  {key}")
    line()
    line("Most common change sets")
    for combo, count in combos.most_common(min(top, 10)):
        line(f"  {count:>9}  {_short(combo, 96)}")
    if examples:
        line()
        line("Examples of a sole-cause change")
        for key, count in sole_cause.most_common(min(top, 5)):
            line(f"  {key}")
            for at, before, after in examples[key]:
                described = _describe_list_change(before, after)
                if described:
                    line(f"      {at}  {described}")
                else:
                    line(f"      {at}  {_short(before)}  ->  {_short(after)}")
    return {"scanned": scanned, "pairs": pairs, "suppressed": suppressed,
            "analysed": analysed, "sole_cause": dict(sole_cause),
            "changed_in": dict(changed_in)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB)
    parser.add_argument("--reason", default=CHURN_REASON,
                        help=f"reason_code to analyse, or ALL (default {CHURN_REASON})")
    parser.add_argument("--since", help="only revisions created at or after this ISO timestamp")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--json", dest="as_json", help="also write the totals here")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2
    reason = None if str(args.reason).upper() == "ALL" else args.reason
    totals = analyse(args.db, reason=reason, since=args.since, top=args.top,
                     samples=args.samples, out=sys.stdout)
    if args.as_json:
        Path(args.as_json).write_text(json.dumps(totals, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
