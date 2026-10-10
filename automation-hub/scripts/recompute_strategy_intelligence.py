#!/usr/bin/env python3
"""Read-only Strategy Intelligence metrics/reconciliation; never start the app."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Script execution supplies scripts/ as sys.path[0], not the Hub package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.strategy_intelligence_metrics import EvidenceCohort
from services.strategy_intelligence_projection import inspect_completeness, inspect_persisted_evidence, recompute


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile", help="Inspect legacy/persisted XRP source coverage")
    reconcile.add_argument("--ledger-db", required=True)
    reconcile.add_argument("--symbol", default="XRPUSDT")
    reconcile.add_argument("--strategy-id", default="adaptive_trend_pullback")
    reconcile.add_argument("--strategy-version", default="1.0.0")
    projection = commands.add_parser("recompute", help="Rebuild one exact immutable episode cohort")
    projection.add_argument("--cohort-json", required=True, help="JSON file with every EvidenceCohort dimension")
    projection.add_argument("--max-episodes", type=int, default=100_000)
    projection.add_argument("--ledger-db", help="Assess authoritative completeness; omitted means unverified journal-only output")
    completeness = commands.add_parser("completeness", help="Read-only authoritative event, episode and cost completeness")
    completeness.add_argument("--ledger-db", required=True)
    completeness.add_argument("--cohort-json", help="Exact cohort; omit to inspect the whole supplied local/exported store")
    completeness.add_argument("--max-records", type=int, default=100_000)
    for command in (reconcile, projection, completeness):
        command.add_argument("--journal-db", required=True)
        command.add_argument("--source-classification", required=True,
                             choices=("development", "production_export"))
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "reconcile":
            result = inspect_persisted_evidence(
                arguments.ledger_db, arguments.journal_db,
                source_classification=arguments.source_classification, symbol=arguments.symbol,
                strategy_id=arguments.strategy_id, strategy_version=arguments.strategy_version)
        elif arguments.command == "completeness":
            cohort = EvidenceCohort(**json.loads(Path(arguments.cohort_json).read_text())) if arguments.cohort_json else None
            result = inspect_completeness(arguments.ledger_db, arguments.journal_db, cohort=cohort,
                source_classification=arguments.source_classification, max_records=arguments.max_records)
        else:
            cohort = EvidenceCohort(**json.loads(Path(arguments.cohort_json).read_text()))
            result = recompute(arguments.journal_db, cohort=cohort,
                               source_classification=arguments.source_classification,
                               max_episodes=arguments.max_episodes, ledger_path=arguments.ledger_db)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
