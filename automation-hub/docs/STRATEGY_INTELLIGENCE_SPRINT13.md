# Strategy Intelligence Sprint 13

## Executive assessment

**SPRINT 13: PARTIAL. REMOTE STAGING: NOT READY. PRODUCTION: NOT READY.**
This sprint implements local integrity and review controls. Actual VPS capacity,
permissions, customer identity/TLS, durable off-host recovery and authorized
sustained public feeds remain unverified. No remote access, public-feed soak,
deployment, production restart, trading activation, financial import/repair,
Git staging/commit/push/reset/rebase or generic SMC observation occurred.

Implemented: opt-in public finalized-candle distribution and separate cursors;
remote profile/preflight; owner/operator readiness diagnostics; bounded recovery
artifact sealing; duplicate metadata identity preflight; measured local recovery.
Independent review artifacts and final test results are recorded below.

## Repository and release audit

Starting branch `work`, HEAD `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`;
163 existing dirty paths, 161 file hashes and two dependency links preserved in
`sprint13-initial-worktree.json`, with the original tracked diff separately saved.
No old change was attributed to this sprint by filename or guessed ownership.
Read Sprint 11/12, readiness/runbook and inspected root Compose/Dockerfile/deploy
script, existing auth/API, workers, PG/cutover/migrations, backup/spools, retained
datasets, hub/stream/provider/HTF and test architecture.

Sprint 12's exact review identity
`release-review:c2c4c32c85119363b0cd73445f9a42f272ce1706b97d7f3e7d0d2012844c0dab`
was independently checked against **1,948 retained files: zero mismatches**.
All 25 prior contribution hashes matched the current starting tree. Its original
archive was not rewritten. The fresh focused baseline was **71 passed, zero
failed/skipped/warnings in 1.63 seconds**; prior full-suite results were retained
as historical evidence and are not substituted for this sprint's reruns.

The reverified bases are API
`sha256:8c4dc4af8e0a18e000db398027cca5c354271a7976a5bfaa35edff1764562b83`
and worker
`sha256:5a7b1f4dadf3c9571a5454d5f2d535455397eefb0b5c9bc0d4467874f8392685`.
Dependency locks and strategy/financial sources remain unchanged from the
Sprint 13 starting hashes. Both frontend outputs derive from Sprint 12's actual
Node 20 build; local current dashboard typecheck/build are separately rerun.

Local candidate runtime source snapshot:
`b58da117543d7817509f8276aacbfb3520aaf51a84cca3dd5655a1f6ff91c4fb`.
API delta image:
`sha256:3e699a26c3106d67820939efcb9a13363ea2bd20507e99fc584d2d888748226f`.
Worker delta image:
`sha256:d6081c22ac0e1da460d127dafc4e531fe8f221e16df4cabd613645723590cd2d`.
Actual local runtime imported the new modules and binary psycopg; 283 API and
253 worker Python/SQL source files matched the checkout. Base layer chains were
verified. These are local verification deltas on the exact bases, **not** a
clean Dockerfile rebuild, bit-identical reproducibility or release approval.
The final operator/doc inventory is separately sealed after validation.

## Changes and files

Four preexisting dirty files extended:

* `automation-hub/services/shadow_worker_main.py` — complete opt-in read-only feed bindings.
* `automation-hub/services/metadata_cutover.py` — duplicate metadata primary-key detection across frozen inputs.
* `automation-hub/routers/strategy_intelligence.py` — authenticated operator-only remote readiness GET.
* `ops/rehearse-staging.py` — candidate diagnostics, sequential initializers, measured recovery.

Sixteen new files:

* `automation-hub/services/market_data_distribution.py`
* `automation-hub/services/remote_staging.py`
* `automation-hub/services/staging_recovery.py`
* `automation-hub/tests/test_sprint13_market_distribution.py`
* `automation-hub/tests/test_sprint13_remote_readiness.py`
* `automation-hub/tests/test_sprint13_recovery.py`
* `automation-hub/tests/test_sprint13_api.py`
* `automation-hub/tests/test_sprint13_postgres.py`
* `automation-hub/tests/test_sprint13_staging_integration.py`
* `ops/assess-remote-staging.py`
* `ops/public-feed-distribution-soak.py`
* `ops/staging.remote.compose.yaml`
* `ops/staging.remote.nginx.conf`
* `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT13.md`
* `automation-hub/docs/NEXUS_REMOTE_STAGING_PLAN.md`
* `automation-hub/docs/NEXUS_MARKET_DATA_RELIABILITY.md`

No production Compose/Dockerfile/deploy script, dependency declaration/lock,
active financial authority, original ledger or strategy rule is changed by this
sprint. Existing uncommitted changes in those paths remain intact.

## Market-data architecture and evidence

