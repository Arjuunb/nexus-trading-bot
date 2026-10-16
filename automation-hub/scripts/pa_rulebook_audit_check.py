#!/usr/bin/env python3
"""Check a backtest export against what the setup review actually needs.

Given a zip, a directory or a JSON file, this reports -- per confirmation --
which required fields are present and which are missing, by name. It renders
nothing and invents nothing: a chart drawn from a record with no candles, or a
net-RR verdict with no cost figures, would be a picture of an assumption.

    python scripts/pa_rulebook_audit_check.py BTCUSDT_2025_Backtest_Complete.zip

The schema below is the one ``pa_rulebook_replay.py --audit`` already writes, so
the shortest path to a complete file is to run that replay over the same
candles rather than to convert an export by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

# (dotted path, why the review needs it)
REQUIRED = [
    ("index", "orders the setups and labels each chart"),
    ("at", "the decision timestamp"),
    ("strategy_id", "separates Setup A from Setup B statistics"),
    ("direction", "long or short"),
    ("regime", "the 1H context the setup was taken under"),
    ("verdict", "ACCEPTED or the blocker code"),
    ("zone.id", "which level was defended"),
    ("zone.lower", "zone band, drawn on the chart"),
    ("zone.upper", "zone band, drawn on the chart"),
    ("setup_atr15", "denominator for every ATR-expressed threshold"),
    ("rejection.t", "highlights the rejection candle"),
    ("rejection.o", "rejection candle body"),
    ("rejection.h", "rejection high -- the confirmation must clear it"),
    ("rejection.l", "rejection low -- the stop and cancellation reference"),
    ("rejection.c", "rejection close"),
    ("confirmation.t", "which 5M candle confirmed"),
    ("confirmation.c", "the executable entry estimate"),
    ("confirm_slot", "whether confirmation arrived late in the window"),
    ("candles.15m", "the historical candles the chart is drawn from"),
    ("plan.entry_bound", "entry line"),
    ("plan.stop", "stop line"),
    ("plan.stop_distance", "risk leg of net RR"),
    ("plan.stop_distance_atr", "tests the 0.30-2.50 ATR band"),
    ("plan.costs_loss", "cost on the loss path"),
    ("plan.blocker", "the refusal code, or null when accepted"),
]

# Required only once a target existed -- a TARGET_UNAVAILABLE setup has none,
# and demanding them would report a correct record as broken.
REQUIRED_WITH_TARGET = [
    ("plan.target", "target line"),
    ("plan.net_rr", "the ratio the gate compared"),
    ("plan.costs_win", "cost on the win path"),
    ("plan.evidence.target_room", "distance from entry to the capping level"),
    ("plan.evidence.required_room_for_min_rr", "room the 2.5R gate needed"),
    ("plan.evidence.target_zone_id", "which level capped the target"),
]

EXPECTED_OUTCOMES = {"NET_RR_TOO_LOW": 17, "STOP_DISTANCE_INVALID": 3,
                     "TARGET_UNAVAILABLE": 2, "ACCEPTED": 3}


#: Paths whose value may legitimately be null. ``plan.blocker`` is null on an
#: accepted setup, and reporting that as missing would make the checker cry
#: wolf on exactly the records that are most complete.
NULLABLE = {"plan.blocker"}


def _dig(record: dict, path: str):
    node = record
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _load(source: Path) -> tuple[dict | None, list[str]]:
    """Find an audit payload in a zip, a directory or a file."""
    notes: list[str] = []
    if not source.exists():
        return None, [f"{source} does not exist"]

    candidates: list[tuple[str, bytes]] = []
    if source.is_dir():
        for path in sorted(source.rglob("*.json")):
            candidates.append((str(path), path.read_bytes()))
    elif source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            notes.append(f"archive contains {len(names)} entries")
            for name in names:
                if name.lower().endswith(".json"):
                    candidates.append((name, archive.read(name)))
            if not candidates:
                notes.append("no .json entry found; entries were: "
                             + ", ".join(names[:25]))
    else:
        candidates.append((str(source), source.read_bytes()))

    for name, blob in candidates:
        try:
            payload = json.loads(blob)
        except ValueError:
            continue
        if isinstance(payload, dict) and "confirmations" in payload:
            notes.append(f"using {name}")
            return payload, notes
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            notes.append(f"using {name} (bare list of {len(payload)} records)")
            return {"meta": {}, "summary": {}, "confirmations": payload}, notes
    notes.append("no file with a 'confirmations' array was found")
    return None, notes


def check(source: Path) -> int:
    payload, notes = _load(source)
    for note in notes:
        print(f"  {note}")
    if payload is None:
        print("\nFAIL: nothing to check. The review needs one JSON payload with a "
              "'confirmations' array; see the schema in this file's REQUIRED list.")
        return 2

    records = payload.get("confirmations", [])
    print(f"\n  {len(records)} confirmations found")

    missing_counts: dict[str, int] = {}
    for record in records:
        has_target, _ = _dig(record, "plan.target")
        checks = REQUIRED + (REQUIRED_WITH_TARGET if has_target is not None else [])
        for path, _why in checks:
            value, present = _dig(record, path)
            absent = not present or (value is None and path not in NULLABLE)
            if absent or (path == "candles.15m" and not value):
                missing_counts[path] = missing_counts.get(path, 0) + 1

    if missing_counts:
        print("\n  MISSING FIELDS (field -- records affected -- why it is needed):")
        why = dict(REQUIRED + REQUIRED_WITH_TARGET)
        for path, count in sorted(missing_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {path:<42} {count:>4}/{len(records)}  {why.get(path, '')}")
    else:
        print("  every required field is present")

    outcomes: dict[str, int] = {}
    for record in records:
        key = record.get("verdict") or "UNKNOWN"
        outcomes[key] = outcomes.get(key, 0) + 1
    print("\n  OUTCOME RECONCILIATION (found vs expected):")
    for key in sorted(set(outcomes) | set(EXPECTED_OUTCOMES)):
        found, want = outcomes.get(key, 0), EXPECTED_OUTCOMES.get(key)
        flag = "" if want is None or found == want else "   <-- mismatch"
        print(f"    {key:<24} found {found:>3}"
              + (f"   expected {want:>3}{flag}" if want is not None else ""))
    total, expected_total = len(records), sum(EXPECTED_OUTCOMES.values())
    print(f"    {'TOTAL':<24} found {total:>3}   expected {expected_total:>3}"
          + ("" if total == expected_total else "   <-- mismatch"))

    return 0 if not missing_counts and outcomes == EXPECTED_OUTCOMES else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="zip, directory or JSON export to check")
    args = parser.parse_args()
    print(f"Checking {args.source} against the setup-review schema\n")
    return check(Path(args.source))


if __name__ == "__main__":
    raise SystemExit(main())
