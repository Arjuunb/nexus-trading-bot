# Journal, weekly review and agent memory: post-implementation audit (2026-09-27)

Audited at `claude/focused-gates-q5nse7` @ `52af33d`, clean tree. Every
earlier "done" claim was treated as unverified. No production behaviour was
changed by this audit. The only repository changes are:

* this document;
* a correction to `docs/JOURNAL_AUDIT.md`. It had wrongly said replay never
  reaches the old journal; see §2.
* a read-only section B3 in `scripts/journal_provenance.py`;
* the audit harness in `scripts/journal_audit/`.

Defects are listed in §20 with EXPECTED / ACTUAL / ROOT CAUSE / SEVERITY /
FILES / FIX. **None of them has been fixed.**

## How the evidence was produced

`scripts/journal_audit/run_all.sh` rebuilds the whole dataset from nothing in
a scratch directory. No payload is hand-built:

* **Instance path.** The real *3-Candle Rejection · EMA 9/33* strategy reads
  deterministic candles (the fixtures from `tests/test_three_candle_rejection.py`).
  `AutoStrategyEngine._process_bar` then does what a Trading Instance worker
  does: it records the decision, builds the payload and routes it through
  `SignalPipeline`. `ForwardPaperExecutionEngine` parks the intent and a later
  quote fills it. The engine's own stop/target check on the following candles
  closes the position. Only after that does the recorder run.
* **SMC lab.** The frozen `smc_strategy_v1.evaluate()` runs on the seeded
  native market-structure engine. The lab's `synchronize_candidate` places the
  order in each operating mode, and `process_candle` fills and exits it.
* **PA lab.** The lab's `synchronize_strategy` places, fills and exits a
  proposal. **The PA strategy itself was not run.** The proposal is shaped as
  the native engine emits it.
* **Agent.** The SMC agent journal API is called in the order
  `services/smc_agent.py` writes it.
* **Legacy.** The old `DecisionJournal` path runs, then two ledger rows and
  two journal rows are removed, as production lost them.
* **Simulation.** A replay-mode instance runs on the synchronous paper engine.

Every write script refuses to run unless `HUB_DATA_DIR` carries the scratch
marker and every database path resolves inside it. Both refusals were
demonstrated. The real backend (`uvicorn app:app`) then served that
directory, and the built dashboard was driven with Playwright.

Harness results on a fresh directory, run twice with identical outcomes:

| Check | Result |
|---|---|
| Instance record vs ledger, field by field (long TP, short SL) | 33/33, 33/33 |
| SMC and PA lab records vs broker v2 fills | 15/15, 15/15 |
| Crash / restart / duplicate scenarios | 18/18 |
| API vs independent SQL (real backend) | 42/42 |
| Weekly / scheduler / memory / isolation | 79/80 (the failure is D11) |
| UI (real backend): KPIs, rows, origins, decisions, notes equal the API | all equal |

## 1. Safety

| Item | Evidence | Verdict |
|---|---|---|
| Branch / commit / tree | `claude/focused-gates-q5nse7`, `52af33d`, clean before the audit | PASS |
| Live disabled | `BrokerRegistry.live_locked()` returns `True` unconditionally (`services/broker.py:107`); `HUB_ENABLE_EXTERNAL_LIVE` defaults to `0` (`config.py:105`) | PASS |
| Paper only | forward fills carry `paper_only: true, real_execution_allowed: false`; lab states say the same | PASS |
| Frozen files unchanged | `git diff 8fe21ec..HEAD` is empty for `mtf_policy.py`, `price_action_lab.py`, `smc_strategy_lab.py`, `native_price_action.py`, `native_smc.py` (service and router), `smc_strategy_ladder.py`, `smc_strategy_v1.py`, `native_smc_live_visual.py`, both freeze manifests and their tests | PASS |
| Execution-path diff | `signal_pipeline.py`: `journal_notify` attribute + three `_nudge_journal()` calls that swallow errors; `trading_instances.py`: the same nudge after fills; `config.py`: one DB path. No gate, sizing, SL/TP, RR or signal code changed | PASS |
| Risk / fail-closed | unchanged (above); stale data still raises `MarketDataStaleError` and blocks | PASS |

## 2. The original 59 / 53

* **Where they come from.** Proven from code. They are running counters in
  `journal.db:evolution_memory`, incremented only by
  `DecisionJournal.record_exit()` from `SignalPipeline`'s close path. They
  store no trade ids.
* **Correction.** `JOURNAL_AUDIT.md` had said replay never reaches this path.
  **That was wrong.** A replay-mode Trading Instance fills synchronously
  through the same pipeline. The audit reproduced it: a replay 3-Candle
  Rejection trade wrote a journal row with `market_data_mode: "replay"` and
  an `evolution_memory` increment. Some of the 112 increments may therefore be
  simulated-candle trades. The document now says so, and provenance section
  B3 prints the recorded market data mode per surviving row. An increment
  whose row is gone cannot be classified.
* **Migrated?** Yes. `LegacyJournalMigration` gives each old journal row a
  `LEGACY_JOURNAL:` record. Where the ledger still has the trade, it merges
  into that trade's canonical record by key.
* **Excluded from forward-paper stats?** Yes. The records API defaults to
  `FORWARD_PAPER`. The audit dataset shows 6 legacy records outside the
  forward KPIs; the legacy KPI view is separate (−$4.00, 6 trades).
