"""Read-only provenance check for the Journal / Evolution Memory numbers.

Opens every database with mode=ro and changes nothing. Prints:
  A. the Evolution Memory counters exactly as stored;
  B. the decision-journal rows (what the Journal page lists);
  C. journal events whose trade row no longer exists (evidence of removed rows);
  D. per setup: counters vs the closed journal rows that could back them;
  E. the ledger's paper trades (the execution facts) by strategy / instance / side;
  F. what the running API returns with no filters.

On the server:
    cd /opt/nexus-trading-bot
    docker compose exec -T app python scripts/journal_provenance.py
    # before a deploy that ships it, from the pulled checkout:
    docker compose exec -T app python - < automation-hub/scripts/journal_provenance.py
"""
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

# Run as `python scripts/journal_provenance.py` from the app root.
# Piped over stdin (`python - < file`) there is no __file__; the container's
# working directory is the app root then.
if "__file__" in globals():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def cols(c, table):
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def show(title, rows):
    print(f"\n== {title} ==")
    for r in rows:
        print("  ", tuple(r))


data_dir = os.path.dirname(settings.journal_db)
print("journal db:", settings.journal_db, "exists:", os.path.exists(settings.journal_db))
for f in sorted(glob.glob(os.path.join(data_dir, "*journal*.db")) + glob.glob("/app/**/journal*.db", recursive=True)):
    st = os.stat(f)
    print(f"  file {f}  {st.st_size} bytes  modified {st.st_mtime:.0f}")

j = ro(settings.journal_db)
show("A. evolution_memory (as stored)", j.execute(
    "SELECT setup_key, trades, wins, ROUND(net_r,2), stage, updated_at FROM evolution_memory ORDER BY trades DESC"))

print("\n== B. trade_decision_journal ==")
print("   total rows:", j.execute("SELECT COUNT(*) FROM trade_decision_journal").fetchone()[0])
show("B1. by status / execution_mode / has instance", j.execute(
    "SELECT status, COALESCE(execution_mode,'<null>'), instance_id IS NOT NULL AND instance_id<>'', COUNT(*), "
    "MIN(created_at), MAX(created_at) FROM trade_decision_journal GROUP BY 1,2,3"))
show("B2. closed rows by strategy|regime|side", j.execute(
    "SELECT strategy, regime, side, COUNT(*), SUM(result='win'), ROUND(SUM(actual_rr),2), MIN(closed_at), MAX(closed_at) "
    "FROM trade_decision_journal WHERE status='closed' GROUP BY 1,2,3"))

print("\n== C. journal events ==")
print("   events:", j.execute("SELECT COUNT(*), COUNT(DISTINCT trade_id), MIN(ts), MAX(ts) FROM trade_decision_events").fetchone())
orph = j.execute(
    "SELECT e.trade_id, e.ts, e.kind, e.detail FROM trade_decision_events e "
    "LEFT JOIN trade_decision_journal t ON t.trade_id = e.trade_id WHERE t.trade_id IS NULL ORDER BY e.id").fetchall()
orph_ids = sorted({r[0] for r in orph})
print("   events whose journal row is missing:", len(orph), "for", len(orph_ids), "trade ids")
kinds = {}
for r in orph:
    kinds[r[2]] = kinds.get(r[2], 0) + 1
print("   their kinds:", kinds)
closed_orphans = [r for r in orph if r[2] == "trade-closed"]
opened_orphans = {r[0]: r[3] for r in orph if r[2] == "trade-opened"}
by_setup = {}
for tid, ts, _k, detail in closed_orphans:
    m = re.match(r"(\w+) · ([+-]?[\d.]+)R", detail or "")
    side = (opened_orphans.get(tid, "").split(" ") or ["?"])[0].lower()
    key = side
    s = by_setup.setdefault(key, [0, 0, 0.0, ts, ts])
    s[0] += 1
    if m:
        s[1] += m.group(1) == "win"
        s[2] += float(m.group(2))
    s[3], s[4] = min(s[3], ts), max(s[4], ts)
show("C1. closed trades evidenced only by events (side, trades, wins, R, first, last)",
     [(k, v[0], v[1], round(v[2], 2), v[3], v[4]) for k, v in by_setup.items()])
print("   sample orphan trade ids:", orph_ids[:8])

print("\n== D. counters vs journal rows that can back them ==")
for key, trades, wins, net_r in j.execute("SELECT setup_key, trades, wins, net_r FROM evolution_memory"):
    strat, regime, side = (key.split("|") + ["", "", ""])[:3]
    n, w, r = j.execute(
        "SELECT COUNT(*), COALESCE(SUM(result='win'),0), COALESCE(SUM(actual_rr),0) FROM trade_decision_journal "
        "WHERE status='closed' AND strategy=? AND regime=? AND side=?", (strat, regime, side)).fetchone()
    print(f"   {key}: counter {trades} trades/{wins} wins/{net_r:+.2f}R | journal rows {n}/{w}/{r:+.2f}R "
          f"| unbacked {trades - n}")

led_path = getattr(settings, "ledger_path", None) or os.path.join(data_dir, "ledger.db")
print("\nledger db:", led_path, "exists:", os.path.exists(led_path))
if os.path.exists(led_path):
    l = ro(led_path)
    pc = cols(l, "paper_trades")
    strat_col = "strategy_id" if "strategy_id" in pc else "''"
    inst_col = "instance_id" if "instance_id" in pc else "''"
    show("E. paper_trades by source / strategy_id / has instance / side / status", l.execute(
        f"SELECT source, COALESCE({strat_col},''), COALESCE({inst_col},'')<>'', side, status, COUNT(*), "
        f"ROUND(SUM(COALESCE(pnl,0)),2), MIN(opened_at), MAX(opened_at) FROM paper_trades GROUP BY 1,2,3,4,5 ORDER BY 6 DESC"))
    if orph_ids:
        marks = ",".join("?" * min(len(orph_ids), 900))
        found = l.execute(f"SELECT COUNT(*), MIN(opened_at), MAX(closed_at) FROM paper_trades WHERE id IN ({marks})",
                          orph_ids[:900]).fetchone()
        print("   orphan journal trade ids present in paper_trades:", found)
        show("E1. those ledger rows by strategy / instance / session", l.execute(
            f"SELECT COALESCE({strat_col},''), COALESCE({inst_col},''), COALESCE(simulation_session_id,''), side, COUNT(*) "
            f"FROM paper_trades WHERE id IN ({marks}) GROUP BY 1,2,3,4", orph_ids[:900]))
    if "instance_id" in pc:
        jt = {r[0] for r in j.execute("SELECT trade_id FROM trade_decision_journal")}
        rows = l.execute("SELECT id, instance_id, status FROM paper_trades WHERE COALESCE(instance_id,'')<>''").fetchall()
        missing = [r for r in rows if r[0] not in jt]
        print(f"\n   instance paper trades: {len(rows)}; with no journal row: {len(missing)} "
              f"({sum(1 for r in missing if r[2]=='closed')} closed, {sum(1 for r in missing if r[2]=='open')} open)")

try:
    req = urllib.request.Request("http://127.0.0.1:8000/journal/trades?limit=500",
                                 headers={"X-Webhook-Secret": settings.admin_key})
    body = json.load(urllib.request.urlopen(req, timeout=30))
    print("\n== F. API /journal/trades with no filters returns", len(body.get("trades", [])), "rows ==")
except Exception as exc:  # noqa: BLE001
    print("\n== F. API check failed:", type(exc).__name__, exc)
