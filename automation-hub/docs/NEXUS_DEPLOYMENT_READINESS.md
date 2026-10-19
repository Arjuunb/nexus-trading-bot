# NEXUS deployment readiness

**Sprint 12 update:** packaging and disposable local staging have now been
implemented and exercised, including the actual Python 3.11/Node 20 image,
HTTPS/authentication, real private PostgreSQL, independent workers and restore.
The final local go/no-go follows the recorded final suites in
[STRATEGY_INTELLIGENCE_SPRINT12.md](STRATEGY_INTELLIGENCE_SPRINT12.md).
**Remote staging and production remain NOT READY / unauthorized.** No VPS,
remote staging or production service was accessed or deployed. The earlier
Sprint 11 decision remains historical evidence in its immutable archive.

## Actual deployment architecture

The existing root `compose.yaml` defines three services:

| Service | Existing purpose | Verification boundary |
| --- | --- | --- |
| `app` | FastAPI plus dashboard and landing bundles; persistent `tradexa-data` volume | Local source startup and isolated APIs are tested; the final production image is not certified. |
| `nginx` | Private app upstream, public HTTP/HTTPS, certificate reload | Source reviewed; actual VPS ingress/TLS is unverified. |
| `certbot` | Certificate renewal and shared certificate/webroot volumes | Source reviewed; no certificate request or renewal was run. |

The root production profile does **not** define PostgreSQL, a research worker or
a standalone shadow worker. `ops/research-workers.local.compose.yaml` and
`ops/shadow-worker.local.compose.yaml` are disposable local validation profiles,
not an installed production deployment. Do not merge or launch them against
existing financial volumes.

The main Dockerfile builds two SPAs with Node 20 and runs Python 3.11. Sprint 12
actually built with Node 20.20.2/Python 3.11.17 and exercised the real image's
startup/shutdown, authentication and browser bundles in disposable local staging.
Cloud unit suites still use Python 3.12.14/Node 24 and are reported separately.
This verifies the local artifact, not an approved deployed production profile.
The Docker build context now excludes dotenv files, the local virtualenv and
`automation-hub/logs`; a real scratch-image context test verifies their absence.
The landing already reads the API-injected public auth configuration before
its optional local build-time fallback, so private dotenv files need not be
baked into an image. Existing secret files were not read, rewritten or deleted.

The main Python image now installs hash-verified Python 3.11 dependencies and
the existing `worker-postgres` optional extra. Actual binary psycopg import,
`pip check` and restricted PostgreSQL initialization passed in the API image.
The complete source/public-input/build manifests identify the dirty-tree build;
this does not grant release approval or silently supply deployed infrastructure.

The new opt-in `ops/staging.local.compose.yaml` and trusted operator helpers
are separate from production Compose. They create unique disposable resources,
fresh fixture credentials, private metadata and loopback HTTPS. Details and
rollback boundaries are in [NEXUS_STAGING_RUNBOOK.md](NEXUS_STAGING_RUNBOOK.md).
The shadow process is disabled by default and its explicit test has zero
registrations. No production configuration or original financial volume changes.

### Required services for an eventual approved profile

* The existing API and both frontend bundles behind the existing authenticated
  HTTPS ingress.
* The independently verified original financial authorities: native SQLite
  accounts and the configured original ledger/Supabase identity services.
* A separate PostgreSQL **research/shadow metadata** authority when that backend
  is selected, with a narrowly privileged runtime role and distinct operator role.
* A separately supervised research worker, with bounded concurrency and a
  durable spool/health mount.
* A separately supervised shadow observer only after explicit registration and
  observation authorization. Keep generic SMC shadow evaluation blocked.
* Durable dataset snapshots, telemetry retention, backups and external monitoring.
  Live cross-process market-data distribution still needs a reviewed design and
  authorized feed-soak validation.

No service in the research profile receives order clients, writable financial
volumes, application-control credentials or exchange credentials. Computational
jobs must use the existing simulator and queue. A missing driver, invalid role,
wrong authority marker or unavailable configured primary must fail closed.

## Database dependencies and boundaries