* **Memory shows provenance?** Yes. Counters are labelled VERIFIED, LEGACY or
  UNVERIFIED, with "N journal rows · N ledger-verified · N with no record".
  The label has a flaw, **D15**: a counter backed only by a SIMULATION record
  is still labelled VERIFIED.
* **Server rows for the real 59/53: NOT TESTED.** That needs the output of
  `scripts/journal_provenance.py` from the production container, which this
  sandbox cannot reach.

## 3. Canonical schema

`trade_records.db` holds these tables:

* `trade_records`: one row per execution lifecycle. Deterministic
  `journal_record_id = tr_ + sha256(execution_key)[:24]`; UNIQUE
  `execution_key` and `trade_id`.
* `trade_record_events`: 10 timeline stages.
* `trade_record_corrections`: ENRICH, DISCREPANCY and CORRECTION entries.
* `trade_reviews`, `trade_notes`, `decision_records`, `weekly_reviews`,
  `improvement_proposals`, `review_runs`, `recorder_state`.

Triggers: `trg_trade_records_immutable` (fact columns of a finalized record)
and `trg_trade_records_no_delete`.

Representative row: `tr_ccd361d11eb9b8b68a40a04f` (see §5). Notes live only
in `trade_notes`. Posting a note left `facts_hash` unchanged (verified
through the API).

Raw-SQL probe on a finalized record:

| Operation | Result |
|---|---|
| `UPDATE net_pnl` | blocked |
| `UPDATE exit_reason` | blocked |
| `DELETE` | blocked |
| `UPDATE record_origin` | **allowed** |
| `UPDATE finalized=0`, then edit or delete anything | **allowed** |

See **D3**.

## 4. Source and origin isolation

Real backend, audit dataset:

* **Sources:** INSTANCE 8, SMC_LAB 2, PA_LAB 1, LEGACY_ENGINE 6, AGENT 0,
  MANUAL 0. Agent facts merge into the SMC record, so AGENT has no records of
  its own.
* **Origins:** FORWARD_PAPER 10, SIMULATION 1, LEGACY_MIGRATION 6,
  BACKTEST 0, RESEARCH 0.

Each filter returns only its own origin, and every count equals SQL.
Backtests and research never write a ledger the recorder reads, so they
cannot enter.

* Weekly scopes are per agent and strategy (`instance_agent:<id>`,
  `smc_agent`, `pa_agent`). Every finding cites only ids inside its own scope.
* Memory is FORWARD_PAPER only.

Two exceptions: **D5** (a replay instance's decision record is labelled
FORWARD_PAPER) and **D15**.

## 5. End-to-end trace (real strategy → memory)

**Long trade (take-profit), `inst-audit-3cr`, session `sess-audit-1`:**

