#!/bin/sh
# Deploy Strategy Intelligence (PR #48, merged as 93769c5; image built and scanned from a4472e7)
# to the PRODUCTION app only.
#
#   plan      read-only. Checks every gate and shows exactly what would change:
#               - a verified production backup (prod-backup) under 6 hours old, taken of this same app
#               - the scanned image is loaded and verified (prod-si-images load)
#               - the Compose files are the exact ones production was started from (SHA-256)
#               - rendering them with production's own values reproduces the live app exactly
#                 (Compose config hash); with the new image only the image changes, no other service
#               - the new app starts with production's own start command (exec uvicorn ...
#                 --timeout-graceful-shutdown 5, so the 40 s shutdown hook still runs), as the same
#                 user, in the same directory
#               - its full environment (image defaults + Compose + .env) is predicted and compared
#                 with the running app's: only GIT_COMMIT/GIT_BRANCH/DEPLOYED_AT, Python base-image
#                 values, and defaults that equal the code's own may differ. The new image turns the
#                 AI Monitoring Agent timer off by default (HUB_MONITOR_AGENT=0); production runs it,
#                 so it is kept on. Anything else blocks.
#               - the old one-off container pa-replay-2025 is outside what 'up' manages
#             Writes plan.json, which 'apply' requires (less than 2 hours old, nothing changed).
#   apply     re-runs every plan check, records the current app for rollback, then recreates ONLY
#             the app with the new image (no other service, no dependency, no pull, no build),
#             waits for it to be healthy, checks its environment and command are exactly as planned,
#             /health (status ok, PA Lab off), that the PA routes are retired and Strategy
#             Intelligence answers, reloads the web proxy the way scripts/deploy.sh does and checks
#             /health through the proxy, as a visitor reaches it. If the new app restarts, exits, is
#             unhealthy, or is not healthy in 8 minutes, the previous image is put back AT ONCE.
#   rollback  recreates the app with the image and values recorded by 'apply', waits for it to be
#             healthy, checks it is exactly the app that ran before (Compose config hash) and
#             reloads the web proxy. Data is not touched.
#
# Never touches the data volume's files, nginx/certbot containers (nginx is only reloaded), the
# checkout in /opt/nexus-trading-bot, or staging.
set -u
umask 077
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in plan|apply|rollback) ;; *) echo "usage: prod-si-deploy.sh plan|apply|rollback"; exit 64 ;; esac
exec python3 -I - "$1" <<'PY'
import hashlib, json, os, subprocess, sys, time

MODE = sys.argv[1]
PROJECT = "nexus-trading-bot"
WD = os.environ.get("PROD_SI_TEST_WD", "/opt/nexus-trading-bot")
WORK = os.environ.get("PROD_SI_TEST_WORK", "/var/lib/nexus-ops/si-production")
BACKUP = os.environ.get("PROD_SI_TEST_BACKUP", "/var/lib/nexus-ops/prod-backup")
COMMIT = os.environ.get("PROD_SI_TEST_COMMIT", "a4472e7b704dfe1321455c64a511f6bc3be2133c")
BRANCH = "codex/guardian-vps-integration"           # a4472e7 was merged there as 93769c5 (identical tree)
FILES = [os.path.join(WD, "compose.yaml"), os.path.join(WD, "compose.override.yaml")]
# Exact files production was started from (first 16 hex of SHA-256), as listed by prod-inventory
# on 2026-10-23 (run 40027418155).
PINS = json.loads(os.environ.get("PROD_SI_TEST_PINS") or json.dumps({
    "compose.yaml": "27ab33c5667a1a84", "compose.override.yaml": "7ddd47613c4dcd4c"}))
