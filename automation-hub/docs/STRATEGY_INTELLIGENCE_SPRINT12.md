# Strategy Intelligence Sprint 12

## Implementation and verification scope

Sprint 12 remediates packaging and local staging blockers using the existing
API, simulator, research queue, shadow worker, metadata migrations and financial
readers. It introduces no trading engine, strategy, financial authority or queue.
No production/VPS/remote staging access, Git mutation, paper activation or live
activation is authorized or performed. Original financial and strategy source
files remain unchanged against the Sprint 12 starting hashes.

The production Dockerfile now installs a hashed Python 3.11 dependency lock and
the existing `worker-postgres` extra. Both bundles actually built with Node
20.20.2; actual image inventory reports Python 3.11.17, psycopg 3.3.6 and binary
libpq. `pip check` passed. Build inputs, source bytes, dependency versions,
frontend hashes, migration hashes and image digest are retained. This is not a
bit-identical rebuild guarantee or release approval.

An explicit disposable local profile adds private PostgreSQL metadata, the
existing independently supervised workers, separate state/spool/dataset volumes,
HTTPS ingress and initialization services. It does not change production
`compose.yaml` or execute `scripts/deploy.sh`. Generic SMC shadow remains blocked;
the rehearsal starts a shadow process with zero registrations, never observation.

Final suite results, contribution inventory and qualified local readiness are
recorded below after final validation. Features are distinguished from operational
certification: financial completeness, customer Supabase identity, sustained
public feeds and any remote deployment remain separate unresolved gates.

## Repository audit and baseline

Repository: `/workspace/nexus-trading-bot`, branch `work`, starting HEAD
`7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`. The initial tree contained **144 dirty
paths**: 142 hashed files and two dependency links. Its full status/hashes and
tracked diff are retained as `sprint12-initial-worktree.json` and
`sprint12-initial-tracked.diff`. No path is attributed to a sprint solely by its
name. The retained Sprint 11 cumulative 44-path inventory provides specific
prior attribution; other Sprint 0–10 report claims are historical and ambiguous
ownership remains UNKNOWN. Existing working-tree changes are preserved.

Read Sprint 10, Sprint 11 and the deployment-readiness report and inspected the
real Dockerfile, production Compose/deploy/health scripts, worker profiles,
entrypoints, metadata selectors/migrations, native authorities, auth/cookie/RBAC,
spools, retained input/backup mechanisms, API and test architecture. The fresh
baseline was **73 passed, 0 failed, 0 skipped, 0 warnings in 32.23 seconds**.
The prior reported 3,856/3,326 totals were not substituted for current reruns.

Verified blockers included absent API PostgreSQL packaging, unbounded release
identity when using HEAD alone, no combined isolated staging profile, unavailable
production-runtime validation, missing layered readiness and limited cloud storage.
The financial/funding and live-feed gaps remain genuine, not packaging defects.

## Production image and release engineering

`Dockerfile` keeps Node 20 and Python 3.11. `ops/api-requirements.lock` is generated
from the existing project extra, hub requirements and pinned existing packaging
tooling with uv, Python 3.11 resolution and hashes. pip verifies hashes, installs
`.[worker-postgres]` without a second dependency resolution, imports binary
psycopg and checks installed compatibility. No dev-only test runner is added to
the API image. Locks must be regenerated and reviewed when declarations change.

`ops/build-staging-release.py` performs read-only Git discovery of tracked and
required untracked source. `services/release_source.py` freezes exact bytes in a
new context, validates paths/symlinks/limits, and retains their SHA-256 identities.
Known runtime state, dotenv, caches and financial snapshots are excluded before
reading. The Docker context additionally excludes SQLite files and WAL/SHM files.
There is no Git commit/stash/reset or tracked-files-only release shortcut.

Public Python artifacts verify against lock hashes; npm verifies lockfile SRI.
An optional named `build_inputs` context supports a fully offline RUN phase and
is checked for changed, unlisted or unsafe inputs. It contains public wheels and
npm cache objects only. Default empty inputs retain the normal online build path.
Optional public CA trust is a temporary BuildKit mount, never an image secret.
No TLS or artifact-integrity bypass is used. Both frontend outputs are hashed.

