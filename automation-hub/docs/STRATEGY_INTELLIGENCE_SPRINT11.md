# Strategy Intelligence Sprint 11

## Executive summary

**IMPLEMENTED:** original native paper transaction evidence, frozen order
provenance, original position/episode allocations, signed cash receipts,
funding deduplication in the financial transaction, source-timed equity points,
read-only normalization into the existing reconciliation calculator, explicit
human review, bounded durable telemetry buffering/replay, operator probes,
retained telemetry copies/aggregates, local PostgreSQL cutover rehearsal,
immutable local readiness assessments, authenticated APIs and dashboard views.

**TESTED:** isolated original broker accounting, partial exits, scale-in,
reversal quantity/risk allocations, funding failure/restart, unknown historical
evidence, metadata outage/restart, duplicate replay, corruption/capacity limits,
real PostgreSQL import/rollback/fencing/recovery, kernel CPU throttling, actual
tmpfs exhaustion, container OOM and spool recovery. Exact final results appear
in the validation section below.

**PARTIAL:** complete native profitability qualification. Future transactions
preserve their original facts, but the existing lab funding pollers cannot prove
complete settlement schedules and historical exposure. Account history preceding
capture, multiple cohorts/open episodes, missing original configuration,
continuous equity history and exact paper operational coverage remain gates.
Trading Instances retain their existing atomic outbox, not a newly invented
account/funding authority. No historical evidence is repaired.

**BLOCKED:** sustained public-feed validation, cross-process feed distribution,
automatic raw telemetry reclamation, actual staging/production cutover and
deployment certification. Sprint 11 is a locally validated contribution, **not
complete production certification or a claim that every acceptance gate passes**.
No staging/production access, trading activation, Git staging/commit/push/reset,
deployment or champion promotion occurred. XRP profitability remains UNVERIFIED.

**Independent verification closure:** a second audit preserved the earlier
implementation, repaired shadow metadata-readiness checks, extended the known
credential guard for both standalone workers, and prevented local dotenv,
virtualenv and financial-state files entering Docker build contexts. It added
25 regression cases and the operator runbook
[NEXUS_DEPLOYMENT_READINESS.md](NEXUS_DEPLOYMENT_READINESS.md). Sprint 11 remains
**PARTIAL** and the deployment decision is **NOT READY FOR STAGING**. These local
checks do not certify an existing VPS or authorize a release.

## Repository audit and working-tree safety

Audit HEAD remains `7dd5e9a`. Before editing, `git status --porcelain=v1 -uall`
contained **117 dirty paths**. File SHA-256 hashes, the full status and the tracked
diff were retained as `/tmp/sprint11-initial-worktree.json`,
`/tmp/sprint11-initial-status.txt` and `/tmp/sprint11-initial-tracked.diff`.
Earlier Sprint 10 figures were treated as historical reports.

Read Sprint 10, 9, 8.5, 8, 7, 6 and production-validation reports and inspected
the actual native brokers/labs, Instance ledger/outbox, decision captures,
reconciliation, worker metadata/leases, operational persistence, dashboard and
test infrastructure. The contribution inventory is separate from prior work.
Final counts and path inventory are recorded below, after validation.

Current pre-change baseline: **111 passed, 0 failed, 2 skipped, 4 warnings,
37.57 seconds**. It included broker/receipt/outbox/reconciliation/reader/
operational/PostgreSQL tests. The two skips required container-supervision
opt-ins. The warnings were existing Supabase HTTP option deprecations.

The independent follow-up began with **142 dirty paths**, including the two
dependency links. Its original status, HEAD and 140 file hashes are retained in
`sprint11-verification-initial.json`. The retained runtime/test sources matched
the earlier Sprint 11 identity before edits. A fresh baseline executed **48
focused tests: 48 passed, 0 failed, 0 skipped, 0 warnings, 30.52 seconds**.
The audit also inspected the complete original Sprint 11 prompt, Sprint 10,
the root Dockerfile/Compose profile, deployment script, authentication, both
frontend build inputs and worker health/credential boundaries. Final inventory
and rerun results below supersede the earlier local counts without deleting
their evidence.

### Independent reliability and packaging findings

1. `shadow_worker_health.check_health` previously checked the candidate reader
   but could accept an explicitly unavailable observation database. Current
   publishers probe both. Readiness now rejects an explicit unavailable,
   unknown or null database and exposes `database_status`/`database_verified`.
   A legacy v1 record without that field retains its old process-only contract
   with database UNKNOWN; it cannot prove metadata readiness. PID generation,
   age, candidate availability and execution-runtime checks remain enforced.
2. The shared standalone-worker guard covered venue aliases but omitted the
   actual app exchange, control, webhook, session, service-role, AI and
   notification credential names. Both builders now reject those names before
   opening storage. An allowlisted supervisor environment is still required;
   a known-name denylist does not prove arbitrary environment secrecy.
3. Root `scripts/deploy.sh` requires `.env`, while Docker uses `COPY . .` and
   the old ignore rules did not exclude root/hub/landing dotenv files, the local
   virtualenv or `automation-hub/logs`. An actual scratch image using the old
   rules copied harmless private-state sentinels. Additive `.dockerignore`
   rules and an actual build/export regression now verify their exclusion and
   retention of ordinary source. No real secret files were inspected or deleted.
   The landing consumes API-injected public identity configuration, so this
   does not require embedding private dotenv in its build.

The main app image still omits the optional PostgreSQL driver and uses Python
3.11/Node 20 rather than this cloud's Python 3.12.14/Node 24. Main-image packaging
and production-profile startup therefore remain explicit rehearsal blockers.
No deployment configuration, financial writer or strategy rule was changed in
this independent follow-up. The migration README now labels its old SQLite-only
statement as historical; no additional migration was created.

### Where information was lost

* `PaperBrokerV2.submit` persisted an order before the lab's separate metadata
  connection wrote configuration/decision links. The original financial order
  did not freeze those identities.
* `_fill` persisted prices, fees and realized P&L, but not the before/after
  position relationship, episode allocations, effective configuration or a
  signed original cash receipt. Protective instructions intentionally have no
  extra `v2_orders` row.
