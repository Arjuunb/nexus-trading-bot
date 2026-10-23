# VPS preflight — Sprint 14

**NOT RUN. Actual VPS: UNKNOWN. Remote access is not authorized.** This is a
read-only plan for a separately approved target, identity and time window. Do
not start containers, transfer files, restart services, change firewall/DNS,
read credentials, or inspect financial records during preflight. Record UTC
time, target identity, command exit status and reviewer with each result.

## Proposed read-only commands

An authorized operator may execute these individually after reviewing the
target. They are deliberately not an automatically executed SSH script.

```sh
date -u
uname -srvmo
cat /etc/os-release
docker version
docker compose version
docker info --format '{{.Architecture}} {{.OSType}} {{.Driver}} {{.DockerRootDir}}'
getconf _NPROCESSORS_ONLN
free -m
swapon --show --bytes
df -hT
df -i
docker system df
docker ps -a --format '{{.Names}} {{.Image}} {{.Status}} {{.Ports}}'
docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.PIDs}}'
docker network ls
docker volume ls
ss -lntup
```

For **reviewed container names only**, use targeted inspect formats for
`.Mounts`, `.NetworkSettings.Networks`, `.HostConfig.Memory`, `.HostConfig.NanoCpus`,
`.HostConfig.PidsLimit`, `.HostConfig.RestartPolicy`, `.State.Health.Status`
(when defined), `.Config.User` and `.Config.Image`. Do not dump full inspect
output: environment bindings may contain secrets. Check existing PostgreSQL
and reverse-proxy container health/ports through these metadata fields, not
database queries or configuration-file contents.

An authorized administrator should additionally review patch support and
pending security updates using the installed OS's read-only package commands;
read effective SSH policy with `sshd -T` (no private keys); inspect firewall
rules with the installed tool's read-only listing; inspect Docker socket and
reviewed storage-directory ownership/modes with `stat`; and inspect the
**public** staging certificate's issuer, SANs and dates with `openssl x509`.
Privilege escalation and access outside the authorized scope remain separate
approval gates. Never print secret files, dotenv values or TLS private keys.

## Required assessment

| Area | Required evidence | Current status |
| --- | --- | --- |
| Runtime | Supported architecture, OS/kernel patches, Engine/Compose compatibility with extends/override | UNKNOWN |
| Capacity | Available CPU/RAM after existing usage; swap/OOM policy; disk/inodes and driver | UNKNOWN |
| Conflicts | Existing service/project/network/volume names and port ownership | UNKNOWN |
| Persistence | New scoped volumes; distinct authority markers; reviewed permissions | UNKNOWN |
| Security | SSH/firewall review, separate credential names/scopes, socket access, TLS/auth ownership | UNKNOWN |
| Operations | Monitoring owner, maintenance window, encrypted off-host restore and rollback approvals | UNKNOWN |

Do not infer capacity from total RAM or this cloud host. Runtime reservations
are API 768 MiB, PG 512 MiB, research 512 MiB and ingress 96 MiB: **1,888 MiB**
and 3.25 CPU limits in aggregate; optional shadow adds 384 MiB and 0.5 CPU.
Limits are ceilings, not measured utilization or available VPS capacity.
Allow headroom for existing workloads, kernel, page cache, initializer peaks,
snapshots, logs, datasets and backup generations. Build elsewhere and transfer
approved images only after authorization; VFS can multiply physical layer and
container storage. A 4 GiB free-disk screen alone is not a capacity guarantee.

## Go/no-go

Preserve all existing volumes, networks, services, TLS and ports. Select a new
`nexus-remote-stage-*` project and an unused loopback port above 1023, excluding
80/443/5432. No remote startup until all mandatory release and target gates in
[NEXUS_RELEASE_CANDIDATE.md](NEXUS_RELEASE_CANDIDATE.md) have passing evidence.
The smallest next external step is explicit authorization for the above
read-only inventory on a named VPS. It does not authorize deployment or a feed.
