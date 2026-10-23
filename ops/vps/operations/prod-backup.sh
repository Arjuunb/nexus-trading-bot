#!/bin/sh
# Verified backup of production's app data before the Strategy Intelligence deploy.
# Production keeps running; nothing in it is stopped, restarted or written.
#
#   start    check that it is safe, then start the backup in the background and return; it keeps
#            running if the GitHub connection ends
#   status   read-only: progress, or the final result
#
# What is backed up: every file in the app's data volume (mounted at /var/lib/tradexa in
# nexus-trading-bot-app-1), except the app's own backups/ folder (older copies of these same files).
#   - each SQLite database through SQLite's online backup: one read transaction per database gives
#     a consistent snapshot that includes committed WAL content, while the app keeps writing
#     (WAL readers do not block writers). The originals are opened read-only and no -wal/-shm
#     file is ever created.
#   - every other file copied as-is, and copied again if it changed while being read
# Also recorded, for rollback: the running app image (ID and tag), the Compose files and their
# SHA-256, and the git commit of the checkout production was started from.
#
# Steps: snapshot each file into a private work folder; integrity_check and row counts on each
# copy; pack into one zstd archive under /var/lib/nexus-ops/prod-backup; delete the work folder;
# restore test (unpack to a temporary folder, every file byte-identical to its snapshot, every
# database passes integrity_check again); write manifest.json only if all of that passed.
# Not included: the Supabase primary ledger (remote; this deploy applies no database migration).
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in start|status) ;; *) echo "usage: prod-backup.sh start|status"; exit 64 ;; esac
BASE=${PROD_BACKUP_TEST_BASE:-/var/lib/nexus-ops/prod-backup}
mkdir -p "$BASE" && chmod 700 "$BASE"
cat > "$BASE/prod_backup.py" <<'PY'
import fcntl, hashlib, json, os, shutil, sqlite3, stat, subprocess, sys, time

MODE, BASE = sys.argv[1], sys.argv[2]
PROJECT = "nexus-trading-bot"
APP = PROJECT + "-app-1"
DATA = "/var/lib/tradexa"
SKIP_DIR = "backups"
MANIFEST = os.path.join(BASE, "manifest.json")
RESULT = os.path.join(BASE, "result.json")
PROGRESS = os.path.join(BASE, "progress.log")
LOCK = os.path.join(BASE, ".lock")
WORK = os.path.join(BASE, "work")
RESTORE = os.path.join(BASE, "restore-test")
MARGIN = int(os.environ.get("PROD_BACKUP_TEST_MARGIN", 4 * 10**9))
ZSTD = shutil.which("zstd")
ARCHIVE = os.path.join(BASE, "app-data.tar." + ("zst" if ZSTD else "gz"))
NICE = ["nice", "-n", "19", "ionice", "-c", "2", "-n", "7"]

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
def docker(*a): return run(["docker", *a], timeout=60).decode()
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""): h.update(b)
    return h.hexdigest()
def is_sqlite(path):
    try:
        with open(path, "rb") as f: return f.read(16) == b"SQLite format 3\x00"
    except OSError: return False

class Blocked(Exception): pass

def preflight():
    try:
        app = json.loads(docker("inspect", APP))[0]
    except RuntimeError:
        raise Blocked("production app container %s not found" % APP)
    if not app["State"]["Running"]: raise Blocked("production app %s is not running" % APP)
    lab = app["Config"].get("Labels") or {}
    if lab.get("com.docker.compose.project") != PROJECT: raise Blocked("%s is not in project %s" % (APP, PROJECT))
    mount = next((m for m in app.get("Mounts", []) if m.get("Destination") == DATA), None)
    if not mount or mount.get("Type") != "volume": raise Blocked("no data volume mounted at %s" % DATA)
    d = mount["Source"]
    files, skipped = {}, 0
    for root, dirs, names in os.walk(d):
        rel_root = os.path.relpath(root, d)
        if rel_root == SKIP_DIR or rel_root.startswith(SKIP_DIR + os.sep):
            skipped += sum(os.lstat(os.path.join(root, n)).st_size for n in names); continue
        for name in names:
            p = os.path.join(root, name); rel = os.path.relpath(p, d)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode): raise Blocked("%s is not a regular file" % rel)
            files[rel] = st.st_size
    wd = lab.get("com.docker.compose.project.working_dir") or ""
    cfg = [f for f in (lab.get("com.docker.compose.project.config_files") or "").split(",") if f]
    rollback = {"app_container": APP, "image_id": app["Image"], "image_ref": app["Config"]["Image"],
                "working_dir": wd, "compose_files": {f: sha256(f) for f in cfg if os.path.isfile(f)},
                "git_head": None, "volume": mount.get("Name"), "mountpoint": d}
    if wd and os.path.isdir(os.path.join(wd, ".git")):
        r = subprocess.run(["git", "--no-optional-locks", "-c", "safe.directory=*", "-C", wd, "rev-parse", "HEAD"], capture_output=True, text=True)
        rollback["git_head"] = r.stdout.strip() or None
    return d, files, skipped, rollback