| Authority | Current source/configuration | Required treatment |
| --- | --- | --- |
| Native PA financial account | `HUB_PRICE_ACTION_PAPER_DB` | Preserve original account, orders, fills, funding and source receipts. |
| Native SMC financial account | `HUB_SMC_PAPER_DB` | Physically separate from PA and all other paper accounts. |
| General native paper account | `HUB_PAPER_BROKER_V2_DB` | Original financial authority; no research writes. |
| Trading Instance ledger | `HUB_LEDGER_PATH` or configured original Supabase ledger | Preserve its original atomic ledger/outbox and ownership; no replacement account authority. |
| Original decision/evidence journal | `HUB_JOURNAL_DB`, `HUB_DECISIONS_DB` and related stores | Preserve history, original configuration references and report lineage. |
| Research/shadow metadata | Explicit `NEXUS_*_WORKER_BACKEND` and DSNs | Producers, workers, candidate registry and validation consumers must use the same approved authority. |
| Telemetry transport | `NEXUS_OPERATIONAL_SPOOL_PATH` | Separate SQLite transport namespace on durable storage; not a financial ledger. |
| Retained inputs/archive files | Existing dataset/operational retention stores | Exact immutable content, hashes, manifests and references must be copied and verified together. |

PostgreSQL worker roles are not superusers, cannot create roles/databases or
migrate schemas, and must not have BYPASSRLS or dangerous predefined-role
memberships. The private metadata schema has an authority marker. Trusted
workers serve multiple owners; API/store owner checks enforce the application
tenant boundary. Do not represent that as per-worker PostgreSQL tenant RLS.
Identity migrations and original financial migrations have different authorities.

## Environment requirements — names only

No credential values belong in this document, logs, source control or worker
command lines. Preserve existing risk and strategy settings without modification.

