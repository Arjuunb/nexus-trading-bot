#!/bin/sh
# Read-only: which Compose files really define the running staging project, before the
# Price Action Lab retirement deploy.
#
#   show   for every staging container: the Compose files, env file and config hash it was
#          created with; every YAML file in the staging folders (memory/image overlays are printed
#          in full when they hold no secret-looking line); variable names in the env file (values
#          only for image, project, uid and on/off switches); for each candidate memory overlay,
#          what the API's memory and config hash would be; and the local nexus images
#
# It changes nothing and starts nothing. Environment values are never printed apart from the
# fixed non-secret list above.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in show) ;; *) echo "usage: staging-config-check.sh show"; exit 64 ;; esac
exec python3 -I - <<'PY'
import hashlib, json, os, re, subprocess

PROJECT = "nexus-remote-stage-rc3-28acdbc"
ROOTS = ["/home/ubuntu/nexus-rc3-staging-28acdbc"]
SECRETISH = re.compile(r"://|@|KEY|SECRET|PASSWORD|PASSWD|TOKEN|DSN|CREDENTIAL", re.I)
SAFE_ENV = re.compile(r"[A-Z0-9_]*(_IMAGE|_PROJECT|_UID|_GID|_ENABLED|_PROFILE|_PROFILES)")

def sh(*a, timeout=120):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def docker_json(*a):
    rc, out, err = sh("docker", *a)
    if rc: raise SystemExit("docker %s failed: %s" % (a[0], err.strip()[:200]))
    return json.loads(out)

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

print("== Compose labels of each staging container")
rc, out, _ = sh("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT)
containers = {}
for cid in out.split():
    c = docker_json("inspect", cid)[0]
    lab = c["Config"].get("Labels") or {}
    svc = lab.get("com.docker.compose.service")
    containers[svc] = c
    print("- %s created %s" % (svc, c["Created"][:19]))
    print("    files %s" % lab.get("com.docker.compose.project.config_files"))
    print("    env file %s  working_dir %s" % (lab.get("com.docker.compose.project.environment_file"),
          lab.get("com.docker.compose.project.working_dir")))
    print("    config-hash %s  image label %s" % ((lab.get("com.docker.compose.config-hash") or "-")[:16],
          lab.get("com.docker.compose.image", "-")[:30]))
    hc = c["HostConfig"]
    print("    memory %s memory+swap %s cpus %s" % (hc.get("Memory"), hc.get("MemorySwap"), (hc.get("NanoCpus") or 0) / 1e9))

print("== YAML files in the staging folders")
roots = list(ROOTS)
for base in ("/home/ubuntu", "/root"):
    try:
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if os.path.isdir(p) and not os.path.islink(p) and re.search(r"intelligence-recovery|nexus-rc3|staging|task6", name) and p not in roots:
                roots.append(p)
    except OSError:
        pass
yamls = []
for root in roots:
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "tls")]
        for f in fs:
            if f.endswith((".yaml", ".yml")):
                p = os.path.join(d, f)
                try: st = os.stat(p)
                except OSError: continue
                yamls.append(p)
                print("- %s (%d bytes, %s, sha256 %s)" % (p, st.st_size,
                      __import__("time").strftime("%Y-%m-%d %H:%M", __import__("time").gmtime(st.st_mtime)), sha(p)[:16]))
                if re.search(r"memory|image|override|worker|recovery", p) and st.st_size <= 4096:
                    text = open(p, errors="replace").read()
                    if SECRETISH.search(re.sub(r"#.*", "", text)):
                        print("    (content not shown: has a secret-looking word)")
                    else:
                        for line in text.splitlines():
                            print("    | " + line)

api = containers.get("api")
if not api:
    raise SystemExit("no staging api container found; nothing else to check. Nothing was changed.")
lab = api["Config"].get("Labels") or {}
files = [f for f in (lab.get("com.docker.compose.project.config_files") or "").split(",") if f]
envs = [f for f in (lab.get("com.docker.compose.project.environment_file") or "").split(",") if f]
wd = lab.get("com.docker.compose.project.working_dir")

print("== Env file variable names")
for e in envs:
    if not os.path.isfile(e):
        print("- %s MISSING" % e); continue
    print("- %s" % e)
    for line in open(e, errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip("'\"")
        if SAFE_ENV.fullmatch(k) and not SECRETISH.search(v):
            print("    %s=%s" % (k, v))
        else:
            print("    %s (set)" % k)

def render(extra, profile=True):
    cmd = ["docker", "compose", "--project-name", PROJECT]
    if wd: cmd += ["--project-directory", wd]
    for f in files + extra: cmd += ["-f", f]
    for f in envs: cmd += ["--env-file", f]
    if profile: cmd += ["--profile", "research"]
    rc, out, err = sh(*cmd, "config", "--format", "json")
    if rc: return None, (err.strip().splitlines() or ["?"])[-1][:300]
    rc2, out2, err2 = sh(*cmd, "config", "--hash", "api,research-worker")
    hashes = dict(l.split(None, 1) for l in out2.splitlines() if " " in l) if rc2 == 0 else {}
    return (json.loads(out), hashes), None

def describe(label, extra):
    got, err = render(extra)
    print("- %s" % label)
    if err:
        print("    render failed: %s" % err); return
    cfg, hashes = got
    for s in ("api", "research-worker"):
        svc = cfg.get("services", {}).get(s)
        if not svc: print("    %s: not in this render" % s); continue
        c = containers.get(s) or {}
        clab = (c.get("Config") or {}).get("Labels") or {}
        live = clab.get("com.docker.compose.config-hash")
        print("    %s: image %s mem_limit %s memswap_limit %s cpus %s; hash %s %s" % (
            s, svc.get("image"), svc.get("mem_limit"), svc.get("memswap_limit"), svc.get("cpus"),
            (hashes.get(s) or "-")[:16], "MATCHES the live container" if live and hashes.get(s) == live else "differs from the live container"))

print("== What Compose would create")
describe("label files only (with --profile research)", [])
for p in yamls:
    if "memory" in os.path.basename(p) and p not in files:
        describe("label files + %s last" % p, [p])

print("== Local nexus images")
rc, out, _ = sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedSince}}")
for line in sorted(out.splitlines()):
    if re.match(r"(nexus-ci|nexus-rc3|ghcr\.io/arjuunb/nexus-trading-bot-(api|worker))", line):
        print("  " + line)
print("Nothing was changed.")
PY
