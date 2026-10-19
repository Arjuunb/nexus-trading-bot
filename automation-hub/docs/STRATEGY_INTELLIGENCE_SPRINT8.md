# Sprint 8 — Matched Shadow Comparison Intelligence

## Executive summary

IMPLEMENTED: a read-only active-evidence adapter, strict versioned decision
comparison contract (`shadow_comparison.v2`), immutable owner-scoped comparison
artifacts, authenticated APIs, Shadow Lab comparison controls, worker health and
an independently startable observation entrypoint, plus bounded exact research
input retention in the existing research journal.

PARTIALLY IMPLEMENTED: real active-versus-shadow comparability. Existing PA/SMC
lab records lack full effective fingerprints, exact native HTF identity and
finality/lookback attestations. Trading-instance records are signals-only. The
adapter reports these limitations and excludes unsupported pairs. No actual
source is labelled FULL_DECISION_EVIDENCE merely because it has evaluation rows.

BLOCKED: PostgreSQL research/shadow worker claims (no such backend exists),
authorized production-shaped/staging migration and restart validation, external
worker supervision, hard resource isolation and complete active evidence.
Local PostgreSQL identity/outbox tests are not PostgreSQL worker certification.

No strategy logic, risk setting, account, balance, position, trading order or
financial journal row is changed. No signals are converted into fills, trades,
R outcomes or profitability. No staging/production access, deployment, Git
staging, commit or push occurred. XRP profitability remains unverified.

## Repository audit and baseline

Read Sprint 7, 6, 5.6, 5.5, 5, 4.6 and production-validation reports, and inspected
the actual active lab persistence, instance decision store, shadow/router/store/
worker, public feed, research engine, journal migrations, tenancy, APIs and UI.
The checkout already contained 51 changed/untracked paths from earlier sprints;
these were preserved. The file list below describes this sprint, not ownership
of the entire diff from HEAD `7dd5e9a`.

Actually executed pre-change root baseline: 3,526 passed, 0 failed, 25 skipped,
85 warnings (353.59s). Pre-change `/usr/bin/chromium` browser suite: 140 passed,
0 failed, 0 skipped (6.0m). Historical prompt counts were not substituted for
current executions.

Audit findings:

- PA `record_evaluation` writes a closed-candle payload, original trace,
  proposal IDs, partial saved settings and feed state. Later order lifecycle
  advancement mutates `state/reason/payload.lifecycle`. Original payload facts,
  not final execution states or orders, are the decision source.
- SMC `record_evaluation` retains the native `source_evaluation`, partial saved
  settings and material MTF evidence. Native ENTRY_READY bullish/bearish reports
  provide direction; PARKED is not a neutral decision. Missing native closed-bar
  context blocks evaluation. No SMC evaluator is invoked by the adapter.
- Instance `decisions` are emitted signal/acceptance/rejection records, not a
  complete no-signal opportunity journal. Owner and forward-paper source filters
  exclude replay/research rows. Exact candle close/context can be absent.
- Legacy lab tables have no tenant columns. They are accessible only to the
  explicitly bound legacy owner when multi-user mode is disabled; in multi-user
  mode they are unavailable rather than assigned invented ownership.
- The existing public `ForwardPaperMarketDataHub` multiplexes Binance USD-M
  finalized native candles. The in-app worker reuses it. The separate entrypoint
  necessarily has its own process-local public hub; it does not claim shared
  cross-process connections. No additional provider or execution engine exists.
- Native 5m, broker-free ATP/PA evaluation, reviewed lookbacks, conservative HTF
  close alignment, snapshot isolation, leases and generation fencing already
  exist. Generic SMC remains unsupported; reading its saved lab decisions does
  not certify an SMC shadow adapter.
- Research/shadow queues use SQLite BEGIN IMMEDIATE, not PostgreSQL SKIP LOCKED.
  PostgreSQL is available locally through disposable Docker fixtures, but no
  equivalent production worker storage interface exists to certify.