def held():
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return fd
    except OSError:
        os.close(fd); return None

def snapshot_db(src_path, dst_path):
    """Consistent copy of a live database without creating any file next to the original."""
    wal, shm = os.path.exists(src_path + "-wal"), os.path.exists(src_path + "-shm")
    tmp = None
    if shm:
        uri = "file:%s?mode=ro" % src_path                      # the app has it open: join read-only
    elif not wal:
        uri = "file:%s?mode=ro&immutable=1" % src_path          # nothing in flight
    else:                                                       # -wal without -shm: read a private pair
        tmp = dst_path + ".src"
        for suffix in ("", "-wal"):
            shutil.copy2(src_path + suffix, tmp + suffix)
        uri = "file:%s?mode=ro" % tmp
    src = sqlite3.connect(uri, uri=True, timeout=30)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)                                         # one step: one read transaction
    finally:
        dst.close(); src.close()
        if tmp:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(tmp + suffix): os.remove(tmp + suffix)

def check_db(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        integrity = [r[0] for r in c.execute("PRAGMA integrity_check")]
        tables = {t: c.execute('SELECT count(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
                  for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    finally:
        c.close()
    if integrity != ["ok"]: raise RuntimeError("integrity_check failed on %s: %s" % (os.path.basename(path), integrity[:2]))
    return {"integrity_check": "ok", "tables": len(tables), "rows": sum(tables.values())}

def start():
    fd = held()
    if fd is None: print("a backup is already running; use 'status'"); return 0
    try:
        if os.path.exists(MANIFEST):
            print("a verified backup already exists (manifest.json); use 'status'"); return 0
        d, files, skipped, rollback = preflight()
        size = sum(files.values())
        need = size + int(size * 0.3) + MARGIN
        if free() < need:
            raise Blocked("not enough free disk: need %s (snapshot, archive, margin), have %s" % (gb(need), gb(free())))
        for p in (RESULT, PROGRESS, ARCHIVE + ".part"):
            if os.path.exists(p): os.remove(p)
        for p in (WORK, RESTORE): shutil.rmtree(p, ignore_errors=True)
        biggest = sorted(files.items(), key=lambda kv: -kv[1])[:3]
        log("start: %d files, %s from %s (largest: %s); skipped the app's own backups/ %s; app image %s; checkout %s; free %s; compressor %s" % (
            len(files), gb(size), d, ", ".join("%s %s" % (k, gb(v)) for k, v in biggest), gb(skipped),
            rollback["image_ref"], (rollback["git_head"] or "-")[:12], gb(free()), "zstd" if ZSTD else "gzip"))
    finally:
        os.close(fd)
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "work", BASE], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    time.sleep(3)
    print("STARTED in the background. Progress so far:")
    print(open(PROGRESS).read().rstrip())
    print("Run 'status' to follow it. Production keeps running and is not changed.")
    return 0

def work():
    fd = held()
    if fd is None: return
    started = time.time()
    try:
        d, files, skipped, rollback = preflight()
        os.makedirs(WORK)
        snap, results = {}, {}
        for rel in sorted(files):
            if rel.endswith(("-wal", "-shm", "-journal")): continue    # their content is in the snapshot
            src, dst = os.path.join(d, rel), os.path.join(WORK, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            t0 = time.time()
            if is_sqlite(src):
                snapshot_db(src, dst)
                results[rel] = dict(check_db(dst), seconds=round(time.time() - t0, 1))
            else:
                for attempt in (1, 2, 3):
                    before = sha256(src); shutil.copy2(src, dst)
                    if sha256(dst) == before: break
                else:
                    raise RuntimeError("%s kept changing while being copied" % rel)
            snap[rel] = {"bytes": os.path.getsize(dst), "sha256": sha256(dst)}
        dbs = len(results)
        log("snapshot: %d files (%d SQLite databases, all pass integrity_check; %d rows), %s" % (
            len(snap), dbs, sum(r["rows"] for r in results.values()), gb(sum(v["bytes"] for v in snap.values()))))
        part = ARCHIVE + ".part"
        tar = subprocess.Popen(NICE + ["tar", "-C", WORK, "-cf", "-", "."], stdout=subprocess.PIPE)
        comp = subprocess.Popen(NICE + ([ZSTD, "-T2", "-3", "-q", "-o", part] if ZSTD else ["sh", "-c", "gzip -6 > \"$0\"", part]), stdin=tar.stdout)
        tar.stdout.close()
        if comp.wait() or tar.wait(): raise RuntimeError("packing failed")
        os.replace(part, ARCHIVE)
        shutil.rmtree(WORK, ignore_errors=True)
        asha = sha256(ARCHIVE); asize = os.path.getsize(ARCHIVE)
        log("archive %s %s sha256 %s; work folder deleted" % (os.path.basename(ARCHIVE), gb(asize), asha[:16]))

        os.makedirs(RESTORE)
        dec = subprocess.Popen(NICE + ([ZSTD, "-d", "-q", "-c", ARCHIVE] if ZSTD else ["gzip", "-dc", ARCHIVE]), stdout=subprocess.PIPE)
        un = subprocess.Popen(NICE + ["tar", "-C", RESTORE, "-xf", "-"], stdin=dec.stdout)
        dec.stdout.close()
        if un.wait() or dec.wait(): raise RuntimeError("unpacking failed")
        for rel, v in snap.items():
            if sha256(os.path.join(RESTORE, rel)) != v["sha256"]: raise RuntimeError("restored %s differs from its snapshot" % rel)
        for rel in results:
            check_db(os.path.join(RESTORE, rel))
        log("restore: all %d files byte-identical to their snapshots; all %d databases pass integrity_check again" % (len(snap), dbs))
        shutil.rmtree(RESTORE, ignore_errors=True)

        manifest = {"source_mountpoint": d, "skipped_app_backups_bytes": skipped, "files": snap, "sqlite": results,
                    "archive": ARCHIVE, "archive_bytes": asize, "archive_sha256": asha,
                    "compressor": "zstd -3" if ZSTD else "gzip -6", "rollback": rollback,
                    "restore_test": {"sha256_match": True, "integrity_check": "ok", "at": now()}}
        with open(MANIFEST + ".tmp", "w") as f: json.dump(manifest, f, indent=1)
        os.replace(MANIFEST + ".tmp", MANIFEST)
        result = {"status": "OK", "seconds": int(time.time() - started), "archive_bytes": asize}
    except Exception as e:
        result = {"status": "FAILED", "reason": "%s: %s" % (type(e).__name__, e), "seconds": int(time.time() - started)}
        if os.path.exists(ARCHIVE + ".part"): os.remove(ARCHIVE + ".part")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(RESTORE, ignore_errors=True)
    log("finished: %s; temporary copies deleted; free %s" % (result.get("status") + (" (" + result.get("reason", "") + ")" if result.get("reason") else ""), gb(free())))
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
            rb = m["rollback"]
            print("verified backup: %s (%s), sha256 %s; restore test at %s passed" % (
                m["archive"], gb(m["archive_bytes"]), m["archive_sha256"][:16], m["restore_test"]["at"]))
            print("rollback record: image %s (%s), checkout %s" % (rb["image_ref"], rb["image_id"][:19], (rb["git_head"] or "-")[:12]))
    print("Nothing was changed by this status check.")
    return 0

try:
    if MODE == "start": sys.exit(start())
    elif MODE == "status": sys.exit(status())
    elif MODE == "work": work()
except Blocked as e:
    print("BLOCKED: %s. Nothing was changed." % e); sys.exit(2)
PY
exec python3 -I "$BASE/prod_backup.py" "$1" "$BASE"
