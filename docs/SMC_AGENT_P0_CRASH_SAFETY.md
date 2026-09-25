# SMC Agent execution/journal crash-safety repair

## Scope and revision

- Original repair branch: `codex/smc-agent-crash-safety`.
- Original repair base: `95fe0c924ce3a9d080f60ca4a39363c3fa7f1fc8`.
- This report records local validation of the repair. Commit/publication
  status is tracked by Git; these results do not establish VPS deployment.
- The older `466eaa4` implementation was inspected and selectively adapted;
  it was not cherry-picked. Current context, memory, and trade-management
  policy modules remain unchanged.
- The separate mode-persistence worktree is unchanged. No production
  restart, deployment, configuration change, account reset, or history
  deletion was performed.

### First integration: running revision cd14b8e

The VPS preflight reported `cd14b8e8eb8e36af0109bd3f691b770b6b70fd0b`,
which contains 13 commits absent from the original repair branch. Deploying
the original repair alone would remove those code changes; the ancestry
guard correctly refused.

Integration branch: `codex/smc-crash-safety-integration`, created from that
running revision. It merges repair commit
`5e494b88f32a69c098eb2e04f97f63ce4d796a70` without conflicts.
All 26 files changed by the 13 running-only commits were compared against
`cd14b8e` and were unchanged in that integration, including Adaptive Lab, lab mode saving,
SMC labels, and report/probe scripts. The repair's production files also
match `5e494b8` exactly. No additional production-code changes were needed.

Integration validation:

- Focused safety/agent/broker/Adaptive Lab set, including the unchanged
  original journal-boundary reproduction: **307 passed**.
- Source and behavior protection checks: **27 passed**.
- Dashboard TypeScript check: passed; dependencies unchanged.
- Full integrated Python suite: **3,671 passed, 15 skipped, 87 warnings**
  in **285.35 seconds**, exit status 0.
- Protected SMC source and freeze fixtures/tests: unchanged.
- No publication, deployment, production restart, mode change, or history
  deletion was performed during integration.

### Updated integration: running revision 3ae62d8

The next VPS preflight reported
`3ae62d8ece400d6afa19fa40718447964da69504`, a direct child of `cd14b8e`
adding the view-only Adaptive MTF Trading Instance mirror. The guard again
stopped before switching the VPS checkout or building/restarting the app.

With operator approval, this commit was merged into the existing
`codex/smc-crash-safety-integration` branch without conflicts or history
rewriting. All 27 files changed between the common base `95fe0c9` and
`3ae62d8` were compared and are preserved exactly. The P0 repair production
files still match `5e494b8`. No manual production-code changes were needed.

Updated validation:

- Focused safety/agent/broker/Adaptive Lab set, including the unchanged
  journal-boundary reproduction: **313 passed**.
- Source and behavior protection checks: **27 passed**.
- Dashboard TypeScript check: passed; dependencies unchanged.
- Full integrated Python suite: **3,677 passed, 15 skipped, 87 warnings**
  in **285.66 seconds**, exit status 0.
- Protected SMC source, strategy behavior and freeze fixtures unchanged.
- Paper/live routing settings unchanged; tests run with
  `HUB_ENABLE_EXTERNAL_LIVE=0`.
- No production restart, deployment, mode change, or history deletion.
  Publication status is tracked by Git, not proof of deployment.

### Latest integration: VPS baseline 44b0258

The VPS subsequently reported `44b02580357edf29fda6e709c57843704a9017d6`.
Its six additional commits include Supabase log-write resilience, dashboard
styling and landing-page content/build corrections. They were merged into
the integration branch without conflicts. All 56 files changed from the
common base `95fe0c9` to `44b0258` are preserved exactly, and the P0
repair's production files still match `5e494b8`.

- Focused safety/agent/broker/Adaptive Lab/Supabase resilience set:
  **318 passed**, including the unchanged original journal-boundary test.
- SMC source/behavior protection checks: **27 passed**.
- Dashboard typecheck and production build: passed.
- Landing typecheck, client/SSR builds and pre-render of 20 routes: passed.
- Full integrated Python suite: **3,682 passed, 15 skipped, 84 warnings**
  in **284.10 seconds**.
- Frontend builds used existing dependencies with matching lockfiles;
  temporary dependency links were removed. No dependency changes.

