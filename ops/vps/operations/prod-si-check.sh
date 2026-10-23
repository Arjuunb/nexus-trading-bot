#!/bin/sh
# Read-only check of the PRODUCTION app after the Strategy Intelligence deploy, over 10 minutes.
#
#   measure   1. identity: the app runs the verified image with production's start command, healthy,
#                no restarts or OOM
#             2. Strategy Intelligence live, Price Action Lab off: /health, the SI routes, every PA route
#                the retired handler, the switch not on, no PA database file written since the deploy;
#                /health through the web proxy as a visitor reaches it
#             3. the snapshot limit holds: equity snapshots (EQUITY_OBSERVED evidence rows) written in
#                the window, per account, against the 1-per-minute limit; their size per day
#             4. production's own work goes on: the SMC database is written; which databases changed
#             5. resources for 10 minutes (every 15 s, kernel cgroup counters): app CPU, memory, disk;
#                host CPU, memory available, disk
#             6. nginx, certbot and pa-replay-2025 unchanged since the deploy; free disk; app log errors
#             Ends with GO or NO-GO and the reasons.
#
# Changes nothing: databases are opened read-only (never creating -wal/-shm files). Log lines are
# shortened and anything secret-looking is hidden.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in measure) ;; *) echo "usage: prod-si-check.sh measure"; exit 64 ;; esac
exec python3 -I - <<'PY'
import glob, json, os, re, sqlite3, statistics, subprocess, time
from datetime import datetime

PROJECT = "nexus-trading-bot"
APP = PROJECT + "-app-1"
SITE = "trade-logx.com"
WORK = os.environ.get("PROD_SI_TEST_WORK", "/var/lib/nexus-ops/si-production")
SECONDS = int(os.environ.get("PROD_SI_TEST_SECONDS", 600))
STEP = int(os.environ.get("PROD_SI_TEST_STEP", 15))
DATA = "/var/lib/tradexa"
SECRETISH = re.compile(r"(?i)(key|secret|token|password|passwd|authorization|bearer|dsn)\S*\s*[=:]\s*\S+|[a-z]+://\S+@\S+")
results = []

def sh(*a, timeout=120):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr
def inspect(n):
    rc, out, _ = sh("docker", "inspect", n)
    return json.loads(out)[0] if rc == 0 else None
def check(ok, label, detail=""):
    results.append((bool(ok), label))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label, (": " + detail) if detail else ""), flush=True)
def info(msg): print("      " + msg, flush=True)
def in_app(code):
    rc, out, _ = sh("docker", "exec", APP, "python", "-c", code, timeout=90)
    return out.strip() if rc == 0 else ""
def clean(line): return SECRETISH.sub("[hidden]", line.strip())[:160]
def parse_time(s): return datetime.fromisoformat(s[:26].rstrip("Z") + "+00:00").timestamp() if s else 0
def ro(path):
    """Read-only connection that never creates files next to the database, or None."""
    if os.path.exists(path + "-shm"): uri = "file:%s?mode=ro" % path
    elif not os.path.exists(path + "-wal"): uri = "file:%s?immutable=1" % path
    else: return None                                  # a -wal without -shm: do not open in place
    c = sqlite3.connect(uri, uri=True, timeout=10); c.execute("PRAGMA query_only=1")
    return c