Actual API image:
`sha256:8c4dc4af8e0a18e000db398027cca5c354271a7976a5bfaa35edff1764562b83`.
Frozen API build source:
`f1e0c04c481660775e2e43b2e17fe32dac533c528a38373e4ac441a34287f148`.
The actual runtime/private-input scan and artifact manifest are retained.
The final operator profile/scripts have separate identities from this API build
snapshot: they were refined during rehearsal and are not silently claimed to be
identical to files already baked into the image. The API/runtime service sources
are checked against that frozen context. The final composite release preserves
both exact image inputs and the tested operator artifacts.

Base references observed in the actual build:

* `node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0`
* `python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`

Exact final image export, public artifact inputs and source references are local
review assets, not off-host backup or proof that upstream bases remain available.
Build timestamps, frontend tooling metadata and mutable tags prevent a claim of
byte-identical rebuilds. Future approved rebuilds must use retained inputs and
record/verify base digests, never silently resolve a newer revision as identical.

## Staging architecture, isolation and supervision

`ops/staging.local.compose.yaml` is opt-in, project-scoped and independent of the
production profile. PostgreSQL exposes no host port. Research/shadow workers
have only the internal metadata network, read-only root filesystems, bounded
CPU/memory/PIDs/tmpfs, two maximum on-failure restarts, graceful-stop limits and
actual health probes. The API shares private metadata and a separate ingress
network; only HTTPS ingress publishes an ephemeral **loopback** port. This API
network is not a claim of hard outbound isolation; credentials, execution flags,
no active instances and explicit read-only rehearsal actions remain boundaries.

One-shot initializers use the smaller code-only worker image. The state helper
sets permissions/markers only on newly created staging volumes. The metadata
helper accepts the fixed private staging target, creates a restricted role and
reuses `migrate_worker_database`. Operator credentials never enter runtime
workers. API/worker bindings select the same authority; failures never select
SQLite as a fallback. Separate app, research, shadow, dataset and PG volumes
survive process restarts during a run. They are removed only as explicitly owned
disposable fixtures on teardown; no original financial volume is mounted.

`ops/rehearse-staging.py` defaults to a plan; `--run-disposable` explicitly starts
local resources. It generates distinct fixture auth/operator/runtime bindings,
uses a local Docker socket and retains redacted diagnostics. `--exercise-shadow`
starts the existing observer process with zero authorized registrations. This
is not observation authorization and creates no shadow decisions or orders.

The API runs its production profile without test/auth bypass. Existing Secure
cookies remain enabled. A fresh one-day localhost certificate is explicitly
trusted by the HTTPS client; Chromium trusts only that generated fixture SPKI,
not arbitrary self-signed certificates. The rehearsal uses existing emergency
legacy auth, not actual customer Supabase JWT, remote TLS or a production login
certification. Both frontend bundles render in Chromium from the actual image.

Worker generation/authority fields are propagated in existing heartbeat files.
Actual child-process SIGKILL, rather than Docker's manual-stop API, exercises
on-failure supervision. Exactly one replacement generation appears, its restart
count is bounded and its original metadata authority is preserved. PG and API
restarts and graceful teardown are exercised. This remains a short local drill,
not continuous uptime, a power-loss test or an installed remote supervisor.

## PostgreSQL, cutover, backup and recovery

Existing real-PG tests cover atomic claims, competing workers, leases, heartbeat
races, stale fencing, cancellation, lost commit ACK, immutable/unique logical
results, crashes/restarts and tenant isolation. Physical execution may repeat;
logical result commitment is unique. Runtime roles are not superusers and cannot
create roles/databases/migrate or bypass RLS. Multi-owner application checks are
not misrepresented as per-worker database tenant RLS.

The unchanged cutover implementation freezes consistent SQLite snapshots,
rejects active leases/unsupported populated tables, verifies ownership/frozen
configuration/data hashes and immutable references, and imports only approved
metadata. Actual populated dry-run/import/rerun and conflict/DDL rollback tests
preserve jobs, attempts, results, campaigns, candidates, holdout exposure and
shadow lineage. No current application authority is switched and no financial
row is imported. Representative remote migration remains unauthorized/unverified.