- Top-level `0002_shadow_research.sql` is legacy unscoped SQLite DDL discovered
  by the PostgreSQL identity runner. Discovery now excludes it without deleting
  the file or changing any already-created tables.

## Architecture and isolation boundary

`persisted active decisions (read-only snapshot) + committed shadow observations
→ canonical candle/identity/evidence gate → bounded matched timeline → descriptive
decision metrics → transactional immutable artifact → authenticated dashboard`.

The active adapter uses SQLite URI `mode=ro`, `PRAGMA query_only=ON` and read
transactions. It cannot create a missing source database, invoke an active
evaluator or receive a trading instance/order client. Comparisons read normalized
source fields only and write solely to the isolated shadow decision namespace.
The shadow and active stores cannot share an atomic snapshot: artifacts explicitly
record separate bounded read transactions and retain the exact normalized input
copies and audit events used. Upstream mutable lifecycle changes cannot rewrite
an existing artifact; a new request/artifact captures later evidence.

Service-level isolation is not a hard OS/security sandbox. The in-app worker
shares the application process. The CLI imports no app, webhook execution,
broker or financial runtime and checks candidate DB read-only access and financial
DB alias/table guards before opening its writable shadow store. It does not
sanitize an operator's entire inherited environment or create an OS security
principal. External credential stripping/resource controls remain prerequisites.

## Active source coverage and missing semantics

| Source | Classification | Facts and limitations |
| --- | --- | --- |
| PA LIVE_PAPER session | PARTIAL_DECISION_EVIDENCE | Original closed candle; explicit empty proposal list means NO_SIGNAL. Proposal IDs alone do not establish direction. Full fingerprint/venue/finality/HTF/lookback unknown. |
| SMC LIVE_PAPER session | PARTIAL_DECISION_EVIDENCE | Original native ready proposal direction or watching/missing-context state. Full market input/fingerprint attestations unavailable. |
| Trading instance forward-paper decisions | SIGNALS_ONLY | Emitted direction and rejection evidence; not every scheduled opportunity. No absent-order inference. |
| Missing/incompatible DB | UNAVAILABLE | Explicit unavailable reason; no synthetic rows. |
| Legacy labs in multi-user mode | UNAVAILABLE/not exposed | Ownership cannot be established. |

Missing active rows are NO_DECISION_RECORDED, never NO_SIGNAL. Unknown historical
pause state is not inferred from current session status. Explicit ACTIVE_PAUSED,
ACTIVE_STOPPED, stale/missing-context/warm-up/data/error/unknown states remain
excluded; none are trading rejections or eligible neutral decisions. Malformed
saved JSON becomes EVALUATION_ERROR with incomplete evidence, retaining the
known candle opportunity when its timestamp is valid. Raw malformed text is
never logged or interpreted as executable input.

## Comparison eligibility and exact identity

The requested observation interval is UTC, aware, half-open `[start,end)`, at
most seven days and 2,016 native opportunities. The API rejects future windows.
Matching uses owner/tenant, venue, market type, symbol, timeframe and exact
normalized candle-close time. Strategies keep different source/config identities;
different fingerprints are expected, not merged. An active fingerprint/version
change inside a comparison blocks that window rather than mixing cohorts.

Mandatory pair checks require authorized observation, valid strategy ID/version
and SHA-256 fingerprint, complete decision evidence, finalized fresh data,
verified lookback and HTF alignment, context identity and decision time not before
close. Each candle must have valid, finite, aligned canonical open-time/OHLCV
material and open+interval=close. Verified canonical equality tolerates timestamp
representation and integer/float differences; conflicting prices/volume do not
match. Bare hashes without canonical material remain unverified.

Duplicate identical records represent one logical observation. Conflicting
records at the same opportunity are excluded, never arbitrarily selected.
Unresolved retained shadow candle-revision conflicts conservatively withhold
all agreement metrics, including conflicts discovered after the window. This
may overexclude unrelated windows; no automatic conflict resolution is claimed.
Old Sprint 7 rows lacking new attestations remain incomplete and unmodified.

