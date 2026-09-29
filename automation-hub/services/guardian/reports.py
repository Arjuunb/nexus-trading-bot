"""Reporting and remote awareness (PRD §28-31; Phase 8).

Daily and weekly reports are built from what Guardian and the journal
recorded -- every number has a source, and where Guardian has no data it
says "not measured" rather than printing a zero. Reports are kept
append-only in ``guardian_reports``.

Notifications go through the platform's existing channel (Telegram, when
configured) and only when useful (§28): a HIGH or CRITICAL incident opening,
its close, and the reports. One notification per incident, however many
updates it gets (§29). Never for strategy inactivity. Every notification is
recorded as a Guardian action (§39).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from services.guardian.strategy import read_only, strategy_performance
from services.redaction import redact, scrub_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_reports (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(kind, period_start)
);
CREATE TRIGGER IF NOT EXISTS trg_grep_no_update BEFORE UPDATE ON guardian_reports
BEGIN SELECT RAISE(ABORT, 'reports are kept as issued'); END;
CREATE TRIGGER IF NOT EXISTS trg_grep_no_delete BEFORE DELETE ON guardian_reports
BEGIN SELECT RAISE(ABORT, 'reports are kept as issued'); END;
"""
_EXECUTION_FAILURES = ("order_rejected", "execution_uncertain")
_GAP_S = 120.0          # a longer silence between cycles is time Guardian did not observe


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _overlap_s(start: str, end: Optional[str], lo: datetime, hi: datetime) -> float:
    a = max(datetime.fromisoformat(start), lo)
    b = min(datetime.fromisoformat(end) if end else hi, hi)
    return max(0.0, (b - a).total_seconds())