A read-only SSH check during validation observed another independently
recreated app container, subsequently confirmed by `/version` as
`fd5285d9e5d3d3d63eb785a6379e6f48a7b3c958` on
`claude/focused-gates-q5nse7`. That revision is NOT included by this
44b0258 integration. The existing external data volume remained mounted;
the root filesystem had 31 GB free at inspection. No VPS restart, deployment,
mode change or data mutation was performed by this task.

**Do not deploy this candidate over a later non-ancestor revision.**
Coordinate the concurrent deployment first; local tests are not production
validation and do not waive the ancestry or backup checks.

The concurrently deployed revision adds six further commits affecting 112
files, including security/key custody, encrypted backups, a public API,
SDKs and signed webhooks. These changes were fetched/read for diagnosis only;
they were not merged or modified in this validation.

## Root cause and execution trace

The old agent called the lab's approval path from inside a journal
transaction. The lab's broker used a different SQLite connection and
committed independently. A later `journal.open_trade()` failure rolled back
the journal, not the broker. The exception handler then recorded MISSED and
the runtime could say “No order was placed.”

Before:

```text
ENTRY_READY -> candidate -> agent/risk checks -> broker commits order
  -> journal.open_trade fails -> journal rollback -> MISSED (false)
```

After:

```text
unchanged SMC evaluation and candidate staging
  -> unchanged agent/context/risk checks
  -> commit DECISION_APPROVED intent and frozen decision identity
  -> commit EXECUTION_PENDING and exact prepared lab order request
  -> approve through the existing lab path using the execution key
  -> broker commits order
  -> persist EXECUTED evidence
  -> atomically record decision + trade relationship + COMPLETE
```

Fills remain the paper broker's responsibility. They can happen before or
after journal finalization. Fill, position, account, protection and quote
cursor writes are rolled back together if an event fails before commit.
Submission failure also rolls back uncommitted rows before reconciliation.
Neither fix changes pricing, fill eligibility, stop geometry or sizing.

## States and truthful status

| Intent state | Meaning |
| --- | --- |
| DECISION_APPROVED | Approved intent and immutable plan are durable; submission has not been claimed. |
| EXECUTION_PENDING | Submission boundary may have been crossed; broker lookup is required after interruption. |
| EXECUTED | A broker order is confirmed. This does **not** mean it has filled. |
| EXECUTION_UNCERTAIN | Broker response or journal finalization is unresolved. Known order IDs are retained. |
| EXECUTION_FAILED | Submission did not create an order, or serialized broker reconciliation proved absence. |
| COMPLETE | Decision, broker order and journal trade relationship are finalized atomically. This does **not** mean the position is closed, or even filled. |
| RECONCILED | Legacy state accepted for recovery of older journals; the new final state is COMPLETE. |

Broker states (open, partially_filled, filled, cancelled, rejected), fills
and explicitly linked current positions remain separate evidence. The
journal preserves the approved entry/size/stop/targets/RR; actual fill price,
filled quantity and position identity are broker facts, not replacements
for the approved plan. Reconciliation stores that evidence alongside the
journal trade.

Post-submission uncertainty is never MISSED. MISSED remains a pre-submission
outcome for a valid proposal that the agent could not act on, after checking
for an existing intent first. If uncertainty itself cannot be persisted, the
previous durable pending intent remains recoverable and the returned status
explicitly reports the persistence error.

The runtime's outer exception fallback also distinguishes a successful lookup
from an unavailable broker: unavailable lookup returns `executed=null`,
`order_presence=UNKNOWN` and `position_count=null`, never invented zeroes.
The existing `candidate_status` agent field describes the candidate observed
at decision time; execution outcome and broker evidence describe what happened
after that observation.

The runtime reconciles before processing another agent entry. An unresolved
intent or journal outage produces PERSISTENCE_BLOCKED, disarms new entries,
and exposes the failure through bot-status. Broker quote/protection processing
is not disabled by that entry gate. No operating mode is changed.

## Idempotency and restart

The key is SHA-256 over a JSON tuple of:

```text
SMC_AGENT, session, symbol, timeframe, original signal candle,
setup, proposal, strategy identity, strategy version
```

Using the proposal's original signal candle prevents a later dashboard poll
from turning the same staged proposal into another decision. The current
closed candle is used when the evaluation has no proposal timestamp.
Tests distinguish symbols, timeframes, setups, proposals, versions, sessions,
and candles, and verify that replay yields the same key. These are identity
tests, not a claim that mathematical SHA-256 collisions are impossible.

