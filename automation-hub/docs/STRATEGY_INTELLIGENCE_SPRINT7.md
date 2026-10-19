# Sprint 7 — Shadow Strategy Router

## Executive summary

An explicitly authorized frozen research candidate can now subscribe to the
existing Binance USDⓈ-M public candle hub, evaluate an allowlisted existing
strategy without a broker, and record immutable forward **decision** evidence.
The Shadow Strategy Lab exposes registration, validation, start, pause, resume,
stop, decision inspection, coverage, comparison availability and audit history.

This is NOT another trading or backtesting engine. It produces no orders,
positions, fills, hypothetical outcomes, R returns or profitability claims.
No active strategy, account, leverage, risk setting or trading mode is changed.
No staging/production access, deployment, Git staging, commit or push occurred.

Local implementation and production readiness are separate. Exact active
decision evidence is unavailable in the default integration; generic SMC has no
verified read-only adapter. Both fail closed. Production readiness is blocked
by the operational prerequisites below.

## Repository audit and baseline

The audit read Sprint 6, 5.6, 5.5, 5, 4.6, 4.5 and production-validation reports,
then inspected their actual services, persistence, APIs, frontend and tests.
The existing dirty working tree contained earlier sprint work. It was preserved;
Sprint 7 changes are listed separately below, not represented as ownership of
all differences from HEAD.

Current pre-change root baseline: **3,453 passed, 0 failed, 25 skipped,
84 warnings**, 335.12 seconds. Focused pre-change strategy/worker/API baseline:
**50 passed, 0 failed, 0 skipped**, 2.29 seconds. Historical reported counts were
not used as substitutes for these executions.

Findings:

- `ForwardPaperMarketDataHub` already multiplexes public native kline streams
  by symbol/timeframe. `candle_notice` is a non-blocking wakeup seam; snapshots
  expose closed history and transport freshness. No new provider is needed.
- `PriceActionPublicStream` uses Binance USD-M public REST bootstrap and public
  WebSockets, explicit kline finality, UTC candle-open timestamps, reconciliation
  and freshness. Its transport state is `CONNECTED`, not `LIVE`.
- ATP 1.0.0 and native PA 1.1.0 have broker-free signal interfaces. Generic SMC
  is unversioned in the registry and cannot be certified by name alone.
- Full Sprint 1 effective snapshots include defaults, component configuration,
  retained settings and resolved timeframes. Older flat research configurations
  do not establish those facts and must remain blocked.
- The existing `ShadowResearchStore` is physically separate from paper ledgers
  and already houses legacy hypothetical research. Its legacy decision identity
  lacks the required tenant/fingerprint/conflict contract. New tables therefore
  use a decision-only namespace in the SAME database; no ledger is added.
- Existing active decision records are primarily signal-level records and do
  not reliably attest a complete opportunity, exact close/context hash and feed
  freshness. A missing record cannot establish `NO_SIGNAL`.
- Per-candle native feed publication times are unavailable. Closed-candle
  selection is conservative, but strict publication-aware regime/volatility
  classification must remain `UNKNOWN`.
- The top-level migrations directory is discovered by a PostgreSQL identity
  runner. The new SQLite-only migration is deliberately in `migrations/shadow/`
  and explicitly applied only to the isolated shadow DB. The existing top-level
  `0002_shadow_research.sql` predates this sprint and remains a migration-routing
  review prerequisite for a future PostgreSQL rollout.

## Architecture and actual isolation boundary

`authorized API → durable registration → bounded observation thread → existing
shared public hub → closed/fresh/causal gates → existing strategy evaluator →
immutable shadow decision → read-only analytics/dashboard`.

The worker receives only the router and public market-data hub. Evaluators
receive symbol, frozen configuration and detached candle history. No exchange
order client, credentials, financial account, trading pipeline, mutable instance
or risk manager is injected. A narrow decision-store facade exposes no
order/fill/outcome methods. Forbidden actions are rejected server-side.

