"""AUDIT: every Journal API number against an independent computation from the DB.

The API is the real backend (uvicorn app:app) serving the audit data dir. The
independent numbers are computed here with plain SQL over trade_records.db,
not with services/journal_stats.py.
"""
import http.cookiejar
import json
import os
import sqlite3
import sys
import urllib.request

RUN = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8777"
if not os.path.exists(os.path.join(RUN, ".journal-audit-scratch")):
    sys.exit("api_vs_db: the run directory is not a journal-audit scratch directory")
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, body=None, method=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with op.open(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


_form = urllib.request.Request(BASE + "/auth/login", data=b"username=admin&password=admin", method="POST",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
try:
    print("login:", op.open(_form, timeout=30).status)
except urllib.error.HTTPError as e:
    print("login:", e.code, e.read()[:200])
db = sqlite3.connect(f"{RUN}/trade_records.db")
db.row_factory = sqlite3.Row
fails = 0


def check(name, got, want, tol=1e-6):
    global fails
    ok = got == want or (isinstance(got, (int, float)) and isinstance(want, (int, float))
                         and abs(got - want) <= tol * max(1, abs(want)))
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:42} api={got!r:28} db={want!r}")


# ---------------- default view = forward paper
st, recs = call("/journal/records?limit=500")
print("\n[records default]", st)
fwd = [dict(r) for r in db.execute("SELECT * FROM trade_records WHERE record_origin='FORWARD_PAPER'")]
closed = [r for r in fwd if r["status"] == "CLOSED"]
check("total records (forward)", recs["total"], len(fwd))
check("every returned record is FORWARD_PAPER", all(r["record_origin"] == "FORWARD_PAPER" for r in recs["records"]), True)
k = recs["kpis"]
print("  kpis:", json.dumps(k)[:600])
net = sum(r["net_pnl"] for r in closed)
wins = [r for r in closed if r["outcome"] == "WIN"]
losses = [r for r in closed if r["outcome"] == "LOSS"]
gw, gl = sum(r["net_pnl"] for r in closed if r["net_pnl"] > 0), -sum(r["net_pnl"] for r in closed if r["net_pnl"] < 0)
check("closed trades", k.get("trades"), len(closed))
check("net pnl", k.get("net_pnl"), round(net, 2), 1e-2)
check("total R", k.get("total_r"), round(sum(r["realized_r"] for r in closed), 2), 1e-2)
check("win rate", k.get("win_rate"), round(len(wins) / len(closed), 4) if closed else None, 1e-3)
check("profit factor", k.get("profit_factor"), round(gw / gl, 2) if gl else None, 1e-2)

# ---------------- origin isolation
for origin in ("SIMULATION", "LEGACY_MIGRATION", "BACKTEST", "RESEARCH"):
    st, r = call(f"/journal/records?origin={origin}&limit=500")
    n = db.execute("SELECT COUNT(*) FROM trade_records WHERE record_origin=?", (origin,)).fetchone()[0]
    check(f"origin={origin} count", r["total"], n)
    check(f"origin={origin} only that origin", all(x["record_origin"] == origin for x in r["records"]), True)
st, r = call("/journal/records?origin=all&limit=500")
check("origin=all count", r["total"], db.execute("SELECT COUNT(*) FROM trade_records").fetchone()[0])

# ---------------- source filters
for source in ("INSTANCE", "SMC_LAB", "PA_LAB", "AGENT", "LEGACY_ENGINE", "MANUAL"):
    st, r = call(f"/journal/records?origin=all&source={source}&limit=500")
    n = db.execute("SELECT COUNT(*) FROM trade_records WHERE record_source=?", (source,)).fetchone()[0]
    got = r["total"] if isinstance(r, dict) else r
    check(f"source={source} count", got, n)

# ---------------- one record detail = DB row
rid = closed[0]["journal_record_id"]
st, full = call(f"/journal/records/{rid}")
row = dict(db.execute("SELECT * FROM trade_records WHERE journal_record_id=?", (rid,)).fetchone())
print(f"\n[detail {rid}]", st)
for f in ("net_pnl", "realized_r", "actual_entry", "actual_exit", "exit_reason", "position_id", "decision_id",
          "entry_filled_at", "position_closed_at", "record_origin", "record_source", "outcome"):
    check(f"detail.{f}", full.get(f), row[f])
check("timeline stages", len(full.get("timeline") or []), 10)

# ---------------- decisions
st, d = call("/journal/decision-records?limit=500")
print("\n[decisions]", st)
check("decision total", d["total"], db.execute("SELECT COUNT(*) FROM decision_records").fetchone()[0])
by = {r[0]: r[1] for r in db.execute("SELECT decision_type, COUNT(*) FROM decision_records GROUP BY 1")}
check("decision by_type", d["by_type"], by)
st, d2 = call("/journal/decision-records?traded=no&limit=500")
check("traded=no has no journal link", all(x["journal_record_id"] is None for x in d2["decisions"]), True)
check("no decision carries P&L", all("net_pnl" not in x for x in d["decisions"]), True)

# ---------------- facets
st, f = call("/journal/records/facets")
print("\n[facets]", st, json.dumps(f)[:300])

# ---------------- notes: a note never changes facts
before = row["facts_hash"]
st, n = call(f"/journal/records/{rid}/notes", {"text": "audit note: target hit on the third candle"})
print("\n[note]", st, str(n)[:160])
after = db.execute("SELECT facts_hash, updated_at FROM trade_records WHERE journal_record_id=?", (rid,)).fetchone()
check("facts_hash unchanged by a note", after[0], before)
check("note stored in trade_notes", db.execute("SELECT COUNT(*) FROM trade_notes WHERE journal_record_id=?",
                                               (rid,)).fetchone()[0] >= 1, True)
st, nn = call("/journal/notes")
check("note author is the signed-in user", (nn["notes"][0] or {}).get("author"), "admin")

# ---------------- recorder status
st, rs = call("/journal/recorder")
print("\n[recorder]", st, json.dumps(rs)[:400])
print(f"\n{'ALL MATCH' if not fails else f'{fails} MISMATCH(ES)'}")
