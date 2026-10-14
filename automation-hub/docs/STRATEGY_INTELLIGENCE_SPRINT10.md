# Strategy Intelligence Sprint 10

## Executive summary

**IMPLEMENTED:** an explicit PostgreSQL metadata backend for the existing research
and shadow interfaces, database-authoritative leases and fencing, atomic research
result commitment, independent worker entrypoints, isolated local supervision,
append-only operational evidence, read-only paper reconciliation, authenticated
APIs and two dashboard sections.

**TESTED:** real disposable PostgreSQL 16 concurrency, cancellation, lost commit
acknowledgement, reconnect/restart recovery, immutable results, backup/restore,
migration reruns and rollback after injected migration failure. Both independent
worker containers have been tested for crash/restart, graceful shutdown and
kernel resource limits. The research container executes the existing simulator
against exact retained fixture input.

**PARTIALLY IMPLEMENTED:** monitoring is bounded, sampled operational evidence;
it is not a certification of continuous transport or process uptime. Native PA,
SMC and Trading Instance financial adapters expose original facts and precise
missing-evidence reasons. They cannot turn their incomplete original account,
configuration, episode, funding or journal evidence into verified performance.

**BLOCKED:** staging/production certification, a live-feed soak, an authorized
legacy-data cutover and complete native paper financial provenance. No staging
or production access, deployment, Git staging, commit or push occurred. Generic
SMC shadow evaluation remains blocked. No strategy rules, risk settings,
execution modes or financial accounting writes were changed.

The earlier XRP figures remain **UNVERIFIED**. No authoritative production
account, exact configuration/session cohort, complete episode/cost/journal
records and monitoring interval were supplied or accessed.

## Repository audit and baseline

Audit HEAD: `7dd5e9a`. The starting working tree contained **92 individual dirty
files** using `git status --porcelain=v1 -uall`. Initial SHA-256 file hashes and
tracked diff were captured before editing. Sprint 10 contributes 41 paths:
16 previously dirty files extended, three previously clean tracked files
modified, and 22 new files. The other 76 initial dirty files are unchanged.
The final worktree contains 117 dirty paths; this is not a claim that all 117
belong to this sprint.

The historical Sprint 9 counts (Hub 3,175; root 3,705; focused 57; database 345;
Chromium 147) were treated as reported results. The actual pre-change relevant
baseline was **155 passed, zero failed/skipped, four warnings, 54.69 seconds**,
including existing PostgreSQL financial outbox and container opt-ins. Final
whole-suite results are recorded below separately.

The cloud setup workflow identified the existing Python 3.12.14 virtualenv,
Node 24, Chromium, Docker/cgroup v2 and PostgreSQL 16 image. PostgreSQL support
is an optional `worker-postgres` extra (`psycopg[binary]>=3.2,<4`; resolved 3.3.6).
A dependency sync initially pruned the pre-existing test extras. Their exact
original versions were restored from the installation log before baseline and
implementation checks continued. Final tests use the existing virtualenv
directly; do not resync it into a different dependency set mid-validation.

### Actual reusable components

| Existing component | Finding and boundary |
| --- | --- |
| `JournalContextStore` / `ResearchJobWorker` | Research queue, attempts, events, frozen requests and simulator bridge already exist; default SQL/locking was SQLite-specific. |
| `ShadowObservationStore` / `ShadowObservationWorker` | Registration lifecycle, generation fencing, decisions and immutable comparisons already exist. |
| `ForwardPaperMarketDataHub` / `PriceActionStream` | Public finalized futures candles and original financial consumers share the existing hub. Cached REST bars do not prove live receipt. |
| Native `v2_account`, `v2_orders`, `v2_fills`, funding tables | Original financial authorities, but not a complete configuration/episode/cash-journal authority. `account()` may persist metrics and is never called by reconciliation. |
| Trading Instance ledger/outbox | Consistent read-only snapshots and original atomic receipts exist; an account equity/funding authority is not established by those receipts alone. |
| Immutable active decision captures | Preserve contemporaneous configurations; they are decision evidence, not proof of a fill or account cash movement. |
| Dataset retention / qualification | Exact input snapshots and PASS/FAIL/UNKNOWN gates already exist. They are reused; no second simulator, queue, ledger or qualification engine is introduced. |

