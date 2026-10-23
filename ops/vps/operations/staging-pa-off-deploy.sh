#!/bin/sh
# Deploy the Price Action Lab retirement (commit f9ebd32) to the existing STAGING API only.
#
#   plan      read-only. Checks every gate and shows exactly what would change:
#               - the verified backup finished OK and the API data has not changed since
#               - the loaded PA-off image is present and verified (staging-pa-off-images load)
#               - the staging Compose files are the exact reviewed ones (SHA-256)
#               - rendering today's files reproduces the live API and research worker exactly
#               - with the new image the API changes ONLY its image (and keeps 1536 MiB memory and
#                 memory+swap, volumes, networks, user, environment, limits)
#             Writes plan.json, which 'apply' requires (less than 2 hours old, nothing changed).
#   apply     re-runs every plan check, records the current API for rollback, then recreates ONLY
#             the staging API with the new image (no other service, no dependency, no pull, no
#             build), waits for it to be healthy, checks /health says the PA lab is off and the
#             retired PA routes answer 410, reloads the staging web proxy so it finds the new API,
#             and confirms production did not change. If the API does not become healthy it is
#             stopped again (staging was stopped before) and the result says how to roll back.
#   rollback  recreates the staging API with the previous image recorded by 'apply' and leaves it
#             STOPPED, as it was before the deploy. Data is not touched.
#
# Never touches production, the research worker, Postgres or any data file.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in plan|apply|rollback) ;; *) echo "usage: staging-pa-off-deploy.sh plan|apply|rollback"; exit 64 ;; esac
exec python3 -I - "$1" <<'PY'
import hashlib, json, os, re, subprocess, sys, time

MODE = sys.argv[1]
PROJECT = "nexus-remote-stage-rc3-28acdbc"
ROOT = os.environ.get("PA_OFF_TEST_ROOT", "/home/ubuntu/nexus-rc3-staging-28acdbc")
WORK = os.environ.get("PA_OFF_TEST_WORK", "/var/lib/nexus-ops/pa-retirement")
BACKUP = os.environ.get("PA_OFF_TEST_BACKUP", "/var/lib/nexus-ops/staging-backup")
COMMIT = os.environ.get("PA_OFF_TEST_COMMIT", "f9ebd32a3eedf6740c14a3daafb32e981d1d7ce8")
WD = ROOT + "/release/ops"
ENV = ROOT + "/private/compose.env"
# Exact reviewed files (first 16 hex of SHA-256), as listed by staging-config-check on 2026-10-23.
FILES = [ROOT + "/release/ops/staging.alpine.compose.yaml",
         ROOT + "/release/customer-workspace/compose.customer-workspace.yaml",
         ROOT + "/task6-worker-rollout-20261021T190957Z-46560af9531a/compose.worker.yaml"]
MEMORY = ROOT + "/release/intelligence-recovery-20261018/compose.memory.yaml"
EXTENDED = [ROOT + "/release/ops/staging.remote.compose.yaml", ROOT + "/release/ops/staging.local.compose.yaml"]
PINS = json.loads(os.environ.get("PA_OFF_TEST_PINS") or json.dumps({
    "staging.alpine.compose.yaml": "4f7855d9b420acd3", "compose.customer-workspace.yaml": "d32a63b43a40b247",
    "compose.worker.yaml": "ebc31d86e14e0c48", "compose.memory.yaml": "a71877aacb2a15ac",
    "staging.remote.compose.yaml": "9a664498f54962ef", "staging.local.compose.yaml": "039316baa8081491"}))
OVERLAY = os.path.join(WORK, "compose.pa-off-api.yaml")
ROLLBACK_OVERLAY = os.path.join(WORK, "compose.rollback-api.yaml")
PLAN = os.path.join(WORK, "plan.json")
ROLLBACK = os.path.join(WORK, "rollback.json")
API, INGRESS, WORKER = PROJECT + "-api-1", PROJECT + "-ingress-1", PROJECT + "-research-worker-1"
MEM = 1610612736
ALLOWED_API_CHANGES = {"image", "pull_policy", "mem_limit", "memswap_limit"}

