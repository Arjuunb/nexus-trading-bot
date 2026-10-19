# NEXUS Strategy Intelligence — Sprint 5.5

## Executive summary

Sprint 5.5 adds a durable matched-research experiment layer on top of the existing Sprint 4.6 research jobs and Sprint 5 Champion/Challenger comparison services. A campaign can freeze one research champion, up to eight challengers, the dataset/revision, period, methodology, cost assumptions and policy versions. Candidate jobs are submitted through the existing research queue; committed results are then compared as advisory evidence.

No trading execution, financial ledger, paper account, risk setting, strategy rule or active champion was changed. The implementation does not promote a challenger automatically.

## Repository findings and architecture

The existing `ResearchJobService` remains the only research worker/backtest path. `StrategyComparisonService` remains the comparison and policy layer. Sprint 5.5 adds `MatchedExperimentService`, persistence for campaigns/experiments/candidate mappings and append-only lifecycle/holdout exposure events, plus authenticated API routes and a dashboard workflow.

The flow is:

`campaign/experiment draft → structural validation → existing research jobs → durable job results → reconciliation → Sprint 5 comparison → advisory recommendation`.

## Files modified

Backend additions/changes:

- `automation-hub/services/matched_experiments.py`
- `automation-hub/data/journal_context_migrations.py`
- `automation-hub/data/journal_context_store.py`
- `automation-hub/routers/strategy_intelligence.py`
- `automation-hub/services/research_jobs.py`
- `automation-hub/services/strategy_comparison.py`
- `automation-hub/tests/test_matched_experiments.py`
- `automation-hub/tests/test_matched_experiment_api.py`

Frontend/test changes:

- `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx`
- `automation-hub-dashboard/e2e/strategy-intelligence.spec.ts`

The repository also contains pre-existing Sprint 3–5 changes; they were preserved.

## Database migrations

Additive SQLite/PostgreSQL-compatible schema changes add:

- `research_campaigns`
- `matched_experiments`
- `matched_experiment_candidates`
- `matched_experiment_events`
- `holdout_exposure_events`

Unique owner/idempotency keys prevent duplicate campaign/experiment creation. Candidate mappings and lifecycle events preserve job/result lineage. Evidence and holdout event tables remain append-only. Existing immutable evidence triggers intentionally exclude lifecycle tables whose status/progress fields must change.

Migration order is the normal journal-context migration order. Rollback is forward-compatible removal only after dependent research artifacts are retained; no financial records are deleted. PostgreSQL multi-worker migration validation and production-shaped rollback testing remain staging prerequisites.

## Experiment lifecycle and orchestration

Supported states are `DRAFT`, `VALIDATING`, `QUEUED`, `RUNNING`, `PARTIALLY_COMPLETED`, `COMPLETED`, `FAILED`, `CANCEL_REQUESTED`, `CANCELLED` and `BLOCKED`. Transitions are persisted as lifecycle events. Submission is idempotent per experiment/candidate mapping. Reconciliation reads the existing durable job records, updates progress, and only creates a comparison after every required candidate has a committed completed result.

Up to eight challengers are accepted. Different challenger recommendations produce an `INCONCLUSIVE` aggregate recommendation rather than selecting the highest result automatically. Failed, cancelled or incomplete jobs are excluded from comparison evidence.

Worker leases, fencing, retries, cancellation and logical result idempotency continue to be provided by Sprint 4.6. A matched experiment does not create a second queue or simulator.

## Dataset, period and cost matching

Creation validates dataset identity/revision, evaluation period, venue, market type, symbol, timeframe, execution mode, methodology, OOS/holdout flags, classifier and cost assumptions. A mismatch produces `BLOCKED` with explicit reasons. Candidate-specific frozen configurations remain distinct by candidate ID and configuration fingerprint.

The worker rejects a requested dataset revision that differs from the immutable dataset manifest. Research requests preserve fee, slippage, spread and funding fields; unknown funding is not converted to zero. The current backtester integration preserves the requested period metadata and identity, but does not yet implement arbitrary period slicing inside every historical provider. Such cases must be treated as a validation limitation, not as independent evidence.

## Lineage and holdout exposure

Campaigns record candidate count, selection criteria and policy versions. Experiments record candidate identities, frozen specification, candidate job IDs, validation run references, warnings and reports. Every lifecycle transition and holdout exposure is append-only.