Implementation followed the audited dependency order: metadata backend and
fencing; telemetry; financial source audit/calculation/artifacts; APIs/UI;
independent supervision and full validation.

## PostgreSQL worker architecture

`services/postgres_workers.py` implements the existing interfaces against a
private `nexus_workers` schema. The target is explicitly
`research_shadow_metadata`, with a durable authority UUID and runtime-role
marker. This is separate from every financial database.

Runtime handles reject a superuser, CREATEDB/CREATEROLE/BYPASSRLS, dangerous
predefined-role memberships, schema CREATE privileges, missing/mismatched
markers and financial tables (including `v2_*`) in the target database.
Connections set UTC, a private search path, a 10-second statement timeout and a
five-second lock timeout. The runtime role receives metadata SELECT/INSERT/
UPDATE and sequence usage; immutable-table triggers reject result changes.
Workers cannot migrate schemas or update the authority marker. No new tenant
RLS claim is made: trusted workers process multiple owners; API/store owner
filters and authorization enforce the application boundary.

A small DBAPI seam adapts only existing parameter bindings, null-safe equality
and timestamp casts. It rejects SQLite PRAGMAs, SQLite catalog operations and
SQLite locking/INSERT-OR statements. PostgreSQL DDL and coordination are
explicit, not emulated SQLite transactions.

### Claims, leases and result commitment

- Claims use a short transaction, a quota advisory lock and `FOR UPDATE SKIP
  LOCKED`. A unique lease token, increasing attempt, database heartbeat and
  expiration commit before computation. No backtest holds the claim transaction.
- Default global research concurrency is one; trusted configuration can request
  one to four. Lease length is bounded to 5–300 seconds. Existing job/dataset/
  runtime/Monte Carlo/window limits remain in force. PostgreSQL submission adds
  a 200-job owner and 2,000-job global retained-metadata cap.
- Heartbeats and worker transitions check owner/token/state/expiration with
  database time. Accepted cancellation prevents heartbeat renewal and successful
  result commitment. Shadow generation/token checks use the same database clock.
- Research result commitment locks the job and verifies its frozen identity,
  active unexpired attempt, logical result key, mode and COMPLETE result. The
  immutable validation insert, terminal job state and completed attempt share
  one PostgreSQL transaction. Result payloads are bounded to 8 MiB.
- Physical execution can repeat. There is one logical accepted result, backed
  by unique identities, immutable validation records and fencing. A conflicting
  duplicate is rejected; exactly-once physical execution is not claimed.
- If the COMMIT acknowledgement is lost, the worker reads the durable completed
  result and reference rather than issuing `COMPLETED -> INTERRUPTED`. The
  recovery test verifies one validation row and no retryable completed job.
- Expired attempts recover to bounded retry or failure; accepted cancellations
  recover to CANCELLED. Attempt status and append-only job events retain history.
  A dead connection reconnects on the next operation, revalidates the original
  authority/role and never blindly replays an uncertain mutation.

Backend selection is explicit through `NEXUS_RESEARCH_WORKER_BACKEND` and
`NEXUS_SHADOW_WORKER_BACKEND` (`sqlite` default or `postgresql`) and their
respective `NEXUS_*_WORKER_DSN` variables. Missing drivers, targets or DSNs fail
closed; PostgreSQL never falls back to SQLite. A running context cannot silently
switch backend/authority. Research producers, candidate/comparison/agent
services and validation consumers use the selected research context. Core
financial analytics continue using the original financial/evidence authority.

No SQLite-to-PostgreSQL data import is automatic. Older artifacts remain in their
original store. A reviewed cutover/import, with exact identities and retained
inputs, is an operational prerequisite. Do not split producers and workers
across authorities or pretend an empty new queue contains the old history.

## Independent supervision and resource isolation

`services/research_worker_main.py` and the existing shadow entrypoint run without
app/webhook/order-runtime imports. Known execution credentials are rejected.
The code-only image excludes environment files, financial databases, app/webhook
entrypoints and the financial execution package. API processes queue durable
PostgreSQL jobs; lazy in-app PostgreSQL workers refuse computation when financial
runtime modules are loaded.