| Link | Id |
|---|---|
| signal | `inst-audit-3cr:1.0.0:BTCUSDT:5m:2026-09-27T18:19:00+00:00` (the strategy's decision identity) |
| decision | `decisions.id = 2` → `decision_record dr_d095e72c83283bb399e1ff14` |
| execution key | `INSTANCE:inst-audit-3cr:sess-audit-1:auto:inst-audit-3cr:BTCUSDT:5m:2026-09-27T18:19:00+00:00:buy` |
| intent = order | `auto:inst-audit-3cr:BTCUSDT:5m:2026-09-27T18:19:00+00:00:buy` (the forward engine has no separate order object, see **D16**) |
| fill evidence | `…:buy:fill:<quote time>` webhook row |
| trade | `6dcff66c303f4b86b6fab7ed0f83e7b7` |
| position | `abb1560fe83a46a3a36ce2e8eb4dadd3` |
| journal record | `tr_ccd361d11eb9b8b68a40a04f` (CLOSED, WIN, finalized) |
| decision → record | `dr_d095e72c83283bb399e1ff14.journal_record_id = tr_ccd361d11eb9b8b68a40a04f` |
| review | journal_reviewer v1 review on the same id |
| weekly | `instance_agent:inst-audit-3cr` review cites `tr_ccd361d11eb9b8b68a40a04f` |
| memory | `3-Candle Rejection · EMA 9/33 / Ranging / long` includes it |

The short trade (stop-loss) is `tr_a01dac4d259746de7fb1cfa4`: trade
`7254c4ea…`, position `c50d4bdb…`, decision 3.

One lifecycle produced exactly one record in every case run.

## 6. Journal vs execution truth

Every field compared equal, 33/33 for each trade:

* ids, instance and session;
* side, quantity, entry fill, exit fill, SL, TP, planned entry;
* fees, net and gross P&L, risk amount, R;
* opened, closed, fill, order and signal timestamps;
* exit reason, decision id, equity before, bid and ask, MFE and MAE,
  strategy id.

The lab records matched their broker fills on 15/15 fields each.

Timestamp findings: **D14** (decision latency counts from the candle's open)
and **D16** (order ack time equals intent time).

## 7. Strategy evidence at decision time

* **3-Candle Rejection (instance):** PARTIAL. The record freezes the
  strategy's reason text and the Decision Brain's full evaluation.
  `strategy_snapshot` is null because the strategy's `Signal` carries none.
  Level, touches and EMA values are not frozen as structured data (**D17**).
  Nothing is invented.
* **SMC:** PARTIAL. The raw `ordered_condition_results`, MTF evidence,
  native object ids and trade plan are frozen. The derived
  `conditions_passed` is always empty and `conditions_required` all null,
  because the projector reads a shape the real strategy never emits (**D8**).
* **PA:** NOT TESTED against the real PA strategy. The lab journal evidence
  (identity, market context, state transitions) is carried; the setup fields
  were null for the synthetic proposal.

## 8. Material non-trade decisions (real engine)

| Case | Recorded as | Trade / position / P&L | Verdict |
|---|---|---|---|
| Signals-only mode | SIGNALS_ONLY | none | PASS |
| Semi-auto | APPROVAL_REQUIRED | none | PASS |
| Operator pause | RISK_BLOCKED | none | PASS |
| Decision Brain block ("…HTF context unavailable…") | **FEED_UNAVAILABLE** | none | FAIL (**D1**) |
| Stale candles (age 10500 s, allowed 315 s) | **nothing** | none | FAIL (**D2**) |
| 60 no-setup candles | 0 decision records (60 cycle reports stay in the candle archive) | none | PASS (no flood) |
| SMC signals_only / manual_approval / unreliable feed | SIGNAL_GENERATED / WAITING_CONFIRMATION / STALE_DATA | none | PARTIAL (**D1**) |
| HTF_BLOCKED, WAITING_CONFIRMATION (instance) | not produced by the instance path; HTF text is only classified by keyword | — | NOT TESTED |

The classifier was also run over the whole real blocker vocabulary. Six of
23 real codes are mislabelled (**D1**).

## 9. Crash and duplicate (real strategy path)

18/18 checks passed:

* **Duplicate decision across a restart:** one entry row plus one
  `duplicate` row, 1 trade, 1 record.
* **Duplicate fill callback:** the second delivery fills nothing; 1 record.
* **Restart before submission:** a claimed-but-never-submitted key leaves no
  record.
* **Restart after submission:** the same record id goes PENDING → OPEN →
  CLOSED.
* **Restart after fill:** the record is rebuilt from the ledger.
* **Journal-write failure:** the error is reported, nothing is half-written,
  and the next pass writes the one record.
* **Recovery:** three fresh recorder passes changed no hash and no
  `updated_at`, and logged no discrepancy.
* **Real server restart:** the same 17 records afterwards.

Side effect found in the duplicate case: **D4**.

## 10. Closure paths

| Path | Evidence | Verdict |
|---|---|---|
| Take-profit | the same record as the open; outcome WIN; R 1.9804 = net P&L / risk from the ledger | PASS |
| Stop-loss | the same record as the open; outcome LOSS; R −1.0 | PASS |
| Lab stop | protective order; R −1.0189 | PASS |
| Lab target | R 2.4835 | PASS |
| Manual close | covered by integrity tests (pipeline close without a reason becomes `manual-close`); not re-run on the real strategy | PARTIAL |

## 11. UI reads canonical data

The built dashboard on the real backend:

* the Trades KPIs, rows and origin chips (forward, legacy, simulation) equal
  the API;
* the detail page shows all 10 sections and the timeline;
* the Decisions chips and rows equal the API;
* Memory shows verified and legacy tables;
* Weekly shows its scopes;
* a Notes entry was posted by `admin`.

The only journal endpoints called were `/journal/records*`,
`/journal/decision-records*`, `/journal/memory`, `/journal/notes`,
`/journal/weekly*` and `/journal/recorder`. No `/journal/*` request failed
and no mocks were involved.

Findings: **D10**, **D12** and **D13** are visible in the UI.

## 12–16. Weekly review, evidence ids, isolation, safe learning, scheduler

* **Deterministic.** Each of 6 scopes built twice gives identical output
  apart from `generated_at`. Trades, net P&L, total R and the id set equal an
  independent SQL computation.
* **Evidence ids.** Every fact and observation cites ≥1 record id, all inside
  its own review and scope.
* **Safe learning.** Findings are split into facts, observations, hypotheses
  and recommendations. Proposals need 20 trades over the scope's history (0
  created on this dataset). They are PENDING_APPROVAL. A decision needs a
  non-empty actor and changes no record. Nothing reads approved proposals to
  change a strategy (grep: only the router).
* **Scheduler.**
  * A normal run writes 6 reviews.
  * A duplicate run writes 0.
  * A restarted scheduler writes 0.
  * 4 concurrent runs store each review once.
  * After three missed weeks it catches up the week with trades and writes
    no empty weeks.
  * A fresh RUNNING claim is respected; a stale one (>600 s) is retried.
* **FAIL: a trade finalized after its week was reviewed is never reflected,
  and the review is not marked stale (D11).**

## 17. Test suites

| Suite | Passed | Failed | Skipped | Warnings |
|---|---|---|---|---|
| automation-hub (`python -m pytest -q`) | 3486 | 0 | 15 | 9: anyio / FastAPI `on_event` deprecations, urllib3 SOCKS, 4 ResourceWarnings (unclosed files and sockets in `alerts.py` and two tests). None is from journal code. |
| engine (`python -m pytest -q tests`) | 509 | 0 | 0 | 3 ResourceWarnings (unclosed sockets) |
| dashboard e2e (Playwright, mocked API) | 159 | 0 | 0 | none reported (8.2 min) |

### 17a. Dashboard e2e

The e2e suite runs on mocked endpoints by design. It shows that the pages
render and behave. It is not evidence that they show real data. That
evidence is §11, where the same pages ran on the real backend and the real
audit dataset.

## 18. Verdict matrix

| # | Area | Verdict |
|---|---|---|
| 1 | Safety | PASS |
| 2 | 59/53 provenance | PARTIAL (code proven, doc corrected, D15; server rows NOT TESTED) |
| 3 | Canonical schema | PARTIAL (D3) |
| 4 | Source / origin isolation | PARTIAL (D5, D15) |
| 5 | End-to-end traceability | PASS (D16, D18 noted) |
| 6 | Journal = execution truth | PASS |
| 7 | Strategy evidence | PARTIAL (D8, D17; PA NOT TESTED) |
| 8 | Material non-trade decisions | FAIL (D1, D2) |
| 9 | Crash / duplicate | PASS (D4 noted) |
| 10 | Closure paths | PASS (manual PARTIAL) |
| 11 | UI on canonical data | PASS (D10, D12, D13 noted) |
| 12 | Weekly stats deterministic | PASS |
| 13 | Findings carry record ids | PASS |
| 14 | Agent isolation | PASS |
| 15 | Safe learning | PASS (proposal path only by unit test) |
| 16 | Scheduler | PARTIAL (D11) |
| 17 | Test suites | PASS (3995 Python + 159 e2e passed; 0 failed) |

## 19. Not tested

* Production rows for 59/53.
* The PA strategy producing its own proposal.
* The SMC lab runtime loop (`tick`) and the real SMC agent deciding.
* The Adaptive lab ledger source.
* A Supabase ledger (the projector skips it).
* Recorder cost on a large ledger. A pass took 0.02–0.04 s here; it
  re-reads every lifecycle per pass.
* Tenant scoping of the journal endpoints (not scoped).

## 20. Defect register

Severity: HIGH = a stated requirement is not met; MEDIUM = the record or
view misleads; LOW = cosmetic or edge case.

**D1: decision classifier mislabels real blocker codes.** MEDIUM
* EXPECTED: a Decision Brain block is QUALITY_BLOCKED or HTF_BLOCKED;
  LOSS_COOLDOWN and TRADE_LIMIT are RISK_BLOCKED; SMC `SIGNAL_ONLY` is
  SIGNALS_ONLY and `PENDING_APPROVAL` is APPROVAL_REQUIRED.
* ACTUAL: `stage=brain` with "…context unavailable" becomes FEED_UNAVAILABLE
  (in the UI too); cooldown and trade limit become SETUP_REJECTED; SMC
  becomes SIGNAL_GENERATED / WAITING_CONFIRMATION; SMC `ORDER_CREATED`
  becomes SIGNAL_GENERATED while the trade is open.
* ROOT CAUSE: `classify_decision` keyword-matches stage, blocker and reason
  together, in a fixed order, before it looks at the structured stage.
* FILES: `services/journal_recorder.py` (`classify_decision`),
  `services/journal_labs.py` (SMC status map).
* FIX: map `gate_stage` and blocker codes first, with an explicit table;
  keywords on the reason only as a fallback; add the SMC lab statuses.

**D2: stale data and feed loss never become decision records.** HIGH
* EXPECTED: one STALE_DATA or FEED_UNAVAILABLE record per outage.
* ACTUAL: the real engine raises `MarketDataStaleError` before any cycle
  report or decision row, so nothing is recorded. `project_feed_incidents`
  reads a top-level `blocker` key that `build_cycle_report` never writes.
  `test_05` passes only on a hand-built cycle-report shape.
* ROOT CAUSE: incidents are projected from the wrong store.
* FILES: `services/journal_recorder.py` (`project_feed_incidents`),
  `tests/test_journal_integrity.py::test_05`.
* FIX: project incidents from the instance lifecycle transitions
  (`data_stale` → `running`) recorded by `trading_instances` /
  `instance_telemetry`, one per outage; rebuild the test on the real engine.

**D3: the finalized-record guard can be bypassed; origin is mutable.** MEDIUM
* EXPECTED: a finalized record cannot be un-finalized, deleted, or re-origined
  outside `correct()`.
* ACTUAL: raw SQL can set `finalized=0` and then edit or delete; it can change
  `record_origin` directly. `upsert_trade` silently updates non-fact columns
  (origin, source, verification) on finalized records.
* FILES: `data/trade_record_store.py`.
* FIX: the trigger aborts `finalized` 1→0 and changes to
  origin/source/key/status; the upsert logs a CORRECTION for any of those.

**D4: a decision record keeps only the latest upstream state.** LOW–MEDIUM
* EXPECTED: the decision that opened a trade stays "accepted"; a later
  duplicate attempt is a separate DUPLICATE_PREVENTED event.
* ACTUAL: after a restart re-evaluated the candle, the record reads
  TRADE_OPENED with status GATE_REJECTED and blocker DUPLICATE_SIGNAL.
* ROOT CAUSE: `DecisionStore.record` returns the existing id,
  `_finalize_decision` overwrites it (upstream, `auto_engine.py`), and the
  projector upserts.
* FILES: `services/journal_recorder.py` (`DecisionProjector`),
  `services/auto_engine.py`.
* FIX: keep the first terminal state and append later ones as events.

**D5: instance decision records are always FORWARD_PAPER.** LOW
* EXPECTED: a replay instance's decision is SIMULATION.
* ACTUAL: FORWARD_PAPER, while its trade is SIMULATION.
* FILES: `services/journal_recorder.py:711`.
* FIX: take the origin from the instance's market data mode, as trades do.

**D6: Price Action lab decisions are not recorded.** MEDIUM
* EXPECTED: PA candidates that were rejected, cancelled or waiting appear
  under Decisions.
* ACTUAL: `PALabProjector` has no `project_decisions`, so 0 PA decisions.
* FILES: `services/journal_labs.py`.
* FIX: project `pa_candidates` / `pa_evaluations` material states.

**D8: SMC condition summaries are always empty.** MEDIUM
* EXPECTED: `conditions_passed` lists the passed conditions.
* ACTUAL: `[]`, and `conditions_required` is 8 nulls. The real evaluation
  uses `{key, label, status: "PASS"}`; the projector reads `name` /
  `condition` / `passed`. The raw list is kept in `evidence.ordered_conditions`.
  `test_21` uses the non-real shape.
* FILES: `services/journal_labs.py` (~370, ~489), `tests/test_journal_integrity.py::test_21`.
* FIX: read `label or key` and `status == "PASS"`; build the fixture from
  `evaluate(seeded_engine())`.

**D9: the agent's TAKEN decision is not linked to its record.** LOW
* EXPECTED: the AGENT decision links to the SMC record.
* ACTUAL: unlinked. The real agent writes `trade_id` only on the execution
  intent and in `agent_trades.decision_id`. Also, if the agent row appears
  after the lab record was finalized, `agent_id` and `decision_id` are never
  filled (finalized lifecycles are skipped).
* FILES: `services/journal_labs.py`.
* FIX: link through `agent_trades.decision_id → proposal_id`; allow ENRICH
  of NULL agent fields on finalized records.

**D10: weekly scope labels truncate the instance id to 8 characters.** LOW
* Two instances can look identical in the list.
* FILES: `services/journal_reviews.py:170`.
* FIX: show the instance name or the full id.

**D11: a late record in a reviewed week is ignored.** MEDIUM
* EXPECTED: the weekly review reflects every record closed in its week, or
  says it is out of date.
* ACTUAL: once DONE, a week is never revisited; the added record is missing
  and nothing flags it.
* FILES: `services/journal_reviews.py` (`run_weekly`).
* FIX: for DONE weeks inside the catch-up window, compare stored ids with
  the current ones; on a difference, write a new revision and mark the old
  one superseded.

**D12: "Risk %" shows the configured risk next to the actual risk amount.** LOW
* 1.00% is shown beside $14.94, which is 0.15% of $10,000 (the exposure cap
  shrank the size).
* FILES: `TradeDetail.tsx`, `journal_recorder.py`.
* FIX: label it configured, and add effective risk = risk amount / equity.

**D13: a quality-gate bypass is not visible.** MEDIUM
* EXPECTED: a trade the owner let through with the quality gate off says so.
* ACTUAL: the Risk section says "every pre-trade gate passed". The frozen
  evidence has `quality_gate.allowed: false`. The decision's bypass reason
  was overwritten upstream by "Market intent awaits…".
* FILES: `services/journal_recorder.py` (risk_check), `services/auto_engine.py`
  (`_finalize_decision`).
* FIX: derive "PASSED — quality gate bypassed by owner" from the frozen
  evidence.

**D14: decision latency starts at the candle's open.** LOW
* It shows 5 m on 5 m candles.
* FIX: measure from the candle close (timestamp + timeframe), or label it.

**D15: a legacy counter is VERIFIED even when its backing record is SIMULATION.** MEDIUM
* EXPECTED: VERIFIED means backed by forward-paper records.
* ACTUAL: a counter backed only by a replay trade shows VERIFIED.
* FILES: `services/journal_legacy.py` (`evolution_provenance`).
* FIX: carry the backing records' origins, and use VERIFIED only for
  FORWARD_PAPER.

**D16: order submit and ack times copy the intent time.** LOW
* The forward engine has no order-ack event; `intent_id == order_id`.
* FIX: leave the ack time NULL when no ack exists; document intent = order
  on this path.

**D17: an instance strategy's structured evidence is not frozen.** MEDIUM (§7)
* 3-Candle Rejection level, touches and EMA values survive only as prose.
* ROOT CAUSE: the engine freezes `Signal.snapshot`, which this strategy does
  not set; `strategy.decision_report()` holds the data.
* FILES: `services/auto_engine.py` (payload). No strategy logic change is
  needed.
* FIX: freeze `decision_report()` into the payload at signal time.

**D18: open instance records have no `position_id`.** LOW–MEDIUM
* EXPECTED: the position id from the OPEN execution row.
* ACTUAL: NULL until the close.
* FILES: `services/journal_recorder.py` (ledger projector).
* FIX: read `position_id` from `paper_executions` OPEN.

(D7 was merged into D1.)

## 21. Fix log

Each fix lands in its own commit, with a test built on real engine or
strategy output, and is re-checked with `scripts/journal_audit/run_all.sh`.

**D2 — FIXED.**
* Change: outages are now projected from the instance lifecycle events the
  manager writes to `instance_engine_logs` (MARKET_STALE, then recovery
  attempts, until MARKET_CONNECTED, or until the worker stops or errors).
  The result is one decision record per outage, updated in place as OPEN →
  RESOLVED / INSTANCE_STOPPED / WORKER_STOPPED_ON_ERROR, with no trade and no
  P&L. The dead cycle-report projector and its hand-built test are removed.
* Tests: `test_05` runs a real `TradingInstanceManager` whose feed serves
  3-hour-old candles, then recovers. `test_05b` stops the worker mid-outage.
* Harness: `inst-dec-stale` now yields STALE_DATA / RESOLVED, 2 recovery
  attempts.

**D1 — FIXED.**
* Change: `classify_decision` now reads, in order:
  1. the terminal state;
  2. the blocker code, through an explicit table covering every code the
     pipeline (`gate_blocker`), the engine, the SMC and PA lab candidates and
     the SMC agent's gates write;
  3. the gate stage.

  Reason words only refine a context block into HTF, or market quality into
  stale data. A code nobody has mapped yet falls back to the old word
  matching. Both labs share one candidate classifier, which also separates
  "placement rejected" (ORDER_REJECTED) and fail-closed DATA_PAUSED
  (EXECUTION_FAILED) from real data pauses.
* Test: `tests/test_journal_decision_types.py` (27 cases), driven by the real
  3-Candle Rejection signal through the engine in every operating mode, the
  pipeline's own `gate_blocker()` codes and the real SMC strategy through the
  lab. 8 of them fail on the old classifier.

**Correction to D6.** `PALabProjector.project_decisions` does exist and reads
`pa_candidates`. The PA lab writes a candidate only for signals-only,
manual-approval, rejected and paused proposals. An automatic placement goes
straight to the broker, and per-candle `pa_evaluations` are not projected.
That is why the audit run, which was automatic mode, showed 0 PA decisions.
D6 is narrower than first written: PA automatic placements have no decision
record of their own (their trade record exists), and PA "waiting" evaluations
are not recorded.

**D8 — FIXED.**
* Change: the SMC projector reads the strategy's real condition shape,
  `{key, label, status}`:
  * **passed** is PASS;
  * **failed** is MISSING / INVALIDATED / EXPIRED;
  * NOT_REQUIRED is left out of **required**.

  The trade record and the decision record share one reader. The raw list
  stays in `evidence.ordered_conditions`.
* Tests:
  * `test_21` now feeds the strategy's own condition list; it was a shape the
    strategy never emits.
  * `test_21b` runs the real SMC strategy, lab placement, fill and stop-out,
    and checks the record's lists against the evaluation.

  Both fail on the old projector.
* Harness: the SMC record lists 7 passed conditions; the not-required pivot
  is excluded.

**D11 — FIXED.**
* Change: each run compares every reviewed week in the catch-up window with
  the records that week has now. A difference writes the next revision,
  with the reason and the added record ids. The earlier revision is marked
  superseded and stays readable by id; lists show the revision in force.
  Proposals of the superseded revision still awaiting a person become
  SUPERSEDED and can no longer be approved; a decision a person already
  made is kept.
* Migration: existing databases are rebuilt once, with every existing
  review as revision 1 under its old id. The Weekly tab shows the revision
  and why it was written.
* Tests:
  * a late trade through the real pipeline and forward engine produces
    revision 2 with both trades, and a third run changes nothing;
  * proposal retirement, with a person's decision kept;
  * migrating a pre-revision database.

  All three fail on the old code.
* Harness: weekly checks 80/80.

**D15 — FIXED.**
* Change: a legacy counter is VERIFIED only when every increment is a
  ledger-verified record and every one of those records is forward paper.
  The other labels:
  * all simulation → SIMULATION;
  * forward and simulation together → MIXED;
  * trades that exist but whose decision never recorded its market data →
    LEGACY.

  Each counter now carries `backing_origins` and the market data mode each
  journal row recorded (`recorded_market_data`). The Memory tab shows the
  origins next to "ledger-verified".
* Tests: `tests/test_journal_legacy_provenance.py` runs the real 3-Candle
  Rejection strategy through the engine and pipeline, with the decision
  journal attached as the platform attaches it. It covers replay → SIMULATION,
  live → VERIFIED and both → MIXED; all three fail on the old code.
  `test_24`'s expectation, which asserted the defect (VERIFIED for a trade of
  unknown data source), is corrected to LEGACY.
