"""Boot one API process in an isolated temp data dir and measure idle cost.

Usage: python boot_measure.py <repo_root> <label> <idle_seconds> [KEY=VAL ...]
Never touches production/staging. Market feeds cannot connect from this sandbox
(outbound Binance is blocked), so this is an import/construction + idle lower bound.
"""
import json, os, secrets, shutil, signal, statistics, subprocess, sys, tempfile, time
from collections import Counter

import requests

repo, label, idle = sys.argv[1], sys.argv[2], float(sys.argv[3])
extra = dict(kv.split("=", 1) for kv in sys.argv[4:])
here = os.path.dirname(os.path.abspath(__file__))
data = tempfile.mkdtemp(prefix=f"data-{label}-", dir=here)
port = 18000 + (abs(hash(label)) % 1000)
local_pw = secrets.token_urlsafe(16)  # throwaway per-run login for the temp instance
env = {k: v for k, v in os.environ.items() if not k.startswith(("HUB_", "SUPABASE"))}
env.update({"HUB_DATA_DIR": data, "HUB_DEV": "1", "HUB_ENV": "development", "PYTHONPATH": repo,
            "HUB_AUTH_MODE": "legacy", "HUB_USERNAME": "admin", "HUB_PASSWORD": local_pw,
            "HUB_DAILY_REPORT_HOUR": "-1"})
env.update(extra)
venv_py = os.path.join(os.path.dirname(here), "venv", "bin", "python")
log = open(os.path.join(here, f"boot-{label}.log"), "w")
t0 = time.time()
proc = subprocess.Popen([venv_py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port),
                         "--log-level", "warning"], cwd=os.path.join(repo, "automation-hub"), env=env,
                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
base = f"http://127.0.0.1:{port}"
ready = None
for _ in range(600):
    try:
        if requests.get(base + "/health", timeout=2).status_code < 500:
            ready = time.time() - t0; break
    except Exception:
        pass
    if proc.poll() is not None:
        break
    time.sleep(0.25)


def proc_stats(pid):
    st = dict(l.split(":", 1) for l in open(f"/proc/{pid}/status") if ":" in l)
    stat = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    cpu = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
    names = Counter()
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            names[open(f"/proc/{pid}/task/{tid}/comm").read().strip()] += 1
        except OSError:
            pass
    dbs = set()
    for fd in os.listdir(f"/proc/{pid}/fd"):
        try:
            tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if data in tgt:
            dbs.add(os.path.basename(tgt))
    return {"rss_mb": int(st["VmRSS"].split()[0]) / 1024, "threads": int(st["Threads"]), "cpu_s": cpu,
            "thread_names": dict(names), "open_data_files": sorted(dbs)}


out = {"label": label, "ready_s": ready, "alive": proc.poll() is None}
if proc.poll() is None:
    pid = proc.pid
    time.sleep(10)
    a = proc_stats(pid); ta = time.time()
    samples = []
    while time.time() - ta < idle:
        time.sleep(5); samples.append(proc_stats(pid)["rss_mb"])
    b = proc_stats(pid); tb = time.time()
    out.update({"rss_mb_after_boot": round(a["rss_mb"], 1), "rss_mb_end": round(b["rss_mb"], 1),
                "rss_mb_max": round(max(samples or [b["rss_mb"]]), 1), "threads": b["threads"],
                "idle_cpu_pct_one_core": round(100 * (b["cpu_s"] - a["cpu_s"]) / (tb - ta), 2),
                "idle_window_s": round(tb - ta), "thread_names": b["thread_names"],
                "open_data_files": b["open_data_files"]})
    s = requests.Session()
    login = s.post(base + "/login", data={"username": "admin", "password": local_pw}, allow_redirects=False)
    out["login_status"] = login.status_code
    lat = {}
    for path in ["/instances", "/research/price-action/bot-status", "/research/smc/bot-status",
                 "/research/smc/agent", "/research/adaptive-lab/status", "/guardian/status",
                 "/strategy/performance", "/paper/account", "/risk/summary", "/system/status",
                 "/calendar/options", "/journal/records?limit=50", "/research/instance-visual/instances"]:
        times, code, size = [], None, None
        for _ in range(5):
            t = time.perf_counter()
            try:
                r = s.get(base + path, timeout=60); code, size = r.status_code, len(r.content)
            except Exception as e:  # noqa: BLE001
                code = f"ERR {type(e).__name__}"
            times.append((time.perf_counter() - t) * 1000)
        lat[path] = {"status": code, "median_ms": round(statistics.median(times), 1), "bytes": size}
    out["endpoint_latency_empty_db"] = lat
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
out["data_files_created"] = sorted(os.listdir(data))
shutil.rmtree(data, ignore_errors=True)
print(json.dumps(out, indent=1))