* `apply_funding_once` previously wrote lab deduplication after the broker's
  funding commit. A crash between them could lose deduplication. The provider
  settlement timestamp also did not accompany the original booked transaction.
* `account()` can persist peak/drawdown scalars. Those scalars are not complete
  equity history and are never used as original period valuation evidence.
* The original Trading Instance outbox already atomically captures execution
  IDs, costs and parent/continuation links. It has no independent complete
  funding schedule, cash-account or mark/equity authority to copy.
* Operational failures were counted in process memory; total metadata outages
  had no durable spool. Monitoring covered attempts/registrations rather than
  idle workers. Raw immutable telemetry had finite capacity without reclamation.

## Financial source matrix

AUTHORITATIVE means an original producer fact is retained, not that the entire
source is complete. PARTIAL means a contemporaneous fact/reference exists but
mandatory coverage is missing. UNKNOWN never means zero. Future and historical
rows are deliberately distinguished.

| Original field | PA Lab future capture | SMC Lab future capture | Trading Instances existing source |
| --- | --- | --- | --- |
| Tenant | AUTHORITATIVE when explicit owner at original account creation; otherwise UNKNOWN historical ownership | Same | PARTIAL: original scoped context, no independent account authority |
| Paper account | AUTHORITATIVE `v2_account.account_id` | Same isolated SMC account | PARTIAL original context |
| Strategy/version | AUTHORITATIVE captured decision/order labels | AUTHORITATIVE canonical strategy plus original model label | AUTHORITATIVE where original outbox context contains them |
| Effective fingerprint | AUTHORITATIVE only from complete original decision snapshot matching execution config | Same; model alias must match frozen execution model | PARTIAL: existing immutable original context, no latest-settings backfill |
| Execution session | AUTHORITATIVE original lab session at order creation | Same | AUTHORITATIVE where original session receipt exists |
| Order intent/decision | AUTHORITATIVE original contemporaneous references when supplied; manual missing reference stays UNKNOWN | Same | PARTIAL original alert/decision/outbox references |
| Order acknowledgement/cancellation | AUTHORITATIVE existing broker acceptance/cancel boundary, paper only | Same | PARTIAL original execution receipt; not a complete order authority |
| Entry/exit/partial fill | AUTHORITATIVE `v2_fills` plus transaction receipt | Same | AUTHORITATIVE committed OPEN/REDUCE/CLOSE receipts |
| Quantity/price | AUTHORITATIVE booked values, unchanged | Same | AUTHORITATIVE original receipt arithmetic |
| Fees | AUTHORITATIVE separately booked commission | Same | AUTHORITATIVE booked close fees, original entry-fee timing preserved |
| Funding charge | AUTHORITATIVE booked amount/rate/mark/actual quantity and provider reference | Same | MISSING complete original funding authority |
| Funding schedule/exposure coverage | PARTIAL; reviewed original complete-range hook exists, poller is incomplete | Same | UNKNOWN, not inferred |
| Spread/slippage | AUTHORITATIVE original reference price and explicit model components embedded in price | Same | PARTIAL original price/fee receipt, full cost assumptions not invented |
| Position lifecycle/episode | AUTHORITATIVE original position IDs and fill allocations for future captures | Same, including existing scale-out | PARTIAL original parent/continuation receipts and evidence journal lineage |
| Realized P&L | AUTHORITATIVE unchanged financial fill value | Same | AUTHORITATIVE original accounting transaction |
| Cash movements | AUTHORITATIVE original opening/capture/reset and signed fill/funding receipts | Same | PARTIAL derived balance model, no new cash-account journal invented |
| Cash balance | AUTHORITATIVE persisted native balance | Same | PARTIAL original engine/ledger aggregate, not native account authority |
| Equity observations | PARTIAL: original source-timed marks; stale/missing marks stay UNKNOWN | Same | MISSING complete original valuation series |
| Paper operational interval | UNKNOWN unless exact account/config/session monitoring exists | Same | UNKNOWN unless independently captured |
| Historical completeness | PARTIAL / UNVERIFIABLE / UNKNOWN as original fields permit | Same | Existing original outbox remains unchanged; funding/equity still UNKNOWN |

No other supported native financial authority was found. Research/qualification,
shadow decisions and hypothetical outcomes are not adopted as financial sources.

## Future evidence contract and transaction boundaries

`paper_execution_evidence.v1` is an original-source, append-only contract in the
**existing native financial database**. It captures tenant, account, strategy/
version, fingerprint, session/mode, venue/market/symbol/timeframe, stable event ID,
UTC event time, source transaction ID and original payload. Missing identities
remain explicit; an incomplete configuration is not authoritatively hashed.

`v2_source_evidence` has unique event IDs, account/owner/time indexes and immutable
update/delete guards. `v2_episode_origins` retains the original position ID,
first entry event and frozen context; its indexed lookup avoids scanning trade
history on each fill. Neither table is a second balance authority.

The public broker methods preserve their existing commits. Evidence inserts
occur before the same commit under the existing lock; failure rolls back the
original financial mutation. No external network call is added. Constructor
migration failure cannot turn an unrecorded opening into complete evidence.
An existing account starts `CAPTURE_STARTED` with historical status UNKNOWN,
not a fabricated original opening.

The authoritative balance remains `v2_account`; original order, fill and
position calculations remain the broker's. Evidence writes add local
serialization/SQLite latency and can fail a financial transaction before commit.
This is an explicitly authorized persistence change, not a new strategy gate,
fill price, timing eligibility, leverage or sizing rule.

### Configuration lineage

At existing PA/SMC order boundaries, `original_lab_context` reads the original
captured decision. It verifies owner/session, strategy/version, frozen effective
configuration and the execution configuration actually used. SMC preserves the
canonical strategy identity and its separate existing model label. Current UI
settings cannot prove an older decision.

Order acceptance freezes a detached copy. Fill, protection and funding receipts
join that original order or original episode origin. Later settings changes do
not alter earlier fingerprints. Missing/manual configurations remain UNKNOWN.

### Episode and risk lineage