class Blocked(Exception): pass
def say(m): print(m, flush=True)
def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()
def sh(*a, timeout=300):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr
def must(*a, timeout=300):
    rc, out, err = sh(*a, timeout=timeout)
    if rc: raise Blocked("%s failed: %s" % (" ".join(a[:4]), (err.strip() or out.strip())[-240:]))
    return out
def inspect(name):
    rc, out, _ = sh("docker", "inspect", name)
    return json.loads(out)[0] if rc == 0 else None
def compose(files, *args, timeout=300):
    cmd = ["docker", "compose", "--project-name", PROJECT, "--project-directory", WD]
    for f in files: cmd += ["-f", f]
    cmd += ["--env-file", ENV, "--profile", "research"]
    return must(*cmd, *args, timeout=timeout)
def render(files):
    cfg = json.loads(compose(files, "config", "--format", "json"))
    hashes = dict(l.split(None, 1) for l in compose(files, "config", "--hash", "*").splitlines() if " " in l)
    return cfg["services"], {k: v.strip() for k, v in hashes.items()}
def env_names(c): return sorted(e.split("=", 1)[0] for e in c["Config"].get("Env") or [])
def env_value(c, k):
    for e in c["Config"].get("Env") or []:
        if e.startswith(k + "="): return e.split("=", 1)[1]
    return None
def mounts(c): return sorted([m.get("Name") or m.get("Source"), m["Destination"], bool(m.get("RW"))] for m in c.get("Mounts", []))
def networks(c): return sorted((c["NetworkSettings"].get("Networks") or {}).keys())
def production():
    rc, out, _ = sh("docker", "ps", "-a", "--format", "{{.Names}}")
    rows = {}
    for n in sorted(x for x in out.split() if x.startswith("nexus-trading-bot-")):
        c = inspect(n)
        rows[n] = [c["Id"], c["Image"], c["State"]["Status"], c["State"].get("StartedAt"), c.get("RestartCount", 0)]
    return rows

def backup_gate():
    try:
        result = json.load(open(os.path.join(BACKUP, "result.json")))
        manifest = json.load(open(os.path.join(BACKUP, "manifest.json")))
    except (OSError, ValueError):
        raise Blocked("the staging backup has not finished (run staging-backup status)")
    if result.get("status") != "OK": raise Blocked("the staging backup did not pass: %s" % result.get("reason", "?"))
    if not os.path.isfile(manifest["archive"]) or sha(manifest["archive"]) != manifest["archive_sha256"]:
        raise Blocked("the backup archive is missing or changed")
    vol = json.loads(must("docker", "volume", "inspect", manifest["source_volume"]))[0]["Mountpoint"]
    for rel, ident in manifest["source"].items():
        try: st = os.lstat(os.path.join(vol, rel))
        except OSError: raise Blocked("%s changed since the backup (missing)" % rel)
        if (st.st_ino, st.st_size, st.st_mtime_ns) != (ident["inode"], ident["size"], ident["mtime_ns"]):
            raise Blocked("%s changed since the backup; take a new backup first" % rel)
    return {"archive_sha256": manifest["archive_sha256"], "files": len(manifest["source"]), "at": manifest["restore_test"]["at"]}

def image_gate():
    try: rec = json.load(open(os.path.join(WORK, "api-image.json")))
    except (OSError, ValueError): raise Blocked("the PA-off image is not loaded yet (run staging-pa-off-images load)")
    img = inspect(rec["image_id"])
    labels = ((img or {}).get("Config") or {}).get("Labels") or {}
    if not img or labels.get("org.opencontainers.image.revision") != COMMIT or rec.get("commit") != COMMIT:
        raise Blocked("the loaded PA-off image is missing or not commit %s" % COMMIT[:12])
    ref = "nexus-ci-api@" + rec["image_id"]
    if (inspect(ref) or {}).get("Id") != rec["image_id"]: raise Blocked("%s does not resolve to the verified image" % ref)
    return rec, ref

def files_gate():
    for p in FILES + [MEMORY] + EXTENDED:
        if not os.path.isfile(p): raise Blocked("%s is missing" % p)
        pin = PINS.get(os.path.basename(p))
        if not pin or not sha(p).startswith(pin): raise Blocked("%s is not the reviewed file (SHA-256 changed)" % p)
    if not os.path.isfile(ENV): raise Blocked("%s is missing" % ENV)

