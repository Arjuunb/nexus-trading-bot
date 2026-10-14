# NEXUS Strategy Intelligence — Sprint 4.6

## Executive summary

Sprint 4.6 upgrades the Sprint 4.5 synchronous research bridge to a durable,
journal-backed asynchronous worker path. A research request is still advisory
and isolated from trading: the API persists a frozen job, a daemon worker
claims it with a lease, runs the existing historical simulator, and commits one
immutable validation result plus the terminal job state under the journal lock.
No ledger, balance, order, position, active strategy setting, or execution mode
is modified.

## Repository audit findings

The repository already had `research_jobs`, immutable
`strategy_validation_runs`, the Sprint 4.5 `ResearchJobService`, SQLite journal
transactions, an instance supervisor, and FastAPI startup/shutdown hooks. No
general-purpose queue was present or required. The smallest compatible design
was therefore a database-backed queue using the existing SQLite writer lock.

## Files modified

* `data/journal_context_migrations.py` — additive lease, attempt, event, and
  queue indexes/tables.
* `data/journal_context_store.py` — lifecycle transition validation, atomic
  claims, leases, heartbeats, cancellation, expiry recovery, fencing, and
  atomic result commitment.
* `services/research_jobs.py` — asynchronous `ResearchJobWorker`, cooperative
  cancellation/timeout checkpoints, retry classification, and validation
  record construction.
* `routers/strategy_intelligence.py` — `/run` now accepts work and starts the
  worker without waiting for the backtest; committed-results endpoint added.
* `app.py` — worker shutdown acknowledgement.
* `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx` — polling,
  durable status display, start-worker and cancellation controls.
* `tests/test_research_jobs.py` — worker race, lease recovery/fencing,
  cancellation race, and logical-result idempotency coverage.

## Database migrations

The existing context migration remains additive and re-runnable. New nullable
lease fields and defaults are added to `research_jobs`; `research_job_attempts`
and `research_job_events` are append-only metadata tables. Queue, owner, and
result-key indexes are created forward-only. Financial tables are untouched.

Rollback is forward recovery: stop research workers, retain the superset
metadata, and roll application code back only if it can ignore the extra
columns. Production/staging migration execution was not authorized or run in
this sprint.

## Worker architecture and lifecycle

`POST .../research-jobs` creates `QUEUED`. `POST .../{id}/run` starts the
process-local daemon and returns immediately with `execution=asynchronous`.
The worker claims one eligible row as `CLAIMED`, records an attempt and fencing
token, transitions to `RUNNING`, renews a lease at simulator checkpoints, and
then commits `COMPLETED` and the immutable validation row in one journal
transaction. Invalid transitions are rejected. Terminal states are
`COMPLETED`, `FAILED`, `CANCELLED`, or `INTERRUPTED`.

Expired leases are reconciled to `INTERRUPTED` and bounded `RETRY_PENDING`.
The current default is one active research job per process and three attempts.
The worker is intentionally local to the application process; a deployment
with multiple processes must run one worker per process against the same
authoritative database and use the same claim transaction.

## Atomic claim, fencing, and recovery

SQLite's existing `BEGIN IMMEDIATE` journal transaction serializes claimers;
the claim predicate excludes live leases and selects the oldest eligible job.
Every attempt receives a unique token. Heartbeats, cancellation completion,
and result commitment require the current token. A stale worker therefore
cannot commit after lease takeover. Recovery is at-least-once physical
execution with exactly-once logical result identity (`validation:<job_id>`).

The implementation does not claim hard process isolation or exactly-once CPU
execution. Cooperative timeout/cancellation checks occur between simulator
segments; a single long simulator call can delay cancellation until it
returns.

## Cancellation, retries, and limits

Queued/retry-pending jobs cancel immediately. Claimed/running jobs move to
`CANCEL_REQUESTED` and are cancelled at the next safe checkpoint. Transient
timeouts/connection/OS failures retry up to the persisted attempt limit;
configuration and simulator/data failures become `FAILED`. A timeout is a
cooperative runtime limit, not a hard OS memory/CPU boundary.

## Backtest integration and isolation

The worker calls the existing `get_bars` and `strategy_presets._run_on` path
with the frozen request/configuration. It writes only research metadata and
`strategy_validation_runs`; it never calls order, ledger, paper, or live
execution APIs. Missing real data remains fail-closed. Cost and dataset
identity remain part of the immutable report.

## Qualification integration

Only a committed `COMPLETED` result is available through
`GET .../{id}/results` and therefore eligible for advisory qualification
refresh. Failed, cancelled, interrupted, or partial jobs are excluded. No
strategy is promoted, qualified for live execution, or automatically selected.

## API and dashboard

Existing research routes remain owner-scoped and authenticated. `/run` is now
an asynchronous compatibility entry point; clients must poll the job route.
`/results` returns 409 until a durable result exists. The dashboard displays
the durable status and polls every two seconds, with explicit start and cancel
controls. No fabricated progress is generated beyond the persisted bounded
progress values.

## Warning audit

The current full Hub run reports 86 warnings: FastAPI `on_event` deprecation
warnings and third-party `aiohttp` cleanup warnings. They are pre-existing
framework/runtime warnings, not suppressed by Sprint 4.6. The network-backed
real-data test path also logs a fail-closed Binance network error when external
data is unavailable.

## Tests and validation

At the time of this report:

* Focused research/intelligence tests: **78 passed, 4 warnings**; the
  dedicated research-job file: **7 passed**.
* Automation Hub suite (full run before the final async-route assertion was
  added): **2,900 passed, 25 skipped, 86 warnings**.
* Repository-root suite (same full-run point): **3,430 passed, 25 skipped, 84
  warnings**.
* Dashboard typecheck and production build: passed.
* Browser strategy-intelligence suite using `/usr/bin/chromium`: **9 passed**.
* Authorized staging migration test: not run.

## Known limitations and blockers

* PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` is not implemented because
  the active repository path is SQLite; staging validation is still required
  before claiming multi-process production readiness.
* Worker startup is lazy (the first `/run` request starts the process-local
  daemon); a separate worker process/service and hard resource sandbox are not
  implemented.
* Queue position, persistent progress checkpoints inside the simulator, and
  browser research-flow coverage require additional staging/browser fixtures.
* No production access or deployment was performed.

## Sprint 5 prerequisites

Authorize a production-shaped migration test, select the supported deployment
worker topology, add process-level resource isolation if required, and run the
browser research flows against an authenticated staging dataset before any
operational promotion decision.
