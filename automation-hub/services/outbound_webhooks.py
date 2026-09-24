"""Signed outbound webhooks for platform events.

Subscriptions name an HTTPS endpoint and the event types it wants. Events use
one envelope::

    {"id": "evt_dec_42", "type": "decision.rejected",
     "occurred_at": "...", "sequence": 42,
     "idempotency_key": "dec_42:rejected", "data": {...the /v1 decision...}}

Events today: ``decision.accepted`` and ``decision.rejected`` -- every
evaluation the engine records, read from the decision store, so no trading
code is involved in producing them -- plus ``webhook.test``.

Delivery is **at-least-once**: a delivery that does not get a 2xx is retried
with exponential backoff (30 s doubling, capped at an hour between attempts)
for 24 hours, then marked failed. Handlers must therefore be idempotent; the
``idempotency_key`` is stable across retries. Every attempt is recorded and
visible in the dashboard.

Each request is signed: ``Nexus-Signature: t=<unix seconds>,v1=<hex>`` where
``v1`` is HMAC-SHA256, keyed with the subscription's secret, over
``"<t>.<raw body>"``. Verify it (and that ``t`` is recent) before parsing.
The secret is shown once, when the subscription is created.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

EVENT_TYPES = ("decision.accepted", "decision.rejected")
RETRY_WINDOW_S = 24 * 3600
FIRST_BACKOFF_S = 30
MAX_BACKOFF_S = 3600
_MAX_SUBSCRIPTIONS = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
    events TEXT NOT NULL, description TEXT NOT NULL, created_at REAL NOT NULL, disabled_at REAL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id TEXT NOT NULL,
    event_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, next_attempt_at REAL NOT NULL,
    last_attempt_at REAL, last_status INTEGER, last_error TEXT NOT NULL DEFAULT '',
    delivered_at REAL,
    UNIQUE (subscription_id, event_id)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS webhook_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

Poster = Callable[[str, bytes, dict, float], int]


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def check_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme == "https" and parsed.hostname:
        return ""
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return ""
    return "Webhook URLs must be https:// (plain http only to localhost)."


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: str, body: bytes, header: str, *, tolerance_s: int = 300, now: Optional[float] = None) -> bool:
    """Reference verification -- the same check the SDKs implement."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - t) > tolerance_s:
        return False
    expected = sign(secret, body, t).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not a delivery. Followed, urllib would resend
    the POST as a bodyless GET (possibly to plain http) and a 200 from there
    would mark an event delivered that never arrived."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # validated URL
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _public_sub(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "url": row["url"], "events": row["events"].split(","),
            "description": row["description"], "created_at": _iso(row["created_at"]),
            "active": row["disabled_at"] is None, "disabled_at": _iso(row["disabled_at"])}


def _public_delivery(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "subscription_id": row["subscription_id"], "event_id": row["event_id"],
            "event_type": row["event_type"], "status": row["status"], "attempts": row["attempts"],
            "created_at": _iso(row["created_at"]), "next_attempt_at": _iso(row["next_attempt_at"])
            if row["status"] == "pending" else None, "last_attempt_at": _iso(row["last_attempt_at"]),
            "last_status": row["last_status"], "last_error": row["last_error"],
            "delivered_at": _iso(row["delivered_at"])}


