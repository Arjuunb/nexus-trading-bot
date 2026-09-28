# Nexus Guardian: Phase 1 (Foundation)

Guardian is the platform's independent, read-only observer. Phase 1 makes the
platform observable, as the PRD asks ("Do NOT begin with AI. First make the
platform observable"):

* the Guardian service;
* the unified event schema;
* an event bus and immutable event storage;
* heartbeats and self-monitoring;
* dependency-aware system health;
* a basic Command Center.

Later phases (strategy telemetry, incidents, reconciliation, research,
recovery, reports) are not built yet, and the UI does not pretend they are.

## What it is made of

| Part | File | What it does |
|---|---|---|
| Event schema | `services/guardian/schema.py` | The PRD §6 fields and the §7 event catalogue (plus Guardian's own types). Also defines the severities INFO/WATCH/WARNING/HIGH/CRITICAL and the health states HEALTHY/DEGRADED/BLOCKED/FAILED/UNKNOWN. An event is validated and stripped of secrets when it is built. |
| Evidence store | `services/guardian/store.py` | Its own `guardian.db` (`HUB_GUARDIAN_DB`), separate from every trading database. `guardian_events` and `guardian_actions` are append-only: triggers abort UPDATE and DELETE. |
| Event bus | `services/guardian/bus.py` | `publish()` is O(1), never blocks and never raises. A full queue drops the event and counts it. A failed write keeps its batch and retries it. |
| Health model | `services/guardian/health.py` | Resolves the dependency graph. A component whose dependency failed is BLOCKED and names the blocker; only its own failure makes it FAILED. |
| Sources | `services/guardian/sources.py` | Read-only adapters over the instance managers, the lab runtimes and the journal recorder. They read in-memory state only, so there is no Supabase load. |
| Service | `services/guardian/service.py` | Runs a cycle every 15 s (`HUB_GUARDIAN_INTERVAL`). Emits `health_changed` only on a transition, monitors itself and audits its own actions. |
| Entry point | `services/guardian/__init__.py` | `emit()`, the one call trading code makes, and the instance lifecycle mapping. |
| API | `routers/guardian.py` | `GET /guardian/status`, `/events`, `/actions`, `/catalogue`. GET only. |
| UI | `automation-hub-dashboard/src/pages/GuardianHub.tsx` | One sidebar entry, "Guardian", with three tabs: Command Center, System Map and Activity. |

## What it observes today

* **Trading Instances and the Adaptive lab's instances.** For each worker:
  is it alive, its lifecycle state, and its market-data status. Every
  lifecycle event (connected, stale, disconnected, crashed, stopped, signal,
  order created or rejected) reaches Guardian through the one telemetry choke
  point, `services/instance_telemetry.log_event`.
* **SMC and Price Action labs.** Worker thread, active session, and the
  stream's own health: state, reliability and the failing dependency.
* **Binance USD-M.** A summary across all live consumers. It is FAILED only
  when two or more independent consumers are all without data. One dead
  socket cannot be told apart from a Binance outage.
* **Ledger database.** The status monitor's confirmed state. Guardian does
  not probe it again.
* **Trade journal.** The recorder's own last pass. A ledger it skipped, such
  as the Supabase ledger in production, makes the journal DEGRADED with the
  reason.
* **Guardian itself.** Drops, write failures, failing collectors, and its
  heartbeat judged at read time. A stopped Guardian reads FAILED.

## Isolation and authority

* Guardian runs on its own daemon threads. A worker crash cannot stop it,
  and nothing in Guardian can stop a worker.
* Trading never waits on Guardian. `emit()` queues or drops. With Guardian
  not started, nothing is installed and `emit()` returns immediately.
* Guardian is built from read-only callables, never from the managers or
  runtimes themselves. It holds no method it could use to start, stop,
  reconfigure or trade anything.
* Tests enforce this boundary:
  * the Guardian package may import only the standard library and the
    redaction helper;
  * `sources.py` may call only read methods;
  * the API is GET-only;
  * live routing stays locked.
* Every Guardian action is recorded append-only in `guardian_actions`. In
  Phase 1 the only actions are `GUARDIAN_STARTED` and `GUARDIAN_STOPPED`;
  there is no recovery action yet.

## PRD §44 acceptance tests: status after Phase 1

| # | Test | Status |
|---|---|---|
| 1 | Detects stale candles | **Done.** Tested with a real manager and the engine's own freshness check. |
| 2 | Detects missing HTF candles | Phase 2 (strategy telemetry). |
| 3 | Detects disconnected feeds | **Done** (instances and labs). |
| 4 | Inactivity vs infrastructure failure | **Done** at component level: a quiet healthy instance raises nothing. |
| 5 | Traces rejected setups | Phase 2. |
| 6 | Records almost-trades | Phase 2. |
| 7 | Detects worker crashes | **Done.** A real crash inside the worker thread. |
| 8 | Detects journal failures | **Partly.** A journal that cannot read its ledger is DEGRADED; per-trade journal checks are Phase 4. |
| 9 | Order/journal inconsistency | Phase 4. |
| 10 | Correlates related incidents | Phase 3. |
| 11 | Survives trading-worker failure | **Done.** |
| 12 | Trading survives Guardian failure | **Done.** A real trade with Guardian's store failing and its queue full. |
| 13–15 | Cannot modify strategy, raise risk or enable live | **Done by construction and tested.** |
| 16 | Research strategies isolated | Phase 5. |
| 17 | Paper/live ledgers isolated | Phase 4 (global risk observer). |
| 18 | Every Guardian action audited | **Done.** Append-only. |
| 19 | Duplicate alerts grouped | **Partly.** A state is reported once per change; incident grouping is Phase 3. |
| 20 | Only evidence-backed conclusions | **Done** for health. A blind collector reports UNKNOWN, and one socket is not called a Binance outage. |

Tests: `tests/test_guardian.py` (16 tests) and
`automation-hub-dashboard/e2e/guardian.spec.ts`. The UI's mock data is the
real service's output (`e2e/fixtures/generate_guardian_fixture.py`).

## Known limits

* Guardian runs as threads in the app process, not as a separate container.
  A worker failure cannot stop it, but a crash of the whole process stops
  both. Running it as its own process is a later step. The collectors read
  in-memory state today, so that move would need them to read durable state
  or the API instead.
* No retention policy yet. Phase 1 writes only state changes and lifecycle
  events, so volume is low. Aggregating high-volume telemetry (§41) comes
  with strategy telemetry in Phase 2.
* The database state comes from the status monitor, which confirms a change
  only after two samples. Guardian can therefore lag the ledger by up to two
  minutes.
* Instances appear only while their owner wants them running. A lab's feed
  counts only while the lab has an active session, so an idle lab is never
  blamed for a feed it is not using.
