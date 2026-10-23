# NEXUS disposable staging runbook

This is an explicit local rehearsal, not `scripts/deploy.sh`, the production
Compose file or an authorization to access remote staging/VPS. Never merge these
profiles into production or reuse existing financial volumes/credentials.
Current evidence and exact results are in STRATEGY_INTELLIGENCE_SPRINT12.md.

## Prerequisites and artifacts

Use the complete reviewed source bundle, including required untracked files,
actual production API image and separate code-only worker image. Match their
source, lock, migration, build/base references and image digests to the retained
composite release manifest. HEAD alone cannot identify this tree. BUILD_VERIFIED
or STAGING_CANDIDATE is not human release approval.

The API retains Python 3.11 and builds both frontends with Node 20. Its hashed
lock installs the existing worker-postgres extra. Review lock regeneration:

```sh
UV_CACHE_DIR=/tmp/nexus-uv-cache uv pip compile pyproject.toml \
  automation-hub/requirements.txt ops/api-build-requirements.in \
  --extra worker-postgres --python-version 3.11 --generate-hashes \
  -o ops/api-requirements.lock
```

The offline public-input manifest must match every file, with no unlisted,
modified, symlinked or private input. Python wheels target CPython 3.11 Linux
amd64 and verify lock hashes; npm cache objects verify package-lock SRI. Record
and retain those inputs, exact final image exports and both frontend hashes.
A hash without original files is not reconstruction.

Example build-only invocation (owned NEW output directory):

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ops/build-staging-release.py \
  --output /tmp/nexus-s12-reviewed-build \
  --image nexus-api:sprint12-local \
  --verified-build-inputs /tmp/nexus-s12-reviewed-public-inputs \
  --build-network none
```

`--public-build-ca` is optional public CA trust for an online build, mounted
only during BuildKit RUN. Never disable TLS or copy runtime credentials into
build inputs. Record base digests and versions. Fresh artifact availability and
bit-identical rebuilds are not assumed from mutable tags.

Build the existing code-only worker Dockerfile with checksum-verified public
wheels prepared by `ops/prepare-shadow-worker-wheels.py`, the named
`verified_wheels` context and `--network=none`. Its trusted initializer is copied
explicitly; app/webhook/execution packages and financial data are excluded.

Before running, inspect free disk, inode and memory capacity. Docker VFS can use
multiple complete filesystem copies per image/container. Do not prune unknown
images, volumes, caches or user data. Back up proven task artifacts before any
scoped cleanup. RAM-backed cloud test storage is not deployed power-loss safety.

## Services and credential names

The opt-in `ops/staging.local.compose.yaml` provides:

* private PG metadata, with no host port;
* one-shot state and operator migration initialization using the code-only image;
* original API and actual dashboard/landing bundles;
* loopback HTTPS ingress with a newly generated local certificate;
* independently supervised research worker;
* shadow worker only in the explicit `shadow` profile, with no registrations;
* separate app, research, shadow, dataset and PG volumes and bounded logs/probes.

Metadata/workers use an internal network. API/ingress additionally use an ingress
network. The only published service is an ephemeral 127.0.0.1 HTTPS port. This
is not a hard network sandbox for the API; no trading credentials, inactive
execution, no restored active instances and read-only actions remain gates.

Fixture bindings are generated locally. Names only:

| Scope | Names |
| --- | --- |
| Compose identity/artifacts | NEXUS_STAGING_PROJECT, NEXUS_STAGING_API_IMAGE, NEXUS_STAGING_WORKER_IMAGE, NEXUS_STAGING_UID |
| Fixture credentials/paths | NEXUS_STAGING_OPERATOR_PASSWORD, NEXUS_STAGING_RUNTIME_PASSWORD, NEXUS_STAGING_APP_ENV, NEXUS_STAGING_TLS_DIR |
| Explicit process test | NEXUS_STAGING_SHADOW_ENABLED |
| Existing app identity | HUB_AUTH_MODE, HUB_EMERGENCY_ADMIN_ENABLED, HUB_USERNAME, HUB_PASSWORD, HUB_SECRET, HUB_CONTROL_KEY, HUB_WEBHOOK_SECRET |
| Existing execution restrictions | HUB_AUTO_ENGINE, HUB_ENABLE_EXTERNAL_LIVE, HUB_USE_LIVE_DATA, HUB_RESEARCH_AUTOSTART |
| Metadata runtime | NEXUS_RESEARCH_WORKER_BACKEND, NEXUS_SHADOW_WORKER_BACKEND, NEXUS_RESEARCH_WORKER_DSN, NEXUS_SHADOW_WORKER_DSN |
| Health/transport | NEXUS_OPERATIONAL_SPOOL_PATH, NEXUS_RESEARCH_HEALTH_FILE, NEXUS_SHADOW_HEALTH_FILE, NEXUS_SHADOW_SUPERVISED |

Do not inherit root `.env` or the app environment into workers. Operator DSNs
belong only to the one-shot initializer. Runtime roles have no superuser,
createdb/createrole/bypassrls or schema-migration privilege. Multi-owner trusted
workers use application owner checks, not per-tenant PostgreSQL RLS.

## Startup and controlled rehearsal

Default helper execution records an isolated plan. Actual startup requires the
explicit flag below and generates new project-scoped resources/fixture secrets:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python ops/rehearse-staging.py \
  --output /tmp/nexus-s12-reviewed-rehearsal --run-disposable \
  --exercise-shadow --browser-smoke
```

Use a new directory per attempt. `--exercise-shadow` tests the empty observer
process only; it authorizes no candidate or observation. Generic SMC evaluation
stays blocked. Never import production desired-running intent into this profile.

