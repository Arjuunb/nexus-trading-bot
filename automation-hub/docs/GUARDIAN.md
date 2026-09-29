# Nexus Guardian

Guardian is the platform's independent observer. All eight phases are built:
Phase 1 (foundation), Phase 2 (deep strategy telemetry), Phase 3 (incident
intelligence), Phase 4 (execution and risk intelligence), Phase 5 (strategy
research), Phase 6 (evidence reasoning), Phase 7 (controlled recovery) and
Phase 8 (reports and remote awareness).

It reads the platform and never trades. Its research changes no strategy,
its reasoning is advice, and its only operational action (restarting an
instance worker) is off unless the owner turns it on.

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
| Incidents | `services/guardian/incidents.py` | Phase 3: grouping by root, diagnosis with confidence, lifecycle, timeline, correlation. |
| Anomalies | `services/guardian/anomalies.py` | Phase 3: deviations from each stream's own baseline. |
| Integrity | `services/guardian/integrity.py` | Phase 4: stage-by-stage reconciliation and the global paper/live exposure view. |
| Instance traces | `services/strategy_trace.py` | Phase 2: the per-candle trace from the engine's own outcome. Outside the Guardian package because it reads the strategy gate registry. |
| API | `routers/guardian.py` | `GET /guardian/status`, `/events`, `/events/{id}`, `/strategies`, `/almost-trades`, `/incidents`, `/incidents/{id}`, `/anomalies`, `/integrity`, `/actions`, `/catalogue`. GET only. |
| UI | `automation-hub-dashboard/src/pages/GuardianHub.tsx` | One sidebar entry, "Guardian", with six tabs: Command Center, Incidents, System Map, Strategies, Risk & Integrity and Activity. |

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
  and nothing in Guardian can stop a worker. The one exception is the
  owner-enabled restart (Phase 7), which is off by default.
* Trading never waits on Guardian. `emit()` queues or drops. With Guardian
  not started, nothing is installed and `emit()` returns immediately.
* Guardian is built from read-only callables, never from the managers or
  runtimes themselves. It holds no method it could use to start, stop,
  reconfigure or trade anything. The exception is one restart callable the
  app hands the recovery controller. It refuses any instance whose owner
  has not asked it to run, and it acts only when the owner enables its
  policy.
* Tests enforce this boundary:
  * the Guardian package may import only the standard library and the
    redaction helper;
  * `sources.py` may call only read methods;
  * the `/guardian/*` read API is GET-only. The only POSTs are the owner's
    research controls and questions (`routers/guardian_research.py`), which
    need the control credential;
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

How far the market moved afterwards (the "+2.7R" in PRD §9) is still not
measured. That needs candles after the setup. The Phase 5 research pipeline
works from finished trades, not from setups that were never taken.

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

Research hypotheses and their pipeline are on the Research tab (Phase 5).
`GET /guardian/events/{id}` returns one
trace and `GET /guardian/almost-trades` the register.

### Cost to trading

The trace is built on the trading thread (about 40 µs per candle measured
here). The event's validation and secret-stripping run on Guardian's bus
thread (`emit_deferred`). A full queue drops the trace and counts it. A
broken trace builder is caught. Tests trade the same with Guardian's queue
full and with the builder raising.

## Phase 3: Incident intelligence

### One outage, one incident (PRD §29)

Grouping follows the dependency graph Guardian already resolves. Each
unhealthy component is traced to the component whose own failure explains
it (its *root*). Every component with the same root belongs to one incident.
When two or more independent live feeds fail together, the root is Binance
USD-M itself. So an outage that stalls three instances and both labs is one
incident with every component listed, not a dozen alerts.

Engine events that happen between Guardian's cycles are attached to the
incident key the component's health would use, so an event and a health
state never open two incidents for one fault:
* `stale_candle`, `websocket_disconnected`, `worker_crashed`,
  `missing_htf_candle` and `collector_failed` open a signal;
* their counterparts clear it.

A component that is only DEGRADED (starting, warming up) opens an incident
only after staying so for 5 minutes (`degraded_grace_s`).

