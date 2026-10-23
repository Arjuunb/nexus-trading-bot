import json, os, re, sqlite3, statistics, sys, tempfile, time
from datetime import datetime, timedelta, timezone
text = open(sys.argv[1]).read()
ddl = re.search(r"CREATE TABLE IF NOT EXISTS pa_evaluations\(.*?\);", text, re.S).group(0)
d = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__))); p = os.path.join(d, "x.db")
c = sqlite3.connect(p); c.execute(ddl); c.execute("CREATE INDEX ix ON pa_evaluations(updated_at)")
s = datetime(2026, 8, 24, tzinfo=timezone.utc); pad = "x" * 2600
c.executemany("INSERT INTO pa_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
  [(f"c{i}", "s", f"i{i}", (s+timedelta(minutes=5*i)).isoformat(), "B", "5m", "P", "1", "W", "r", "[]", pad,
    (s+timedelta(minutes=5*i)).isoformat(), (s+timedelta(minutes=5*i)).isoformat()) for i in range(100000)])
c.commit()
at, rid = c.execute("SELECT updated_at, rowid FROM pa_evaluations ORDER BY rowid DESC LIMIT 1").fetchone()
q = "SELECT rowid AS _rowid, * FROM pa_evaluations WHERE (updated_at, rowid) > (?, ?) ORDER BY updated_at, rowid LIMIT 500"
print(" | ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + q, (at, rid))))
ts = []
for _ in range(7):
    t = time.perf_counter(); c.execute(q, (at, rid)).fetchall(); ts.append((time.perf_counter()-t)*1000)
print(f"row-value seek @100k rows: {statistics.median(ts):.3f} ms")
c.close(); [os.remove(os.path.join(d, f)) for f in os.listdir(d)]; os.rmdir(d)
