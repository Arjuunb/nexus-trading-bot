#!/bin/sh
# Read-only inventory of production, before deploying Strategy Intelligence (PR #48).
#
#   show   print how production is set up today: containers, images, limits, mounts, the Compose
#          files and checkout they were started from, non-secret switches (sign-in mode, PA Lab),
#          data volume files and their owner, open Price Action Lab positions, free disk and memory
#
# It changes nothing: no container is started, stopped, created or entered, git runs without
# optional locks (no index refresh), SQLite files are opened read-only, and no environment value
# is printed except a fixed list of non-secret switches.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
case "${1:-}" in show) ;; *) echo "usage: prod-inventory.sh show"; exit 64 ;; esac
exec python3 -I - <<'PY'
import hashlib, json, os, re, sqlite3, subprocess

PROJECT = "nexus-trading-bot"
SAFE_VALUE = re.compile(r"HUB_AUTH_MODE|PRICE_ACTION_LAB_ENABLED|HUB_ENV|HUB_DATA_DIR|HUB_AUTO_ENGINE|"
                        r"HUB_ENABLE_EXTERNAL_LIVE|HUB_USE_LIVE_DATA|HUB_RESEARCH_AUTOSTART|HUB_MONITOR_AGENT|"
                        r"HUB_CANDLE_SYNC|HUB_DAILY_REPORT_HOUR|HUB_ADAPTIVE_LAB_AUTOSTART|HUB_GUARDIAN_INTERVAL|"
                        r"HUB_STATUS_INTERVAL|HUB_[A-Z_]*_POLL(_S)?|HUB_[A-Z_]*_DB|NEXUS_ACCEPTANCE_READ_ONLY|"
                        r"GIT_COMMIT|GIT_BRANCH|DEPLOYED_AT|PORT")
SECRETISH = re.compile(r"://|@|KEY|SECRET|PASSWORD|TOKEN|DSN", re.I)

def sh(*a, timeout=60):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def docker_json(*a):
    rc, out, err = sh("docker", *a)
    if rc: raise SystemExit("docker %s failed: %s" % (a[0], err.strip()[:200]))
    return json.loads(out)

def gb(n): return "%.2f GB" % (n / 1e9)
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""): h.update(b)
    return h.hexdigest()

def ro(path):
    """Open without creating anything. While the app has the file open its -shm exists and a
    read-only reader just joins it; with no -wal at all the file is complete and is opened as
    immutable. Anything else (a -wal left without -shm) is read from a private copy."""
    import shutil, tempfile
    wal, shm = os.path.exists(path + "-wal"), os.path.exists(path + "-shm")
    if shm:
        uri = "file:%s?mode=ro" % path
    elif not wal:
        uri = "file:%s?mode=ro&immutable=1" % path
    else:
        import atexit
        tmp = tempfile.mkdtemp(prefix="prod-inventory-")
        atexit.register(shutil.rmtree, tmp, True)   # the copy never outlives this run
        for suffix in ("", "-wal"):
            shutil.copy2(path + suffix, os.path.join(tmp, "copy.db" + suffix))
        uri = "file:%s?mode=ro" % os.path.join(tmp, "copy.db")
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    return db

print("== host")
rc, out, _ = sh("docker", "version", "--format", "{{.Server.Version}}")
rc2, out2, _ = sh("docker", "compose", "version", "--short")
info = docker_json("info", "--format", "{{json .}}")
print("docker %s, compose %s, storage %s (%s)" % (out.strip(), out2.strip(), info.get("Driver"),
      "containerd image store" if any("containerd" in str(x) for x in (info.get("DriverStatus") or [])) else "classic image store"))
s = os.statvfs("/"); print("disk / free %s of %s" % (gb(s.f_bavail * s.f_frsize), gb(s.f_blocks * s.f_frsize)))
mem = dict(l.split(":", 1) for l in open("/proc/meminfo"))
print("memory available %.2f GiB of %.2f GiB, swap total %.2f GiB" % (
    int(mem["MemAvailable"].split()[0]) / 2**20, int(mem["MemTotal"].split()[0]) / 2**20,
    int(mem["SwapTotal"].split()[0]) / 2**20))

print("== all containers on this host")
rc, out, _ = sh("docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}")
for line in sorted(out.splitlines()):
    print("  " + line)