This is service-level isolation in the application process, with physically
separate SQLite storage. It is **not** OS sandboxing, a separate security
principal, restricted process environment or protection against arbitrary
trusted Python introspection. No user-controlled code/import/evaluation or
filesystem path is accepted. Public transport owns its existing reconnect logic.

## Files modified or added in Sprint 7

- `services/shadow_router.py` — frozen adapters, gates, lifecycle, comparisons.
- `services/shadow_observation_store.py` — restricted persistence facade,
  SQLite claims/fencing, immutable decisions/audit and analytics.
- `services/shadow_observation_worker.py` — shared-feed subscriptions,
  background scheduling, leases, reconnect/attach retry, recovery and shutdown.
- `migrations/shadow/0003_shadow_observation.sql` — additive isolated schema.
- `routers/strategy_intelligence.py` — authenticated owner-scoped shadow routes.
- `webhook_api.py` — wire the existing candidate reader/store/feed into the
  observation runtime without injecting execution services.
- `app.py` — restore authorized observation intent at existing startup;
  stop the observation worker at existing shutdown. Observation restoration
  errors do not change sibling financial execution startup gates.
- Dashboard `src/pages/ShadowStrategyLab.tsx`, `ShadowStrategyLab.css` and
  `StrategyIntelligenceHub.tsx` — scoped dark-theme lab and navigation tab.
- `tests/test_shadow_router.py`, `test_shadow_observation_worker.py`,
  `test_shadow_router_api.py` and dashboard `e2e/shadow-strategy-lab.spec.ts`.
- This report.

## Migration and backward compatibility

Three tables extend the existing shadow DB: `shadow_registrations`,
`shadow_observations`, `shadow_observation_events`. Unique constraints enforce
owner/idempotency identity and one decision per registration/candle close.
Indexes cover owner lists, lease recovery, timelines and audit history. Triggers
protect frozen registration fields and prohibit observation/audit updates and
deletes. Foreign keys are enabled on the observation connection.

Application initialization applies idempotent `CREATE ... IF NOT EXISTS` SQL.
No trading or historical row is rewritten. Existing hypothetical tables and
public dashboards keep their existing semantics. A representative legacy
shadow row survives repeated migration; local integrity/foreign-key checks pass.

Before authorized staging: back up the isolated shadow SQLite DB using its
online backup procedure (include consistent WAL state), test the actual schema
and retained representative rows, and verify restart behavior. This local test
is not production-shaped migration certification. Rollback is code rollback
with additive tables retained, not destructive SQL. Disable/pause registrations
through authorized controls first; retain committed evidence. Forward recovery
reapplies the additive schema and restores observation intent. No automatic
deletion or retention-expiry mutation is implemented.

## Candidate registration and lifecycle

Registration resolves an existing owner-scoped candidate and freezes identity,
effective configuration, candidate/validation references, source hash,
venue/market/symbol/timeframe, reviewed lookback, authorization actor/reason/time
and policy version. Qualification never registers a candidate automatically.

Full snapshots round-trip through the actual constructor and Sprint 1 SHA-256
identity capture. Configuration/version/source mismatches block validation.
When a historical candidate lacks a source hash, requirements expose a hash
observed **now** for explicit human review. It is stored as a current adapter
attestation, never retroactively asserted as historical research provenance.

States: `DRAFT`, `READY`, `OBSERVING`, `PAUSED`, `BLOCKED`, `STOPPED`, `FAILED`.
`VALIDATE` moves a draft/ready/blocked registration to ready or blocked. `START`
and `RESUME` enable durable observation intent only after authorization,
configuration and lookback checks. `READY` does not mean warm-up is complete:
readiness is initially `WARMING_UP`; only an eligible evaluation becomes
`OBSERVING`/`READY`. Data failures become `BLOCKED` without disabling the
authorized intent, so fresh valid opportunities may recover. Pause/stop disable
intent and increment generation, immediately fencing in-flight commits. Resume
records the paused interval and prohibits observations earlier than resume.
Stopped registrations cannot restart; create a new authorized registration.
Every explicit transition records actor, reason, states, time and fingerprint.

## Lookback and evaluator behavior

