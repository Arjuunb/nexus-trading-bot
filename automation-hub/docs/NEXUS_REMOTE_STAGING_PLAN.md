# NEXUS remote staging plan — Sprint 13

This is a reviewable plan, not deployment authorization. No VPS connection,
remote command, staging/production access, DNS/firewall/certificate change,
production restart, financial import or trading activation has occurred.

## Evidence and target boundary

Sprint 12 review `release-review:c2c4c32c85119363b0cd73445f9a42f272ce1706b97d7f3e7d0d2012844c0dab`
was reverified locally: all 1,948 retained files and all 25 contribution hashes
matched. Branch `work`, HEAD `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`,
163 preexisting dirty paths. HEAD alone does not identify this release.

`ops/assess-remote-staging.py` defaults to offline assessment. It verifies
retained release bytes and optionally an operator-generated **local** Compose
JSON. `--collect-local-host` samples this disposable cloud host, never the VPS.
It has no SSH, deployment or provider operation. Actual remote capacity,
permissions, installed supervision and customer authentication remain UNKNOWN.
Never upload a resolved Compose JSON containing credentials with public reports.

## Isolated architecture

`ops/staging.remote.compose.yaml` extends the existing locally validated service
definitions. It must never be merged with root `compose.yaml`. Compose 2.40.3
locally resolves `extends` and `!override`; target compatibility must be checked.
Use a unique `nexus-remote-stage-*` project and the same value for
`NEXUS_STAGING_PROJECT` and `NEXUS_REMOTE_STAGING_PROJECT` during interpolation.
No global `container_name`, external network, external volume or production-state
mount is used. Every state/metadata/spool/dataset/feed volume is project-scoped.

PostgreSQL has no host port. Only a separately reviewed, unused loopback HTTPS
port above 1023 is published; 80, 443 and 5432 are forbidden by preflight.
No public ingress, DNS, firewall or existing TLS change is authorized. Reviewed
staging certificate files must be separate from production certificates.
`staging.remote.nginx.conf` permits authentication and read-only diagnostics;
financial/trading paths and research mutation routes are absent from its allowlist.
Direct private-network API access remains an operator/security boundary, not a
claim that the application has no execution code.

API execution and feed autostart flags remain zero. Remote auth is Supabase with
emergency admin disabled; missing required bindings must fail closed. Existing
local emergency-admin smoke tests are not customer identity-provider validation.
The reviewed app binding file supplies **distinct staging** identity and session
credentials. Workers receive only their metadata role and their own spool/state;
no production exchange credentials or financial volume is provided.

Shadow is opt-in and disabled by default. The remote profile uses a read-only
finalized-feed volume and a separate writable cursor database. An absent journal
blocks startup; there is no fallback to another provider/revision. The profile
does not start a public-feed producer or authorize a candidate. Generic SMC
shadow remains blocked. Research workers use retained historical datasets and
do not become market-data or trading workers.

## Operator-reviewed infrastructure assessment

Record actual timestamp, target identity, evidence reference and reviewer for
each item. An unchecked item is UNKNOWN, not a passing measurement.

| Check | Required review |
| --- | --- |
| OS/runtime | OS/kernel, Docker Engine, Compose version, supported architecture, security patch state |
| CPU/memory | Available capacity **after existing production usage**, cgroup limits, swap/OOM policy |
| Disk/inodes | Free bytes/inodes, filesystem, storage driver, persistent paths, permissions, growth headroom |
| Existing workload | Current containers/resource use and occupied bindings, existing PostgreSQL services |
| Networking | Private metadata network, loopback port availability, firewall exposure, TLS architecture |
| Credentials | Separate customer identity project, runtime/operator PG roles, session/control secrets, no production reuse |
| Persistence | New volumes/markers, original financial authority untouched, independent spool/dataset storage |
| Recovery | Encrypted off-host destination, tested restoration, backup/log capacity, retention and alert ownership |
| Operations | Worker supervision, bounded restarts, heartbeat alerts, incident escalation and approved maintenance window |

Local profile budgets are API 1 CPU/768 MiB, PG 1 CPU/512 MiB, research 1 CPU/
512 MiB, optional shadow .5 CPU/384 MiB, ingress .25 CPU/96 MiB, bounded PIDs,
read-only roots and tmpfs. These are reused local limits, not proof that an
unknown VPS can spare them. A 4 GiB disk review headroom check is an operational
screen prompted by observed VFS exhaustion; full builds can need substantially
more. Measure peak usage on the actual storage driver before approving a target.
Initializer peak use matters too; local rehearsal now removes each completed
one-shot initializer before starting runtime services.

