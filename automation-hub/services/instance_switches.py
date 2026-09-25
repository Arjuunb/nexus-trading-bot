"""Per-instance on/off switches kept in a small JSON file.

Used for owner choices that must not change anything until they are made: an
instance that has never been switched reads as off. The file sits beside the
other provider JSONs rather than as columns on ``trading_instances``, because a
new column would have to be migrated on every Supabase deployment before any
instance could be saved again. Same approach as services/instance_event_guard.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class InstanceSwitches:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory: dict = {}          # used when no path is configured
        self._cache: tuple[float, dict] | None = None

    def _read(self) -> dict:
        if not self.path:
            return self._memory
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}
        # Some switches are read on every signal: skip the parse while the
        # file is unchanged.
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            data = json.loads(self.path.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        self._cache = (mtime, data)
        return data

    def _write(self, data: dict) -> None:
        if not self.path:
            self._memory = data
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
        self._cache = None

    def row(self, instance_id: str) -> dict:
        return dict(self._read().get(instance_id) or {})

    def enabled(self, instance_id: str) -> bool:
        return bool(self.row(instance_id).get("enabled"))

    def enabled_ids(self) -> list[str]:
        return [key for key, row in self._read().items()
                if isinstance(row, dict) and row.get("enabled")]

    def set(self, instance_id: str, enabled: bool, *, by: str = "") -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            data = dict(self._read())
            previous = data.get(instance_id) or {}
            row = {"enabled": bool(enabled), "by": by, "updated_at": now,
                   # When it was last switched on; kept while it stays on.
                   "since": (previous.get("since") if previous.get("enabled") else now)
                   if enabled else None}
            data[instance_id] = row
            self._write(data)
        return dict(row)

    def forget(self, instance_id: str) -> None:
        with self._lock:
            data = dict(self._read())
            if data.pop(instance_id, None) is not None:
                self._write(data)
