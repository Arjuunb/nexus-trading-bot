# Sprint 9 — Forward evidence qualification

## Executive summary

**IMPLEMENTED:** advisory forward-decision qualification, versioned policies,
immutable bounded observation windows and reports, exact-cohort read-only paper
episode references, human review, history, authenticated APIs and dashboard.
This workflow consumes existing evidence; it never evaluates a trading strategy,
places an order, starts a paper account or modifies execution settings.

**TESTED:** local SQLite integrity/recovery, financial non-mutation, committed
native active/shadow matching, additive migrations, backup/restore, bounded
retained datasets, a real standalone worker, Docker supervision and kernel
resource limits. Final regression results are recorded below.

**PARTIALLY IMPLEMENTED:** paper-trial readiness and paper-outcome qualification.
The existing journal supplies actual episode/cost references but cannot prove
external ledger completeness or missing execution costs. Continuous worker
availability and holdout independence remain UNKNOWN when not attested by
original evidence. These gaps block a complete readiness recommendation.

**BLOCKED:** PostgreSQL research/shadow worker concurrency: these workers have
no PostgreSQL persistence/claim backend. Authorized staging migration/restart
validation and production supervision/credential separation remain outstanding.

**NOT IMPLEMENTED:** trading activation, promotion, new simulation/queue systems,
profitability inference from signals, fabricated historical backfills, automatic
deployment. Generic SMC shadow support remains blocked without changing rules.

## Repository audit and working-tree reconciliation

HEAD at audit: `7dd5e9a`. The actual initial checkout contained 23 modified
tracked files and 77 modified/untracked paths. The supplied 19-file count was
not the current status. Sprint 8.5's 14 reported contribution paths describe six
new files and eight extensions within accumulated Sprint 3–8.5 work, rather
than the entire diff from HEAD; all 14 existed. Initial hashes and the tracked
diff were saved in `/tmp/sprint9-initial-worktree.json` and
`/tmp/sprint9-initial-tracked.diff`. Sprint 9 adds 15 files and extends three
existing dirty files: 18 contribution paths. Other initial changes are preserved.

Read Sprint 8.5, 8, 7, 6, 5.6, 5.5 and production-validation reports, then
inspected native PA/SMC capture, active adapters, shadow router/store/worker,
comparison service, episode evidence/completeness, research qualification and
leases, retained datasets, authentication, routes, dashboard and tests.

| Existing component | Finding and reuse |
| --- | --- |
| `ShadowObservationStore` | Separate SQLite decision/audit store; `BEGIN IMMEDIATE`, immutable evidence and fenced worker leases |
| `ActiveDecisionSources` | Read-only native PA/SMC receipts preserve original effective configuration/candle/context; historical decisions remain partial and Trading Instances signals-only |
| `compare_decisions`, `shadow_comparison.v2` | Exact finalized-candle matching, explicit exclusion reasons and agreement denominators; no execution or P&L |
| `build_episode` | Existing constituent-fill episode reconstruction; one completed episode is one statistical trade, including partial exits |
| `JournalContextStore` | Original completed validation-run and holdout references; first access alone is not independence |
| `ResearchDatasetRetention` | Immutable exact-input snapshots with owner/job references and content verification; hashes alone are not retention |
| `shadow_worker_main` | Existing independently startable public-data observer and graceful shutdown; enhanced health and local supervision |
| Research/shadow worker persistence | SQLite only; real local PostgreSQL financial outbox tests certify a different boundary |

The audit plan was completed in dependency order: current baseline, pure
qualification/contracts, additive immutable storage, original evidence readers,
owner-scoped APIs/UI, disposable operational tests, final regressions/diff.
No previous strategy, financial, accounting, risk or market-data source is edited.

Actual pre-edit baseline: repository root **3,648 passed, 0 failed, 15 skipped,
90 warnings, 403.19s**; Chromium **144 passed, 0 failed/skipped, 4 environment
warnings, 5.8m**. Root includes the Hub suites. A separate pre-edit Hub run was
not executed. Existing disposable PostgreSQL outbox tests were enabled.

## Evidence taxonomy

The machine-readable taxonomy is returned with policies. Each type declares
source/configuration/data identity, half-open UTC period, completeness,
execution assumptions, cost coverage, reliability and qualification eligibility.