Supported: ATP 1.0.0, PA rejection and flip-retest 1.1.0, native 5m decisions,
Binance USD-M/perpetual only. Other adapters/timeframes fail closed.

ATP requires native 5m/15m/1h; 4h remains optional bias. Reviewed requirements
combine configured minima, EMA/ADX/ATR initialization, structure/pullback/
confirmation lookbacks and the registry's conservative 150-entry-bar bootstrap.
PA uses at least the existing reviewed 400-bar bootstrap, expanded for configured
swing, zone expiry and event/setup history. Native primary context requires at
least 51 bars, or the expanded structure history when HTF zones are selected.
Requirements exceeding the shared 1,500-bar capacity are blocked, never shortened.

Candidates reconstruct isolated evaluator state from bounded causal snapshots.
No indicator/engine instance is shared. Native PA bootstrap is primed through
the SAME native engine, one earlier bar at a time with closed HTF context at its
own close. This avoids applying current HTF context to earlier warm-up. Bootstrap
votes are suppressed; only the current closed opportunity is recorded. ATP's
existing `on_bar` and decision report are used directly. No position-lifecycle
callbacks are invoked and no trading rule/source file changed.

These reviewed finite-history requirements are not a mathematical claim that
EMA infinite-history convergence or every possible future strategy dependency
is certified. Unsupported configurations must receive new reviewed specifications.

## Market data and point-in-time gates

Bar timestamps are UTC opens; decision opportunity is the native candle close.
Actual evaluation/observation time is recorded separately (no backdating).
Each context series is trimmed to candles whose close is at/before the common
opportunity. No resampling, spot fallback, interpolation or forming-candle use.
OHLCV must be finite, positive-price, internally consistent and interval-aligned.
Critical gaps, conflicts, invalid ordering, missing entry/primary history,
insufficient warm-up and non-current required HTF boundaries block evaluation.
Optional missing/stale context is excluded and warned, not promoted to an entry
gate. Changes strictly after T do not change decisions at T.

The source must be the registered USD-M scope and the feed must be connected,
reliable and recently updated. Defaults: 15-second stream freshness, 30-second
maximum close delivery delay, 10-second post-evaluation latency limit. There is
a five-second healthy close-delivery grace; an unavailable feed records a blocked
scheduled opportunity. Shared quote-dependent health may conservatively block
a shadow subscriber when another consumer upgraded the same feed to quotes.

The shared provider may normalize raw events before exposing snapshots; this
service verifies received finalized snapshots, not every raw exchange payload
or provider-internal correction. Individual publication times are unknown and
that limitation is recorded. Existing classifier output remains `UNKNOWN` when
its stronger availability requirements cannot be established; sessions use its
existing UTC/local-calendar rules. No invented context classification.

## Immutable evidence and coverage

Every scheduled opportunity while attached/authorized is an eligible decision
or blocked/error record. Records contain tenant, registration/candidate,
strategy/version/fingerprint/source, market scope, candle close, actual decision
and observation time, data availability/heartbeat, candle/input/used-context
hashes, lookback/policy, actual strategy report/conditions, signal direction,
strategy-defined strength, rejection reasons, latency and completeness.
PA has no strategy-defined strength, so it remains null.

Input identity is independent of mutable transport health. Duplicate input
returns the same committed canonical response and records `REPLAY_IGNORED`.
Changed input at an already committed close produces `CANDLE_REVISION_CONFLICT`
audit evidence, never overwrites a decision, and makes coverage `CONFLICTED`.
Corrections require new versioned observation identity/review; no silent rewrite.

`shadow_decisions.v1` reports scheduled recorded opportunities, eligible count,
signals/eligible frequency, long/short counts, blocks/recorded opportunities,
reason/session/regime/volatility distributions, mean/max observed latency,
gap/failure/conflict counts and `COMPLETE`/`PARTIAL`/`UNKNOWN`/`CONFLICTED`.
Completeness concerns recorded decisions, NOT financial cost coverage or profit.
Continuous uptime is null/not measured; gap events are not an invented uptime
percentage or exact exchange-opportunity denominator. All profitability is
`NOT_ASSESSED`. These queries do not alter `strategy_intelligence.v1/v2` formulas.