COMPARABLE requires every scheduled pair eligible and full source coverage.
PARTIALLY_COMPARABLE permits only explicitly eligible pairs; incomplete source
coverage is still displayed. NOT_COMPARABLE has no eligible evidence or a critical
conflict. UNKNOWN applies when evidence/denominators cannot be established.
These statuses are not qualification or trading authorization.

## Agreement formulas and forward intelligence

For eligible matched pairs let `N` be their count, `L` both-long, `S` both-short,
`Z` both-no-signal. All-eligible agreement is `(L+S+Z)/N`; disagreement its
complement. Signal-relevant denominator is `N-Z`, and agreement `(L+S)/(N-Z)`.
Zero denominators return null/UNAVAILABLE, not zero performance. The shared
neutral count is disclosed alongside both rates. Active-only, shadow-only and
opposite signals are distinct counts; partial exits/trades are not involved.

Missing-decision rate is missing active records / requested scheduled candle
opportunities. Excluded opportunities are scheduled count minus matched eligible
count. Eligible-only breakdowns include session, regime, volatility, symbol,
timeframe and direction pair. Unknown classifications stay UNKNOWN; disagreeing
known classifications form an explicit mismatch subgroup. Rejection and missing
condition distributions preserve actual saved reasons. No causal inference,
statistical confidence or profitability is generated.

## Coverage, latency and reliability

Separate ratios use the explicit requested native candle clock denominator:
recorded finalized canonical snapshots (market data), successful complete shadow
decisions passing shadow identity/quality checks (shadow evaluation), recorded
active opportunities (active records),
eligible matched pairs (comparison). Authorization, pause/offline and warm-up
periods remain visible; scheduled clocks do not prove exchange availability.
Active-record coverage does not attest full decision completeness.

New shadow rows record evaluation start/completion and the host timestamp
recorded inside evidence commit. Duration uses existing monotonic timing.
Artifact summaries include sample count/mean/max/nearest-rank p95 of known
durations and host close-to-commit timing. Commit timestamp is not a filesystem
fsync acknowledgement; host clock skew is unmeasured. Per-candle exchange data
receipt and exchange-to-system latency remain UNKNOWN. A snapshot read timestamp
is not falsely relabelled exchange publication time.

Retained registration audit counts show gaps, persistence failures, duplicate
events and worker attachments, explicitly ALL_RETAINED_REGISTRATION_EVENTS—not
window uptime/duration. Continuous feed uptime and exact disconnected/stale/
downtime durations remain unknown. Existing leases/recovery record gaps and
never backfill missed forward observations. Evidence read limits fail explicitly
rather than silently truncating history.

## Immutable evidence and recovery

Logical artifact identity includes owner, registration, active source, normalized
window, calculation version, exact input watermark and source descriptor.
Owner/idempotency request binding and artifact insert share a short SQLite
transaction. Separate connections racing commit one logical artifact. Same key
with different material fields conflicts; same key replays its original artifact
even after upstream evidence changes. Immutable triggers reject artifact/request
update/delete. A new evidence revision produces a new artifact, not a rewrite.

Exact input copies and audit references permit recomputing the versioned
calculation: call `compare_decisions` with retained active/shadow inputs, recorded
period, owner and registration timeframe; apply recorded source-coverage and
unresolved-conflict exclusions. Retain matching software to reproduce older
versions. No automatic historical software reconstruction is promised.

## Worker supervision and SMC safety

IMPLEMENTED/locally tested: `python -m services.shadow_worker_main` with explicit
operator `--shadow-db`, `--journal-db`, `--market-data-dir`; optional `--once` is a
bounded readiness smoke check. Long-running mode exposes structured health on
stdout every five seconds, concurrency one, sweep/heartbeat freshness and
error category. SIGTERM/SIGINT request cooperative shutdown, subscription release
and lease release; acknowledgement timeout exits nonzero. Existing expiration,
fencing, restart gap and duplicate safeguards remain in force.

