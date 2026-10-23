#!/bin/sh
# Read-only inventory of the existing staging project, before the Price Action Lab retirement deploy.
#
#   show   print how staging is set up today: containers, images, limits, mounts, Compose files
#          (rendered, values hidden), volumes and their SQLite files, the uploaded image package,
#          production identity and free disk
#
# It changes nothing: no container is started, stopped or created, no file is written outside
# the runner's own log, and no environment value is printed except a fixed list of non-secret
# switches and paths.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in show) ;; *) echo "usage: staging-inventory.sh show"; exit 64 ;; esac
exec python3 -I - <<'PY'
import hashlib, json, os, re, subprocess

PROJECT = "nexus-remote-stage-rc3-28acdbc"
PACKAGE = "/home/ubuntu/nexus-pa-retirement/unpublished-staging-images-39861495119-1.zip"
PACKAGE_SHA256 = "dedef7da62fb90aff5078614186684076849b699559e16bb95c1fa021f4c616e"
SAFE_VALUE = re.compile(r"PRICE_ACTION_LAB_ENABLED|HUB_ENV|HUB_DATA_DIR|HUB_AUTO_ENGINE|HUB_ENABLE_EXTERNAL_LIVE|"
                        r"HUB_USE_LIVE_DATA|HUB_RESEARCH_AUTOSTART|HUB_MONITOR_AGENT|HUB_DAILY_REPORT_HOUR|"
                        r"HUB_[A-Z_]*_POLL|HUB_[A-Z_]*_DB|NEXUS_[A-Z_]*_(PATH|FILE|BACKEND)|NEXUS_SHADOW_SUPERVISED")
SECRETISH = re.compile(r"://|@|KEY|SECRET|PASSWORD|TOKEN|DSN", re.I)

def sh(*a, timeout=60):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def docker_json(*a):
    rc, out, err = sh("docker", *a)
    if rc: raise SystemExit("docker %s failed: %s" % (a[0], err.strip()[:200]))
    return json.loads(out)

def gb(n): return "%.2f GB" % (n / 1e9)
def short(i): return (i or "")[:19]
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""): h.update(b)
    return h.hexdigest()

print("== host")
rc, out, _ = sh("docker", "version", "--format", "{{.Server.Version}}")
rc2, out2, _ = sh("docker", "compose", "version", "--short")
info = docker_json("info", "--format", "{{json .}}")
print("docker %s, compose %s, storage %s (%s)" % (out.strip(), out2.strip(), info.get("Driver"),
      "containerd image store" if any("containerd" in str(x) for x in (info.get("DriverStatus") or [])) else "classic image store"))
s = os.statvfs("/"); print("disk / free %s" % gb(s.f_bavail * s.f_frsize))