The original position ID allocated by `_apply_position` is the episode identity.
Scale-in retains that ID; partial exits are legs, not new statistical trades.
A reversing physical fill records an EXIT allocation to the original episode
and an ENTRY allocation to the new original position ID. Quantity and fees
reconcile to the physical fill. Entry risk uses actual allocated entry quantity
and original stop/price; the closing part of a reversal adds no new entry risk.

**One statistical trade is one original closed episode.** Open/unresolved or
mixed-configuration episodes do not silently become complete cohort evidence.
Internal protection instructions are recorded as original instructions without
inserting additional financial orders. Replay creates no orders or fills.

### Funding provenance and deduplication

The original funding amount, rate, mark, contemporaneous position quantity,
position ID and booking timestamp are retained. PA/SMC also supply the original
provider settlement timestamp/reference. Their existing deduplication row now
commits in the same broker transaction. The provider reference supplies a stable
future retry key; first-delivery quantity/rate/amount and booking time are
unchanged. No-position metadata remains an existing no-charge observation.

This closes the broker-commit/lab-ack crash gap; a restart finds the original
applied receipt and cannot book that same event again. A failed receipt insert
rolls back the charge. The two lab implementations are tested separately.

`record_funding_schedule` is a trusted producer hook for an original complete
provider range. There is no HTTP endpoint accepting funding proofs. An empty,
original, complete schedule can prove no applicable settlement inside a recorded
episode; absence of rows cannot. Existing pollers are **not** wired to provide
complete historical schedules/exposure. Booked processing-time quantity does
not prove quantity at an older provider settlement; delayed/missed coverage
therefore remains PARTIAL/UNKNOWN. No funding backfill occurred.

### Costs, cash and equity

Original fee rate, spread/slippage model, reference price and price components
are captured; spread/slippage are explicitly embedded in execution price, while
commission/funding are separate cash impacts. Reconciliation does not deduct
embedded costs a second time. Original financial arithmetic remains REAL/float;
reconciliation uses the existing 50-digit Decimal calculator and tolerances.

Signed cash receipts accompany original fill gross P&L minus commission and
funding debits/credits. Opening, capture-start, reset and restore observations
are distinguished. Existing reset/restore semantics are not changed or invoked
against user data. A reset/restore or missing old cash history prevents a false
complete journal claim. There is no second independently updated cash balance.

The public stream preserves the mark's own exchange timestamp separately from
a later bookTicker timestamp. Existing native quote processing records cash,
positions, marks, unrealized P&L and equity before the original commit. Unknown,
future or stale marks give UNKNOWN equity, not entry-price substitutes. Other
unvalued positions make the account observation incomplete. Flat cash/equity is
recorded as such. These are future valuation points, not continuous equity or
authoritative historical drawdown; the normalized series remains PARTIAL.

## Read-only reconciliation and historical protection

`native_paper_snapshot` consumes consistent original rows and verifies retained
event hashes. Physical fill facts must still agree with the original financial
row. Original transactional allocations become normalized episode legs; source
references and physical IDs remain visible. Original signed receipts supply
cash-journal references to the existing `paper_reconciliation.v1` calculator.

The calculator and existing formulas are reused. Mandatory unknown configuration,
ownership, account/session cohort, missing fills, funding, journal or exact paper
operational coverage still block VERIFIED performance. Newly captured facts do
not retroactively verify old reports. Existing immutable reports retain their
original snapshots and calculation identities. No production data was obtained.

The Instance reader continues to consume its original atomic outbox read-only.
Creating a separate funding/account/equity writer would exceed the safe audited
authority, so that part remains **BLOCKED**. Generic SMC shadow evaluation stays
blocked independently of native SMC financial evidence capture.

Human evidence review is append-only, authenticated and owner-scoped. It records
reviewer, reason, original report hash and idempotency key. REQUEST_MORE_EVIDENCE,
ACKNOWLEDGED, REJECTED and ARCHIVED are review actions, not execution permission.

## Durable outage buffering and operator monitoring

`DurableOperationalSpool` is a local SQLite outbox with synchronous FULL, stable
event IDs/hashes, UTC canonicalization, integrity checking and ordered bounded
replay into the existing operational store. Local commit happens before a
metadata delivery attempt. BUFFERED is distinct from COMMITTED. Lost ACKs replay
at least once with exactly-once logical effects through existing idempotency.
Conflicts and corruption are quarantined; overflow is latched and visible.

Limits: 10,000 transport records, 32 MiB global payload, 8 MiB per owner,
2,000 source registrations, 4 KiB events and 64 MiB SQLite page ceiling.
Delivered transport copies may expire after seven days; unacknowledged,
corrupt/conflicting records are not silently removed. Original metadata evidence
is unaffected. A physical disk-full write cannot report buffered success.

PostgreSQL standalone workers require `--operational-spool`, an explicit
`NEXUS_OPERATIONAL_SPOOL_PATH`, or the sibling of an explicit health file.
The local Compose profile provides durable bind-mounted paths. Filesystem/mount
durability and off-host backup remain operator prerequisites. An in-process
PostgreSQL test service without a configured path truthfully reports unavailable
buffering; it is not a supervised deployment profile.

Idle and busy operator workers now emit lifecycle/generation, sampled readiness,
metadata connectivity and research queue-backlog observations. Worker readiness,
feed connectivity, finalized receipt, evaluation and commitment remain separate.
Operator evidence uses the platform owner scope; it cannot certify a paper
account/configuration/session interval. Shutdown and worker generations remain
visible. Reports never interpolate an unmonitored period.

The existing feed callback still only enqueues bounded notices. Durable capture
happens when the observation worker consumes them, avoiding filesystem/network
work on the trading callback. A crash before consumption can lose volatile
notices; that interval remains UNKNOWN. This is not a zero-loss socket receipt
or continuous uptime guarantee.

## Telemetry retention and aggregation

`OperationalRetention` creates bounded, content-addressed, integrity-checked gzip
copies and versioned original-event count aggregates in trusted operator storage.
Manifests retain source/owner, period, IDs, hashes and original rows. Retrieval
verifies exact original content; tenant access and corruption/capacity fail closed.
Limits: 16 MiB uncompressed/8 MiB compressed per segment, 128 MiB global and
32 MiB per owner. Publishing is file-fsynced and directory-fsynced before the
immutable metadata reference commits; retries reuse content-addressed copies.

