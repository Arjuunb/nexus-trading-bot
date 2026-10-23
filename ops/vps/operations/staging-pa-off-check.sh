#!/bin/sh
# Read-only check of the staging API after the Price Action Lab retirement deploy, over 10 minutes.
#
#   measure   1. identity: the staging API runs the verified PA-off image, healthy, no restarts or
#                OOM, 1536 MiB memory
#             2. PA is gone at runtime: /health says off, every PA route is the retired handler, the
#                switch is not set on, price_action_paper.db was not re-created, the leftover
#                price_action_research.db was not touched since the deploy, no API process holds any
#                price_action file open, no PA runtime/subscription lines in the API log
#             3. shared features: SMC database still being written, research worker and web proxy
#                healthy, API log error count
#             4. resources for 10 minutes (every 15 s, from the kernel's cgroup counters): API CPU,
#                memory, disk read/write; production app CPU/memory; host CPU, memory, disk activity;
#                compared with the P0 measurement (staging API 95.5% CPU, 1.50 GiB, 220.5 MiB/s read)
#             5. production unchanged since the deploy plan; free disk and staging data growth
#             Ends with GO or NO-GO and the reasons.
#
# Changes nothing. Log lines are shortened and anything secret-looking is hidden.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in measure) ;; *) echo "usage: staging-pa-off-check.sh measure"; exit 64 ;; esac
exec python3 -I - <<'PY'
import glob, json, os, re, statistics, subprocess, time
from datetime import datetime, timezone

PROJECT = "nexus-remote-stage-rc3-28acdbc"
API, INGRESS, WORKER = PROJECT + "-api-1", PROJECT + "-ingress-1", PROJECT + "-research-worker-1"
WORK = os.environ.get("PA_OFF_TEST_WORK", "/var/lib/nexus-ops/pa-retirement")
SECONDS = int(os.environ.get("PA_OFF_TEST_SECONDS", 600))
STEP = int(os.environ.get("PA_OFF_TEST_STEP", 15))
MEM = 1610612736
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
def in_api(code):
    rc, out, _ = sh("docker", "exec", API, "python", "-c", code, timeout=60)
    return out.strip() if rc == 0 else ""
def clean(line): return SECRETISH.sub("[hidden]", line.strip())[:160]
def parse_time(s): return datetime.fromisoformat(s[:26].rstrip("Z") + "+00:00").timestamp() if s else 0

def cgroup_dir(c):
    """cgroup v2 directory, or a dict of cgroup v1 controller directories."""
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
    idle = cpu[3] + cpu[4]
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    rd = wr = 0
    for l in open("/proc/diskstats"):
        f = l.split()
        if re.fullmatch(r"(sd[a-z]|vd[a-z]|nvme\d+n\d+|xvd[a-z])", f[2]): rd += int(f[5]); wr += int(f[9])
    return sum(cpu), idle, mem["MemAvailable"] * 1024, rd * 512, wr * 512
def free(): s = os.statvfs("/"); return s.f_bavail * s.f_frsize
def volume_dir():
    rc, out, _ = sh("docker", "volume", "inspect", "-f", "{{.Mountpoint}}", PROJECT + "_api-state")
    return out.strip()

try: rec = json.load(open(os.path.join(WORK, "api-image.json")))
except (OSError, ValueError): rec = {}
try: plan = json.load(open(os.path.join(WORK, "plan.json")))
except (OSError, ValueError): plan = {}
receipts = sorted(glob.glob(os.path.join(WORK, "deployed-*.json")))
deployed_at = parse_time(json.load(open(receipts[-1]))["at"]) if receipts else 0

print("== 1. Identity")
api = inspect(API)
if not api or not api["State"]["Running"]:
    check(False, "staging API is running"); raise SystemExit("NO-GO: the staging API is not running. Nothing was changed.")
check(api["Image"] == rec.get("image_id"), "API runs the verified PA-off image", api["Image"][:19])
check((api["State"].get("Health") or {}).get("Status") == "healthy", "API healthy")
check(api.get("RestartCount", 0) == 0 and not api["State"].get("OOMKilled"), "no restarts, not OOM-killed", "restarts %d" % api.get("RestartCount", 0))
check([api["HostConfig"].get("Memory"), api["HostConfig"].get("MemorySwap")] == [MEM, MEM], "memory limit 1536 MiB / 1536 MiB")
started = parse_time(api["State"]["StartedAt"])
info("started %s (%.0f minutes ago)" % (api["State"]["StartedAt"][:19], (time.time() - started) / 60))

