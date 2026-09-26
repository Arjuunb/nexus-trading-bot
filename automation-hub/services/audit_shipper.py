"""Scheduled export of the audit log to storage you control.

The chain (services/audit_log.py) proves that nothing inside the log was
changed; it cannot prove that the newest entries were never cut off, and it
lives on the same disk as everything else. A copy held somewhere else fixes
both. Every ``interval_s`` the exporter sends each entry it has not sent yet,
oldest first, hashes included, to ``HUB_AUDIT_EXPORT_URL`` -- a log
collector, SIEM intake or any HTTPS endpoint that accepts JSON Lines -- and
remembers how far it got, so a failed attempt resumes from the same entry and
nothing is sent twice after a success.

Request: ``POST`` with ``Content-Type: application/x-ndjson``, one entry per
line, ``Authorization: Bearer <HUB_AUDIT_EXPORT_TOKEN>`` when a token is set,
and ``X-Audit-First-Seq`` / ``X-Audit-Last-Seq`` / ``X-Audit-Head-Hash``
headers so the receiver can check continuity (each entry's ``prev_hash`` is
the previous entry's ``hash``).

Only HTTPS is accepted (plain HTTP only to localhost, for a collector on the
same machine). Entries are already redacted before they are written, and the
status shown in the dashboard names the destination's host, never its full
URL, which may carry a credential.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

Poster = Callable[[str, bytes, dict, float], int]


def _post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- validated https URL
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_destination(url: str) -> str:
    """Empty when the URL is acceptable, else the reason it is not."""
    if not url:
        return "HUB_AUDIT_EXPORT_URL is not set."
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return ""
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return ""
    return "HUB_AUDIT_EXPORT_URL must be an https:// URL (plain http only to localhost)."


class AuditExporter:
    def __init__(self, log_factory, *, url: str, token: str = "", interval_s: float = 900.0,
                 batch: int = 500, timeout_s: float = 20.0, post: Optional[Poster] = None):
        self.log_factory = log_factory
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        self.interval_s = max(30.0, float(interval_s))
        self.batch = max(1, int(batch))
        self.timeout_s = timeout_s
        self.post = post or _post
        self.problem = check_destination(self.url)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def configured(self) -> bool:
        return not self.problem

    @property
    def destination(self) -> str:
        parsed = urlparse(self.url)
        return f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else ""

    # ----------------------------------------------------------------- run
    def export_once(self) -> dict:
        """Send everything not yet sent. Returns what happened."""
        if not self.configured:
            return {"ok": False, "sent": 0, "error": self.problem}
        log = self.log_factory()
        if log is None:
            return {"ok": False, "sent": 0, "error": "The audit log is not available."}
        with self._lock:
            cursor = int(log.get_state("export.last_seq") or 0)
            sent, error = 0, ""
            log.set_state("export.last_attempt_at", _now_iso())
            while True:
                rows = log.entries_after(cursor, limit=self.batch)
                if not rows:
                    break
                body = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows).encode()
                headers = {"Content-Type": "application/x-ndjson",
                           "User-Agent": "TradeLogX-Nexus-audit-export/1",
                           "X-Audit-First-Seq": str(rows[0]["seq"]),
                           "X-Audit-Last-Seq": str(rows[-1]["seq"]),
                           "X-Audit-Head-Hash": rows[-1]["hash"]}
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                try:
                    code = self.post(self.url, body, headers, self.timeout_s)
                except Exception as exc:  # noqa: BLE001 -- the reason is recorded, the cursor is not moved
                    error = f"{type(exc).__name__} reaching {self.destination}"
                    break
                if not 200 <= int(code) < 300:
                    error = f"{self.destination} answered HTTP {code}"
                    break
                cursor = rows[-1]["seq"]
                sent += len(rows)
                log.set_state("export.last_seq", str(cursor))
                log.set_state("export.last_success_at", _now_iso())
            log.set_state("export.last_error", error)
        return {"ok": not error, "sent": sent, "last_seq": cursor, "error": error}

    def status(self) -> dict:
        log = self.log_factory()
        head = log.head()["seq"] if log else 0
        last = int((log.get_state("export.last_seq") if log else 0) or 0)
        return {
            "configured": self.configured,
            "problem": self.problem,
            "destination": self.destination if self.configured else "",
            "interval_s": self.interval_s,
            "last_exported_seq": last,
            "head_seq": head,
            "pending": max(0, head - last),
            "last_success_at": log.get_state("export.last_success_at") if log else None,
            "last_attempt_at": log.get_state("export.last_attempt_at") if log else None,
            "last_error": (log.get_state("export.last_error") if log else "") or "",
        }

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if not self.configured or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audit-export", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.export_once()
                if result.get("error"):
                    print(f"[audit-export] {result['error']}", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 -- the loop must outlive a bad attempt
                print(f"[audit-export] attempt failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            self._stop.wait(self.interval_s)