## Active comparisons and optional outcomes

`shadow_comparison.v1` uses a bounded, explicitly labelled recent decision set.
Only eligible active records matching tenant, market, symbol, timeframe, close,
candle/context hash and fresh data are paired. It reports agreement/disagreement
and unmatched opportunity count; no trade pairing, profit inference or returns.
Paused/error/missing active records are excluded, not counted as `NO_SIGNAL`.

The read-only matching seam is tested with attested fixtures. The production
integration intentionally supplies NO active reader because the current active
journal does not prove this contract. UI displays `NO_DECISION_RECORDED` and
unavailable agreement. A verified read-only opportunity event adapter is a
prerequisite for actual active comparisons; do not alter active execution to
manufacture those records. Hypothetical outcome tracking is NOT IMPLEMENTED;
legacy shadow virtual-fill systems are not invoked. Unknown funding stays
unknown; no XRP or other profitability claim is made.

## Worker, feed and restart recovery

One process-local bounded observation thread owns only its subscriptions; the
hub shares existing public channels, with quote subscriptions disabled for new
shadow consumers. Notices only set an event; computation does not block feed
callbacks or run inside HTTP requests.

Atomic `BEGIN IMMEDIATE` claims assign a unique token, generation and 60-second
lease before evaluation. Heartbeats renew owned unexpired leases. Commit checks
durable intent, generation and current unexpired token inside its short SQLite
transaction; no computation runs in that transaction. Two-connection racing
claims and stale-worker commit rejection are tested. These are SQLite results,
NOT PostgreSQL multi-worker certification.

Expired/released leases record gaps; delayed initial attachment and skipped
scheduled intervals are recorded too. On restart the worker reconstructs causal
state but considers only opportunities AFTER attachment and resume. Cached
bootstrap/offline candles never become forward observations. Failed bootstrap
attachment retries the same subscriptions with bounded 5–30-second backoff.
Disconnects/stale HTF block decisions. Persistence errors are logged without
payloads/secrets and audited when possible; retries use the same logical key.
If the DB is unavailable for a prolonged outage, restart/lease reconciliation
records the interval, not synthetic missing decisions. Pause/stop fences a
completion race. Shutdown detaches only owned subscriptions and acknowledges
within five seconds, or explicitly reports failure to acknowledge.

## Authorization, resource policy and API

Existing signed-session/control authorization resolves the owner; this platform's
current owner identity is also the research tenant boundary. Explicit human
authorization and a non-empty audited reason are required. This is not a new
multi-user RBAC/admin system. Another owner's candidate/registration/decisions/
analytics/audit is inaccessible. Lease tokens are not exposed in API responses.

Policy `shadow-observation.v1`: five retained active registrations per owner,
20 globally; lifetime registration quotas 100/owner and 1,000/global prevent
stop/recreate quota evasion; 1,500 candles/context, 100,000 observations per
registration, fixed 5m opportunity clock, 30-day observation duration. Stop and
retention limits fail closed without deleting evidence. These conservative local
caps are not capacity certification; no new hardware/queue/distributed platform.
Runtime/latency limits are cooperative/post-evaluation, not hard CPU/memory limits.

Under `/api/v2/strategy-intelligence/shadow`:

- `GET /policy`, `/requirements/{candidate_id}`.
- `POST/GET /registrations`, `GET /registrations/{id}`.
- `POST /registrations/{id}/{validate|start|pause|resume|stop|block}` with reason.
- `GET /registrations/{id}/{decisions|coverage|comparison|audit}`.

Requests reject unknown mutation fields. Listings and decision/audit queries are
bounded; decisions have limit/offset pagination. No HTTP candle ingestion,
execution account selection, order placement, active configuration mutation,
code evaluation, dataset filesystem paths or live toggle exists.

## Dashboard and browser validation