print("== staging containers (project %s)" % PROJECT)
rc, out, _ = sh("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT)
ids = out.split()
compose_labels = None
api_env = None
for cid in ids:
    c = docker_json("inspect", cid)[0]
    lab = c["Config"].get("Labels") or {}
    hc = c["HostConfig"]
    img = docker_json("image", "inspect", c["Image"])[0] if sh("docker", "image", "inspect", c["Image"])[0] == 0 else {}
    ilab = (img.get("Config") or {}).get("Labels") or {}
    st = c["State"]
    print("- %s  service=%s  state=%s%s  exit=%s  started=%s  finished=%s" % (
        c["Name"].lstrip("/"), lab.get("com.docker.compose.service"), st["Status"],
        " health=" + st["Health"]["Status"] if st.get("Health") else "", st.get("ExitCode"),
        st.get("StartedAt", "")[:19], st.get("FinishedAt", "")[:19]))
    print("    image %s id %s revision %s source %s" % (c["Config"]["Image"], c["Image"],
          (ilab.get("org.opencontainers.image.revision") or "-")[:12], (ilab.get("nexus.source.sha256") or "-")[:12]))
    print("    restart %s:%s  memory %s  memory+swap %s  cpus %s  pids %s  user %s  read_only %s" % (
        hc["RestartPolicy"]["Name"], hc["RestartPolicy"].get("MaximumRetryCount"), hc.get("Memory"), hc.get("MemorySwap"),
        (hc.get("NanoCpus") or 0) / 1e9, hc.get("PidsLimit"), c["Config"].get("User") or "-", hc.get("ReadonlyRootfs")))
    for m in c.get("Mounts", []):
        print("    mount %s %s -> %s %s" % (m["Type"], m.get("Name") or m.get("Source"), m["Destination"], "rw" if m.get("RW") else "ro"))
    print("    networks %s  depends_on %s" % (",".join(sorted((c["NetworkSettings"].get("Networks") or {}).keys())),
          lab.get("com.docker.compose.depends_on", "-")))
    if compose_labels is None and lab.get("com.docker.compose.oneoff") != "True":
        compose_labels = lab
    if lab.get("com.docker.compose.service") == "api":
        api_env = c["Config"].get("Env") or []
        compose_labels = lab

print("== API environment (names; values only for non-secret switches and paths)")
for e in sorted(api_env or []):
    k, _, v = e.partition("=")
    if SAFE_VALUE.fullmatch(k) and not SECRETISH.search(v):
        print("  %s=%s" % (k, v))
    else:
        print("  %s (set)" % k)
if not any(e.startswith("PRICE_ACTION_LAB_ENABLED=") for e in (api_env or [])):
    print("  PRICE_ACTION_LAB_ENABLED is not set (new code treats unset as false)")

print("== Compose files used at the last 'up'")
wd = files = envf = None
if compose_labels:
    wd = compose_labels.get("com.docker.compose.project.working_dir")
    files = [f for f in (compose_labels.get("com.docker.compose.project.config_files") or "").split(",") if f]
    envf = [f for f in (compose_labels.get("com.docker.compose.project.environment_file") or "").split(",") if f]
    print("working_dir %s" % wd)
    for i, f in enumerate(files, 1):
        ok = os.path.isfile(f)
        print("  %d. %s %s" % (i, f, "(%d bytes, sha256 %s)" % (os.path.getsize(f), sha(f)[:16]) if ok else "MISSING"))
    for f in envf:
        print("  env file %s %s" % (f, "present" if os.path.isfile(f) else "MISSING"))
    print("  compose version label %s" % compose_labels.get("com.docker.compose.version"))

if files and all(os.path.isfile(f) for f in files):
    cmd = ["docker", "compose", "--project-name", PROJECT]
    if wd: cmd += ["--project-directory", wd]
    for f in files: cmd += ["-f", f]
    for f in envf or []:
        if os.path.isfile(f): cmd += ["--env-file", f]
    rc, out, err = sh(*cmd, "config", "--format", "json", timeout=120)
    print("== Rendered configuration (exit %d; environment values hidden)" % rc)
    if rc:
        print("  " + err.strip().splitlines()[-1][:300] if err.strip() else "  (no message)")
    else:
        cfg = json.loads(out)
        for name, svc in sorted(cfg.get("services", {}).items()):
            vols = ["%s:%s%s" % (v.get("source"), v.get("target"), ":ro" if v.get("read_only") else "") for v in svc.get("volumes", [])]
            env = svc.get("environment") or {}
            print("- %s image=%s pull_policy=%s mem_limit=%s memswap_limit=%s cpus=%s restart=%s profiles=%s" % (
                name, svc.get("image"), svc.get("pull_policy", "-"), svc.get("mem_limit"), svc.get("memswap_limit"),
                svc.get("cpus"), svc.get("restart"), ",".join(svc.get("profiles", [])) or "-"))
            print("    volumes %s" % ", ".join(vols))
            print("    depends_on %s  networks %s" % (",".join(sorted((svc.get("depends_on") or {}).keys())) or "-",
                  ",".join(sorted((svc.get("networks") or {}).keys())) or "-"))
            pa = env.get("PRICE_ACTION_LAB_ENABLED")
            print("    environment: %d names; PRICE_ACTION_LAB_ENABLED=%s" % (len(env), pa if pa is not None else "unset"))
        print("  volumes: %s" % ", ".join("%s->%s" % (k, (v or {}).get("name", k)) for k, v in sorted(cfg.get("volumes", {}).items())))

print("== Staging volumes")
rc, out, _ = sh("docker", "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT)
for vname in out.split():
    v = docker_json("volume", "inspect", vname)[0]
    mp = v["Mountpoint"]; total = 0; rows = []
    for root, dirs, fs in os.walk(mp):
        for f in fs:
            p = os.path.join(root, f)
            try: st = os.lstat(p)
            except OSError: continue
            total += st.st_size
            rows.append((st.st_size, os.path.relpath(p, mp)))
    print("- %s: %s in %d files" % (vname, gb(total), len(rows)))
    shown = [r for r in sorted(rows, reverse=True)
             if r[0] >= 100_000_000 or re.search(r"\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$", r[1])]
    for size, rel in shown[:40]:
        print("    %s  %s" % (gb(size), rel))
    if len(shown) > 40: print("    ... %d more" % (len(shown) - 40))

print("== Uploaded image package")
if os.path.isfile(PACKAGE):
    got = sha(PACKAGE)
    print("%s %s sha256 %s... %s" % (PACKAGE, gb(os.path.getsize(PACKAGE)), got[:16],
          "MATCHES GitHub's recorded digest" if got == PACKAGE_SHA256 else "DOES NOT MATCH (expected %s...)" % PACKAGE_SHA256[:16]))
else:
    print("not uploaded yet (expected at %s)" % PACKAGE)

print("== Production (for before/after comparison; not changed)")
rc, out, _ = sh("docker", "ps", "-a", "--format", "{{.Names}}")
for name in sorted(n for n in out.split() if n.startswith("nexus-trading-bot-")):
    c = docker_json("inspect", name)[0]
    print("- %s state=%s image %s started %s restarts %d" % (name, c["State"]["Status"], short(c["Image"]),
          c["State"].get("StartedAt", "")[:19], c.get("RestartCount", 0)))
print("Nothing was changed.")
PY
