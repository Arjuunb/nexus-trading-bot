# NEXUS Strategy Intelligence — Sprint 3

## Scope and safety boundary

Sprint 3 adds a dedicated, read-only Strategy Intelligence section to the
existing Automation Hub. It projects the persisted Sprint 2 `strategy_intelligence.v2`
cohorts and reports; it does not add a strategy selector, qualification engine,
order path, risk change, ledger, or live-trading capability. No production
deployment or production data access occurred.

The Adaptive MTF identity remains `adaptive_trend_pullback` version `1.0.0`.
The claimed XRPUSDT values (22 trades, 40.9% win rate, PF 1.13, +$0.84) remain
**UNVERIFIED** because no authoritative export was supplied.

## Implemented views

The new `Strategy Intelligence` navigation entry contains:

| View | Evidence source and behavior |
|---|---|
| Overview | Exact cached cohorts, episode counts, descriptive metrics and explicit `NOT_AVAILABLE` qualification status. |
| Strategy Explorer | Existing Context Intelligence view, reused without a second calculation path. |
| Edge Map | Server-side two-dimensional projections over supported cohort groupings; unknown or partial evidence remains visibly unverified. |
| Strategy Comparison | Two-to-four strategy IDs, exact compatible cohort identity, and `NOT_DIRECTLY_COMPARABLE` when rows do not share the required scope. |
| Market Regimes / Sessions | Existing context API and classifier outputs, presented as read-only projections. |
| Trade Evidence | Bounded, server-paginated completed episodes; partial exits remain one episode and show context/cost warnings. |
| Research Findings | Explicitly states that Sprint 3 does not generate or infer findings. |

Routes are authenticated and scope-resolved by the existing owner, account,
instance, session, strategy, version, configuration-fingerprint, execution-mode
and source-kind bindings. The new routes are GET-only:
`/overview`, `/edge-map`, `/comparison`, and `/episodes` beneath
`/api/v2/strategy-intelligence`.

## Data and metric contract

No new database migration is required. The service reads the immutable cached
cohort reports, context snapshots and evidence journal introduced in Sprints
1–2. Decimal values are rendered as strings; no financial values are recomputed
in the browser. One final-closed position episode is one statistical trade;
partial fills and exits cannot inflate trade count. Rejected, cancelled,
expired, counterfactual, replay and backtest records are excluded unless their
execution/source filters explicitly select a compatible cohort.

The existing `strategy_intelligence.v2` contract remains authoritative for
profit factor, win rate, net P&L, fees/funding, expectancy/R, cost coverage,
sample confidence and evidence completeness. A report can display descriptive
numbers while `profitability_verified` is false. Unknown funding, slippage,
missing context, stale cache, binding conflicts and incomplete evidence are
warnings/blockers, never zeroes or inferred costs. Calculation timestamps and
cache status are retained in API responses.

The Edge Map currently exposes only dimensions already supported by the v2
grouping registry: symbol, session, trend regime, volatility regime, direction
and entry timeframe. Unsupported or duplicate dimensions are rejected rather
than silently producing a different cohort.

## Compatibility and recovery

Existing strategy rules, optional HTF fail-closed behavior, fill eligibility,
paper-account isolation, accounting writes, evidence recovery and financial
records are unchanged. Existing Analytics/Context routes and formulas remain
available. The new projections are additive and read-only; retries cannot write
orders or journal entries.

No migration rollback is needed for Sprint 3. The forward recovery procedure is
the existing Sprint 1.5/Sprint 2 evidence reconciliation worker; if cached
reports are unavailable, the API reports `PENDING`, `STALE` or
`RECONCILIATION_REQUIRED` rather than rebuilding financial history.

## Validation

Validated in this checkout:

* Strategy Intelligence API: 51 passed.
* Strategy Intelligence service plus API: 62 passed.
* New service projection regression: 1 passed.
* Dashboard TypeScript typecheck and Vite production build: passed.
* `git diff --check`: passed.
* Full Automation Hub suite: 2,884 passed, 25 skipped, 86 warnings.
* Repository-root suite: 3,414 passed, 25 skipped, 85 warnings.

Browser tests were initially blocked by a missing bundled Chromium executable.
Sprint 4 closure reused the approved system Chromium at `/usr/bin/chromium` and
the Strategy Intelligence browser suite now passes **9 tests**.

## Remaining risks and readiness

Sprint 3 is code-complete for the requested read-only dashboard projections but
remains advisory and is **not production-deployment ready** without staging
validation.
The XRP evidence reconciliation is still unverified. Strategy qualification,
automated findings, regime-edge attribution, and dashboard formula migration
remain Sprint 4/Sprint 2 follow-up work and are intentionally not implemented.

Changes are intentionally left uncommitted and unstaged pending explicit
authorization to create a Sprint 3 commit or push.