print("== production containers (project %s)" % PROJECT)
rc, out, _ = sh("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT)
compose_labels = app_env = app_image = None
app_mounts, bind_sources = [], []
for cid in out.split():
    c = docker_json("inspect", cid)[0]
    lab = c["Config"].get("Labels") or {}
    hc = c["HostConfig"]; st = c["State"]
    img = docker_json("image", "inspect", c["Image"])[0] if sh("docker", "image", "inspect", c["Image"])[0] == 0 else {}
    ilab = (img.get("Config") or {}).get("Labels") or {}
    print("- %s  service=%s  state=%s%s  started=%s  restarts=%d" % (
        c["Name"].lstrip("/"), lab.get("com.docker.compose.service"), st["Status"],
        " health=" + st["Health"]["Status"] if st.get("Health") else "", st.get("StartedAt", "")[:19],
        c.get("RestartCount", 0)))
    print("    image %s id %s revision %s user %s" % (c["Config"]["Image"], c["Image"],
          (ilab.get("org.opencontainers.image.revision") or "-")[:12], (img.get("Config") or {}).get("User") or "-"))
    print("    restart %s  memory %s  cpus %s  pids %s  read_only %s  stop_grace %s" % (
        hc["RestartPolicy"]["Name"], hc.get("Memory"), (hc.get("NanoCpus") or 0) / 1e9, hc.get("PidsLimit"),
        hc.get("ReadonlyRootfs"), c["Config"].get("StopTimeout")))
    for m in c.get("Mounts", []):
        print("    mount %s %s -> %s %s" % (m["Type"], m.get("Name") or m.get("Source"), m["Destination"], "rw" if m.get("RW") else "ro"))
        if m["Type"] == "bind":
            bind_sources.append(m.get("Source") or "")
    if lab.get("com.docker.compose.service") == "app" and st["Status"] == "running":
        app_mounts = c.get("Mounts", [])
        print("    compose labels: %s" % ", ".join("%s=%s" % (k.replace("com.docker.compose.", ""), v)
              for k, v in sorted(lab.items()) if k.startswith("com.docker.compose.") and "config_hash" not in k))
    if lab.get("com.docker.compose.oneoff") != "True" and compose_labels is None:
        compose_labels = lab
    if lab.get("com.docker.compose.service") == "app" and st["Status"] == "running":
        compose_labels, app_env, app_image = lab, c["Config"].get("Env") or [], c["Image"]

print("== app environment (names; values only for non-secret switches)")
names = set()
for e in sorted(app_env or []):
    k, _, v = e.partition("=")
    names.add(k)
    print("  %s=%s" % (k, v) if SAFE_VALUE.fullmatch(k) and not SECRETISH.search(v) else "  %s (set)" % k)
for k in ("HUB_AUTH_MODE", "PRICE_ACTION_LAB_ENABLED", "NEXUS_ACCEPTANCE_READ_ONLY"):
    if k not in names:
        print("  %s is not set" % k)

print("== Compose files and checkout used at the last 'up'")
wd = files = envf = None
if compose_labels:
    wd = compose_labels.get("com.docker.compose.project.working_dir")
    files = [f for f in (compose_labels.get("com.docker.compose.project.config_files") or "").split(",") if f]
    envf = [f for f in (compose_labels.get("com.docker.compose.project.environment_file") or "").split(",") if f]
    print("working_dir %s" % wd)
    for i, f in enumerate(files, 1):
        print("  %d. %s %s" % (i, f, "(%d bytes, sha256 %s)" % (os.path.getsize(f), sha(f)[:16]) if os.path.isfile(f) else "MISSING"))
    for f in envf:
        print("  env file %s %s" % (f, "present" if os.path.isfile(f) else "MISSING"))
if not wd:
    # No working_dir label: look for the checkout above the project's bind mounts.
    for src in bind_sources:
        d = os.path.dirname(src)
        while d not in ("", "/"):
            if os.path.isdir(os.path.join(d, ".git")):
                wd = d; break
            d = os.path.dirname(d)
        if wd:
            print("  no working_dir label; checkout found above the bind mounts: %s" % wd)
            for name in ("compose.yaml", "compose.override.yaml", "docker-compose.yml", ".env"):
                f = os.path.join(wd, name)
                print("  %s %s" % (name, "(%d bytes, sha256 %s)" % (os.path.getsize(f), sha(f)[:16]) if os.path.isfile(f) else "absent"))
            break
if wd and os.path.isdir(os.path.join(wd, ".git")):
    g = ["git", "--no-optional-locks", "-c", "safe.directory=*", "-C", wd]
    for label, args in (("HEAD", ["rev-parse", "HEAD"]), ("branch", ["branch", "--show-current"]),
                        ("upstream", ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])):
        rc, out, err = sh(*(g + args))
        print("  git %s: %s" % (label, out.strip() if rc == 0 else "(none)"))
    rc, out, _ = sh(*(g + ["status", "--porcelain", "--untracked-files=no"]))
    changed = [l for l in out.splitlines() if l.strip()]
    print("  git tracked files changed in the checkout: %d%s" % (len(changed), "".join("\n    " + l for l in changed[:20])))
    rc, out, _ = sh(*(g + ["remote", "get-url", "origin"]))
    host = re.sub(r"^[a-z]+://([^@/]*@)?", "", out.strip()).split("/")[0] if rc == 0 else "-"
    print("  git remote host: %s" % host)
    for name in ("scripts/deploy.sh", "compose.guardian.yaml", ".env.guardian"):
        print("  %s %s" % (name, "present" if os.path.exists(os.path.join(wd, name)) else "absent"))