`ops/research-workers.local.compose.yaml` is **opt-in, disposable local**
supervision, with an optional shadow profile. It does not change production
Compose or deployment settings. Both workers use init, bounded restart policy,
health probes, a read-only rootfs, dropped capabilities, no-new-privileges,
bounded temporary storage and bounded logs. The private network contains only
the disposable metadata database and test workers; no trading secrets or
financial volumes are passed.

| Worker | CPU quota | Memory limit | PID limit | Temporary storage |
| --- | --- | --- | --- | --- |
| Research | 1 CPU | 512 MiB | 64 | 64 MiB |
| Shadow | 0.5 CPU | 384 MiB | 64 | 64 MiB |

Tests read actual cgroup `cpu.max`, `memory.max` and `pids.max`, kill the worker
process, verify a new worker generation and healthy restarted container, and
verify graceful STOPPED reporting. Research also commits an actual existing-
simulator job using retained fixture input. These are measured **local** controls,
not deployed-host certification. Runtime timeouts/cancellation remain
cooperative; containers supply hard CPU/memory/PID bounds, not a complete
security sandbox. Arbitrary filesystem/code execution is not an API capability.

The isolated network deliberately does not certify production historical
provider access or a live Binance feed. Shared subscriptions are reused within
one process; cross-process feed fan-out and outbound provider access require an
explicit deployment design. No unnecessary production exchange connections
were opened for this validation.

## Durable operational evidence and reliability

SQLite migration 0006 and the PostgreSQL metadata migration add immutable
owner-scoped sources and events. Source registration time is separate from the
first actual monitoring-start event. Sources record observation kind, strategy,
version, fingerprint, venue/market/symbol/timeframe where originally known.

Events preserve UTC occurrence and evidence-commit timestamps, source/owner,
worker/generation, idempotency key, payload hash and COMMITTED state. Capture
includes sampled health/transport transitions, finalized candle receipts,
evaluation start/completion/error, stale/missing context, commit success/failure,
monitoring start/stop and recovery evidence. Existing durable research job
history also records queue, claim, attempt, heartbeat and recovery transitions.
PostgreSQL research attempts emit owner-scoped operational records. Idle
research readiness is a current health file, not a historical uptime certificate.

The public-feed callback adds only a bounded queue notice after the unchanged
original closed-bar consumer. It performs no SQL or external network operation.
Only finalized live WebSocket events count as live receipt. REST repairs remain
separate. Invalid notices cannot stop the entire worker sweep. Queue loss and
recoverable telemetry-persistence failure counts are recorded when persistence
returns; a crash before those counters can commit remains an unknown/missing
period, not reconstructed evidence.

Health probes are emitted every 60 seconds while a registration/attempt is
observed. Probe timestamps describe detection time. A sampled disconnect is not
assigned an invented exchange disconnect timestamp. A stop prevents an older
start from certifying a later window. Resume creates a new immutable start event.
Reports include the latest preceding lifecycle anchor even after a long-running
source has exceeded the earlier seven-day lookup horizon.

### Exact denominators

Windows are UTC half-open intervals `[start,end)`, bounded to seven days.

| Metric | Numerator / denominator and missing behavior |
| --- | --- |
| Worker/feed probe availability | Slots containing all affirmative original boolean probes / expected 60-second probe slots. Missing/unknown/conflicting monitoring means UNKNOWN, not 100%. This is sampled availability only. |
| Finalized live receipt coverage | Unique compatible finalized LIVE_WEBSOCKET close opportunities / expected timeframe boundaries. Unknown capture capability stays UNKNOWN; REST repair does not count. |
| Processing completion rate | EVALUATION_COMPLETED / (EVALUATION_COMPLETED + EVALUATION_ERROR). A handled data block is processing completion, not an eligible trading decision. Errors are not counted twice. |
| Commitment success rate | COMMIT_SUCCEEDED / (COMMIT_SUCCEEDED + COMMIT_FAILED) for recorded attempts. An acknowledgement failure can coexist with subsequent durable recovery evidence. |
| Missing probes / candle opportunities | Expected minus actually observed; unobserved intervals are not filled with synthetic heartbeats or decisions. |
| Latency | Original local processing latency only; no exchange-to-host latency or exact recovery duration is asserted when source timing is missing. |

