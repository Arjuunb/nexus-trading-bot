# Local check (SQLite 3.45.1): which read-only open mode leaves a live WAL database untouched.
# Usage: python3 -I sqlite_open_mode_check.py <empty scratch dir>
import hashlib, os, sqlite3, sys, tempfile
d = tempfile.mkdtemp(dir=sys.argv[1]); p = os.path.join(d, "g.db")
h = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()
app = sqlite3.connect(p); app.execute("PRAGMA journal_mode=WAL"); app.execute("PRAGMA wal_autocheckpoint=0")
app.execute("CREATE TABLE meta(k PRIMARY KEY, v)"); app.execute("INSERT INTO meta VALUES ('heartbeat','42')"); app.commit()
for mode in ("mode=ro&immutable=1", "mode=ro&readonly_shm=1", "mode=ro"):
    before = {f: (h(os.path.join(d, f)), os.stat(os.path.join(d, f)).st_mtime_ns) for f in sorted(os.listdir(d))}
    try:
        c = sqlite3.connect(f"file:{p}?{mode}", uri=True); r = c.execute("SELECT v FROM meta").fetchall(); c.close()
    except Exception as e:
        r = str(e)
    after = {f: (h(os.path.join(d, f)), os.stat(os.path.join(d, f)).st_mtime_ns) for f in sorted(os.listdir(d))}
    print(f"{mode:28s} -> {r} | changed: {[f for f in after if before.get(f) != after[f]]} | new: {sorted(set(after)-set(before))}")

# Second scenario: the app has closed its connections, so -wal/-shm do not exist.
for mode in ("mode=ro", "mode=ro&readonly_shm=1", "mode=ro&immutable=1"):
    d = tempfile.mkdtemp(dir=sys.argv[1]); p = os.path.join(d, "g.db")
    c = sqlite3.connect(p); c.execute("PRAGMA journal_mode=WAL"); c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES (1)"); c.commit(); c.close()
    before = sorted(os.listdir(d))
    try:
        r = sqlite3.connect(f"file:{p}?{mode}", uri=True); out = r.execute("SELECT count(*) FROM t").fetchall(); r.close()
    except Exception as e:
        out = str(e)
    print(f"no sidecars | {mode:26s} -> {out} | created: {sorted(set(os.listdir(d)) - set(before))}")