APP, NGINX = PROJECT + "-app-1", PROJECT + "-nginx-1"
SITE = "trade-logx.com"
OVERLAY = os.path.join(WORK, "compose.si-app.yaml")
PLAN = os.path.join(WORK, "plan.json")
ROLLBACK = os.path.join(WORK, "rollback.json")
DEPLOY_VARS = ("GIT_COMMIT", "GIT_BRANCH", "DEPLOYED_AT")
# Values of the Python base image (Debian slim -> Alpine) that may differ; shown in the plan.
BASE_IMAGE_VARS = ("PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256", "PYTHON_PIP_VERSION",
                   "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256",
                   "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED")
# Unset on production; the new image sets them to the code's own default, so nothing changes.
SAME_AS_CODE_DEFAULT = {"HUB_ENABLE_EXTERNAL_LIVE": "0", "NEXUS_SHADOW_SUPERVISED": "0"}
# Unset on production (code default on); the new image would switch them off, so they are kept on.
KEEP_CODE_DEFAULT = {"HUB_MONITOR_AGENT": "1"}
BACKUP_MAX_AGE = 6 * 3600
HEALTH_WAIT = int(os.environ.get("PROD_SI_TEST_HEALTH_WAIT", "480"))

class Blocked(Exception): pass
def say(m): print(m, flush=True)
def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()
def sh(*a, timeout=300, env=None, stdin=None):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout, env=env, stdin=stdin)
    return r.returncode, r.stdout, r.stderr
def must(*a, timeout=300, env=None):
    rc, out, err = sh(*a, timeout=timeout, env=env)
    if rc: raise Blocked("%s failed: %s" % (" ".join(a[:4]), (err.strip() or out.strip())[-240:]))
    return out
def inspect(name):
    rc, out, _ = sh("docker", "inspect", name)
    return json.loads(out)[0] if rc == 0 else None
def env_value(c, k):
    for e in c["Config"].get("Env") or []:
        if e.startswith(k + "="): return e.split("=", 1)[1]
    return None
def env_dict(c): return dict(e.split("=", 1) for e in (c["Config"].get("Env") or []) if "=" in e)
def unescape(v):
    """'docker compose config' prints every literal $ as $$; the container gets a single $."""
    return v.replace("$$", "$") if isinstance(v, str) else v
def predicted_env(image, service):
    """The environment a container gets: the image's defaults, overridden by Compose (with .env)."""
    env = env_dict(image)
    for k, v in (service.get("environment") or {}).items():
        if v is not None: env[k] = unescape(str(v))
    return env
