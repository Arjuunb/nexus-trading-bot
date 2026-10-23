#!/bin/sh
# Verified backup of the staging data the Price Action Lab retirement deploy could change.
# The originals are only read.
#
#   start    check that it is safe, then start the backup in the background and return; it keeps
#            running if the GitHub connection ends
#   status   read-only: progress, or the final result
#
# What is backed up (the new API image can only write these two places):
#   1. The STOPPED staging API's data volume (nexus-remote-stage-rc3-28acdbc_api-state): every
#      file, mainly smc_strategy_paper.db and its -wal/-shm, except the Task 6 recovery folder
#      (.task6-sqlite-recovery-*), which is itself a compressed preservation copy the app never opens.
#   2. The staging Postgres metadata database, with Postgres's own pg_dumpall/pg_dump, taken from
#      the running staging Postgres container (a consistent snapshot; nothing is stopped).
# Not included: research/shadow/datasets volumes (the API mounts them read-only) and production.
#
# Steps:
#   1. Record identity (inode, size, mtime) and SHA-256 of every source file (staging API stopped,
#      no running container uses the volume, no process holds any file in it open).
#   2. Pack them into one compressed archive under /var/lib/nexus-ops/staging-backup.
#   3. Confirm the source files did not change while packing.
#   4. Dump Postgres: roles (globals) and each database in custom format; count every table's rows.
#   5. Restore test in temporary places:
#      - SQLite: unpack, every file byte-identical to the original; open each database copy so
#        SQLite applies its committed WAL, then full integrity_check, foreign_key_check, row counts
#      - Postgres: start a throwaway container from the same image with no network, load the dump,
#        require every table to come back, count its rows
#   6. Write manifest.json only if all of that passed, then delete the temporary copies.
# It never opens, writes, locks or checkpoints the original SQLite files, never writes to the
# staging Postgres, and never touches production.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in start|status) ;; *) echo "usage: staging-backup.sh start|status"; exit 64 ;; esac
BASE=${STAGING_BACKUP_TEST_BASE:-/var/lib/nexus-ops/staging-backup}
mkdir -p "$BASE" && chmod 700 "$BASE"
cat > "$BASE/staging_backup.py" <<'PY'
import fcntl, hashlib, json, os, re, secrets, shutil, sqlite3, stat, subprocess, sys, time

MODE, BASE = sys.argv[1], sys.argv[2]
PROJECT = "nexus-remote-stage-rc3-28acdbc"
API = PROJECT + "-api-1"
PG = PROJECT + "-postgres-1"
VOLUME = PROJECT + "_api-state"
SKIP = re.compile(r"\.task6-sqlite-recovery-[0-9a-f]+(/|$)")
MUST_HAVE = "smc_strategy_paper.db"
MANIFEST = os.path.join(BASE, "manifest.json")
RESULT = os.path.join(BASE, "result.json")
PROGRESS = os.path.join(BASE, "progress.log")
LOCK = os.path.join(BASE, ".lock")
RESTORE = os.path.join(BASE, "restore-test")
PGTEST_DIR = os.path.join(BASE, "pg-restore-test")
PGTEST = "nexus-ops-staging-backup-pgtest"
MARGIN = int(os.environ.get("STAGING_BACKUP_TEST_MARGIN", 4 * 10**9))
ZSTD = shutil.which("zstd")
ARCHIVE = os.path.join(BASE, "api-state.tar." + ("zst" if ZSTD else "gz"))
PGDIR = os.path.join(BASE, "postgres")
NICE = ["nice", "-n", "19", "ionice", "-c", "2", "-n", "7"]
COUNT_SQL = ("SELECT format('SELECT %L || ''|'' || count(*) FROM %I.%I', n.nspname || '.' || c.relname, n.nspname, c.relname) "
             "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') "
             "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' ORDER BY 1 \\gexec\n")

def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def gb(n): return "%.2f GB" % (n / 1e9)
def log(msg):
    with open(PROGRESS, "a") as f: f.write("%s %s\n" % (now(), msg))