No new DDL is introduced in Sprint 12. Metadata initialization reuses worker
migrations 0001 then 0002; native financial 0001 and shadow 0003–0008 keep their
existing separate authorities and compatibility rules. The identity migration
scanner remains separate. Runtime workers cannot apply operator migrations.

`services/staging_backup.py` requires an explicit disposable marker, bounds input
size/count, reads SQLite originals query-only, uses the backup API and verifies
integrity, foreign keys, schema versions, row counts and exact original row hashes.
Restore writes a new destination and rejects corruption/path changes. Fixtures
cover PA/SMC/general paper, ledger, journal/decisions and spool namespaces. Actual
staging backs up/restores the original freshly initialized app stores. Real
`pg_dump`/`pg_restore` restore to a different disposable database preserves the
original authority identity. Exact datasets and immutable archives must accompany
metadata backups; a hash alone cannot reconstruct missing content. No production
backup, original financial repair or off-host durability certification occurred.

## Financial and market-data gates

Financial implementation/rules and original SMC/PA/Instance source hashes are
unchanged. Existing quantity, episode, partial exit, cost, cash and outbox tests
remain enforced. The actual staging drill captures 26 financial table hashes
and verifies equality after browsers, worker/PG/API recovery and reporting.
Research workers load no execution runtime, have no app/financial volumes or
trading credentials, and no paper/live strategy is activated.

Verified accounting fixtures do not prove strategy profitability. Complete
original funding schedules/exposure, historical ownership/configuration, coherent
cohorts, cash/equity coverage and precise paper operational periods remain
PARTIAL/UNKNOWN. Trading Instances still lack a separate complete native
account/funding authority. XRP claims remain UNVERIFIED. No unknown cost is zero
and no old evidence is rewritten to become complete.

No external market-data provider is contacted in this sprint. Existing finalized
candle, gap/staleness, reconnect, duplicate, venue, revision, HTF/future-data and
retention regressions execute. The bounded `public_feed_soak` hook remains
available to a separately authorized caller; deterministic fixtures are not a
live soak. Public egress/sustained reliability and cross-process finalized-feed
distribution remain BLOCKED. Shadow observation is not authorized by a passing
local packaging or supervision test.

## Authenticated readiness contract

GET `/api/v2/strategy-intelligence/deployment-readiness` requires the existing
verified owner plus operator RBAC and returns `Cache-Control: no-store`.
No write/activation method, user filesystem path, DSN or execution object is
accepted. Authentication is verified before probes. Owner-specific spool counts
use parameterized reads; unowned artifacts, paths and credentials are not exposed.
Errors are sanitized. Existing interfaces and `/health` liveness are unchanged.

`nexus_deployment_readiness.v1` separates HTTP_LIVE, API_READY, AUTH_READY,
POSTGRES_READY, RESEARCH_WORKER_READY, SHADOW_WORKER_READY, FEED_READY,
FINANCIAL_EVIDENCE_READY and OPERATIONAL_SPOOL_READY. Every check contains status,
UTC observation timestamp, source, authority identity, reason and an optional
evidence reference. Statuses are READY, DEGRADED, BLOCKED and UNKNOWN. Worker
reports must be fresh, authority-compatible, database-ready and execution-free.
Shared-container PID verification is not invented: recent self-report is labelled
as such, and actual per-container probes/supervision are separately tested.
FEED remains BLOCKED and generic financial readiness UNKNOWN. A cohort-specific
committed reconciliation is needed for financial qualification. Nothing in this
contract grants execution, strategy promotion or production certification.

## Intermediate failures and recovery evidence

Failures are preserved rather than counted as successes:

* New-module test-first collection failed before implementation; unit guards
  then passed without removing assertions.
* A private build daemon failed a minimal runc startup probe before application
  execution. The established runtime passed the same minimal probe. That daemon
  was stopped/removed; its owned temporary state/proofs remain.
* Online build RUN phases could not resolve package registries, including host
  networking. Offline retained hash/SRI-verified inputs built successfully.
