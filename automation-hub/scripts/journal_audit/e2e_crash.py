"""AUDIT: crash, restart and duplicate scenarios on the real strategy path.

Each scenario runs on its own instance id in the same data dir, drives the
3-Candle Rejection strategy through AutoStrategyEngine._process_bar, and
asserts on the ledger AND the canonical store. Prints PASS/FAIL per check.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import e2e_real as h  # noqa: E402
from bot.types import Bar  # noqa: E402
from data.trade_record_store import TradeRecordStore  # noqa: E402
from services.journal_recorder import JournalRecorder, LedgerSource  # noqa: E402
from test_three_candle_rejection import _history, _long_pattern  # noqa: E402

os.environ.setdefault("AUDIT_QUALITY_BYPASS", "1")
h.BYPASS = True
STORE = TradeRecordStore(h.settings.trade_records_db)
results: list = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def recorder():
    rec = JournalRecorder(STORE)
    rec.add_ledger(LedgerSource("MAIN", h.ledger, decision_store=h.decisions, cycle_store=h.cycles))
    return rec


def records(inst):
    return STORE.query_trades(where="instance_id=?", params=(inst,), limit=100)


def series():
    rows, i = _history()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return h.shifted(rows + _long_pattern(i), now - h.TF)


def warm(engine, bars):
    strategy = engine.strategy_factory(h.SYM)
    strategy.bars.extend(bars[:-3])
    return strategy


def signal(engine, strategy, bars):
    for bar in bars[-3:]:
        engine._process_bar(h.SYM, bar, strategy)


def quote(paper, px=102.21, at=None):
    return paper.process_quote({"bid": px - 0.01, "ask": px + 0.01, "mark": px,
                                "received_at": (at or datetime.now(timezone.utc)).isoformat(),
                                "candle_id": "audit"})


def exit_bars(engine, strategy, bars, target=True):
    t = bars[-1].timestamp + h.TF
    pos = engine.paper.open_position(h.SYM)
    tgt, stp = pos["target"], pos["stop"]
    path = [(102.2, tgt + 0.2, 102.0, tgt)] if target else [(102.2, 102.3, stp - 0.3, stp - 0.1)]
    for o, hi, lo, c in path:
        t += h.TF
        engine._process_bar(h.SYM, Bar(t, o, hi, lo, c, 1.0), strategy)


def scenario(inst):
    os.environ["AUDIT_INST"] = inst
    return h.build(instance_id=inst, session=f"sess-{inst}")


print("\n[9a] duplicate decision: the same candle processed twice across a restart")
inst = "inst-crash-dupdecision"
bars = series()
_, paper, _, engine, saved = scenario(inst)
strat = warm(engine, bars)
signal(engine, strat, bars)
_, paper2, _, engine2, _ = h.build(instance_id=inst, session=f"sess-{inst}", intents=paper.pending_intents())
strat2 = warm(engine2, bars)
signal(engine2, strat2, bars)                     # the restarted worker sees the same candle
wh = h.ledger._c.execute("SELECT alert_id, status FROM webhook_events WHERE instance_id=?", (inst,)).fetchall()
quote(paper2)
recorder().reconcile()
entry_rows = [w for w in wh if ":fill:" not in w[0]]
check("one live entry row; the replay is refused and logged as duplicate",
      sorted(w[1] for w in entry_rows) in (["duplicate", "pending"], ["accepted", "duplicate"]),
      str([tuple(w) for w in entry_rows]))
check("one paper trade", len(h.ledger.get_paper_trades(instance_id=inst)) == 1)
check("one canonical record", len(records(inst)) == 1, str([r["status"] for r in records(inst)]))

print("\n[9b] duplicate fill callback: the same quote delivered twice")
inst = "inst-crash-dupfill"
_, paper, _, engine, _ = scenario(inst)
strat = warm(engine, bars)
signal(engine, strat, bars)
at = datetime.now(timezone.utc)
f1 = quote(paper, at=at)
f2 = quote(paper, at=at)
rec = recorder()
rec.notify()
rec.notify()
rec.reconcile()
rec.reconcile()
check("second delivery fills nothing", len(f1) == 1 and len(f2) == 0, f"{len(f1)} then {len(f2)}")
check("one paper trade", len(h.ledger.get_paper_trades(instance_id=inst)) == 1)
check("one canonical record, OPEN", [r["status"] for r in records(inst)] == ["OPEN"])

print("\n[9c] restart before submission: key claimed, process died before the order")
inst = "inst-crash-presubmit"
h.ledger.insert_webhook_event(alert_id=f"auto:{inst}:BTCUSDT:5m:x:buy", symbol="BTCUSDT", side="BUY",
                              entry=102.2, stop=99.1, payload={"timestamp": bars[-1].timestamp.isoformat()},
                              status="claimed", instance_id=inst)
recorder().reconcile()
check("no trade record for a claimed-but-never-submitted key", records(inst) == [])
check("no paper trade", h.ledger.get_paper_trades(instance_id=inst) == [])

print("\n[9d] restart after submission: intent parked, worker restarted, then filled")
inst = "inst-crash-postsubmit"
_, paper, _, engine, saved = scenario(inst)
strat = warm(engine, bars)
signal(engine, strat, bars)
recorder().reconcile()
before = records(inst)
check("PENDING record while the intent waits", [r["status"] for r in before] == ["PENDING"])
_, paper2, _, engine2, _ = h.build(instance_id=inst, session=f"sess-{inst}", intents=dict(saved))
quote(paper2)
recorder().reconcile()
after = records(inst)
check("same record id after the restart, now OPEN",
      len(after) == 1 and after[0]["journal_record_id"] == before[0]["journal_record_id"]
      and after[0]["status"] == "OPEN", str([(r["journal_record_id"], r["status"]) for r in after]))
strat2 = warm(engine2, bars)
exit_bars(engine2, strat2, bars, target=True)
recorder().reconcile()
final = records(inst)
check("closed on the same record", len(final) == 1 and final[0]["status"] == "CLOSED"
      and final[0]["journal_record_id"] == before[0]["journal_record_id"],
      str([(r["status"], r["exit_reason"]) for r in final]))

print("\n[9e] restart after fill, before any journal pass")
inst = "inst-crash-postfill"
_, paper, _, engine, _ = scenario(inst)
strat = warm(engine, bars)
signal(engine, strat, bars)
quote(paper)                                          # no recorder ran: the crash
_, paper2, _, engine2, _ = h.build(instance_id=inst, session=f"sess-{inst}")
check("restarted engine sees the durable position", paper2.open_position(h.SYM) is not None)
recorder().reconcile()
r = records(inst)
check("one OPEN record rebuilt from the ledger", [x["status"] for x in r] == ["OPEN"])

print("\n[9f] journal-write failure")
inst = "inst-crash-writefail"
_, paper, _, engine, _ = scenario(inst)
strat = warm(engine, bars)
signal(engine, strat, bars)
quote(paper)
exit_bars(engine, strat, bars, target=False)
rec = recorder()
orig = STORE.upsert_trade


def boom(*a, **k):
    raise sqlite3.OperationalError("disk I/O error (audit)")


STORE.upsert_trade = boom
rep = rec.reconcile()
STORE.upsert_trade = orig
check("the failing pass reports the error", bool(rep.get("errors")), str(rep.get("errors"))[:160])
check("nothing half-written", records(inst) == [])
rec.reconcile()
r = records(inst)
check("next pass writes the one CLOSED record",
      [(x["status"], x["exit_reason"]) for x in r] == [("CLOSED", "stop-loss")], str([(x["status"], x["exit_reason"]) for x in r]))

print("\n[9g] reconciliation after restart is idempotent")
snap = {x["journal_record_id"]: (x["facts_hash"], x["updated_at"]) for x in STORE.query_trades(limit=1000)}
for _ in range(3):
    recorder().reconcile()                        # a fresh recorder each time = a restart
snap2 = {x["journal_record_id"]: (x["facts_hash"], x["updated_at"]) for x in STORE.query_trades(limit=1000)}
check("no record changed across 3 fresh recorder passes", snap == snap2, f"{len(snap)} records")
corr = STORE._c.execute("SELECT kind, COUNT(*) FROM trade_record_corrections GROUP BY kind").fetchall()
check("no DISCREPANCY corrections logged", not any(k == "DISCREPANCY" for k, _ in corr), str(corr))

failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
json.dump([{"check": n, "ok": ok, "detail": d} for n, ok, d in results],
          open(os.path.join(os.environ["HUB_DATA_DIR"], "e2e_crash.json"), "w"), indent=2)
sys.exit(1 if failed else 0)
