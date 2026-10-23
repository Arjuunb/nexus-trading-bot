#!/bin/sh
# Verified backup of the STOPPED staging Price Action database (the originals are only read).
#
#   start    check that it is safe, then start the backup in the background and return; it keeps
#            running if the GitHub connection ends
#   status   read-only: progress, or the final result
#
# The backup:
#   1. Records the identity and SHA-256 of price_action_paper.db, -wal and -shm (staging stopped,
#      no running container uses the volume, no process holds any file in it).
#   2. Packs exactly those three files into one compressed archive under /var/lib/nexus-ops/pa-backup.
#   3. Confirms the originals did not change while packing.
#   4. Restore test in a temporary folder:
#      - unpack it; every restored file must match the original SHA-256 byte for byte
#      - open the restored copy, so SQLite applies its committed WAL transactions, and record how
#        many frames it applied
#      - run the full PRAGMA integrity_check and foreign_key_check
#      - count the rows of every table
#   5. Writes manifest.json only if all of that passed, then deletes the temporary restored copy.
# It never opens, writes, locks or checkpoints the original files, and never touches production,
# SMC or any other file.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in start|status) ;; *) echo "usage: pa-backup.sh start|status"; exit 64 ;; esac
BASE=${STAGING_PA_TEST_BACKUP:-/var/lib/nexus-ops/pa-backup}
mkdir -p "$BASE" && chmod 700 "$BASE"
cat > "$BASE/pa_backup.py" <<'PY'
import fcntl, hashlib, json, os, shutil, sqlite3, stat, subprocess, sys, time

MODE, BASE = sys.argv[1], sys.argv[2]
PROJECT = "nexus-remote-stage-rc3-28acdbc"
API = PROJECT + "-api-1"
VOLUME = PROJECT + "_api-state"
DB = "price_action_paper.db"
TARGETS = (DB, DB + "-wal", DB + "-shm")
MANIFEST = os.path.join(BASE, "manifest.json")
RESULT = os.path.join(BASE, "result.json")
PROGRESS = os.path.join(BASE, "progress.log")
LOCK = os.path.join(BASE, ".lock")
RESTORE = os.path.join(BASE, "restore-test")
MARGIN = int(os.environ.get("STAGING_PA_TEST_MARGIN", 4 * 10**9))
ZSTD = shutil.which("zstd")
ARCHIVE = os.path.join(BASE, "price_action_paper.tar." + ("zst" if ZSTD else "gz"))
NICE = ["nice", "-n", "19", "ionice", "-c", "2", "-n", "7"]

def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def gb(n): return "%.2f GB" % (n / 1e9)
def log(msg):
    with open(PROGRESS, "a") as f: f.write("%s %s\n" % (now(), msg))
def free(path=BASE):
    s = os.statvfs(path); return s.f_bavail * s.f_frsize
def docker(*a):
    r = subprocess.run(["docker", *a], capture_output=True, text=True, timeout=60)
    if r.returncode: raise RuntimeError("docker %s failed" % a[0])
    return r.stdout

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
            raise Blocked("running container %s uses the staging volume" % c["Name"].lstrip("/"))
    for pid in os.listdir("/proc"):
        if not pid.isdigit(): continue
        try: fds = os.listdir("/proc/%s/fd" % pid)
        except OSError: continue
        for fd in fds:
            try: t = os.readlink("/proc/%s/fd/%s" % (pid, fd))
            except OSError: continue
            if t.startswith(d + "/"): raise Blocked("process %s holds %s open" % (pid, t))
    ident = {}
    for f in TARGETS:
        p = os.path.join(d, f)
        if os.path.lexists(p):
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1: raise Blocked("%s is not a single-link regular file" % f)
            ident[f] = {"inode": st.st_ino, "size": st.st_size, "mtime": int(st.st_mtime), "mtime_ns": st.st_mtime_ns}
    if DB not in ident: raise Blocked("%s is not there" % DB)
    return d, ident

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""): h.update(b)
    return h.hexdigest()

def held():
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return fd
    except OSError:
        os.close(fd); return None

def start():
    fd = held()
    if fd is None: print("a backup is already running; use 'status'"); return 0
    try:
        if os.path.exists(MANIFEST):
            print("a verified backup already exists (manifest.json); use 'status'"); return 0
        d, ident = preflight()
        size = sum(i["size"] for i in ident.values())
        need = int(size * 0.25) + size + MARGIN
        if free() < need:
            raise Blocked("not enough free disk: need %s (archive, temporary restore test, margin), have %s" % (gb(need), gb(free())))
        for p in (RESULT, PROGRESS, ARCHIVE + ".part"):
            if os.path.exists(p): os.remove(p)
        shutil.rmtree(RESTORE, ignore_errors=True)
        log("start: %s in %s; free %s; compressor %s" % (", ".join("%s %s" % (f, gb(i["size"])) for f, i in ident.items()), d, gb(free()), "zstd" if ZSTD else "gzip"))
    finally:
        os.close(fd)
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "work", BASE], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    time.sleep(3)
    print("STARTED in the background. Progress so far:")
    print(open(PROGRESS).read().rstrip())
    print("Run 'status' to follow it. Nothing in staging or production is changed.")
    return 0