* VFS copies initially exhausted staging disk during PG initialization. Two
  initialization services now use the code-only image. Old task-built worker
  images were identified by retained build logs, exported and verified before
  reclaiming only their unshared cache branches. Unknown volumes were preserved.
* Internal-only API networking did not publish a port. Separate loopback HTTPS
  ingress and private metadata fixed the topology.
* Production Secure cookies correctly rejected an HTTP-only login. HTTPS fixed
  the rehearsal; no cookie or auth protection was downgraded.
* nginx's unused default cache directories failed on a read-only root. All temp
  paths now use bounded tmpfs, retaining the read-only root and dropped caps.
* Docker kill denotes a manual stop and disables restart policy. The crash
  injection now kills the actual child in its own namespace, and verifies a new
  generation and bounded automatic restart count.

Recorded cache proofs name exact task-owned IDs and preserved image hashes.
This is cloud workspace recovery, not a capacity/RPO/RTO certification. Dataset,
spool and PG durability on an actual persistent remote host remain prerequisites.

## Final results, inventory and go/no-go

The final validation and retained composite manifest below establish a local
rehearsal candidate, not release approval. The staging runbook documents exact
operator boundaries and recovery.

## Exact contribution inventory

There are **25 contribution paths**: six pre-existing dirty files extended, one
previously clean tracked Dockerfile modified and 18 new files. The final tree has
163 dirty paths. Of the 142 pre-existing files, **136 retain their original
SHA-256 hashes**; both environment links are preserved. No file is deleted and
HEAD is unchanged. Original financial, PA, SMC and Instance rule sources retain
their initial hashes. The machine inventory is `sprint12-contribution-inventory.json`;
prior ownership is supported only by retained report/inventory evidence, otherwise UNKNOWN.

| Path | Contribution |
| --- | --- |
| `.dockerignore` | Extended prior dirty work |
| `Dockerfile` | Previously clean tracked file |
| `automation-hub/docs/NEXUS_DEPLOYMENT_READINESS.md` | Extended prior dirty work |
| `automation-hub/docs/NEXUS_STAGING_RUNBOOK.md` | New file |
| `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT12.md` | New file |
| `automation-hub/routers/strategy_intelligence.py` | Extended prior dirty work |
| `automation-hub/services/deployment_readiness.py` | New file |
| `automation-hub/services/release_source.py` | New file |
| `automation-hub/services/research_worker_main.py` | Extended prior dirty work |
| `automation-hub/services/shadow_worker_main.py` | Extended prior dirty work |
| `automation-hub/services/staging_backup.py` | New file |
| `automation-hub/tests/test_sprint12_api.py` | New file |
| `automation-hub/tests/test_sprint12_backup_profile.py` | New file |
| `automation-hub/tests/test_sprint12_release_readiness.py` | New file |
| `automation-hub/tests/test_sprint12_staging_integration.py` | New file |
| `ops/api-build-requirements.in` | New file |
| `ops/api-requirements.lock` | New file |
| `ops/build-inputs.md` | New file |
| `ops/build-staging-release.py` | New file |
| `ops/rehearse-staging.py` | New file |
| `ops/shadow-worker.Dockerfile` | Extended prior dirty work |
| `ops/staging-browser-smoke.cjs` | New file |
| `ops/staging-initialize.py` | New file |
| `ops/staging.local.compose.yaml` | New file |
| `ops/staging.nginx.conf` | New file |

## Operational milestones and remaining blockers