def free(path=BASE):
    s = os.statvfs(path); return s.f_bavail * s.f_frsize
def run(cmd, timeout=600, **kw):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    if r.returncode: raise RuntimeError("%s failed (exit %d): %s" % (" ".join(cmd[:4]), r.returncode, r.stderr.decode(errors="replace").strip()[-200:]))
    return r.stdout
def docker(*a):
    return run(["docker", *a], timeout=60).decode()

class Blocked(Exception): pass

def preflight():
    vol = json.loads(docker("volume", "inspect", VOLUME))[0]
    if (vol.get("Labels") or {}).get("com.docker.compose.project") != PROJECT: raise Blocked("volume is not staging's")
    d = vol["Mountpoint"]
    api = json.loads(docker("inspect", API))[0]
    if api["State"]["Running"]: raise Blocked("staging API is running")
    for cid in docker("ps", "-q").split():
        c = json.loads(docker("inspect", cid))[0]
        if any(m.get("Name") == VOLUME or (m.get("Source") or "").startswith(d) for m in c.get("Mounts", [])):
            raise Blocked("running container %s uses the staging API volume" % c["Name"].lstrip("/"))
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try: fds = os.listdir("/proc/%s/fd" % pid)
        except OSError: continue
        for fd in fds:
            try: t = os.readlink("/proc/%s/fd/%s" % (pid, fd))
            except OSError: continue
            if t.startswith(d + "/"): raise Blocked("process %s holds %s open" % (pid, t))
    ident, skipped = {}, 0
    for root, dirs, files in os.walk(d):
        for name in sorted(files) + [x for x in dirs if os.path.islink(os.path.join(root, x))]:
            p = os.path.join(root, name); rel = os.path.relpath(p, d)
            if SKIP.match(rel):
                skipped += os.lstat(p).st_size; continue
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1: raise Blocked("%s is not a single-link regular file" % rel)
            ident[rel] = {"inode": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if MUST_HAVE not in ident: raise Blocked("%s is not there" % MUST_HAVE)
    pg = json.loads(docker("inspect", PG))[0]
    if not pg["State"]["Running"] or (pg["State"].get("Health") or {}).get("Status") not in (None, "healthy"):
        raise Blocked("staging Postgres is not running and healthy")
    return d, ident, skipped, pg

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""): h.update(b)
    return h.hexdigest()

def is_sqlite(path):
    try:
        with open(path, "rb") as f: return f.read(16) == b"SQLite format 3\x00"
    except OSError: return False

def held():
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return fd
    except OSError:
        os.close(fd); return None

def psql(container, db, sql, timeout=600):
    return run(["docker", "exec", "-i", "-u", "postgres", container, "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
                "-U", "postgres", "-d", db, "-f", "-"], timeout=timeout, input=sql.encode()).decode()

def counts(container, db):
    out = {}
    for line in psql(container, db, COUNT_SQL).splitlines():
        if "|" in line:
            k, _, v = line.rpartition("|"); out[k] = int(v)
    return out

def start():
    fd = held()
    if fd is None: print("a backup is already running; use 'status'"); return 0
    try:
        if os.path.exists(MANIFEST):
            print("a verified backup already exists (manifest.json); use 'status'"); return 0
        d, ident, skipped, pg = preflight()
        size = sum(i["size"] for i in ident.values())
        need = int(size * 0.25) + size + MARGIN
        if free() < need:
            raise Blocked("not enough free disk: need %s (archive, temporary restore test, margin), have %s" % (gb(need), gb(free())))
        for p in (RESULT, PROGRESS, ARCHIVE + ".part"):
            if os.path.exists(p): os.remove(p)
        for p in (RESTORE, PGTEST_DIR, PGDIR): shutil.rmtree(p, ignore_errors=True)
        biggest = sorted(ident.items(), key=lambda kv: -kv[1]["size"])[:3]
        log("start: %d files, %s from %s (largest: %s); skipped Task 6 recovery folder %s; Postgres %s; free %s; compressor %s" % (
            len(ident), gb(size), d, ", ".join("%s %s" % (k, gb(v["size"])) for k, v in biggest), gb(skipped),
            pg["Config"]["Image"].split("@")[0], gb(free()), "zstd" if ZSTD else "gzip"))
    finally:
        os.close(fd)
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "work", BASE], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    time.sleep(3)
    print("STARTED in the background. Progress so far:")
    print(open(PROGRESS).read().rstrip())
    print("Run 'status' to follow it. Nothing in staging or production is changed.")
    return 0

