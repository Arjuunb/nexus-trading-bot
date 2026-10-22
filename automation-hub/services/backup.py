"""Automated backups of the bot's state — trade history is an asset.

Snapshots every SQLite database (via the sqlite3 backup API, so a copy is
consistent even mid-write) and every JSON store from the data directory into
a timestamped folder under ``<data_dir>/backups``, pruning to the newest N.
Runs nightly with the daily report and on demand via POST /ops/backup.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from services.feature_flags import price_action_lab_enabled

KEEP = 7


def retired_pa_paths(root: Path) -> set[Path]:
    if price_action_lab_enabled():
        return set()
    retired = {Path(os.environ.get(key, str(root/name))).resolve() for key, name in (
        ('HUB_PRICE_ACTION_PAPER_DB', 'price_action_paper.db'),
        ('HUB_PRICE_ACTION_RESEARCH_DB', 'price_action_research.db'))}
    protected = {Path(os.environ.get(key, str(root/name))).resolve() for key, name in (
        ('HUB_ACCOUNT_DB', 'account.db'), ('HUB_JOURNAL_DB', 'journal.db'),
        ('HUB_LEDGER_PATH', 'ledger.db'), ('HUB_PAPER_BROKER_V2_DB', 'paper_broker_v2.db'),
        ('HUB_SMC_PAPER_DB', 'smc_strategy_paper.db'), ('HUB_MARKET_DB', 'market_data.db'),
        ('HUB_SHADOW_RESEARCH_DB', 'shadow_research.db'), ('HUB_DECISIONS_DB', 'decisions.db'))}
    if retired & protected:
        raise RuntimeError('PA_DATABASE_ALIASES_PROTECTED_STORE')
    return retired


def backup_now(data_dir: str, *, keep: int = KEEP, now: datetime = None) -> dict:
    """Snapshot *.db (consistent) and *.json from ``data_dir`` into
    backups/<UTC timestamp>/. Returns what was saved and what was pruned."""
    src = Path(data_dir)
    if not src.exists():
        return {"ok": False, "error": f"data dir {data_dir} does not exist"}
    retired = retired_pa_paths(src)
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest = src / "backups" / stamp
    dest.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    errors: list[str] = []
    for f in sorted(src.iterdir()):
        if f.resolve() in retired:
            continue
        if not f.is_file():
            continue
        try:
            if f.suffix == ".db":
                with sqlite3.connect(str(f)) as conn, \
                        sqlite3.connect(str(dest / f.name)) as out:
                    conn.backup(out)
                saved.append(f.name)
            elif f.suffix == ".json":
                shutil.copy2(str(f), str(dest / f.name))
                saved.append(f.name)
        except Exception as e:  # noqa: BLE001 — back up everything we can
            errors.append(f"{f.name}: {e}")

    pruned = []
    retained = []
    root = src / "backups"
    snapshots = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    while len(snapshots) > max(1, keep):
        victim = snapshots.pop(0)
        # Retiring the feature must not silently delete its prior financial
        # backups. Their deletion belongs to the explicit retirement plan.
        if any((victim/path.name).is_file() for path in retired):
            retained.append(victim.name)
            continue
        shutil.rmtree(victim, ignore_errors=True)
        pruned.append(victim.name)

    (dest / "manifest.json").write_text(json.dumps(
        {"created": now.isoformat(), "files": saved, "errors": errors}, indent=1))
    return {"ok": not errors, "snapshot": stamp, "files": saved,
            "errors": errors, "pruned": pruned, "retained_retired_lab_backups": retained}


def list_backups(data_dir: str) -> dict:
    root = Path(data_dir) / "backups"
    if not root.exists():
        return {"backups": []}
    out = []
    for d in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        out.append({"snapshot": d.name, "bytes": size,
                    "files": len([f for f in d.iterdir() if f.is_file()])})
    return {"backups": out}


def restore_check(data_dir: str, snapshot: str) -> dict:
    """Verify a snapshot is restorable: every .db opens and answers a query.
    (Actual restore is a deliberate manual action: copy the files back.)"""
    d = Path(data_dir) / "backups" / snapshot
    if not d.exists():
        return {"ok": False, "error": f"snapshot {snapshot} not found"}
    checked = {}
    for f in sorted(d.glob("*.db")):
        try:
            with sqlite3.connect(str(f)) as c:
                tables = [r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
            checked[f.name] = {"ok": True, "tables": len(tables)}
        except Exception as e:  # noqa: BLE001
            checked[f.name] = {"ok": False, "error": str(e)}
    ok = all(v["ok"] for v in checked.values()) if checked else False
    return {"ok": ok, "snapshot": snapshot, "databases": checked}