* Harness: the replay-built counter reads SIMULATION ({SIMULATION: 1},
  {replay: 1}).

**D13 — FIXED.**
* Change: the risk-check receipt reads the Decision Brain verdict frozen
  with the trade. A trade whose verdict says it was not allowed could only
  have gone through the owner's per-instance quality-gate switch. Its record
  now says PASSED_WITH_QUALITY_GATE_OFF, lists what the Brain would have
  blocked it for, and no longer claims every pre-trade gate passed. The
  trade page shows this in words in the Risk section.
* Limit: a score below the minimum, with no hard block, is not detected. The
  minimum is not frozen with the trade, so this is not claimed either way.
* Tests: the real 3-Candle Rejection signal, which the Brain blocks, traded
  with the switch off through the engine and a forward fill. This test fails
  on the old code. An ordinary trade stays PASSED.
* Harness: the audit trade reads PASSED_WITH_QUALITY_GATE_OFF, with the
  Brain's two blocks.

**Found while fixing, outside the journal.** In replay mode the strategy
engine numbers execution ids with a counter that restarts at 1 for every
engine (`auto-{symbol}-{action}-{n}`). A second replay run or instance on
the same ledger therefore reuses an id, and its first trade fails closed.
The cause is execution code (`services/auto_engine.py`), so it was not
changed here; it is filed as its own task.