Event queries are owner-scoped and paginated (at most 200 per page). Reports
retain exact event references/hash. Bounds are 500,000 global/100,000 owner events,
256 MiB global/64 MiB owner payload, 4 KiB individual event, and 2,000 sources.
The 30-day reporting horizon is not a promise of 30 days at unlimited candidate
volume. No automatic raw-evidence deletion/archive was implemented: capacity
exhaustion fails closed and missing subsequent monitoring stays unknown.

**PARTIALLY IMPLEMENTED:** standalone operator-wide idle monitoring, durable
write-failure spooling during a total metadata outage, long-term aggregation,
exact recovery durations and a real-feed soak. Logs/counters and missing probes
remain evidence of gaps; they cannot reconstruct offline observations.

## Forward Qualification integration

New forward report inputs freeze the operational report, original event
references and hash. Software component hashes include the operational module.
Observed worker/feed probe availability and commitment rates are exposed
separately. Existing v1 formulas and completed reports are not rewritten.
Sampled probes do **not** satisfy the existing continuous unexplained-worker-
downtime gate. Missing operational history remains UNKNOWN. Shadow decisions,
research results, paper fills and live trades remain separate categories.

## Paper evidence availability matrix

The reader resolves only configured owned active sources. It opens native
SQLite sources read-only/query-only under a consistent snapshot and invokes
existing Trading Instance snapshot getters. It never invokes broker/account
constructors or the `account()` method that can persist metrics. No arbitrary
client path, snapshot, cost, owner override or verification flag is accepted.

| Required original fact | PA / SMC native paper | Trading Instances |
| --- | --- | --- |
| Account ID/balance | Original `v2_account` row; account-owner attribution/history not inferred from selection | Scoped ledger/outbox exists; account equity authority unavailable |
| Order/intent | Original order rows and per-session proposal metadata; protection/exit links can be incomplete | Original intent/context where captured; not automatically a complete order journal |
| Quantity/price/fees | Original fill facts where persisted | Original atomic outbox receipts retained, without synthetic transactions |
| Version/configuration | Per-order original version; fingerprint propagation only for exact original decision/order join plus identical complete evaluator/execution configuration | Original context/identity where captured; missing fields remain unknown |
| Frozen snapshot | Immutable active receipt can prove a matching complete configuration; sparse/current settings cannot | Original immutable context reference required; absence blocks verification |
| Episodes/partial exits | Fill-to-original-episode identity not universally present | Existing receipt lineage is readable; it does not prove a complete account/journal/cost cohort by itself |
| Funding | Original rate/mark/charge events when present; quantity/schedule/exposure completeness not universally recorded | Often explicitly not modelled; unknown is not zero |
| Spread/slippage | Original amounts retained; full spread/requested price is not automatically a signed embedded-price cost model | Original assumption/reference evidence required |
| Cash journal/equity series | Activity logs and current drawdown/peak scalars are not a complete cash/equity history | Trade receipts are not automatically an account-equity authority |
| Operational coverage | No original paper-specific sustained telemetry supplied | No original paper-specific sustained telemetry supplied |

The native adapters intentionally return **PARTIAL/UNKNOWN**. They preserve
original orders, fills, funding, order metadata and immutable decision receipts.
They do not substitute a current strategy configuration, assign an episode by
trade direction, use fill funding=0 as proof, or synthesize a cash journal from
fills. SMC active decision observation and generic SMC shadow execution remain
separate; no SMC rule or confirmation gate was changed.

## Reconciliation methodology and financial calculations

`paper_financial_snapshot.v1` is a trusted server-side normalized original-source
contract, never an HTTP body. The calculator uses Decimal with precision 50;
monetary tolerance defaults to 1e-8 and quantity tolerance to 1e-12. Policy
versions/fingerprints are immutable. Existing producer amounts are not rewritten.

Required checks include original consistent source authority, exact owner/account/
strategy/version/fingerprint/session/symbol/mode, a matching original immutable
configuration snapshot, complete uncapped records, unique order/fill/funding/
journal identities, original intent references, one episode per fill, quantity
and side conservation, chronological scale-in/partial-exit accounting, actual
booked gross P&L, fees, funding exposure/schedule, cash-journal references and
account cash reconciliation. Paper operational evidence must match the same
financial cohort and cover the requested period; shadow uptime cannot certify
paper execution. Missing originals are UNKNOWN; contradictions FAIL.