The same identity is used for:

1. Unique execution-intent key and preallocated decision ID.
2. Lab idempotency key and broker candle/provenance key.
3. Broker's existing unique order-key constraint.
4. Position's new nullable `entry_order_id`, written atomically on creation.
5. Journal decision/trade linkage and atomic COMPLETE transition.

An OS advisory lock on the persistent journal directory serializes agent
submission/reconciliation across local processes, without holding a journal
transaction over broker work. Process death releases that lock. Short
journal transactions use WAL, a 10-second busy timeout, FULL synchronization
and a connection lock. Immutable event and identity triggers preserve
evidence. Unchanged recovery failures do not append heartbeat revisions.

Recovery looks up the broker by execution key, restores missing lab ownership
metadata from the prepared request, and finalizes the journal exactly once.
It never resubmits an uncertain order and never modifies broker state to
make the journal agree. An intent with no order after verified reconciliation
becomes FAILED; stale decisions are not automatically resubmitted.

Older safety intents retain their original execution identity. Existing
positions/history are not backfilled with guessed ownership: a legacy
same-symbol position lacking an origin order ID causes a blocker when
ownership cannot be verified. Recovery requires review instead of assigning
someone else's position by symbol.

This coordinator assumes the existing local persistent SQLite deployment,
shared by all agent processes. It is not a distributed multi-host lock or a
certification against storage-device/power-loss failures.

## Failure-injection matrix

O/P/I/J = broker orders / open positions / intents / journal trades.
Counts are measured in isolated temporary test databases. A journal trade is
an order/plan relationship and can exist before a fill.

| Boundary | O/P/I/J at failure | Durable / returned state | After reconciliation |
| --- | --- | --- | --- |
| A: before intent persistence | 0/0/0/0 | FAILED on handled error; no return on process termination | No broker work; no invented history |
| B: after approved intent, before submission | 0/0/1/0 | APPROVED; no decision/trade row yet | 0/0/1/0, FAILED after broker proves absence |
| B2: after PENDING, before submission | 0/0/1/0 | PENDING | 0/0/1/0, FAILED; same decision cannot resubmit |
| C: broker commit fails before commit | 0/0/1/0 | FAILED; failure decision recorded | Still zero orders/positions |
| D: order commit then lost response/process death | 1/0/1/0 | UNCERTAIN or PENDING after process death | 1/0/1/1, COMPLETE/TAKEN |
| E: order exists, open_trade raises | 1/0/1/0 | UNCERTAIN, known order retained; no MISSED | 1/0/1/1, COMPLETE/TAKEN |
| F: filled position, then journal failure | 1/1/1/0 | UNCERTAIN/PENDING/EXECUTED according to crash boundary | 1/1/1/1, same order and position IDs |
| G: partial fill, then finalization failure | 1/1/1/0 | UNCERTAIN; 0.1 filled of 0.5 ordered | 1/1/1/1; 0.1 position and 0.4 remaining unchanged |
| H: immediately after successful journal commit | 1/1/1/1 | COMPLETE; response may be uncertain if lost | 1/1/1/1; no new decision/trade |
| I: three restarts while broker lookup fails | 1/1/1/0 | UNCERTAIN; new entry refused | 1/1/1/1 when lookup recovers |
| J: duplicate decision after restart | 1/1/1/1 | ALREADY_DECIDED, existing COMPLETE identity | Unchanged, no executor call |
| In-flight position/fill/protection write fails | 1/0/1/1 | Order remains open; event transaction rolls back | Retrying the event gives one position/fill, not two |
| Whole journal becomes read-only after fill | 1/1/1/0 | Durable PENDING, returned UNCERTAIN with persistence error | 1/1/1/1 after reopening |

The matrix also tests a commit which succeeds before its response raises,
and verifies durable truth through a separate database connection.
The real process-death test uses a child process calling `os._exit(73)`
immediately after broker fill/position creation: no cleanup/finally handlers
run. Restart finds the existing order and position and finalizes one trade.
Repeated reconciliation and two concurrent workers do not duplicate entries.

Decision records remain absent when their finalization transaction rolls back
(B, D, E, F, G and I before recovery); their durable intent is the recovery
record. Successful reconciliation records TAKEN exactly once. Proven
pre-commit failures record EXECUTION_FAILED, not MISSED. H and J retain the
original TAKEN decision. A failure before any intent cannot create a durable
execution record when the journal itself is unavailable.