**PARTIAL:** automatic raw deletion/reclamation is not implemented. Raw events
may be pinned by immutable qualification/reconciliation reports, and the runtime
PG role intentionally has no DELETE privilege. There is no unreviewed removal
or trigger bypass. Raw capacity exhaustion remains visible. Aggregate counts
cannot substitute for missing probes, original rows or continuous uptime.

## Feed soak and cross-process design

`observe_existing_feed` attaches to an existing public feed hub, disables quote
execution consumers, bounds duration to 1–300 seconds and limits receipt queues.
It records sampled transport probes and original delivery notices, supports
cancellation and always detaches. Fixtures are labelled DETERMINISTIC_FIXTURE;
no strategy evaluator, order client or financial object is injected.

A single public Futures `/fapi/v1/time` connectivity probe was denied by the
egress proxy: **HTTP CONNECT 403**. TLS verification remained enabled; no bypass
was used. A sustained live-feed soak is **BLOCKED**, not passed. Public
`fapi.binance.com` and `fstream.binance.com` access plus an explicitly bounded
observation plan are prerequisites. No long external soak was started.

The existing hub deduplicates channels inside one process. It is not a durable
cross-process publisher. Independently supervised shadow processes can still
create separate public connections for different registrations. Deployment
requires a reviewed single-publisher topology with source/channel ownership,
bounded read-only distribution, idempotent canonical candle receipts, gap/repair
provenance and restart checkpoints. Preserve the financial consumer's current
timing and never relabel REST repairs as live receipts. Implementing that
transport now would require a broader execution-adjacent architecture change;
it is **NOT IMPLEMENTED / BLOCKED** for deployment.

## PostgreSQL cutover rehearsal

`metadata_cutover` is a trusted, local operator tool, not an API. It accepts at
most two original SQLite stores and freezes consistent backup snapshots before
reading rows. Backup integrity, foreign keys, SHA-256 and JSON validity are
checked. Export is bounded to 100,000 metadata rows, 128 MiB material and 256 MiB
backup footprint. Explicit allowlisted metadata tables are imported; financial
tables in the full source backup are never imported into worker metadata.

Active leases/observations, unsupported legacy research tables, inconsistent
frozen/data hashes, missing completed-result references and owner/reference
conflicts block preflight. External telemetry archive references require a
separate copy rehearsal; reference rows alone do not preserve files.

Local PostgreSQL targets must already have the installed isolated authority.
Import locks claims and refuses active target workers. It preserves original
IDs, owners, frozen JSON, attempts, lifecycle, results, candidates/campaigns,
holdout and shadow records. Primary-key duplicates must be byte/value identical;
conflicts roll back the entire import. Per-table target hashes must match.
Default dry run rolls back; committed rows are separately counted. Backend
selection is never changed.

Tested one-store job/attempt/result import, dry-run rollback, identical rerun,
conflict rollback and two-store campaign/candidate/experiment/holdout/shadow
identity preservation. Empty/unreviewed sources cannot claim a valid rehearsal.
No production cutover was performed.

Trusted local command (DSN supplied via local operator environment, never logged):

```sh
PYTHONPATH=automation-hub .venv/bin/python -m services.metadata_cutover \
  --source /tmp/isolated/research.sqlite --source /tmp/isolated/shadow.sqlite \
  --backup-dir /tmp/isolated/frozen-backups
```

`--apply-disposable` is an explicit local import. Use a fresh backup directory;
retain source/target backups and authority markers. Stop producers before any
real cutover. Application rollback keeps the old authority and additive evidence;
do not DROP tables or reverse financial receipts. A post-cutover rollback requires
reconciling any newly committed target jobs, not silently switching back.

## Local staging, failures and readiness

Existing local research/shadow Compose supervision is reused with mounted spool
paths, separate credentials-free entrypoints, bounded CPU/memory/PIDs, read-only
rootfs, dropped capabilities, no-new-privileges, bounded logs, health and graceful
shutdown. Actual local workers/PG transactions and fixture data are tested.
This is not an installed production supervisor or an authorized restored staging
environment.

| Drill | Result / limitation |
| --- | --- |
| Native evidence insert failure | Original order/fill/funding mutation rolls back; retry commits one receipt. |
| Funding commit before caller ACK | Both native labs recover one original applied receipt without another charge. |
| Metadata connection kill | Stable local event survives restart and idempotent replay. |
| Total metadata outage | Disposable PG paused; bounded original events spooled, local spool reopened, PG restored, each ID committed once. Unmonitored time stays UNKNOWN. |
| Lost research result ACK | Existing real PostgreSQL regression preserves one logical committed result. |
| Stale worker / cancellation / heartbeat | Existing actual PostgreSQL races and fencing regressions rerun. |
| Worker crash/restart/shutdown | Existing independently supervised research/shadow container regressions rerun. |
| Corruption / overflow | Quarantine or durable capacity failure; no accepted synthetic recovery event. |
| Disk-full | Actual 1 MiB container tmpfs returns ENOSPC; no persistence success claimed. |
| CPU pressure | Real 0.1-CPU cgroup increases `nr_throttled`. |
| OOM | 64 MiB container exits 137 with OOMKilled; replacement reads original bound spool. |
| Backup/restore / migration interruption | Actual existing PG tests and cutover rollback/rerun; no production target. |
| Feed recovery | Deterministic existing fixtures; live provider soak remains blocked. |
| Dataset expiry | Existing exact retention/mismatch/unavailable tests; no newer revision substitution. |

Local readiness artifacts retain environment identity, software/schema/backend,
window, reviewer, gate statuses and evidence hashes/references. LOCAL_VERIFIED,
PARTIAL, BLOCKED and UNKNOWN are **local** states. No API accepts a client claim
that a test passed or a filesystem path to a test result. Trusted operator JUnit
inspection records actual pass/fail/skip counts and file hashes. Corrections make
new assessment IDs. The artifacts grant no execution/deployment permission.

### Cloud storage incident and environment recovery

The root overlay exhausted available space during the first full validation
attempt. Docker reported 194 unused volumes, approximately 14.46 GB. Their
older provenance was insufficient for deletion; **none was pruned**. Root/Hub
runs were interrupted and preserved as intermediate failed setup evidence.