def through_proxy():
    import socket, ssl
    req = ("GET /health HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % SITE).encode()
    seen = []
    for port in (443, 80):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            if port == 443:
                ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
                s = ctx.wrap_socket(s, server_hostname=SITE)
            s.sendall(req); data = b""
            while len(data) < 1 << 20:
                b = s.recv(65536)
                if not b: break
                data += b
            s.close()
        except (OSError, ssl.SSLError) as e:
            seen.append("port %d: %s" % (port, type(e).__name__)); continue
        head, _, body = data.partition(b"\r\n\r\n")
        parts = head.split(b" ", 2)
        status = parts[1].decode(errors="replace") if len(parts) > 1 else "?"
        if status == "200" and b'"status"' in body: return "ok (port %d)" % port
        seen.append("port %d: HTTP %s" % (port, status))
    return "NO ANSWER (%s)" % "; ".join(seen)

def cgroup_dir(c):
    try: lines = open("/proc/%d/cgroup" % c["State"]["Pid"]).read().splitlines()
    except (OSError, TypeError): return None
    v1 = {}
    for line in lines:
        hid, ctrl, path = line.split(":", 2)
        if hid == "0" and os.path.isfile("/sys/fs/cgroup%s/cpu.stat" % path): return "/sys/fs/cgroup" + path
        for k in ctrl.split(","):
            if k in ("cpuacct", "memory", "blkio") and os.path.isdir("/sys/fs/cgroup/%s%s" % (k, path)): v1[k] = "/sys/fs/cgroup/%s%s" % (k, path)
    return v1 if len(v1) == 3 else None
def stat_value(path, key):
    for l in open(path):
        f = l.split()
        if f and f[0] == key: return int(f[1])
    return None
def counters(d):
    """(cpu microseconds, memory incl. file cache, read bytes, write bytes, app memory without file cache)"""
    if isinstance(d, dict):
        cpu = int(open(d["cpuacct"] + "/cpuacct.usage").read()) // 1000
        mem = int(open(d["memory"] + "/memory.usage_in_bytes").read())
        anon = stat_value(d["memory"] + "/memory.stat", "total_rss") or stat_value(d["memory"] + "/memory.stat", "rss") or mem
        rb = wb = 0
        for l in open(d["blkio"] + "/blkio.throttle.io_service_bytes"):
            f = l.split()
            if len(f) == 3 and f[1] == "Read": rb += int(f[2])
            elif len(f) == 3 and f[1] == "Write": wb += int(f[2])
        return cpu, mem, rb, wb, anon
    cpu = stat_value(d + "/cpu.stat", "usage_usec")
    mem = int(open(d + "/memory.current").read())
    anon = stat_value(d + "/memory.stat", "anon") or mem
    rb = wb = 0
    try:
        for l in open(d + "/io.stat"):
            for f in l.split()[1:]:
                k, _, v = f.partition("=")
                if k == "rbytes": rb += int(v)
                elif k == "wbytes": wb += int(v)
    except OSError:
        pass
    return cpu, mem, rb, wb, anon
def host():
    cpu = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    rd = wr = 0
    for l in open("/proc/diskstats"):
        f = l.split()
        if re.fullmatch(r"(sd[a-z]|vd[a-z]|nvme\d+n\d+|xvd[a-z])", f[2]): rd += int(f[5]); wr += int(f[9])
    return sum(cpu), cpu[3] + cpu[4], mem["MemAvailable"] * 1024, rd * 512, wr * 512, mem["MemTotal"] * 1024
def free(): s = os.statvfs("/"); return s.f_bavail * s.f_frsize

def files(vol):
    out = {}
    for dp, _, fs in os.walk(vol):
        for f in fs:
            p = os.path.join(dp, f)
            if not os.path.islink(p):
                try: st = os.stat(p); out[os.path.relpath(p, vol)] = (st.st_size, st.st_mtime)
                except OSError: pass
    return out
def evidence_marks(vol):
    """{database: (max rowid of v2_source_evidence)} for every database holding evidence."""
    marks = {}
    for rel in sorted(files(vol)):
        if not rel.endswith(".db") or rel.startswith("backups/"): continue
        try:
            c = ro(os.path.join(vol, rel))
            if c is None: continue
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_source_evidence'").fetchone():
                marks[rel] = c.execute("SELECT coalesce(max(rowid), 0) FROM v2_source_evidence").fetchone()[0]
            c.close()
        except sqlite3.Error:
            pass
    return marks
def equity_since(vol, marks):
    """{database: (equity rows, accounts, payload bytes)} written after the marks."""
    out = {}
    for rel, mark in marks.items():
        try:
            c = ro(os.path.join(vol, rel))
            if c is None: continue
            out[rel] = c.execute("SELECT count(*), count(DISTINCT account_id), coalesce(sum(length(payload_json)), 0) FROM v2_source_evidence "
                                 "WHERE rowid > ? AND event_type = 'EQUITY_OBSERVED'", (mark,)).fetchone()
            c.close()
        except sqlite3.Error as e:
            out[rel] = (None, None, str(e))
    return out

try: rec = json.load(open(os.path.join(WORK, "api-image.json")))
except (OSError, ValueError): rec = {}
try: plan = json.load(open(os.path.join(WORK, "plan.json")))
except (OSError, ValueError): plan = {}
receipts = sorted(glob.glob(os.path.join(WORK, "deployed-*.json")))
deployed_at = parse_time(json.load(open(receipts[-1]))["at"]) if receipts else 0

print("== 1. Identity")
app = inspect(APP)
if not app or not app["State"]["Running"]:
    check(False, "production app is running"); raise SystemExit("NO-GO: the production app is not running. Nothing was changed.")
check(rec.get("image_id") and app["Image"] == rec.get("image_id"), "app runs the verified Strategy Intelligence image", app["Image"][:19])
check(plan.get("command") and app["Config"].get("Cmd") == plan["command"], "app runs production's start command (graceful shutdown kept)")
check((app["State"].get("Health") or {}).get("Status") == "healthy", "app healthy")
check(app.get("RestartCount", 0) == 0 and not app["State"].get("OOMKilled"), "no restarts, not OOM-killed", "restarts %d" % app.get("RestartCount", 0))
check(deployed_at > 0, "deploy receipt found", receipts[-1] if receipts else "none")
info("started %s (%.0f minutes ago)" % (app["State"]["StartedAt"][:19], (time.time() - parse_time(app["State"]["StartedAt"])) / 60))
mount = next((m for m in app.get("Mounts", []) if m.get("Destination") == DATA), {})
vol = mount.get("Source") or ""
check(os.path.isdir(vol), "data volume found", "%s at %s" % (mount.get("Name"), vol))

print("== 2. Strategy Intelligence live, Price Action Lab off")
try: h = json.loads(in_app("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/health',timeout=10)); print(json.dumps({'status':d.get('status'),'pa':(d.get('features') or {}).get('price_action_lab_enabled')}))"))
except ValueError: h = {}
check(h.get("status") == "ok" and h.get("pa") is False, "/health: status ok, price_action_lab_enabled false", json.dumps(h))
try: r = json.loads(in_app("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/openapi.json',timeout=60)); "
                           "ops=[(p,o.get('tags')) for p,i in d.get('paths',{}).items() if '/research/price-action' in p for m,o in i.items() if isinstance(o,dict)]; "
                           "si=[p for p in d.get('paths',{}) if p.startswith('/api/v2/strategy-intelligence')]; "
                           "print(json.dumps({'operations':len(ops),'not_retired':sorted({p for p,t in ops if t!=['retired-price-action']}),'si':len(si)}))"))
except ValueError: r = {}
check(r.get("si"), "Strategy Intelligence routes served", "%s routes" % r.get("si"))
check(r.get("operations") and not r.get("not_retired"), "every PA lab route is the retired handler", "%s operations" % r.get("operations"))
env = dict(e.split("=", 1) for e in app["Config"].get("Env") or [] if "=" in e)
check(env.get("PRICE_ACTION_LAB_ENABLED", "false").strip().lower() in ("", "0", "false", "no", "off"), "PRICE_ACTION_LAB_ENABLED not on", env.get("PRICE_ACTION_LAB_ENABLED", "unset"))
via = through_proxy()
check(via.startswith("ok"), "https://%s/health through the web proxy" % SITE, via)
f0 = files(vol) if os.path.isdir(vol) else {}

def pa_written():
    """PA files (kept, not deleted) written since the deploy; -shm is shared memory a reader may touch."""
    return sorted(k for k, (size, mtime) in files(vol).items()
                  if os.path.basename(k).startswith("price_action") and not k.endswith("-shm") and mtime > deployed_at > 0)

print("== 3/4/5. Measuring for %s (sample every %d s)" % ("%d minutes" % (SECONDS // 60) if SECONDS >= 120 else "%d s" % SECONDS, STEP))
marks = evidence_marks(vol) if os.path.isdir(vol) else {}
d = cgroup_dir(app)
prev = counters(d) if d else None; hprev = host(); tprev = t0 = time.time(); free0 = free()
cpu, mem, anon, rd, wr, hcpu, hmem, hrd, hwr = ([] for _ in range(9))
while time.time() - t0 < SECONDS:
    time.sleep(STEP)
    now = time.time(); dt = now - tprev
    hcur = host()
    if d:
        try:
            cur = counters(d)
            cpu.append((cur[0] - prev[0]) / 1e6 / dt * 100); mem.append(cur[1]); anon.append(cur[4])
            rd.append((cur[2] - prev[2]) / dt); wr.append((cur[3] - prev[3]) / dt); prev = cur
        except (OSError, TypeError):
            d = None
    tot, idle = hcur[0] - hprev[0], hcur[1] - hprev[1]
    hcpu.append(100.0 * (tot - idle) / tot if tot else 0); hmem.append(hcur[2])
    hrd.append((hcur[3] - hprev[3]) / dt); hwr.append((hcur[4] - hprev[4]) / dt)
    hprev, tprev = hcur, now
window = time.time() - t0
app2 = inspect(APP) or {"State": {}, "RestartCount": -1}

print("== 3. Equity snapshot limit (1 per account per minute while nothing changes)")
eq = equity_since(vol, marks)
rows = sum(v[0] or 0 for v in eq.values()); accounts = sum(v[1] or 0 for v in eq.values()); size = sum(v[2] if isinstance(v[2], int) else 0 for v in eq.values())
limit = accounts * (window / 60 + 1) * 2          # 1 per minute, doubled for real changes (fills, stops)
for rel, v in sorted(eq.items()):
    info("%s: %s equity rows from %s accounts in %.0f s" % (rel, v[0], v[1], window))
check(marks and rows <= limit, "equity snapshots within the limit", "%d rows, %d accounts, %.1f per account per minute; %.0f MB/day at this rate" % (
    rows, accounts, rows / max(accounts, 1) / (window / 60), size / window * 86400 / 1e6))

print("== 4. Production's own work goes on")
f1 = files(vol) if os.path.isdir(vol) else {}
changed = sorted(k for k in f1 if f0.get(k) != f1[k] and not k.endswith("-shm"))
check(any(k.startswith("smc_strategy_paper.db") for k in changed), "SMC database written during the window")
info("written during the window: %s" % (", ".join(changed[:12]) + (" ..." if len(changed) > 12 else "") or "nothing"))
pa = pa_written()
check(not pa, "no Price Action Lab file written since the deploy", ", ".join(pa) or "kept, unchanged")

print("== 5. Resources")
def p95(x): return sorted(x)[max(0, int(len(x) * 0.95) - 1)] if x else 0
def avg(x): return statistics.fmean(x) if x else 0
MiB, GiB = 1024**2, 1024**3
check(app2.get("RestartCount", 0) == 0 and not app2["State"].get("OOMKilled") and (app2["State"].get("Health") or {}).get("Status") == "healthy",
      "app still healthy, no restart or OOM during the window")
if cpu:
    info("app    CPU avg %.1f%% / p95 %.1f%% of one core   memory avg %.2f / peak %.2f GiB (with file cache peak %.2f)   disk read %.2f MiB/s, write %.2f MiB/s" % (
        avg(cpu), p95(cpu), avg(anon) / GiB, max(anon) / GiB, max(mem) / GiB, avg(rd) / MiB, avg(wr) / MiB))
    info("before (P0, 2026-10-22): app CPU 107.5%, memory 4.77 GiB avg")
else:
    check(False, "app resource counters readable")
info("host   CPU busy avg %.1f%% / p95 %.1f%%   memory available min %.2f of %.1f GiB   disk read %.2f MiB/s, write %.2f MiB/s" % (
    avg(hcpu), p95(hcpu), min(hmem) / GiB, hprev[5] / GiB, avg(hrd) / MiB, avg(hwr) / MiB))
check(min(hmem) >= GiB, "host keeps at least 1 GiB memory available", "min %.2f GiB" % (min(hmem) / GiB))

print("== 6. Other containers, disk, log")
now_rows = {}
for n in plan.get("others") or {}:
    c = inspect(n)
    now_rows[n] = [c["Id"], c["Image"], c["State"]["Status"], c["State"].get("StartedAt"), c.get("RestartCount", 0)] if c else None
check(plan.get("others") is not None and now_rows == plan["others"], "nginx, certbot and the rest unchanged since the deploy",
      ", ".join("%s %s" % (k.replace(PROJECT + "-", ""), (v or ["", "", "missing"])[2]) for k, v in sorted(now_rows.items())))
rate = (free0 - free()) / window * 86400
check(free() > 10e9, "free disk above 10 GB", "%.1f GB free; %+.2f GB/day during the window (whole host)" % (free() / 1e9, rate / 1e9))
rc, out, err = sh("docker", "logs", "--since", app["State"]["StartedAt"], APP, timeout=120)
log = (out + err).splitlines()
errors = [l for l in log if re.search(r"Traceback|\bERROR\b|CRITICAL", l)]
info("app log since start: %d lines, %d error lines" % (len(log), len(errors)))
for l in errors[-5:]: info("error: " + clean(l))
info("not checked here: the Settings page in Supabase sign-in mode needs Supabase migration 0006")

bad = [l for ok, l in results if not ok]
print("GO: production runs Strategy Intelligence, Price Action Lab is off, and production is healthy." if not bad else "NO-GO: " + "; ".join(bad))
print("Nothing was changed.")
PY