| Gate | Disposable local result | Remote / production boundary |
| --- | --- | --- |
| Actual production image and PostgreSQL driver | VERIFIED | Release approval and target artifact verification still required. |
| Actual frontend bundles / HTTPS auth | VERIFIED locally | Uses generated fixture TLS and emergency legacy identity, not customer Supabase/domain TLS. |
| Common PG authority / restricted roles | VERIFIED locally | Persistent target permissions, customer identity and representative data unverified. |
| Atomic leases/fencing/result commitment | VERIFIED locally by real-PG suites | Target soak, restart and incident operations unverified. |
| Independent worker supervision/resources | VERIFIED locally | No remote service installed; no continuous uptime or power-loss certification. |
| Populated cutover / additive migration recovery | VERIFIED locally | Actual authority untouched; representative remote export/cutover unauthorized. |
| Backup/restore and exact local retention | VERIFIED locally | Off-host durability, expiry/reconstruction guarantees and remote restore unverified. |
| Tenant/API isolation and private worker credentials | VERIFIED locally | Deployed secret bindings/identity permissions require independent review. |
| Financial non-mutation | VERIFIED locally | Does not establish historical/forward profitability. |
| Complete financial performance evidence | PARTIAL / UNKNOWN | Funding schedules/exposure, ownership/cohorts, cash/equity and exact operational intervals still missing. |
| Sustained finalized feed / cross-process distribution | BLOCKED | No external provider access/soak, no approved distribution boundary. |
| Complete release source and final regressions | Final results recorded below | Neither commits nor automatic approval are implied. |
| Production activation/deployment | NOT AUTHORIZED | No live/paper activation, promotion or deployment. |

Sprint 13 should first obtain explicit remote rehearsal authorization, provision
durable capacity/monitoring/backups, verify customer identity/TLS/secret bindings
and run representative migration/restore/restart drills. Separately close original
funding/exposure and account/cohort/equity/operational evidence; implement/review
the single-publisher finalized-feed boundary and authorized bounded soak; retain
exact dataset/archive revisions and review expiry/reconstruction policy. None of
these steps authorizes strategy changes, paper activation or live execution.

## Final test evidence

All final code/configuration changes preceded the final focused, root, Hub,
database and browser runs. Only documentation/review artifacts were finalized
afterward. Original JUnit/logs preserve identities and exact counts. Suites
overlap and must not be summed.

| Selection | Passed | Failed/errors | Skipped | Warnings | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Sprint 12 focused, including actual image/HTTPS/PG/worker/Chromium rehearsal | 37 | 0 | 0 | 0 | 82.88 s |
| Repository root | 3,893 | 0 | 15 | 90 | 621.90 s |
| Automation Hub | 3,363 | 0 | 15 | 88 | 596.24 s |
| Database/recovery/migration/security/financial/resource selection | 527 | 0 | 0 | 4 | 250.58 s |
| Dedicated real PostgreSQL | 30 | 0 | 0 | 0 | 79.46 s |
| Existing Chromium regression suite | 154 | 0 | 0 | 4 | 6.1 min |
| Actual-image HTTPS Chromium smoke (inside integration selection above) | 2 scenarios | 0 | 0 | Not a separate pytest warning count | Completed |
| Dashboard typecheck | PASS | 0 | — | 0 | Completed |
| Dashboard production build | PASS | 0 | — | 1 Vite advisory | 7.46 s |
| Actual production Dockerfile, both Node 20 bundles, Python 3.11/binary psycopg/pip check | PASS | 0 | — | Build tooling notices retained | Completed offline |
| Actual image private-input/build-CA absence check | PASS | 0 | — | 0 | Completed |
| git diff --check | PASS | 0 | — | 0 | Completed |

The 15 Hub/root skips remain existing conditional risk-parity cases: eight
“not a validity scenario”, seven “only comparable when both approve”. No staging,
PG or browser test is silently skipped in these final opt-in runs. All 30 real
PG cases, 73 Sprint 11 focused tests and 37 new tests are included in the 527-test
selection. Disposable PG is actual 16.15; optional original RAM-backed fixtures
are not a substitute for durable storage/power-loss certification. The staging
profile uses project volumes and verifies process-restart persistence locally.

Warning audit: root/Hub FastAPI lifecycle deprecations 4/4; aiohttp redundant
cleanup option on fixed Python 3.12.14 82/80; Supabase HTTP option deprecations
4/4 (also 4 in the database selection). No new warning category, leak, unraisable
or unhandled-thread warning appeared. Chromium reports four existing
NO_COLOR/FORCE_COLOR display warnings. Vite reports its existing large-chunk
advisory. No global suppression, threshold weakening or deleted assertion.
Production build notices include existing package engine/deprecation/audit
messages preserved in its log; an offline build is not a dependency CVE audit.