The inspected hub is process-local. A new bounded notice publisher attaches to
that existing public hub; its callback only enqueues, and a separate drain commits
closed validated candles to a purpose-specific SQLite transport. It neither
evaluates a strategy nor calls an execution/financial interface. Existing candle
identity and canonical SHA-256 utilities are reused. Immutable identity/receipt
timestamps, explicit origin, canonical content and transformation revision are
stored; duplicates have one logical effect, conflicting data blocks readers.

Consumers open the journal query-only and retain separate authority-bound,
channel-specific cursors. They validate finality, hashes, native HTF timing,
continuity and freshness; future data cannot change earlier snapshots. Missing,
stale, conflicting or incompatible evidence fails closed. REST history/repair is
context only, never manufactured forward observation. Queues/read windows/storage
are bounded, with no automatic raw deletion. Detailed contract and unresolved
upstream deduplication limitations are in `NEXUS_MARKET_DATA_RELIABILITY.md`.

The shadow CLI can select this adapter without constructing an exchange provider.
Default behavior is preserved; no automatic candidate authorization/start occurs.
Research continues to read frozen historical inputs. No financial authority,
second market-data provider, simulator, strategy engine or job queue is added.
Live public soak is **NOT RUN**; the finite public-only harness defaults to a plan.

## PostgreSQL, cutover and migrations

Existing real-PG tests exercise claims, heartbeats, leases, fencing, cancellation,
commit ACK loss, unique logical results, tenant checks and crash/restore. New
populated fixtures preserve multiple owners, committed validation, cancelled jobs
and recovered expired attempts. An expired-but-active lease blocks export until
the original fixture's recovery has quiesced it. Frozen backup absence blocks
import; duplicate IDs across snapshots are now detected before target mutation.
Original input snapshots, hashes, attempts, candidate/campaign/holdout/shadow
lineage remain preserved by the existing importer; no authority switch occurs.

No new PostgreSQL/financial migration is introduced. Existing worker 0001 then
0002 remains the operator migration order. Runtime roles remain restricted and
cannot migrate. The fresh feed SQLite namespace has explicit identity/candle/
transport tables, immutable candle triggers and scope index; its separate cursor
namespace has cursor and authority-binding tables. These are transport-only
constructor schemas, never applied to a financial or metadata database. Unknown
existing namespaces and incompatible revisions are rejected rather than upgraded
silently. Remote/populated target migration is not certified by local fixtures.

## Backup and failure recovery

Existing SQLite backup API and PG dump/restore preserve consistent original
authorities, schema versions, exact rows and references in disposable fixtures.
The new recovery seal copies only explicitly selected bounded non-financial
artifacts into a new namespace, verifies each original/copy hash, then atomically
names a completed bundle. Interrupted copies remain `.partial`, never complete.
Raw financial/SQLite inputs require their existing separate workflow. There is
no off-host upload, encryption service, power-loss or remote RPO/RTO claim.

Local rehearsal measures PG dump/restore, PG/API restart and worker SIGKILL-to-
ready duration. Both original metadata authority and financial table hashes are
checked after recovery. Worker restarts are bounded; no observation gap is filled
with invented live decisions. RPO and RTO targets remain UNVERIFIED.

Intermediate failures are retained: test-first missing-module collection;
temporary fixture setup errors (method/field names and future receipt boundary);
VFS ENOSPC during PG initialization/API state creation; temporary image-context
directory permissions preventing an import. No test assertion or auth/TLS/strategy
gate was weakened. Validation contexts now explicitly grant read/traverse modes;
one-shot local initializers run sequentially and are removed before runtime startup.
Only identified task-built images/cache branches were reclaimed after ownership/
export verification; all unknown old Docker volumes and prior release archives
were preserved. This host's capacity is not a VPS capacity measurement.

## Authentication and monitoring

GET `/api/v2/strategy-intelligence/remote-staging-readiness` reuses existing owner
authentication and operator RBAC, returns no-store and exposes no write/activation
method. The existing v1 deployment-readiness contract is unchanged. New diagnostics
separate remote authorization/capacity/permissions/customer auth/PG/supervision,
feed, financial, backup freshness, local disk/memory, restart, migration and image
identity. Trusted local diagnostics must match owner and age; missing/foreign/
stale/unverifiable records never become verified remote gates. Raw paths, DSNs,
secrets and another owner's records are not returned. Local self-reports are
explicitly not independent remote measurement or production certification.

The remote profile reuses supervised, bounded, read-only workers and isolated
networks/volumes. Preflight rejects shared volumes, wrong project names, mutable
images, public DB/API ports, unsafe worker bindings and enabled execution. Remote
Supabase auth has emergency admin disabled; actual customer JWT and target TLS
remain mandatory unverified gates. Ingress read-only restrictions do not claim
that the full API runtime lacks execution code. Worker capability/volume/credential
boundaries and original fail-closed controls remain unchanged.