def digest(obj): return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
def interpolation(values):
    """The variables scripts/deploy.sh exports, and nothing else from this shell."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")}
    env.update(values)
    return env
def compose(files, values, *args, timeout=300):
    cmd = ["docker", "compose", "--project-name", PROJECT, "--project-directory", WD]
    for f in files: cmd += ["-f", f]
    return must(*cmd, *args, timeout=timeout, env=interpolation(values))
def render(files, values):
    cfg = json.loads(compose(files, values, "config", "--format", "json"))
    hashes = dict(l.split(None, 1) for l in compose(files, values, "config", "--hash", "*").splitlines() if " " in l)
    return cfg["services"], {k: v.strip() for k, v in hashes.items()}
def mounts(c): return sorted([m.get("Name") or m.get("Source"), m["Destination"], bool(m.get("RW"))] for m in c.get("Mounts", []))
def networks(c): return sorted((c["NetworkSettings"].get("Networks") or {}).keys())
def others():
    """Every other container of the project: id, image, state, start time, restarts."""
    rc, out, _ = sh("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT)
    rows = {}
    for cid in out.split():
        c = inspect(cid)
        name = c["Name"].lstrip("/")
        if name == APP: continue
        rows[name] = [c["Id"], c["Image"], c["State"]["Status"], c["State"].get("StartedAt"), c.get("RestartCount", 0)]
    return rows
def live_values(app):
    tag = app["Config"]["Image"]
    return {"GIT_COMMIT": env_value(app, "GIT_COMMIT") or "unknown", "GIT_BRANCH": env_value(app, "GIT_BRANCH") or "unknown",
            "DEPLOYED_AT": env_value(app, "DEPLOYED_AT") or "unknown",
            "APP_IMAGE_TAG": tag.split(":", 1)[1] if tag.startswith(PROJECT + "-app:") else "dev"}

def backup_gate(app):
    try:
        result = json.load(open(os.path.join(BACKUP, "result.json")))
        manifest = json.load(open(os.path.join(BACKUP, "manifest.json")))
    except (OSError, ValueError):
        raise Blocked("no finished production backup (run prod-backup start, then prod-backup status)")
    if result.get("status") != "OK": raise Blocked("the production backup did not pass: %s" % result.get("reason", "?"))
    if not os.path.isfile(manifest["archive"]) or sha(manifest["archive"]) != manifest["archive_sha256"]:
        raise Blocked("the backup archive is missing or changed")
    at = time.mktime(time.strptime(manifest["restore_test"]["at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    if time.time() - at > BACKUP_MAX_AGE:
        raise Blocked("the backup is older than 6 hours (%s); take a new one" % manifest["restore_test"]["at"])
    if manifest["rollback"]["image_id"] != app["Image"]:
        raise Blocked("the backup was taken while a different app image ran; take a new one")
    return {"archive_sha256": manifest["archive_sha256"], "files": len(manifest["files"]),
            "databases": len(manifest["sqlite"]), "at": manifest["restore_test"]["at"]}

def image_gate():
    try: rec = json.load(open(os.path.join(WORK, "api-image.json")))
    except (OSError, ValueError): raise Blocked("the Strategy Intelligence image is not loaded yet (run prod-si-images load)")
    img = inspect(rec["image_id"])
    labels = ((img or {}).get("Config") or {}).get("Labels") or {}
    if not img or labels.get("org.opencontainers.image.revision") != COMMIT or rec.get("commit") != COMMIT:
        raise Blocked("the loaded image is missing or not commit %s" % COMMIT[:12])
    ref = "nexus-ci-api@" + rec["image_id"]
    if (inspect(ref) or {}).get("Id") != rec["image_id"]: raise Blocked("%s does not resolve to the verified image" % ref)
    return rec, ref

def files_gate():
    for p in FILES:
        if not os.path.isfile(p): raise Blocked("%s is missing" % p)
        pin = PINS.get(os.path.basename(p))
        if not pin or not sha(p).startswith(pin): raise Blocked("%s is not the file production was started from (SHA-256 changed)" % p)

def check_all(deployed_at=None):
    files_gate()
    app = inspect(APP)
    if not app or not app["State"]["Running"]: raise Blocked("production app %s is not running" % APP)
    labels = app["Config"]["Labels"]
    if labels.get("com.docker.compose.project.config_files") != ",".join(FILES):
        raise Blocked("production was started from other Compose files: %s" % labels.get("com.docker.compose.project.config_files"))
    for name, row in others().items():
        c = inspect(row[0])
        lab = c["Config"].get("Labels") or {}
        if lab.get("com.docker.compose.service") == "app" and lab.get("com.docker.compose.oneoff") != "True":
            raise Blocked("%s is a second, non-one-off container of the app service; 'up' could recreate or remove it" % name)
    rec, ref = image_gate()
    gate = backup_gate(app)
    new_img, old_img = inspect(rec["image_id"]), inspect(app["Image"])
    if not old_img: raise Blocked("the running app's image %s is not on this host" % app["Image"][:19])
    live_cfg, new_cfg = app["Config"], new_img["Config"]
    for k, what in (("User", "user"), ("WorkingDir", "working directory"), ("Entrypoint", "entrypoint")):
        if (new_cfg.get(k) or None) != (live_cfg.get(k) or None):
            raise Blocked("the new image's %s (%s) differs from the running app's (%s)" % (what, new_cfg.get(k), live_cfg.get(k)))
    live_cmd = live_cfg.get("Cmd") or []
    if not live_cmd: raise Blocked("the running app has no command to keep")
    live_env = env_dict(app)
    overlay_env = {k: v for k, v in KEEP_CODE_DEFAULT.items()
                   if k not in live_env and env_dict(new_img).get(k) not in (None, v)}
    before_values = live_values(app)
    after_values = dict(before_values, GIT_COMMIT=COMMIT, GIT_BRANCH=BRANCH, DEPLOYED_AT=deployed_at or now())
    overlay = {"services": {"app": {"image": ref, "pull_policy": "never",
                                     "command": [c.replace("$", "$$") for c in live_cmd]}}}
    if overlay_env: overlay["services"]["app"]["environment"] = overlay_env
    with open(OVERLAY + ".tmp", "w") as f:
        f.write("# Strategy Intelligence: the app runs the verified, scanned %s image with production's own\n"
                "# start command. Written by prod-si-deploy.\n" % COMMIT[:12])
        json.dump(overlay, f, indent=1); f.write("\n")
    os.replace(OVERLAY + ".tmp", OVERLAY)
    a_svc, a_hash = render(FILES, before_values)
    if a_hash.get("app") != labels.get("com.docker.compose.config-hash"):
        raise Blocked("the Compose files with production's own values no longer reproduce the running app")
    old_pred = predicted_env(old_img, a_svc["app"])
    if old_pred != live_env:
        raise Blocked("the running app's environment is not what its image and Compose files give (%s)"
                      % ", ".join(sorted(k for k in set(old_pred) | set(live_env) if old_pred.get(k) != live_env.get(k))))
    c_svc, c_hash = render(FILES + [OVERLAY], after_values)
    before, after = a_svc["app"], c_svc["app"]
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if "build" in changed:      # never used (--no-build, image present); only its deploy-label args may differ
        strip = lambda bd: dict(bd or {}, args={k: v for k, v in ((bd or {}).get("args") or {}).items() if k not in DEPLOY_VARS})
        if strip(before.get("build")) == strip(after.get("build")): changed.remove("build")
    if not set(changed) <= {"image", "pull_policy", "environment", "command"}:
        raise Blocked("the app would also change: %s" % ", ".join(sorted(set(changed) - {"image", "pull_policy", "environment", "command"})))
    if after.get("image") != ref or after.get("pull_policy") != "never": raise Blocked("rendered app image is not the verified image")
    if [unescape(c) for c in after.get("command") or []] != live_cmd: raise Blocked("rendered start command is not production's")
    new_env = predicted_env(new_img, after)
    env_changes, unknown = [], []
    for k in sorted(set(live_env) | set(new_env)):
        a, b = live_env.get(k), new_env.get(k)
        if a == b: continue
        if k in DEPLOY_VARS and b == after_values[k]: env_changes.append([k, a, b, "deploy label"])
        elif k in BASE_IMAGE_VARS: env_changes.append([k, a, b, "Python base image"])
        elif a is None and SAME_AS_CODE_DEFAULT.get(k) == b: env_changes.append([k, a, b, "the code's own default, no change"])
        elif a is None and k in overlay_env and b == KEEP_CODE_DEFAULT[k]:
            env_changes.append([k, a, b, "kept on: the code's default; the new image would set %s" % env_dict(new_img).get(k)])
        else: unknown.append(k)
    if unknown: raise Blocked("the app's environment would also change: %s" % ", ".join(unknown))
    pa = new_env.get("PRICE_ACTION_LAB_ENABLED")
    if pa is not None and str(pa).strip().lower() not in ("", "0", "false", "no", "off"):
        raise Blocked("PRICE_ACTION_LAB_ENABLED is set on")
    moved = sorted(s for s in c_svc if s != "app" and c_hash.get(s) != a_hash.get(s))
    if moved: raise Blocked("other services would change: %s" % ", ".join(moved))
    plan = {"at": now(), "at_epoch": time.time(), "image_id": rec["image_id"], "image_ref": ref, "overlay_sha256": sha(OVERLAY),
            "before_values": before_values, "after_values": after_values,
            "app_hash_before": a_hash["app"], "app_hash_after": c_hash["app"],
            "old_image": app["Image"], "old_image_ref": app["Config"]["Image"], "old_container": app["Id"],
            "backup": gate, "env_changes": env_changes, "env_digest": digest(new_env), "command": live_cmd,
            "image_command": new_cfg.get("Cmd"), "others": others()}
    return app, plan, new_env

def show(app, plan):
    say("GATES PASSED")
    b = plan["backup"]
    say("- backup: verified %s (%d files, %d databases, restore test %s)" % (b["archive_sha256"][:16], b["files"], b["databases"], b["at"]))
    say("- image: %s (commit %s), verified and loaded" % (plan["image_id"], COMMIT[:12]))
    say("- Compose files: compose.yaml and compose.override.yaml match production's; with production's own values they reproduce the running app exactly")
    say("WHAT CHANGES (production app only)")
    say("- image: %s (%s) -> %s" % (plan["old_image_ref"], plan["old_image"][:19], plan["image_ref"]))
    for k, a, b, why in plan["env_changes"]:
        say("- %s: %s -> %s (%s)" % (k, "unset" if a is None else a, "unset" if b is None else b, why))
    say("- start command: production's own, kept: %s (the new image's default would be: %s)" % (
        " ".join(plan["command"]), " ".join(plan["image_command"] or [])))
    say("- unchanged: volumes %s; networks %s; user %s; restart policy; healthcheck; stop grace; every other environment value; PRICE_ACTION_LAB_ENABLED unset = OFF" % (
        ", ".join("%s->%s" % (m[0], m[1]) for m in mounts(app)), ",".join(networks(app)), app["Config"].get("User")))
    say("- not touched: %s (nginx is only reloaded after the app is healthy)" % ", ".join(sorted(plan["others"])))
    say("- downtime: the app restarts once (about 40-90 s); the web proxy answers 502 meanwhile")

def wait_healthy(name, seconds):
    """'healthy', or why not. A restart, exit or 'unhealthy' ends the wait at once."""
    end = time.time() + seconds
    while time.time() < end:
        c = inspect(name)
        if c is None: time.sleep(3); continue
        st = c["State"]
        if c.get("RestartCount", 0) or st["Status"] == "restarting":
            return "crashing (restarted %d times, OOM %s)" % (c.get("RestartCount", 0), st.get("OOMKilled"))
        if st["Status"] in ("exited", "dead"):
            return "%s (exit code %s, OOM %s)" % (st["Status"], st.get("ExitCode"), st.get("OOMKilled"))
        health = (st.get("Health") or {}).get("Status")
        if health == "healthy": return "healthy"
        if health == "unhealthy": return "unhealthy"
        time.sleep(3)
    return "not healthy after %d s" % seconds

def in_app(code):
    rc, out, err = sh("docker", "exec", APP, "python", "-c", code, timeout=60)
    return out.strip() if rc == 0 else "ERROR"

def reload_proxy(problems):
    ng = inspect(NGINX)
    if not ng or not ng["State"]["Running"]:
        problems.append("web proxy not running"); return
    script = os.path.join(WD, "nginx", "reload-config.sh")
    if os.path.isfile(script):                       # the same reload scripts/deploy.sh does
        with open(script) as f:
            rc, out, err = sh("docker", "exec", "-i", NGINX, "sh", "-s", stdin=f, timeout=120)
    else:
        rc, out, err = sh("docker", "exec", NGINX, "nginx", "-s", "reload")
    if rc:
        problems.append("web proxy reload"); say("web proxy reload failed: %s" % (err.strip() or out.strip())[-200:]); return
    via = through_proxy()
    say("web proxy reloaded; https://%s/health through the proxy: %s" % (SITE, via))
    if not via.startswith("ok"): problems.append("the web proxy does not reach the app")

def through_proxy():
    """GET /health the way a visitor does: through the web proxy published on this host
    (HTTPS with the site's name; plain HTTP where no certificate is installed, as in tests)."""
    import socket, ssl
    req = ("GET /health HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % SITE).encode()
    seen = []
    for port in (443, 80):
        for attempt in range(5):                      # the proxy may need a moment after a reload
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
                seen.append("port %d: %s" % (port, type(e).__name__)); break
            head, _, body = data.partition(b"\r\n\r\n")
            parts = head.split(b" ", 2)
            status = parts[1].decode(errors="replace") if len(parts) > 1 else "?"
            if status == "200" and b'"status"' in body: return "ok (port %d)" % port
            if status in ("301", "302", "307", "308"): seen.append("port %d: redirect" % port); break
            seen.append("port %d: HTTP %s" % (port, status)); time.sleep(2)
    return "NO ANSWER (%s)" % "; ".join(seen[-3:])

def recreate(files, values, timeout=600):
    compose(files, values, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "app", timeout=timeout)

def put_back(rb, reason):
    if (inspect(rb["old_image_ref"]) or {}).get("Id") != rb["old_image"]:
        raise Blocked("%s no longer points to the previous image %s" % (rb["old_image_ref"], rb["old_image"][:19]))
    # production's own two files with the recorded values name exactly the previous image; no extra file,
    # so the app is recreated as scripts/deploy.sh created it (no build, no pull)
    recreate(FILES, rb["values"])
    state = wait_healthy(APP, HEALTH_WAIT)
    c = inspect(APP) or {"Image": None, "Config": {"Labels": {}}}
    lab = c["Config"].get("Labels") or {}
    same = (c["Image"] == rb["old_image"] and lab.get("com.docker.compose.config-hash") == rb["old_config_hash"]
            and lab.get("com.docker.compose.project.config_files") == ",".join(FILES))
    say("%s: previous image %s back, %s; %s" % (reason, rb["old_image_ref"], state,
        "exactly the app that ran before (same Compose config hash)" if same else "NOT the same configuration as before"))
    return state == "healthy" and same

def apply():
    try: old = json.load(open(PLAN))
    except (OSError, ValueError): raise Blocked("run 'plan' first")
    if time.time() - old.get("at_epoch", 0) > 7200: raise Blocked("the plan is older than 2 hours; run 'plan' again")
    app, plan, new_env = check_all(deployed_at=old["after_values"]["DEPLOYED_AT"])
    for k in ("image_id", "overlay_sha256", "app_hash_before", "app_hash_after", "old_image", "old_container", "backup",
              "before_values", "after_values", "env_digest", "command"):
        if old.get(k) != plan.get(k): raise Blocked("something changed since the plan (%s); run 'plan' again" % k)
    rollback = {"at": now(), "old_image": app["Image"], "old_image_ref": app["Config"]["Image"],
                "old_config_hash": app["Config"]["Labels"].get("com.docker.compose.config-hash"),
                "values": plan["before_values"], "mounts": mounts(app), "networks": networks(app), "user": app["Config"].get("User")}
    with open(ROLLBACK, "w") as f: json.dump(rollback, f, indent=1)
    say("recorded the current app for rollback: %s (%s)" % (rollback["old_image_ref"], rollback["old_image"][:19]))
    recreate(FILES + [OVERLAY], plan["after_values"])
    say("%s recreated with the Strategy Intelligence image; waiting for it to be healthy (up to %d minutes)" % (APP, HEALTH_WAIT // 60))
    state = wait_healthy(APP, HEALTH_WAIT)
    if state != "healthy":
        back = put_back(rollback, "the new app was %s, so it was rolled back" % state)
        problems = []
        reload_proxy(problems)
        raise Blocked("the new app was %s; production is %s" % (state, "back on the previous image and healthy" if back and not problems else "NOT confirmed healthy - check now"))
    new = inspect(APP)
    problems = []
    if new["Image"] != plan["image_id"]: problems.append("image")
    if mounts(new) != rollback["mounts"]: problems.append("volumes")
    if networks(new) != rollback["networks"]: problems.append("networks")
    if new["Config"].get("User") != rollback["user"]: problems.append("user")
    got = env_dict(new)
    if got != new_env:
        problems.append("environment (%s)" % ", ".join(sorted(k for k in set(got) | set(new_env) if got.get(k) != new_env.get(k))))
    if (new["Config"].get("Cmd") or []) != plan["command"]: problems.append("start command")
    say("container check: %s" % ("image, volumes, networks, user, start command and all %d environment values as planned" % len(new_env)
                                 if not problems else "DIFFERS: " + ", ".join(problems)))
    health = in_app("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/health',timeout=10)); print(json.dumps({'status':d.get('status'),'pa':(d.get('features') or {}).get('price_action_lab_enabled')}))")
    retired = in_app("import json,urllib.request as u; d=json.load(u.urlopen('http://127.0.0.1:8000/openapi.json',timeout=60)); "
                     "ops=[(p,m,o.get('tags')) for p,i in d.get('paths',{}).items() if '/research/price-action' in p for m,o in i.items() if isinstance(o,dict)]; "
                     "si=[p for p in d.get('paths',{}) if p.startswith('/api/v2/strategy-intelligence')]; "
                     "print(json.dumps({'operations':len(ops),'not_retired':sorted({p for p,m,t in ops if t!=['retired-price-action']}),'strategy_intelligence_routes':len(si)}))")
    say("/health: %s; PA routes and Strategy Intelligence routes: %s" % (health, retired))
    try: h = json.loads(health)
    except ValueError: h = {}
    if h.get("status") != "ok" or h.get("pa") is not False: problems.append("/health")
    try: r = json.loads(retired)
    except ValueError: r = {}
    if not r.get("operations") or r.get("not_retired"): problems.append("PA routes not all retired")
    if not r.get("strategy_intelligence_routes"): problems.append("Strategy Intelligence routes missing")
    reload_proxy(problems)
    after = others()
    moved = sorted(n for n in plan["others"] if after.get(n) != plan["others"][n])
    say("other containers: %s" % ("unchanged (same container, image, state, start time, restart count)" if not moved else "CHANGED: " + ", ".join(moved)))
    if moved: problems.append("other containers")
    receipt = {"at": now(), "image_id": plan["image_id"], "health": health, "routes": retired, "problems": problems, "rollback": ROLLBACK}
    with open(os.path.join(WORK, "deployed-%s.json" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())), "w") as f: json.dump(receipt, f, indent=1)
    if problems: raise Blocked("deployed, but these checks failed: %s. The new app is left running; 'rollback' puts the previous image back" % ", ".join(problems))
    say("DEPLOYED: production runs Strategy Intelligence (%s); Price Action Lab OFF. Next: prod-si-check measure." % COMMIT[:12])

def do_rollback():
    try: rb = json.load(open(ROLLBACK))
    except (OSError, ValueError): raise Blocked("no rollback record; 'apply' has not run")
    files_gate()
    img = inspect(rb["old_image_ref"])
    if not img or img["Id"] != rb["old_image"]: raise Blocked("the previous image %s is not available as recorded" % rb["old_image_ref"])
    before = others()
    ok = put_back(rb, "rollback")
    problems = []
    reload_proxy(problems)
    after = others()
    say("other containers: %s" % ("unchanged" if all(after.get(n) == before[n] for n in before) else "CHANGED - check now"))
    if not ok or problems: raise Blocked("rollback did not reach a healthy previous app: %s" % (", ".join(problems) or "app not healthy"))
    say("ROLLED BACK: production runs the previous image (%s) and is healthy. Data was not touched." % rb["old_image_ref"])

try:
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    if MODE == "plan":
        app, plan, _ = check_all()
        with open(PLAN, "w") as f: json.dump(plan, f, indent=1)
        show(app, plan)
        say("PLAN READY (valid 2 hours). Nothing was changed.")
    elif MODE == "apply":
        apply()
    else:
        do_rollback()
except Blocked as e:
    print("BLOCKED: %s." % e); sys.exit(2)
PY
