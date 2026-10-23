#!/bin/sh
# Staging Price Action lab database: read-only safety check, and a gated delete.
#
#   check    READ-ONLY. Verifies every safety and recovery gate and prints the exact deletion scope.
#            Ends with "SAFE FOR SEPARATE DELETION APPROVAL" or "BLOCKED: <reasons>".
#   delete   DISABLED in this version: re-runs every gate, then always stops without deleting.
#            Deleting needs a separately reviewed release after the owner's explicit approval.
#
# Targets (only these, in the STAGING api-state volume): price_action_paper.db and its -wal / -shm.
# Gates: staging API and every container using that volume stopped; no process holds any file in the
# volume open; exact volume/path identity, regular files, no hard links; production volume separate;
# SQLite header and PA schema confirmed; no committed WAL frames still waiting for a checkpoint; a
# verified, restore-tested backup of the current files. The check opens the database only with
# SQLite's immutable read-only mode and reads WAL/SHM headers directly: it never writes, locks,
# checkpoints or recovers anything. Production, its PA Lab, SMC, journals and every other file are
# never touched.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in check|delete) ;; *) echo "usage: staging-pa-delete.sh check|delete"; exit 64 ;; esac
exec python3 -I - "$1" "${STAGING_PA_TEST_STATE:-/var/lib/nexus-ops/staging-pa}" \
    "${STAGING_PA_TEST_BACKUP:-/var/lib/nexus-ops/pa-backup}" <<'PY'
import hashlib, json, os, re, sqlite3, stat, struct, subprocess, sys, time, urllib.parse, urllib.request, zlib

MODE, STATE, BACKUP = sys.argv[1], sys.argv[2], sys.argv[3]
PROJECT = "nexus-remote-stage-rc3-28acdbc"
API = PROJECT + "-api-1"
VOLUME = PROJECT + "_api-state"
PROD = "nexus-trading-bot-app-1"
PROD_VOLUME = "nexus-migration-tradexa-data-cutover-20260913"
DB = "price_action_paper.db"
TARGETS = (DB, DB + "-wal", DB + "-shm")
os.makedirs(STATE, mode=0o700, exist_ok=True)
gates, notes = [], []

def say(*a): print(*a, flush=True)
def gb(n): return "%.2f GB" % (n / 1e9)
def gate(name, ok, detail):
    gates.append((name, bool(ok), detail))
    say("  [%s] %s: %s" % ("PASS" if ok else "FAIL", name, detail))
def docker(*args):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("docker %s failed: %s" % (args[0], r.stderr.strip()[:160]))
    return r.stdout
def inspect(kind, name):
    try:
        return json.loads(docker(kind, "inspect", name))[0]
    except Exception:
        return None
def free_bytes(path="/"):
    s = os.statvfs(path); return s.f_bavail * s.f_frsize

def identity():
    out = {}
    for f in TARGETS:
        p = os.path.join(DIR, f)
        if os.path.lexists(p):
            st = os.lstat(p)
            out[f] = {"inode": st.st_ino, "dev": st.st_dev, "size": st.st_size, "alloc": st.st_blocks * 512,
                      "uid": st.st_uid, "gid": st.st_gid, "mode": oct(stat.S_IMODE(st.st_mode)),
                      "nlink": st.st_nlink, "regular": stat.S_ISREG(st.st_mode), "mtime": int(st.st_mtime)}
    return out