NOT IMPLEMENTED: an installed systemd/container supervisor, HTTP health server,
OS CPU/memory/network sandbox, durable standalone *research* worker service or
PostgreSQL shadow worker backend. Entrypoint paths are trusted operator inputs,
never accepted via the API. Generic SMC shadow evaluation remains BLOCKED because
no source-version-equivalent broker-free adapter is certified. Saved native SMC
decision consumption is read-only and does not weaken any SMC confirmation gate.

## Dataset retention and reconstruction

The existing research journal now retains content-addressed canonical simulator
input, including warm-up, in `retained_research_datasets` and immutable job/owner
references. No new storage platform/provider/backtester exists. SHA-256 covers
the exact chronologically preserved canonical input. Duplicate input is not
silently removed or sorted after execution. Retry reconstructs the retained
revision before attempting a provider fetch and verifies content hash/identity.

Metadata distinguishes RETAINED, HASH_VERIFIED and RECONSTRUCTABLE. Missing legacy
snapshots remain UNKNOWN; a hash alone does not prove reconstructability. A
stored input is not a completed result and cannot qualify a queued/cancelled job.
This retains actual backtester input, not nonexistent native HTF/publication
evidence. Funding and cost completeness retain existing UNKNOWN semantics.

Conservative limits: 15,000 candles, 4 MiB/input, 64 MiB/global canonical payload,
32 MiB/owner charged references, 1,000 references/owner. These are bounded local
defaults, not hard OS memory limits or complete DB/WAL/disk quotas. Expiry/garbage
collection is NOT IMPLEMENTED; quota exhaustion fails closed. Backup and retention
approval must cover both snapshots and referencing validation metadata. Never
delete financial evidence to free space. Exact input reconstruction was tested
with provider re-fetch prohibited; altered input cannot overwrite the revision.

## Migrations and compatibility

- `migrations/shadow/0004_shadow_comparisons.sql`: additive isolated artifact and
  request tables, owner/time index, foreign keys, immutable triggers.
- `migrations/research/0001_dataset_retention.sql`: additive research-journal
  snapshot/reference tables, owner index, foreign keys and immutable triggers.
- PostgreSQL discovery excludes existing SQLite `0002_shadow_research.sql`;
  no PostgreSQL financial/identity table changes are introduced by this sprint.

Fresh/repeated local SQLite initialization, representative prior rows,
interrupted additive migration forward recovery, integrity and foreign-key checks
are exercised. Existing production-shaped historical journal tests remain in
the validation set. Local PostgreSQL 16 identity DDL preserves representative
rows and interrupted transactions roll back. Outbox/PostgREST integration tests
exercise their existing financial evidence boundary, not shadow worker claims.

Order: existing shadow initialization/0003 before 0004; existing journal context
schema before retention. Back up each DB with a consistent SQLite backup/WAL
procedure before authorized upgrade. Roll back code while retaining additive
tables; do not drop evidence or triggers. Forward recovery reapplies idempotent
DDL. Initialization can partially install additive SQLite DDL before an error;
successful rerun is tested, not advertised as globally atomic schema migration.
Already-applied unscoped PostgreSQL shadow tables require separately authorized
tenant/data-preservation review; they are not automatically dropped or migrated.
Large-table growth/backup latency and actual staging schema remain unvalidated.

## APIs and dashboard

Under authenticated `/api/v2/strategy-intelligence`:

- GET `/shadow/active-sources` and `/{source_id}/coverage`.
- POST/GET `/shadow/comparisons`; GET `/{comparison_id}` and
  `/{comparison_id}/timeline` with bounded pagination.
- GET `/research-jobs/{job_id}/dataset` exposes owner-scoped retained metadata,
  not unrestricted candle paths or data downloads.

Existing v1 shadow comparison/dashboard metrics are preserved. v2 has explicit
denominators and completeness; it does not silently replace old formulas.
Authentication, no-store responses, tenant scoping, repeated-query rejection,
request field allowlists, idempotency and bounded request/artifact quotas apply.
No execution routes, secrets, arbitrary paths or code invocation are added.
Comparisons are bounded read/calculation operations, not a new research queue.
Limits also include 1,000 retained registration audit events per input snapshot,
4 MiB per comparison artifact, 1,000 idempotency bindings/owner and 10,000 global
artifacts. A seven-day request may hit the byte/evidence cap earlier; it fails
explicitly rather than shortening the interval or dropping required inputs.

