# Nexus Guardian

Guardian is the platform's independent, read-only observer. Built so far:
Phase 1 (foundation) and Phase 2 (deep strategy telemetry). Phases 3–8 are
not built yet, and the UI does not pretend they are.

## Phase 1: Foundation

Phase 1 makes the platform observable, as the PRD asks ("Do NOT begin with
AI. First make the platform observable"):

* the Guardian service;
* the unified event schema;
* an event bus and immutable event storage;
* heartbeats and self-monitoring;
* dependency-aware system health;
* a basic Command Center.

## What it is made of

| Part | File | What it does |
|---|---|---|
| Event schema | `services/guardian/schema.py` | The PRD §6 fields and the §7 event catalogue (plus Guardian's own types). Also defines the severities INFO/WATCH/WARNING/HIGH/CRITICAL and the health states HEALTHY/DEGRADED/BLOCKED/FAILED/UNKNOWN. An event is validated and stripped of secrets when it is built. |
| Evidence store | `services/guardian/store.py` | Its own `guardian.db` (`HUB_GUARDIAN_DB`), separate from every trading database. `guardian_events` and `guardian_actions` are append-only: triggers abort UPDATE and DELETE. |
| Event bus | `services/guardian/bus.py` | `publish()` is O(1), never blocks and never raises. A full queue drops the event and counts it. A failed write keeps its batch and retries it. |
| Health model | `services/guardian/health.py` | Resolves the dependency graph. A component whose dependency failed is BLOCKED and names the blocker; only its own failure makes it FAILED. |
| Sources | `services/guardian/sources.py` | Read-only adapters over the instance managers, the lab runtimes and the journal recorder. They read in-memory state only, so there is no Supabase load. |
| Service | `services/guardian/service.py` | Runs a cycle every 15 s (`HUB_GUARDIAN_INTERVAL`). Emits `health_changed` only on a transition, monitors itself and audits its own actions. |
| Entry point | `services/guardian/__init__.py` | `emit()`, the one call trading code makes, and the instance lifecycle mapping. `emit_deferred()` builds the event on Guardian's thread instead (Phase 2). |
| Strategy telemetry | `services/guardian/strategy.py` | Phase 2: SMC and PA trace mapping, the read-only lab readers, the almost-trade rules, the strategy view and closed-trade results. |
| Instance traces | `services/strategy_trace.py` | Phase 2: the per-candle trace from the engine's own outcome. Outside the Guardian package because it reads the strategy gate registry. |
| API | `routers/guardian.py` | `GET /guardian/status`, `/events`, `/events/{id}`, `/strategies`, `/almost-trades`, `/actions`, `/catalogue`. GET only. |
| UI | `automation-hub-dashboard/src/pages/GuardianHub.tsx` | One sidebar entry, "Guardian", with four tabs: Command Center, System Map, Strategies and Activity. |

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

## Phase 2: Deep strategy telemetry

Every strategy evaluation now leaves a **decision trace** (PRD §8): the
strategy's conditions in order, which passed, which one stopped it, and which
were never reached; then the engine's gates (Decision Brain, context, risk,
execution) the same way; then the final outcome.

### Where traces come from

| Producer | How Guardian gets it | Per |
|---|---|---|
| Trading Instances (main and Adaptive lab) | The engine publishes it (`services/strategy_trace.py`, called from `AutoStrategyEngine._process_bar`) | closed candle, live engines only |
| SMC lab | Guardian reads the agent journal's `agent_decisions` | closed candle |
| Price Action lab | Guardian reads the lab's `pa_evaluations`, including each later lifecycle step (risk refusal, order, fill, exit) | closed candle and each step |

* **Instances.** The trace is not a second implementation of any strategy.
  It locates what the engine already decided: the strategy's own blocker code
  inside its declared gate sequence (`services/strategy_visual_registry.py`,
  the same one the Instance Visual Lab draws), the pipeline stage that
  refused a signal, and the Decision Brain verdict. Everything before the
  failing gate passed, the failing gate failed, and everything after it was
  never reached. A code the sequence does not know is shown as UNATTRIBUTED.
  It is never guessed at.
* **A quality gate the owner switched off** reads BYPASSED, never PASS, when
  the Brain would have refused.
* **Replays do not publish.** A replay runs history as fast as it can; it is
  a simulation, not the platform's behaviour.
* **Labs.** Guardian opens the lab databases with SQLite read-only
  connections (`mode=ro`, `query_only`). The database itself refuses a write.
  Reads are incremental, and every event's id is derived from the row it came
  from, so a re-read after a restart adds nothing twice. The PA lab file
  (`price_action_lab.py`) is frozen and was not changed; Guardian only reads
  its table.

### Missing higher-timeframe candles (acceptance test 2)

A live instance reports a missing or stale higher timeframe once, when it
starts, and once when it recovers (`missing_htf_candle` / `stale_htf_candle`
/ `htf_candle_recovered`). It is detected three ways, all from structured
states:
* the engine could not load a mandatory higher-timeframe series;
* the strategy's own code (`HTF_NOT_READY`, `STALE_HTF_CANDLE`, …);
* the Decision Brain's hard block, matched to its exact words.

### Almost-trades (PRD §9)

A trace is an almost-trade when either:
* the strategy's own conditions were all evaluated, exactly one failed, at
  least two passed, and the failed one was a setup condition (not warm-up or
  data, and not invalidated or expired); or
* the setup was complete and exactly one gate refused it. A duplicate, an
  existing position or a pending order does not count.

It is recorded once per setup, however many candles it stayed one condition
short, as a **MISSED OPPORTUNITY CANDIDATE**. It carries the PRD's warning:
this does not mean the rule was wrong, and it is for research only. Nothing
reads it to change a rule. Examples:
* the 3-Candle Rejection pattern filtered by EMA 9/33 (4 of 5);
* the SMC setup with every condition but the rejection candle (6 of 7);
* a PA1 pattern refused only by the pin-bar experiment (2 of 3).

A two-condition strategy with one condition met is not an almost-trade: one
of two is not "most".

How far the market moved afterwards (the "+2.7R" in PRD §9) is not measured
yet. That needs candles after the setup, and it belongs to the research
pipeline (Phase 5).

### Storage

Traces are events in `guardian_events`, so they are immutable evidence.
Two derived tables are maintained as traces arrive, counting each trace only
the first time it is stored:
* `guardian_strategy_rollup`: counts per day, strategy, market, outcome and
  reason (PRD §41 aggregation);
* `guardian_almost_trades`: the register.

Both can be rebuilt from the events.

### Strategy view (PRD §37)

`GET /guardian/strategies?days=N` and the **Strategies** tab show, per
strategy:
* evaluations, setups, entries and refusals;
* the top reasons it did not trade;
* its almost-trades;
* the production version;
* closed-trade results read from the journal's trade records (read-only):
  trades, wins/losses, net P&L, expectancy, average R, profit factor and
  maximum drawdown in R.

Research hypotheses, research versions and forward-paper experiments are
Phase 5 and are shown as not built. `GET /guardian/events/{id}` returns one
trace and `GET /guardian/almost-trades` the register.

### Cost to trading

The trace is built on the trading thread (about 40 µs per candle measured
here). The event's validation and secret-stripping run on Guardian's bus
thread (`emit_deferred`). A full queue drops the trace and counts it. A
broken trace builder is caught. Tests trade the same with Guardian's queue
full and with the builder raising.

## PRD §44 acceptance tests: status after Phase 2

| # | Test | Status |
|---|---|---|
| 1 | Detects stale candles | **Done.** Tested with a real manager and the engine's own freshness check. |
| 2 | Detects missing HTF candles | **Done.** A real engine whose mandatory higher timeframe cannot load: one event, and one on recovery. |
| 3 | Detects disconnected feeds | **Done** (instances and labs). |
| 4 | Inactivity vs infrastructure failure | **Done** at component level: a quiet healthy instance raises nothing. |
| 5 | Traces rejected setups | **Done.** Tested with the real 3-Candle Rejection (instance), SMC (agent) and Price Action (lab) strategies. |
| 6 | Records almost-trades | **Done.** One per setup, never a rule verdict. |
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

Tests: `tests/test_guardian.py` (16), `tests/test_guardian_strategy.py` (15)
and `automation-hub-dashboard/e2e/guardian.spec.ts` (5). The UI's mock data
is the real service's output over real strategies
(`e2e/fixtures/generate_guardian_fixture.py`).

## Known limits

* Guardian runs as threads in the app process, not as a separate container.
  A worker failure cannot stop it, but a crash of the whole process stops
  both. Running it as its own process is a later step. The collectors read
  in-memory state today, so that move would need them to read durable state
  or the API instead.
* No retention policy yet. Phase 2 writes one trace per closed candle per
  live instance and lab (about 288 a day per 5-minute stream). Counts are
  aggregated as they arrive. Pruning old raw traces is not built.
* Main-engine and instance decisions are traced from the moment Guardian
  starts. Nothing before that is backfilled for instances; the labs are read
  from their existing tables.
* The database state comes from the status monitor, which confirms a change
  only after two samples. Guardian can therefore lag the ledger by up to two
  minutes.
* Instances appear only while their owner wants them running. A lab's feed
  counts only while the lab has an active session, so an idle lab is never
  blamed for a feed it is not using.
