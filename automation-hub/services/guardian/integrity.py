"""Execution integrity, journal completeness and the global risk observer
(PRD §23, §24, §25; Phase 4).

Every stage of a trade should have its counterpart at the next stage:

    intent -> fill -> position and trade -> journal record

Guardian reads each stage's own records through read-only connections and
reports where one has no counterpart. It never repairs anything: a finding
is evidence for the owner (and, for journal gaps, the journal recorder
repairs them on its own next pass if the ledger is intact).

The global risk observer adds up open exposure across every instance and
lab from the positions themselves (PRD §24). Paper and live are never mixed:
an account is counted as paper only when its own record says PAPER, anything
else is listed separately, and a non-paper position while live routing is
locked is a CRITICAL finding.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from services.guardian.strategy import read_only

#: rule -> (severity, what it means)
RULES = {
    "fill_without_position": ("HIGH", "a fill whose position or trade record does not exist"),
    "position_trade_mismatch": ("HIGH", "a position and its trade disagree about whether it is open"),
    "trade_not_journalled": ("HIGH", "a completed trade with no journal record"),
    "open_trade_not_journalled": ("WARNING", "an open trade with no journal record"),
    "intent_unresolved": ("WARNING", "an order intent still pending or claimed long after it arrived"),
    "lab_position_not_journalled": ("WARNING", "an open lab position with no open journal record"),
    "journal_incomplete": ("WATCH", "closed journal records missing most core facts (graded MINIMAL)"),
    "live_exposure_while_locked": ("CRITICAL", "a non-paper position while live routing is locked"),
}
_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH")
#: Every account label the platform's paper broker writes (execution/
#: paper_broker_v2.py default, and the two labs' own labels). An account
#: labelled anything else is not added to paper totals.
PAPER_ACCOUNT_TYPES = frozenset({"PAPER", "PA_LAB", "SMC_LAB"})


def _side(value) -> str:
    v = str(value or "").lower()
    return "long" if v in ("buy", "long") else "short" if v in ("sell", "short") else v or "?"


def cluster(symbol: str) -> str:
    """Correlation cluster: crypto majors move together, so they are one
    bet (the same rule as services/signal_pipeline.py::_cluster)."""
    s = (symbol or "").upper().replace("/", "").replace("-", "")
    return "crypto" if s.endswith(_CRYPTO_QUOTES) else "other"


def _age_s(stamp: Optional[str], now: float) -> Optional[float]:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - dt.timestamp()


def _rows(conn: sqlite3.Connection, sql: str, args: Iterable = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, tuple(args))]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise


class Source:
    """One account's records. ``kind`` is ``ledger`` (the instance ledger
    schema) or ``lab_broker`` (the labs' paper broker)."""

    def __init__(self, name: str, path: Optional[str], *, kind: str, journal_source: Optional[str] = None):
        self.name, self.path, self.kind = name, path, kind
        self.journal_source = journal_source      # the journal's record_source for a lab


class IntegrityMonitor:
    def __init__(self, store, sources: Iterable[Source], *, journal_path: Optional[str] = None,
                 live_status: Optional[Callable[[], dict]] = None, grace_s: float = 900.0,
                 intent_timeout_s: float = 1800.0, every: int = 4):
        self.store = store
        self.sources = list(sources)
        self.journal_path = journal_path
        self.live_status = live_status
        self.grace_s = float(grace_s)
        self.intent_timeout_s = float(intent_timeout_s)
        self.every = max(1, int(every))
        self._n = 0
        self.last: Optional[dict] = None

    # ------------------------------------------------------------ journal
    def _journal(self) -> dict:
        if not self.journal_path:
            return {"trade_ids": None, "open_by_source": {}, "incomplete": []}
        conn = read_only(self.journal_path)
        try:
            rows = _rows(conn, "SELECT journal_record_id, record_source, status, trade_id, symbol, "
                               "data_completeness, missing_json, position_closed_at FROM trade_records")
        finally:
            conn.close()
        open_by: dict[str, set[str]] = {}
        for r in rows:
            if r["status"] == "OPEN":
                open_by.setdefault(r["record_source"], set()).add(str(r["symbol"] or "").upper())
        incomplete = [{"journal_record_id": r["journal_record_id"], "record_source": r["record_source"],
                       "completeness": r["data_completeness"], "missing": r["missing_json"]}
                      # The journal's own grade: MINIMAL means three or more core facts
                      # are missing. PARTIAL is ordinary (a replay has no quote evidence).
                      for r in rows if r["status"] == "CLOSED" and r["data_completeness"] == "MINIMAL"]
        return {"trade_ids": {str(r["trade_id"]) for r in rows if r["trade_id"]},
                "open_by_source": open_by, "incomplete": incomplete}

    # ------------------------------------------------------------- checks
    def _ledger(self, source: Source, journal: dict, now: float, findings: list, exposure: list) -> dict:
        conn = read_only(source.path)
        try:
            trades = _rows(conn, "SELECT * FROM paper_trades")
            positions = {r["id"]: r for r in _rows(conn, "SELECT id, status, symbol FROM positions")}
            execs = _rows(conn, "SELECT * FROM paper_executions")
            intents = _rows(conn, "SELECT id, alert_id, symbol, status, received_at, instance_id "
                                  "FROM webhook_events WHERE status IN ('pending','claimed')")
        finally:
            conn.close()
        by_id = {r["id"]: r for r in trades}
        legs = {e["trade_id"] for e in execs if e["action"] == "REDUCE"}
        pos_of_trade = {e["trade_id"]: e["position_id"] for e in execs if e["action"] in ("OPEN", "REDUCE")}

        def add(rule, item, detail):
            findings.append({"rule": rule, "source": source.name, "item": str(item), "detail": detail})

        for e in execs:
            if e["action"] in ("OPEN", "REDUCE") and (e["trade_id"] not in by_id or e["position_id"] not in positions):
                add("fill_without_position", e["execution_id"],
                    f"{e['action']} fill {e['execution_id']}: trade {'found' if e['trade_id'] in by_id else 'missing'},"
                    f" position {'found' if e['position_id'] in positions else 'missing'}")
        for tid, t in by_id.items():
            pid = pos_of_trade.get(tid)
            if pid is None or tid in legs:
                continue
            pos = positions.get(pid)
            if pos is not None and (pos["status"] == "open") != (t["status"] == "open"):
                add("position_trade_mismatch", tid, f"trade {tid} is {t['status']} but position {pid} is {pos['status']}")
        if journal["trade_ids"] is not None:
            for tid, t in by_id.items():
                if tid in legs or tid in journal["trade_ids"]:
                    continue
                if t["status"] == "closed" and (_age_s(t["closed_at"], now) or 0) > self.grace_s:
                    add("trade_not_journalled", tid, f"{t['symbol']} {t['side']} closed {t['closed_at']}; "
                                                     "no journal record")
                elif t["status"] == "open" and (_age_s(t["opened_at"], now) or 0) > self.grace_s:
                    add("open_trade_not_journalled", tid, f"{t['symbol']} {t['side']} open since {t['opened_at']}; "
                                                          "no journal record")
        for i in intents:
            age = _age_s(i["received_at"], now)
            if age is not None and age > self.intent_timeout_s:
                add("intent_unresolved", i["id"], f"{i['symbol']} intent {i['alert_id']} still {i['status']} "
                                                  f"after {int(age // 60)} min")
        for t in trades:
            if t["status"] != "open":
                continue
            size, entry, stop = (float(t[k]) if t[k] is not None else None for k in ("size", "entry", "stop"))
            exposure.append({"account": source.name, "account_type": "PAPER", "paper": True, "symbol": t["symbol"],
                             "side": _side(t["side"]), "size": size, "entry": entry, "stop": stop,
                             "notional": round(entry * size, 2) if entry and size else None,
                             "risk": round(abs(entry - stop) * size, 2) if entry and stop and size else None,
                             "instance_id": t.get("instance_id") or None, "strategy_id": t.get("strategy_id") or None,
                             "opened_at": t["opened_at"]})
        return {"trades": len(trades), "open": sum(1 for t in trades if t["status"] == "open"), "fills": len(execs)}

    def _lab(self, source: Source, journal: dict, now: float, findings: list, exposure: list) -> dict:
        conn = read_only(source.path)
        try:
            account = (_rows(conn, "SELECT account_id, account_type FROM v2_account WHERE id=1") or [{}])[0]
            positions = _rows(conn, "SELECT * FROM v2_positions")
            stops = _rows(conn, "SELECT symbol, stop_price FROM v2_orders WHERE reduce_only=1 AND stop_price IS NOT "
                                "NULL AND status IN ('OPEN','PENDING','ACCEPTED','NEW','PARTIALLY_FILLED')")
        finally:
            conn.close()
        account_type = str(account.get("account_type") or "PAPER").upper()
        stop_of = {r["symbol"]: float(r["stop_price"]) for r in stops}
        journal_open = journal["open_by_source"].get(source.journal_source or "", set())
        for p in positions:
            stop = p.get("stop_loss") if p.get("stop_loss") is not None else stop_of.get(p["symbol"])
            size, entry = float(p["size"]), float(p["entry_price"])
            exposure.append({"account": source.name, "account_type": account_type,
                             "paper": account_type in PAPER_ACCOUNT_TYPES, "symbol": p["symbol"],
                             "side": _side(p["side"]), "size": size, "entry": entry,
                             "stop": float(stop) if stop is not None else None,
                             "notional": round(entry * size, 2),
                             "risk": round(abs(entry - float(stop)) * size, 2) if stop is not None else None,
                             "instance_id": None, "strategy_id": None, "opened_at": p["opened_at"]})
            if (journal["trade_ids"] is not None and source.journal_source
                    and str(p["symbol"]).upper() not in journal_open
                    and (_age_s(p["opened_at"], now) or 0) > self.grace_s):
                findings.append({"rule": "lab_position_not_journalled", "source": source.name, "item": p["symbol"],
                                 "detail": f"{p['symbol']} {p['side']} open since {p['opened_at']}; "
                                           "no open journal record"})
        return {"account_type": account_type, "open": len(positions)}

    # --------------------------------------------------------- exposure
    def _exposure(self, rows: list[dict], findings: list) -> dict:
        live = self.live_status() if self.live_status else {"locked": None}
        paper = [r for r in rows if r["paper"]]
        other = [r for r in rows if not r["paper"]]
        if other and live.get("locked"):
            for r in other:
                findings.append({"rule": "live_exposure_while_locked", "source": r["account"], "item": r["symbol"],
                                 "detail": f"{r['account']} ({r['account_type']}) holds {r['symbol']} "
                                           "while live routing is locked"})

        def totals(items: list[dict]) -> dict:
            by: dict[tuple, dict] = {}
            for r in items:
                key = (r["symbol"], r["side"])
                agg = by.setdefault(key, {"symbol": r["symbol"], "side": r["side"], "positions": 0,
                                          "notional": 0.0, "risk": 0.0, "risk_unknown": 0, "accounts": set()})
                agg["positions"] += 1
                agg["notional"] += r["notional"] or 0.0
                if r["risk"] is None:
                    agg["risk_unknown"] += 1
                else:
                    agg["risk"] += r["risk"]
                agg["accounts"].add(r["account"])
            out = sorted(({**a, "accounts": sorted(a["accounts"]), "notional": round(a["notional"], 2),
                           "risk": round(a["risk"], 2)} for a in by.values()),
                         key=lambda a: -a["notional"])
            clusters: dict[str, dict] = {}
            for r in items:
                c = clusters.setdefault(cluster(r["symbol"]), {"long": 0.0, "short": 0.0, "positions": 0})
                c[r["side"] if r["side"] in ("long", "short") else "long"] += r["notional"] or 0.0
                c["positions"] += 1
            for c in clusters.values():
                c["net"] = round(c["long"] - c["short"], 2)
                c["long"], c["short"] = round(c["long"], 2), round(c["short"], 2)
            return {"positions": len(items), "notional": round(sum(r["notional"] or 0 for r in items), 2),
                    "risk": round(sum(r["risk"] or 0 for r in items), 2),
                    "risk_unknown": sum(1 for r in items if r["risk"] is None),
                    "by_symbol": out, "by_cluster": clusters}

        return {"paper": totals(paper), "live": {**totals(other), "routing_locked": live.get("locked")},
                "positions": rows, "note": "Paper and live are never added together. Risk is entry-to-stop "
                                           "distance times size; a position with no recorded stop counts "
                                           "as unknown risk, never as zero."}

    # ------------------------------------------------------------- cycle
    def run(self, *, now: float) -> dict:
        findings: list[dict] = []
        exposure: list[dict] = []
        errors: dict[str, str] = {}
        sources: dict[str, dict] = {}
        try:
            journal = self._journal()
        except Exception as exc:  # noqa: BLE001 -- say it could not be read
            journal = {"trade_ids": None, "open_by_source": {}, "incomplete": []}
            errors["journal"] = f"{type(exc).__name__}: {exc}"[:300]
        for source in self.sources:
            if not source.path:
                continue
            try:
                check = self._ledger if source.kind == "ledger" else self._lab
                sources[source.name] = check(source, journal, now, findings, exposure)
            except Exception as exc:  # noqa: BLE001 -- one account cannot hide the others
                errors[source.name] = f"{type(exc).__name__}: {exc}"[:300]
        if journal["incomplete"]:
            findings.append({"rule": "journal_incomplete", "source": "journal", "item": "closed-records",
                             "detail": f"{len(journal['incomplete'])} closed journal records are graded MINIMAL",
                             "records": journal["incomplete"][:20]})
        exposure_view = self._exposure(exposure, findings)
        for f in findings:
            f["severity"], f["meaning"] = RULES[f["rule"]]
        report = {"at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "findings": findings,
                  "sources": sources, "errors": errors, "journal_checked": journal["trade_ids"] is not None,
                  "exposure": exposure_view}
        self.last = report
        return report

    def cycle(self, nodes, *, now: float, publish: Callable[..., None]) -> Optional[dict]:
        """Every ``every`` cycles: reconcile, then report each rule once when
        it starts failing and once when it is clear again."""
        self._n += 1
        if self._n % self.every != 1 and self.every > 1 and self.last is not None:
            return None
        report = self.run(now=now)
        before = self.store.meta("integrity.failing") or {}
        failing: dict[str, dict] = {}
        for f in report["findings"]:
            key = f"integrity:{f['source']}:{f['rule']}"
            entry = failing.setdefault(key, {"rule": f["rule"], "source": f["source"], "severity": f["severity"],
                                             "meaning": f["meaning"], "items": []})
            entry["items"].append({"item": f["item"], "detail": f["detail"]})
        for key in sorted(set(failing) - set(before)):
            f = failing[key]
            publish("integrity_violation", source_component=key, severity=f["severity"],
                    reason=f"{f['meaning']}: {len(f['items'])} found", state_after="VIOLATED",
                    evidence={"rule": f["rule"], "source": f["source"], "items": f["items"][:20]})
        for key in sorted(set(before) - set(failing)):
            publish("integrity_resolved", source_component=key, severity="INFO", state_before="VIOLATED",
                    state_after="RECONCILED", reason=f"{before[key]['meaning']}: none found now")
        errors_before = self.store.meta("integrity.errors") or {}
        for name, error in report["errors"].items():
            if errors_before.get(name) != error:
                publish("collector_failed", source_component=f"guardian.integrity.{name}", severity="WARNING",
                        reason=error)
        changed = sorted(failing) != sorted(before) or report["errors"] != errors_before
        self.store.set_meta("integrity.failing", failing)
        self.store.set_meta("integrity.errors", report["errors"])
        if not changed:
            return report                      # the same answer is not news; the API has it
        publish("reconciliation_completed", source_component="guardian.integrity", severity="INFO",
                reason=f"{len(report['findings'])} finding(s) across {len(report['sources'])} account(s)",
                evidence={"failing_rules": sorted(failing), "sources": report["sources"],
                          "errors": report["errors"],
                          "paper_open_risk": report["exposure"]["paper"]["risk"],
                          "live_positions": report["exposure"]["live"]["positions"]})
        return report