**D3 — FIXED.**
* Change: once a record is finalized, its origin, source, execution key and
  record id are guarded with its facts. A new trigger refuses to un-finalize
  a record; that was the step that let raw SQL edit or delete one. A later
  recorder pass that computes a different origin or source now logs a
  DISCREPANCY and leaves the record as it is. `correct()` can change origin
  or source to a valid value, with a reason, an actor and a logged
  CORRECTION.
* Tests: raw SQL can no longer reopen, re-label or re-key a finished record;
  a later pass cannot re-label it; a correction can. Both tests fail on the
  old store.
* Harness: unchanged results, 0 discrepancies.

**D18 — FIXED.**
* Change: an instance record takes its `position_id` from the OPEN execution
  row, so an open trade names its position from the fill on, and keeps the
  same id at the close.
* Test: open, then closed, through the real pipeline and forward engine, with
  no discrepancy. It fails on the old projector.
* Harness: all three open records carry their position id.

**D5 — FIXED.**
* Change: an instance decision's origin comes from what can show it.
  * A decision that became a trade takes the trade's origin, proven from its
    fill.
  * Otherwise the instance's mode decides: "trading" is FORWARD_PAPER, any
    other mode is SIMULATION. The instance metadata the recorder reads now
    carries the mode.
  * A decision whose instance is no longer known keeps the origin it was
    first recorded with.
  * One never recorded, with nothing to show its data, is LEGACY_MIGRATION,
    not forward paper.