def work():
    fd = held()
    if fd is None: return
    started = time.time()
    try:
        d, ident0 = preflight()
        sums = {}
        for f in ident0:
            sums[f] = sha256(os.path.join(d, f)); log("source %s sha256 %s" % (f, sums[f][:16]))
        part = ARCHIVE + ".part"
        tar = subprocess.Popen(NICE + ["tar", "-C", d, "-cf", "-", *ident0], stdout=subprocess.PIPE)
        comp = subprocess.Popen(NICE + ([ZSTD, "-T2", "-3", "-q", "-o", part] if ZSTD else ["sh", "-c", "gzip -6 > \"$0\"", part]), stdin=tar.stdout)
        tar.stdout.close()
        if comp.wait() or tar.wait(): raise RuntimeError("packing failed")
        d1, ident1 = preflight()
        if ident1 != ident0: raise RuntimeError("the source files changed while packing")
        os.replace(part, ARCHIVE)
        asha = sha256(ARCHIVE); asize = os.path.getsize(ARCHIVE)
        log("archive %s %s sha256 %s" % (os.path.basename(ARCHIVE), gb(asize), asha[:16]))
        os.makedirs(RESTORE)
        dec = subprocess.Popen(NICE + ([ZSTD, "-d", "-q", "-c", ARCHIVE] if ZSTD else ["gzip", "-dc", ARCHIVE]), stdout=subprocess.PIPE)
        un = subprocess.Popen(NICE + ["tar", "-C", RESTORE, "-xf", "-"], stdin=dec.stdout)
        dec.stdout.close()
        if un.wait() or dec.wait(): raise RuntimeError("unpacking failed")
        for f in ident0:
            if sha256(os.path.join(RESTORE, f)) != sums[f]: raise RuntimeError("restored %s differs from the original" % f)
        log("restore: all %d files byte-identical to the originals" % len(ident0))
        c = sqlite3.connect(os.path.join(RESTORE, DB))
        applied = c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[1]   # TRUNCATE would report 0
        log("restore: SQLite applied %d committed WAL frames to the restored copy" % applied)
        integrity = [r[0] for r in c.execute("PRAGMA integrity_check")]
        fk = len(c.execute("PRAGMA foreign_key_check").fetchall())
        tables = {t: c.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
                  for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
        c.close()
        ok = integrity == ["ok"]
        log("restore: integrity_check %s; foreign key violations %d; %d tables, %d rows" % ("ok" if ok else integrity[:3], fk, len(tables), sum(tables.values())))
        if not ok: raise RuntimeError("integrity_check failed on the restored copy")
        manifest = {"source": {f: dict(ident0[f], sha256=sums[f]) for f in ident0}, "archive": ARCHIVE,
                    "archive_bytes": asize, "archive_sha256": asha, "compressor": "zstd -3" if ZSTD else "gzip -6",
                    "restore_test": {"integrity_check": "ok", "foreign_key_violations": fk, "sha256_match": True,
                                     "wal_frames_applied": applied, "tables": tables, "at": now()}}
        with open(MANIFEST + ".tmp", "w") as f: json.dump(manifest, f, indent=1)
        os.replace(MANIFEST + ".tmp", MANIFEST)
        result = {"status": "OK", "seconds": int(time.time() - started), "archive_bytes": asize, "wal_frames_applied": applied}
    except Exception as e:
        result = {"status": "FAILED", "reason": "%s: %s" % (type(e).__name__, e), "seconds": int(time.time() - started)}
        if os.path.exists(ARCHIVE + ".part"): os.remove(ARCHIVE + ".part")
    finally:
        shutil.rmtree(RESTORE, ignore_errors=True)
    log("finished: %s; temporary restored copy deleted; free %s" % (result.get("status") + (" (" + result.get("reason", "") + ")" if result.get("reason") else ""), gb(free())))
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
            print("verified backup: %s (%s), sha256 %s; restore test at %s: integrity_check ok, %d WAL frames applied, %d tables, %d rows" % (
                m["archive"], gb(m["archive_bytes"]), m["archive_sha256"][:16], m["restore_test"]["at"],
                m["restore_test"]["wal_frames_applied"], len(m["restore_test"]["tables"]), sum(m["restore_test"]["tables"].values())))
    print("Nothing was changed by this status check.")
    return 0

try:
    if MODE == "start": sys.exit(start())
    elif MODE == "status": sys.exit(status())
    elif MODE == "work": work()
except Blocked as e:
    print("BLOCKED: %s. Nothing was changed." % e); sys.exit(2)
PY
exec python3 -I "$BASE/pa_backup.py" "$1" "$BASE"