A completed statistical trade is one original closed episode. Partial exits do
not create extra statistical trades. Inclusion uses original episode closure
within `[start,end)`; metrics include the entire episode's original costs, not
an invented cash-period allocation. Financial account reconciliation uses the
complete original snapshot, not a selective profitable subset.

Fees/funding are separately recorded. Signed funding is checked against original
rate, mark price and actual chronological exposure. Missing quantity/schedule or
ambiguous equal timestamps remain unknown. Verified zero funding requires an
original no-exposure/exemption reference and complete expected-event list.
Embedded spread/slippage is not deducted twice and must explain the signed
fill/reference-price difference. Native full-spread rows are not silently
interpreted as this basis. Cost stress is a separate MODELLED_COST_STRESS output.

Only when all mandatory gates pass are episode count, win rate, average win/loss,
expectancy, profit factor, gross P&L, fees/funding/net P&L, concentration and exact-
cohort/session breakdowns populated. R additionally requires original actual
entry risk; its denominator is the sum of recorded entry-leg risk allocations
for that episode, not a count of its exits. Drawdown additionally requires a complete original, period-matched,
referenced equity series; a current account balance or peak scalar is insufficient.
Undefined denominators remain null. Costs stressed by 25%/50% are explicit v1
hypothetical scenarios on the recorded outcomes; they are not strategy re-runs,
actual costs or future profitability predictions.

Performance labels are RECONCILED_PAPER_PERFORMANCE,
PARTIALLY_RECONCILED_PAPER_PERFORMANCE or UNVERIFIED_PAPER_PERFORMANCE. Shadow
behaviour never becomes a trading return. No result grants execution authority.

## Immutable reconciliation reports

The isolated metadata store persists versioned policy, original snapshot,
source/owner/cohort/period, passed/failed/unknown gates, cost coverage, monetary
results when eligible, software hashes, evidence hash, original references,
creation timestamp and supersession lineage. A preview is NOT_COMMITTED.

Report creation is owner/idempotency scoped. Concurrent duplicates resolve to
one logical immutable artifact; conflicting content is rejected. Corrections
create a new report pointing at the original. Original snapshots are retained
with the report for recomputation rather than relying on a content hash alone.
Limits are 8 MiB per report/snapshot pair, 100 reports and 64 MiB per owner,
256 MiB globally, and 1,000 owner/10,000 global request bindings. No financial
ledger, order, position, journal or balance is updated.

## APIs and dashboard

All routes extend `/api/v2/strategy-intelligence`, reuse existing authentication,
owner authorization, no-store behavior and bounded request models. No execution
endpoint was introduced. PostgreSQL persistence errors return a bounded 503
message without DSNs or source contents.

| Method/path | Purpose |
| --- | --- |
| GET `/operational-reliability/sources` | Owned monitoring sources |
| GET `/operational-reliability/sources/{id}` | Bounded window reliability report |
| GET `/operational-reliability/sources/{id}/events` | Paginated immutable original events |
| GET `/paper-reconciliation/sources` | Owned configured paper evidence sources |
| GET `/paper-reconciliation/sources/{id}/availability` | Field-by-field original evidence diagnostics |
| POST `/paper-reconciliation/preview` | Read-only assessment, no result commitment |
| POST `/paper-reconciliation/reports` | Idempotent immutable metadata report |
| GET `/paper-reconciliation/reports` | Paginated owner history |
| GET `/paper-reconciliation/reports/{id}` | Report and original retained snapshot |

`IntegrityOperations.tsx` adds Operational Reliability and Paper Reconciliation
tabs to the existing hub. Controls select owned sources/UTC periods, inspect real
backend evidence, preview/commit reports and inspect history/original snapshots.
Missing metrics show UNKNOWN. Source/period/tab changes reset stale results.
There is no order, activation or promotion control. Existing dashboard financial
formulas and unrelated layouts remain unchanged.