if app_image:
    rc, out, err = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "id", app_image, timeout=60)
    print("== app image user (throwaway container, no network, no volume): %s" % (out.strip() or err.strip()[:200]))

print("== production volumes (labelled by Compose, plus every volume the app mounts)")
rc, out, _ = sh("docker", "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + PROJECT)
names = out.split()
names += [m["Name"] for m in app_mounts if m.get("Type") == "volume" and m.get("Name") not in names]
data_dir = next((m["Source"] for m in app_mounts if m.get("Destination") == "/var/lib/tradexa"), None)
for vname in names:
    v = docker_json("volume", "inspect", vname)[0]
    mp = v["Mountpoint"]; total = 0; rows = []; owners = {}
    for root, dirs, fs in os.walk(mp):
        for f in fs:
            p = os.path.join(root, f)
            try: st = os.lstat(p)
            except OSError: continue
            total += st.st_size
            owners[(st.st_uid, st.st_gid)] = owners.get((st.st_uid, st.st_gid), 0) + 1
            rows.append((st.st_size, os.path.relpath(p, mp)))
    st = os.stat(mp)
    print("- %s: %s in %d files; top dir owner %d:%d; file owners %s" % (vname, gb(total), len(rows), st.st_uid, st.st_gid,
          ", ".join("%d:%d x%d" % (u, g, n) for (u, g), n in sorted(owners.items()))))
    for size, rel in sorted(rows, reverse=True)[:30]:
        if size >= 50_000_000 or re.search(r"\.(db|sqlite)(-wal|-shm)?$", rel):
            print("    %s  %s" % (gb(size), rel))
    if data_dir is None and (os.path.exists(os.path.join(mp, "smc_strategy_paper.db")) or os.path.exists(os.path.join(mp, "ledger.db"))):
        data_dir = mp

print("== Price Action Lab state (read-only; data dir %s)" % data_dir)
pa = os.path.join(data_dir or "/nonexistent", "price_action_paper.db")
if not os.path.isfile(pa):
    print("no price_action_paper.db in the data volume")
else:
    try:
        db = ro(pa)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, where in (("v2_positions", ""), ("v2_orders", " WHERE status IN ('OPEN','PENDING','ACCEPTED','NEW','PARTIALLY_FILLED')")):
            if table in tables:
                print("  %s: %d%s" % (table, db.execute("SELECT COUNT(*) FROM %s%s" % (table, where)).fetchone()[0],
                                       " open" if where else " open positions"))
        for table in ("pa_sessions", "sessions"):
            if table in tables:
                row = db.execute("SELECT * FROM %s ORDER BY rowid DESC LIMIT 1" % table).fetchone()
                cols = [d[0] for d in db.execute("SELECT * FROM %s LIMIT 0" % table).description]
                rec = dict(zip(cols, row)) if row else {}
                print("  latest %s: %s" % (table, {k: rec.get(k) for k in ("id", "status", "mode", "operating_mode", "symbol", "timeframe", "started_at", "ended_at") if k in rec}))
        db.close()
    except sqlite3.Error as exc:
        print("  could not read: %s" % exc)

print("Nothing was changed.")
PY
