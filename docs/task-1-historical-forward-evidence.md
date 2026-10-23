# Task 1: historical-to-forward-paper evidence integration

Development branch: `finish/strategy-intelligence-staging`, based on PR #40
head `9957e3e749fb0ef0a8072eb451d93f4da77ab4ab`.

## Resulting behavior

Qualification joins historical backtests, chronological OOS validation,
walk-forward validation, robustness and cost-stress reports to actual completed
forward-paper position episodes. Execution modes retain separate provenance;
they no longer prevent evidence with the same frozen strategy identity from
being inspected together.

The binding requires an observed strategy ID, version, effective configuration
fingerprint and source hash matching the immutable strategy snapshot. Historical
reports must also match the asset, timeframe, dataset ID and content revision.
Research workers capture the instantiated strategy before simulation instead of
trusting the requested fingerprint. Simulator tuning overrides are explicitly
unverified because they are outside that observed configuration identity.

Owner/account/instance/session boundaries remain explicit. An owner's wholly
isolated historical research can bind to that owner's exact strategy identity;
partly bound research and other accounts' bound results cannot act as wildcard
matches. Holdout access counting now includes owner identity. Existing immutable
reports and their historical counts are not rewritten.

## Financial and temporal checks

- Historical financial summaries are reconstructed from saved original closed
  samples, with original positive entry risk, finite amounts, reconciled booked
  net/gross/fees/signed funding, explicit cost coverage and named cost models.
- Missing financial fields remain unknown. A summary flag cannot replace the
  original financial samples. Profit factor uses booked money; expectancy and
  stress outcomes use net R. Slippage already in filled prices is not charged a
  second time. Stress assumptions must include finite fee/slippage/funding
  amounts and reconcile to the same original net-R financial samples.
- OOS partitions need complete, disjoint financial samples and chronological
  boundaries. Reused holdouts cannot pass. Every walk-forward development and
  evaluation window must be complete; evaluation windows cannot overlap or
  consume future development outcomes.
- Forward evidence comes from the existing freshly reconciled cached episode
  projection, including its source watermark, exact cohort, unique episode IDs
  and observation period. Period boundaries compare aware timestamps in UTC,
  including when the original records contain different time-zone offsets.
  Stored assertions labelled `forward_paper` do not
  substitute for actual episode evidence. Partial exits remain constituents of
  one completed position episode.
- Historical data and validation completion must precede the first qualifying
  forward episode's opening time. Stale, missing, conflicted, bounded or
  incomplete evidence remains unqualified. A newer unfinished research attempt
  cannot resurrect an older passing validation result.

The qualification, OOS, walk-forward and robustness API projections use this
binding. Qualification retains the public `strategy_intelligence.v2` envelope
and separately identifies its qualification contract, avoiding a router contract
mismatch that previously hid real service responses.

New worker reports separate durable calculation completion (`status=COMPLETE`)
from the validation verdict (`result_status=PASS/FAIL/UNKNOWN`). This conforms
to the existing PostgreSQL result-commit contract while preserving failed or
incomplete evidence as failed or incomplete qualification input. Old reports
remain immutable and readable under their original contract.

## Scope and limitations

The existing simulator's legacy R-only records do not contain all financial
cost facts, particularly proved funding coverage. They remain available as
research output but cannot become complete qualification evidence. This change
does not invent those amounts or certify an existing strategy as profitable.

No strategy rules, primary financial records, live-trading flags or production
configuration are changed. No deployment, image publication, merge or Task 2
work is part of this change.

Two existing notification test files now isolate their notifier/channel fixtures
and mock the transport-failure test instead of making a real Telegram request.
No existing assertions were removed. Regression commands clear notification
destinations and use the CI execution-disabled environment.

## Validation

New regression tests cover complete and rejected evidence chains, raw financial
sample integrity, variable-risk profit factor, actual partial/final paper exits,
worker persistence and identity capture, stale evidence, unfinished jobs,
tenant boundaries, temporal leakage and the real authenticated API.

Validation used Python 3.12.14 and the hash-locked API/CI dependencies. The
dependency check reported no broken requirements. Notification destinations
were cleared, external/live execution and automatic workers were disabled, and
the Telegram transport-failure test used its local stub.

| Check | Actual result |
| --- | --- |
| Focused services, qualification, API, worker, financial evidence, notification tests | 253 passed, 4 warnings |
| Complete repository regression | 4,227 passed, 2 failed, 76 skipped, 86 warnings |
| Entire worker-health test file, current Task 1 source | 5 passed, 1 existing opt-in skip |
| Same worker-health file, unchanged PR #40 | 5 passed, 1 existing opt-in skip |
| Compose resolution check, unchanged PR #40 | Same missing `docker` executable failure |
| Source whitespace/diff checks | Passed |

The full-run failures were:

1. `test_forward_operations.py::test_real_standalone_process_writes_shutdown_health_without_execution`:
   its readiness assertion returned false during the complete suite. The whole
   test file passes independently on both sources. This full-suite failure is
   unresolved; no production worker-health behavior or assertion was weakened.
2. `test_sprint13_remote_readiness.py::test_real_compose_resolution_keeps_independent_volume_names_and_ports`:
   this workspace has no Docker CLI. The unchanged PR #40 source reproduces the
   same `FileNotFoundError`. The real Compose assertion was not replaced or skipped.

Existing opt-in integration skips were retained. No PostgreSQL/container drill,
staging operation, deployment or live trading was performed. Task 1's focused
validation passes, but the full repository suite is **not green**. Deployment
and Task 2 remain on hold. Machine-readable counts are in
`validation/task1/results.json`.
