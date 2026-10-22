#!/usr/bin/env python3
"""Nexus Proposal 01 / P0: read-only VPS operational evidence collector.

Run ON THE VPS HOST, as root, from the operator's trusted SSH session:

    sudo python3 collect_p0_evidence.py --out /root/p0-evidence

What it does (all read-only):
  * Docker metadata: containers, images, volumes, `system df`, logs (counts only).
  * Running source fingerprint: git blob hashes of a fixed list of files read
    through /proc/<pid>/root (no `docker exec`).
  * Disk: df, bounded metadata walks of data volumes and selected host dirs,
    largest files, bytes by modification day.
  * Resources: passive /proc + cgroup v2 sampling of the host and containers.
  * SQLite: header reads, then a connection opened with ``mode=ro&immutable=1``:
    SQLite takes no locks, never touches -wal/-shm, never creates a file and
    never writes. It sees the checkpointed state only (normally minutes behind
    the WAL), which is labelled in the output. Every query has a time limit.
    Table sizes are ESTIMATED from MIN/MAX(rowid) and a small rowid sample:
    no full-table scans, no dbstat on large files.
  * Backups: listing, manifests, header-consistency checks. Nothing is copied
    or restored.

What it never does: deploy, restart, exec into containers, write to any data
volume, delete, prune, vacuum, checkpoint, change configuration, print secret
values, print balances or P&L. The only file written is the output JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

SCHEMA = "nexus_p0_vps_evidence.v1"

# ---------------------------------------------------------------- safety ---
_DOCKER_READ_ONLY = {("ps",), ("inspect",), ("logs",), ("info",), ("version",), ("stats",),
                     ("image", "ls"), ("image", "inspect"), ("volume", "ls"),
                     ("volume", "inspect"), ("system", "df")}
_DOCKER_FORBIDDEN = {"rm", "rmi", "prune", "exec", "run", "cp", "kill", "stop", "restart",
                     "start", "update", "pull", "push", "tag", "save", "load", "build",
                     "create", "commit", "pause", "unpause", "rename", "attach", "import",
                     "export", "compose", "network", "plugin", "swarm", "service", "stack"}

# Environment values that are configuration, never credentials. Everything else
# in a container's environment is dropped without being read into the output.
ENV_VALUE_KEYS = {
    "GIT_COMMIT", "GIT_BRANCH", "DEPLOYED_AT", "HUB_ENV", "HUB_DATA_DIR", "PORT",
    "PRICE_ACTION_LAB_ENABLED", "HUB_RESEARCH_AUTOSTART", "HUB_ADAPTIVE_LAB_AUTOSTART",
    "HUB_AUTO_ENGINE", "HUB_USE_LIVE_DATA", "HUB_PA_POLL", "HUB_SMC_POLL", "HUB_LIVE_POLL",
    "HUB_GUARDIAN_INTERVAL", "HUB_JOURNAL_RECONCILE_S", "HUB_INSTANCE_SUPERVISOR_INTERVAL",
    "HUB_RETENTION_ROWS", "HUB_DAILY_REPORT_HOUR", "HUB_GUARDIAN_RECOVERY", "HUB_MONITOR_AGENT",
    "HUB_CANDLE_SYNC", "HUB_AUTH_MODE", "HUB_STATUS_INTERVAL", "HUB_WEEKLY_REVIEW_INTERVAL_S",
    "HUB_ACCEPTANCE_READ_ONLY", "HUB_EXTERNAL_LIVE_ENABLED"}
# Only whether these are set (true/false) is recorded.
ENV_PRESENCE_KEYS = {"HUB_MASTER_KEY", "HUB_MASTER_KEY_PREVIOUS", "SUPABASE_URL", "SUPABASE_KEY",
                     "SUPABASE_SERVICE_ROLE_KEY", "HUB_AUDIT_EXPORT_URL", "HUB_TELEGRAM_TOKEN",
                     "TELEGRAM_BOT_TOKEN", "HUB_LEDGER_MIRROR_DB", "DATABASE_URL"}

# Source files fingerprinted inside each running API container (git blob SHA-1).
FINGERPRINT_FILES = (
    "Dockerfile", "compose.yaml", "scripts/deploy.sh",
    "automation-hub/app.py", "automation-hub/webhook_api.py", "automation-hub/config.py",
    "automation-hub/services/backup.py", "automation-hub/services/feature_flags.py",
    "automation-hub/services/guardian/service.py", "automation-hub/services/journal_recorder.py",
    "automation-hub/services/adaptive_lab.py", "automation-hub/services/smc_agent_runtime.py",
    "automation-hub/services/price_action_lab.py", "automation-hub/services/price_action_governance.py",
    "automation-hub/routers/instance_visual_lab.py",
    "automation-hub/services/strategy_intelligence_service.py",
    "automation-hub-dashboard/src/app-context.ts",
)

SECRETISH = re.compile(r"(token|secret|password|passwd|apikey|api_key|authorization|bearer)\S*", re.I)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip(value, n=200):
    text = "" if value is None else str(value)
    text = SECRETISH.sub("[redacted]", text)
    return text if len(text) <= n else text[:n] + "…"


def docker_args_allowed(args) -> bool:
    if not args or any(str(a) in _DOCKER_FORBIDDEN for a in args):
        return False
    if args[0] == "stats" and "--no-stream" not in args:
        return False
    return (args[0],) in _DOCKER_READ_ONLY or tuple(args[:2]) in _DOCKER_READ_ONLY


class Runner:
    """All external commands go through here so tests can substitute fakes."""

    def docker(self, args, timeout=60) -> str:
        if not docker_args_allowed(args):
            raise PermissionError(f"docker {' '.join(map(str, args[:2]))} is not read-only")
        # Container logs carry tracebacks on stderr, so logs merge both streams.
        merge = args[0] == "logs"
        out = subprocess.run(["docker", *map(str, args)], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT if merge else subprocess.PIPE, text=True,
                             timeout=timeout, errors="replace")
        if out.returncode != 0:
            raise RuntimeError(f"docker {args[0]} failed: {clip(out.stderr or out.stdout, 300)}")
        return out.stdout

    def cmd(self, argv, timeout=30) -> str:
        allowed = {"df", "git", "crontab", "systemctl", "journalctl", "uname", "nproc"}
        if os.path.basename(argv[0]) not in allowed:
            raise PermissionError(f"{argv[0]} not allowed")
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""                       # a missing tool degrades one field, not the step
        return out.stdout if out.returncode == 0 else ""


def step(report, name, fn, *args, **kwargs):
    started = time.monotonic()
    try:
        report[name] = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- one failed step never stops the others
        report[name] = {"error": f"{type(exc).__name__}: {clip(exc, 300)}"}
    report.setdefault("_step_seconds", {})[name] = round(time.monotonic() - started, 2)
    return report[name]


# ------------------------------------------------------------ identity -----
def filter_env(env_list):
    values, present = {}, {}
    for item in env_list or []:
        key, _, value = str(item).partition("=")
        if key in ENV_VALUE_KEYS:
            values[key] = clip(value, 200)
        elif key in ENV_PRESENCE_KEYS:
            present[key] = bool(value)
    for key in ENV_PRESENCE_KEYS:
        present.setdefault(key, False)
    return {"values": values, "present": present}


def _labels(labels):
    keep = {}
    for key, value in (labels or {}).items():
        if key.startswith(("com.docker.compose.", "org.opencontainers.", "nexus.")):
            keep[key] = clip(value, 400)
    return keep


def containers(run: Runner):
    ids = run.docker(["ps", "-a", "--no-trunc", "--format", "{{.ID}}"]).split()
    if not ids:
        return []
    rows = json.loads(run.docker(["inspect", "--size", *ids], timeout=180))
    out = []
    for c in rows:
        state, host = c.get("State") or {}, c.get("HostConfig") or {}
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        out.append({
            "id": c.get("Id", "")[:64], "name": (c.get("Name") or "").lstrip("/"),
            "image_id": c.get("Image"), "image_ref": (c.get("Config") or {}).get("Image"),
            "created": c.get("Created"), "status": state.get("Status"),
            "started_at": state.get("StartedAt"), "finished_at": state.get("FinishedAt"),
            "pid": state.get("Pid"), "restart_count": c.get("RestartCount"),
            "oom_killed": state.get("OOMKilled"), "exit_code": state.get("ExitCode"),
            "health": (state.get("Health") or {}).get("Status"),
            "memory_limit": host.get("Memory"), "memory_swap_limit": host.get("MemorySwap"),
            "nano_cpus": host.get("NanoCpus"), "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
            "log_driver": (host.get("LogConfig") or {}).get("Type"),
            "log_options": (host.get("LogConfig") or {}).get("Config"),
            "size_rw": c.get("SizeRw"), "size_root_fs": c.get("SizeRootFs"),
            "graph_driver": (c.get("GraphDriver") or {}).get("Name"),
            "labels": _labels((c.get("Config") or {}).get("Labels")),
            "env": filter_env((c.get("Config") or {}).get("Env")),
            "ip_addresses": {n: v.get("IPAddress") for n, v in nets.items() if v.get("IPAddress")},
            "mounts": [{"type": m.get("Type"), "name": m.get("Name"), "source": m.get("Source"),
                        "destination": m.get("Destination"), "rw": m.get("RW")}
                       for m in c.get("Mounts") or []],
        })
    return out


def images(run: Runner, used_ids):
    rows = [json.loads(line) for line in
            run.docker(["image", "ls", "--all", "--no-trunc", "--digests", "--format", "{{json .}}"]).splitlines()
            if line.strip()]
    listed = [{"repository": r.get("Repository"), "tag": r.get("Tag"), "id": r.get("ID"),
               "digest": r.get("Digest"), "created": r.get("CreatedAt"), "size": r.get("Size")}
              for r in rows]
    wanted = {i for i in used_ids if i}
    for r in listed:
        repo = (r["repository"] or "").lower()
        if any(k in repo for k in ("nexus", "tradexa", "<none>", "trading")):
            wanted.add(r["id"])
    detail = []
    if wanted:
        for img in json.loads(run.docker(["image", "inspect", *sorted(wanted)], timeout=120)):
            cfg = img.get("Config") or {}
            detail.append({"id": img.get("Id"), "repo_tags": img.get("RepoTags"),
                           "repo_digests": img.get("RepoDigests"), "created": img.get("Created"),
                           "size": img.get("Size"), "labels": _labels(cfg.get("Labels")),
                           "env": filter_env(cfg.get("Env"))["values"],
                           "used_by_container": img.get("Id") in used_ids})
    return {"listed": listed, "inspected": detail}


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fingerprint_root(root: str):
    """Git blob hashes of FINGERPRINT_FILES under ``root`` (the image's /app)."""
    out = {}
    for rel in FINGERPRINT_FILES:
        path = os.path.join(root, rel)
        try:
            with open(path, "rb") as fh:
                data = fh.read(4_000_001)
            out[rel] = git_blob_sha(data) if len(data) <= 4_000_000 else "TOO_LARGE"
        except FileNotFoundError:
            out[rel] = None
        except OSError as exc:
            out[rel] = f"UNREADABLE:{type(exc).__name__}"
    return out


def running_source(conts):
    result = {}
    for c in conts:
        if c.get("status") != "running" or not c.get("pid"):
            continue
        if not any(m.get("destination") == "/var/lib/tradexa" for m in c["mounts"]):
            continue
        root = None
        for candidate in ("app", "srv/app", "opt/app"):
            p = f"/proc/{c['pid']}/root/{candidate}"
            if os.path.exists(os.path.join(p, "automation-hub", "app.py")):
                root = p
                break
        result[c["name"]] = ({"root": root.split("/root/", 1)[1], "blobs": fingerprint_root(root)}
                             if root else {"error": "application root not found under /proc/<pid>/root"})
    return result


def checkout_state(run: Runner, conts):
    dirs = sorted({c["labels"].get("com.docker.compose.project.working_dir") for c in conts
                   if c["labels"].get("com.docker.compose.project.working_dir")})
    out = {}
    for d in dirs:
        # The deploy checkout sits above ops/ for the staging project.
        for candidate in (d, os.path.dirname(d)):
            if os.path.isdir(os.path.join(candidate, ".git")):
                g = ["git", "-c", f"safe.directory={candidate}", "--no-optional-locks", "-C", candidate]
                status = run.cmd(g + ["status", "--porcelain=v1", "--untracked-files=no"]).splitlines()
                upstream = run.cmd(g + ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]).strip()
                out[candidate] = {
                    "head": run.cmd(g + ["rev-parse", "HEAD"]).strip(),
                    "branch": run.cmd(g + ["rev-parse", "--abbrev-ref", "HEAD"]).strip(),
                    "head_commit": clip(run.cmd(g + ["log", "-1", "--format=%H%x09%cI%x09%s"]).strip(), 300),
                    "modified_tracked_files": len(status), "modified_paths": [clip(s, 160) for s in status[:30]],
                    "upstream": upstream,
                    "upstream_head_local_ref": run.cmd(g + ["rev-parse", upstream]).strip() if upstream else "",
                }
                break
        else:
            out[d] = {"git": "no .git directory found (archive or copied release)"}
    return out


