"""Read-only RC3 inventory/config comparison. Never prints credentials or starts services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

STAGE = "nexus-remote-stage-rc3-28acdbc"
PROD = "nexus-trading-bot"
ROOT = Path("/home/ubuntu/nexus-rc3-staging-28acdbc")
RELEASE = ROOT / "release"
FILES = [RELEASE / "ops/staging.alpine.compose.yaml",
         RELEASE / "customer-workspace/compose.customer-workspace.yaml",
         RELEASE / "intelligence-recovery-20261018/compose.memory.yaml"]
ENVFILE = ROOT / "private/compose.env"
FLAGS = ("HUB_AUTH_MODE", "HUB_EMERGENCY_ADMIN_ENABLED", "HUB_AUTO_ENGINE",
         "HUB_ENABLE_EXTERNAL_LIVE", "HUB_USE_LIVE_DATA", "HUB_RESEARCH_AUTOSTART",
         "HUB_MONITOR_AGENT", "HUB_DAILY_REPORT_HOUR", "NEXUS_SHADOW_SUPERVISED")


def command(args, *, env=None):
    result = subprocess.run(["docker", *args], capture_output=True, text=True,
                            timeout=45, env=env)
    if result.returncode:
        # Captured Docker/Compose errors may contain interpolated secrets.
        raise RuntimeError("DOCKER_READ_FAILED:" + args[0] + ":exit=" + str(result.returncode))
    return result.stdout


def environment(items):
    return dict(item.split("=", 1) for item in items or [] if "=" in item)


def mount_summary(mounts):
    return sorted([{"type": m["Type"], "source": m.get("Name") or m["Source"],
                    "target": m["Destination"], "read_only": not m["RW"]}
                   for m in mounts], key=lambda m: (m["target"], m["source"]))


def main():
    report = {"scope": "READ_ONLY_EXISTING_RC3", "deployment_performed": False,
              "credential_values_printed": False, "containers": [], "blockers": []}
    all_containers = []
    for project in (STAGE, PROD):
        ids = command(["ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).split()
        if not ids:
            report["blockers"].append("PROJECT_NOT_FOUND:" + project)
            continue
        for c in json.loads(command(["inspect", *ids])):
            labels, state, host = c["Config"].get("Labels") or {}, c["State"], c["HostConfig"]
            all_containers.append(c)
            report["containers"].append({
                "project": project, "service": labels.get("com.docker.compose.service"),
                "id": c["Id"], "name": c["Name"], "image": c["Image"],
                "status": state["Status"], "exit_code": state["ExitCode"],
                "health": (state.get("Health") or {}).get("Status", "none"),
                "started": state["StartedAt"], "restarts": c["RestartCount"],
                "oom": state["OOMKilled"], "user": c["Config"].get("User", ""),
                "memory": host["Memory"], "memory_plus_swap": host["MemorySwap"],
                "mounts": mount_summary(c.get("Mounts", []))})
    prod_sources = {m["Source"] for c in all_containers
                    if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == PROD
                    for m in c.get("Mounts", [])}
    shared = sorted({m["Source"] for c in all_containers
                     if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == STAGE
                     for m in c.get("Mounts", []) if m["Source"] in prod_sources})
    report["stage_production_shared_mount_sources"] = shared
    if shared:
        report["blockers"].append("STAGE_PRODUCTION_SHARED_MOUNT_SOURCE")
    report["files"] = [{"path": str(p), "exists": p.is_file(),
                         "mode": oct(p.stat().st_mode & 0o777) if p.exists() else None}
                        for p in [*FILES, ENVFILE]]
    missing = [str(p) for p in [*FILES, ENVFILE] if not p.is_file()]
    if missing:
        report["blockers"].append("REQUIRED_BINDING_FILE_MISSING")
    else:
        # Use existing files, research profile and the required memory overlay LAST.
        args = ["compose", "--project-directory", str(RELEASE / "ops"),
                "--env-file", str(ENVFILE), "-p", STAGE, "--profile", "research"]
        for p in FILES:
            args += ["-f", str(p)]
        # Shell overrides are not allowed to replace the identified file bindings.
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("NEXUS_", "HUB_", "COMPOSE_"))}
        config = json.loads(command([*args, "config", "--format", "json"], env=clean_env))
        report["rendered_project"] = config.get("name")
        report["memory_overlay_last"] = str(FILES[-1])
        report["services"] = {}
        if config.get("name") != STAGE:
            report["blockers"].append("RENDERED_PROJECT_MISMATCH")
        for name in ("api", "research-worker", "postgres", "ingress"):
            service = config["services"][name]
            current = [c for c in all_containers
                       if (c["Config"].get("Labels") or {}).get("com.docker.compose.project") == STAGE
                       and c["Config"]["Labels"].get("com.docker.compose.service") == name
                       and not c["Config"]["Labels"].get("com.docker.compose.oneoff", "false").lower() == "true"]
            expected_mounts = []
            for v in service.get("volumes", []):
                source = v["source"]
                if v["type"] == "volume":
                    source = config["volumes"][source].get("name", STAGE + "_" + source)
                expected_mounts.append({"type": v["type"], "source": source,
                                        "target": v["target"], "read_only": v.get("read_only", False)})
            expected_mounts.sort(key=lambda m: (m["target"], m["source"]))
            check = {"image_reference": service["image"], "configured_user": service.get("user", "IMAGE_DEFAULT"),
                     "memory": service.get("mem_limit"), "memory_plus_swap": service.get("memswap_limit"),
                     "ports": service.get("ports", []), "mounts": expected_mounts,
                     "profiles": service.get("profiles", [])}
            env = service.get("environment") or {}
            check["safe_controls"] = {k: env[k] for k in FLAGS if k in env}
            if len(current) != 1:
                report["blockers"].append("EXPECTED_ONE_CURRENT_SERVICE:" + name)
            else:
                c = current[0]
                actual_env = environment(c["Config"].get("Env"))
                changed = sorted(k for k, v in env.items()
                                 if (actual_env.get(k) != str(v) if v is not None else k in actual_env))
                check["environment_diff_keys"] = changed
                check["configured_environment_preserved"] = not changed
                check["mounts_preserved"] = expected_mounts == mount_summary(c.get("Mounts", []))
                image = json.loads(command(["image", "inspect", c["Image"]]))[0]
                expected_user = service.get("user", image["Config"].get("User", ""))
                check["runtime_user_preserved"] = str(expected_user) == c["Config"].get("User", "")
                check["current_image_revision"] = (image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
                check["current_image_source_sha256"] = (image["Config"].get("Labels") or {}).get("nexus.source.sha256")
                if changed or not check["mounts_preserved"] or not check["runtime_user_preserved"]:
                    report["blockers"].append("CURRENT_CONFIG_DIFFERENCE_REQUIRES_REVIEW:" + name)
            if name == "api" and (service.get("mem_limit"), service.get("memswap_limit")) != (1610612736, 1610612736):
                report["blockers"].append("API_1536_MIB_OVERRIDE_NOT_PRESERVED")
            report["services"][name] = check
        report["networks"] = {k: {"name": v.get("name"), "internal": v.get("internal", False)}
                              for k, v in config.get("networks", {}).items()}
    report["disk_free_bytes"] = shutil.disk_usage("/").free
    report["acceptance_and_backups"] = "NOT_TESTED_BY_THIS_READ_ONLY_CHECK"
    print(json.dumps(report, indent=2))
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not print interpolated config, subprocess stderr or exception values.
        reason = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("DOCKER_READ_FAILED:") else type(exc).__name__
        print(json.dumps({"scope": "READ_ONLY_EXISTING_RC3", "status": "BLOCKED", "reason": reason,
                          "deployment_performed": False, "credential_values_printed": False}))
        sys.exit(1)