The first exposure is `UNTOUCHED` before results are revealed, subsequent exposure becomes `EXPOSED`, and repeated exposure is `REUSED`. A new experiment ID does not reset exposure history. Historical lineage gaps are reported as unknown by the surrounding qualification policy. Holdout evidence is therefore advisory and cannot satisfy an independence requirement after reuse.

## Multiple testing and comparison methodology

Each challenger is compared separately against the same frozen champion and scope using the existing Sprint 5 comparison policy. Comparisons use risk-normalized metrics and preserve OOS, walk-forward, cost-coverage and concentration warnings. The implementation does not invent paired observations, adjusted significance or independent samples; overlapping windows and repeated holdout use remain explicit limitations. A campaign’s candidate count and repeated holdout exposure are available for conservative policy warnings.

Recommendations remain `CHALLENGER_PREFERRED`, `CHAMPION_RETAINED`, `INCONCLUSIVE`, `NOT_COMPARABLE` or `INSUFFICIENT_EVIDENCE`. No recommendation changes execution state or the research champion registry.

## API and dashboard

Authenticated, owner-scoped routes provide campaign creation/listing, experiment creation/listing, submit, reconcile, cancel, status, lineage and holdout exposure. Submission starts the existing process-local research worker using the existing worker lifecycle; no arbitrary paths or code are accepted.

The Strategy Intelligence Champion/Challenger view now supports selecting multiple challengers, submitting a matched experiment and displaying its durable status. Existing single-comparison advisory UI remains available. Results are not shown as final until reconciliation reports committed candidate results.

## Financial and execution isolation

Matched experiments write only research metadata, job records and immutable research evidence. They do not write balances, positions, orders, financial journals, paper ledgers, active strategy settings, leverage or risk limits. No automatic promotion or trading activation exists.

## Tests and validation

Focused Sprint 5.5/backend validation: **90 passed, 4 warnings**. This includes matched experiment persistence/API tests and existing Sprint 5 comparison, research-job, qualification and Strategy Intelligence tests.

Dashboard validation: **typecheck passed; production build passed**. The build emitted the existing large-chunk advisory warning.

Browser validation: the approved Chromium environment ran the updated Champion/Challenger matched-submit flow and the final complete suite: **136 passed**. Two initial contract-level failures (stale sidebar expectation and a non-exact `LOCKED` selector) were corrected; the affected 15-test rerun passed before the final 136-test run.

Pre-change full-suite baseline: Automation Hub **2,907 passed, 25 skipped, 83 warnings**. Repository root **3,436 passed, 25 skipped, 85 warnings, 1 unrelated failure** in a cache-key test caused by an existing timestamp-string assertion (`"777"`), not cross-tenant result leakage. These suites must be rerun after final Sprint 5.5 changes.

`py_compile` and `git diff --check` are required final checks. No production or staging environment was accessed.

## Known limitations and operational blockers

- Worker startup is still process-local/lazy; a dedicated independently supervised worker service is not implemented.
- SQLite tests do not prove PostgreSQL `SKIP LOCKED` multi-worker behavior.
- No hard OS-level CPU/memory sandbox was added; cooperative limits remain the boundary.
- Production-shaped migration, restart and lease-fencing validation requires authorized staging.
- Arbitrary historical period slicing and provider-specific warm-up execution need further backtester work before claiming universal matched-period equivalence.
- Full post-change root suite retains the known unrelated flaky timestamp test until independently corrected.

## Deployment readiness

The code is suitable for continued local/integration validation only. It is **not production-deployable** until PostgreSQL migrations, multi-worker recovery, dataset retention/reproducibility, resource isolation and full post-change suites are validated in an authorized staging environment. No deployment occurred.

## Sprint 6 readiness

Prerequisites are a supervised worker service, staging migration/recovery evidence, complete period-aware backtest partitioning, stronger multiple-testing controls and a final browser/full-suite pass. Any future champion designation must remain an explicit human research action and must not be connected to execution automation.

## Status summary

- **IMPLEMENTED:** frozen matched experiment identity, multi-challenger orchestration, dataset/cost compatibility gates, idempotent candidate mapping, lifecycle/holdout lineage, authenticated APIs, dashboard submission flow.
- **TESTED:** focused backend tests, typecheck/build, Chromium matched-submit flow, Python compilation.
- **PARTIALLY IMPLEMENTED:** provider-specific period slicing, campaign-wide statistical adjustment, worker supervision/resource sandboxing.
- **BLOCKED:** production/staging validation without authorization; universal independent holdout claims after reuse.
- **NOT IMPLEMENTED:** automatic promotion, strategy switching, live execution changes, financial-ledger mutation.
