# Sprint 14 addendum — staging prerequisites and release reconstruction

**Preparation and validation only. No VPS connection, transfer or deployment.**
Earlier Sprint 14 reports are historical evidence and remain unchanged. The
latest VPS assessment is at
`/workspace/nexus-audit/NEXUS_VPS_INFRASTRUCTURE_ASSESSMENT_20261013_1606UTC.md`.
Its measurements were supplied by the operator at 17:06 London / 16:06 UTC on
13 October 2026, not independently collected by these tools.

## Release packet

The earlier archive was linked into temporary storage that is now unavailable.
The exact API and worker images remain in the local daemon. Before these
changes, all 1,362 current operator-source inputs reproduced the earlier
`c6e02b7a6c5b15302cf7d632739484ef53a3f8a2ed76d01304279342842bd52a`
source identity. The API's retained build manifest and actual files reproduced
`e7de53f84034262433699c91588f1b60f8915e82861cbd0fd02de6b73cd44dc1`;
only three later documentation updates differed.

`ops/recover-staging-release.py` reuses `services.release_source.bundle_source`
and the existing archive verifier. It freezes required tracked and untracked
inputs, denies private/runtime state before reading, checks unchanged runtime,
build definitions, locks and migrations, and records operator-only changes.
It verifies actual API source, compiled frontend hashes, Python 3.11/binary
PostgreSQL driver, locked runtime dependency versions and the worker's copied
inputs. It exports four existing immutable images, independently verifies each
configuration hash and uncompressed layer hash, and checks app-layer private
input exclusion. No image rebuild, pull, publication or Git action occurs.

The new packet contains source/archive hashes, lock and migration inventories,
frontend/source proofs, image exports, selected public test reports, a release
manifest and per-file integrity manifest. It gets a **new** review identity.
Absent old archive bytes, original build caches and raw test logs are explicitly
unavailable; recreated bytes never inherit the missing archive checksum.

Example local preparation, outside the repository (does not deploy):

```sh
python ops/recover-staging-release.py \
  --output /workspace/nexus-audit/NEW_REVIEW_DIRECTORY \
  --evidence /absolute/path/to/current-public-test-report.xml
```

Use a new output path. Completion is atomic; `.partial` output is not a release.
Local Docker config IDs are distinct from registry manifest digests. Transfer
exact exports only after separate authorization, then verify loaded config IDs.
Public registry retrievability and off-host backup are separate requirements.

## Resource limits and target sampling

Only the isolated staging profile changes. Production `compose.yaml`, trading
logic, risk settings and financial writes remain untouched. Both one-shot
initializers now have **0.5 CPU, 256 MiB and 64 PIDs**; run sequentially and remove
only their completed, owned containers. With PostgreSQL already running, this
initialization phase has 768 MiB / 1.5 CPU declared limits. Runtime remains:

| Phase | Memory ceilings | CPU ceilings |
| --- | ---: | ---: |
| API + PG + loopback ingress | 1,376 MiB | 2.25 |
| Above + one research worker | 1,888 MiB | 3.25 |
| Optional shadow, separately authorized | 2,272 MiB | 3.75 |

Local cgroup validation uses the actual retained initializer image and confirms
kernel CPU/memory/PID limits, metadata purpose and the restricted runtime role.
Local fixture duration is not a target performance or RPO/RTO guarantee.

The reported 5.5 GiB available RAM leaves about 3.66 GiB after standard runtime
ceilings if other use stays unchanged. The reported 134.51% Docker CPU is about
1.345 logical CPU equivalents for one application. Other container/host load,
production peaks, steal, OOM policy and growth reserves remain unverified.

`ops/sample-vps-resources.py` prints **NOT_RUN** by default. It has no SSH,
deployment, service-control, financial-query or arbitrary-command interface.
An operator must explicitly attest read-only authorization and independent
host-key verification and supply timezone-aware window boundaries. It checks
the local target IP, permits only the local Docker daemon, runs a fixed metadata
allowlist, stops at the window/budget boundary and limits output to 4 MiB.
Command errors report categories and exit status without untrusted stderr.

Default sampling is 12 intervals of five seconds, with command overhead included
in a bounded budget. Maximum total budget is 300 seconds. It records whole-host
interval CPU/steal, RAM/swap, root disk/inodes, Docker root/driver/storage,
container usage, bindings, network/volume names, update/reboot status and limited
security metadata. It never reads Docker container environment values.

```sh
# Safe planning invocation; no target inspection occurs.
python3 ops/sample-vps-resources.py --samples 12 --interval 5
```

After a **new** explicitly approved target window, an operator may use
`--authorize-read-only --independent-host-key-verified --target <approved-ip>
--window-start <ISO-8601-with-UTC-offset> --window-end <ISO-8601-with-UTC-offset>
--stdout`. This emits sanitized JSON without creating a report file on the VPS.
Collect through the operator's trusted local SSH environment; do not export the
Mac's SSH identity into this cloud task. No authorized execution occurred here.