## Final validation and independent artifacts

Final commands, exact counts, recovery timings, contribution hashes, release
manifest and immutable local archive are recorded below after completed validation.
Overlapping test suites must not be summed. Unrun remote/public-feed tests remain
distinct from local deterministic, real-PG and actual-image/browser checks.

## Remaining gates and next action

IMPLEMENTED: local transport/optional shadow adapter, profile/preflight, bounded
recovery seals, duplicate cutover checks, authenticated diagnostics and docs.
TESTED: exact baseline/archive verification and focused deterministic/real-PG
checks; final comprehensive results below identify what actually completed.
PARTIALLY IMPLEMENTED: operational feed distribution/monitoring, dataset retention
and recovery are locally testable but not proven on an independent remote target.
BLOCKED: remote access/deployment, public-feed soak and actual customer identity/
TLS need separate explicit authorization and bindings. Clean release rebuild,
durable capacity/off-host restore, representative target cutover and operational
restart/power-loss/monitoring soak remain release gates.
NOT IMPLEMENTED: new strategies, trading activation, automatic promotion,
financial repair or invented profitability; all are outside authorized scope.

Financial integrity remains PARTIAL: original funding/exposure, historic owner/
configuration/cohort and cash/equity completeness and native Trading Instance
account/funding authority gaps are unchanged. XRP 22 trades/40.9%/PF 1.13/+$0.84
remains UNVERIFIED. Unknown costs are not zero. The safest next action is
independent review of the complete local evidence and offline remote plan; obtain
separate authorization only for specifically defined target assessment or bounded
public-feed tests. No recommendation here authorizes trading or production.

## Completed final validation — 2026-10-13 UTC

All implementation code changes preceded these completed final runs. Later edits
only complete reports/source inventories; runtime source hashes are rechecked.

| Suite/check | Passed | Failed/errors | Skipped | Warnings | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Sprint 13 focused, including actual Docker/HTTPS/browser fixture | 57 | 0 | 0 | 0 | 96.62 s |
| Repository root | 3,950 | 0 | 15 | 88 | 733.54 s |
| Automation Hub | 3,420 | 0 | 15 | 89 | 714.32 s |
| Database/recovery/migration/security/financial/resource suite, 42 files | 584 | 0 | 0 | 4 | 353.21 s |
| Actual PostgreSQL integration cases | 33 | 0 | 0 | 0 | Executed within root/Hub/database suites |
| Existing Chromium regressions | 154 | 0 | 0 | 4 | 6.2 min |
| Actual production-bundle HTTPS smoke | 2 scenarios | 0 | 0 | 0 | Included in staging test |

Suites overlap: **do not sum** these counts. The PG subset XML is extracted from
actual executed root cases and explicitly labelled as a subset, not another
independent command. Fifteen root/Hub skips are eight `not a validity scenario`
and seven `only comparable when both approve` risk-parity cases. None is a silently
skipped browser, PG or new Sprint 13 check. Network/VPS/remote-target tests are
NOT RUN and are not represented as passing or skipped pytest cases.

Dashboard typecheck passed. Production build passed in 6.70 seconds with one
existing Vite large-chunk advisory. Browser uses `/usr/bin/chromium`; its four
warnings are existing NO_COLOR/FORCE_COLOR messages. npm upgrade notices are
informational. Real remote ingress `nginx -t` passed with the exact reviewed
profile capability set, read-only root, fixture TLS, no network/public port.
An initial manual syntax probe omitted the profile's DAC_OVERRIDE capability and
correctly failed to read owner-only fixture files; its failure is retained.
`git diff --check` passed. No test assertion was weakened or globally suppressed.

Warnings: root/Hub FastAPI lifespan deprecations 4/4, aiohttp's ignored cleanup
option on Python 3.12.14 80/81, Supabase HTTP-option deprecations 4/4. Database has
four existing Supabase warnings. The differing connector count is existing
fixture/transport frequency, not a new warning class. No new resource leak,
unraisable or unhandled-thread warning was observed.

Commands use the existing `.venv/bin/python -m pytest -q`, a `/tmp` cache and
JUnit outputs. Full/backend/database runs explicitly enable the existing
NEXUS_STAGING_TEST, NEXUS_PG_TEST_TMPFS, NEXUS_PG_WORKER_TEST,
NEXUS_PG_SUPERVISOR_TEST, NEXUS_SHADOW_CONTAINER_TEST, NEXUS_PG_OUTBOX_TEST and
NEXUS_SPRINT11_RESOURCE_TEST opt-ins. Docker suites run serially. Dashboard runs
`npm run typecheck`, `npm run build`, then the existing approved local Playwright
configuration with `PW_CHROMIUM=/usr/bin/chromium`. No remote endpoint, deployment
script or Git mutation is part of these commands.