def wal_state(wal, shm):
    """Read WAL frames and the SHM index header directly; count committed frames not yet checkpointed."""
    res = {"wal_bytes": 0, "valid_frames": 0, "committed_frames": 0, "backfilled": None, "outstanding_frames": None}
    if not os.path.exists(wal) or os.path.getsize(wal) == 0:
        res["outstanding_frames"] = 0
        return res
    res["wal_bytes"] = os.path.getsize(wal)
    with open(wal, "rb") as f:
        hdr = f.read(32)
        if len(hdr) < 32:
            res["outstanding_frames"] = 0; return res
        magic, ver, psz, ckseq, salt1, salt2, c1, c2 = struct.unpack(">8I", hdr)
        if magic not in (0x377F0682, 0x377F0683):
            res["error"] = "bad WAL magic"; return res
        big = magic & 1
        fmt = (">" if big else "<")
        def ck(data, s1, s2):
            w = struct.unpack(fmt + "%dI" % (len(data) // 4), data)
            for i in range(0, len(w), 2):
                s1 = (s1 + w[i] + s2) & 0xFFFFFFFF
                s2 = (s2 + w[i + 1] + s1) & 0xFFFFFFFF
            return s1, s2
        if ck(hdr[:24], 0, 0) != (c1, c2):
            res["error"] = "WAL header checksum mismatch (empty/reset WAL)"; res["outstanding_frames"] = 0; return res
        s1, s2, n, last_commit = c1, c2, 0, 0
        while True:
            fh = f.read(24)
            if len(fh) < 24: break
            page = f.read(psz)
            if len(page) < psz: break
            pgno, commit, fs1, fs2, k1, k2 = struct.unpack(">6I", fh)
            if (fs1, fs2) != (salt1, salt2): break
            s1, s2 = ck(fh[:8], s1, s2)
            s1, s2 = ck(page, s1, s2)
            if (s1, s2) != (k1, k2): break
            n += 1
            if commit: last_commit = n
        res.update(valid_frames=n, committed_frames=last_commit, page_size=psz, checkpoint_seq=ckseq)
    backfill = None
    if os.path.exists(shm) and os.path.getsize(shm) >= 136:
        with open(shm, "rb") as f:
            h = f.read(136)
        is_init, mx, salt_a, salt_b = h[12], struct.unpack("<I", h[16:20])[0], *struct.unpack("<2I", h[32:40])
        if h[0:48] == h[48:96] and is_init == 1 and (salt_a, salt_b) == (salt1, salt2):
            backfill = struct.unpack("<I", h[96:100])[0]
            res["shm_max_frame"] = mx
    res["backfilled"] = backfill
    res["outstanding_frames"] = last_commit - (backfill or 0) if backfill is None or last_commit >= backfill else 0
    return res

def compression_estimate(path, samples=16, chunk=4 << 20):
    size = os.path.getsize(path)
    if size == 0: return None
    raw = comp = 0
    with open(path, "rb") as f:
        for i in range(samples):
            f.seek(int(i * max(0, size - chunk) / max(1, samples - 1)))
            data = f.read(chunk)
            raw += len(data); comp += len(zlib.compress(data, 6))
    return comp / raw if raw else None

def find_backups():
    """Look for anything that could be a backup of the staging PA database; verify only our manifest format."""
    found = []
    roots = [(os.path.join(DIR, d), 3) for d in ("backups", "verified-backup", "verified-restore")]
    roots += [(os.path.join(DIR, d), 4) for d in sorted(os.listdir(DIR)) if d.startswith(".task6-sqlite-recovery-")]
    roots += [("/home/ubuntu", 4), ("/root", 3), ("/var/backups", 2), ("/var/lib/nexus-backup", 2), (BACKUP, 1)]
    seen = 0
    for root, depth in roots:
        if not os.path.isdir(root): continue
        base = root.rstrip("/").count("/")
        for d, dirs, files in os.walk(root):
            if d.count("/") - base >= depth: dirs[:] = []
            for name in files:
                seen += 1
                if seen > 200000: break
                p = os.path.join(d, name)
                if "price_action_paper" in name:
                    try: found.append((p, os.path.getsize(p)))
                    except OSError: pass
                elif name in ("manifest.json", "receipt.json") and os.path.getsize(p) < 4 << 20:
                    try:
                        if "price_action_paper" in open(p, errors="replace").read(): found.append((p, os.path.getsize(p)))
                    except OSError: pass
    return found

MANIFEST = {}

def verify_manifest(ident):
    m = os.path.join(BACKUP, "manifest.json")
    if not os.path.isfile(m): return False, "no verified backup manifest at %s" % m
    try: man = json.load(open(m))
    except ValueError: return False, "backup manifest unreadable"
    MANIFEST.update(man)
    for f in (DB, DB + "-wal"):
        want, have = man.get("source", {}).get(f, {}), ident.get(f, {})
        if (f in ident) != (f in man.get("source", {})) or (f in ident and any(
                want.get(k) != have.get(k) for k in ("inode", "size", "mtime") if k in want)):
            return False, "backup was made from different files than the current %s" % f
    if man.get("restore_test", {}).get("integrity_check") != "ok":
        return False, "backup has no passed restore test (integrity_check)"
    arch = man.get("archive", "")
    if not (os.path.isfile(arch) and os.path.getsize(arch) == man.get("archive_bytes")):
        return False, "backup archive missing or wrong size"
    h = hashlib.sha256()
    with open(arch, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    if h.hexdigest() != man.get("archive_sha256"):
        return False, "backup archive checksum mismatch"
    return True, "verified: archive %s (%s), sha256 matches, restore test integrity_check ok at %s" % (
        arch, gb(man.get("archive_bytes", 0)), man.get("restore_test", {}).get("at", "?"))

# ------------------------------------------------------------------------------------------------
say("== staging Price Action database: %s (%s)" % ("read-only check" if MODE == "check" else "gated delete", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
if os.geteuid() != 0:
    say("BLOCKED: must run as root"); sys.exit(2)

vol = inspect("volume", VOLUME)
if not vol:
    say("BLOCKED: volume %s not found" % VOLUME); sys.exit(2)
DIR = vol["Mountpoint"]
say("volume: %s  driver=%s  project=%s  created=%s" % (VOLUME, vol.get("Driver"), (vol.get("Labels") or {}).get("com.docker.compose.project"), vol.get("CreatedAt", "")[:19]))
say("folder: %s" % DIR)

say("-- containers and services")
containers = [json.loads(l) for l in docker("ps", "-a", "--no-trunc", "--format", "{{json .}}").splitlines() if l.strip()]
users, running_users = [], []
for c in containers:
    full = inspect("container", c["ID"]) or {}
    mounts = [m.get("Name") or m.get("Source") for m in full.get("Mounts", [])]
    if VOLUME in mounts or any((m.get("Source") or "").startswith(DIR) for m in full.get("Mounts", [])):
        users.append(c["Names"])
        if full.get("State", {}).get("Running"): running_users.append(c["Names"])
    if PROJECT in c["Names"]:
        say("  %s: %s%s" % (c["Names"], c["State"], " (uses this volume)" if c["Names"] in users else ""))
say("  containers inspected: %d; using this volume: %s" % (len(containers), ", ".join(users) or "none"))
api = inspect("container", API)
gate("staging API stopped", api is not None and not api["State"]["Running"], "%s is %s" % (API, api["State"]["Status"] if api else "missing"))
gate("no running container uses the volume", not running_users, ", ".join(running_users) or "none running (stopped users: %s)" % (", ".join(users) or "none"))

say("-- processes")
holders, pids, unreadable = [], 0, 0
for pid in os.listdir("/proc"):
    if not pid.isdigit(): continue
    pids += 1
    try: fds = os.listdir("/proc/%s/fd" % pid)
    except OSError: unreadable += 1; continue
    for fd in fds:
        try: t = os.readlink("/proc/%s/fd/%s" % (pid, fd))
        except OSError: continue
        if t.startswith(DIR + "/"): holders.append("%s:%s" % (pid, t[len(DIR) + 1:]))
gate("no process holds any file in the volume open", not holders,
     "%d processes scanned, %d unreadable; holders: %s" % (pids, unreadable, ", ".join(holders[:8]) or "none"))

say("-- exact targets")
ident = identity()
for f in TARGETS:
    i = ident.get(f)
    say("  %s/%s: %s" % (DIR, f, "absent" if not i else "%s, allocated %s, inode %d, uid:gid %d:%d, mode %s, links %d, modified %s" % (
        gb(i["size"]), gb(i["alloc"]), i["inode"], i["uid"], i["gid"], i["mode"], i["nlink"],
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(i["mtime"])))))
dbi = ident.get(DB)
gate("database present as a regular file", bool(dbi) and dbi["regular"], "present" if dbi else "absent")
gate("targets are regular files with one link each (no hard links, no symlinks)",
     all(i["regular"] and i["nlink"] == 1 for i in ident.values()), "%d target files" % len(ident))
gate("targets live in the staging volume folder", all(os.path.realpath(os.path.join(DIR, f)) == os.path.join(DIR, f) for f in ident) and PROD_VOLUME not in DIR, DIR)

say("-- SQLite identity (immutable read-only open; WAL ignored by design)")
schema_ok, detail = False, "not opened"
if dbi:
    with open(os.path.join(DIR, DB), "rb") as f: h = f.read(100)
    psize = struct.unpack(">H", h[16:18])[0]; psize = 65536 if psize == 1 else psize
    pages = struct.unpack(">I", h[28:32])[0]
    say("  header: SQLite format 3 = %s, page size %d, pages %d (%s), WAL mode = %s" % (h[:16] == b"SQLite format 3\x00", psize, pages, gb(pages * psize), h[18] == 2))
    try:
        c = sqlite3.connect("file:%s?mode=ro&immutable=1" % urllib.parse.quote(os.path.join(DIR, DB)), uri=True)
        names = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
        c.close()
        pa = sorted(n for n in names if n.startswith("pa_")); v2 = sorted(n for n in names if n.startswith("v2_"))
        smc = sorted(n for n in names if "smc" in n.lower())
        schema_ok = bool(pa) and not smc
        detail = "%d tables: %d pa_* (e.g. %s), %d v2_* PA broker tables (incl. v2_source_evidence=%s), %d SMC-named" % (
            len(names), len(pa), ", ".join(pa[:3]), len(v2), "v2_source_evidence" in v2, len(smc))
    except Exception as e:
        detail = "schema read failed: %s" % type(e).__name__
gate("schema is the Price Action paper database (pa_* tables, no SMC tables)", schema_ok, detail)

say("-- WAL (frames read directly; nothing is checkpointed or recovered)")
ws = wal_state(os.path.join(DIR, DB + "-wal"), os.path.join(DIR, DB + "-shm"))
say("  WAL %s; valid frames %d; committed frames %d; checkpointed per SHM index %s" % (
    gb(ws["wal_bytes"]), ws["valid_frames"], ws["committed_frames"], ws["backfilled"] if ws["backfilled"] is not None else "unknown (SHM index not usable)"))
out = ws["outstanding_frames"]
backup_ok, backup_why = verify_manifest(ident)
applied = MANIFEST.get("restore_test", {}).get("wal_frames_applied")
covered = backup_ok and out and applied == ws["committed_frames"]
gate("no committed WAL transactions waiting for a checkpoint, or all of them inside the verified backup", out == 0 or covered,
     "none" if out == 0 else
     "%s committed frames exist only in the WAL; the verified backup's restore test applied exactly %s of them" % (out, applied) if covered else
     "%s committed frames (%s) exist only in the WAL and no verified backup contains them" % (out, gb((out or 0) * ((ws.get("page_size") or 4096) + 24))))

say("-- production isolation (read-only)")
prod = inspect("container", PROD)
pmount = [m for m in (prod or {}).get("Mounts", []) if m.get("Destination") == "/var/lib/tradexa"]
pdir = pmount[0]["Source"] if pmount else ""
pfile = os.path.join(pdir, DB) if pdir else ""
pst = os.stat(pfile) if pfile and os.path.exists(pfile) else None
sep = bool(pmount) and pmount[0].get("Name") == PROD_VOLUME and pdir != DIR and not pdir.startswith(DIR) and not DIR.startswith(pdir) \
    and (pst is None or not dbi or (pst.st_ino, pst.st_dev) != (dbi["inode"], dbi["dev"]))
gate("production uses a different volume and a different PA file", sep,
     "production %s mounts %s at %s; its %s is %s (inode %s) and is NOT a target" % (
         PROD, pmount[0].get("Name") if pmount else "?", pdir, DB, gb(pst.st_size) if pst else "absent", pst.st_ino if pst else "-"))
if prod:
    say("  production container: %s, health %s, restarts %s" % (prod["State"]["Status"], (prod["State"].get("Health") or {}).get("Status", "-"), prod.get("RestartCount")))
    try:
        ip = list(prod["NetworkSettings"]["Networks"].values())[0]["IPAddress"]
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        s = json.load(op.open("http://%s:8000/status/public" % ip, timeout=30))
        say("  production status: %s; %s" % (s.get("overall"), "; ".join("%s %s (%s)" % (x.get("id"), x.get("state"), x.get("detail")) for x in s.get("components", []))))
    except Exception as e:
        say("  production status: no answer (%s)" % type(e).__name__)

say("-- excluded from deletion (kept in the staging volume)")
for name in sorted(os.listdir(DIR)):
    if name in TARGETS: continue
    p = os.path.join(DIR, name)
    try: size = os.path.getsize(p) if os.path.isfile(p) else sum(os.path.getsize(os.path.join(a, b)) for a, _, bs in os.walk(p) for b in bs)
    except OSError: size = 0
    say("  %s%s  %s" % (name, "/" if os.path.isdir(p) else "", gb(size)))

say("-- backup and recovery")
cands = find_backups()
for p, s in cands[:20]:
    say("  found (not verified by this check unless listed below): %s  %s" % (p, gb(s)))
if not cands: say("  no file mentioning price_action_paper found in the staging volume backup folders, /home/ubuntu, /root, /var/backups or %s" % BACKUP)
gate("verified, restore-tested backup of the current files", backup_ok, backup_why)
ratio = compression_estimate(os.path.join(DIR, DB)) if dbi else None
logical = (dbi or {}).get("size", 0) + ident.get(DB + "-wal", {}).get("size", 0)
if ratio:
    say("  backup sizing: database + WAL %s; sampled compression ratio %.2f -> archive about %s; a restore test needs %s more, temporarily; free now %s" % (
        gb(logical), ratio, gb(logical * ratio), gb(logical), gb(free_bytes())))

say("-- scope")
total = sum(i["alloc"] for i in ident.values())
say("  deletion would remove exactly: %s" % ", ".join("%s/%s" % (DIR, f) for f in ident))
say("  disk space it would free: %s (allocated blocks)" % gb(total))
failed = [g[0] for g in gates if not g[1]]
verdict = {"time": time.time(), "safe": not failed, "failed": failed, "identity": ident}
with open(os.path.join(STATE, "verdict.json"), "w") as f: json.dump(verdict, f)

if MODE == "check":
    say("")
    if failed:
        say("BLOCKED: " + "; ".join(failed))
        say("Nothing was changed. Deletion is not possible until every gate passes.")
        sys.exit(2)
    say("SAFE FOR SEPARATE DELETION APPROVAL")
    say("Nothing was changed. This is not permission to delete; deletion needs a separate, explicit approval.")
    sys.exit(0)

# MODE == delete: every gate was re-checked above. Deleting stays disabled in this version.
if failed:
    say("BLOCKED: " + "; ".join(failed)); say("Nothing was deleted."); sys.exit(2)
say("BLOCKED: deletion is disabled in this version; it needs a separately reviewed and approved release")
say("Nothing was deleted.")
sys.exit(2)
PY