| Category | Meaning in forward qualification |
| --- | --- |
| `HISTORICAL_BACKTEST` | Historical simulation, separate from forward observations |
| `OUT_OF_SAMPLE_VALIDATION` | Frozen chronological research/provenance references |
| `WALK_FORWARD_VALIDATION` | Frozen window research/provenance references |
| `HYPOTHETICAL_SHADOW_OUTCOME` | Hypothetical versioned assumptions; never an actual fill |
| `FORWARD_SHADOW_DECISION` | Observation without execution; behaviour only |
| `FORWARD_PAPER_TRADE` | Actual isolated completed paper episodes, conditional on full lineage/cost authority |
| `LIVE_TRADE` | Requires authoritative live fills; not consumed by this sprint |
| `ACTIVE_DECISION_OBSERVATION` | Persisted original decision, not proof of execution |

Historical categories are ineligible for **forward profitability** in this
contract; this does not redefine existing historical analytics. Only shadow and
active decision categories create observation windows. Paper references are a
separate exact cohort. No dashboard P&L formula or earlier qualification policy
is changed. Reports always state `profitability=NOT_ASSESSED`,
`profitability_verified=false`, `execution_authorization=false`.

## Versioned qualification policy and outcomes

`ForwardQualificationPolicy` is frozen, validated and fingerprinted using the
existing canonical SHA-256 utility. A changed payload under an installed policy
version fails with `POLICY_VERSION_CONFLICT`. HTTP callers select an installed
version and cannot assert verification, supply costs or weaken thresholds.
Trusted service construction can install a reviewed additional version.

| Default `forward-qualification.v1` gate | Threshold |
| --- | --- |
| Elapsed native observation period | 7 days |
| Expected opportunities | 1,000 |
| Eligible/expected coverage | At least 95% |
| Missing decisions / expected | At most 5% |
| Stale/error opportunities / expected | At most 1% each |
| Duplicate/conflicting evidence / expected | 0 |
| Unexplained worker downtime | At most 300 seconds, requires actual measurement |
| Matched opportunities, when requested | At least 100 |
| Signal-sample warning | Fewer than 30 |
| Paper episode sample | At least 30, with all required evidence |

`forward-qualification.strict.v1` uses 99% coverage, 1% missing data and no
stale/error tolerance. Configuration, candle/context, reviewed lookback, tenant
authorization, holdout provenance and outcome cost gates are explicit.
Every check has PASS/FAIL/UNKNOWN, purpose and reason. Reasons and original
evidence references are retained with the selected policy.

These are conservative engineering/research defaults for the existing 5m
observer, not statistically validated guarantees. At slower timeframes the
seven-day/1,000-opportunity default can remain insufficient; a reviewed policy
version must suit the native clock. Windows are capped at seven days/2,016
opportunities to reuse existing comparison/resource limits. Longitudinal
analysis preserves separate windows rather than bypassing this bound.

Outcomes: `FORWARD_EVIDENCE_SUFFICIENT`, `FORWARD_EVIDENCE_INSUFFICIENT`,
`FORWARD_EVIDENCE_UNRELIABLE`, `BEHAVIOUR_ONLY`, `NOT_COMPARABLE`, `UNKNOWN`.
Complete decision evidence without verified outcome/operational provenance can
yield BEHAVIOUR_ONLY; a short period fails the duration/sample gates. Invalid
identity/context and critical conflicts fail closed. An empty/unavailable
history is UNKNOWN. Signal frequency or agreement never becomes profitability.
The sufficient outcome requires every applicable mandatory gate to pass; this
implementation deliberately cannot attest absent operational or paper facts.

## Immutable observation windows and evidence commitment

Aligned UTC intervals use candle **close** timestamps and `[start, end)`.
Future, off-clock, foreign-tenant, mixed-category/scope, invalid canonical
candle, uncommitted, warm-up, stale, paused/stopped and error observations are
excluded explicitly. Missing records stay `NO_DECISION_RECORDED`; NO_SIGNAL is
an original valid decision. Conflicting outcomes on the same known canonical
candle exclude the decision but retain the known candle receipt.

Windows freeze owner/source, original strategy version/fingerprint, market
scope, period, native clock, projected decision facts, original decision and
context identities, configuration snapshots, audit events, paper/holdout
references, calculation version and hashes of relevant software components.
The calculation retains only context labels actually used, rather than copying
indicator histories into every row. Context hashes remain original references
to full immutable source evidence; no partial context is rehashed as an
authoritative original context.