### Lifecycle and history (PRD §36)

OPEN → RECOVERED (every affected component healthy again) → CLOSED once
recovery has held for the verification period (`incident_verify_s`, 60 s).
Failing again before that reopens the same incident. Every step is appended
to `guardian_incident_log`, which refuses UPDATE and DELETE. The incidents
table refuses DELETE: a closed incident is kept as history.

### Root cause with honest confidence (PRD §11)

Each incident states, in order:
* the symptom;
* the affected components;
* the upstream (root) component;
* the root-cause candidate;
* the supporting evidence;
* a confidence level;
* why it has that confidence;
* a recommended action. This is advice only; Guardian takes no action.

| Situation | Confidence |
|---|---|
| Two or more live feeds fail together | HIGH CONFIDENCE (Binance's side is not visible, so not CONFIRMED) |
| One feed fails while others receive data | HIGH CONFIDENCE that it is local to that consumer |
| The only live feed fails | POSSIBLE: one consumer cannot tell its connection from Binance |
| A worker stopped and recorded its error | CONFIRMED (its own report) |
| A worker stopped without an error | UNKNOWN cause |
| Missing higher timeframe | CONFIRMED (the engine's own check) |
| Database, journal, lab thread, Guardian itself | CONFIRMED (the component's own report) |
| A Guardian collector cannot read something | UNKNOWN |

### Timeline and correlation (PRD §10, §22)

An incident's timeline is rebuilt from the evidence. It covers everything
the affected components, their feeds and the root reported from 10 minutes
before the start until the close, with the incident's own log entries
interleaved.

Open incidents that overlap are *related*:
* PROBABLE when they share an instance or component (for example a stale
  feed and a missing higher timeframe on the same instance);
* POSSIBLE when they only overlap in time.

### Anomalies (PRD §21)

Each detector judges a stream against its own history, and every anomaly
says it is not a failure:
* **evaluations_stopped**: a stream that evaluates every candle has been
  silent for three candle intervals while its component and feed report
  healthy.
* **evaluation_latency**: candle close to evaluation, against the stream's
  7-day 95th percentile (at least 50 samples).
* **setup_drought**: no setups today where the stream's 7-day rate makes
  that improbable (Poisson p < 0.01 at today's evaluation count; needs all
  7 days of history).
* **rejection_mix**: a rejection reason's share moved by more than 20
  points and 3 standard errors (at least 50 evaluations in each window).

Without the history a detector needs, it reports nothing. Each anomaly is
reported once when it appears and once when it clears.

### API and UI

API: `GET /guardian/incidents?state=active|OPEN|RECOVERED|CLOSED`,
`/guardian/incidents/{id}` (diagnosis, log, timeline, related) and
`/guardian/anomalies`.

UI: a new **Incidents** tab. The Command Center shows open incidents and
anomalies.

## Phase 4: Execution and risk intelligence

### Every stage reconciles (PRD §23, §25)

Guardian reads each account's own records through read-only connections:
* the instance ledger and the Adaptive lab ledger (fills, positions, trades,
  order intents);
* the SMC and PA labs' paper broker;
* the journal's trade records.

It reports any record with no counterpart at the next stage:

| Rule | Severity | Meaning |
|---|---|---|
| `fill_without_position` | HIGH | A fill whose position or trade record does not exist |
| `position_trade_mismatch` | HIGH | A position and its trade disagree about whether it is open |
| `trade_not_journalled` | HIGH | A completed trade with no journal record after the grace period (15 min) |
| `open_trade_not_journalled` | WARNING | An open trade with no journal record after the grace period |
| `intent_unresolved` | WARNING | An order intent still pending or claimed after 30 minutes |
| `lab_position_not_journalled` | WARNING | An open lab position with no open journal record |
| `journal_incomplete` | WATCH | Closed journal records the journal itself grades MINIMAL |
| `live_exposure_while_locked` | CRITICAL | A position in an account not labelled paper while live routing is locked |

Notes:
* Legs created by a partial reduce are judged with their root trade.
* PARTIAL journal records are not flagged. A replayed trade has no quote
  evidence, and that is not a fault.
* A journal Guardian cannot read is reported as such. Guardian then makes no
  journal findings at all rather than guessing.

A rule is reported once when it starts failing (`integrity_violation`) and
once when it clears (`integrity_resolved`). Both are incident signals, so a
journal gap becomes a CONFIRMED incident that recovers when the recorder
catches up. Guardian repairs nothing.

### Global risk observer (PRD §24)

Open exposure is added up across every instance and lab from the positions
themselves:
* risk to stop is entry-to-stop distance times size;
* a position with no recorded stop counts as unknown risk, never as zero.

It is grouped by symbol and side (listing the accounts) and by correlated
cluster. Crypto majors form one cluster, the same rule as the signal
pipeline's correlation guard, so "three BTC-quoted longs" reads as one bet.

**Paper and live are never added together.** An account is paper only when
its own record says so. The known paper labels are the broker's `PAPER` and
the labs' `SMC_LAB` and `PA_LAB`. Any other label is kept out of the paper
totals and listed separately. If such an account holds a position while live
routing is locked (the broker registry's own answer), that is CRITICAL.

API: `GET /guardian/integrity`. UI: a **Risk & Integrity** tab. The Command
Center shows the finding count and paper open risk.

## Phase 5: Strategy research (PRD §12–16, §26, §38)

`services/guardian/research.py`. Observations become hypotheses, never
changes. The engine reads the journal's finished trades read-only. It writes
only to Guardian's own tables, `guardian_hypotheses` and
`guardian_hypothesis_log`.

**Evidence.** Only `FORWARD_PAPER` records with a measured R are used.
Simulated, backtested, research and migrated records never enter. Results
are grouped by record source, strategy *and version* (§16), so two versions
are never combined.

**Strategy analyst (§12).** Each version's trades are split by:
* side, session, regime and HTF bias;
* symbol, timeframe and setup type;
* winners against losers.

A cohort is compared with the rest only when both have at least 20 trades
(Welch's test).

**Hypotheses (§13).** A cohort that loses (mean R below 0) and is
significantly worse than the rest (p < 0.05) becomes a filter hypothesis:
"skipping X may improve expectancy". Its status is UNPROVEN. It is found on
the first 60% of trades by close time only, which leaves a true hold-out. A
hypothesis is never deleted (a trigger refuses it). A rejected idea is
therefore never rediscovered (§26). A p-value too small to print reads
`p<0.00001`, never `p=0`.

**Pipeline (§14).** The stages run in order, and none is skipped:

| Stage | What passes it |
|---|---|
| Historical backtest | The filter raises expectancy on the discovery trades. |
| Out of sample | The held-out trades (never seen at discovery) show the same effect. It waits for 10 cohort trades. |
| Walk forward | The effect holds in all but at most one of four chronological folds. |
| Stress test | 95%+ of 1,000 bootstrap resamples improve, and the effect survives removing the best 5% of trades. |
| Forward paper | Trades closed *after* the hypothesis was created show the effect. It waits for 20 cohort trades. |
| Statistical comparison | Over every recorded trade the cohort is worse than the rest at p < 0.01. |
| Recommendation | Every evidence stage passed. |
| Owner approval | The owner's alone. |

A failed stage ends the idea as REJECTED_BY_EVIDENCE and it is kept.
Each test replays the filter over recorded trades, and every stage says so.
That is exact for a filter, but it is not a candle-level backtest: it cannot
see trades the strategy would have taken instead.

**Owner controls (§38).** These are `POST /guardian/research/{id}/action`,
behind the control credential:
* REVIEW;
* REJECT;
* SEND_TO_BACKTEST (only at that stage);
* SEND_TO_FORWARD_PAPER (only once every earlier stage passed);
* APPROVE_FOR_DEVELOPMENT (only when Guardian recommends it).

Approval means "approved for a person to develop". Production code is
unchanged until a person implements, tests and deploys it. There is no
"optimize production" control, and a test checks that no route resembles
one.

Research runs in Guardian's own loop at most once an hour. It publishes
`hypothesis_created` and appears on a **Research** tab. A journal Guardian
cannot read is a failing collector, never an empty answer.

## Phase 6: Evidence reasoning (PRD §20, §27, §43)

`services/guardian/evidence.py` builds the evidence pack:
* Guardian's own findings, each with a citable id: platform state,
  incidents, anomalies, integrity findings, strategies, almost-trades,
  hypotheses, recent warnings and Guardian's actions;
* bounded, saying what was left out;
* run through the platform's secret redaction.

The model never sees a database, a ledger or a credential.

`services/guardian_reasoning.py` sends the pack and the owner's question to
Claude.
* The question is scrubbed of secrets too.
* The request uses a JSON schema: answer, confidence, citations,
  limitations.
* The system prompt forbids recommending any production, risk, SL/TP, RR,
  position, credential or live change. It says a correlation is not a proven
  improvement.
* Every citation is checked against the pack. Ids that do not exist are
  listed as ignored. An answer citing nothing real is UNKNOWN, whatever
  confidence the model claimed.
* Refusals and API failures are recorded, never raised.
* Every question is recorded in the append-only action audit with the
  pack's SHA-256, so what the model was shown can be verified later.

It is **off until a key is configured** (`HUB_LLM_API_KEY` or
`ANTHROPIC_API_KEY`, as the Strategy Studio uses). With no key nothing is
sent. The model defaults to `claude-opus-5-5` and can be changed with
`HUB_GUARDIAN_LLM_MODEL`. API: `GET /guardian/reasoning`, and
`POST /guardian/reasoning/ask` (control credential). UI: **Ask Guardian**.

## Phase 7: Controlled recovery (PRD §39, §40)

`services/guardian/recovery.py`. Guardian may take two operational actions,
and never a trading one:

| Action | Automatic? | What it does |
|---|---|---|
| `GATHER_DIAGNOSTICS` | Yes, for HIGH/CRITICAL incidents | Records the incident, component states and recent warnings. Changes nothing. |
| `RESTART_INSTANCE_WORKER` | **Off by default.** On only when the owner lists it in `HUB_GUARDIAN_RECOVERY` | The instance manager's own staged Full Bot Reboot. |

A restart also requires:
* a CONFIRMED or HIGH CONFIDENCE diagnosis;
* a worker that is not alive;
* an instance its owner wants running. Guardian never starts a stopped
  instance.

The reboot's validation and fail-closed checks stay the manager's. There
are at most 3 restarts an hour and a 15-minute cooldown per target.

Every decision is recorded, whether taken or not. When the evidence
supports a restart but the policy is off, it is recorded once as
`NOT_TAKEN_POLICY_DISABLED`. The Command Center's boundary line follows the
policies actually on: "Guardian observes only" becomes "may restart an
instance worker…" only when the owner enabled it.

API: `GET /guardian/recovery`. UI: **Reports & Recovery**.

## Phase 8: Reports and remote awareness (PRD §28–31)

`services/guardian/reports.py`. A daily and a weekly report are issued once
per finished period. They are kept append-only (a trigger refuses edits) and
published as `report_issued`.

* **Coverage.** Every report states how much of its period Guardian actually
  observed. This is measured from Guardian's own cycles, so a crash or a
  stopped container is never counted as watched. A period Guardian never
  observed gets no report.
* **Numbers.** Every figure has a source. Trades are forward-paper journal
  records. Incident-free time counts only the time Guardian observed. What
  could not be measured says "not measured" or "not checked", never 0.
* **Weekly report.** It covers:
  * reliability by incident kind;
  * strategy performance;
  * evaluation latency and slippage;
  * refused setups and journal integrity;
  * recurring rejection reasons;
  * missed-opportunity candidates (research only);
  * hypotheses;
  * unresolved incidents and engineering priorities.
* **Notifications** go through the platform's Telegram notifier:
  * one message when a HIGH or CRITICAL incident opens and one when it
    closes, however many updates it gets (§29);
  * one per report;
  * never for strategy inactivity;
  * never for incidents from before notifications began: deploying this
    sends none of Guardian's history.

  Each message is scrubbed of secrets and recorded as a `NOTIFY` action.
  With no channel configured it is recorded as `NO_CHANNEL`, not dropped
  silently.

API: `GET /guardian/reports`. UI: **Reports & Recovery**.

## PRD §44 acceptance tests: status after Phase 8

| # | Test | Status |
|---|---|---|
| 1 | Detects stale candles | **Done.** Tested with a real manager and the engine's own freshness check. |
| 2 | Detects missing HTF candles | **Done.** A real engine whose mandatory higher timeframe cannot load: one event, and one on recovery. |
| 3 | Detects disconnected feeds | **Done** (instances and labs). |
| 4 | Inactivity vs infrastructure failure | **Done** at component level: a quiet healthy instance raises nothing. |
| 5 | Traces rejected setups | **Done.** Tested with the real 3-Candle Rejection (instance), SMC (agent) and Price Action (lab) strategies. |
| 6 | Records almost-trades | **Done.** One per setup, never a rule verdict. |
| 7 | Detects worker crashes | **Done.** A real crash inside the worker thread. |
| 8 | Detects journal failures | **Done.** A journal that cannot read its ledger is DEGRADED; a completed trade with no journal record is a HIGH finding and a CONFIRMED incident. |
| 9 | Order/journal inconsistency | **Done.** Fill, position, trade and journal reconciled; tested with real trades and a corrupted ledger copy. |
| 10 | Correlates related incidents | **Done.** A Binance outage across two instances and a lab is one incident; overlapping incidents on one instance are linked. |
| 11 | Survives trading-worker failure | **Done.** |
| 12 | Trading survives Guardian failure | **Done.** A real trade with Guardian's store failing and its queue full. |
| 13–15 | Cannot modify strategy, raise risk or enable live | **Done by construction and tested.** |
| 16 | Research strategies isolated | **Done.** Research reads forward-paper journal records only, writes only Guardian's research tables, never combines versions, and approval changes no strategy (the journal's hash is unchanged). |
| 17 | Paper/live ledgers isolated | **Done.** Paper totals hold only paper-labelled accounts; any other account with a position while live is locked is CRITICAL. |
| 18 | Every Guardian action audited | **Done.** Append-only. |
| 19 | Duplicate alerts grouped | **Done.** One incident per outage, and one notification when it opens and one when it closes, however many updates. |
| 20 | Only evidence-backed conclusions | **Done.** A blind collector reports UNKNOWN; one socket is not called a Binance outage; a reasoning answer's citations are checked against the evidence and an uncited answer is UNKNOWN. |

Tests:
* `tests/test_guardian.py` (16), `tests/test_guardian_strategy.py` (15),
  `tests/test_guardian_incidents.py` (13), `tests/test_guardian_integrity.py` (10);
* `tests/test_guardian_research.py` (11), `tests/test_guardian_reasoning.py` (8),
  `tests/test_guardian_recovery.py` (10), `tests/test_guardian_reports.py` (12);
* `automation-hub-dashboard/e2e/guardian.spec.ts` (13).

The UI's mock data is the real service's output
(`e2e/fixtures/generate_guardian_fixture.py`):
* strategy telemetry comes from real strategies;
* research runs over journal records seeded through the journal's store, on
  a simulated clock;
* the reasoning answer is the real `ask()` with a stub model reply.

The reasoning tests never call the API: a stub records the exact request.

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
* Research is a filter replay over recorded trades, not a candle-level
  backtest. It needs at least 40 finished forward-paper trades per strategy
  version before it can say anything, so on a new deployment it will be
  quiet for a while, correctly. Many cohorts are tested at once, which is
  why an idea must also pass the hold-out, walk-forward, stress and forward
  stages before Guardian recommends it.
* Report coverage is measured from the deployment of Phase 8. Periods before
  it get no report.
* Instances appear only while their owner wants them running. A lab's feed
  counts only while the lab has an active session, so an idle lab is never
  blamed for a feed it is not using.