# ---------------------------------------------------------------- disk -----
def df(run: Runner):
    text = run.cmd(["df", "-B1", "--output=source,fstype,size,used,avail,pcent,target"])
    inodes = run.cmd(["df", "--output=source,itotal,iused,iavail,ipcent,target"])
    keep = lambda t: [l for l in t.splitlines() if not re.match(r"^(tmpfs|devtmpfs|overlay|shm|udev|none)\s", l)]
    return {"bytes": keep(text), "inodes": keep(inodes)}


def walk(root, *, max_entries=200_000, deadline_s=60.0, recent_days=21, top=25, exclude=()):
    """Bounded metadata-only walk: sizes, mtimes, largest files. Never reads file content."""
    started = time.monotonic()
    try:
        root_dev = os.stat(root).st_dev
    except OSError as exc:
        return {"root": root, "error": f"{type(exc).__name__}"}
    total_app = total_alloc = files = dirs = 0
    seen_inodes = set()
    by_child = defaultdict(lambda: [0, 0, 0])
    by_day = defaultdict(int)
    largest = []
    truncated = None
    cutoff = time.time() - recent_days * 86400
    stack = [root]
    entries = 0
    while stack:
        if entries >= max_entries:
            truncated = "max_entries"
            break
        if time.monotonic() - started > deadline_s:
            truncated = "deadline"
            break
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                entries += 1
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                full = e.path
                if any(full.startswith(x) for x in exclude):
                    continue
                if e.is_dir(follow_symlinks=False):
                    if st.st_dev == root_dev:
                        stack.append(full)
                        dirs += 1
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)
                alloc = st.st_blocks * 512
                files += 1
                total_app += st.st_size
                total_alloc += alloc
                rel = os.path.relpath(full, root)
                child = rel.split(os.sep, 1)[0]
                agg = by_child[child]
                agg[0] += st.st_size; agg[1] += alloc; agg[2] += 1
                if st.st_mtime >= cutoff:
                    by_day[datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d")] += alloc
                largest.append((alloc, rel, st.st_size, st.st_mtime))
                if len(largest) > top * 4:
                    largest.sort(reverse=True)
                    del largest[top:]
    largest.sort(reverse=True)
    return {
        "root": root, "files": files, "dirs": dirs, "apparent_bytes": total_app,
        "allocated_bytes": total_alloc, "truncated": truncated,
        "seconds": round(time.monotonic() - started, 2),
        "by_top_level": sorted(({"name": k, "apparent": v[0], "allocated": v[1], "files": v[2]}
                                for k, v in by_child.items()), key=lambda r: -r["allocated"])[:40],
        "allocated_by_mtime_day": dict(sorted(by_day.items())),
        "largest_files": [{"path": p, "allocated": a, "apparent": s,
                           "mtime": datetime.fromtimestamp(m, timezone.utc).isoformat(timespec="seconds"),
                           "kind": classify(p)} for a, p, s, m in largest[:top]],
    }


def classify(path):
    p = path.lower()
    for suffix, kind in ((".db-wal", "sqlite-wal"), (".db-shm", "sqlite-shm"), (".db", "sqlite"),
                         (".sqlite", "sqlite"), (".tlxb", "sealed-backup"), (".tgz", "archive"),
                         (".tar.gz", "archive"), (".gz", "archive"), (".tar", "archive"),
                         (".zip", "archive"), (".log", "log"), ("container.log", "log"),
                         (".jsonl", "jsonl"), (".json", "json")):
        if p.endswith(suffix):
            return kind
    return "other"


def disk(run: Runner, conts, volumes_info, opts):
    report = {"df": df(run)}
    try:
        report["docker_info"] = {k: v for k, v in json.loads(run.docker(["info", "--format", "{{json .}}"])).items()
                                 if k in ("DockerRootDir", "Driver", "ServerVersion", "Images", "Containers",
                                          "ContainersRunning", "ContainersStopped", "CgroupVersion",
                                          "CgroupDriver", "MemTotal", "NCPU", "OperatingSystem", "KernelVersion")}
    except Exception as exc:  # noqa: BLE001
        report["docker_info"] = {"error": clip(exc)}
    try:
        report["docker_system_df"] = [json.loads(l) for l in
                                      run.docker(["system", "df", "--format", "{{json .}}"], timeout=180).splitlines()
                                      if l.strip()]
    except Exception as exc:  # noqa: BLE001
        report["docker_system_df"] = {"error": clip(exc)}
    walks = {}
    budget_end = time.monotonic() + opts.walk_budget

    def bounded(label, root, **kw):
        if time.monotonic() > budget_end:
            walks[label] = {"root": root, "skipped": "global walk budget reached"}
        else:
            walks[label] = walk(root, max_entries=opts.max_entries, deadline_s=opts.walk_deadline, **kw)

    for v in volumes_info:
        if v.get("mountpoint"):
            bounded(f"volume:{v['name']}", v["mountpoint"])
    roots = set(opts.host_root)
    for c in conts:
        wd = c["labels"].get("com.docker.compose.project.working_dir")
        if wd:
            parts = wd.rstrip("/").split("/")
            # /home/ubuntu/<project>/... -> the whole project tree (release, private, job dirs).
            roots.add("/".join(parts[:4]) if wd.startswith("/home/") and len(parts) > 4 else wd)
    docker_root = (report.get("docker_info") or {}).get("DockerRootDir") or "/var/lib/docker"
    roots.add(os.path.join(docker_root, "containers"))
    for r in sorted(roots):
        if os.path.isdir(r):
            bounded(f"host:{r}", r, exclude=(os.path.join(docker_root, "overlay2"),
                                             os.path.join(docker_root, "volumes")))
    report["walks"] = walks
    report["journald_usage"] = clip(run.cmd(["journalctl", "--disk-usage"]), 200)
    return report


def volumes(run: Runner):
    names = run.docker(["volume", "ls", "-q"]).split()
    if not names:
        return []
    out = []
    for v in json.loads(run.docker(["volume", "inspect", *names])):
        out.append({"name": v.get("Name"), "mountpoint": v.get("Mountpoint"), "created": v.get("CreatedAt"),
                    "driver": v.get("Driver"), "labels": _labels(v.get("Labels"))})
    return out


# ------------------------------------------------------------ resources ----
def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def _kv(text):
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                pass
    return out


def cgroup_dir(pid):
    text = _read(f"/proc/{pid}/cgroup") or ""
    for line in text.splitlines():
        if line.startswith("0::"):
            return "/sys/fs/cgroup" + line[3:].strip()
    return None


def snapshot(cgroups, main_pid):
    host_cpu = [int(x) for x in (_read("/proc/stat") or "cpu 0").splitlines()[0].split()[1:]]
    disks = {}
    for line in (_read("/proc/diskstats") or "").splitlines():
        f = line.split()
        if len(f) >= 14 and not f[2].startswith(("loop", "ram", "dm-", "sr")):
            disks[f[2]] = {"rd_sectors": int(f[5]), "wr_sectors": int(f[9]), "io_ms": int(f[12])}
    psi = {}
    for kind in ("cpu", "memory", "io"):
        text = _read(f"/proc/pressure/{kind}")
        if text:
            m = re.search(r"some avg10=([\d.]+) avg60=([\d.]+).*total=(\d+)", text)
            if m:
                psi[kind] = {"some_avg10": float(m.group(1)), "some_avg60": float(m.group(2)),
                             "some_total_us": int(m.group(3))}
    conts = {}
    for name, path in cgroups.items():
        if not path:
            continue
        cpu = _kv(_read(os.path.join(path, "cpu.stat")))
        mem = _kv(_read(os.path.join(path, "memory.stat")))
        io_r = io_w = 0
        for line in (_read(os.path.join(path, "io.stat")) or "").splitlines():
            m = dict(x.split("=", 1) for x in line.split()[1:] if "=" in x)
            io_r += int(m.get("rbytes", 0)); io_w += int(m.get("wbytes", 0))
        conts[name] = {"cpu_usec": cpu.get("usage_usec"), "mem_current": _int(_read(os.path.join(path, "memory.current"))),
                       "mem_peak": _int(_read(os.path.join(path, "memory.peak"))), "anon": mem.get("anon"),
                       "file": mem.get("file"), "file_dirty": mem.get("file_dirty"),
                       "io_rbytes": io_r, "io_wbytes": io_w, "pids": _int(_read(os.path.join(path, "pids.current"))),
                       "oom_kill": _kv(_read(os.path.join(path, "memory.events"))).get("oom_kill")}
    threads = {}
    if main_pid:
        try:
            for tid in os.listdir(f"/proc/{main_pid}/task"):
                stat = (_read(f"/proc/{main_pid}/task/{tid}/stat") or "").rsplit(")", 1)
                if len(stat) == 2:
                    f = stat[1].split()
                    threads[tid] = int(f[11]) + int(f[12])
        except OSError:
            pass
    return {"t": time.time(), "host_cpu": host_cpu, "meminfo": _kv(_read("/proc/meminfo")),
            "loadavg": (_read("/proc/loadavg") or "").split()[:3], "disks": disks, "psi": psi,
            "containers": conts, "threads": threads}


def _int(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def summarize_samples(samples, nproc):
    out = {"samples": len(samples), "window_s": round(samples[-1]["t"] - samples[0]["t"], 1) if samples else 0}
    if len(samples) < 2:
        return out
    host = []
    for a, b in zip(samples, samples[1:]):
        d = [y - x for x, y in zip(a["host_cpu"][:8], b["host_cpu"][:8])]
        tot = sum(d) or 1
        host.append({"busy": 100 * (tot - d[3] - d[4]) / tot, "iowait": 100 * d[4] / tot, "steal": 100 * d[7] / tot})
    out["host_cpu_percent"] = {k: _stats([h[k] for h in host]) for k in ("busy", "iowait", "steal")}
    out["host_mem_available_mib"] = _stats([s["meminfo"].get("MemAvailable", 0) / 1024 for s in samples])
    out["host_swap_total_mib"] = samples[-1]["meminfo"].get("SwapTotal", 0) / 1024
    out["loadavg_last"] = samples[-1]["loadavg"]
    out["psi_last"] = samples[-1]["psi"]
    dt = samples[-1]["t"] - samples[0]["t"]
    disks = {}
    for dev, a in samples[0]["disks"].items():
        b = samples[-1]["disks"].get(dev)
        if b and (b["wr_sectors"] - a["wr_sectors"] or b["rd_sectors"] - a["rd_sectors"]):
            disks[dev] = {"read_mib_s": round((b["rd_sectors"] - a["rd_sectors"]) * 512 / 2**20 / dt, 3),
                          "write_mib_s": round((b["wr_sectors"] - a["wr_sectors"]) * 512 / 2**20 / dt, 3),
                          "util_percent": round(100 * (b["io_ms"] - a["io_ms"]) / 1000 / dt, 2)}
    out["disk_io"] = disks
    conts = {}
    for name in samples[0]["containers"]:
        series = [s["containers"].get(name) for s in samples]
        if any(x is None or x.get("cpu_usec") is None for x in series):
            continue
        cpu = [100 * (b["cpu_usec"] - a["cpu_usec"]) / 1e6 / (sb["t"] - sa["t"])
               for (a, b, sa, sb) in zip(series, series[1:], samples, samples[1:])]
        first, last = series[0], series[-1]
        conts[name] = {"cpu_percent_of_one_core": _stats(cpu),
                       "mem_current_mib": _stats([x["mem_current"] / 2**20 for x in series if x["mem_current"]]),
                       "mem_peak_mib": round((last.get("mem_peak") or 0) / 2**20, 1),
                       "anon_mib_last": round((last.get("anon") or 0) / 2**20, 1),
                       "read_mib_s": round((last["io_rbytes"] - first["io_rbytes"]) / 2**20 / dt, 4),
                       "write_mib_s": round((last["io_wbytes"] - first["io_wbytes"]) / 2**20 / dt, 4),
                       "pids_last": last.get("pids"), "oom_kill_total": last.get("oom_kill")}
    out["containers"] = conts
    out["nproc"] = nproc
    a, b = samples[0]["threads"], samples[-1]["threads"]
    deltas = sorted(((b[t] - a[t], t) for t in b if t in a), reverse=True)
    total = sum(d for d, _ in deltas) or 1
    out["main_app_threads"] = {"count": len(b), "top_thread_share_percent":
                               [{"tid": t, "share": round(100 * d / total, 1)} for d, t in deltas[:8]]}
    return out


def _stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    s = sorted(values)
    return {"avg": round(sum(s) / len(s), 2), "p50": round(s[len(s) // 2], 2),
            "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 2), "max": round(s[-1], 2)}


def resources(conts, prod_name, opts):
    cgroups = {c["name"]: cgroup_dir(c["pid"]) for c in conts if c.get("status") == "running" and c.get("pid")}
    main_pid = next((c["pid"] for c in conts if c["name"] == prod_name), None)
    samples = []
    end = time.monotonic() + opts.sample_seconds
    while True:
        samples.append(snapshot(cgroups, main_pid))
        if time.monotonic() >= end:
            break
        time.sleep(min(opts.sample_interval, max(0.0, end - time.monotonic())))
    return summarize_samples(samples, os.cpu_count())


# ------------------------------------------------------ app performance ----
LOG_RE = re.compile(r'"(?P<method>[A-Z]{3,7}) (?P<path>\S+) HTTP/[\d.]+" (?P<status>\d{3}) (?P<bytes>\d+|-)')
ID_SEG = re.compile(r"^(?:[0-9a-f]{8,}|[0-9a-f-]{32,36}|\d+|[A-Za-z0-9_-]{24,})$", re.I)


def normalize_path(path):
    path = path.split("?", 1)[0]
    segs = [(":id" if ID_SEG.match(s) else s) for s in path.split("/")]
    return "/".join(segs)[:120]


def parse_access_log(text, max_lines=500_000):
    per_path = defaultdict(lambda: {"count": 0, "bytes": 0, "status": Counter()})
    status = Counter()
    lines = 0
    for line in text.splitlines()[-max_lines:]:
        m = LOG_RE.search(line)
        if not m:
            continue
        lines += 1
        p = normalize_path(m.group("path"))
        row = per_path[p]
        row["count"] += 1
        row["bytes"] += int(m.group("bytes")) if m.group("bytes") != "-" else 0
        row["status"][m.group("status")] += 1
        status[m.group("status")[0] + "xx"] += 1
    top = sorted(per_path.items(), key=lambda kv: -kv[1]["count"])[:40]
    errors = {p: {s: n for s, n in r["status"].items() if s in ("499", "500", "502", "503", "504")}
              for p, r in per_path.items() if any(s in r["status"] for s in ("499", "500", "502", "503", "504"))}
    return {"requests": lines, "status_classes": dict(status),
            "top_paths": [{"path": p, "count": r["count"], "bytes": r["bytes"], "status": dict(r["status"])}
                          for p, r in top],
            "error_paths": dict(sorted(errors.items(), key=lambda kv: -sum(kv[1].values()))[:30])}


def app_performance(run: Runner, conts, opts):
    out = {}
    for c in conts:
        if c.get("status") != "running":
            continue
        image = (c.get("image_ref") or "").lower()
        if "nginx" in image or c["labels"].get("com.docker.compose.service") in ("nginx", "ingress"):
            try:
                text = run.docker(["logs", "--since", f"{opts.log_minutes}m", c["id"]], timeout=120)
                parsed = parse_access_log(text)
                parsed["window_minutes"] = opts.log_minutes
                parsed["requests_per_minute"] = round(parsed["requests"] / max(1, opts.log_minutes), 1)
                out[f"access_log:{c['name']}"] = parsed
            except Exception as exc:  # noqa: BLE001
                out[f"access_log:{c['name']}"] = {"error": clip(exc)}
        if any(m.get("destination") == "/var/lib/tradexa" for m in c["mounts"]):
            out[f"health:{c['name']}"] = health_samples(c)
            out[f"startup_log:{c['name']}"] = startup_lines(run, c)
            out[f"log_signals_24h:{c['name']}"] = log_signals(run, c)
    return out


def health_samples(c, n=5):
    port = c["env"]["values"].get("PORT") or "8000"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = []
    for ip in list(c["ip_addresses"].values())[:1]:
        for _ in range(n):
            t = time.perf_counter()
            try:
                with opener.open(f"http://{ip}:{port}/health", timeout=5) as r:
                    body = json.loads(r.read(200_000) or b"{}")
                    code = r.status
            except Exception as exc:  # noqa: BLE001
                body, code = {}, f"ERR {type(exc).__name__}"
            ms = round((time.perf_counter() - t) * 1000, 1)
            results.append({"status": code, "ms": ms})
            time.sleep(1)
        keep = {k: body.get(k) for k in ("status", "commit", "commit_short", "branch", "deployed_at",
                                         "features", "acceptance_read_only", "tenancy") if k in body}
        return {"samples_ms": [r["ms"] for r in results], "codes": [r["status"] for r in results],
                "deploy_info": keep, "public_status": public_status(opener, ip, port)}
    return {"error": "no container IP"}


def public_status(opener, ip, port):
    """The app's own unauthenticated, 30 s cached status feed (component states,
    daily uptime, incidents). Designed to be polled; carries no secrets or account data."""
    try:
        with opener.open(f"http://{ip}:{port}/status/public", timeout=10) as r:
            view = json.loads(r.read(2_000_000) or b"{}")
    except Exception as exc:  # noqa: BLE001 -- absent on builds without the status monitor
        return {"error": clip(exc, 120)}
    return {"overall": view.get("overall"), "last_sample_at": view.get("last_sample_at"),
            "monitoring_since": view.get("monitoring_since"),
            "components": [{"id": c.get("id"), "state": c.get("state"), "detail": clip(c.get("detail"), 160),
                            "since": c.get("since"), "uptime_pct": c.get("uptime_pct"),
                            "last_14_days": [{"date": d.get("date"), "state": d.get("state"), "uptime": d.get("uptime")}
                                             for d in (c.get("days") or [])[-14:]]}
                           for c in view.get("components") or []],
            "incident_count": len(view.get("incidents") or []),
            "incidents": [{k: (clip(i.get(k), 160) if k == "detail" else i.get(k))
                           for k in ("component", "state", "started_at", "ended_at", "duration_min", "ongoing", "detail")}
                          for i in (view.get("incidents") or [])[:15]]}


def startup_lines(run: Runner, c):
    try:
        start = datetime.fromisoformat((c.get("started_at") or "")[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"error": "no start time"}
    until = (start + timedelta(minutes=10)).isoformat()
    text = run.docker(["logs", "--since", start.isoformat(), "--until", until, c["id"]], timeout=120)
    lines = [clip(l, 300) for l in text.splitlines() if l.startswith("[startup]")]
    return {"lines": lines[:80], "count": len(lines)}


def log_signals(run: Runner, c):
    text = run.docker(["logs", "--since", "24h", c["id"]], timeout=180)
    patterns = {"persistence_blocked": "PERSISTENCE_BLOCKED", "database_locked": "database is locked",
                "traceback": "Traceback", "memory_error": "MemoryError", "disk_full": "database or disk is full",
                "no_space": "No space left", "timeout": "timed out", "supabase": "Supabase"}
    counts = {k: 0 for k in patterns}
    lines = 0
    for line in text.splitlines():
        lines += 1
        for k, p in patterns.items():
            if p in line:
                counts[k] += 1
    return {"log_lines_24h": lines, "counts": counts}


# ----------------------------------------------------------------- SQLite --
def sqlite_header(path):
    try:
        with open(path, "rb") as fh:
            h = fh.read(100)
        size = os.path.getsize(path)
    except OSError as exc:
        return {"error": type(exc).__name__}
    if len(h) < 100 or not h.startswith(b"SQLite format 3\x00"):
        return {"valid": False, "size": size}
    page_size = struct.unpack(">H", h[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    change, pages, _trunk, freelist = struct.unpack(">IIII", h[24:40])
    valid_for = struct.unpack(">I", h[92:96])[0]
    header_pages_valid = pages > 0 and valid_for == change
    expected = pages * page_size if header_pages_valid else None
    return {"valid": True, "size": size, "page_size": page_size, "write_version": h[18],
            "wal_format": h[18] == 2, "header_pages": pages, "freelist_pages": freelist,
            "freelist_bytes": freelist * page_size, "header_size_matches_file":
            (expected == size) if expected is not None else None, "expected_size": expected}


# Runs in a child process that has already dropped to the database file's owner.
DB_CHILD = r'''
import json, os, sqlite3, sys, time, urllib.parse
p = json.load(sys.stdin)
out = {"path": p["path"], "uid": os.getuid()}
deadline = [0.0]
def run(sql, args=(), budget=None):
    deadline[0] = time.monotonic() + (budget or p["query_budget_s"])
    return con.execute(sql, args).fetchall()
try:
    con = sqlite3.connect("file:" + urllib.parse.quote(p["path"]) + "?mode=ro&immutable=1", uri=True, timeout=1.0)
    con.set_progress_handler(lambda: int(time.monotonic() > deadline[0]), 2000)
    run("PRAGMA query_only=1")
    out["pragma"] = {k: run(f"PRAGMA {k}")[0][0] for k in ("page_size", "page_count", "freelist_count", "journal_mode", "auto_vacuum")}
    objs = run("SELECT type, name, tbl_name, COALESCE(sql,'') FROM sqlite_master")
    out["objects"] = {"tables": sum(1 for o in objs if o[0] == "table"), "indexes": sum(1 for o in objs if o[0] == "index"),
                      "triggers": [o[1] for o in objs if o[0] == "trigger"][:40]}
    tables = [(o[1], "WITHOUT ROWID" in o[3].upper()) for o in objs if o[0] == "table" and not o[1].startswith("sqlite_")]
    U = out["pragma"]["page_size"]
    X, M = U - 35, ((U - 12) * 32 // 255) - 23
    def cell(payload, local_max):
        # SQLite b-tree cell layout: (bytes kept on the leaf page incl. its 2-byte
        # pointer, number of whole overflow pages).
        if payload <= local_max:
            return payload + 8, 0
        k = M + ((payload - M) % (U - 4))
        if k > local_max:
            k = M
        return k + 12, -(-(payload - k) // (U - 4))
    def leaf_bytes_per_row(cells):
        # Rows are appended in rowid order, so leaf pages fill greedily: a cell
        # that does not fit starts a new page. ~3 KB rows therefore cost a whole
        # 4 KB page each, which a payload-only estimate misses.
        if not cells:
            return 0
        pages, used = 1, 0
        for c in cells:
            if used + c > U - 8:
                pages, used = pages + 1, 0
            used += c
        return pages * U / len(cells)
    attr = {}
    sample_cols = ("reason_code", "kind", "state", "status", "event_type", "decision", "outcome", "record_source")
    time_cols = ("created_at", "updated_at", "candle_time", "at", "timestamp", "received_at", "started_at")
    big = set(p.get("big_tables", []))
    for name, no_rowid in tables:
        q = '"' + name.replace('"', '""') + '"'
        cols = [r[1] for r in run(f"PRAGMA table_info({q})")]
        if no_rowid or not cols:
            attr[name] = {"skipped": "WITHOUT ROWID" if no_rowid else "no columns"}; continue
        try:
            mn, mx = run(f"SELECT MIN(rowid), MAX(rowid) FROM {q}")[0]
        except sqlite3.OperationalError as e:
            attr[name] = {"error": str(e)[:120]}; continue
        if mn is None:
            attr[name] = {"rows_est": 0, "est_bytes": 0, "est_index_bytes": 0}; continue
        idx = []
        for il in run(f"PRAGMA index_list({q})"):
            icols = [r[2] for r in run('PRAGMA index_info("%s")' % il[1].replace('"', '""')) if r[2]]
            if icols:
                idx.append((il[1], icols))
        k = p["big_sample_k"] if (name in big or p["file_size"] > 256 * 2**20) else p["sample_k"]
        span = mx - mn + 1
        ids = sorted({mn + (span - 1) * i // max(1, k - 1) for i in range(k)})
        L = lambda c: "COALESCE(length(CAST(" + '"' + c.replace('"', '""') + '"' + " AS BLOB)),0)"
        cat = next((c for c in sample_cols if c in cols), None)
        tcol = next((c for c in time_cols if c in cols), None)
        sel = ("SELECT " + " + ".join(L(c) for c in cols) + (f', "{cat}"' if cat else ", NULL") +
               (f', "{tcol}"' if tcol else ", NULL") + "".join(", " + " + ".join(L(c) for c in ic) for _, ic in idx))
        hits, payload_sum, overflow_pages, cats, times, cells = 0, 0, 0, {}, [], []
        idx_sum = [0] * len(idx)
        try:
            for rid in ids:
                rows = run(sel + f" FROM {q} WHERE rowid=?", (rid,))
                if not rows:
                    continue
                r = rows[0]
                hits += 1
                rec = r[0] + len(cols) + 2          # record body + header
                payload_sum += r[0]
                local, ovf = cell(rec + 4, X)       # + rowid/length varints
                cells.append(local)
                overflow_pages += ovf
                for j, (_, ic) in enumerate(idx):
                    ilocal, iovf = cell(r[3 + j] + len(ic) + 2 + 4, ((U - 12) * 64 // 255) - 23)
                    idx_sum[j] += ilocal + iovf * U
                if cat:
                    cats[str(r[1])[:60]] = cats.get(str(r[1])[:60], 0) + 1
                if tcol:
                    times.append([rid, str(r[2])[:32]])
        except sqlite3.OperationalError as e:
            attr[name] = {"error": "sample interrupted: " + str(e)[:80]}; continue
        rows_est = (int(span * hits / len(ids)) if len(ids) < span else hits)
        per = lambda total: total / hits if hits else 0
        attr[name] = {"min_rowid": mn, "max_rowid": mx, "sampled": len(ids), "hits": hits, "rows_est": rows_est,
                      "avg_row_payload_bytes": round(per(payload_sum), 1),
                      "est_bytes": int(rows_est * (leaf_bytes_per_row(cells) + per(overflow_pages) * U)),
                      "overflow_pages_per_row": round(per(overflow_pages), 2),
                      "est_index_bytes": {n: int(rows_est * per(idx_sum[j]) / p["index_fill"]) for j, (n, _) in enumerate(idx)},
                      "sample_category_column": cat, "sample_categories": cats,
                      "time_column": tcol, "rowid_time_samples": times[:: max(1, len(times) // 24)]}
    out["tables"] = attr
    if p.get("exact") and p["file_size"] <= p["exact_max_bytes"]:
        try:
            out["dbstat_exact"] = {r[0]: r[1] for r in run("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name", budget=p["exact_budget_s"])}
        except sqlite3.OperationalError as e:
            out["dbstat_exact"] = {"error": str(e)[:120]}
    names = {t for t, _ in tables}
    def cols_of(t):
        return [r[1] for r in run('PRAGMA table_info("%s")' % t)]
    def pick(t, wanted, order, limit):
        have = cols_of(t); use = [c for c in wanted if c in have]
        if not use: return []
        order_sql = f" ORDER BY {order} DESC" if order in have else ""
        rows = run(f'SELECT {", ".join(chr(34)+c+chr(34) for c in use)} FROM "{t}"{order_sql} LIMIT {limit}')
        return [{c: (str(v)[:160] if isinstance(v, str) else v) for c, v in zip(use, r)} for r in rows]
    app = {}
    inst_cols = ["id", "symbol", "strategy_key", "strategy_label", "strategy_version", "timeframe", "exchange",
                 "instrument_type", "execution_mode", "mode", "market_data_mode", "state", "desired_running",
                 "created_at", "started_at", "stopped_at", "updated_at", "last_error"]
    sess_cols = ["id", "started_at", "ended_at", "status", "end_reason", "mode", "symbol", "timeframe",
                 "operating_mode", "model_id", "updated_at"]
    try:
        if "trading_instances" in names: app["trading_instances"] = pick("trading_instances", inst_cols, "created_at", 50)
        for t in ("pa_sessions", "smc_sessions"):
            if t in names: app[t] = pick(t, sess_cols, "started_at", 5)
        latest = {}
        for t, c in (("pa_evaluations", "updated_at"), ("pa_activity", "created_at"), ("pa_journal_revisions", "created_at"),
                     ("smc_evaluations", "updated_at"), ("smc_activity", "created_at"), ("smc_journal_revisions", "created_at"),
                     ("agent_decisions", "at"), ("guardian_events", "timestamp"), ("trade_records", "updated_at"),
                     ("v2_fills", "timestamp"), ("paper_trades", "opened_at"), ("decisions", "created_at")):
            if t in names and c in cols_of(t):
                r = run(f'SELECT "{c}" FROM "{t}" WHERE rowid=(SELECT MAX(rowid) FROM "{t}")')
                latest[t] = r[0][0] if r else None
        app["latest_row_time"] = latest
        if "guardian_meta" in names:
            meta = {}
            for key, value in run("SELECT key, value FROM guardian_meta WHERE key IN ('heartbeat','research.last_run')"):
                v = json.loads(value)
                meta[key] = {k: v.get(k) for k in ("at", "cycles", "interval_s", "last_cycle_ms", "trades", "created", "advanced")}
            app["guardian_meta"] = meta
            if "guardian_events" in names:
                first = run("SELECT seq, timestamp FROM guardian_events ORDER BY seq LIMIT 1")
                last = run("SELECT seq, timestamp FROM guardian_events ORDER BY seq DESC LIMIT 1")
                app["guardian_events_span"] = {"first": list(first[0]) if first else None, "last": list(last[0]) if last else None}
        if "recorder_state" in names:
            keys = [r[0] for r in run("SELECT key FROM recorder_state")]
            r = run("SELECT value_json FROM recorder_state WHERE key='last_reconcile'")
            last = json.loads(r[0][0]) if r else {}
            app["journal_recorder"] = {"keys": keys[:40], "last_reconcile_at": last.get("at"),
                                       "last_reconcile_seconds": last.get("seconds"),
                                       "last_reconcile_errors": len(last.get("errors") or []),
                                       "first_errors": [str(e)[:200] for e in (last.get("errors") or [])[:3]]}
        if "trade_records" in names:
            try:
                app["trade_records_by_source"] = [list(r) for r in run(
                    "SELECT record_source, status, COUNT(*) FROM trade_records GROUP BY 1, 2", budget=3.0)]
            except sqlite3.OperationalError as e:
                app["trade_records_by_source"] = "skipped: " + str(e)[:80]
        if "current" in names and "streak" in cols_of("current"):
            app["status_current"] = [list(r[:3]) + [str(r[3])[:160]] for r in run("SELECT component, state, since, detail FROM current")]
    except Exception as e:
        app["error"] = type(e).__name__ + ": " + str(e)[:160]
    out["app"] = app
except Exception as e:
    out["error"] = type(e).__name__ + ": " + str(e)[:200]
print(json.dumps(out, default=str))
'''


def inspect_db(path, opts, *, big_tables=(), python=None):
    header = sqlite_header(path)
    info = {"header": header, "sidecars": {}}
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            st = os.stat(path + suffix)
            info["sidecars"][suffix] = {"size": st.st_size, "uid": st.st_uid}
        except OSError:
            pass
    if not header.get("valid"):
        return info
    st = os.stat(path)
    info["owner_uid"] = st.st_uid
    info["view"] = ("checkpointed state; up to "
                    f"{info['sidecars'].get('-wal', {}).get('size', 0)} WAL bytes not included")
    params = {"path": path, "file_size": st.st_size, "query_budget_s": opts.query_budget,
              "sample_k": opts.sample_k, "big_sample_k": opts.big_sample_k, "big_tables": list(big_tables),
              "exact": opts.exact_max_bytes > 0, "exact_max_bytes": opts.exact_max_bytes, "exact_budget_s": 20.0,
              "index_fill": opts.index_fill}
    try:
        proc = subprocess.run([python or sys.executable, "-I", "-c", DB_CHILD], input=json.dumps(params),
                              capture_output=True, text=True, timeout=opts.db_budget, cwd="/")
        info["opened"] = True
        info["inspection"] = json.loads(proc.stdout) if proc.stdout.strip() else {"error": clip(proc.stderr, 300)}
    except subprocess.TimeoutExpired:
        info["inspection"] = {"error": f"database budget {opts.db_budget}s exceeded; child stopped"}
    return info


def databases(conts, opts):
    out = {}
    for c in conts:
        mount = next((m for m in c["mounts"] if m.get("destination") == "/var/lib/tradexa"), None)
        if not mount or not mount.get("source") or c.get("status") != "running":
            continue
        root = mount["source"]
        files = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "backups"]
            if dirpath.count(os.sep) - root.count(os.sep) > 2:
                dirnames[:] = []
            for f in filenames:
                if f.endswith((".db", ".sqlite", ".sqlite3")):
                    full = os.path.join(dirpath, f)
                    files[os.path.relpath(full, root)] = inspect_db(
                        full, opts, big_tables=("pa_journal_revisions", "pa_evaluations", "pa_activity",
                                                "guardian_events", "smc_journal_revisions"))
        out[c["name"]] = {"data_dir": root, "files": files, "open_fds": open_fds(c, root)}
    return out


def open_fds(c, root):
    counts = Counter()
    pid = c.get("pid")
    if not pid:
        return {}
    for task_pid in [pid] + _children(pid):
        try:
            for fd in os.listdir(f"/proc/{task_pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{task_pid}/fd/{fd}")
                except OSError:
                    continue
                if target.startswith("/var/lib/tradexa/"):
                    counts[target[len("/var/lib/tradexa/"):]] += 1
        except OSError:
            continue
    return dict(sorted(counts.items()))


def _children(pid):
    kids = []
    text = _read(f"/proc/{pid}/task/{pid}/children") or ""
    for k in text.split():
        kids.append(int(k))
    return kids


# ---------------------------------------------------------------- backups --
def backups(conts, extra_dirs):
    out = {}
    roots = []
    for c in conts:
        mount = next((m for m in c["mounts"] if m.get("destination") == "/var/lib/tradexa"), None)
        if mount and mount.get("source"):
            roots.append((c["name"], os.path.join(mount["source"], "backups")))
    roots += [(f"host:{d}", d) for d in extra_dirs]
    for label, root in roots:
        if not os.path.isdir(root):
            out[label] = {"path": root, "exists": False}
            continue
        entries = []
        for e in sorted(os.scandir(root), key=lambda x: x.name):
            st = e.stat(follow_symlinks=False)
            row = {"name": e.name, "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")}
            if e.is_dir(follow_symlinks=False):
                w = walk(e.path, max_entries=5000, deadline_s=10)
                row.update(kind="snapshot-dir", allocated=w.get("allocated_bytes"), files=w.get("files"))
                manifest = _small_json(os.path.join(e.path, "manifest.json"))
                if isinstance(manifest, dict):
                    row["manifest"] = {"created": manifest.get("created"), "files": len(manifest.get("files") or []),
                                       "errors": [clip(x, 160) for x in (manifest.get("errors") or [])[:5]],
                                       "encrypted": manifest.get("encrypted")}
                else:
                    row["manifest"] = manifest
                checks = {}
                for f in os.listdir(e.path):
                    if f.endswith(".db"):
                        h = sqlite_header(os.path.join(e.path, f))
                        checks[f] = {"size": h.get("size"), "valid": h.get("valid"),
                                     "header_size_matches_file": h.get("header_size_matches_file")}
                row["db_header_checks"] = checks
            else:
                row.update(kind=classify(e.name), allocated=st.st_blocks * 512, apparent=st.st_size)
                if e.name.endswith(".tlxb.json"):
                    m = _small_json(e.path)
                    if isinstance(m, dict):
                        row["sidecar"] = {"files": len(m.get("files") or []), "encrypted": m.get("encrypted"),
                                          "errors": len(m.get("errors") or []), "created": m.get("created")}
            entries.append(row)
        out[label] = {"path": root, "exists": True, "entries": entries,
                      "total_allocated": sum((r.get("allocated") or 0) for r in entries),
                      "snapshot_count": sum(1 for r in entries if r.get("kind") in ("snapshot-dir", "sealed-backup"))}
    return out


def _small_json(path, limit=1_000_000):
    try:
        if os.path.getsize(path) > limit:
            return {"error": "manifest larger than limit"}
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"error": "missing"}
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}


def restore_readiness(conts):
    out = {}
    for c in conts:
        if c.get("status") != "running" or not c.get("pid"):
            continue
        p = f"/proc/{c['pid']}/root/app/automation-hub/services/backup.py"
        text = _read(p)
        if text is None:
            continue
        out[c["name"]] = {"backup_module": True, "has_restore_to": "def restore_to" in text,
                          "has_restore_check": "def restore_check" in text,
                          "supports_sealed_archives": ".tlxb" in text, "keep_default": _keep(text),
                          "master_key_configured": c["env"]["present"].get("HUB_MASTER_KEY"),
                          "previous_master_key_configured": c["env"]["present"].get("HUB_MASTER_KEY_PREVIOUS")}
    return out


def _keep(text):
    m = re.search(r"^KEEP\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def schedules(run: Runner):
    keywords = ("backup", "rsync", "rclone", "restic", "borg", "s3", "scp", "tar ", "sqlite3", "docker",
                "prune", "certbot", "deploy")
    rows = []
    sources = {"root_crontab": run.cmd(["crontab", "-l"]),
               "ubuntu_crontab": run.cmd(["crontab", "-l", "-u", "ubuntu"])}
    cron_d = sorted(os.listdir("/etc/cron.d")) if os.path.isdir("/etc/cron.d") else []
    for path in ["/etc/crontab"] + [os.path.join("/etc/cron.d", f) for f in cron_d
                                     if os.path.isfile(os.path.join("/etc/cron.d", f))]:
        sources[path] = _read(path) or ""
    for src, text in sources.items():
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" in line.split()[0]:
                continue
            fields = line.split()
            sched = " ".join(fields[:5]) if not fields[0].startswith("@") else fields[0]
            low = line.lower()
            rows.append({"source": src, "schedule": sched,
                         "keywords": [k.strip() for k in keywords if k in low]})
    timers = run.cmd(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend"])
    timer_units = sorted({t for line in timers.splitlines() for t in line.split() if t.endswith(".timer")})
    return {"cron_entries": rows, "systemd_timers": timer_units,
            "off_host_tool_detected": any(k in r["keywords"] for r in rows for k in ("rsync", "rclone", "restic", "borg", "s3", "scp"))}


# ----------------------------------------------------------------- main ----
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=".", help="directory for the output JSON (never a data volume)")
    ap.add_argument("--prod-name", default="nexus-trading-bot-app-1")
    ap.add_argument("--sample-seconds", type=float, default=300.0)
    ap.add_argument("--sample-interval", type=float, default=15.0)
    ap.add_argument("--log-minutes", type=int, default=60)
    ap.add_argument("--max-entries", type=int, default=200_000)
    ap.add_argument("--walk-deadline", type=float, default=60.0, help="per-directory walk limit")
    ap.add_argument("--walk-budget", type=float, default=480.0, help="total walk time limit")
    ap.add_argument("--query-budget", type=float, default=2.0)
    ap.add_argument("--db-budget", type=float, default=30.0)
    ap.add_argument("--sample-k", type=int, default=48)
    ap.add_argument("--big-sample-k", type=int, default=256)
    ap.add_argument("--exact-max-bytes", type=int, default=64 * 2**20,
                    help="exact dbstat page accounting only for files at or below this size (0 disables)")
    ap.add_argument("--index-fill", type=float, default=0.75,
                    help="assumed index page fill (hash-like keys split pages to ~70-80%%)")
    ap.add_argument("--host-root", action="append",
                    default=["/opt/nexus-trading-bot", "/home/ubuntu", "/root", "/var/backups", "/tmp",
                             "/var/tmp", "/srv", "/mnt", "/var/log"])
    ap.add_argument("--skip-resources", action="store_true")
    opts = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("Run as root (sudo): volume paths and container /proc entries are root-only.", file=sys.stderr)
        return 2
    try:
        os.nice(19)
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        pass
    run = Runner()
    report = {"schema": SCHEMA, "started_at": utcnow(), "host": socket.gethostname(),
              "kernel": run.cmd(["uname", "-r"]).strip(), "python": sys.version.split()[0],
              "guarantees": {"read_only": True, "docker_subcommands": sorted(" ".join(k) for k in _DOCKER_READ_ONLY),
                             "secrets_printed": False, "financial_values_printed": False,
                             "sqlite_open_mode": "mode=ro&immutable=1 (no locks, no sidecars, no writes)",
                             "full_table_scans": False}}
    conts = step(report, "containers", containers, run)
    if isinstance(conts, dict):
        conts = []
    out_dir = os.path.abspath(opts.out)
    forbidden = [m["source"] for c in conts for m in c["mounts"] if m.get("source")] + ["/var/lib/docker"]
    if any(out_dir == f or out_dir.startswith(f.rstrip("/") + "/") for f in forbidden):
        print(f"Refusing to write inside a data volume or Docker storage: {out_dir}", file=sys.stderr)
        return 2
    step(report, "images", images, run, {c.get("image_id") for c in conts})
    step(report, "running_source", running_source, conts)
    step(report, "checkouts", checkout_state, run, conts)
    vols = step(report, "volumes", volumes, run)
    step(report, "disk", disk, run, conts, vols if isinstance(vols, list) else [], opts)
    step(report, "databases", databases, conts, opts)
    step(report, "backups", backups, conts, ["/root/tradexa-backups"])
    step(report, "restore_readiness", restore_readiness, conts)
    step(report, "schedules", schedules, run)
    step(report, "app_performance", app_performance, run, conts, opts)
    if not opts.skip_resources:
        step(report, "resources", resources, conts, opts.prod_name, opts)
        step(report, "docker_stats_once", lambda: [json.loads(l) for l in run.docker(
            ["stats", "--no-stream", "--format", "{{json .}}"], timeout=60).splitlines() if l.strip()])
    report["finished_at"] = utcnow()
    os.makedirs(out_dir, exist_ok=True)
    import shutil
    if shutil.disk_usage(out_dir).free < 100 * 2**20:
        print("Refusing to write: less than 100 MiB free at the output directory.", file=sys.stderr)
        return 3
    path = os.path.join(out_dir, f"nexus-p0-evidence-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