The known dependency directories were copied to `/tmp`, SHA-256 verified and
linked at their original paths. `.venv` digest:
`4e912e0ca57dd1e37f2d86850ef479c5571b99a190ac3bd5d9fc120e754321ce`;
dashboard modules digest:
`6c94f4b59a546a9bbd3e4538d43fd96221743b85a88f6afa1d5180ca4d69429f`.
No dependency content, secret or unrelated user source was discarded.

Disposable PG fixtures have an opt-in `NEXUS_PG_TEST_TMPFS=1` profile with 512 MiB
per server. Their own cleanup now removes anonymous volumes explicitly. This
uses real PG transactions/locks but RAM-backed test storage is **not disk/VPS
durability certification**. Before the storage incident, disk-backed local PG
tests had already passed. Later final runs use the explicit RAM-backed profile
and heavy suites run sequentially. A real staging rehearsal still needs
provisioned durable PG storage.

The earlier validated worker image was rebuilt offline from its source:
`sha256:14f585f000ffe998aae833c383c60abb03d8fdf3f533830820c2eeb1710780a3`.
Only three untagged images built during this sprint and 22 recorded, unshared
build-cache records from this sprint were reclaimed. The cache required an
explicit zero reservation / one-byte target; its default reservation had kept
the selected cache. BuildKit reported 21.61 MB of logical cache, while Docker
VFS reclamation returned approximately 3.8 GB of actual filesystem space.
The current tagged image and all 194 unknown volumes were preserved. The
reclamation proof and log are `/tmp/sprint11-final-owned-cache-proof.json` and
`/tmp/sprint11-cache-reclamation.log`. This is local cache recovery, not a
durable-storage capacity certification.

The independent follow-up rebuilt the code-only worker image after the health
and credential fixes:
`sha256:b8f23abadfa2382fed3fa5faa5e5a626449af83f71908985b69d65110ff69474`.
Final container tests use this image. The preceding image was exported first;
its verified archive SHA-256 is
`37d5faddbd0049626c8ad2897031d16bc252164aff9abd5c37ced84b37f28f07`.
Only that task-built untagged image and eight proven, unshared stale records in
its cache branch were reclaimed. The proof is
`sprint11-verification-image-proof.json`; branch cleanup recovered roughly
1.4 GB of VFS space. All unknown volumes were preserved. This remains local
capacity recovery, not durable deployed-storage validation.

The cloud configuration draft saves updated startup notes, the
`worker-postgres` installation extra and additive public-provider egress domains
`fapi.binance.com` / `fstream.binance.com`. Saving does not apply/publish it.
The current proxy still rejects the public connectivity probe with HTTP 403. Temporary
dependency-link restoration in a fresh task has not been independently verified.

## APIs and dashboard

Existing authenticated Strategy Intelligence routes remain. Additions:

* GET `operational-resilience/health`: owned spool backlog/corruption/overflow,
  raw retention usage/limits, explicit feed-soak blocker; no other tenant details.
* GET `paper-reconciliation/sources/{id}/lineage`: original configuration,
  paginated episode/funding references, cash/equity coverage and limitations.
* POST `paper-reconciliation/reports/{id}/review`: explicit owned immutable
  human review with idempotency; no activation or financial writer operation.
* GET `local-readiness` and `local-readiness/{id}`: owner-scoped immutable local
  evidence; there is no HTTP assessment-write/test-success override.

Queries are bounded and cache-disabled. Missing sources are 404, missing auth
401, oversized/extra parameters rejected and persistence errors sanitized.
No arbitrary paths, code, funding proofs, ownership overrides or order clients
are accepted. Reconciliation/lineage read native financial stores query-only.

The existing dashboard now shows Financial Evidence Integrity, configuration/
episode lineage, operational resilience/backlog/capacity, local readiness and
explicit review. All values come from backend contracts. Unknown funding does
not show green readiness or returns. API contract browser fixtures are clearly
test fixtures; actual numerical/accounting proofs are backend tests.

## Migrations and compatibility

1. `migrations/financial/0001_native_paper_evidence.sql`: additive original native
   source receipts and indexed episode origins, installed by the existing broker.
2. `migrations/shadow/0008_operational_resilience.sql`: additive local readiness,
   retention manifests and paper evidence review in existing isolated metadata.
3. `migrations/workers/0002_operational_resilience.sql`: equivalent PG tables,
   immutable guards/indexes, applied explicitly after worker metadata 0001 in
   the existing operator migration transaction.

Existing metadata marker version remains 1; the added feature schema is the
0002 superset. Runtime workers cannot migrate the database. Old application
rollback retains additive tables. Original legacy financial rows and earlier
unknown configuration/cost values are not updated. Account reset/restore
continuity remains explicit and incomplete instead of being falsely repaired.

The existing PR6 whole-file integrity assertions remain enforced. The manifest
adds a named authorized Sprint 11 delta, retains prior hashes and all earlier
deltas, pins current wrappers/broker/helper/DDL, and checks 166 unchanged
methods. The previous 113-method guard verifies explicit versioned transitions
for the four newly instrumented existing methods. Native PA, native SMC and
MTF rule files retain their original hashes. No assertion is removed.

## Files modified and contribution inventory

The final status contains **144 dirty paths**: the original 117, 25 new source
paths and two environment links. The cumulative Sprint 11 contribution spans
**44 paths**: 19 originally dirty files extended, three originally clean tracked
files modified and 22 new files. The other **98 originally dirty files retain
their original SHA-256 hashes**. The earlier 40-path/142-dirty inventory remains
historical evidence; the current inventory is
`sprint11-verification-cumulative-inventory.json`. HEAD remains unchanged.

The independent follow-up changes **eight paths** listed below. Against its
142-path baseline, 134 pre-existing files and both dependency links retain their
original identities. There are no deleted paths and no Git mutations.

