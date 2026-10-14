# NEXUS Strategy Intelligence — Sprint 4

## Executive summary

Sprint 4 adds an advisory, deterministic qualification and validation contract
on top of the existing evidence and backtesting architecture. It does not
change strategy rules, order eligibility, risk settings, paper accounting or
live-trading controls. A missing or incomplete requirement produces
`UNKNOWN`/`NOT_QUALIFIED`; it is never converted to a zero-cost or assumed pass.

No production data was accessed and nothing was deployed, committed or pushed.
Adaptive MTF remains `adaptive_trend_pullback` version `1.0.0`; the historical
XRP claim remains unverified.

## Repository findings and design

The repository already contained:

* `backtesting/` and `services/backtest_lab.py` for real replay/backtest work;
* `scripts/strategy_validation.py` for archive-fingerprinted causal validation;
* immutable v1/v2 episode, context, cost and configuration evidence;
* `JournalContextStore` and additive SQLite migrations;
* authenticated Strategy Intelligence routes and the Sprint 3 dashboard.

Sprint 4 therefore adds pure research primitives and immutable metadata records,
rather than another simulator, paper ledger, market-data path or analytics
database.

## Files modified and models

* `services/strategy_qualification.py` — versioned policy, explicit checks,
  chronological partitions, anchored/rolling windows, OOS reports, walk-forward
  summaries, seeded Monte Carlo, cost stress and concentration analysis.
* `data/journal_context_migrations.py` — additive immutable
  `strategy_validation_runs` table and indexes.
* `data/journal_context_store.py` — idempotent validation-run persistence,
  immutable conflict detection, scope-filtered history and holdout-access counts.
* `services/strategy_intelligence_service.py` — qualification, OOS,
  walk-forward, robustness and validation-history read projections.
* `routers/strategy_intelligence.py` — authenticated GET-only validation routes.
* `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx` — qualification,
  OOS, walk-forward, robustness/Monte Carlo and validation-history views.
* `automation-hub-dashboard/playwright.config.ts` — use an existing approved
  `/usr/bin/chromium` when the bundled executable is unavailable.
* Sprint 4 regression tests in `tests/test_strategy_qualification.py`, API and
  service test modules.

No financial or execution migration was created. The validation table is
journal-context metadata only, with immutable rows and no accounting foreign
keys or write path to orders.

## Qualification policy

`QualificationPolicy` is deterministic and fingerprinted (`qualification.v1`).
Defaults are configurable policy starting points: 100 historical episodes,
profit factor >= 1.30, positive expectancy, complete/approved conservative
cost coverage, independent OOS, walk-forward, concentration testing and at
least 30 forward-paper episodes. Maximum drawdown is optional and configurable.

Each report includes strategy identity, policy fingerprint, evidence quality,
check-by-check PASS/FAIL/UNKNOWN status, passed/failed/unknown requirement
lists, blocking reasons and a recommended next action. Qualification is
advisory; no automatic lifecycle transition or champion selection exists.

## OOS and walk-forward methodology

`chronological_partition` sorts observations by timestamp and creates train,
validation and untouched holdout segments. Warmup rows are retained as context,
never counted as validation trades. The injected evaluator is the seam for the
existing replay/backtest engine; this module does not download data or optimize
active configurations.

`walk_forward_windows` supports anchored and rolling development windows with
disjoint chronological evaluation windows. `out_of_sample_report` and
`walk_forward_report` preserve development/evaluation separation, report
relative deterioration, profitable/loss-making windows and median expectancy.
Validation-run persistence records dataset identity, revision, configuration
fingerprint, policy/cost/classifier/software versions, time period and seed.
Repeated OOS/walk-forward use of the same dataset increments
`holdout_access_count`, flagging possible holdout contamination.

## Robustness methods

* Cost stress keeps fee, slippage and funding assumptions explicit. Missing
  funding returns `UNKNOWN`, never zero.
* Seeded Monte Carlo resamples reliable completed-episode outcomes and returns
  reproducible net and drawdown percentiles. It explicitly warns that reshuffling
  preserves the observed outcome set and bootstrap exchangeability may fail for
  clustered returns.
* Concentration analysis reports largest/top-three contribution and profit after
  removing them; non-positive totals do not produce misleading ratios.
* Parameter sensitivity remains an isolated research concern. No production
  strategy object is mutated and unavailable parameter surfaces remain unknown.
* Regime/session robustness consumes existing point-in-time context snapshots;
  no future classification or synthetic regime is created.

## Forward-paper integration

Forward-paper results remain separate from historical backtests and replay.
Qualification reads the existing exact execution-mode/source cohort and requires
the configurable forward-paper episode target before it can pass. It does not
start paper trading, create a second ledger, promote a strategy or unlock live
execution.

## APIs and dashboard

Authenticated GET-only routes under `/api/v2/strategy-intelligence`:

* `/qualification`
* `/oos`
* `/walk-forward`
* `/robustness`
* `/validation-history`

All cohort routes require the existing strategy/version/fingerprint,
instance/session, execution-mode and source bindings. History can be queried at
authorized instance scope. The dashboard displays unavailable evidence,
blocking reasons and immutable validation history; it does not render simulated
curves as actual trading results.

## Security and authorization

The existing owner/session authorization is reused. Query fields remain
validated and repeated filters are rejected. Validation endpoints are read-only;
no route calls an order, ledger, market-data download or active strategy
configuration setter. Research metadata is scoped to the server-resolved owner,
account, instance and session.

## Validation results

Current Sprint 4 focused checks: **71 passed, 4 warnings**.

Sprint 3 closure browser tests were rerun using the existing approved Chromium:
**9 passed**. The Playwright configuration now falls back to `/usr/bin/chromium`
when the pinned bundle is absent.

The post-change Automation Hub suite passed **2,893 passed, 25 skipped, 83
warnings**; the repository-root suite passed **3,423 passed, 25 skipped, 86
warnings**.
Production-shaped database validation remains required before staging approval.

## Performance and resource controls

Pure Monte Carlo runs are bounded to 5,000 simulations. Validation history reads
are bounded to 1,000 rows. Existing backtest lab limits, worker isolation and
market-data resource controls remain authoritative. No distributed worker or
live-cycle blocking path was introduced.

## Remaining limitations and deployment blockers

The repository has no authorized production evidence export for XRP, so no
historical profitability or qualification claim is made. This sprint provides
the deterministic evaluation seam and durable result contract; an operator must
submit an immutable, correctly scoped dataset and invoke the existing backtest
engine before OOS/walk-forward records become available. Parameter-sensitivity
and regime subgroup runs are not fabricated when their source facts are absent.

The code is ready for isolated staging validation, not production deployment.
Required staging work includes migration testing against a restored schema,
representative dataset replay, full post-change backend/root suites and browser
tests in the target build environment. No automatic lifecycle transition,
promotion or deployment is permitted.

## Sprint 5 prerequisites

1. Authorized immutable historical datasets and cost-model definitions for each
   target cohort.
2. A reviewed research-job runner using existing worker infrastructure with
   cancellation, timeout and progress persistence.
3. Staging validation of validation-run migration, retention and authorization.
4. Human-reviewed policy thresholds and an explicit approval process for any
   future lifecycle transition.
5. Independent forward-paper evidence with complete fee/funding coverage.