print("== 2. Price Action Lab is gone at runtime")
try: h = json.loads(in_api("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/health',timeout=10)); print(json.dumps({'status':d.get('status'),'pa':(d.get('features') or {}).get('price_action_lab_enabled'),'tenancy':d.get('tenancy')}))"))
except ValueError: h = {}
check(h.get("status") == "ok" and h.get("pa") is False, "/health: status ok, price_action_lab_enabled false", json.dumps(h))
try: r = json.loads(in_api("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/openapi.json',timeout=30)); "
                           "ops=[(p,o.get('tags')) for p,i in d.get('paths',{}).items() if '/research/price-action' in p for m,o in i.items() if isinstance(o,dict)]; "
                           "print(json.dumps({'operations':len(ops),'not_retired':sorted({p for p,t in ops if t!=['retired-price-action']})}))"))
except ValueError: r = {}
check(r.get("operations") and not r.get("not_retired"), "every PA lab route is the retired handler (HTTP 410)", "%s operations" % r.get("operations"))
env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in api["Config"].get("Env") or [] if "=" in e}
check(env.get("PRICE_ACTION_LAB_ENABLED", "false").strip().lower() in ("", "0", "false", "no", "off"), "PRICE_ACTION_LAB_ENABLED not on", env.get("PRICE_ACTION_LAB_ENABLED", "unset"))
vol = volume_dir()
check(not any(os.path.exists(os.path.join(vol, "price_action_paper.db" + s)) for s in ("", "-wal", "-shm", "-journal")),
      "price_action_paper.db not re-created")
touched = [f for f in sorted(os.listdir(vol)) if f.startswith("price_action_research.db") and os.path.getmtime(os.path.join(vol, f)) > deployed_at > 0]
check(deployed_at > 0 and not touched, "leftover price_action_research.db not touched since the deploy", ", ".join(touched) or "unchanged")
cid = api["Id"]; pids = []; opened = []; unreadable = 0; threads = 0
for p in os.listdir("/proc"):
    if not p.isdigit(): continue
    try:
        if cid not in open("/proc/%s/cgroup" % p).read(): continue
    except OSError: continue
    pids.append(p)
    try: threads += len(os.listdir("/proc/%s/task" % p))
    except OSError: pass
    try:
        for fd in os.listdir("/proc/%s/fd" % p):
            try: t = os.readlink("/proc/%s/fd/%s" % (p, fd))
            except OSError: continue
            if "price_action" in os.path.basename(t): opened.append(t)
    except OSError: unreadable += 1
check(pids and not opened and not unreadable, "no API process holds a price_action file open", "%d processes, %d threads checked%s" % (len(pids), threads, (", open: " + ", ".join(sorted(set(opened)))) if opened else ""))
rc, out, err = sh("docker", "logs", "--since", api["State"]["StartedAt"], API, timeout=120)
log = (out + err).splitlines()
pa_runtime = [l for l in log if re.search(r"PA_LAB|price-action-paper-runtime|price_action_paper\.db|PriceActionLab(Supervisor|Runtime)", l)]
pa_any = [l for l in log if re.search(r"(?i)price[ _-]?action", l)]
check(not pa_runtime, "no PA runtime or subscription lines in the API log", "%d lines mention price action" % len(pa_any))
for l in pa_any[:3]: info("log: " + clean(l))

print("== 3. Shared features")
smc = os.path.join(vol, "smc_strategy_paper.db")
def smc_state(): return [(os.path.getsize(smc + s), os.path.getmtime(smc + s)) if os.path.exists(smc + s) else None for s in ("", "-wal")]
smc0 = smc_state()
for name in (WORKER, INGRESS):
    c = inspect(name)
    check(c and c["State"]["Running"] and (c["State"].get("Health") or {}).get("Status") in (None, "healthy"),
          "%s running and healthy" % name.replace(PROJECT + "-", ""), ((c or {}).get("State") or {}).get("Status", "missing"))