class WebhookService:
    def __init__(self, path: str | Path, *, decision_source: Optional[Callable[[int, int], list[dict]]] = None,
                 latest_decision_id: Optional[Callable[[], int]] = None,
                 render: Optional[Callable[[dict], dict]] = None, post: Optional[Poster] = None,
                 clock: Callable[[], float] = time.time, interval_s: float = 15.0, timeout_s: float = 10.0):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.executescript(_SCHEMA)
            self._c.commit()
        # decision_source(after_id, limit): decisions with a larger id, oldest first.
        self.decision_source = decision_source
        self.latest_decision_id = latest_decision_id or (lambda: 0)
        self.render = render or (lambda d: d)
        self.post = post or _post
        self.clock = clock
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -------------------------------------------------------- subscriptions
    def subscribe(self, tenant: str, url: str, events, *, description: str = "") -> dict:
        problem = check_url(url)
        if problem:
            raise ValueError(problem)
        wanted = sorted({str(e) for e in (events or EVENT_TYPES)})
        unknown = [e for e in wanted if e not in EVENT_TYPES]
        if unknown:
            raise ValueError(f"Unknown event type(s): {', '.join(unknown)}. Available: {', '.join(EVENT_TYPES)}.")
        with self._lock:
            active = self._c.execute("SELECT COUNT(*) FROM subscriptions WHERE tenant=? AND disabled_at IS NULL",
                                     (tenant,)).fetchone()[0]
            if active >= _MAX_SUBSCRIPTIONS:
                raise ValueError(f"At most {_MAX_SUBSCRIPTIONS} active webhooks.")
            sub_id = f"whk_{secrets.token_hex(6)}"
            secret = f"whsec_{secrets.token_urlsafe(32)}"
            self._c.execute("INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,NULL)",
                            (sub_id, tenant, url, secret, ",".join(wanted), (description or "")[:120],
                             self.clock()))
            self._c.commit()
            if self._state("decision_cursor") is None:
                # A first subscription starts from now, not from the whole history.
                self._set_state("decision_cursor", str(int(self.latest_decision_id() or 0)))
            row = self._c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        return {**_public_sub(row), "secret": secret}

    def list(self, tenant: str) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT * FROM subscriptions WHERE tenant=? ORDER BY created_at DESC",
                                   (tenant,)).fetchall()
        return [_public_sub(r) for r in rows]

    def disable(self, tenant: str, sub_id: str) -> dict:
        with self._lock:
            row = self._c.execute("SELECT * FROM subscriptions WHERE id=? AND tenant=?", (sub_id, tenant)).fetchone()
            if row is None:
                raise KeyError(sub_id)
            if row["disabled_at"] is None:
                self._c.execute("UPDATE subscriptions SET disabled_at=? WHERE id=?", (self.clock(), sub_id))
                self._c.execute("UPDATE deliveries SET status='cancelled' WHERE subscription_id=? AND status='pending'",
                                (sub_id,))
                self._c.commit()
            row = self._c.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        return _public_sub(row)

    def deliveries(self, tenant: str, sub_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            owned = self._c.execute("SELECT 1 FROM subscriptions WHERE id=? AND tenant=?", (sub_id, tenant)).fetchone()
            if owned is None:
                raise KeyError(sub_id)
            rows = self._c.execute("SELECT * FROM deliveries WHERE subscription_id=? ORDER BY id DESC LIMIT ?",
                                   (sub_id, max(1, min(int(limit), 500)))).fetchall()
        return [_public_delivery(r) for r in rows]

    # --------------------------------------------------------------- events
    def _state(self, key: str) -> Optional[str]:
        row = self._c.execute("SELECT value FROM webhook_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_state(self, key: str, value: str) -> None:
        self._c.execute("INSERT INTO webhook_state(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def enqueue(self, event: dict, *, only: Optional[str] = None) -> int:
        """Queue one delivery of ``event`` per active subscription that wants it."""
        body = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        now, queued = self.clock(), 0
        with self._lock:
            subs = self._c.execute("SELECT * FROM subscriptions WHERE disabled_at IS NULL").fetchall()
            for sub in subs:
                if only and sub["id"] != only:
                    continue
                if not only and event["type"] not in sub["events"].split(","):
                    continue
                cur = self._c.execute(
                    "INSERT OR IGNORE INTO deliveries(subscription_id,event_id,event_type,payload,status,created_at,"
                    "next_attempt_at) VALUES (?,?,?,?, 'pending', ?, ?)",
                    (sub["id"], event["id"], event["type"], body, now, now))
                queued += cur.rowcount
            self._c.commit()
        return queued

    def poll_events(self) -> int:
        """Turn decisions recorded since the last poll into events."""
        if not self.decision_source:
            return 0
        with self._lock:
            has_subs = self._c.execute("SELECT 1 FROM subscriptions WHERE disabled_at IS NULL LIMIT 1").fetchone()
            cursor = int(self._state("decision_cursor") or 0)
        if not has_subs:
            return 0
        rows = self.decision_source(cursor, 500)
        queued = 0
        for row in sorted(rows, key=lambda r: r["id"]):
            verdict = row.get("decision")
            if verdict not in ("accepted", "rejected"):
                continue
            queued += self.enqueue({
                "id": f"evt_dec_{row['id']}", "type": f"decision.{verdict}",
                "occurred_at": row.get("ts"), "sequence": row["id"],
                "idempotency_key": f"dec_{row['id']}:{verdict}", "data": self.render(row)})
        if rows:
            with self._lock:
                self._set_state("decision_cursor", str(max(r["id"] for r in rows)))
                self._c.commit()
        return queued

    def send_test(self, tenant: str, sub_id: str) -> int:
        with self._lock:
            if self._c.execute("SELECT 1 FROM subscriptions WHERE id=? AND tenant=? AND disabled_at IS NULL",
                               (sub_id, tenant)).fetchone() is None:
                raise KeyError(sub_id)
        event_id = f"evt_test_{secrets.token_hex(6)}"
        return self.enqueue({"id": event_id, "type": "webhook.test",
                             "occurred_at": _iso(self.clock()), "sequence": 0,
                             "idempotency_key": event_id, "data": {"message": "A test event from TradeLogX Nexus."}},
                            only=sub_id)

    # ------------------------------------------------------------- delivery
    def deliver_due(self, limit: int = 50) -> dict:
        now = self.clock()
        with self._lock:
            due = self._c.execute(
                "SELECT d.*, s.url, s.secret FROM deliveries d JOIN subscriptions s ON s.id = d.subscription_id "
                "WHERE d.status='pending' AND d.next_attempt_at <= ? AND s.disabled_at IS NULL "
                "ORDER BY d.id LIMIT ?", (now, limit)).fetchall()
        sent = failed = 0
        for row in due:
            body = row["payload"].encode()
            ts = int(self.clock())
            headers = {"Content-Type": "application/json", "User-Agent": "TradeLogX-Nexus-webhooks/1",
                       "Nexus-Event-Id": row["event_id"], "Nexus-Event-Type": row["event_type"],
                       "Nexus-Signature": sign(row["secret"], body, ts)}
            try:
                code, error = int(self.post(row["url"], body, headers, self.timeout_s)), ""
            except Exception as exc:  # noqa: BLE001 -- the attempt is recorded, not raised
                code, error = 0, f"{type(exc).__name__}"
            attempts = row["attempts"] + 1
            with self._lock:
                if 200 <= code < 300:
                    self._c.execute("UPDATE deliveries SET status='delivered', attempts=?, last_attempt_at=?, "
                                    "last_status=?, last_error='', delivered_at=? WHERE id=?",
                                    (attempts, now, code, now, row["id"]))
                    sent += 1
                else:
                    error = error or f"HTTP {code}"
                    wait = min(MAX_BACKOFF_S, FIRST_BACKOFF_S * 2 ** (attempts - 1))
                    expired = now + wait > row["created_at"] + RETRY_WINDOW_S
                    self._c.execute("UPDATE deliveries SET status=?, attempts=?, last_attempt_at=?, last_status=?, "
                                    "last_error=?, next_attempt_at=? WHERE id=?",
                                    ("failed" if expired else "pending", attempts, now, code or None, error,
                                     now + wait, row["id"]))
                    failed += 1
                self._c.commit()
        return {"delivered": sent, "retrying_or_failed": failed}

    # ------------------------------------------------------------ lifecycle
    def run_once(self) -> dict:
        return {"queued": self.poll_events(), **self.deliver_due()}

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="outbound-webhooks", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[webhooks] cycle failed: {type(exc).__name__}: {exc}"[:300], file=sys.stderr, flush=True)
            self._stop.wait(self.interval_s)