Final backend commands use PYTHONDONTWRITEBYTECODE=1, NEXUS_STAGING_TEST=1,
NEXUS_PG_TEST_TMPFS=1, NEXUS_PG_WORKER_TEST=1, NEXUS_PG_SUPERVISOR_TEST=1,
NEXUS_SHADOW_CONTAINER_TEST=1, NEXUS_PG_OUTBOX_TEST=1 and
NEXUS_SPRINT11_RESOURCE_TEST=1 with the existing .venv Python and a /tmp cache.
Heavy selections run sequentially. The browser suite uses approved
/usr/bin/chromium and the existing fresh preview config. No uv sync/pruning of
prior dependency extras occurred. Cloud setup notes are saved as a draft only,
requiring user review/publication; saving is not runtime application or deployment.

## Final go/no-go decision

**SPRINT 12: PARTIAL. PRODUCTION IMAGE: VERIFIED locally. POSTGRESQL: VERIFIED
locally. WORKER SUPERVISION: VERIFIED locally. FINANCIAL INTEGRITY: PARTIAL.**

**READY FOR DISPOSABLE LOCAL STAGING** with the recorded fixtures/artifacts and
explicit opt-in profile. **NOT READY FOR AUTHORIZED REMOTE STAGING** and **NOT
READY FOR CONTROLLED PRODUCTION DEPLOYMENT**. These are different milestones.

| Mandatory local check | Result |
| --- | --- |
| Actual production image, driver, both bundles | PASS |
| Actual startup/shutdown and local HTTPS/production-cookie auth | PASS |
| Restricted roles, same PG authority, owner checks | PASS |
| Independent bounded workers and new crash generations | PASS |
| Populated cutover dry-run/rerun/rollback and migration recovery | PASS locally |
| SQLite/PG backup/restore and exact references | PASS locally |
| No financial mutation or trading activation | PASS locally |
| Complete reviewable source/image/operator artifact | PASS; not human approved |
| Isolated project volumes, private PG and scoped credentials | PASS |
| Final requested tests | PASS with 15 documented existing conditional exclusions |
| Customer Supabase identity / remote TLS / actual target secrets | BLOCKED / UNKNOWN |
| Representative remote data, durable off-host capacity/restore/soak | BLOCKED / UNKNOWN |
| Complete financial profitability evidence / sustained feeds | PARTIAL / BLOCKED |

The safest next action is independent review of the retained composite artifact
and runbook, followed by a separately explicitly authorized remote staging plan
with durable capacity, scoped customer identity/TLS/credentials, original
representative exports and target migration/restore/recovery monitoring. Do not
activate paper/live strategies or change original economic/risk semantics. No
commit, push, production Compose/deploy script or remote deployment occurred.

## Retained composite review artifact

Review identity:
`release-review:c2c4c32c85119363b0cd73445f9a42f272ce1706b97d7f3e7d0d2012844c0dab`.
Status: **STAGING_CANDIDATE / DISPOSABLE_LOCAL_ONLY**. This is automated local
review, not human approval, production certification or execution authorization.

Archive: `/workspace/nexus-audit/sprint12-local-release-c2c4c32c8511`. It retains
original final JUnit/logs, failed/intermediate build and staging evidence, exact
API/worker image exports, frozen API source/build manifest, the final operator
source bundle, public build input manifest/artifacts, separate runtime-source
equality proof (317 files), contribution/attribution inventory, actual image
private-input absence proof, redacted actual staging/browser/recovery reports,
final documents and an integrity manifest. Private generated `.env`, TLS keys
and browser credentials are excluded from public review evidence.

The exact API image export SHA-256 is
`60179a9d2680cb00084fca1a72299d3f962e6c9637956800605d2eaa6929a415`.
The current code-only worker is
`sha256:5a7b1f4dadf3c9571a5454d5f2d535455397eefb0b5c9bc0d4467874f8392685`.
The original Sprints 11 assessments/33- and 59-artifact archives remain unchanged.
Local retention is not off-host backup, fresh-task survival or upstream base
availability; record those limitations before any remote staging approval.