### Local measurements and failure drill scope

The focused actual candidate rehearsal preserved 26 financial table hashes across
read-only browser/diagnostic calls, recovery and reporting. Both worker generations
restarted exactly once; zero shadow registrations/observations were authorized.
These are disposable initialized app stores, not verified production financial
history or profitability. Populated research cutover fixtures preserve committed
results/cancelled/interrupted attempts, owner/configuration and immutable lineage.

| Actual measurement | Seconds | Scope |
| --- | ---: | --- |
| Research worker SIGKILL to replacement ready | 6.523 | One local fixture generation |
| Shadow worker SIGKILL to replacement ready | 7.259 | Zero observing registrations |
| PostgreSQL restart to common authority ready | 3.403 | Disposable metadata DB |
| Consistent PostgreSQL dump | 0.223 | Small initialized fixture |
| PostgreSQL restore to different DB and authority verification | 0.632 | Disposable restore |
| API restart to HTTP ready | 17.881 | Local production profile |
| Seal/hash verification of actual metadata dump and report | 0.0011 | Local copy; no off-host guarantee |

RPO/RTO targets remain UNVERIFIED. Docker/PG interruption, stale leases, lost
ACK, fencing, spool recovery and memory limits run through the existing actual
PG/resource suites. New transport/recovery tests cover missing/conflicting data,
revision/authority mismatch, interrupted copies, missing backup references,
capacity/backpressure, restart cursors and immutable receipts. Actual VFS disk
exhaustion failures were preserved and resolved using only identified owned
image/cache branches and staged initialization. Remote network/power loss,
real-provider continuity and large representative operational soaks remain NOT RUN.

### Immutable local review and working-tree closure

Review identity:
`release-review:c20dce6c0b9542911125d21537ab354a387ee5f745c12c4c9a7fb486e98ba75f`.
Archive: `/workspace/nexus-audit/sprint13-local-validation-c20dce6c0b95`.

The identity basis binds exact runtime source/images, executed suite XML hashes,
remote profile and unchanged HEAD. The complete final operator/source inventory,
report bytes, source tar, compressed image exports/config identities, runtime
source proofs, release manifests, raw passing and intermediate failed logs/JUnits,
redacted rehearsal reports, recovery seal and preflight/ownership proofs are
individually hashed in the archive. Report completion does not silently change
the earlier runtime-build source identity. The prior Sprint 12 archive remains
separate and is reverified; it is not replaced by this new review.

Final contribution: **20 paths**, four extended previous files and sixteen new
files. Tree: **179 dirty paths**. All other 157 starting file hashes, both
setup dependency links and Git HEAD remain unchanged. No financial or strategy
source changed in this sprint. Known older user/unknown volumes are preserved.
Review assets are locally retained, not off-host disaster recovery or publication.
Reusable setup instructions were appended to the existing cloud draft, preserving
other fields; saved configuration is not published/applied automatically.

### Final decision and operational gates

| Dimension | Result | Evidence/limitation |
| --- | --- | --- |
| Sprint 13 | PARTIAL | Authorized local implementation/validation complete; external mandatory gates remain |
| Disposable local staging | READY | Exact tested candidate/runtime/fixture scope only; limited VFS headroom |
| Authorized remote staging | NOT READY | Access, target resources, bindings and recovery not verified |
| Production deployment | NOT READY | No release/deployment/trading approval |
| PostgreSQL | VERIFIED locally | Real multi-worker/fencing/backup tests; remote target UNKNOWN |
| Market data | PARTIAL | Deterministic integrity tests; live soak NOT RUN |
| Cross-process distribution | PARTIAL | Two real reader processes and optional shadow adapter; independent live operational soak unverified |
| Financial integrity | PARTIAL | Local non-mutation/authority checks; original evidence/cost completeness unresolved |
| Security | PARTIAL | Local RBAC/isolation/capabilities verified; actual remote customer/credentials/TLS unknown |

Remaining mandatory gates: explicit remote/read-only access and separate public
soak authorization; actual VPS CPU/memory/disk/inodes/driver/permissions/bindings;
separate customer Supabase/session credentials and TLS; reviewed clean release
build and supply-chain/release approval; original representative metadata and
retained-file cutover review; durable off-host backup/restore and measured RPO/RTO;
independent supervision/monitoring/incident ownership and resource controls on the
actual target; sustained public feed/cross-process continuity; dataset retention/
reconstruction after expiry; remote restart/network/power-loss validation; and
cohort-specific original funding/exposure/financial completeness. No unknown
historical fact or missing cost is invented. Review this evidence and the offline
plan before requesting a narrowly defined, separately authorized next operation.