Identity derives from source/owner/period, evidence SHA-256 and software
revision. Policy reassessment shares the same window but gets a different
report. Changed evidence/software creates a new window. Corrections can refer
to a prior same-owner/identity report with `supersedes_report_id`. Prior reports
and windows cannot be updated/deleted through the service or ordinary SQL.

Active and shadow inputs are separate bounded read snapshots, **not** a claimed
cross-store atomic capture. The frozen calculation facts make the recorded
report reproducible; original source evidence and matching software are needed
for a fresh provenance audit. Hashes do not retain software or external data.

Commitment uses the existing shadow transaction: window, report, request binding
and DRAFT/EVALUATING/COMPLETED lifecycle audit are atomic. A write interruption
rolls back the entire logical commitment. Retries and independent SQLite
connections commit one logical report; conflicting content is rejected.
Preview is DRAFT/NOT_COMMITTED and supplies no completed qualification evidence.
This bounded read calculation does not create a new research job queue.

## Coverage and reliability formulas

Let `N=(end-start)/native timeframe seconds`, using the requested aligned clock;
`E` is eligible committed decisions; `M` is opportunities without any record;
`F` is opportunities with a valid received finalized canonical candle.

| Metric | Numerator / denominator or meaning |
| --- | --- |
| Market-data coverage | `F/N`; evidence-backed receipt proxy, not exchange feed uptime |
| Decision evaluation coverage | `E/N` |
| Active/shadow evidence coverage | `E/N` for that side; other side unavailable |
| Evidence commitment completeness | Verified committed decision opportunities / `N` |
| Missing-data rate | `M/N`; does not identify why the record is missing |
| Stale-data rate | Recorded stale opportunities / `N` |
| Evaluation error rate | Original EVALUATION_ERROR opportunities / `N` |
| Duplicate/conflict rate | Extra identical records plus conflicted opportunities / `N` |
| Data freshness compliance | Fresh recorded rows / received decision rows; UNKNOWN when none |
| Unobserved opportunity duration | `M * timeframe seconds`; not proven worker downtime |
| Signal frequency | Eligible long/short decisions / `E`; unavailable with no eligible decisions |
| Direction share | Long or short signals / signal count; unavailable with no signals |

Recorded observation-gap events are included only when their interval/event
time overlaps the window; unlocatable gaps are separate. They do not prove
continuous downtime duration. Worker availability, unexplained worker downtime
and recovery success rate remain UNKNOWN without continuous operator evidence.
No fake percentage is derived from a healthy current process or a missing
decision. Worker downtime is therefore a mandatory unknown readiness gate.

Signal/session/regime/volatility distributions are descriptive. Rejections and
missing conditions retain original labels. Sample/temporal-dependence warnings
are explicit; neither rate is a significance claim. Current operator health
omits other tenants' registration counts and worker identity from this API.

## Matched forward comparison qualification

The existing comparison engine matches owner, venue/market, symbol/timeframe,
native close time and verified canonical candle, using original committed
active/shadow configurations. Different strategy fingerprints are permitted.
Partial sources stay partial, missing sides are not neutral, and conflicting
revision/source references prevent a valid recommendation.

Behavioural classifications are `BEHAVIOUR_COMPARABLE`, `PARTIALLY_COMPARABLE`,
`NOT_COMPARABLE`, `UNKNOWN`, with policy sample checks and exact exclusions.
All-eligible agreement is `(both long + both short + both neutral)/matched`.
Signal-relevant agreement is `(both long + both short)/(matched - both neutral)`.
Zero signal denominator is UNKNOWN. Neither metric ranks profitability.

## Forward-paper integration and human review

**PARTIALLY IMPLEMENTED:** the API resolves the owned instance/account/session
using existing authority, then opens the existing journal read-only. It filters
original strategy/version/configuration/symbol, paper mode, forward-paper source
and instance/account/session exactly. Existing episode reconstruction consumes
actual execution-fill events; closure in `[start,end)` controls episode selection.
Partial exits remain constituents of one episode. Decimals, recorded fees,
funding and fill lineage are preserved; no synthetic transactions are created.

Queries are capped at 200 episode headers and 2,000 fill events; overflow fails
incomplete instead of silently claiming complete performance. Missing entry/exit
IDs, costs, slippage/spread, original authority or journal completeness remain
unknown. Modelled funding is not observed funding, and claimed exempt/verified
zero costs require a recorded zero. The reader cannot independently certify an
external ledger or synthesize fields the journal lacks. It reports actual
references and count, not an unsupported aggregate P&L or trading qualification.
Historical reported XRP performance remains unverified here.