* Tests: the real 3-Candle Rejection signal, blocked by the Decision Brain,
  on a replay and a trading instance; a decision linked to a forward-filled
  trade. The replay and no-evidence cases fail on the old code.
* Harness: its recorder has no instance registry, so its unlinked instance
  decisions are LEGACY_MIGRATION, and those linked to trades take the
  trade's origin.

**D4 — FIXED (in the journal; the decision store is unchanged).**
* Change: a decision linked to a trade is, by that link, one whose order
  filled. Its record says FILLED with no blocker, so it no longer shows the
  pending order's "ORDER_PENDING". When a restart re-evaluated the candle and
  the pipeline refused the replay, that refusal is kept as
  `evidence.duplicate_attempt` instead of replacing the decision. The
  upstream store still overwrites its row, because PENDING_INTENT →
  GATE_REJECTED is also a legitimate transition there (an expired limit
  order). `blocker` is now taken from the source as-is, so a cleared blocker
  clears.
* Test: a real 3-Candle Rejection trade, then a restarted worker
  re-evaluating the same candles. It fails on the old projector.
* Harness: all 8 traded instance decisions read FILLED with no blocker; the
  restarted one keeps its duplicate attempt.

**D9 — FIXED.**
* Change: the agent's TAKEN decision is linked through
  `agent_trades.decision_id`, which is where the real agent records the trade
  it took. The decision row, written first, never carries it. A finished SMC
  record whose agent row arrived after it was finalized is linked on the next
  pass. The agent and decision ids are previously unknown facts, so they fill
  in and are logged as an enrichment; a known value is never replaced.