## Release review

Use immutable image IDs/repository digests, complete tracked/untracked source,
lock hashes, public-package input hashes, frontend and migration hashes. Preserve
the full manifest and originals for independent review. A mutable tag alone is
insufficient. `validate_remote_profile` rejects mutable images, shared state,
public database/API ports, unsafe worker networks and enabled execution flags.

Sprint 13 locally tests runtime deltas on the reverified Sprint 12 Python 3.11 /
Node 20 API and code-only worker bases. Both exact base layer chains and runtime
source hashes are recorded. This is **not** a clean root-Dockerfile rebuild or a
bit-identical reproducibility claim. A reviewed clean build from retained public
inputs, pinned bases and the final source inventory remains a release gate.
The Sprint 12 public wheel hashes/npm SRI and source bundle mechanisms are reused;
there are no new dependencies or changed locks.

## Proposed startup sequence — do not execute remotely

1. Obtain separate explicit target/access/rehearsal authorization. Review the
   capacity checklist, immutable release, workload maintenance plan and rollback.
2. Provision distinct staging bindings securely; never copy production `.env`,
   exchange keys, financial history or existing Compose volumes.
3. Transfer exact approved images and manifests using an approved authenticated
   channel. Verify content/config IDs after transfer. No transfer occurred here.
4. Resolve and validate only the separate remote profile, ownership, volume and
   network names, loopback binding and immutable images. Preserve sanitized proof.
5. Create the fresh staging PG database. Run its operator-only initializer and
   worker migrations 0001 then 0002; verify purpose/authority marker and restricted
   `nexus_staging_worker` role. Runtime role has no superuser/database/role/RLS-bypass
   privilege and cannot migrate. Never migrate the financial authority.
6. Initialize new marked state volumes, then start API with all execution flags
   zero. Verify actual customer login/session/RBAC and separate TLS trust.
7. Start the bounded research worker, verify common PG authority, health,
   heartbeat, lease recovery, cancellation and logical result uniqueness.
8. Keep shadow and public producer stopped until separate feed/candidate/
   lookback authorization. No registrations or observation are implicitly created.
9. Verify backups/restores, readiness diagnostics, source/image identities,
   storage/worker alerts and immutable evidence. Root `/health` is only liveness.
10. Conduct the separately reviewed failure/recovery soak and sign go/no-go.

Do not run `scripts/deploy.sh`. It targets the production Compose services and
may fetch Git state. No commit, push, reset, rebase or production restart is part
of this plan. Compose configuration validity is not permission to start it.

## Backup design and recovery

RPO is the maximum tolerable age of unrecoverable commits; RTO is the maximum
tolerable restoration outage. No remote target or achieved remote RPO/RTO is
asserted. Local drills measure actual PG dump/restore, API restart and worker
SIGKILL-to-ready time. A sampled local fixture is not power-loss certification.

Use consistent `pg_dump` and the existing SQLite backup API while preserving each
original authority. Back up metadata plus exact dataset archives, immutable
reports and operational spools. A hash or external-reference row alone does not
retain the referenced bytes. `staging_recovery` atomically names a verified,
bounded non-financial artifact bundle only after all copies pass integrity checks;
interrupted `.partial` directories never become completed bundles.

Proposed off-host storage must use independently scoped encryption/access,
immutable generation names, checksums, reviewed retention, separate credentials,
capacity monitoring and recurring restore tests. Never upload a production
backup without explicit authorization. Do not silently copy original financial
records into staging. The helper seals already-created backups; it does not
create a financial authority, provide encryption or certify durable off-host
storage. SQLite authority backups follow their existing explicit workflow.

On failure, stop submissions and quiesce only the reviewed staging project.
Preserve attempts, fencing generations, committed results, pending spools and
original references. Restore into a **new** disposable database, compare schema,
authority, row counts and hashes before review. Do not switch to old SQLite,
overwrite a financial ledger, drop evidence tables, relabel replay as forward
data, or reset holdout history. Keep additive schemas for compatible rollback.
Remove only explicitly owned staging resources; broad Docker prune is forbidden.

## Go/no-go

**REMOTE STAGING: NOT READY. PRODUCTION: NOT READY.** Mandatory unverified gates
include remote authorization/capacity/permissions, customer identity/TLS,
clean release build, durable off-host restore, representative target cutover,
sustained public-feed reliability, independent process distribution soak,
monitoring/incident policy and remote restart/power-loss validation. Financial
completeness and all execution authorization remain separate gates. The safest
next step is independent review of the retained local artifacts and offline
target checklist; any access or soak needs its own explicit authorization.