| Follow-up path | Change |
| --- | --- |
| `.dockerignore` | Exclude private dotenv, virtualenv and native app state from build contexts. |
| `automation-hub/services/research_worker_main.py` | Shared credential-name guard used by both worker builders. |
| `automation-hub/services/shadow_worker_health.py` | Observation-database readiness and explicit verification diagnostics. |
| `automation-hub/tests/test_sprint11_operations.py` | 24 new health/credential regression cases. |
| `automation-hub/tests/test_sprint11_resource_drills.py` | Real Docker build-context exclusion regression. |
| `automation-hub/migrations/README.md` | Historical SQLite-only statement clarified; current operator migration boundary. |
| `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT11.md` | Independent audit, current results and limitations. |
| `automation-hub/docs/NEXUS_DEPLOYMENT_READINESS.md` | Services, variable names, backups, migration/startup/rollback and go/no-go review. |

### Previously dirty files extended

* `automation-hub/data/pr6_real_paper_freeze.json`
* `automation-hub/routers/strategy_intelligence.py`
* `automation-hub/services/price_action_lab.py`
* `automation-hub/services/price_action_stream.py`
* `automation-hub/services/smc_strategy_lab.py`
* `automation-hub-dashboard/src/pages/IntegrityOperations.tsx`
* `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx`
* `automation-hub/services/operational_evidence.py`
* `automation-hub/services/paper_evidence_reader.py`
* `automation-hub/services/postgres_workers.py`
* `automation-hub/services/research_jobs.py`
* `automation-hub/services/research_worker_main.py`
* `automation-hub/services/shadow_observation_worker.py`
* `automation-hub/services/shadow_worker_main.py`
* `automation-hub/tests/test_active_evidence_capture.py`
* `automation-hub/tests/test_postgres_workers.py`
* `ops/research-workers.local.compose.yaml`
* `automation-hub/services/shadow_worker_health.py`
* `automation-hub/migrations/README.md`

### Previously clean tracked files modified

* `automation-hub/execution/paper_broker_v2.py`
* `automation-hub/tests/test_paper_evidence_outbox.py`
* `.dockerignore`

### New files

* `automation-hub-dashboard/e2e/native-resilience.spec.ts`
* `automation-hub-dashboard/src/pages/LocalReadiness.tsx`
* `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT11.md`
* `automation-hub/docs/NEXUS_DEPLOYMENT_READINESS.md`
* `automation-hub/migrations/financial/0001_native_paper_evidence.sql`
* `automation-hub/migrations/shadow/0008_operational_resilience.sql`
* `automation-hub/migrations/workers/0002_operational_resilience.sql`
* `automation-hub/services/local_readiness.py`
* `automation-hub/services/metadata_cutover.py`
* `automation-hub/services/native_paper_evidence.py`
* `automation-hub/services/native_paper_snapshot.py`
* `automation-hub/services/operational_retention.py`
* `automation-hub/services/operational_spool.py`
* `automation-hub/services/paper_evidence_review.py`
* `automation-hub/services/public_feed_soak.py`
* `automation-hub/services/worker_operational_monitor.py`
* `automation-hub/tests/test_native_paper_evidence.py`
* `automation-hub/tests/test_operational_spool.py`
* `automation-hub/tests/test_sprint11_api.py`
* `automation-hub/tests/test_sprint11_operations.py`
* `automation-hub/tests/test_sprint11_postgres.py`
* `automation-hub/tests/test_sprint11_resource_drills.py`

Environment-only links `.venv` and dashboard `node_modules` are local setup
artifacts, not code contributions or files to commit. No Git operation has
staged them or any source changes.

## Validation results

Final code validation completed with **zero failures or setup errors**:

| Check | Passed | Failed/errors | Skipped | Warnings | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Focused Sprint 11 tests | 73 | 0 | 0 | 0 | 31.36 s |
| Automation Hub full suite | 3,326 | 0 | 15 | 90 | 521.41 s |
| Repository-root full suite | 3,856 | 0 | 15 | 90 | 528.20 s |
| Database/recovery selection | 490 | 0 | 0 | 4 | 161.15 s |
| Dedicated actual PostgreSQL selection | 30 | 0 | 0 | 0 | 79.12 s |
| Chromium browser suite | 154 | 0 | 0 | 4 | 6.0 min |
| Dashboard typecheck | PASS | 0 | — | 0 | Completed |
| Dashboard production build | PASS | 0 | — | 1 Vite advisory | 7.04 s |
| `git diff --check` | PASS | 0 | — | 0 | Completed |

These suites overlap; their totals must not be added. The database selection
includes all 73 focused tests and all 30 PostgreSQL cases. All final container
runs used worker image `b8f23abadfa2…`, rebuilt after the worker changes.
The final code/configuration change was the real Docker context regression;
the root, Hub, database, focused, typecheck/build and browser checks were rerun
after it. No source code changed after the final regression runs;
only documentation and retained local validation artifacts were finalized.
PostgreSQL is actual disposable PostgreSQL 16.15, not SQLite substitution.
The 15 root/Hub skips are existing conditional risk-parity exclusions:
eight `not a validity scenario`, seven `only comparable when both approve`.
No browser tests were skipped. The suite includes the four original Sprint 11
browser flows; this independent follow-up added no browser assertions.

The earlier retained validation remains historical: focused 48 passed; Hub
3,301 passed / 15 skipped / 89 warnings; root 3,831 passed / 15 skipped / 91
warnings; database 465 passed / 4 warnings; PostgreSQL 30 passed; browser 154
passed / 4 environment warnings. Intermediate follow-up runs before the final
Docker context regression also passed (focused 72, root 3,855 / 15 skipped /
90 warnings, browser 154). They are not substituted for the final results above.

Final commands (root working directory unless noted):

```sh
PYTHONDONTWRITEBYTECODE=1 NEXUS_PG_TEST_TMPFS=1 \
NEXUS_PG_WORKER_TEST=1 NEXUS_PG_SUPERVISOR_TEST=1 \
NEXUS_SHADOW_CONTAINER_TEST=1 NEXUS_PG_OUTBOX_TEST=1 \
NEXUS_SPRINT11_RESOURCE_TEST=1 .venv/bin/python -m pytest -q \
  -o cache_dir=/tmp/sprint11-verification-pytest-cache
# Hub: cd automation-hub; use ../.venv/bin/python with the same flags.
# Database: same root command plus files listed in
# /tmp/sprint11-database-test-files.txt.
# Dashboard: cd automation-hub-dashboard
npm run typecheck
npm run build
PW_CHROMIUM=/usr/bin/chromium npx playwright test \
  --config=/tmp/nexus-sprint9-browser.config.ts
```