print("== 4. Resources for %d minutes (sample every %d s)" % (SECONDS // 60, STEP))
prod = inspect("nexus-trading-bot-app-1")
dirs = {"api": cgroup_dir(api), "prod": cgroup_dir(prod) if prod else None}
samples = []; free0 = free(); t0 = time.time()
prev = {k: counters(d) for k, d in dirs.items() if d}; hprev = host(); tprev = time.time()
cpu_api, mem_api, cpu_prod, mem_prod, hcpu, hmem, rd_api, wr_api, hrd, hwr, anon_api = ([] for _ in range(11))
while time.time() - t0 < SECONDS:
    time.sleep(STEP)
    now = time.time(); dt = now - tprev
    cur = {k: counters(d) for k, d in dirs.items() if d}; hcur = host()
    if "api" in cur:
        cpu_api.append((cur["api"][0] - prev["api"][0]) / 1e6 / dt * 100); mem_api.append(cur["api"][1]); anon_api.append(cur["api"][4])
        rd_api.append((cur["api"][2] - prev["api"][2]) / dt); wr_api.append((cur["api"][3] - prev["api"][3]) / dt)
    if "prod" in cur:
        cpu_prod.append((cur["prod"][0] - prev["prod"][0]) / 1e6 / dt * 100); mem_prod.append(cur["prod"][1])
    tot, idle = hcur[0] - hprev[0], hcur[1] - hprev[1]
    hcpu.append(100.0 * (tot - idle) / tot if tot else 0); hmem.append(hcur[2])
    hrd.append((hcur[3] - hprev[3]) / dt); hwr.append((hcur[4] - hprev[4]) / dt)
    prev, hprev, tprev = cur, hcur, now
def p95(x): return sorted(x)[max(0, int(len(x) * 0.95) - 1)] if x else 0
def avg(x): return statistics.fmean(x) if x else 0
MiB, GiB = 1024**2, 1024**3
if cpu_api:
    print("      staging API   CPU avg %.1f%% / p95 %.1f%% of one core   app memory avg %.2f / peak %.2f GiB (with file cache peak %.2f)   disk read %.2f MiB/s, write %.2f MiB/s" % (
        avg(cpu_api), p95(cpu_api), avg(anon_api) / GiB, max(anon_api) / GiB, max(mem_api) / GiB, avg(rd_api) / MiB, avg(wr_api) / MiB))
    print("      P0 (PA on)    CPU avg 95.5% / p95 100.1% (capped)     container memory 1.50 GiB (at limit), app 1.07 GiB   disk read 220.5 MiB/s, write 0.36 MiB/s")
    check(max(anon_api) < MEM * 0.9, "API app memory stays well below its limit", "peak %.2f of 1.50 GiB" % (max(anon_api) / GiB))
else:
    check(False, "API resource counters readable")
if cpu_prod:
    print("      production    CPU avg %.1f%% / p95 %.1f%%   memory avg %.2f / peak %.2f GiB   (P0: 107.5%% CPU, 4.77 GiB avg)" % (
        avg(cpu_prod), p95(cpu_prod), avg(mem_prod) / GiB, max(mem_prod) / GiB))
print("      host          CPU busy avg %.1f%% / p95 %.1f%% (6 cores)   memory available min %.1f GiB   disk read %.2f MiB/s, write %.2f MiB/s   (P0: 39%% busy, 5.2 GiB available, 220.6 MiB/s read)" % (
    avg(hcpu), p95(hcpu), min(hmem) / GiB if hmem else 0, avg(hrd) / MiB, avg(hwr) / MiB))

smc1 = smc_state()
grew = smc0 != smc1
check(grew, "SMC database is being written (shared market data and SMC lab work)",
      "db %s, wal %s" % tuple("%+.1f MB" % ((b[0] - a[0]) / 1e6) if a and b else "-" for a, b in zip(smc0, smc1)))

print("== 5. Production and disk")
rc, out, _ = sh("docker", "ps", "-a", "--format", "{{.Names}}")
prod_now = {}
for n in sorted(x for x in out.split() if x.startswith("nexus-trading-bot-")):
    c = inspect(n); prod_now[n] = [c["Id"], c["Image"], c["State"]["Status"], c["State"].get("StartedAt"), c.get("RestartCount", 0)]
check(plan.get("production") and prod_now == plan["production"], "production unchanged since the deploy plan", ", ".join("%s %s" % (k.replace("nexus-trading-bot-", ""), v[2]) for k, v in prod_now.items()))
used = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(vol) for f in fs if not os.path.islink(os.path.join(dp, f)))
rate = (free0 - free()) / (time.time() - t0) * 86400 if time.time() > t0 else 0
info("free disk %.2f GB; staging API data %.2f GB; disk use during the window %+.2f GB/day (all of the host)" % (free() / 1e9, used / 1e9, rate / 1e9))

bad = [l for ok, l in results if not ok]
errors = [l for l in log if re.search(r"Traceback|\bERROR\b|CRITICAL", l)]
info("API log since start: %d lines, %d error lines" % (len(log), len(errors)))
for l in errors[-3:]: info("error: " + clean(l))
print("GO: Price Action Lab is off on staging and staging is healthy." if not bad else "NO-GO: " + "; ".join(bad))
print("Nothing was changed.")
PY