A further runtime test loses both the agent response and broker lookup after
successful execution: counts remain 1/0/1/1, while the response truthfully says
EXECUTION_UNCERTAIN/UNKNOWN instead of claiming that no order exists.

The original `test_deployed_journal_boundary.py` was run unchanged. Its
broker-order/no-MISSED safety assertion passes. Existing tests that expected
MISSED after successful execution were corrected to assert broker counts,
position state, UNCERTAIN status and recoverable intent. Restart fixtures now
reopen the same persistent session rather than silently create another account.

## Production files changed

| File | Change |
| --- | --- |
| automation-hub/services/smc_agent.py | Stable keys, durable intent-first submission, truthful failures, idempotent finalization/reconciliation; existing gates retained. |
| automation-hub/services/smc_agent_journal.py | Additive intent/event schema, immutable evidence, atomic writes, synchronization and local execution coordination. |
| automation-hub/services/smc_agent_runtime.py | Bind the key to existing lab approval, persist prepared request, recover before entries, expose blockers and broker evidence. |
| automation-hub/services/smc_agent_recovery.py | Restore missing lab order metadata/candidate linkage from durable request; never edit broker orders or positions. |
| automation-hub/execution/paper_broker_v2.py | Roll back failed submission/fill events; persist position origin order ID and preserve it in snapshots. |
| automation-hub-dashboard/src/pages/SMCAgent.tsx | Only type/label compatibility for failure/uncertainty/reconciled outcomes in the existing panel. No new dashboard feature. |

Tests added: `test_smc_agent_crash_boundaries.py`,
`test_smc_agent_p0_recovery.py`.
Tests updated: `test_smc_agent.py`, `test_smc_agent_live_wiring.py`.

## Validation and strategy protection

- Final focused safety/agent/journal/broker/mode/API set, including the
  unchanged deployed-boundary artifact: **284 passed**.
- Separate source/behavior protection set: **27 passed**.
- Full final regression suite: **3,648 passed, 15 skipped, 87 warnings**
  in **268.61 seconds**, exit status 0.
- Dashboard TypeScript compatibility check (`tsc --noEmit`): passed;
  no dashboard build or dependency changes.
- Diff check: clean.
- Protected strategy source, freeze fixtures and freeze tests: unchanged.
- Source strategy, lab strategy path, context, memory, trade-management
  policy modules and mode-persistence router: unchanged.
- Behavior fingerprint:
  `7230a75e0af39ac608dfa862ad1c85903db4f244bd864c5e519c28abf8457d5e`.

Verified unchanged protected source SHA-256 values:

| File (under automation-hub/services) | SHA-256 |
| --- | --- |
| native_smc.py | 47c2b7e28fee15e3366ea5434092f13c99cb50cbf718e56a35becaf715c8c36d |
| smc_strategy_ladder.py | 51d3a293062644a7f1eae8585ae0ff8cf8fe0f0b143fe6388f5fe9155b1b9e8b |
| smc_strategy_v1.py | dca7f79a3ba18b126e9193db696d8997436bf86c446c25fdfed280d5f36e7afc |
| native_smc_live_visual.py | 1e0056cf067800d3e13a66349de231f24d75efbd7dc8a21dc2eebc1164550635 |

Reproduction from the repository root using the project's test environment:

```sh
HUB_ENABLE_EXTERNAL_LIVE=0 PYTHONPATH=.:automation-hub python -m pytest -q tests automation-hub/tests
HUB_ENABLE_EXTERNAL_LIVE=0 PYTHONPATH=.:automation-hub python -m pytest -q automation-hub/tests/test_smc_agent*.py automation-hub/tests/test_smc_decision_path_freeze.py automation-hub/tests/test_paper_broker_v2.py
```

## Safety and review boundary

Local API tests assert `paper_only=true` and
`real_execution_allowed=false`. Validation runs explicitly set
`HUB_ENABLE_EXTERNAL_LIVE=0`; the inspected local settings report
`external_live_enabled=false`. Production configuration was not read or
changed during this repair, so these are local—not new VPS—claims.

`signals_only` stays observation-only; `manual_approval` with the attached
agent remains Agent Mode; `automatic` remains strategy-direct paper.
This repair does not loosen SMC conditions or make a WATCHING setup trade.
No dashboard build-out, weekly scheduler, strategy optimization, or deployment
is included. Publishing this repair does not authorize deploying it.
