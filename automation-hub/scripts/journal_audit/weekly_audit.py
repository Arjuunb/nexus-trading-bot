"""AUDIT: weekly reviews, scheduler, agent isolation, memory and safe learning.

Runs on a COPY of the audit trade_records.db (argv[1] = run dir, argv[2] =
scratch copy path) so the served dataset keeps its own state.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from data.trade_record_store import TradeRecordStore  # noqa: E402
from services import journal_stats as stats  # noqa: E402
from services.journal_memory import verified_memory  # noqa: E402
from services.journal_reviews import (WeeklyReviewScheduler, build_weekly_review, review_scopes,  # noqa: E402
                                      run_weekly, week_bounds)

run, copy = sys.argv[1], sys.argv[2]
shutil.copy(f"{run}/trade_records.db", copy)
store = TradeRecordStore(copy)
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


now = datetime.now(timezone.utc)
after = now + timedelta(days=8)            # the current week is now complete
start, end = week_bounds(now)
print(f"week under review: {start.isoformat()} -> {end.isoformat()}")
scopes = review_scopes(store)
print("scopes:", [(s["agent_id"], s["strategy_id"]) for s in scopes])

# ---------------- 12. determinism: same inputs, same review
print("\n[12] determinism")
for s in scopes:
    a = build_weekly_review(store, s, start, end)
    b = build_weekly_review(store, s, start, end)
    for x in (a, b):
        x.pop("generated_at")
    check(f"{s['agent_id']}|{s['strategy_id']} built twice is identical", a == b)
# independent recomputation of the headline figures
print("\n[12] stats vs independent SQL")
db = sqlite3.connect(copy)
db.row_factory = sqlite3.Row
for s in scopes:
    rv = build_weekly_review(store, s, start, end)
    rows = [dict(r) for r in db.execute(
        f"SELECT * FROM trade_records WHERE record_origin='FORWARD_PAPER' AND status='CLOSED' AND ({s['where']}) "
        "AND position_closed_at>=? AND position_closed_at<?", (*s["params"], start.isoformat(), end.isoformat()))]
    o = rv["stats"]["overall"]
    check(f"{s['agent_id']} trades", o["trades"] == len(rows), f"{o['trades']} vs {len(rows)}")
    check(f"{s['agent_id']} net_pnl", abs((o["net_pnl"] or 0) - sum(r["net_pnl"] for r in rows)) < 1e-6)
    check(f"{s['agent_id']} total_r", abs((o["total_r"] or 0) - sum(r["realized_r"] for r in rows)) < 1e-4)
    check(f"{s['agent_id']} ids = scope records", sorted(rv["journal_record_ids"]) == sorted(r["journal_record_id"] for r in rows))

# ---------------- 13/14. every finding cites records, and only records of its own scope
print("\n[13/14] evidence ids and agent isolation")
for s in scopes:
    rv = build_weekly_review(store, s, start, end)
    own = set(rv["journal_record_ids"])
    in_scope = {r[0] for r in db.execute(
        f"SELECT journal_record_id FROM trade_records WHERE ({s['where']})", tuple(s["params"]))}
    for kind, items in rv["findings"].items():
        for f in items:
            ids = set(f.get("journal_record_ids") or [])
            check(f"{s['agent_id']} {kind}: '{f['text'][:50]}' cites only its scope",
                  ids <= own and ids <= in_scope, f"{len(ids)} id(s)")
            if kind in ("facts", "observations") and "No completed trades" not in f["text"]:
                check(f"{s['agent_id']} {kind} has at least one id", len(ids) > 0)
other = {r[0]: r[1] for r in db.execute("SELECT journal_record_id, record_source FROM trade_records")}

# ---------------- 16. scheduler
print("\n[16] scheduler")
sched = WeeklyReviewScheduler(store, interval_s=60)
first = sched.tick(now=after)
check("normal run writes one review per scope with data", len(first["written"]) == len(scopes),
      f"{len(first['written'])} written for {len(scopes)} scopes")
second = sched.tick(now=after)
check("duplicate invocation writes nothing", second["written"] == [], json.dumps(second))
restarted = WeeklyReviewScheduler(store, interval_s=60).tick(now=after)
check("a restarted scheduler writes nothing", restarted["written"] == [])
n_reviews = db.execute("SELECT COUNT(*) FROM weekly_reviews").fetchone()[0]
check("stored weekly reviews = scopes", n_reviews == len(scopes), str(n_reviews))
# concurrent ticks on a fresh copy
shutil.copy(f"{run}/trade_records.db", copy + ".conc")
cstore = TradeRecordStore(copy + ".conc")
outs = []
ts = [threading.Thread(target=lambda: outs.append(run_weekly(cstore, now=after))) for _ in range(4)]
for t in ts:
    t.start()
for t in ts:
    t.join()
cn = sqlite3.connect(copy + ".conc").execute("SELECT COUNT(*) FROM weekly_reviews").fetchone()[0]
check("4 concurrent runs write each review once", cn == len(scopes), f"{cn} rows; written per run {[len(o['written']) for o in outs]}")
# missed run: the scheduler was down for three weeks
shutil.copy(f"{run}/trade_records.db", copy + ".missed")
mstore = TradeRecordStore(copy + ".missed")
missed = run_weekly(mstore, now=now + timedelta(days=22))
check("after 3 missed weeks it catches up the week that had trades, and writes no empty weeks",
      len(missed["written"]) == len(scopes), json.dumps(missed)[:200])
# a RUNNING claim left by a crash is retried after its lease
shutil.copy(f"{run}/trade_records.db", copy + ".crash")
xstore = TradeRecordStore(copy + ".crash")
s0 = scopes[0]
xstore.claim_review_run(s0["agent_id"], s0["strategy_id"], start.isoformat(), end.isoformat())
blocked = run_weekly(xstore, now=after)
check("a fresh RUNNING claim is respected", blocked["already_done"] == 1 and len(blocked["written"]) == len(scopes) - 1,
      f"written {len(blocked['written'])}, already_done {blocked['already_done']}")
xstore._c.execute("UPDATE review_runs SET started_at=? WHERE status='RUNNING'",
                  ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),))
xstore._c.commit()
retried = run_weekly(xstore, now=after)
check("a stale RUNNING claim (crashed run) is retried", len(retried["written"]) == 1, json.dumps(retried)[:200])

# ---------------- late record: a trade that lands in an already-reviewed week
print("\n[16b] late-arriving record for a reviewed week")
rv_before = store.weekly_reviews(agent_id=scopes[0]["agent_id"], strategy_id=scopes[0]["strategy_id"], limit=5)[0]
late = dict(store.query_trades(where=f"({scopes[0]['where']}) AND status='CLOSED'",
                               params=tuple(scopes[0]["params"]), limit=1)[0])
full = store.get(late["journal_record_id"])
rec = {k: full.get(k) for k in ("record_source", "record_origin", "status", "instance_id", "strategy_id",
                                 "strategy_name", "symbol", "side", "timeframe", "net_pnl", "realized_r",
                                 "outcome", "position_opened_at", "position_closed_at", "exit_reason")}
rec.update({"data_completeness": "FULL", "verification": "VERIFIED", "operating_mode": "paper", "execution_key": "AUDIT:late-arrival", "trade_id": "audit-late-1", "net_pnl": -10.0,
            "realized_r": -1.0, "outcome": "LOSS", "exit_reason": "stop-loss"})
store.upsert_trade(rec)
again = run_weekly(store, now=after)
rv_after = store.weekly_reviews(agent_id=scopes[0]["agent_id"], strategy_id=scopes[0]["strategy_id"], limit=5)
ids_after = set(rv_after[0]["journal_record_ids"])
late_id = store.by_key("AUDIT:late-arrival")["journal_record_id"]
print(f"  review written again: {again['written']}; stored review includes the late record: {late_id in ids_after}")
check("a late record in a reviewed week is reflected (or the review is flagged stale)", late_id in ids_after,
      "EXPECTED FAIL if the scheduler never revisits a DONE week")

# ---------------- 15. memory and safe learning
print("\n[15] memory")
mem = verified_memory(store)
for m in mem:
    ids = set(m["journal_record_ids"])
    origins = {r[0] for r in db.execute(
        f"SELECT DISTINCT record_origin FROM trade_records WHERE journal_record_id IN ({','.join('?' * len(ids))})",
        tuple(ids))} if ids else set()
    check(f"memory {m['setup_key']}: {m['trades']} trades, ids={len(ids)}, only FORWARD_PAPER",
          m["trades"] == len(ids) and origins <= {"FORWARD_PAPER"}, str(origins))
    check(f"memory {m['setup_key']}: under {stats.MIN_SAMPLE} trades is not 'evidence'",
          m["trades"] >= stats.MIN_SAMPLE or m["stage"] != "evidence", m["stage"])
props = store.proposals()
check("no proposals below the 20-trade sample", props == [] or all(p["sample_size"] >= 20 for p in props),
      f"{len(props)} proposal(s)")

print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