* Test: the frozen SMC strategy's setup, placed, filled and stopped out by the
  lab, and then the agent's writes in the real agent's order. It fails on the
  old projector.
* Harness: the SMC record names `smc_agent` and its decision, and the agent's
  TAKEN decision opens the record.

**D6 — FIXED.**
* Change: the PA projector now records every material Price Action decision
  once:
  * candidates;
  * orders the lab placed by itself in automatic mode, which write no
    candidate row;
  * one WAITING_CONFIRMATION record per setup the strategy formed. It is read
    from the trace each closed-candle evaluation saves, with the conditions
    passed and the next required event. It is dropped once the setup becomes
    a proposal, whose decision covers it. WATCHING candles are never
    recorded.

  A second bug found here is also fixed. A candidate row names its proposal
  `{session}:{proposal}` while the trade record uses the proposal id, so an
  approved candidate never linked to the trade it became.
* Tests: `tests/test_journal_pa_decisions.py`. The waiting-setup case runs
  the real, frozen PA engine over deterministic candles through the lab's own
  evaluation: 150 evaluations give exactly one record per pending setup, with
  no trade. There are also an automatic placement and an approved candidate.
  All three fail on the old projector.
* Harness: the automatic PA trade now has its TRADE_OPENED decision, linked.

**Found while fixing D6, in a frozen file: the PA lab never attests its
strategy's proposals.**
* Evidence: `PriceActionPaperAccount.record_evaluation`
  (`services/price_action_lab.py`) only attests proposals whose
  `str(signal_at)` equals `candle.timestamp.isoformat()`. The engine's
  `visual_state()` gives `signal_at` as a `datetime`, whose `str()` has a
  space where `isoformat()` has a "T". Both the live tick and the replay loop
  pass that state straight in. Reproduced with the real engine: at the candle
  where it created a PA1_SR_REJECTION proposal, the lab saved the evaluation
  as WATCHING with no proposal ids, so no order could follow.