class Reporter:
    def __init__(self, store, *, incidents, integrity=None, research=None,
                 journal_path: Optional[str] = None, notify: Optional[Callable[[str], object]] = None):
        self.store = store
        self.incidents = incidents
        self.integrity = integrity
        self.research = research
        self.journal_path = journal_path
        self.notify = notify
        with store._lock:
            store._c.executescript(_SCHEMA)
            store._c.commit()
        # Notifications start from the first time Guardian could send them:
        # its incident history is never replayed to the owner's phone.
        if self.store.meta("notify.since") is None:
            self.store.set_meta("notify.since", _iso(datetime.now(timezone.utc)))

    # ---------------------------------------------------------- sources
    def _count(self, event_type: str, lo: datetime, hi: datetime) -> int:
        with self.store._lock:
            return int(self.store._c.execute(
                "SELECT COUNT(*) FROM guardian_events WHERE event_type=? AND timestamp>=? AND timestamp<?",
                (event_type, _iso(lo), _iso(hi))).fetchone()[0])

    def _incidents(self, lo: datetime, hi: datetime) -> list[dict]:
        return [i for i in self.incidents.list(limit=500)
                if datetime.fromisoformat(i["started_at"]) < hi
                and (i["closed_at"] is None or datetime.fromisoformat(i["closed_at"]) >= lo)]

    def _trades(self, lo: datetime, hi: datetime) -> Optional[list[dict]]:
        if not self.journal_path:
            return None
        conn = read_only(self.journal_path)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT record_source, record_origin, strategy_id, strategy_version, net_pnl, realized_r, "
                "outcome, slippage, execution_latency_ms FROM trade_records WHERE status='CLOSED' "
                "AND position_closed_at>=? AND position_closed_at<? "
                "AND record_origin='FORWARD_PAPER'", (_iso(lo), _iso(hi)))]
        except Exception:  # noqa: BLE001 -- reported as not measured
            return None
        finally:
            conn.close()

    def observed(self, now: float) -> None:
        """Called every Guardian cycle: extends the current observed interval,
        or starts a new one after a gap. Coverage is what Guardian actually
        saw, so a crash or a stopped container is never counted as watched."""
        spans = [list(s) for s in (self.store.meta("coverage.spans") or [])]
        if spans and 0 <= now - spans[-1][1] <= _GAP_S:
            spans[-1][1] = now
        else:
            spans.append([now, now])
        spans = [s for s in spans if now - s[1] < 45 * 86_400]      # enough for any weekly report
        self.store.set_meta("coverage.spans", spans)

    def coverage(self, lo: datetime, hi: datetime) -> float:
        """The share of the period Guardian observed (see ``observed``)."""
        a, b = lo.timestamp(), hi.timestamp()
        seen = sum(max(0.0, min(e, b) - max(s, a)) for s, e in (self.store.meta("coverage.spans") or []))
        return round(min(1.0, seen / max(1.0, b - a)), 4)

    # ------------------------------------------------------------ daily
    def daily(self, day_end: datetime) -> dict:
        hi = day_end
        lo = hi - timedelta(days=1)
        incidents = self._incidents(lo, hi)
        serious = [i for i in incidents if i["severity"] in ("HIGH", "CRITICAL")]
        down = sum(_overlap_s(i["started_at"], i["recovered_at"] or i["closed_at"], lo, hi) for i in serious)
        observed_s = self.coverage(lo, hi) * 86_400
        components = self.store.components()

        def health(kind: str) -> Optional[str]:
            nodes = [c for c in components.values() if c.get("kind") == kind]
            if not nodes:
                return None
            ok = sum(1 for c in nodes if c.get("effective") == "HEALTHY")
            return f"{ok}/{len(nodes)} healthy"
        trades = self._trades(lo, hi)
        integrity = getattr(self.integrity, "last", None) or {}
        body = {
            "period": {"start": _iso(lo), "end": _iso(hi)},
            "guardian_coverage": self.coverage(lo, hi),
            # of the time Guardian observed: an unobserved hour is never counted as clean
            "time_without_serious_incident": (round(1 - min(down, observed_s) / observed_s, 4)
                                              if observed_s > 0 else None),
            "now": {"feeds": health("feed"), "instances": health("instance"), "labs": health("lab"),
                    "journal": (components.get("journal") or {}).get("effective"),
                    "database": (components.get("database") or {}).get("effective")},
            "trades": None if trades is None else {
                "closed": len(trades),
                "wins": sum(1 for t in trades if (t["net_pnl"] or 0) > 0),
                "losses": sum(1 for t in trades if (t["net_pnl"] or 0) < 0),
                "net_pnl": round(sum(t["net_pnl"] or 0 for t in trades), 2)},
            "setups_refused": self._count("setup_rejected", lo, hi),
            "failed_executions": sum(self._count(t, lo, hi) for t in _EXECUTION_FAILURES),
            "worker_crashes": self._count("worker_crashed", lo, hi),
            "unjournalled_trades": sum(1 for f in integrity.get("findings", []) if f["rule"] == "trade_not_journalled"),
            "integrity_checked": bool(integrity),
            "stale_data_incidents": sum(1 for i in incidents if i["kind"] == "data_failure"),
            "serious_incidents": len(serious),
            "almost_trades": self.store.count_almost_trades(since=_iso(lo)),
            "research_observations": (sum(1 for h in self.research.list() if h["created_at"] >= _iso(lo))
                                      if self.research else None),
            "guardian_actions": [a for a in self.store.actions(200)
                                 if a["at"] >= _iso(lo) and a["action"] not in ("GUARDIAN_STARTED", "GUARDIAN_STOPPED")],
        }
        return body

    @staticmethod
    def _daily_text(b: dict) -> str:
        t = b["trades"]
        lines = [f"GUARDIAN DAILY REPORT · {b['period']['start'][:10]}",
                 f"Guardian observed {b['guardian_coverage'] * 100:.1f}% of this day",
                 ("Time without a HIGH/CRITICAL incident: not measured"
                  if b["time_without_serious_incident"] is None else
                  f"Time without a HIGH/CRITICAL incident: {b['time_without_serious_incident'] * 100:.2f}%"
                  " of the time Guardian observed"),
                 f"Feeds: {b['now']['feeds'] or 'not observed'} · Instances: {b['now']['instances'] or 'none running'}"
                 f" · Labs: {b['now']['labs'] or 'not observed'}",
                 ("Trades: not measured (journal unavailable)" if t is None else
                  f"Trades: {t['closed']} closed · {t['wins']} wins · {t['losses']} losses · net {t['net_pnl']}"),
                 f"Setups refused: {b['setups_refused']} · Failed executions: {b['failed_executions']}"
                 f" · Worker crashes: {b['worker_crashes']}",
                 ("Unjournalled trades: " + (str(b["unjournalled_trades"]) if b["integrity_checked"] else "not checked")),
                 f"Stale-data incidents: {b['stale_data_incidents']} · Serious incidents: {b['serious_incidents']}",
                 f"Almost-trades: {b['almost_trades']} · Research observations: "
                 f"{b['research_observations'] if b['research_observations'] is not None else 'not measured'}",
                 f"Guardian actions: {len(b['guardian_actions'])}"]
        return "\n".join(lines)

    # ----------------------------------------------------------- weekly
    def weekly(self, week_end: datetime) -> dict:
        hi = week_end
        lo = hi - timedelta(days=7)
        incidents = self._incidents(lo, hi)
        by_kind: dict[str, int] = {}
        for i in incidents:
            by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
        trades = self._trades(lo, hi)
        perf = strategy_performance(self.journal_path) if self.journal_path else []
        latencies = []
        with self.store._lock:
            latencies = [r[0] for r in self.store._c.execute(
                "SELECT latency_ms FROM guardian_events WHERE category='strategy' AND latency_ms IS NOT NULL "
                "AND timestamp>=? AND timestamp<?", (_iso(lo), _iso(hi)))]
        rollup = self.store.strategy_rollup(since_day=lo.date().isoformat())
        reasons: dict[tuple, int] = {}
        for r in rollup:
            if r["blocker_code"] and r["decision"] in ("NO_SETUP", "REJECTED", "MISSED"):
                key = (r["source_component"], r["strategy_id"], r["blocker_code"])
                reasons[key] = reasons.get(key, 0) + int(r["count"])
        integrity = getattr(self.integrity, "last", None) or {}
        hypotheses = self.research.list() if self.research else []
        unresolved = [i for i in self.incidents.list(state="active", limit=100)]
        priorities = []
        for f in integrity.get("findings", []):
            if f["severity"] in ("HIGH", "CRITICAL"):
                priorities.append(f"Integrity: {f['meaning']} ({f['source']}) — {f['detail']}")
        counts: dict[str, int] = {}
        for i in incidents:
            counts[i["root_component"]] = counts.get(i["root_component"], 0) + 1
        for root, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if n >= 3:
                priorities.append(f"Recurring: {n} incidents rooted at {root} this week")
        for i in unresolved:
            priorities.append(f"Unresolved: incident #{i['id']} {i['title']}")
        return {
            "period": {"start": _iso(lo), "end": _iso(hi)},
            "guardian_coverage": self.coverage(lo, hi),
            "reliability": {"incidents": len(incidents), "by_kind": by_kind,
                            "serious": sum(1 for i in incidents if i["severity"] in ("HIGH", "CRITICAL"))},
            "strategy_performance": [p for p in perf if p["last_closed_at"] and p["last_closed_at"] >= _iso(lo)],
            "trades_closed": None if trades is None else len(trades),
            "execution": {"evaluation_latency_ms_p50": round(statistics.median(latencies), 1) if latencies else None,
                          "evaluations_measured": len(latencies),
                          "average_slippage": (round(statistics.mean([t["slippage"] for t in trades
                                                                      if t["slippage"] is not None]), 6)
                                               if trades and any(t["slippage"] is not None for t in trades) else None)},
            "setups_refused": self._count("setup_rejected", lo, hi),
            "worker_crashes": self._count("worker_crashed", lo, hi),
            "journal_integrity": {"checked": bool(integrity),
                                  "findings": [{k: f[k] for k in ("rule", "source", "severity", "detail")}
                                               for f in integrity.get("findings", [])][:50]},
            "recurring_rejections": [{"scope": s, "strategy_id": st, "code": c, "count": n}
                                     for (s, st, c), n in sorted(reasons.items(), key=lambda kv: -kv[1])[:10]],
            "missed_opportunity_candidates": self.store.almost_trades(since=_iso(lo), limit=20),
            "research_hypotheses": [{k: h[k] for k in ("id", "strategy_id", "strategy_version", "hypothesis",
                                                       "status", "stage")} for h in hypotheses],
            "forward_paper_experiments": [h["id"] for h in hypotheses if h["stage"] == "FORWARD_PAPER"],
            "unresolved_incidents": [{k: i[k] for k in ("id", "title", "state", "started_at")} for i in unresolved],
            "engineering_priorities": priorities or ["Nothing Guardian observed this week calls for engineering work."],
        }

    @staticmethod
    def _weekly_text(b: dict) -> str:
        r = b["reliability"]
        lines = [f"GUARDIAN WEEKLY INTELLIGENCE · {b['period']['start'][:10]} to {b['period']['end'][:10]}",
                 f"Guardian observed {b['guardian_coverage'] * 100:.1f}% of this week",
                 f"Incidents: {r['incidents']} ({r['serious']} serious) · by kind: "
                 + (", ".join(f"{k} {v}" for k, v in r["by_kind"].items()) or "none"),
                 "Trades closed: " + ("not measured" if b["trades_closed"] is None else str(b["trades_closed"])),
                 f"Journal integrity findings: {len(b['journal_integrity']['findings'])}"
                 + ("" if b["journal_integrity"]["checked"] else " (not checked)"),
                 f"Missed-opportunity candidates: {len(b['missed_opportunity_candidates'])} (research only)",
                 f"Research hypotheses: {len(b['research_hypotheses'])} · unresolved incidents: "
                 f"{len(b['unresolved_incidents'])}", "Priorities:"]
        lines += [f"- {p}" for p in b["engineering_priorities"][:8]]
        return "\n".join(lines)

    # ------------------------------------------------------------ issue
    def _issue(self, kind: str, body: dict, text: str) -> Optional[dict]:
        body, text = redact(body), scrub_text(text)           # a report never carries a secret
        with self.store._lock:
            cur = self.store._c.execute(
                "INSERT OR IGNORE INTO guardian_reports(kind,period_start,period_end,created_at,body,text) "
                "VALUES (?,?,?,?,?,?)",
                (kind, body["period"]["start"], body["period"]["end"], _iso(datetime.now(timezone.utc)),
                 json.dumps(body, default=str), text))
            self.store._c.commit()
        if cur.rowcount != 1:
            return None
        self._send(f"report:{kind}:{body['period']['start']}", text, reason=f"{kind} report issued")
        return {"kind": kind, **body}

    def _exists(self, kind: str, start: datetime) -> bool:
        with self.store._lock:
            return self.store._c.execute("SELECT 1 FROM guardian_reports WHERE kind=? AND period_start=?",
                                         (kind, _iso(start))).fetchone() is not None

    def cycle(self, *, now: float) -> list[str]:
        """Issue the report for each finished period that has none. A period
        Guardian did not observe at all gets no report: there is nothing to
        say about it."""
        issued = []
        today = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        monday = today - timedelta(days=today.weekday())
        for kind, end, span, build, text in (("daily", today, 1, self.daily, self._daily_text),
                                             ("weekly", monday, 7, self.weekly, self._weekly_text)):
            start = end - timedelta(days=span)
            if self._exists(kind, start) or self.coverage(start, end) == 0:
                continue
            body = build(end)
            if self._issue(kind, body, text(body)):
                issued.append(kind)
        return issued

    def reports(self, kind: Optional[str] = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM guardian_reports" + (" WHERE kind=?" if kind else "") + " ORDER BY seq DESC LIMIT ?"
        args: list[Any] = ([kind] if kind else []) + [max(1, min(int(limit), 100))]
        with self.store._lock:
            rows = [dict(r) for r in self.store._c.execute(sql, args)]
        for r in rows:
            r["body"] = json.loads(r["body"])
        return rows

    def view(self, kind: Optional[str] = None, limit: int = 20) -> dict:
        """What /guardian/reports shows: reports and the notifications sent."""
        return {"reports": self.reports(kind, limit),
                "notifications": [a for a in self.store.actions(200) if a["action"] == "NOTIFY"][:50]}

    # ------------------------------------------------------ notifications
    def _send(self, key: str, text: str, *, reason: str) -> bool:
        """At most once per key, recorded as a Guardian action. With no
        notifier wired (tests, tools) nothing is sent or recorded. The
        notifier returns True when delivered, None when no channel is
        configured, and False when delivery failed."""
        if self.notify is None:
            return False
        sent = list(self.store.meta("notify.sent") or [])
        if key in sent:
            return False
        sent.append(key)
        self.store.set_meta("notify.sent", sent[-2000:])          # oldest dropped first
        text = scrub_text(text)
        try:
            out = self.notify(text)
            result = "SENT" if out is True else "NO_CHANNEL" if out is None else "FAILED"
        except Exception as exc:  # noqa: BLE001 -- a failed send is recorded, never raised
            result = f"FAILED: {type(exc).__name__}"
        self.store.record_action("NOTIFY", reason=reason, policy="NOTIFY_OWNER", result=result,
                                 evidence={"key": key, "text": text[:1500]})
        return result == "SENT"

    def notify_incidents(self) -> int:
        """HIGH/CRITICAL incidents: one message when opened, one when closed.
        Updates in between are grouped into the incident, not sent (§29).
        Only incidents detected since notifications began, and a close only
        for an incident whose opening was sent."""
        if self.notify is None:
            return 0
        n = 0
        since = self.store.meta("notify.since") or ""
        sent = set(self.store.meta("notify.sent") or [])
        for i in self.incidents.list(limit=100):
            if i["severity"] not in ("HIGH", "CRITICAL") or i["detected_at"] < since:
                continue
            opened, closed = f"incident:{i['id']}:opened", f"incident:{i['id']}:closed"
            if opened in sent and (closed in sent or i["state"] != "CLOSED"):
                continue                                  # nothing new to say
            d = i["diagnosis"] or {}
            if opened not in sent and self._send(opened,
                          f"GUARDIAN · INCIDENT #{i['id']} ({i['severity']})\n{i['title']}\n"
                          f"Root cause: {d.get('root_cause')} ({d.get('confidence')})\n"
                          f"Affected: {len(i['affected']) or len(i['signals'])}\n"
                          f"Advice: {d.get('recommended_action')}", reason=f"incident #{i['id']} opened"):
                n += 1
            if i["state"] == "CLOSED" and opened in set(self.store.meta("notify.sent") or []) and self._send(
                    closed,
                    f"GUARDIAN · INCIDENT #{i['id']} CLOSED\n{i['title']}\nRecovered {i['recovered_at']}, "
                    f"verified {i['verified_at']} · {i['updates']} updates", reason=f"incident #{i['id']} closed"):
                n += 1
        return n