Original completed OOS run references must match owner/version/fingerprint.
First holdout access alone never proves independence; unknown original search
history stays UNKNOWN and reused/contaminated lineage is preserved.

Human review is a separate authenticated owner action: ACKNOWLEDGED, REJECTED,
REQUEST_MORE_EVIDENCE or ARCHIVED, with reviewer, reason, timestamp and original
report. Reviews append; the report never changes. Archived reviews cannot be
edited. Every review states execution_authorization=false. A human-reviewed
paper-trial recommendation requires behavioural, matched (if selected), holdout
and operational gates; absent worker timing blocks it. No route starts a trial.

## Longitudinal analysis and resource limits

History preserves individual report/window identities and configuration cohorts.
Overlap discovery is atomic with commitment, scoped by owner and exact strategy
configuration/market/category. Later reassessment cannot rewrite earlier as-of
lineage. History also flags repeated policy assessments of the same window.
Overlapping or reassessed windows are never summed as independent samples.
Coverage/errors/signal trends are displayed per immutable row.

Each process permits one simultaneous assessment; overflow returns a bounded
busy error, not another background computation. Maximum window/report payload
is 8 MiB, retained windows 64 MiB globally/32 MiB per owner, request bindings
2,000 globally/200 per owner, reviews 1,000 per owner. History pages are at most
100 and decision pages 200. Application bounds are cooperative, not an OS
sandbox. Deployment sizing and longer retention require operator review.

## APIs and dashboard

All routes reuse `/api/v2/strategy-intelligence`, authentication, owner scoping,
no-store responses and repeated-query rejection. Models forbid extra fields.
No unrestricted path, execution account selector, secret or code is accepted.

| Forward qualification suffix | Behaviour |
| --- | --- |
| `GET /policies` | Installed versions and evidence taxonomy |
| `GET /sources` | Bounded owned active/shadow sources |
| `POST /eligibility` | Read-only NOT_COMMITTED preview |
| `POST /reports` | Idempotent immutable commitment |
| `GET /reports`, `/history` | Bounded history and per-cohort longitudinal rows |
| `GET /reports/{id}` | Original report and append-only review state |
| `POST /reports/{id}/review` | Owner-scoped human evidence review |
| `GET /windows/{id}` | Original frozen evidence, paged decisions |
| `GET /operations` | Actual runtime limitations and readiness matrix |

The Forward Qualification tab uses actual API responses for source/policy/window
selection, eligibility preview, generation, coverage/reliability, signal and
matched evidence, unknown paper costs, review, original evidence and history.
Material edits invalidate a displayed preview. Incomplete responses cannot be
shown as committed reports. The Research Agent page links to these read-only
reports; no research approval or agent permission is broadened. Existing dark
theme controls are reused. Missing values display UNKNOWN. No activation button
or fabricated profitability appears.

## Worker supervision, PostgreSQL and resource isolation

The existing standalone observer supports a bounded operator-only health file.
It checks real candidate schema availability, recent successful sweep, actual
execution-runtime absence, PID and Linux process generation. A fresh old health
file cannot survive a reused PID after restart. Atomic replacement and a 16 KiB
limit preserve unrelated files. Graceful shutdown writes STOPPED. Readiness is
**process readiness**, not evidence, strategy readiness or market-feed uptime.
PostgreSQL DSNs fail closed before opening a misleading local path.

`ops/shadow-worker.local.compose.yaml` is an opt-in disposable test harness,
separate from application/production compose. The code-only image excludes
environment files, financial databases and app/webhook entrypoints. It has a
read-only candidate fixture, private observation directory, no network/ports,
no credential environment, non-root user, read-only rootfs, dropped capabilities
and no-new-privileges. Verified defaults: CPU 0.5, memory 384 MiB, 64 PIDs,
64 MiB temporary storage, logs bounded to 2 MiB, concurrency one and at most
two on-failure restarts. This is a credentials-free local supervision test,
not a networked production observer installation or full security certification.
These conservative limits are for the disposable local observer test; they do
not establish capacity for a fully populated set of candidates. Supervision of
the separate research worker and maximum-load sizing remain deployment work.