The existing Strategy Intelligence tab bar now includes Shadow Strategy Lab.
Real authorized backend controls provide full frozen identity/lookback review,
registration/lifecycle, polling, decision evidence details, feed/readiness,
coverage, unavailable active comparisons, gap/audit inspection and errors.
Scoped styling uses the existing dark-theme variables/components. Unknown
verification and counts are displayed as unknown, never unsupported green/zero.

Browser contract fixtures are explicitly synthetic and not market results.
Chromium uses `PW_CHROMIUM=/usr/bin/chromium`. The new workflows exercise
authorization checkbox, frozen review, register/validate/start, no-signal journal,
pause/resume gaps, stop, unavailable comparison, blocked configuration and HTTP
401. Browser mocks test presentation; backend tests independently prove actual
authorization and persistence. DevTools MCP is unavailable; Playwright is the
verified browser fallback, not a claim of MCP execution.

## Tests and benchmarks

Final implementation test counts and full-suite durations are recorded below
after final-code verification. An initial full hub run was intentionally
interrupted after 241 passes when replay hardening changed code; it is NOT used
as final evidence. Earlier failing development tests (HTTP null-field
idempotency normalization, tuple/list canonical response, misplaced boundary
guard) were corrected with retained regressions, not suppressed.

Final focused Sprint 7: **73 passed, 0 failed, 0 skipped, 0 warnings**, 6.63s.
Database/recovery integration: **123 passed, 0 failed, 10 skipped, 0 warnings**,
8.23s. The skipped tests require explicitly enabled isolated PostgreSQL/
PostgREST Docker fixtures (`NEXUS_PG_OUTBOX_TEST=1`); these legacy financial
outbox tests are not shadow PostgreSQL certification. No PostgreSQL, staging or
production execution is claimed.
Automation Hub full suite: **2,996 passed, 0 failed, 25 skipped, 84 warnings**,
348.07s, after final backend changes.
Repository-root full suite: **3,526 passed, 0 failed, 25 skipped, 85 warnings**,
340.15s, after final code changes. All 3,453 previously passing root tests were
retained and 73 new backend tests were added. The full suite includes the Hub.
Dashboard typecheck/production build: `npm run build:check` passed (TypeScript
and Vite production build). Test pass/fail/skip counts do not apply to builds.
Full Chromium suite: **140 passed, 0 failed, 0 skipped**, 5.9 minutes, using
`/usr/bin/chromium` after the serialized build. This includes all prior browser
regressions and three new shadow workflows. Six CLI Node color-environment
warning records and one Vite bundle-size warning were emitted (seven observed
warning records, not a Playwright aggregate). New workflow page-console
errors/warnings were asserted empty; desktop and stable phone screenshots
were visually inspected.
`git diff --check`: passed. New untracked Sprint 7 sources were also checked
for trailing whitespace. Final review confirms changes are limited to the
listed shadow feature/integration/test/report paths; prior unrelated dirty
worktree edits remain intact. No Git mutation or deployment was performed.

Tests cover native signal preservation, PA causal bootstrap, identity/version/
configuration/source isolation, authorization, all forbidden capabilities, finite
OHLCV/order/gaps/finality/freshness/HTF/warm-up/future-data invariants, null strength,
logical duplicates/conflicts/canonical replay, database immutability/legacy
compatibility, quotas/duration/latency, two-connection claims, renewal/expiry/
stale commits, pause/completion races, restart gaps/no backfill, feed bootstrap
retry, DB write failure retry, active matching/missing/paused distinctions and
real authoritative financial/journal state non-mutation with instrumented
execution interfaces. No real exchange/order credentials in fixtures.

Native adapter fixture benchmark: **4 passed, 0 failed, 0 skipped, 0 warnings**,
2.67s. Entire ATP signal-test calls (including setup/assertions) took 0.07s long
and 0.06s short; PA causal/future-invariance test calls took 0.81s rejection and
0.76s flip-retest. These are local fixture test durations, not live throughput
or isolated evaluator latency benchmarks. Production capacity, real network
delays and long-duration storage growth remain unmeasured. No hard sandbox or
real-time latency guarantee.