Browser fixtures are explicitly API/UI contracts, not market or profitability
evidence. Backend tests separately exercise the real calculator, stores,
permissions, original read-only adapters and actual PostgreSQL/container paths.

## Migrations, rollback and recovery

| New migration | Target |
| --- | --- |
| `migrations/workers/0001_postgresql_worker_metadata.sql` | Private PostgreSQL metadata schema, original research/shadow/qualification/campaign/agent interfaces, marker, immutable triggers, queue/lease indexes and new operational/report metadata |
| `migrations/shadow/0006_operational_evidence.sql` | Isolated SQLite operational source/event tables, owner/time indexes and append-only triggers |
| `migrations/shadow/0007_paper_reconciliation.sql` | Isolated SQLite versioned reconciliation artifacts/request bindings and immutable triggers |

PostgreSQL installation is an explicit operator call to
`migrate_worker_database(operator_dsn, target='research_shadow_metadata',
worker_role='nexus_worker')`. Runtime constructors do not perform PostgreSQL DDL.
The migration runs atomically, takes a migration advisory lock, verifies the
purpose/role marker and rejects financial databases before any DDL. New metadata
indexes, foreign keys and immutable triggers follow existing interface needs.
Reruns preserve accepted jobs, retained inputs and results. Injected SQL failure
rolls back the transaction; a clean forward rerun preserves earlier evidence.

Before an authorized cutover take verified backups, stop producers/workers,
resolve active leases and retain both schema/data and matching software. The
local PostgreSQL dump/restore test verifies authority UUID, accepted job and
immutable validation equivalence, then reruns migration without duplication.
Do not erase the private schema to roll back or delete financial history. Older
code can continue using its original SQLite store, but that is not a rollback of
new PostgreSQL evidence. Reversing a cutover needs a reviewed import/restore and
must not orphan accepted jobs or pretend two queues are one authority.

Dataset retention continues using immutable exact-input snapshots and original
manifest hashes/references. PostgreSQL storage reuses that implementation. Hashes
alone do not reconstruct expired/unavailable provider revisions. Quotas remain
15,000 candles/4 MiB per snapshot, 64 MiB global/32 MiB owner retained data. No
new external storage system or silent provider re-fetch substitution was added.

## Tests and observed validation issues

The following completed runs use the final implementation, including the native
financial-database guard. All final suites completed. Earlier partial/interrupted
runs are not reported as whole-suite success.

| Validation | Passed | Failed | Skipped | Warnings | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Focused Sprint 10 plus existing worker regressions | 91 | 0 | 0 | 0 | 105.31s |
| Automation Hub full suite | 3,253 | 0 | 15 | 89 | 513.02s |
| Repository-root full suite | 3,783 | 0 | 15 | 89 | 523.05s |
| Database/recovery/migration/restore selection | 417 | 0 | 0 | 4 | 149.00s |
| Real PostgreSQL tests, included above | 24 | 0 | 0 | 0 | Includes both supervised-container cases |
| Chromium full suite, approved `/usr/bin/chromium` | 150 | 0 | 0 | 4 environment warnings | 5.9m |
| New Chromium integrity workflows after final guard | 3 | 0 | 0 | 4 environment warnings | 11.9s |
| Dashboard TypeScript check | Passed | 0 | 0 | 0 | Existing `npm run typecheck` |
| Dashboard production build | Passed | 0 | 0 | 1 existing bundle-size warning | 6.72s |
| `git diff --check` | Passed | 0 | 0 | 0 | No Git mutation |

There are **78 new Sprint 10 tests**. The 91-test focused selection also includes
13 earlier worker regressions. The 15 whole-suite skips are the existing
conditional risk parity scenarios (`not a validity scenario` / `only comparable
when both approve`), not skipped PostgreSQL, database, recovery or browser tests.

Commands were executed with:

```sh
NEXUS_PG_WORKER_TEST=1 NEXUS_PG_SUPERVISOR_TEST=1 \
NEXUS_SHADOW_CONTAINER_TEST=1 NEXUS_PG_OUTBOX_TEST=1 \
  .venv/bin/python -m pytest -q
# Run separately from automation-hub with ../.venv/bin/python.
# Focused selection: the eight new test files plus forward_operations and
# shadow_operations. Database selection: financial/outbox/journal/migration/
# recovery/retention/qualification/worker suites, including all real PG tests.
cd automation-hub-dashboard
npm run typecheck
npm run build
PW_CHROMIUM=/usr/bin/chromium npx playwright test \
  --config=/tmp/nexus-sprint9-browser.config.ts
```