The browser config reuses the repository's suite with an explicit fresh local
preview and output directory. JUnit/XML files contain original test identities
and counts. Logs preserve the underlying command exit status. The local fixture
RAM-backed PG option and moved dependency targets are described above; neither
constitutes production storage validation.

### Isolated application startup, authentication and shutdown

A code-only mirror verified all 358 copied Python/SQL files against this
working tree and copied both actually built frontend bundles. Uvicorn ran on
an owned loopback port with newly created isolated state, a deliberately
allowlisted development environment, random scoped credentials and execution
methods instrumented to reject any call. No app `.env` or existing user database
was copied or read. The actual HTTP/lifecycle results were:

* `/health`: 200; anonymous readiness API: 401; development login: 303.
* Authenticated empty readiness and dashboard: 200; operational resilience
  retained the sustained-feed blocker.
* A separate unconfigured production-auth import refused startup.
* Zero execution calls; 26 financial tables retained identical before/after
  rows; the existing legacy engine stopped in the completed shutdown callback.

The final SIGTERM return was -15 after the verified shutdown callback (Uvicorn
may re-raise its handled signal). The original proof is
`sprint11-verification-startup.json`. This is **local development authentication
and source startup**, not customer Supabase JWT, HTTPS ingress, production image
or VPS certification. No paper/live account was activated.

### Failed intermediate attempts and recovery

Earlier storage failures are preserved and do not appear as successful runs:

* Initial database attempt: 422 passed / 42 setup errors; dedicated PG: 30
  setup errors.
* Interrupted root: 2,076 passed / 15 skipped / 50 warnings / 34 errors;
  interrupted Hub: 2,126 passed / 15 skipped / 51 warnings / 36 errors.
* Later Hub capacity failure: 3,290 passed / 2 failed / 9 setup errors /
  15 skipped / 89 warnings. Database capacity failure: 439 passed / 26 errors.
* The subsequent database run recorded 419 passed / 3 failed / 43 errors in
  JUnit, then pytest session-summary/cache writing aborted with ENOSPC. Its
  warning count was not reported and is not inferred as zero.
* The diagnostic captured Docker's exact failure: creating a VFS init directory
  returned `no space left on device`. The final image rebuild and verified
  cache reclamation preceded the final zero-failure runs.
* One new browser fixture initially used a glob that missed the detail route.
  It now matches the actual endpoint; the UI rejects incomplete assessment
  responses. Assertions remain intact and all four new flows passed.
* The independent health/credential regression first failed all 24 new cases
  before the fixes; the same assertions then passed. A Docker-context helper
  initially failed before its assertion because of default client configuration
  and a cleanup attempt for an unbuilt image. The helper now uses an isolated
  client configuration and cleans only successfully created, owned artifacts.
  An actual old-rule image separately proved the fixture exposure; the final
  exclusion assertion passed in every final container-enabled suite.
* The first isolated startup helper lacked the landing bundle, and a second
  used an unsupported test-helper event-registration call. These were helper
  failures, preserved separately. The successful run included both actual SPA
  bundles and the supported router shutdown hook; HTTP and financial assertions
  were retained.

Intermediate logs remain under `/tmp/sprint11-*-diskfull*.log` and
`/tmp/sprint11-*-capacity-failed.*`; the last summary-write failure is retained
as `/tmp/sprint11-database-summary-write-failed.*`. The evidence archive below
also retains the later failed runs alongside final successful originals.

### Warning audit

| Source | Root / Hub / DB frequency | Severity and cause | Treatment |
| --- | --- | --- | --- |
| FastAPI | 4 / 4 / 0 | Existing deprecated `on_event` lifecycle API | Lifecycle migration remains separate; no current correctness failure. |
| aiohttp | 82 / 82 / 0 | Existing `enable_cleanup_closed` ignored on fixed Python 3.12.14 | Benign redundant option; no resource-leak warning observed. |
| Supabase | 4 / 4 / 4 | Existing HTTP client `timeout` / `verify` options deprecated | Actual gateway regression passes; pinned dependency migration remains separate. |
| Chromium runner | 4 browser warnings | Existing `NO_COLOR` / `FORCE_COLOR` interaction | Environment display warning, not browser assertion failure. |
| Vite | 1 build advisory | Existing large-chunk threshold | Build passed; no threshold/assertion weakened. |

No new warning class, resource-leak warning, unraisable exception or unhandled
thread warning appears in final runs. No global warning suppression was added.
Npm version-update notices are informational, not counted as test warnings.

### Immutable retained local assessment

Current independent assessment ID:
`local-readiness:536f0c05f94b1b99f793fce84086358927b261c6252549cc9ffd77ba466f696e`.
Overall status: **BLOCKED**. Financial reconciliation certification and the
sustained feed soak remain blocked. Local PG, supervision, resource pressure,
migration, backup/restore and spool recovery have original passing test-case
references. Credential/deployment separation remains PARTIAL; regression
records the 15 conditional exclusions explicitly.

Software identity combines HEAD with worktree source hash
`3e2a5df0e2ce9e60d7eb295d45a081d2478edb07b6b91449b3329e80d52ae0e3`;
HEAD alone is not presented as the build identity of this dirty tree. Reviewer
is `AUTOMATED_LOCAL_VALIDATION / Codex; not human deployment approval`.
`production_certification`, `staging_certification` and
`execution_authorization` are all **false**.

The isolated assessment is committed through `LocalReadinessStore`, then
backed up consistently with SQLite's backup API. It is not inserted into an
active application store or used to activate an instance. Original JUnit/logs,
connectivity evidence, source identity, benchmark and reclamation proof were
copied with SHA-256 checks into:

`/workspace/nexus-audit/sprint11-verification-536f0c05f94b`

`assessment.json`, `metadata.sqlite`, `suite-evidence.json` and
`validation-artifact-manifest.json` are locally retained review artifacts.
Their existence does not prove off-host backup, fresh-task restoration or
production certification. The source inventory is retained alongside them.