* Consequence: the PA lab cannot place orders from its own strategy in live
  or replay. Its orders so far could only come from states whose proposals
  carry no `signal_at`, such as the lab's own tests.
* Not changed: `price_action_lab.py` is frozen. A one-line fix would compare
  `isoformat()` values. It needs the owner's decision to lift the freeze.

**D17 — FIXED (level and touches; EMA values stay unrecorded).**
* Change: when a strategy signals, the engine now also freezes the
  strategy's `decision_report()` into the fill payload as `strategy_report`,
  and the record keeps it as `evidence.strategy_report`. For 3-Candle
  Rejection that is the decision, direction, level price and level touches as
  data, not only prose. `services/auto_engine.py` was touched only to add
  this evidence field. The report is read after the signal exists and never
  feeds back into the order. A strategy without a report, or one whose report
  fails, trades exactly as before and records `null`.
* Not fixed: the strategy's report does not carry the EMA 9/33 values, only
  their relation inside the reason text ("EMA9 > EMA33"). Recording them
  would mean changing the strategy's report, which is strategy code, so they
  are still absent. Nothing is filled in for them.
* Tests: `tests/test_journal_strategy_evidence.py`. The real strategy trades
  through the engine, the pipeline and a forward fill. The record's level
  equals the strategy's own rejection event level (100.0, 2 touches). A report
  that raises on the signal candle still gives an open trade, with no
  report. Both fail on the old code.
* Harness: all 8 instance trades (7 forward paper, 1 simulation) carry the
  report. Lab and legacy records have none, since their strategies do not go
  through this engine.

**D16 — FIXED.**
* Change: no order on these paths is ever acknowledged. The forward engine
  parks the intent as the order, and the lab's paper broker writes its order
  row and fills it later. So `order_acknowledged_at` is now left empty on
  instance and lab records; it used to repeat the submit time. The instance
  path keeps `intent_id == order_id` and one time for intent and order,
  because they are one event there.
* Tests: `tests/test_journal_record_timing.py`. A real forward-paper
  3-Candle Rejection trade, where the order time equals the fill event's
  `order_timestamp` and there is no ack, and a real SMC lab trade whose
  submit time is the broker's order-row time, again with no ack. Both fail on
  the old code.
* Harness: `truth.py` and `lab_truth.py` now also check the ack time, so the
  instance truth is 34/34. On the old code they report 33/34 and a lab
  mismatch.

**D14 — FIXED.**
* Change: the decision latency now starts where the decision could first be
  made, and each record states its basis in
  `source_ref.decision_latency_basis`. The Trade page shows it next to the
  value.
  * Instance and engine trades are measured from the close of the signal
    candle: the signal's candle open time plus the timeframe.
  * Replay trades are not measured. Their signal time is a replayed candle
    and their decision time is the wall clock, and the gap between two clocks
    is not a latency. It used to read about 209 days.
  * SMC and PA lab trades are not measured. Both labs stamp their decision
    with the signal candle's close time by construction, so the gap was always
    exactly one candle (SMC) or zero (PA).
  * Other ledger trades (webhook alerts with no candle identity) keep the
    signal time as the start.
* Tests: `tests/test_journal_record_timing.py`, with three new tests:
  * a real forward trade whose latency equals the decision row's time minus
    the candle close (old code: 323 s instead of 23 s);
  * a real replay trade with no latency and no clock-inconsistency flag;
  * a real SMC lab trade whose signal-to-decision gap is exactly one candle
    and whose latency is not measured.

  All three fail on the old code.
* Harness: `truth.py` recomputes the latency from the ledger's decision row
  and the fill's signal time (35/35). `lab_truth.py` expects none. The
  harness's instance latencies of about 58 s come from its own synthetic
  candles, which close on the minute before the run. They are not an engine
  delay.

**D12 — FIXED, together with a unit error found alongside it.**
* Change: the risk receipt on an instance record now has a `risk` block. It
  holds the target the sizer aimed for (`target_pct`), what the trade took
  once sized (`taken_pct`, the risk amount over the equity before the trade)
  and whether the size was cut after sizing. The Trade page labels the stored
  value "Risk target" and adds "Risk taken", computed from the record's own
  risk amount and equity.
* Also fixed: the SMC lab configures risk in percent (0.5 means 0.5%), and its
  records stored that number as-is. Instance records store a fraction (0.01
  means 1%), so an SMC trade showed as risking 50%. Lab records now store the
  fraction.
* Tests: `tests/test_journal_risk_and_scope.py`:
  * a real forward trade with a 1% target of $10,000, cut by the 5% exposure
    cap to about 0.15%, checked against the ledger's entry, stop and size;
  * a real SMC lab trade that stores 0.005 and sized for 0.5% of its balance.

  Both fail on the old code.
* Harness: `truth.py` checks the target and taken risk against the frozen
  sizing receipt and the ledger (37/37). `lab_truth.py` checks the SMC
  record's fraction against the lab session's configured percent.