The temporary browser config imports the repository config, keeps every test,
assertion, Chromium option and timeout, and uses a fresh production-preview
server on port 4190 with output in `/tmp`. It does not reuse a previously running
server or rebuild assets while testing. PostgreSQL and Docker tests create
unique, disposable resources and remove them in fixture teardown.

Two real issues were reproduced with failing regression tests and corrected:
old monitoring starts certifying stopped windows, and evaluation errors counted
as both completion and error. Additional failing regressions drove configuration
snapshot verification, exact operational account/period checks, immutable resume
starts, original episode fill coverage, reconnect and lost-ack recovery.

The native `v2_*` financial-target regression showed the standalone SQLite
constructor could append shadow tables before a later guard rejected the file.
The guard now rejects the original target **before construction**, preserving
both schema and balance. This correction does not change trading behavior.

An earlier whole-suite test falsely detected another tenant's `777` sentinel in
an owned calculation timestamp's microseconds. The fixture now uses whole-second
timestamps; the original whole-response leak assertions are unchanged. Its full
52-test API file passed after the fix.

An initial browser run had 149 passes and one 8-second selector timeout in an
existing shadow-comparison test. Both tests passed with trace capture on an
isolated approved Chromium rerun; the full approved Chromium rerun passed 150.
No test or timeout assertion was removed or weakened. This unreproduced initial
browser failure is retained here rather than hidden.

The calculator capacity benchmark is a **synthetic fixture, not performance
evidence**: 500 episodes/1,500 fills, 2,358,627-byte serialized snapshot, 1.2948s
pure calculation and 7,330,438-byte peak traced allocation under concurrent test
load. Source fetch, networking and commitment are excluded. Container recovery
checks include deliberate delays, so their test durations are not throughput or
live-feed latency guarantees.

Warning audit (final Hub/root **89 each**: four FastAPI, 81 aiohttp and four
Supabase; focused zero, database selection four Supabase): existing FastAPI `on_event` lifecycle deprecations; existing
ccxt/aiohttp `enable_cleanup_closed` deprecation because Python 3.12.14 contains
the upstream cleanup fix; existing Supabase timeout/verify API deprecations.
These summaries did not indicate a new worker connection leak or unclosed
resource. No global warning suppression was added. Browser NO_COLOR/FORCE_COLOR
warnings and the existing Vite bundle-size warning are recorded separately.

## Operational readiness matrix

| Gate | Local evidence | Deployment status |
| --- | --- | --- |
| PostgreSQL multi-worker claims/fencing | VERIFIED real PG16 connections, cancellation, expiry, duplicate/conflicting commit and recovery | PARTIAL: no deployed/staging certification |
| Independent supervision | VERIFIED research and shadow containers, restart/generation, graceful stop | BLOCKED: not installed in any authorized staging/production service |
| Resource limits | VERIFIED kernel CPU/memory/PID/read-only/capability controls | PARTIAL: no load/soak/OOM drill or app-wide sandbox certification |
| Migration safety | VERIFIED disposable fresh installs, preserved-row reruns, injected-failure rollback | BLOCKED: no authorized production-shaped upgrade/cutover |
| Backup/restore | VERIFIED disposable PG and existing SQLite restore/recovery tests | PARTIAL: no operational schedule, RPO/RTO or staging restore drill |
| Dataset retention | VERIFIED immutable retained input/retrieval/hash/PG use | PARTIAL: quotas; unavailable provider revisions remain unreconstructable |
| Market-data monitoring | VERIFIED original delivery provenance, sampled health, stale/gap/error handling | PARTIAL: no live provider reliability soak or continuous uptime certificate |
| Observability | VERIFIED durable source/job/attempt/decision/commit evidence and current process health | PARTIAL: no total-outage durable spool or operator-wide idle history |
| Tenant isolation | VERIFIED owned APIs/stores, cross-owner denial, metadata-only target/role | PARTIAL: no deployed PostgreSQL RLS or permission review certification |
| Financial integrity | VERIFIED exact Decimal fixture reconciliation and original-source non-mutation; native gaps fail closed | BLOCKED for verified native performance until original authority gaps are resolved |
| Production credentials | VERIFIED code-only local image and explicit local environment isolation | UNKNOWN deployed credential separation; no production environment accessed |