| Scope | Names | Requirement |
| --- | --- | --- |
| App lifecycle/storage | `HUB_ENV`, `HUB_DATA_DIR`, `PORT` | Reviewed environment and durable app state location. |
| App identity | `HUB_AUTH_MODE`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | Use only the documented app/identity consumers; verify actual customer authentication and the original configured ledger. |
| App control/session | `HUB_CONTROL_KEY`, `HUB_WEBHOOK_SECRET`, `HUB_SECRET`, `HUB_EMERGENCY_ADMIN_ENABLED`, `HUB_USERNAME`, `HUB_PASSWORD` | Distinct scoped credentials; emergency legacy access is not normal customer authentication. |
| Trading restrictions | `HUB_AUTO_ENGINE`, `HUB_ENABLE_EXTERNAL_LIVE`, `HUB_RESEARCH_AUTOSTART`, `HUB_USE_LIVE_DATA`, `HUB_CORE_V2_MODE` | Preserve fail-closed restrictions; this task authorizes no execution or observation activation. |
| Original state paths | `HUB_DB_PATH`, `HUB_LEDGER_PATH`, `HUB_JOURNAL_DB`, `HUB_DECISIONS_DB`, `HUB_ACCOUNT_DB`, `HUB_CYCLES_DB`, `HUB_TRADE_MEMORY_DB`, `HUB_ORDER_STATE_DB`, `HUB_SETTINGS_PATH`, `HUB_PROVIDERS_PATH` | Verify every override and persistent mount before any startup; never point a rehearsal at the live paths. |
| Native accounts/research | `HUB_PAPER_BROKER_V2_DB`, `HUB_PRICE_ACTION_PAPER_DB`, `HUB_SMC_PAPER_DB`, `HUB_PRICE_ACTION_RESEARCH_DB`, `HUB_SHADOW_RESEARCH_DB` | Preserve account/session/tenant separation. |
| Market inputs | `HUB_MARKET_DB`, `HUB_MARKET_DATA_DIR`, `HUB_MARKET_DATA_V2` | Preserve provider/venue/revision identity and closed-candle semantics. |
| PostgreSQL metadata | `NEXUS_RESEARCH_WORKER_BACKEND`, `NEXUS_SHADOW_WORKER_BACKEND`, `NEXUS_RESEARCH_WORKER_DSN`, `NEXUS_SHADOW_WORKER_DSN` | Explicit selection, matching authority and restricted runtime role; no silent SQLite fallback. |
| Worker spool | `NEXUS_OPERATIONAL_SPOOL_PATH` | Writable durable bounded transport mount, not a financial DB or ephemeral rootfs. |
| Local Compose only | `NEXUS_RESEARCH_LOCAL_DSN`, `NEXUS_RESEARCH_LOCAL_DATA`, `NEXUS_RESEARCH_LOCAL_NETWORK`, `NEXUS_RESEARCH_WORKER_IMAGE`, `NEXUS_RESEARCH_UID`, `NEXUS_RESEARCH_GID` | Disposable local resources; these are not production deployment bindings. |
| SQLite shadow local profile | `NEXUS_SHADOW_CANDIDATE_DB`, `NEXUS_SHADOW_LOCAL_DATA`, `NEXUS_SHADOW_WORKER_IMAGE`, `NEXUS_SHADOW_WHEELHOUSE`, `NEXUS_SHADOW_UID`, `NEXUS_SHADOW_GID` | Candidate input is read-only; observation output is separate. |
| Frontend | `VITE_API_BASE`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_APP_URL`, `DASHBOARD_BASE` | Build-time public configuration only; never embed server/service-role/exchange secrets. |
| Build provenance | `GIT_COMMIT`, `GIT_BRANCH`, `DEPLOYED_AT` | Must identify the approved full artifact; HEAD alone does not identify this dirty tree. |
| Trusted local cutover | `NEXUS_LOCAL_CUTOVER_OPERATOR_DSN` | Operator-only disposable loopback target; never pass the operator role to runtime workers. |

Standalone workers reject the known venue, original-ledger, app exchange,
application-control, webhook, session, AI and notification credential names.
They must still receive a deliberately allowlisted environment. A denylist is
not proof that an arbitrary unknown environment variable cannot contain a
secret. Do not inherit the app's `.env` or copy all app bindings into a worker.

## Required backups

Before an authorized migration/cutover, quiesce producers and take consistent
backups of **all** original financial stores, identity/ledger authorities,
decision journals, instance/lab/session settings, operational metadata/spools,
and exact dataset/archive files. Keep the approved application/worker images,
schema/authority markers and full source/build manifest.

Use SQLite's backup API or the tested snapshot mechanism, not a naked copy of
an actively written database without its WAL. Use a PostgreSQL backup/restore
procedure with restricted operator access. Retain restore hashes, row counts,
foreign-key checks and original report/result IDs. Source snapshots and retained
files have independent integrity checks; a manifest hash cannot reconstruct
missing candles or an unavailable telemetry archive.

Disposable local SQLite/PG backup, restore, migration interruption and import
rollback are tested. Off-host backup, production-sized restore, storage
durability, recovery objectives and actual VPS permissions are **not verified**.
Never use an empty fresh database as evidence of a successful historical cutover.

## Migration order

1. Back up and record the existing authorities, software/schema versions and
   retained-file hashes. Stop producers/claimants before any real cutover.
2. On the separately approved identity target, apply `data.pg_migrations.apply`
   using its own migration discovery. Do not feed it nested research/shadow or
   financial SQLite DDL. Legacy `0002_shadow_research.sql` is excluded.
3. For the original financial authority, follow its existing migration/outbox
   process. Native `migrations/financial/0001_native_paper_evidence.sql` is
   additive SQLite DDL installed by the existing broker. Preserve prior records;
   capture on existing accounts begins with historical UNKNOWN, not a fabricated
   original opening.
4. For PostgreSQL research/shadow metadata, run the existing trusted operator
   `migrate_worker_database` entrypoint on the isolated metadata target. It
   installs `workers/0001_postgresql_worker_metadata.sql`, then
   `workers/0002_operational_resilience.sql`, validates the marker/runtime role
   and applies grants. Runtime workers cannot migrate.
5. If SQLite metadata remains selected, use its existing compatible constructor
   migrations: research journal context and shadow migrations 0003–0008.
   Do not execute SQLite DDL on PostgreSQL. There are no new migrations in the
   independent verification follow-up.
6. Before switching an authority, use the existing `services.metadata_cutover`
   frozen export and default dry-run import. Preflight must reject active leases,
   unsupported populated tables, missing immutable references, owner conflicts
   and changed dataset/configuration hashes. Verify the full retained files too.
7. An actual import/authority switch requires separate explicit authorization,
   reviewed export and target backups. Local `--apply-disposable` tests do not
   authorize a staging/production import. No automatic backend switch occurs.

Migrations are additive/forward compatible. A prior application version can
retain added tables; dropping those tables loses evidence and is not a rollback.
No financial record is deleted or rewritten to make reconciliation pass.

## Startup order and health checks

Only after the go/no-go gates and separate authorization:

1. Verify persistent volumes, ownership, free space, restored snapshots and
   exact image/software identity while all execution/observation intent is
   explicitly reviewed and inactive for the rehearsal.
2. Start the isolated metadata database, apply operator migrations, verify the
   authority marker and connect with the restricted runtime role.
3. Validate producer/API bindings against the same selected authority. Start
   the existing API with the reviewed authentication profile and execution
   restrictions retained; then verify ingress and both frontend bundles.
4. Start a separately supervised research worker with one bounded claimant and
   its own health/spool mount. Verify actual role, lease, heartbeat, quotas and
   recovery before any approved fixture job.
5. Start shadow observation only after a separately approved feed topology,
   dataset/lookback/freshness review and explicit candidate registration/start.
   An authorized research result does not authorize shadow or paper execution.

The root `/health` status proves HTTP liveness, **not** complete financial,
PostgreSQL, feed or execution readiness. Inspect persistence and configured
primary connection status as well as API authorization and tenant boundaries.
`/version` must match the approved full build, not just an old Git HEAD.

Worker probes use `python -m services.shadow_worker_health` with the trusted
health-file argument. Require fresh timestamps, matching PID/start generation,
the expected backend/authority, `execution_runtime_loaded` false, database and
candidate-store availability, and a healthy bounded spool. New probes expose
`database_verified`; a legacy v1 process-only record can retain its old process
contract while reporting database UNKNOWN. That is not a verified metadata gate.
An explicitly unavailable/unknown observation database must reject readiness.

Use the authenticated operational-resilience, reliability, native lineage,
reconciliation and local-readiness routes for evidence diagnostics. A sampled
probe does not establish continuous uptime. Missing live intervals stay UNKNOWN;
no historical replay is relabelled as forward observation.

## Rollback and incident recovery

* Stop new submissions and quiesce workers. Record current attempts, lease
  generations, cancellations, committed result references and pending spools.
* Retain additive schema and original financial/evidence records. Revert only
  to an explicitly approved compatible artifact; do not drop evidence tables,
  rewrite balances or backfill unknown historical configurations/funding.
* If a metadata authority has not switched, keep the original authority and
  frozen backups. The tested dry-run leaves the target unchanged.
* After a switch, reconcile all target commits, attempts, cancellations and
  retained inputs before any decision to return to the old authority. An old
  SQLite snapshot cannot safely replace newly committed PostgreSQL work.
* Recover expired leases through the existing bounded mechanism. Stale workers
  remain fenced. A committed result with a lost ACK is read and reconciled;
  it is not executed or inserted again as a new logical result.
* Reopen and replay the original durable spool at least once into its idempotent
  destination. Quarantine conflicts/corruption; never invent a recovery event
  or claim unmonitored time was healthy. Preserve pinned raw evidence.
* Restore exact retained input/archive revisions or mark them unavailable.
  Do not substitute newer provider data or reset holdout exposure history.
* Require explicit human review after recovery. Restoring a database or image
  authorizes neither observation nor paper/live trading.

The existing `scripts/deploy.sh` actually deploys services and can fetch Git
metadata. It was **read but not executed**. It refuses tracked dirty state;
the full release review must also include untracked files. This task must not
use its revision-check bypass, Git staging/commit/push/reset or Compose startup
against the root production profile. No production rollback was exercised.

## Production risks and go/no-go checklist

| Requirement | Current status | Required next evidence |
| --- | --- | --- |
| Source application startup | VERIFIED locally | Actual Python 3.11 image and production-cookie HTTPS lifecycle passed; approved remote profile still required. |
| Customer authentication/HTTPS | PARTIAL | Local auth/tenant regressions and fail-closed checks; actual Supabase, ingress and TLS unverified. |
| PostgreSQL claims/leases/fencing | VERIFIED locally | Disposable real-PG tests; approved persistent deployment/restart soak remains. |
| Standalone supervision/resource controls | VERIFIED locally | Real local cgroups/restarts; no installed VPS supervisor or live-feed certification. |
| Production API PostgreSQL driver/build | VERIFIED locally | Actual Python 3.11/Node 20 image, hashed dependencies/import/connection/startup verified; release/target approval still required. |
| Historical authority cutover | BLOCKED | Representative authorized exports, retained-file copies and reviewed authority switch. |
| Native verified financial performance | PARTIAL | Complete original funding schedule/exposure, ownership/cohort/journal/equity and paper monitoring. |
| Sustained live feed/cross-process distribution | BLOCKED | Current public-feed soak is unverified; approved egress/topology and bounded soak required. |
| Telemetry raw reclamation | PARTIAL | Report-pinned retention/expiry and operator policy; no automatic raw deletion. |
| Durable capacity/off-host recovery | BLOCKED | Durable storage, approved backup/restore and capacity drills; cloud VFS headroom is insufficient certification. |
| Credential separation/tenant authorization | PARTIAL | Local guards verified; actual deployed credential bindings, volume permissions and identity services unverified. |
| Complete release artifact | BLOCKED | Dirty tracked/untracked work requires independent review and separately authorized release preparation. |

Go only when every mandatory gate has original evidence and the reviewer
explicitly authorizes the target rehearsal. Keep live trading, new paper
activation, automatic promotion and generic SMC shadow execution disabled.
Do not call a strategy profitable from signal agreement, fixture P&L, a hash
or a passing coverage threshold. XRP performance remains UNVERIFIED.

The safest next step is to review the complete Sprint 12 composite artifact and
local evidence. Any remote rehearsal still needs separate authorization, durable
capacity, distinct scoped customer identity/metadata credentials, original
representative exports and target backup/restore/recovery verification. Financial
completeness, sustained feeds and execution authorization remain separate gates;
this document performs none of those external actions.