One preliminary full browser run reported **139 passed, 1 failed, 0 skipped**:
the existing Paper Trading navigation failed with
`page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE` from the local preview server,
not an app assertion/JavaScript failure. It passed **3/3 unchanged isolated
repetitions**, 16.1s. Concurrent production-build/preview artifact replacement
was a possible contributor; the final full browser rerun is serialized after
the build. No existing assertion, timeout or test was weakened/deleted.

## Warning audit

The final Hub suite emitted 84 warnings, matching the pre-change root warning
total: four existing FastAPI `on_event` deprecation records and 80 existing
`aiohttp` `enable_cleanup_closed` deprecation records on Python 3.12.14. The
former is lifecycle API migration debt; changing shared financial startup/
shutdown semantics is outside this isolated sprint. The latter documents an
upstream CPython cleanup fix, not a newly detected unclosed-resource warning.
Focused/new database tests emitted no warnings. No warning filter/suppression
was added. Root emitted 85 warnings: the same four FastAPI records plus 81
aiohttp records. The one-record frequency increase over the root baseline is
from existing worker/instance connection fixtures, not a new warning source.
No resource-leak warning was observed; counts may vary with fixture reconnects.

The production build emitted one existing large-chunk warning (ECharts/core
bundle footprint); no new chart dependency was added. Browser CLI color
environment warnings are tooling noise (`NO_COLOR` plus `FORCE_COLOR`), retained
and counted separately. npm upgrade notices and slow-test advice are not test
warnings. No credential content, page-console defect or new resource-leak
warning was observed in the new browser workflows.

## Known limitations and operational blockers

IMPLEMENTED / locally TESTED: authorized frozen registration, verified finite
lookback, shared public-feed dispatcher, isolated native evaluators, causal/data
gates, immutable journal, SQLite fencing/recovery, no-order/financial isolation,
read-only decision metrics, lifecycle APIs and functional dashboard.

PARTIALLY IMPLEMENTED: active-strategy comparison (strict tested matching seam,
default unavailable); market-regime/volatility distributions (unknown when native
publication evidence is missing); gap/coverage reporting (not continuous uptime).
Registration-card feed/readiness fields are the last persisted evaluation state,
not continuous current-feed monitoring; consult decision timestamps. The worker
checks live freshness at every actual opportunity regardless of the last stored
card state. Independent continuous feed-health display is a follow-up limitation.

BLOCKED: generic SMC certification, partial historical effective configurations,
unreviewed/excessive lookbacks, unavailable/stale/gapped/future native data.

NOT IMPLEMENTED: hypothetical outcomes, financial performance, automatic
champion promotion/switching, trading activation, new execution engine, hard
resource sandbox, independently supervised shadow service, PostgreSQL shadow
backend/multi-worker guarantee, production deployment.

Preserved prerequisites requiring explicit operational authorization/evidence:

1. Independently supervised research AND observation workers; process-local
   startup is not independently managed production supervision.
2. PostgreSQL worker/claim/fencing and migration routing validation; do not
   extrapolate SQLite concurrency tests or prior financial-outbox tests.
3. Production-shaped schema/data backup/migration validation and rollback drill.
4. Hard OS/container CPU/memory/environment isolation and deployment credentials
   policy; same-process code cannot claim secret-free process environment.
5. Dataset retention/reconstruction and raw provider correction/finality audit.
6. Independent review of eligible strategy/configuration-specific lookbacks.
7. Real feed reliability/availability/freshness and load testing in authorized
   isolated staging, followed by restart/reconnect/DB outage recovery drills.
8. Verified read-only active opportunity evidence adapter, without modifying
   active strategy or execution semantics.

## Sprint 8 prerequisites and readiness

Complete the above staging checks and exact active evidence adapter, validate
point-in-time context publication identity, and measure operational coverage and
throughput before broader shadow comparisons. Any hypothetical evaluation must
be separately approved, cost-versioned and isolated, with unknown funding never
zeroed. Promotion and execution authorization remain separate human policies.
This sprint authorizes no trading or deployment and proves no strategy edge.

Code is available for controlled local review and explicitly authorized isolated
staging validation after operational prerequisites are resolved. It is not
certified for production rollout.