The Shadow Lab now has persisted-source selection, source limitations, UTC
window inputs, comparison creation/history, eligible/excluded decision timeline,
both agreement rates, descriptive subgroup tables, coverage/reliability and
immutable evidence details. Nulls visibly remain UNKNOWN/UNAVAILABLE. Result
display requires a durable artifact. Initial active comparison may be unavailable
for the exact audit reasons above; no fake example market statistics are seeded.

## Files modified/added for this sprint

Hub services: `active_decision_evidence.py`, `shadow_comparison_service.py`,
`shadow_worker_main.py`, `research_dataset_retention.py` (new);
`shadow_observation_store.py`, `shadow_observation_worker.py`, `shadow_router.py`,
`research_jobs.py` (extended).

Integration: `routers/strategy_intelligence.py`, `webhook_api.py`,
`data/journal_context_migrations.py`, `data/pg_migrations.py`,
`migrations/README.md`, the two SQL migrations above and this report.

Dashboard: `src/pages/ShadowComparisons.tsx` (new),
`src/pages/ShadowStrategyLab.tsx` (integration/readiness label),
`e2e/shadow-comparison.spec.ts` (new).

New tests: `test_shadow_comparison_intelligence.py`, `test_shadow_operations.py`,
`test_shadow_comparison_api.py`, `test_research_dataset_retention.py`,
`test_shadow_postgres_migration_routing.py`. Existing prior sprint tests are
preserved, not weakened or deleted.

## Validation results

All final production-code changes were validated. Full repository root:
3,600 passed, 0 failed, 15 skipped, 89 warnings (361.76s). Final full Hub:
3,070 passed, 0 failed, 15 skipped, 90 warnings (367.82s).
PostgreSQL/PostgREST fixtures were opted in with
`NEXUS_PG_OUTBOX_TEST=1`; these replace ten prior optional skips with executed
tests, and include two new PostgreSQL migration tests.

| Final check (overlapping selections, not additive totals) | Passed | Failed | Skipped | Warnings |
| --- | ---: | ---: | ---: | ---: |
| New Sprint 8 focused backend | 64 | 0 | 0 | 0 |
| Full Automation Hub | 3,070 | 0 | 15 | 90 |
| Full repository root | 3,600 | 0 | 15 | 89 |
| Database/recovery selection | 211 | 0 | 0 | 4 |
| Chromium full suite | 142 | 0 | 0 | 7 CLI/build notices |
| Dashboard typecheck/build | PASS | 0 | 0 | 1 build advisory |
| `git diff --check` | PASS | 0 | 0 | 0 |

The 15 remaining skips are existing risk-parity cases: eight are not validity
scenarios and seven are comparable only when both engines approve. They are
not skipped browser or PostgreSQL server tests. PostgreSQL *research/shadow*
claim/heartbeat/fencing tests remain BLOCKED/NOT EXECUTED because no PostgreSQL
worker storage backend exists; they are not included in the passed totals.
Staging migration/soak/restart, external supervision and hard sandbox tests were
not run and have no claimed pass count. An earlier Hub run was intentionally
interrupted for the final stale-data coverage correction; its partial counts
are excluded. Both complete backend suites ran after that correction.

Database/recovery selection: 211 passed, 0 failed, 0 skipped, 4 warnings
(34.98s). This explicitly ran production-shaped historical-row compatibility,
financial evidence/outbox/PostgREST, recovery, migration routing, shadow worker/
router/API/operations, research job/retention and comparison tests. It includes
the actual server tests; it is not an authorized staging validation.