The packet's storage model separately records compressed exports, distinct raw
layers, cumulative layer-prefix sizes relevant to VFS and runtime/initializer
image sizes. These are planning quantities, not actual target allocations.
Verify the VPS driver and backing filesystem before selecting a budget. Add
image-load temporary space, state/WAL, datasets, spools, logs, backup generations,
restore temporary space and a production reserve. The reported 22 GB free is
promising; 37.17 GB reclaimable cache is not free space and must not be counted
or deleted by this procedure. No on-VPS build or broad prune is proposed.

## Separate security preparation

`ops/prepare-staging-bindings.py prepare` creates a new private directory outside
the repository. Its generated operator/runtime PG passwords, session, control
and webhook secrets are independent 256-bit values, stored with mode 0600 in a
0700 directory. No existing production dotenv or SSH identity is read or copied.
Project names must be `nexus-remote-stage-*`; images must be immutable config IDs;
ports 80/443/5432 and privileged/invalid UIDs are rejected.

Both project variables match. Auth is Supabase, emergency admin is disabled,
execution/feed/research autostart and monitoring engine flags are zero, and
shadow stays disabled. The existing profile provides a fresh private PostgreSQL
authority, restricted `nexus_staging_worker` role, internal worker network,
project-scoped volumes and loopback-only ingress. No producer is introduced.

Customer Supabase URL/key are **not invented**. Supply a distinct staging identity
project through the approved secret mechanism, never in chat or CLI arguments.
Customer login/RBAC and staging ownership must still be tested after separately
authorized startup. Local self-signed TLS generation is opt-in and explicitly
fixture-only; it does not verify customer trust. A separate reviewed certificate,
key, intended hostname, client trust and renewal owner remain target gates.

The `check` command rejects empty/directory/unmarked/symlinked binding paths,
wrong projects, mutable images, unsafe ports, production/execution credentials
and enabled execution. It emits only sanitized diagnostics. A BLOCKED check
returns exit code **2**, preventing accidental `check && startup` shell chains.
It has no deploy subcommand and never treats configuration validity as approval.

## Isolated backup and recovery procedure

Preparation scope is metadata and non-financial research artifacts only. Never
copy original PA/SMC/general-paper/Trading Instance databases or query their rows.
The existing SQLite staging-backup helper requires a disposable-local marker;
do not forge that marker to apply it to production or an unreviewed remote store.

After separate approval, the operator must:

1. Verify the fresh project's metadata database, purpose `research_shadow_metadata`,
   original authority ID, writer role and migration versions 0001/0002. The runtime
   role has no superuser, database/role creation or RLS bypass privilege.
2. Capture a consistent `pg_dump` of the approved `nexus_workers` schema. Retained
   candle payloads and references are stored there; manifests alone do not retain
   data. Capture approved separate operational spools and artifacts through their
   existing consistent backup workflows, not live file copies or financial imports.
   Quiesce only the approved new staging writers where cross-store consistency
   requires it; production stopping is outside the procedure.
3. Record hashes, source authority, schema versions, dataset content/reference
   checks, validation/attempt/holdout IDs and artifact inventory. Seal only complete
   bundles with `services.staging_recovery`; its existing 256 MiB/file-count limits
   apply. Exceeding the bound fails closed and requires a reviewed storage plan.
   Encrypt and retain through independently scoped off-host storage; local hashes
   alone are not durable backup or encryption.
4. Restore into a **different** staging database/namespace. Verify original
   logical authority, schema/role restrictions, job/result identities, immutable
   record counts and reconstructed candle content hashes before accepting it.
   Never run workers simultaneously against source and verification restore.
   Restoring a dataset with a different provider revision is forbidden.
5. Only after an independently approved cutover, reconcile pending receipts,
   expired leases, spools and committed results using existing fencing/idempotency
   mechanisms. Never reset attempts, holdout exposure, financial history or balances.

Local PostgreSQL integration covers consistent dump/restore into another DB,
dataset payload/reference reconstruction, one logical committed result, role/
authority preservation and original-source non-mutation. Existing local worker
tests cover crash/restart, heartbeats, lease fencing and committed-result recovery.
Exact fresh test counts and original reports accompany the new packet.

Rollback quiesces only the newly approved staging project, retains its state and
evidence and selects a compatible approved image. No `down -v`, shared-volume
deletion, broad prune, root deployment script, production proxy/service change
or financial database overwrite is part of rollback. Remote achieved RPO/RTO,
restore performance and power-loss behavior remain UNKNOWN until measured.

## Deployment gates and next authorization

Local packet reconstruction, resource controls, private settings preparation,
profile validation and disposable recovery tests can PASS independently of remote
deployment. A completed packet is not human release or execution authorization.

External prerequisites remain: bounded representative VPS sampling and storage
driver/budget; architecture/profile compatibility; unused loopback port and fresh
resource names; security update/restart risk disposition; distinct staging customer
credentials/TLS; monitoring/incident/backup ownership; target-shaped migration,
restore and restart rehearsal; and explicit transfer/resource/startup approval.
No untested target gate is PASS. Public feed and shadow remain off, so an absent
live soak does not authorize enabling them. XRP profitability stays UNVERIFIED.

The smallest next externally authorized operation is a finite read-only sampling
and namespace/binding verification on the named VPS through the trusted local
SSH environment, in a new explicit window. Review the actual remaining gates
before separately approving transfer and minimal isolated startup.
