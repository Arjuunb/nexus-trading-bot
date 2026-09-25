"""Automated backups of the bot's state — trade history is an asset.

Snapshots every SQLite database (via the sqlite3 backup API, so a copy is
consistent even mid-write) and every JSON store from the data directory,
pruning to the newest N. Runs nightly with the daily report and on demand via
POST /ops/backup.

When a master key is configured (``HUB_MASTER_KEY``, the key that also
protects stored exchange keys) each snapshot is packed and sealed into one
encrypted archive, ``backups/<stamp>.tlxb`` (services/backup_crypto.py), and
no plaintext copy is left behind. A small ``<stamp>.tlxb.json`` beside it
lists what is inside without needing the key. Without a master key snapshots
stay as plain folders, as before, and every result says ``encrypted: false``
so that is never mistaken for protection.

Restoring is a deliberate manual action: ``restore_to`` (or
``python -m services.backup restore <stamp> <dir>``) writes a snapshot's files
into a directory you choose; it never overwrites the live data directory.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.backup_crypto import BackupSealError, open_file, seal_file

KEEP = 7
ARCHIVE = ".tlxb"
_ENV = object()


def _env_master_key() -> Optional[bytes]:
    try:
        from services.key_vault import parse_master_key
        return parse_master_key(os.environ.get("HUB_MASTER_KEY"))
    except Exception:  # noqa: BLE001 -- a malformed key means "not configured"
        return None


def _resolve_key(master_key) -> Optional[bytes]:
    return _env_master_key() if master_key is _ENV else master_key


def _restore_keys(master_key) -> list[bytes]:
    """Keys a snapshot may have been sealed with: the current master key and,
    after a rotation, the previous one (HUB_MASTER_KEY_PREVIOUS), so backups
    taken before the rotation still restore."""
    if master_key is not _ENV:
        return [master_key] if master_key else []
    keys: list[bytes] = []
    for name in ("HUB_MASTER_KEY", "HUB_MASTER_KEY_PREVIOUS"):
        try:
            from services.key_vault import parse_master_key
            key = parse_master_key(os.environ.get(name))
        except Exception:  # noqa: BLE001 -- a malformed key is simply not a candidate
            key = None
        if key and key not in keys:
            keys.append(key)
    return keys


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _snapshots(root: Path) -> list[tuple[str, Path]]:
    """Every snapshot, oldest first: plain folders and sealed archives."""
    if not root.exists():
        return []
    found = [(d.name, d) for d in root.iterdir() if d.is_dir()]
    found += [(f.name[:-len(ARCHIVE)], f) for f in root.iterdir()
              if f.is_file() and f.name.endswith(ARCHIVE)]
    return sorted(found, key=lambda item: item[0])


def backup_now(data_dir: str, *, keep: int = KEEP, now: datetime = None,
               master_key=_ENV) -> dict:
    """Snapshot *.db (consistent) and *.json from ``data_dir``. Returns what was
    saved, whether it was encrypted, and what was pruned."""
    src = Path(data_dir)
    if not src.exists():
        return {"ok": False, "error": f"data dir {data_dir} does not exist"}
    key = _resolve_key(master_key)
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    root = src / "backups"
    dest = root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    errors: list[str] = []
    for f in sorted(src.iterdir()):
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

    manifest = {"created": now.isoformat(), "files": saved, "errors": errors,
                "encrypted": bool(key)}
    archive = None
    if key:
        try:
            archive = _seal_snapshot(dest, root, stamp, key, manifest)
        except Exception as e:  # noqa: BLE001 — never leave a half-sealed snapshot
            errors.append(f"encryption failed: {type(e).__name__}: {e}")
            (root / f"{stamp}{ARCHIVE}").unlink(missing_ok=True)
            manifest["encrypted"] = False
    if archive is None:
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))

    pruned = []
    snapshots = _snapshots(root)
    while len(snapshots) > max(1, keep):
        name, victim = snapshots.pop(0)
        if victim.is_dir():
            shutil.rmtree(victim, ignore_errors=True)
        else:
            victim.unlink(missing_ok=True)
            Path(str(victim) + ".json").unlink(missing_ok=True)
        pruned.append(name)

    out = {"ok": not errors, "snapshot": stamp, "files": saved, "errors": errors,
           "pruned": pruned, "encrypted": archive is not None}
    if archive is None and not key:
        out["warning"] = "Not encrypted: HUB_MASTER_KEY is not set."
    return out


def _seal_snapshot(dest: Path, root: Path, stamp: str, key: bytes, manifest: dict) -> Path:
    archive = root / f"{stamp}{ARCHIVE}"
    with tempfile.TemporaryDirectory() as td:
        tar_path = Path(td) / "snapshot.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for f in sorted(dest.iterdir()):
                tar.add(str(f), arcname=f.name)
            payload = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        seal = seal_file(tar_path, archive, key, context=stamp)
    Path(str(archive) + ".json").write_text(json.dumps({
        **manifest, "archive": archive.name, "bytes": archive.stat().st_size,
        "sha256": _sha256(archive), "master_key_id": seal["kek_id"]}, indent=1))
    shutil.rmtree(dest)
    return archive


def list_backups(data_dir: str) -> dict:
    root = Path(data_dir) / "backups"
    out = []
    for name, path in reversed(_snapshots(root)):
        if path.is_dir():
            files = [f for f in path.iterdir() if f.is_file()]
            out.append({"snapshot": name, "encrypted": False,
                        "bytes": sum(f.stat().st_size for f in files), "files": len(files)})
        else:
            try:
                meta = json.loads(Path(str(path) + ".json").read_text())
            except (OSError, ValueError):
                meta = {}
            out.append({"snapshot": name, "encrypted": True, "bytes": path.stat().st_size,
                        "files": len(meta.get("files", [])), "master_key_id": meta.get("master_key_id")})
    return {"backups": out}


def _safe_extract(tar_path: Path, dest: Path) -> list[str]:
    """Extract a snapshot tarball: flat regular files only, nothing that could
    write outside ``dest``."""
    names = []
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if not member.isfile() or "/" in name or "\\" in name or name.startswith("..") or not name:
                raise BackupSealError(f"Archive contains an unexpected entry: {name!r}")
            with tar.extractfile(member) as fin, open(dest / name, "wb") as out:
                shutil.copyfileobj(fin, out)
            names.append(name)
    return names


def restore_to(data_dir: str, snapshot: str, dest_dir: str, *, master_key=_ENV) -> dict:
    """Write one snapshot's files into ``dest_dir`` (created if needed). The
    live data directory is never a valid destination."""
    root = Path(data_dir) / "backups"
    dest = Path(dest_dir)
    if dest.resolve() == Path(data_dir).resolve():
        return {"ok": False, "error": "Refusing to restore over the live data directory; "
                                      "restore elsewhere and move files deliberately."}
    folder, archive = root / snapshot, root / f"{snapshot}{ARCHIVE}"
    dest.mkdir(parents=True, exist_ok=True)
    if folder.is_dir():
        names = []
        for f in folder.iterdir():
            if f.is_file():
                shutil.copy2(str(f), str(dest / f.name))
                names.append(f.name)
        return {"ok": True, "snapshot": snapshot, "encrypted": False, "files": sorted(names)}
    if not archive.exists():
        return {"ok": False, "error": f"snapshot {snapshot} not found"}
    keys = _restore_keys(master_key)
    if not keys:
        return {"ok": False, "error": "This snapshot is encrypted and HUB_MASTER_KEY is not set."}
    error = ""
    for key in keys:
        try:
            with tempfile.TemporaryDirectory() as td:
                tar_path = Path(td) / "snapshot.tar.gz"
                open_file(archive, tar_path, key, context=snapshot)
                names = _safe_extract(tar_path, dest)
            return {"ok": True, "snapshot": snapshot, "encrypted": True, "files": sorted(names)}
        except BackupSealError as e:
            error = str(e)
    if master_key is _ENV and len(keys) == 1:
        error += (" If the master key was rotated after this snapshot, set HUB_MASTER_KEY_PREVIOUS "
                  "to the key it was sealed with.")
    return {"ok": False, "snapshot": snapshot, "error": error}


def restore_check(data_dir: str, snapshot: str, *, master_key=_ENV) -> dict:
    """Verify a snapshot is restorable: it decrypts (if sealed) and every .db
    opens and answers a query."""
    with tempfile.TemporaryDirectory() as td:
        restored = restore_to(data_dir, snapshot, td, master_key=master_key)
        if not restored.get("ok"):
            return {"ok": False, "snapshot": snapshot,
                    "error": restored.get("error", f"snapshot {snapshot} not found")}
        checked = {}
        for f in sorted(Path(td).glob("*.db")):
            try:
                with sqlite3.connect(str(f)) as c:
                    tables = [r[0] for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")]
                checked[f.name] = {"ok": True, "tables": len(tables)}
            except Exception as e:  # noqa: BLE001
                checked[f.name] = {"ok": False, "error": str(e)}
    ok = all(v["ok"] for v in checked.values()) if checked else False
    return {"ok": ok, "snapshot": snapshot, "encrypted": restored["encrypted"], "databases": checked}


def status(data_dir: str) -> dict:
    """What the backups look like right now, for the Security settings."""
    listed = list_backups(data_dir)["backups"]
    key = _env_master_key()
    latest = listed[0] if listed else None
    return {
        "encrypting": key is not None,
        "count": len(listed), "keep": KEEP,
        "latest": latest,
        "unencrypted_kept": sum(1 for b in listed if not b["encrypted"]),
        "problem": "" if key else "HUB_MASTER_KEY is not set, so new backups are not encrypted.",
    }


def _main(argv: list[str]) -> int:
    """``python -m services.backup now``                  take a snapshot now
    ``python -m services.backup list``                 list snapshots
    ``python -m services.backup restore STAMP DIR``    write a snapshot's files into DIR"""
    import config
    data_dir = str(config.DATA_DIR)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "now":
        print(json.dumps(backup_now(data_dir), indent=1))
        return 0
    if cmd == "list":
        print(json.dumps(list_backups(data_dir), indent=1))
        return 0
    if cmd == "restore" and len(argv) == 4:
        result = restore_to(data_dir, argv[2], argv[3])
        print(json.dumps(result, indent=1))
        return 0 if result.get("ok") else 1
    print(_main.__doc__)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