The current assessment records supersession of
`local-readiness:f42999057664bf7fba304c03016e3e62311ef1e636887341b2000db570ef4cd5`;
the original assessment/archive at
`/workspace/nexus-audit/sprint11-local-validation-f42999057664` remain unchanged.
The new archive includes actual final JUnit/logs, failed intermediate helper
and red-test runs, both original context-control and repaired context proof,
isolated startup evidence, current image/build identity, source and contribution
inventories, both final documents and a hash-verified consistent metadata backup.
Only the automated local assessment is stored there; it grants no human
research/deployment approval and changes no application authority.

## Descriptive performance measurement

The isolated original-broker fixture completed 100 episodes / 200 financial fill
transactions in 1.4709 seconds under concurrent cloud test load. Median candle
transaction time was 6.188 ms, p95 10.531 ms, peak traced memory 414,238 bytes.
It retained 401 original events / 1,097,253 payload bytes.
`/tmp/sprint11-benchmark.json` records the methodology and limitation. These
measurements describe fixture overhead; they are not strategy performance, a
production latency target, continuous uptime or an RPO/RTO guarantee.

## Readiness and Sprint 12 prerequisites

| Gate | Local status | Remaining requirement |
| --- | --- | --- |
| Native original future receipts | TESTED | Complete original account/config/session prerequisites per supported path. |
| Native verified profitability | PARTIAL / BLOCKED | Complete funding/exposure, journal/cohort, equity and exact paper monitoring. |
| Instance full account/funding authority | BLOCKED | Reviewed original writer capability, not synthetic account evidence. |
| Spool restart/idempotency | TESTED | Durable deployed mount, backup and operational monitoring. |
| Raw telemetry reclamation | PARTIAL | Reviewed report pin/expiry/archive/PG operator policy. |
| Worker supervision/fencing | TESTED locally | Authorized durable staging deployment and soak. |
| PostgreSQL cutover | TESTED locally | Quiescence, representative authorized staging exports, archive copy and reviewed authority switch. |
| CPU/memory/disk-full drills | TESTED locally | Real staging capacity, durable storage, kernel/profile review. |
| Cloud root storage | PARTIAL, locally recovered | Only ~2.1 GB idle headroom; provision durable capacity and preserve unknown volumes. |
| Sustained live-feed reliability | BLOCKED | Public egress access and explicit bounded test plan. |
| Cross-process feed distribution | NOT IMPLEMENTED / BLOCKED | Single publisher, bounded distribution and provenance/recovery review. |
| Continuous equity/uptime certification | NOT IMPLEMENTED | Complete original valuation/monitoring schedule and outage coverage. |
| Backup/restore | TESTED locally | Durable disk/off-host restore drill and operational policy. |
| Staging/production certification | BLOCKED | Separate explicit authorization and independent operational evidence. |

### Independent deployment-readiness assessment

VERIFIED below means original **disposable-local** evidence. No VPS/staging
verification was performed; absent deployed evidence remains UNKNOWN or BLOCKED.

| Requirement | Status | Actual boundary / unresolved evidence |
| --- | --- | --- |
| Application startup/shutdown | PARTIAL | Actual isolated source/development lifecycle verified; final Python 3.11/Node 20 production image not built/tested. |
| Required databases | PARTIAL | Real isolated PG 16.15 and fresh native SQLite verified; configured deployed authorities, mounts and driver packaging unverified. |
| Worker startup, claims, recovery | VERIFIED locally | Actual restricted containers, restarts, leases/fencing and spool replay; installed external VPS supervision UNKNOWN. |
| Authentication | PARTIAL | Actual local login/401 and production fail-closed import; deployed Supabase/customer JWT and HTTPS UNKNOWN. |
| Tenant isolation | VERIFIED locally | Actual owner-scoped API/store and real PG cases passed; deployed credentials/permissions UNKNOWN. |
| Market-data safety | PARTIAL | Existing finality/freshness/point-in-time regressions pass; live egress/soak blocked, cross-process publisher boundary incomplete. |
| Migration recovery | VERIFIED locally | Fresh/upgrade/rerun, interrupted PG DDL rollback, cutover conflict rollback and two-store lineage preservation; representative staging cutover BLOCKED. |
| Financial evidence consistency | PARTIAL | Original fixture quantities/cash/fees/episodes reconcile; full funding exposure, historical provenance and continuous equity/monitoring unavailable. |
| Unauthorized trading prevention | VERIFIED locally | Zero instrumented execution calls and financial non-mutation; no actual strategy/account activation occurred. |
| Backup/restore | VERIFIED locally | Actual SQLite consistent snapshots and PostgreSQL restore preserve IDs; off-host/durable staging restore UNKNOWN. |
| Secret handling | PARTIAL | Actual context-exclusion and worker-name guards verified; exhaustive environment isolation/deployed secret bindings UNKNOWN. |
| Complete release artifact | BLOCKED | Dirty/untracked release review, API driver packaging and actual-runtime build/rehearsal still required. |

**Sprint 11 status: PARTIAL. Financial integrity: PARTIAL. PostgreSQL: VERIFIED
locally. Worker reliability: VERIFIED locally for exercised failure scenarios;
sustained operational certification remains PARTIAL. Deployment: NOT READY FOR
STAGING.** No Git staging/commit/push or deployment occurred.

The complete operator services, database dependencies, variable names, backups,
migration/startup sequence, probe interpretation and rollback limitations are
in [NEXUS_DEPLOYMENT_READINESS.md](NEXUS_DEPLOYMENT_READINESS.md). The safest next
action is an independent release/package review and preparation of a separately
authorized disposable staging rehearsal with durable capacity, distinct scoped
credentials and original backup inputs. No such authorization or external
access is inferred from these local test results.

Sprint 12 should first close original provider schedule/exposure retention,
future account epoch/ownership and exact paper monitoring; review Instance
account authority; implement report-pinned raw archival/reclamation and the
single-publisher feed boundary; provision durable storage and run authorized
staging cutover, power-loss, restart and feed-soak drills. No alpha optimization,
champion switching or live activation is implied by these prerequisites.