Local feature validation does not establish production readiness. Authorized
staging must verify credentials/permissions, metadata cutover, representative
schema/data sizes, independently supervised restarts, retained-data recovery,
provider access and sustained operational capacity. Native paper profitability
stays blocked until its original financial and operational provenance is complete.

## Sprint 11 recommendations

1. Obtain authorization for a disposable production-shaped staging database and
   reviewed metadata cutover; preserve legacy immutable IDs and dataset inputs.
2. Resolve original native account ownership, financial order/config/episode
   linkage, funding exposure/schedule, cash journal and equity authority without
   inventing historical facts or rewriting financial records.
3. Add reviewed durable outage buffering/aggregation and operator-wide monitoring
   so longer campaigns do not exhaust the append-only capacity.
4. Validate live finalized futures delivery, cross-process feed sharing and
   lookback/freshness requirements under an authorized sustained soak.
5. Exercise OOM/CPU pressure, disconnect/restart, restore and retention expiry
   against representative loads and publish explicit operational certificates.
6. Keep qualification, research designations and any future paper trial separate
   from execution authorization. No automatic promotion or live activation.

## Contribution-only file inventory

This list is relative to the captured Sprint 10 baseline, not to HEAD.

### Extended pre-existing dirty files

- `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx`
- `automation-hub/routers/strategy_intelligence.py`
- `automation-hub/services/forward_paper_evidence_reader.py`
- `automation-hub/services/forward_qualification.py`
- `automation-hub/services/forward_qualification_store.py`
- `automation-hub/services/research_jobs.py`
- `automation-hub/services/shadow_observation_worker.py`
- `automation-hub/services/shadow_worker_health.py`
- `automation-hub/services/shadow_worker_main.py`
- `automation-hub/services/strategy_intelligence_service.py`
- `automation-hub/tests/test_shadow_operations.py`
- `automation-hub/tests/test_strategy_intelligence_api.py`
- `automation-hub/webhook_api.py`
- `ops/prepare-shadow-worker-wheels.py`
- `ops/shadow-worker.Dockerfile`
- `uv.lock`

### Previously clean tracked files modified

- `automation-hub/services/forward_paper_hub.py`
- `automation-hub/services/price_action_stream.py`
- `pyproject.toml`

### New files

- `automation-hub-dashboard/e2e/integrity-operations.spec.ts`
- `automation-hub-dashboard/src/pages/IntegrityOperations.tsx`
- `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT10.md`
- `automation-hub/migrations/shadow/0006_operational_evidence.sql`
- `automation-hub/migrations/shadow/0007_paper_reconciliation.sql`
- `automation-hub/migrations/workers/0001_postgresql_worker_metadata.sql`
- `automation-hub/services/operational_evidence.py`
- `automation-hub/services/paper_evidence_reader.py`
- `automation-hub/services/paper_reconciliation.py`
- `automation-hub/services/paper_reconciliation_store.py`
- `automation-hub/services/postgres_workers.py`
- `automation-hub/services/research_worker_main.py`
- `automation-hub/services/worker_backends.py`
- `automation-hub/tests/test_candle_observation_provenance.py`
- `automation-hub/tests/test_integrity_operations_api.py`
- `automation-hub/tests/test_operational_evidence.py`
- `automation-hub/tests/test_paper_evidence_reader.py`
- `automation-hub/tests/test_paper_reconciliation.py`
- `automation-hub/tests/test_paper_reconciliation_reports.py`
- `automation-hub/tests/test_postgres_workers.py`
- `automation-hub/tests/test_worker_backends.py`
- `ops/research-workers.local.compose.yaml`

The remaining initial files have matching SHA-256 hashes. No paths were staged,
committed, pushed or discarded. Strategy/financial writer changes inherited from
earlier sprints remain distinguishable from this contribution.