The integration test inspects actual cgroup v2 `memory.max`, `cpu.max` and
`pids.max`, kills the worker child under Docker's init process, verifies a new
worker/readiness generation, no fabricated observations and graceful shutdown.
Existing SQLite restart/claim/heartbeat/fencing tests remain enabled. Local
Docker supervision does not prove an external production supervisor is running.

The public wheel helper uses verified HTTPS/PyPI SHA-256 and exact sizes for four
pinned packages (setuptools 80.9.0, wheel 0.45.1, packaging 25.0, websockets
15.0.1). Docker builds offline with hash enforcement; no TLS/checksum bypass.
This fallback was required because container DNS could not resolve PyPI.
The validated wheel/image platform is Linux amd64, CPython 3.12.

Example local-only preparation (candidate DB must be a disposable fixture):

```sh
python ops/prepare-shadow-worker-wheels.py /tmp/nexus-sprint9-wheels
DOCKER_CONFIG=/tmp/nexus-sprint9-docker docker build --network=none \
  --build-context verified_wheels=/tmp/nexus-sprint9-wheels \
  -f ops/shadow-worker.Dockerfile -t nexus-shadow-worker:sprint9-local .
```

**BLOCKED:** PostgreSQL research/shadow worker claim, heartbeat, fencing and
cancellation-race tests. Local PostgreSQL is available and the existing
financial outbox/PostgREST migration tests run in disposable containers, but
there is no PostgreSQL worker backend to exercise. SQLite correctness is not
reported as PostgreSQL worker correctness. No external database was accessed.

## Migration, backup/restore and retention verification

New additive SQLite migration: `migrations/shadow/0005_forward_qualification.sql`.
It adds policy, window, report, idempotency, review and audit tables, owner/scope
indexes, foreign keys and append-only triggers. Existing shadow schema comes
first. Installation runs through the isolated shadow facade; financial-table
targets are refused **before** this migration. It is not routed to PostgreSQL.

Local tests cover fresh install, prior-shadow-schema upgrade, repeat installation,
interruption after earlier table creation and forward recovery, original shadow
row preservation, foreign-key check, report transaction rollback, duplicate
commit and SQLite backup/restore with committed reports/reviews intact.
Existing financial receipt/outbox and research/shadow migration/recovery tests
run unchanged. Disposable tests are not production-shaped staging certification.

Before an authorized upgrade, take SQLite backup-API snapshots of the shadow
store and independently preserve active/journal sources; do not copy only a
live SQLite main file while ignoring WAL. Recover partial additive installation
by rerunning the migration, then integrity/FK checks and replaying the same
idempotency key. Stop writers before restore. Rolling the application back can
leave unused additive tables intact; deleting committed evidence or a destructive
down migration is unsupported. Restore requires an operator-approved consistent
backup and accounting for newer evidence.

Existing `ResearchDatasetRetention` is verified by its regression tests:
content-addressed exact ordered canonical input, original manifest and owner/job
reference, retained payload retrieval and SHA-256 validation, mismatch rejection
and tenant/quota bounds. Limits are 15,000 candles/4 MiB per snapshot, 64 MiB
total, 32 MiB per owner and 1,000 owner references. Stored revisions can be
reconstructed; a manifest hash or unavailable/expired snapshot cannot. Provider
refetch is never substituted as the original revision. Storage is append-only
and quota bounded; automatic expiry/GC is not implemented. No new storage system
or financial evidence deletion is introduced.

## Tests, benchmarks and warning audit

All final suites below ran after the last source/test change. Intermediate runs
are not substituted for these results. All 57 new backend tests passed.

| Final validation | Passed | Failed | Skipped | Warnings | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Focused Sprint 9 backend | 57 | 0 | 0 | 0 | 34.43s |
| Automation Hub full suite | 3,175 | 0 | 15 | 89 | 448.98s |
| Repository-root full suite | 3,705 | 0 | 15 | 91 | 445.19s |
| Database/recovery/migration/restore/retention selection | 345 | 0 | 0 | 4 | 94.76s |
| Chromium full suite, `/usr/bin/chromium` | 147 | 0 | 0 | 4 environment warnings | 5.7m |
| Dashboard typecheck + production build | Passed | 0 | 0 | 1 existing bundle advisory | Build 8.33s |
| `git diff --check` | Passed | 0 | 0 | 0 | Completed |
| PostgreSQL research/shadow worker claims/fencing | Not executed | — | — | — | BLOCKED: backend absent |
| Authorized staging/production-shaped migration/restart tests | Not executed | — | — | — | BLOCKED: no authorization/access |