def check_all():
    files_gate()
    api, worker = inspect(API), inspect(WORKER)
    if not api: raise Blocked("staging API container not found")
    if api["State"]["Running"]: raise Blocked("staging API is already running; this deploy expects it stopped (P1-B)")
    if not worker or not worker["State"]["Running"]: raise Blocked("staging research worker is not running")
    rec, ref = image_gate()
    gate = backup_gate()
    with open(OVERLAY + ".tmp", "w") as f:
        f.write("# PA retirement: the staging API runs the verified f9ebd32 image. Written by staging-pa-off-deploy.\n"
                "services:\n  api:\n    image: %s\n    pull_policy: never\n" % ref)
    os.replace(OVERLAY + ".tmp", OVERLAY)
    live_files = (api["Config"]["Labels"].get("com.docker.compose.project.config_files") or "").split(",")
    a_svc, a_hash = render(live_files)
    if a_hash.get("api") != api["Config"]["Labels"].get("com.docker.compose.config-hash"):
        raise Blocked("today's Compose files no longer reproduce the stopped API's recorded configuration")
    b_svc, b_hash = render(FILES + [MEMORY])
    if b_hash.get("research-worker") != worker["Config"]["Labels"].get("com.docker.compose.config-hash"):
        raise Blocked("the reviewed file set does not reproduce the live research worker")
    c_svc, c_hash = render(FILES + [OVERLAY, MEMORY])
    before, after = a_svc["api"], c_svc["api"]
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if not set(changed) <= ALLOWED_API_CHANGES: raise Blocked("the API would also change: %s" % ", ".join(sorted(set(changed) - ALLOWED_API_CHANGES)))
    if after.get("image") != ref or after.get("pull_policy") != "never": raise Blocked("rendered API image is not the verified image")
    hc = api["HostConfig"]
    if int(after.get("mem_limit") or 0) != MEM or int(after.get("memswap_limit") or 0) != MEM or hc.get("Memory") != MEM or hc.get("MemorySwap") != MEM:
        raise Blocked("memory would not stay at 1536 MiB (rendered %s/%s, live %s/%s)" % (after.get("mem_limit"), after.get("memswap_limit"), hc.get("Memory"), hc.get("MemorySwap")))
    pa = (after.get("environment") or {}).get("PRICE_ACTION_LAB_ENABLED")
    if pa is not None and str(pa).strip().lower() not in ("", "0", "false", "no", "off"): raise Blocked("PRICE_ACTION_LAB_ENABLED is set on in staging")
    others = sorted(s for s in c_svc if s != "api" and c_hash.get(s) != b_hash.get(s))
    if others: raise Blocked("other services would change: %s" % ", ".join(others))
    plan = {"at": now(), "at_epoch": time.time(), "image_id": rec["image_id"], "image_ref": ref, "overlay_sha256": sha(OVERLAY),
            "env_digest": hashlib.sha256(json.dumps(after.get("environment") or {}, sort_keys=True).encode()).hexdigest(),
            "files": FILES + [OVERLAY, MEMORY], "api_hash_after": c_hash["api"], "api_hash_before": a_hash["api"],
            "old_image": api["Image"], "old_image_ref": api["Config"]["Image"], "backup": gate,
            "api_changes": changed, "production": production()}
    return api, worker, plan

