"""Synthetic benchmark of the queries that run continuously over pa_evaluations.

Uses the exact DDL from the deployed lineage's services/price_action_lab.py and
synthetic rows (no production data). Measures:
  A. Guardian PAEvaluationReader steady-state query (every 15 s cycle)
  B. Same query with a hypothetical index on (updated_at, rowid)
  C. Journal Recorder _project_waiting_setups full read + json.loads (every 30 s pass)
"""
import json, os, re, sqlite3, statistics, sys, tempfile, time
from datetime import datetime, timedelta, timezone

SRC = sys.argv[1]
SIZES = [int(x) for x in sys.argv[2].split(",")]
PAYLOAD_BYTES = int(sys.argv[3]) if len(sys.argv) > 3 else 3000

text = open(SRC).read()
ddl = re.search(r"CREATE TABLE IF NOT EXISTS pa_evaluations\(.*?\);", text, re.S).group(0)
idx = re.search(r"CREATE INDEX IF NOT EXISTS pa_evaluations_session_candle\s+ON pa_evaluations\(.*?\);", text, re.S).group(0)

GUARDIAN_SQL = ("SELECT rowid AS _rowid, * FROM pa_evaluations WHERE updated_at > ? "
                "OR (updated_at = ? AND rowid > ?) ORDER BY updated_at, rowid LIMIT ?")
JOURNAL_SQL = ("SELECT session_id, candle_time, strategy_id, payload_json "
               "FROM pa_evaluations ORDER BY session_id, candle_time")


def timeit(fn, n=5):
    out = []
    for _ in range(n):
        t = time.perf_counter(); fn(); out.append((time.perf_counter() - t) * 1000)
    return statistics.median(out)


def build(path, rows):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=NORMAL")
    c.execute(ddl); c.execute(idx)
    start = datetime(2026, 8, 24, tzinfo=timezone.utc)
    filler = "x" * max(0, PAYLOAD_BYTES - 400)
    batch = []
    for i in range(rows):
        t = (start + timedelta(minutes=5 * i)).isoformat()
        trace = {"setup_id": f"s{i // 40}" if i % 7 == 0 else None,
                 "state": "ORDER_PENDING" if i % 7 == 0 else "WATCHING",
                 "conditions": [{"id": "c1", "ok": True}], "pad": filler}
        payload = json.dumps({"closed_candle": {"t": t, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
                              "trace": trace, "mtf_evidence": {"primary": {"t": t}}})
        batch.append((f"corr-{i}", "sess-1", f"idem-{i}", t, "BTCUSDT", "5m", "PA_V1", "1",
                      "WATCHING", "no eligible setup", "[]", payload, t, t))
        if len(batch) == 5000:
            c.executemany("INSERT INTO pa_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch); batch = []
    if batch:
        c.executemany("INSERT INTO pa_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    c.commit(); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()


def main():
    print(f"payload_bytes~{PAYLOAD_BYTES}")
    for n in SIZES:
        d = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(d, "pa.db")
        build(path, n)
        size_mb = os.path.getsize(path) / 1e6
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True); c.row_factory = sqlite3.Row
        last = c.execute("SELECT updated_at, rowid FROM pa_evaluations ORDER BY rowid DESC LIMIT 1").fetchone()
        args = (last[0], last[0], last[1], 500)
        plan = " | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + GUARDIAN_SQL, args))
        a = timeit(lambda: c.execute(GUARDIAN_SQL, args).fetchall())
        # C: journal recorder full pass incl. JSON parse
        def journal():
            for r in c.execute(JOURNAL_SQL):
                json.loads(r["payload_json"])
        cj = timeit(journal, 3)
        c.close()
        # B: hypothetical index (on a copy, never on real data)
        w = sqlite3.connect(path); w.execute("CREATE INDEX ix_tmp ON pa_evaluations(updated_at)"); w.commit(); w.close()
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        plan_b = " | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + GUARDIAN_SQL, args))
        b = timeit(lambda: c.execute(GUARDIAN_SQL, args).fetchall())
        c.close()
        print(f"rows={n:>7} db={size_mb:7.1f}MB | guardian_reader={a:8.1f}ms plan=[{plan}] | "
              f"with_index={b:6.2f}ms plan=[{plan_b}] | journal_full_pass={cj:8.1f}ms")
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
        os.rmdir(d)


main()
