"""Refuse to run the journal audit anywhere but a scratch data directory.

The audit scripts place real paper orders through the real execution code,
so pointed at the app's data directory they would write into its ledger. Every
script that writes calls require_scratch() before touching a database: the
data directory must carry the marker run_all.sh creates, and every database
path the app settings resolve must sit inside it (an HUB_*_DB variable left
over in the environment would otherwise redirect a write elsewhere).
"""
from __future__ import annotations

import os
import sys

MARKER = ".journal-audit-scratch"


def require_scratch() -> str:
    root = os.environ.get("HUB_DATA_DIR", "")
    if not root or not os.path.exists(os.path.join(root, MARKER)):
        sys.exit("journal audit: refusing to run. HUB_DATA_DIR must be a scratch directory "
                 f"created by scripts/journal_audit/run_all.sh (it holds {MARKER}).")
    from config import settings
    root = os.path.realpath(root)
    for name in ("ledger_path", "decisions_db", "cycles_db", "journal_db", "trade_records_db",
                 "smc_paper_db", "price_action_paper_db", "smc_agent_journal_db"):
        path = os.path.realpath(str(getattr(settings, name)))
        if os.path.commonpath([root, path]) != root:
            sys.exit(f"journal audit: refusing to run. settings.{name} resolves to {path}, "
                     f"outside the scratch directory {root}.")
    return root