def show(api, plan):
    say("GATES PASSED")
    say("- backup: verified %s (%d files, restore test %s) and the API data is unchanged since" % (plan["backup"]["archive_sha256"][:16], plan["backup"]["files"], plan["backup"]["at"]))
    say("- image: %s (commit %s), verified and loaded" % (plan["image_id"], COMMIT[:12]))
    say("- Compose files: the 6 reviewed files match their SHA-256; they reproduce the stopped API and the live research worker exactly")
    say("WHAT CHANGES (staging API only)")
    say("- image: %s (%s) -> %s" % (plan["old_image_ref"], plan["old_image"][:19], plan["image_ref"]))
    say("- recorded memory setting: 768m -> 1536m, the same 1536 MiB / 1536 MiB the API already had (set directly on Oct 18; now also in Compose)")
    say("- unchanged: volumes %s; networks %s; user %s; read-only root; restart policy; %d environment names; PRICE_ACTION_LAB_ENABLED unset = OFF" % (
        ", ".join("%s->%s" % (m[0].replace(PROJECT + "_", ""), m[1]) for m in mounts(api)), ",".join(n.replace(PROJECT + "_", "") for n in networks(api)),
        api["Config"].get("User"), len(env_names(api))))
    say("- not touched: research worker, Postgres, ingress (only reloaded after the API is healthy), production %s" % ", ".join(plan["production"]))

def wait_healthy(name, seconds):
    end = time.time() + seconds
    while time.time() < end:
        c = inspect(name)
        st = c["State"]
        if not st["Running"] and st["Status"] in ("exited", "dead"): return "exited (code %s, OOM %s)" % (st.get("ExitCode"), st.get("OOMKilled"))
        h = (st.get("Health") or {}).get("Status")
        if h == "healthy": return "healthy"
        time.sleep(3)
    return "not healthy after %d s" % seconds

def in_api(code):
    rc, out, err = sh("docker", "exec", API, "python", "-c", code, timeout=60)
    return out.strip() if rc == 0 else "ERROR"

def apply():
    api, worker, plan = check_all()
    try: old = json.load(open(PLAN))
    except (OSError, ValueError): raise Blocked("run 'plan' first")
    if time.time() - old.get("at_epoch", 0) > 7200: raise Blocked("the plan is older than 2 hours; run 'plan' again")
    for k in ("image_id", "overlay_sha256", "api_hash_after", "api_hash_before", "old_image", "backup", "production", "env_digest"):
        if old.get(k) != plan.get(k): raise Blocked("something changed since the plan (%s); run 'plan' again" % k)
    rollback = {"at": now(), "old_image": api["Image"], "old_image_ref": api["Config"]["Image"], "old_config_hash": api["Config"]["Labels"].get("com.docker.compose.config-hash"),
                "old_files": api["Config"]["Labels"].get("com.docker.compose.project.config_files"), "mounts": mounts(api), "networks": networks(api),
                "env_names": env_names(api), "user": api["Config"].get("User"), "memory": [api["HostConfig"].get("Memory"), api["HostConfig"].get("MemorySwap")]}
    with open(ROLLBACK, "w") as f: json.dump(rollback, f, indent=1)
    say("recorded the current API for rollback: %s (%s)" % (rollback["old_image_ref"], rollback["old_image"][:19]))
    compose(plan["files"], "up", "-d", "--no-deps", "--no-build", "--pull", "never", "api", timeout=600)
    say("%s recreated with the PA-off image; waiting for it to be healthy (up to 8 minutes)" % API)
    state = wait_healthy(API, 480)
    if state != "healthy":
        sh("docker", "stop", "-t", "15", API)
        raise Blocked("the new API is %s; it was stopped again, so staging is back to stopped. To put the old image back: rollback" % state)
    new = inspect(API)
    problems = []
    if new["Image"] != plan["image_id"]: problems.append("image")
    if [new["HostConfig"].get("Memory"), new["HostConfig"].get("MemorySwap")] != [MEM, MEM]: problems.append("memory")
    if mounts(new) != rollback["mounts"]: problems.append("volumes")
    if networks(new) != rollback["networks"]: problems.append("networks")
    c_svc, _ = render(plan["files"])
    want = {k: ("" if v is None else str(v)) for k, v in (c_svc["api"].get("environment") or {}).items()}
    if any(env_value(new, k) != v for k, v in want.items()): problems.append("environment values from Compose")
    if new["Config"].get("User") != rollback["user"]: problems.append("user")
    say("container check: %s" % ("image, 1536 MiB memory, volumes, networks, user and all %d Compose environment values as planned" % len(want) if not problems else "DIFFERS: " + ", ".join(problems)))
    health = in_api("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/health',timeout=10)); print(json.dumps({'status':d.get('status'),'pa':(d.get('features') or {}).get('price_action_lab_enabled')}))")
    retired = in_api("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/openapi.json',timeout=30)); "
                     "ops=[(p,m,o.get('tags')) for p,i in d.get('paths',{}).items() if p.split('/research/',1)[-1].startswith('price-action') and '/research/price-action' in p for m,o in i.items() if isinstance(o,dict)]; "
                     "print(json.dumps({'operations':len(ops),'not_retired':sorted({p for p,m,t in ops if t!=['retired-price-action']})}))")
    say("/health: %s; PA lab routes in the API's route list: %s" % (health, retired))
    try: h = json.loads(health)
    except ValueError: h = {}
    if h.get("status") != "ok" or h.get("pa") is not False: problems.append("/health")
    try: r = json.loads(retired)
    except ValueError: r = {}
    if not r.get("operations") or r.get("not_retired"): problems.append("PA lab routes not all retired")
    ing = inspect(INGRESS)
    if ing and ing["State"]["Running"]:
        rc, _, err = sh("docker", "exec", INGRESS, "nginx", "-t")
        if rc == 0:
            sh("docker", "exec", INGRESS, "nginx", "-s", "reload")
            ist = wait_healthy(INGRESS, 120) if (ing["State"].get("Health") is not None) else "running"
            say("staging web proxy reloaded: %s" % ist)
            if ist != "healthy" and ist != "running": problems.append("web proxy")
        else:
            problems.append("web proxy config test"); say("web proxy config test failed; not reloaded")
    prod_after = production()
    say("production: %s" % ("unchanged (same containers, start times, restart counts)" if prod_after == plan["production"] else "CHANGED - check now"))
    if prod_after != plan["production"]: problems.append("production")
    receipt = {"at": now(), "image_id": plan["image_id"], "health": health, "retired_route": retired, "problems": problems, "rollback": ROLLBACK}
    with open(os.path.join(WORK, "deployed-%s.json" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())), "w") as f: json.dump(receipt, f, indent=1)
    if problems: raise Blocked("deployed, but these checks failed: %s. The API is left running for inspection; 'rollback' restores the old image (stopped)" % ", ".join(problems))
    say("DEPLOYED: staging API runs the PA-off image (Price Action Lab OFF). Next: staging-pa-off-check measure.")