def pg_restore_test(image, dumps, source_counts):
    shutil.rmtree(PGTEST_DIR, ignore_errors=True); os.makedirs(PGTEST_DIR)
    subprocess.run(["docker", "rm", "-f", PGTEST], capture_output=True)
    cfg = json.loads(docker("image", "inspect", image))[0]["Config"]
    pgdata = next((e.split("=", 1)[1] for e in cfg.get("Env") or [] if e.startswith("PGDATA=")), "/var/lib/postgresql/data")
    try:
        run(["docker", "run", "-d", "--name", PGTEST, "--network", "none", "--memory", "512m", "--cpus", "1",
             "--label", "nexus-ops=staging-backup-restore-test", "-e", "POSTGRES_PASSWORD=" + secrets.token_hex(16),
             "-v", "%s:%s" % (PGTEST_DIR, pgdata), image], timeout=120)
        # The image's entrypoint runs a temporary server for initdb, stops it, then starts the real one.
        for _ in range(120):
            logs = subprocess.run(["docker", "logs", PGTEST], capture_output=True)
            done = b"init process complete" in logs.stdout + logs.stderr
            if done and subprocess.run(["docker", "exec", "-u", "postgres", PGTEST, "psql", "-At", "-U", "postgres", "-c", "select 1"],
                                       capture_output=True).returncode == 0:
                break
            if json.loads(docker("inspect", PGTEST))[0]["State"]["Status"] != "running":
                raise RuntimeError("throwaway Postgres stopped during start-up")
            time.sleep(2)
        else:
            raise RuntimeError("throwaway Postgres did not become ready")
        g = subprocess.run(["docker", "exec", "-i", "-u", "postgres", PGTEST, "psql", "-X", "-q", "-U", "postgres", "-d", "postgres", "-f", "-"],
                           input=open(dumps["globals"], "rb").read(), capture_output=True, timeout=300)
        errors = [l for l in g.stderr.decode(errors="replace").splitlines() if "ERROR" in l]
        unexpected = [l for l in errors if "already exists" not in l]
        if unexpected: raise RuntimeError("roles did not load: %s" % unexpected[0][-160:])
        restored = {}
        for db, path in dumps["databases"].items():
            if db != "postgres":
                run(["docker", "exec", "-u", "postgres", PGTEST, "createdb", "-U", "postgres", db], timeout=120)
            run(["docker", "exec", "-i", "-u", "postgres", PGTEST, "pg_restore", "-U", "postgres", "--exit-on-error", "-d", db],
                timeout=1200, input=open(path, "rb").read())
            restored[db] = counts(PGTEST, db)
            missing = sorted(set(source_counts[db]) - set(restored[db]))
            if missing: raise RuntimeError("tables missing after restore in %s: %s" % (db, ", ".join(missing[:5])))
        return restored
    finally:
        subprocess.run(["docker", "rm", "-f", PGTEST], capture_output=True)
        shutil.rmtree(PGTEST_DIR, ignore_errors=True)