Focused new Sprint 8 tests: 64 passed, 0 failed, 0 skipped, 0 warnings (14.26s),
including two real disposable PostgreSQL identity-migration cases. Dashboard
`npm run build:check`: typecheck and production build passed (Vite 6.41s), one
existing bundle-size warning. Approved Chromium full suite: 142 passed, 0 failed,
0 skipped (5.9m), with six Node color-environment warnings and one existing Vite
chunk-size warning; no page/console errors in the new flows. Desktop and 390px
mobile output were inspected. No browser tests were skipped or assertions weakened.
The mobile assertion waits for the existing sidebar transition to settle, so
screenshots and readiness checks do not capture an intermediate layout frame.

New tests cover missing/neutral distinctions, exact metrics, all-neutral/null
denominators, canonical identity/conflicts, stale/paused/errors, version/tenant/
category isolation, source read-only access, native SMC report normalization,
malformed payloads, latency clocks, immutable idempotency, independent-connection
races, artifact recomputation, actual financial-state snapshots/zero execution
calls, retained dataset identity/owner/quotas/provider-independent recovery,
worker health/process shutdown and additive migration interruption/reruns.

Browser tests use the approved `/usr/bin/chromium`, real built React runtime and
synthetic authenticated API contract fixtures (not claimed live market evidence).
They assert exact denominators, missing/excluded decisions, subgroup output,
artifact traceability, unavailable reasons, no invented agreement, console
cleanliness and mobile controls; existing browser regressions also run.

Local synthetic pure-calculation benchmark: 2,016 eligible opportunities,
three runs 155.14/159.30/155.70 ms. This excludes database, provider, strategy
evaluation and network latency, and is not a production capacity certification.

### Warning audit

No warning filters or meaningful assertions were removed or weakened. In the
final Hub run, four FastAPI `on_event` deprecations and 82 aiohttp
`enable_cleanup_closed` deprecations occur in existing app/runtime fixtures.
The latter is ignored by aiohttp because this Python 3.12.14 runtime already
contains the CPython fix; it is not evidence of a newly leaked connector.
Four Supabase/PostgREST `timeout`/`verify` deprecations surface when the existing
opt-in gateway tests run; these were skipped in the baseline root run.
New focused Sprint 8 tests emit zero warnings. These are existing API/dependency
deprecations, not suppressed correctness failures. A separately reviewed
lifespan/client dependency upgrade is preferable to changing execution startup
semantics inside this evidence sprint.
The root run has 81 aiohttp warnings rather than the Hub's 82; both have the
same four FastAPI and four Supabase deprecations. No new warning class appeared.

Frontend warnings are the existing Vite chunk-size advisory and six Node
NO_COLOR/FORCE_COLOR notices. They do not reflect JavaScript page errors; new
browser flows explicitly collect and reject console/page errors. Production
bundle splitting and dependency upgrades remain separate follow-up work.

The onboarding skill saved these tested validation/standalone startup notes in
the cloud environment draft, preserving installation, repositories, secrets and
network settings. The draft is unpublished; review/save and publish the
environment separately to activate it. This is not application deployment or
proof of fresh-task restoration of the uncommitted implementation.

## Operational blockers and Sprint 9 recommendations

BLOCKED/NOT VERIFIED: actual staging/production schema migration, PostgreSQL
research/shadow atomic claims/heartbeat/fencing, external service supervision,
hard resource sandbox, complete immutable active decision capture, exact native
HTF/source availability attestations, live feed soak/recovery, backup restore,
large-data storage management and production dataset retention. Production/staging
access was not requested or performed. Passing local Docker identity/outbox
tests does not resolve these blockers.
No fresh live exchange feed soak or publication-latency certification was
performed: feed dispatch/recovery uses the existing implementation with controlled
test hubs, and browser APIs use labelled contract fixtures. Health of a local
empty worker sweep does not attest market-data readiness for a candidate.

Sprint 9 should introduce separately authorized complete immutable active
decision capture without changing evaluation/order semantics; define tenant
ownership for legacy labs; implement/test a real PostgreSQL worker backend or
explicitly approve the SQLite operational topology; supervise and sandbox the
tested entrypoint; validate staging migration/restart/backup recovery; approve
snapshot retention/quotas; and certify any additional strategy-specific adapters
through equivalence tests. No automatic champion/trading promotion is implied.