The helper rejects remote Docker endpoints and non-disposable image namespaces.
It validates the profile, creates scoped volumes/permissions, starts private PG,
applies worker metadata 0001 then 0002, verifies the same runtime authority and
starts API/workers followed by HTTPS ingress. The existing financial constructor
uses freshly isolated original SQLite authorities, never a replacement ledger.
No automatic backend authority switch occurs.

Production auth runs without test bypass. Local emergency legacy mode and fresh
credentials exercise the existing production Secure-cookie boundary. The HTTPS
client validates the generated SAN certificate; Chromium trusts only the fixture
SPKI and uses `/usr/bin/chromium`. This does not verify customer Supabase JWT,
remote domain TLS or authorize permanent emergency-admin deployment.

The helper checks liveness, anonymous 401, actual public config without private
bindings, both browser-rendered bundles, operator readiness, restricted roles,
new generations after internal child crashes, PG/API recovery, consistent
backup/restore and financial-table hash equality. No trading UI action is invoked.
Diagnostics redact fixture secrets. Do not archive private generated `.env`,
certificate keys or browser credentials as public release evidence.

## Health contract

`/health` proves HTTP liveness only. The authenticated operator endpoint
`/api/v2/strategy-intelligence/deployment-readiness` separates nine checks:
HTTP_LIVE, API_READY, AUTH_READY, POSTGRES_READY, RESEARCH_WORKER_READY,
SHADOW_WORKER_READY, FEED_READY, FINANCIAL_EVIDENCE_READY,
OPERATIONAL_SPOOL_READY. Each exposes READY/DEGRADED/BLOCKED/UNKNOWN, timestamp,
source, authority, reason and optional evidence reference without credentials.

PG authorities must agree and be accessible under the actual restricted role.
A configured failure never falls back to SQLite. Worker heartbeats must be
fresh, authority-compatible and execution-free. Self-reported process readiness
is labelled; actual PID/generation probes run in the worker namespace. Do not
infer continuous uptime or market freshness from either. Generic financial
readiness remains UNKNOWN without a cohort-specific committed reconciliation;
feed remains BLOCKED without sustained/cross-process proof. A healthy local API
never authorizes paper/live execution or promotion.

## Backups, cutover and restore

Before any separately authorized real migration, quiesce producers/claims and
preserve original PA/SMC/general account authorities, Instance ledger, original
journals/decisions, settings/sessions, research/shadow metadata, operational
spools, exact retained datasets/archives and matching image/source manifests.
Never copy a live SQLite main file without its WAL consistency boundary.

The tested staging helper uses SQLite's backup API, integrity/FK/schema/count
and original-row hashes, and restore to a NEW directory. Its disposable marker
is mandatory. PG uses actual pg_dump/pg_restore into a DIFFERENT disposable DB
and verifies original authority. No original authority or financial row is
rewritten. Off-host/production-sized backup and power-loss verification remain
unverified. Restore a dataset's exact revision or mark it unavailable.

For metadata cutover, reuse `services.metadata_cutover`: freeze one/two source
snapshots, reject active leases/unsupported populated schemas/owner conflicts,
verify immutable configuration, dataset and result references, and use default
transaction-rollback dry-run. Only explicit local fixture import is tested.
Never call a fresh empty import a successful historical cutover. Exact retained
files must accompany their metadata references. Remote import/authority switch
requires separate review and authorization.

## Failure handling and rollback

* Preserve original failure logs and snapshot identities. Freeze submissions and
  quiesce claimants before changing any authority.
* A process crash is injected inside the container; Docker's manual kill is a
  stop request and deliberately disables restart policy. Verify bounded restart
  count, new generation, fresh heartbeat and unchanged authority before reuse.
* Keep additive schemas. Never drop financial/evidence tables or overwrite new
  PG commits with an old SQLite snapshot to roll back software.
* Recover expired leases through existing fencing/reconciliation. Lost ACKs read
  committed results; they do not create a second logical outcome.
* Replay original spooled events idempotently and quarantine conflict/corruption.
  Unobserved time remains UNKNOWN. No synthetic financial transaction repairs it.
* Restore reviewed consistent backups and exact retained files to a new target;
  compare hashes, rows, schema and result IDs before any approved switch.
* The helper tears down only its unique disposable project and volumes. Never
  invoke root production Compose, `scripts/deploy.sh`, broad prune or destructive
  Git operations. No real staging/production rollback was exercised.

## Go/no-go milestones

READY FOR DISPOSABLE LOCAL STAGING requires original passing image, HTTPS/auth,
private metadata, supervision, migrations/cutover/restore, owner checks,
non-mutation and final-suite evidence plus a complete reviewed artifact.

READY FOR AUTHORIZED REMOTE STAGING additionally requires explicit authorization,
durable capacity/permissions/backups, target identity/TLS/secrets, representative
exports, retained data and restart/restore/recovery validation on that target.

READY FOR CONTROLLED PRODUCTION DEPLOYMENT additionally requires independently
verified mandatory operational gates, sustained/cross-process feed evidence,
financial completeness where performance is claimed, monitoring/incident policy
and explicit release approval. None follows automatically from local tests.

Keep live/paper activation, strategy switching, champion promotion and generic
SMC shadow observation disabled. Passing deployment checks do not prove any
strategy profitable. XRP performance remains UNVERIFIED.

Final local evidence is retained at
`/workspace/nexus-audit/sprint12-local-release-c2c4c32c8511`. The review identity
is `release-review:c2c4c32c85119363b0cd73445f9a42f272ce1706b97d7f3e7d0d2012844c0dab`.
The recorded milestone is READY FOR DISPOSABLE LOCAL STAGING; remote staging
and controlled production remain NOT READY and require separate authorization.