def do_rollback():
    try: rb = json.load(open(ROLLBACK))
    except (OSError, ValueError): raise Blocked("no rollback record; 'apply' has not run")
    files_gate()
    img = inspect(rb["old_image_ref"])
    if not img or img["Id"] != rb["old_image"]: raise Blocked("the previous image %s is not available as recorded" % rb["old_image_ref"])
    with open(ROLLBACK_OVERLAY, "w") as f:
        f.write("# Rollback of the PA retirement deploy. Written by staging-pa-off-deploy.\nservices:\n  api:\n    image: %s\n    pull_policy: never\n" % rb["old_image_ref"])
    before = production()
    compose(FILES + [ROLLBACK_OVERLAY, MEMORY], "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--no-start", "api", timeout=600)
    sh("docker", "stop", "-t", "15", API)
    c = inspect(API)
    ok = c["Image"] == rb["old_image"] and not c["State"]["Running"] and [c["HostConfig"].get("Memory"), c["HostConfig"].get("MemorySwap")] == [MEM, MEM]
    say("staging API: image %s, %s, memory %s" % (c["Config"]["Image"], c["State"]["Status"], c["HostConfig"].get("Memory")))
    say("production: %s" % ("unchanged" if production() == before else "CHANGED - check now"))
    if not ok: raise Blocked("rollback did not reach the expected state")
    say("ROLLED BACK: staging API is on the previous image and stopped, as before the deploy. Data was not touched.")

try:
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    if MODE == "plan":
        api, worker, plan = check_all()
        with open(PLAN, "w") as f: json.dump(plan, f, indent=1)
        show(api, plan)
        say("PLAN READY (valid 2 hours). Nothing was changed.")
    elif MODE == "apply":
        apply()
    else:
        do_rollback()
except Blocked as e:
    print("BLOCKED: %s." % e); sys.exit(2)
PY
