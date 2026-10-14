# NEXUS Strategy Intelligence — Sprint 4.5

## Executive summary

Sprint 4.5 connects the Sprint 4 qualification contract to a durable,
authorized research-job pipeline. Jobs use the existing real-data market-data
facade and `services.strategy_presets._run_on` simulator. They never call an
order API, paper ledger, financial journal, live position or active strategy
configuration.

No production data was accessed, and no commit, push or deployment occurred.
The XRPUSDT claim remains unverified.

## Architecture and repository findings

The repository already contained the tested `backtesting` adapter, the real-data
`strategy_presets` simulator, immutable `strategy_validation_runs`, and Sprint 4
chronological/OOS, walk-forward, Monte Carlo, concentration and qualification
primitives. It did not contain a durable generic job bridge. This sprint adds
only that missing bridge; it does not add another simulator or ledger.

```text
Authorized POST /research-jobs
        -> frozen QUEUED metadata
        -> explicit authorized /research-jobs/{id}/run
        -> real candles (require_real=True)
        -> existing strategy_presets simulator
        -> OOS / walk-forward / stress / robustness report
        -> immutable strategy_validation_runs
        -> advisory qualification reads
```

## Files and migration

* `services/research_jobs.py` — bounded lifecycle, dataset manifest, existing
  simulator adapter and validation-result commitment.
* `data/journal_context_migrations.py` — additive `research_jobs` table/indexes.
* `data/journal_context_store.py` — idempotent create/list/get/update operations.
* `routers/strategy_intelligence.py` — authenticated create/list/get/run/cancel
  endpoints and strict bounded request validation.
* `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx` — research
  method selector, queue/run controls and actual job status responses.
* `tests/test_research_jobs.py` plus API/service/qualification regressions.

The job table is mutable only for lifecycle state/progress. Completed validation
artifacts remain in the immutable `strategy_validation_runs` table. Migration is
additive and backward-compatible; rollback retains metadata and runs the prior
application. No financial table is modified or deleted.

## Job lifecycle and execution

Supported types are `HISTORICAL_BACKTEST`, `OUT_OF_SAMPLE_VALIDATION`,
`ANCHORED_WALK_FORWARD`, `ROLLING_WALK_FORWARD`, `COST_STRESS`,
`ROBUSTNESS_EVALUATION` and `QUALIFICATION_REFRESH`. States are `QUEUED`,
`RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` and reserved `INTERRUPTED`.

Idempotency keys are unique per owner. Explicit `/run` claims a queued job,
records progress and commits one logical validation result. Repeated execution
of a completed job is a no-op. Cancellation prevents successful completion when
observed before commitment. The current runner is synchronous behind the
explicit run endpoint; asynchronous lease/heartbeat recovery is a staging
prerequisite and is not claimed complete.

The stable registry ID is adapted to the existing display-name simulator without
changing strategy rules. Jobs call `get_bars(require_real=True)` and fail closed
when real data is unavailable. Bundled and synthetic fallback data is never used.
No research episode is written to the financial ledger or trading journal.

## Dataset identity and validation methodology

Each run fingerprints the exact candle payload and records venue, market type,
symbol, timeframe, source, period, count, timestamp semantics, content hash and
data-quality state. OOS partitions are chronological train/validation/holdout
segments with explicit boundaries and warmup rows excluded from validation
trades. Anchored and rolling walk-forward windows are disjoint in evaluation
time and record development/evaluation results separately. No parameter
optimization is claimed when none occurred.

Dataset changes produce a new manifest identity. Holdout reuse is tracked by the
immutable validation-run record. Missing funding remains `UNKNOWN`, never zero;
stress, seeded Monte Carlo and concentration analysis consume research outcomes
only and document their limitations.

## Qualification integration

Validation records retain strategy/version/fingerprint, dataset revision,
execution mode, method, period, policy/cost/classifier/software versions, seed
and owner/session scope. Sprint 4 qualification consumes those persisted
records and continues to require historical evidence, OOS, walk-forward, cost
coverage, robustness and forward-paper requirements. A successful research job
never changes lifecycle state, execution mode or live authorization.

## API and dashboard

Authorized routes under `/api/v2/strategy-intelligence`:

* `POST /research-jobs`
* `GET /research-jobs`
* `GET /research-jobs/{job_id}`
* `POST /research-jobs/{job_id}/run`
* `POST /research-jobs/{job_id}/cancel`

Requests are strict, bounded models with no filesystem paths or arbitrary code.
Owner/session isolation reuses existing authentication and instance authority.
The Qualification dashboard queues and runs a selected methodology and displays
actual job status; no fabricated metrics are rendered.

## Security, resources and limitations

Bars are limited to 600–10,000 and Monte Carlo runs to 100–5,000. Production
deployment should move execution behind the existing worker lease mechanism and
add per-tenant quotas, timeout/heartbeat recovery, progress checkpoints and a
dataset-manifest catalog. An authorized staging dataset and cost-model registry
are still required. Funding remains unknown when not supplied.

## Validation and readiness

Sprint 4.5 focused checks: **61 passed, 4 warnings**. The post-change
Automation Hub suite passed **2,896 passed, 25 skipped, 87 warnings** and the
repository-root suite passed **3,426 passed, 25 skipped, 84 warnings**.
Dashboard typecheck/build passed and the approved Chromium browser suite passed
**9 tests**. No production-shaped migration or remote staging run was performed.

The implementation is suitable for isolated staging validation, not production
deployment. XRP profitability remains unverified. No commit, push or deployment
was performed.