The 15 full-suite skips are unchanged conditional risk-engine parity scenarios
(`not a validity scenario` / `only comparable when both approve`), not skipped
new regressions or browsers. Disposable PostgreSQL outbox and Docker worker
tests were explicitly enabled, with no skip in the database selection.
That selection covers the four new test files plus existing production validation,
paper outbox/receipt, execution-receipt lookup, simulation restart, shadow
PostgreSQL migration routing/comparison/router/API/worker/operations, research
jobs/retention, native active capture/API and PA/SMC source-integrity suites.
Root includes Hub results; these counts must not be summed as independent tests.

The new real-browser contract tests exercise
preview, committed report, human review, evidence inspection, authorization and
stale-preview invalidation. They use explicitly labelled API fixtures and are
not evidence of live trading performance; genuine backend/journal integration
and financial non-mutation are tested separately.

The test-first checks exposed and corrected a financial DB target guard,
conflict/market-receipt denominator, missing-context gate, oversized indicator
history projection and reused-PID readiness. No assertion was weakened. The
first full browser run had one navigation failure because assets were rebuilt
while its preview server ran; the final run uses unchanged clean-build assets.

Reproducible local validation commands, with existing `.venv`/frozen `uv.lock`:

```sh
NEXUS_PG_OUTBOX_TEST=1 NEXUS_SHADOW_CONTAINER_TEST=1 \
  UV_CACHE_DIR=/tmp/uv-cache uv run --frozen pytest -q
# Run from repository root and separately from automation-hub.
cd automation-hub-dashboard
npm run build:check
PW_CHROMIUM=/usr/bin/chromium npm run test:e2e
```

The executed browser configuration imports the repository Playwright config,
preserves all tests/browser/timeouts, uses a non-reused preview server on port
4190 and writes results to `/tmp`. Browser assertions and skips are unchanged.
The new page was inspected in Chromium for DOM, runtime errors and screenshot.

Synthetic bounded-calculation benchmark, 2,016 projected opportunities, three
tracemalloc-instrumented runs under concurrent test load: **0.7407/0.8321/0.7689s**,
**785,223 bytes** peak traced allocation during calculation and **3,095,467 bytes**
projected decision JSON. Input construction/source fetch/serialization/storage
and matched comparison are outside this timing. This is a synthetic capacity
benchmark, not actual forward observations or an operational latency guarantee.
The previous full-context projection measured 89,239,147 bytes and failed the
storage bound; its histories were removed without changing calculated facts.

Warning audit, based on the actual summaries:

| Source | Hub/root count | Classification and cause |
| --- | --- | --- |
| Existing FastAPI app/library startup/shutdown | 4 / 4 | Maintenance deprecation: `on_event`, future lifespan migration; unchanged source |
| aiohttp connectors in existing instance/reboot tests | 81 / 83 | Benign compatibility deprecation: `enable_cleanup_closed` is ignored because CPython 3.12.14 contains the referenced fix |
| Supabase/PostgREST transport tests | 4 / 4 | Third-party maintenance deprecations: timeout and verify parameters; original transport security preserved |
| New focused backend | 0 / 0 | No newly introduced warning |
| Browser Node environment | 4 | Existing NO_COLOR/FORCE_COLOR conflict, no browser correctness failure |
| Vite ECharts output | 1 | Existing large chunk performance advisory, no build/type failure |

Observed connector-warning frequencies vary between runs; the warning categories
are unchanged from baseline. No warning is globally suppressed. No migration,
restart or focused test emits a database/resource-leak warning. FastAPI lifecycle
and third-party deprecated options should be addressed in a separate reviewed
maintenance change, preserving the user's existing dirty app/runtime work.
Npm update notices are informational and are not counted as test warnings.

## Files modified in this sprint

These 18 paths are the Sprint 9 contribution, not all accumulated Git changes:

| Path | Change |
| --- | --- |
| `automation-hub/services/forward_qualification.py` | New contracts, policies, bounded calculation, reports, history |
| `automation-hub/services/forward_qualification_store.py` | New immutable transaction/quotas/review storage |
| `automation-hub/services/forward_paper_evidence_reader.py` | New read-only exact-cohort references/provenance/diagnostics |
| `automation-hub/services/shadow_worker_health.py` | New bounded atomic process-generation health |
| `automation-hub/services/shadow_worker_main.py` | Extend existing trusted health/shutdown and fail-closed DSN handling |
| `automation-hub/migrations/shadow/0005_forward_qualification.sql` | New additive isolated SQLite schema |
| `automation-hub/routers/strategy_intelligence.py` | Extend existing authenticated routes |
| `automation-hub-dashboard/src/pages/ForwardQualification.tsx` | New real-response qualification/review UI |
| `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx` | Extend tab/navigation and Research Agent evidence link |
| `automation-hub-dashboard/e2e/forward-qualification.spec.ts` | Three new browser contract workflows |
| `automation-hub/tests/test_forward_qualification.py` | Policy, integrity, migration, concurrency/recovery/financial tests |
| `automation-hub/tests/test_forward_qualification_api.py` | Auth, ownership, commitment/review and request bounds |
| `automation-hub/tests/test_forward_paper_evidence_reader.py` | Original costs, episodes/partial exits, provenance/readonly safety |
| `automation-hub/tests/test_forward_operations.py` | Real process/container startup/restart/resources and health |
| `ops/prepare-shadow-worker-wheels.py` | Bounded verified public dependency preparation |
| `ops/shadow-worker.Dockerfile` | Opt-in code-only offline-built local worker image |
| `ops/shadow-worker.local.compose.yaml` | Opt-in disposable supervision/resource test harness |
| `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT9.md` | Audit, implementation/validation/readiness report |

## Operational readiness matrix and remaining blockers

Statuses describe the actual boundary tested; VERIFIED locally does not attest
deployment. The API deliberately remains conservative without attached
operator certification.

| Gate | Local evidence | Deployment status |
| --- | --- | --- |
| Worker supervision | VERIFIED real independent process/Docker restart/graceful stop | PARTIAL; no installed production service attested |
| PostgreSQL worker concurrency | BLOCKED; no worker backend | BLOCKED |
| Resource isolation | VERIFIED local cgroup CPU/memory/PID controls | PARTIAL; no deployed sandbox certification |
| Migration safety | VERIFIED disposable SQLite fresh/upgrade/rerun/interruption | PARTIAL; authorized production-shaped tests pending |
| Backup/restore | VERIFIED local committed report/review restoration | PARTIAL; no operational backup schedule/restore drill |
| Dataset retention | VERIFIED retained revision/hash/retrieval/quotas locally | PARTIAL; availability/retention/reconstruction guarantees pending |
| Market-data reliability | UNKNOWN continuous feed/clock/downtime history | UNKNOWN; live soak and provider monitoring required |
| Observability | PARTIAL process-generation health and original audit/events | PARTIAL; no continuous availability/persistence counters |
| Tenant isolation | VERIFIED local owner-scoped API/read/storage tests | PARTIAL; deployed/RLS identity configuration unverified |
| Incident recovery | VERIFIED local lease/commit/backup/container recovery | PARTIAL; no staging restart drill |
| Production credential separation | VERIFIED credentials-free no-network local harness | UNKNOWN; production never inspected |
| Strategy lookback support | Original reviewed supported strategy gates retained | PARTIAL; generic SMC and unreviewed candidates blocked |
| Full paper cost/authority qualification | UNKNOWN external ledger/journal/slippage/spread as applicable | BLOCKED until original evidence resolves required facts |

Local feature validation does not complete the operational acceptance gates.
The code is available for review and an **authorized isolated staging assessment**,
but staging/deployment readiness is not certified. PostgreSQL support requires a
deliberate worker-backend implementation and real concurrency/fencing tests,
not a connection-string change. No staging/production access, trading activation,
automatic research designation, Git staging/commit/push or deployment occurred.

## Sprint 10 recommendations

1. Resolve PostgreSQL worker persistence/claims/fencing in an explicit scope and
   test real concurrent worker/recovery races on disposable PostgreSQL.
2. Add independently sourced continuous feed/worker availability and commitment
   telemetry so unexplained downtime/recovery gates can be evaluated.
3. Reconcile owned paper episodes read-only against authoritative account/journal
   receipts and original execution-cost evidence; preserve unknown funding.
4. Obtain authorized production-shaped migration, backup/restore and restart
   testing, external supervision/resource sizing and credential-separation review.
5. Review slower-timeframe policy versions/lookbacks and longer-term retention
   against actual opportunity counts and capacity without merging overlaps.
6. Retain original datasets/software and independent holdout lineage; maintain
   human review and separate trading authorization. No recommendation enables
   live or paper execution automatically.