def work():
    fd = held()
    if fd is None: return
    started = time.time()
    try:
        d, ident0, skipped, pg = preflight()
        sums = {}
        for f in sorted(ident0):
            sums[f] = sha256(os.path.join(d, f))
        log("source: SHA-256 of %d files recorded (%s %s)" % (len(sums), MUST_HAVE, sums[MUST_HAVE][:16]))
        part = ARCHIVE + ".part"
        listing = os.path.join(BASE, "files.list")
        with open(listing, "wb") as f: f.write(b"".join(n.encode() + b"\0" for n in sorted(ident0)))
        tar = subprocess.Popen(NICE + ["tar", "-C", d, "--null", "--no-recursion", "-T", listing, "-cf", "-"], stdout=subprocess.PIPE)
        comp = subprocess.Popen(NICE + ([ZSTD, "-T2", "-3", "-q", "-o", part] if ZSTD else ["sh", "-c", "gzip -6 > \"$0\"", part]), stdin=tar.stdout)
        tar.stdout.close()
        if comp.wait() or tar.wait(): raise RuntimeError("packing failed")
        _, ident1, _, _ = preflight()
        if ident1 != ident0: raise RuntimeError("the source files changed while packing")
        os.replace(part, ARCHIVE)
        asha = sha256(ARCHIVE); asize = os.path.getsize(ARCHIVE)
        log("archive %s %s sha256 %s" % (os.path.basename(ARCHIVE), gb(asize), asha[:16]))

        os.makedirs(PGDIR)
        dbs = [x for x in psql(PG, "postgres", "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1;").split() if x]
        dumps = {"globals": os.path.join(PGDIR, "globals.sql"), "databases": {}}
        with open(dumps["globals"], "wb") as f:
            f.write(run(["docker", "exec", "-u", "postgres", PG, "pg_dumpall", "-U", "postgres", "--globals-only"], timeout=300))
        source_counts = {}
        for db in dbs:
            path = os.path.join(PGDIR, "db-%s.dump" % re.sub(r"[^A-Za-z0-9_.-]", "_", db))
            with open(path, "wb") as f:
                f.write(run(["docker", "exec", "-u", "postgres", PG, "pg_dump", "-U", "postgres", "-Fc", "-d", db], timeout=1200))
            dumps["databases"][db] = path
            source_counts[db] = counts(PG, db)
        pg_files = {os.path.relpath(p, BASE): {"bytes": os.path.getsize(p), "sha256": sha256(p)}
                    for p in [dumps["globals"]] + list(dumps["databases"].values())}
        log("postgres: dumped roles and %d databases (%s), %s; %d tables, %d rows counted just after the dump" % (
            len(dbs), ", ".join(dbs), gb(sum(v["bytes"] for v in pg_files.values())),
            sum(len(v) for v in source_counts.values()), sum(sum(v.values()) for v in source_counts.values())))

        os.makedirs(RESTORE)
        dec = subprocess.Popen(NICE + ([ZSTD, "-d", "-q", "-c", ARCHIVE] if ZSTD else ["gzip", "-dc", ARCHIVE]), stdout=subprocess.PIPE)
        un = subprocess.Popen(NICE + ["tar", "-C", RESTORE, "-xf", "-"], stdin=dec.stdout)
        dec.stdout.close()
        if un.wait() or dec.wait(): raise RuntimeError("unpacking failed")
        for f in ident0:
            if sha256(os.path.join(RESTORE, f)) != sums[f]: raise RuntimeError("restored %s differs from the original" % f)
        log("restore: all %d files byte-identical to the originals" % len(ident0))
        sqlite_results = {}
        for f in sorted(ident0):
            p = os.path.join(RESTORE, f)
            if f.endswith(("-wal", "-shm", "-journal")) or not is_sqlite(p): continue
            c = sqlite3.connect(p)
            applied = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[1]   # TRUNCATE would report 0
            integrity = [r[0] for r in c.execute("PRAGMA integrity_check")]
            fk = len(c.execute("PRAGMA foreign_key_check").fetchall())
            tables = {t: c.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
                      for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
            c.close()
            if integrity != ["ok"]: raise RuntimeError("integrity_check failed on restored %s: %s" % (f, integrity[:2]))
            sqlite_results[f] = {"integrity_check": "ok", "foreign_key_violations": fk, "wal_frames_applied": max(applied, 0),
                                 "tables": len(tables), "rows": sum(tables.values())}
        big = sqlite_results.get(MUST_HAVE, {})
        log("restore: %d SQLite databases pass integrity_check; %s: %d WAL frames applied, %d tables, %d rows, %d foreign key violations" % (
            len(sqlite_results), MUST_HAVE, big.get("wal_frames_applied", 0), big.get("tables", 0), big.get("rows", 0), big.get("foreign_key_violations", 0)))
        shutil.rmtree(RESTORE, ignore_errors=True)

        restored_counts = pg_restore_test(pg["Image"], dumps, source_counts)
        changed = sum(1 for db in source_counts for t in source_counts[db] if restored_counts[db].get(t) != source_counts[db][t])
        log("restore: Postgres dump loaded into a throwaway container (no network): all %d tables back, %d rows; %d tables differ from the count taken just after the dump (the research worker keeps writing)" % (
            sum(len(v) for v in restored_counts.values()), sum(sum(v.values()) for v in restored_counts.values()), changed))

        manifest = {"source_volume": VOLUME, "source": {f: dict(ident0[f], sha256=sums[f]) for f in ident0},
                    "skipped_task6_recovery_bytes": skipped, "archive": ARCHIVE, "archive_bytes": asize, "archive_sha256": asha,
                    "compressor": "zstd -3" if ZSTD else "gzip -6", "postgres_image": pg["Image"], "postgres_files": pg_files,
                    "postgres_counts_after_dump": source_counts,
                    "restore_test": {"sha256_match": True, "sqlite": sqlite_results, "postgres_restored_counts": restored_counts, "at": now()}}
        with open(MANIFEST + ".tmp", "w") as f: json.dump(manifest, f, indent=1)
        os.replace(MANIFEST + ".tmp", MANIFEST)
        result = {"status": "OK", "seconds": int(time.time() - started), "archive_bytes": asize}
    except Exception as e:
        result = {"status": "FAILED", "reason": "%s: %s" % (type(e).__name__, e), "seconds": int(time.time() - started)}
        if os.path.exists(ARCHIVE + ".part"): os.remove(ARCHIVE + ".part")
    finally:
        shutil.rmtree(RESTORE, ignore_errors=True)
        shutil.rmtree(PGTEST_DIR, ignore_errors=True)
    log("finished: %s; temporary restored copies deleted; free %s" % (result.get("status") + (" (" + result.get("reason", "") + ")" if result.get("reason") else ""), gb(free())))
    with open(RESULT, "w") as f: json.dump(result, f)
    os.close(fd)

def status():
    running = held()
    if running is not None: os.close(running)
    if os.path.exists(PROGRESS): print(open(PROGRESS).read().rstrip())
    else: print("no backup has been started")
    if running is None: print("STILL RUNNING")
    elif os.path.exists(RESULT):
        r = json.load(open(RESULT))
        print("RESULT: %s" % r["status"] + ("" if r["status"] == "OK" else " - " + r.get("reason", "")))
        if r["status"] == "OK":
            m = json.load(open(MANIFEST))
            pgb = sum(v["bytes"] for v in m["postgres_files"].values())
            print("verified backup: %s (%s), sha256 %s, plus Postgres dumps (%s); restore test at %s passed" % (
                m["archive"], gb(m["archive_bytes"]), m["archive_sha256"][:16], gb(pgb), m["restore_test"]["at"]))
    print("Nothing was changed by this status check.")
    return 0

try:
    if MODE == "start": sys.exit(start())
    elif MODE == "status": sys.exit(status())
    elif MODE == "work": work()
except Blocked as e:
    print("BLOCKED: %s. Nothing was changed." % e); sys.exit(2)
PY
exec python3 -I "$BASE/staging_backup.py" "$1" "$BASE"

