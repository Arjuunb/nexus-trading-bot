# Sprint 8.5 — Active evidence completeness

## Audit and implementation plan

The working tree already contains the previous sprints as uncommitted work.
These changes are preserved. No staging, commit, push, deployment or external
environment access is authorized by this sprint.

Source inspection identifies these loss boundaries:

| Source | Exact boundary | Lost evidence | Existing trustworthy facts |
| --- | --- | --- | --- |
| PA Lab | `PriceActionLabRuntime._on_closed_bar` → `PriceActionPaperAccount.record_evaluation` | Actual `engine.config` is reduced to a display hash; evaluator input history/native HTF context is not persisted. Recorder saves proposal IDs without direction and only `PaperExecutionConfig`. | Native version 1.1.0, per-candle correlation/idempotency IDs, OHLCV, original trace, explicit empty proposal list, feed status. |
| Native SMC Lab | `native_smc_live_visual.live_visual_state` → `SMCPaperAccount.record_evaluation` | Engine configuration and exact closed input are available during evaluation but disappear from `source_strategy`; saved configuration contains only five settings. Material HTF IDs omit OHLCV. | `SMC_SOURCE_V1.0.0-paper-draft`, original model report, direction, missing conditions, per-candle IDs, feed state. |
| Trading Instances | `AutoStrategyEngine._process_bar` → `_on_signal` / `_capture_strategy_identity` → `DecisionStore` | Identity observation happens on emitted signals. No durable complete opportunity journal; source candle and native HTF material are absent from normalized decision rows. | Owner/instance/session, emitted direction, observed strategy identity when capture succeeded, acceptance/rejection. |

Legacy labs have no tenant columns. Only the explicitly configured legacy owner
can access them; multi-user lab comparison remains blocked. Current settings
are not historical configuration evidence. Mutable lifecycle states are not
the original decision outcome. Generic SMC shadow evaluation remains blocked;
observing persisted native SMC decisions is a separate capability.

Implementation order:

1. Establish backend/browser baselines and retain existing tests.
2. Capture detached actual evaluator configuration and causal market input at
   the existing lab boundary; use the existing canonical SHA-256 utility.
3. Include the capture in the existing decision insert. Protect capture fields
   against lifecycle edits. Project into immutable non-financial receipts with
   fail-soft persistence and bounded restart reconciliation.
4. Extend read-only adapters, window-specific completeness/health and existing
   comparison APIs. Preserve historical UNKNOWN and instance SIGNALS_ONLY.
5. Add Active Evidence Health to the existing Shadow Lab; validate backend,
   migration/recovery, financial isolation, build and Chromium workflows.

Risks: source/receipt projection is not an atomic cross-store transaction;
closed-candle availability uses the existing conservative assumption; reviewed
finite lookbacks do not prove infinite-history convergence; legacy global lab
reset can remove pending source rows; no PostgreSQL worker certification or
production-shaped staging certification follows from local tests.

## Executive summary and scope

IMPLEMENTED: contemporaneous native PA/SMC configuration and closed market-input
capture, immutable decision receipts, bounded source-owned reconciliation,
read-only adapter verification, window-specific active evidence health,
authenticated health/timeline APIs, and the Shadow Lab health interface.

TESTED: genuine native PA evaluation plus genuine existing ATP shadow evaluation
produce a committed COMPARABLE artifact in a disposable fixture. Different
fingerprints are preserved. Financial state remains unchanged. Native SMC
decision observation preserves original rule outcomes and engine checkpoints.
No signal, fill, profit or historical performance is invented by these tests.

PARTIALLY IMPLEMENTED: active comparison support. Newly captured, complete
native lab decisions are eligible individually. Trading Instances remain
SIGNALS_ONLY, old lab records remain incomplete, multi-user legacy lab ownership
is unsupported, and session/regime/volatility coverage depends on actual source
fields. Capture does not certify every strategy/configuration/timeframe.

BLOCKED: PostgreSQL worker certification, externally supervised services, hard
resource isolation, authorized staging/production-shaped migrations and recovery,
complete instance opportunities and continuous real-feed operational validation.
Generic SMC shadow evaluation remains BLOCKED independently of active SMC reads.

NOT IMPLEMENTED: trading execution hooks, automatic promotion, historical
backfill from current settings, invented neutral decisions, profitability from
agreement, a second ledger, provider, queue or fingerprint implementation.

No Git staging, commit, push, reset, deployment or staging/production access
occurred. Existing dirty files were preserved; this report describes this
sprint's contribution, not the entire accumulated diff from HEAD `7dd5e9a`.

## Current baseline actually executed

Before source changes, repository-root tests with isolated local PostgreSQL
fixtures enabled passed: **3,600 passed, 0 failed, 15 skipped, 88 warnings**
(394.23s). This includes Hub tests; a separate pre-change Hub run was not claimed.
Affected source/shadow tests passed: **137 passed, 0 failed, 0 skipped, 0 warnings**
(7.18s). Approved Chromium baseline: **142 passed, 0 failed, 0 skipped** (6.0m).
Historical reported counts were not substituted for these executions.

## Active evidence completeness matrix

Legend: V = PRESENT_VERIFIED, U = PRESENT_UNVERIFIED, M = MISSING,
? = UNKNOWN, N/A = NOT_APPLICABLE. New V states apply only after receipt
verification; capture/projection/context errors downgrade the record.

| Field | Legacy PA | New audited PA | Legacy SMC | New audited SMC | Trading Instance |
| --- | --- | --- | --- | --- | --- |
| Tenant identity | U: configured legacy owner | V: contemporaneous explicit owner | U: legacy owner | V: explicit owner | V: persisted owner |
| Strategy ID/version | U: saved source fields | V: source + frozen identity | U: saved source fields | V: source + frozen identity | U/V: signal identity only |
| Effective fingerprint | M | V | M | V | U: when original signal capture succeeded |
| Frozen configuration reference | M | V: immutable receipt | M | V: immutable receipt | M in comparison row |
| Venue/market type | M | V: actual Binance USD-M scope | M | V: actual USD-M scope | M |
| Symbol/timeframe | U | V: original decision clock | U | V: original decision clock | U: signal scope |
| Candle close timestamp | U: original open + interval | V | U: original open + interval | V | M/?: signal time is not candle identity |
| Canonical candle identity | U: saved OHLCV | V: complete validated OHLCV | M | V | M |
| HTF context identity | U: material IDs only | V: causal native OHLCV + policy input | U: material IDs | V: causal native OHLCV + actual policy input | M |
| Source identity | M | V: actual runtime source | M | V: actual runtime source | M |
| Finality/freshness | U: saved feed state | V under documented close/publication assumption | U | V under same assumption | ? |
| Session | ? | ? | M in persisted decision | V: existing native snapshot session | M/? |
| Market regime/volatility | ? | ? | ? | ? | M/? |
| Original outcome | U: neutral explicit empty proposals, signal direction unavailable | V: original proposals/trace | U: original native report | V: original native report | U: emitted signals only |
| Signal direction | M for signals; N/A neutral | V signals; N/A neutral | U from original proposal | V signals; N/A neutral | U from emitted signal |
| Rejection/missing reasons | U: original trace | V: recorded original reasons | U: original source conditions | V: recorded original reasons | U: acceptance/rejection reason |
| Evaluation timestamp | U: source creation clock | V: immutable source decision-record clock | U: report/creation clocks | V: immutable source clock; original report also retains evaluated_at | U: persisted decided_at when present |
| Evidence commitment | U: mutable source row | V: committed immutable receipt | U: mutable row | V: committed receipt | U: persisted signal decision |

Known provenance is not converted into an unavailable exchange publication
timestamp. PA has no native session field at this boundary; SMC's actual native
session is preserved without substituting a different classifier. Regime and
volatility are UNKNOWN because contemporaneous authoritative classifications
are absent. These fields are optional for candle/decision agreement; they do
not make mandatory configuration/candle/HTF gates optional.

Other supported active sources: no additional complete opportunity journal was
found. Orders, fills and financial journals are not adopted as decision sources.

## Configuration propagation and immutability

The runtime observes the instantiated `NativePriceActionEngine.config` or
`SMCMarketStructureEngine.config` after the existing evaluation. It never
rebuilds a historical identity from UI settings. `asdict` captures every actual
native dataclass field, including inherited defaults. The source also freezes
the actual lab evaluation/operating policy (`PaperExecutionConfig` or effective
`SMCPaperConfig`), strategy ID/version, resolved entry/primary/secondary clocks,
and existing SMC rule manifest. SMC inherited hard-limit defaults are the same
defaults used by its existing source path; missing fields are not manufactured.

`effective-native-lab-configuration-v1` uses the existing
`strategy_identity.canonical_configuration_json` and SHA-256 fingerprint
utility. Snapshot reference is `active-config:<fingerprint>` within the
immutable receipt. Both native and lab-policy schemas must have all exact
fields before an authoritative fingerprint is produced. Missing input yields
UNKNOWN, not a hash of an incomplete guessed configuration. Identical material
remains deterministic; changed actual native parameters produce a new identity.

The actual SMC report's selected model must match the frozen lab model. An
older/different source report cannot inherit a later settings fingerprint;
capture returns an explicit unavailable-model diagnostic without an authoritative
fingerprint. Original source decisions and execution dispatch are untouched.

The scope is strategy evaluation plus lab decision policy. Mutable account
balances, margin/leverage ledger state, fill-cost observations and funding are
not claimed as verified strategy input or profitability. Existing native
display hashes and Sprint 1 identities remain unchanged, not reinterpreted.

The recorder preserves the outer policy actually used by the existing
synchronization call. If a concurrent settings read differs, original strategy
and execution behavior continue unchanged but evidence becomes PARTIAL with
`CONFIGURATION_CHANGED_AT_DECISION_BOUNDARY`. A transient engine token detects
concurrent SMC feed/context changes and capture-time state changes without
adding a lock or rerunning strategy logic. Such records cannot qualify as
complete comparisons. Object-address tokens are not persisted as identities.

## Market context and point-in-time safeguards

Canonical candle material reuses the existing shadow and comparison utilities:
UTC candle-open timestamp and complete finite OHLCV, native interval alignment,
valid high/low/price/volume, and exact close = open + timeframe. Provider scope
must actually be `binance_usdm` / perpetual / `BINANCE_USDM_PUBLIC`. Other venues
are unverified; there is no spot/futures substitution or new network request.

The capture retains causal entry/HTF candle material, its configuration-utility
digest, separate HTF-only identity, and the policy's original material candle
IDs/bias/close times. Native PA's saved MTF input and SMC's separately supplied
`source_strategy.mtf_evidence` must match material derived with the existing
policy from the actual native candles. A mismatch or missing actual input is
explicitly excluded. This prevents a full engine snapshot from falsely
attesting a different context consumed by the source decision.

Only native candles closed by the evaluation candle's close are included.
Required clock sequences must be continuous, ordered, unique, aligned and
current. Optional 4h absence remains optional; malformed supplied optional
context is preserved and invalidates evidence. Future HTF changes cannot
change earlier captured input. Exchange publication times are unavailable:
`CLOSED_AT_OR_BEFORE_DECISION; INDIVIDUAL_PUBLICATION_UNKNOWN` is the explicit
assumption. Host decision-record age over 30 seconds is incomplete, not fresh.

Reviewed finite native history uses an entry floor of 400, expanded by actual
PA swing/zone/event/setup/confusion/entry-expiry dependencies and SMC
swing/internal pivot, sweep and ATR dependencies; primary
HTF requires 51 completed candles. PA higher-timeframe zones expand primary
history as well. At most 1,500 candles per clock are retained; larger unmet
requirements fail closed. This is a conservative evidence requirement, not a
change to strategy warm-up rules or proof of infinite EMA convergence/full
engine-state reconstruction. Unknown/insufficient context blocks comparability.

## Non-intrusive decision capture and persistence

Capture is detached metadata at the existing `record_evaluation` boundary.
Original proposal selection, missing conditions, PA/SMC rules, order eligibility,
ledger writes, fill timing model and execution mode are unchanged. Signals are
not generated again. The record keeps explicit original NO_SIGNAL versus
LONG/SHORT, context/stale blocks, parked UNKNOWN and contemporaneous inactive
source states. Mutable order lifecycle status is not substituted for outcome.

New metadata is serialized in the existing source decision INSERT. That INSERT
is the durable evidence outbox boundary; no extra receipt database write occurs
before order execution. Preparation failure produces a persisted unavailable
capture diagnostic and logs only source/category, without vetoing an otherwise
existing decision. Failure of the original source INSERT retains its existing
behavior; the new code does not claim an unpersisted decision later occurred.

Source startup and existing PA/SMC supervisors project frozen source material to
non-financial `active_decision_receipts`. Projection is fail-soft, bounded to
200 records and 4 MiB of source payload per pass, and emits batch-limit/backlog
diagnostics. It reads no current configuration and creates no financial event.
PK identity is the existing durable correlation ID. Repeat delivery/restart
has exactly-once logical receipt effects; physical projection is at least once.
No cross-store atomicity or exactly-once physical delivery is claimed.

Source capture and its identity fields are protected from UPDATE while existing
order lifecycle edits remain allowed. Immutable receipt/supersession triggers
reject UPDATE/DELETE. Explicit trusted supersession annotations retain both
records and exclude the old record from new calculations; there is no automatic
correction or public evidence mutation route. Source/receipt conflicts are
reported rather than resolved by choosing the latest row.

## Recovery, gaps and historical limitations

Startup/supervisor reconciliation materializes only contemporaneous captures
already committed in source rows. Tests cover source-only process termination,
projection interruption, independent connection races, bounded prolonged
outages, duplicates and restart. Recovery metadata records backlog/status and
last successful complete reconciliation. Missing opportunities remain missing.
The existing shadow worker's gap/lease/fencing safeguards remain untouched.

Historical rows without exact capture stay PARTIAL/UNVERIFIABLE with unknown
fingerprint/context. Immutable trigger protection also prevents attaching a
new capture to an old row. Current UI settings, later orders and current owner
configuration cannot make those rows VERIFIED. Malformed historical payloads
become EVALUATION_ERROR and do not prevent valid receipt recovery.

Committed receipts survive existing operational lab/session resets with their
known original tenant. No financial reset behavior is changed. **Pending source
rows may still be lost by an explicitly authorized legacy global reset before
projection**; archive/reconcile first. No original reset is invoked by the new
components. Archive/backup verification and delivery-failure counters remain
operational prerequisites, not fabricated historical facts.

## Adapter compatibility and comparison activation

Adapters retain original source identity/reference and validate schema, full
configuration digest, ownership, source clock, canonical candle, causal native
context digest and completed input bounds. Database access stays SQLite
`mode=ro` + `query_only`, within bounded read snapshots. A receipt awaiting
projection is not complete. Corrupt, superseded, conflicting and stale evidence
is excluded. Original row timeframe, not latest session/UI timeframe, determines
earlier candle boundaries.

Locally tested complete cases: native 5m PA and native SMC source decisions with
verified actual input, authorized single-owner LIVE_PAPER scope and sufficient
history. Historical/other scopes are partial or unknown. Generic SMC shadow is
still unsupported; observing persisted active SMC is a different capability.
Trading Instances remain signals-only because manufacturing neutral opportunity
records would require an unverified change to their execution boundary.

The existing `shadow_comparison.v2` engine is reused unchanged. Same tenant,
venue/market/symbol/timeframe, finalized candle opportunity and verified
canonical OHLCV are mandatory. Each candidate keeps its own configuration
fingerprint; fingerprints need not match. Complete required decision evidence,
authorization, fresh/causal HTF and lookback are required. Missing active records
stay `NO_DECISION_RECORDED`. Conflicting OHLCV yields NOT_COMPARABLE.

COMPARABLE requires every scheduled pair eligible. PARTIALLY_COMPARABLE exposes
only eligible pairs plus exclusions. NOT_COMPARABLE/UNKNOWN disclose blockers.
Completed artifacts remain immutable and reproducible from retained inputs;
later evidence produces a new artifact. Old artifacts are historical as-of
calculations, not silently updated to VERIFIED by new source capture.

Let N be eligible pairs, L/S same-direction long/short, Z both neutral. Overall
agreement = (L+S+Z)/N; signal-relevant agreement = (L+S)/(N-Z). Zero denominators
are null/UNAVAILABLE. Active-only, shadow-only and opposite signals are separate.
Excluded count = scheduled opportunities minus eligible pairs. Missing active
rate uses the requested native candle clock denominator. No missing record is
neutral, and agreement is not profitability, trade count or cost evidence.

## SMC safety findings

`native_smc.py`, `smc_strategy_v1.py`, ladder rules, BOS/CHoCH, liquidity,
order-block/FVG, HTF confirmation, rejection and risk logic are unmodified.
Capture reads actual report/config/context/session and does not invoke the
evaluator again. Tests compare original reports/checkpoints and instrument
broker submission to fail if evidence calls it. Generic SMC read-only shadow
equivalence remains NOT IMPLEMENTED/BLOCKED, not certified by these tests.

## Source-integrity freeze contract

The existing whole-file PR6 hash test detected the authorized lab wrapper edits
because `data/pr6_real_paper_freeze.json` still pinned their pre-sprint content.
The repository already records subsequent approved deltas in that manifest.
This sprint appends an evidence-only delta, retains both original lab hashes
and every earlier delta, updates only the reviewed wrappers, and additionally
pins the new capture service, native SMC visual adapter and additive SQL.
The existing test and hash assertions are unchanged.

An additional regression verifies exact original source hashes for all 113
other lab functions/methods and accounts for the nine explicitly reviewed
instrumented methods. Native PA, native SMC and MTF policy remain byte-identical
to their original pinned hashes. This is an audited source-contract revision,
not removal of the integrity test or approval to modify trading rules.

## APIs, health and dashboard

New authenticated routes under the existing Strategy Intelligence router:

- `GET /shadow/active-sources/{id}/health?start&end`
- `GET /shadow/active-sources/{id}/decisions?start&end&limit&offset`

Both enforce owner access, aware UTC, non-future half-open windows of at most
seven days, and no-store responses. Timeline pages are at most 200 records;
underlying windows are at most 2,016 observations and fail explicitly above the
bound. No source files/paths, execution actions or arbitrary code are accepted.
Legacy sources remain explicitly bound to the configured owner only; a different
tenant receives no sources/404, not another owner's data or recovery backlog.

`active-evidence-health.v1` exposes field-by-field classifications/counts,
mandatory blocking reasons, original configuration references, canonical/HTF
completeness, requested-clock expected/observed/verified opportunities, missing
decisions, last verified observation and actual reconciliation metadata.
VERIFIED requires complete eligible records for the entire requested clock,
one active identity and no unresolved reason. Otherwise PARTIAL/BLOCKED/UNKNOWN
is shown. Configuration presence alone cannot make window health VERIFIED.
Multiple source records for one candle produce
`DUPLICATE_ACTIVE_OPPORTUNITY`, block VERIFIED readiness and remain excluded
by the existing comparison gate; they are not extra evaluation opportunities.

Duplicate logical-record counts are not redelivery counts. Persisted delivery
retry/failure counters are unavailable and return null, not a guessed zero.
Unverifiable-record counts and known pending receipt backlog are separate.
Exact feed uptime, exchange latency and source outages without durable records
remain unknown. Evidence commitment time is a host timestamp recorded during
projection, not an fsync acknowledgement or exchange publication timestamp.

The existing `ShadowComparisons` view now polls actual health every ten seconds,
shows missing fields/readiness/coverage/backlog, and can inspect the bounded
active decision timeline, frozen identity and evidence. It rejects stale health
responses from a previously selected source/window. Existing comparison controls
still call the real authenticated immutable comparison API. No unsupported green
indicator, fake health value or synthetic performance is supplied by the UI.

Browser tests use real production-built React and Chromium with API contract
fixtures. A fixture route collision was corrected: active rows had incorrectly
been returned to the shadow timeline. Assertions were not weakened. Backend
tests separately prove the real capture/health/comparison pipeline.

One later browser attempt reported 110 passed / 34 failed because a reused
preview process disappeared mid-run (`ERR_CONNECTION_REFUSED`). The final run
uses an ignored `/tmp` override importing the unchanged repository Playwright
configuration, an owned strict-port loopback preview with server reuse disabled,
and the already typechecked production build. All tests/timeouts/assertions and
the approved Chromium installation remain unchanged. The environment failure
is diagnosed, not silently skipped or counted as a passing run.

## Additive migration and compatibility

`migrations/active/0001_active_decision_receipts.sql` is a trusted source-owned
SQLite template for PA/SMC initialization after their existing base tables.
It adds receipt, recovery and append-only supersession tables, owner/session/
candle and supersession indexes, receipt/supersession immutability triggers, and
the source capture UPDATE guard. It alters no financial table/record or prior
calculation. No cascade FK can erase receipts when an existing lab reset removes
operational source rows.

Fresh installation, prior populated evaluation schema, rerun and interrupted
DDL forward recovery are tested locally, including integrity/foreign-key/index
checks and preserved prior rows. Existing methods accept optional evidence
keywords; legacy callers still work and retain partial classification.
Startup recovers durable captures, never current-settings backfills.

Before authorized rollout take a consistent database/WAL backup and verify
recovery. Prefer forward recovery: rerun additive installation/reconciliation.
Code rollback preserves new tables/triggers; older code can continue ordinary
lifecycle writes without deleting evidence. Do not drop committed evidence or
remove integrity guards to force a rollback. PostgreSQL and large production
schema/backup restore certification are not claimed by SQLite tests.

## Files modified in this sprint

| Path under repository root | Contribution |
| --- | --- |
| `automation-hub/services/active_evidence_capture.py` | New native capture, full fingerprint, immutable projection/recovery and supersession primitives |
| `automation-hub/data/pr6_real_paper_freeze.json` | Append audited evidence-only delta, retain prior hashes and pin unchanged methods/current components |
| `automation-hub/migrations/active/0001_active_decision_receipts.sql` | New additive SQLite evidence schema/guards |
| `automation-hub/services/price_action_lab.py` | Existing source-boundary metadata and supervisor reconciliation |
| `automation-hub/services/smc_strategy_lab.py` | Existing native source-boundary metadata and reconciliation |
| `automation-hub/services/native_smc_live_visual.py` | Actual input observation and fail-closed concurrency token |
| `automation-hub/services/active_decision_evidence.py` | Extend Sprint 8 read-only receipts, original clocks, conflict checks and health |
| `automation-hub/routers/strategy_intelligence.py` | Health and active timeline routes |
| `automation-hub/webhook_api.py` | Explicit single-owner evidence binding at existing lab construction |
| `automation-hub/tests/test_active_evidence_capture.py` | Capture/integrity/migration/recovery/comparison/financial tests |
| `automation-hub/tests/test_active_evidence_api.py` | Real authenticated health/timeline/tenant/bound tests |
| `automation-hub-dashboard/src/pages/ShadowComparisons.tsx` | Active Evidence Health and active timeline |
| `automation-hub-dashboard/e2e/active-evidence-health.spec.ts` | Verified/blocked health, identity inspection and auth browser contracts |
| `automation-hub/docs/STRATEGY_INTELLIGENCE_SPRINT85.md` | Audit, design, compatibility and actual validation |

## Test evidence and benchmark

All final executions below completed with exit status 0 after the last source
change, including the duplicate-opportunity readiness regression. Counts are
per suite, overlap, and must not be added together.

| Final execution | Passed | Failed | Skipped | Warnings | Duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Focused Sprint 8.5 capture/API | 48 | 0 | 0 | 0 | 11.91s |
| Automation Hub full suite | 3,118 | 0 | 15 | 90 | 388.84s |
| Repository-root full suite | 3,648 | 0 | 15 | 89 | 405.16s |
| Database/recovery selection | 288 | 0 | 0 | 4 | 56.82s |
| Chromium full browser suite | 144 | 0 | 0 | 4 environment warnings | 5.9m |
| Dashboard typecheck + production build | Passed | 0 | 0 | 1 bundle-size advisory | Build 7.40s |
| `git diff --check` | Passed | 0 | 0 | 0 | Completed |

Focused commands use `UV_CACHE_DIR=/tmp/uv-cache uv run --frozen pytest -q -ra
tests/test_active_evidence_capture.py tests/test_active_evidence_api.py` in
`automation-hub`. Both full Python suites use `pytest -q -ra` from their
respective directories with `NEXUS_PG_OUTBOX_TEST=1`; this enables only the
existing disposable local PostgreSQL/PostgREST financial outbox fixtures.
No production credentials or external environment were used.

The database/recovery selection includes both new test files and existing
production-validation, paper outbox/receipt, execution receipt lookup,
simulation restart, shadow PostgreSQL migration-routing, shadow comparison,
router/API/worker/operations, research jobs/retention and PR6 source-integrity
suites. This covers local migrations and recovery, not PostgreSQL research or
shadow worker ownership semantics. Those unsupported integration workflows
remain BLOCKED and are not counted as executed tests.

Dashboard command: `npm run build:check`. Browser command:
`PW_CHROMIUM=/usr/bin/chromium npm run test:e2e --
--config=/tmp/nexus-sprint85-browser.config.ts`. The temporary configuration
imports the repository configuration and changes only the owned preview
server/output directory and loopback port, as described above. It does not
change test discovery, assertions, browser selection, timeouts or worker count.

Final local logs are `/tmp/sprint85-delivery-{focused,hub,root,database,browser,build}.log`.
These workspace logs are ephemeral; the counts and methodology above are the
persistent report. The 15 skips are existing risk-engine parity cases: eight
are not validity scenarios and seven compare only when both sides approve.
No new tests or browser tests were skipped.

During implementation, the unmodified PR6 hash test failed on newly instrumented
wrapper files. The reviewed source-manifest delta and additional 113-method
invariance test resolved that contract failure. Interrupted/incomplete runs
and the diagnosed browser preview failure are excluded from final counts.

Affected source/adapter stage: **129 passed, 0 failed, 0 skipped, 0 warnings**
(10.65s; before the last additional historical/inactive-source tests).
Targeted browser flows: **4 passed, 0 failed, 0 skipped** (13.3s).

New regression coverage includes actual configuration changes retaining prior
identity, missing full configuration rejecting fingerprinting, original candle
clock despite UI timeframe change, bound HTF policy input, future HTF invariance,
concurrent SMC context exclusion, true neutral/null agreement denominators,
non-default lookback expansion and bounded entry-history copying,
historical UNKNOWN, canonical conflicts, original SMC session, inactive source
exclusion, capture/projection failures, bounded outage recovery, spawned process
termination after source commit, independent-connection receipt races,
immutability/supersession, additive interrupted migration, malformed historical
compatibility, owner isolation, genuine active/shadow pipeline and financial
snapshots before/after capture, comparison and recovery. Existing comparison
concurrency and worker fencing/cancellation tests are retained and rerun.

Local fixture benchmark while full suites were running: 400 entry/60 primary/
55 secondary candles; 100 detached capture/preparation iterations; median
14.787 ms, p95 27.394 ms, 47,642-byte capture. One receipt recovery: 2.359 ms;
25 one-opportunity health reads: median 10.835 ms. Native strategy evaluation,
provider/network I/O, real feed contention and production database are excluded.
This measures added local CPU/serialization cost; it does not claim zero timing
overhead, hard latency bounds or production capacity. Projection stays outside
the decision-to-order critical path. Real-feed latency/soak validation is still
an operational prerequisite.

## Warning audit

No warnings are suppressed globally, and the new focused tests produce none.
The final backend warning categories are pre-existing dependency/lifecycle
deprecations, with slightly different existing client-construction counts in
the Hub and root runs.

| Source | Final frequency | Cause and assessment |
| --- | --- | --- |
| Existing FastAPI startup/shutdown hooks | 4 in each full suite | `on_event` deprecation; migrate lifecycle hooks in a separately reviewed change. No new evidence hook uses this API. |
| Existing aiohttp connector | 82 Hub / 81 root | `enable_cleanup_closed` is ignored because Python 3.12.14 includes the underlying CPython fix. No unclosed-resource warning was reported; this is a dependency configuration deprecation. |
| Existing Supabase/PostgREST outbox transport | 4 in each full/database selection | Two `timeout` and two `verify` deprecations in disposable real transport tests. Existing HTTP client options need migration; no evidence/financial integrity failure occurred. |
| Node browser/preview processes | 4 in final browser run | `NO_COLOR`/`FORCE_COLOR` environment conflict; display formatting only. |
| Vite production build | 1 advisory | Existing EChart bundle exceeds 500 kB. Performance concern, not a failed build or fabricated readiness. |

The root baseline had 88 warnings; the final root run has 89, with the same
categories. No new capture/API warning category, database reliability warning
or leaked-resource warning appeared. npm version notices are informational
notices, not warnings. Warnings remain visible in the final logs.

## Remaining operational blockers and Sprint 9 readiness

1. PostgreSQL active/shadow/research worker multi-claim/heartbeat/fencing backend
   and production-like concurrency certification. Existing disposable PG
   financial outbox/identity tests do not prove these worker semantics.
2. Externally supervised worker installation and health/monitoring integration;
   the earlier standalone shadow entrypoint is locally tested, not deployed.
3. OS CPU/memory/network sandbox and reviewed research/storage/retention quotas.
4. Explicitly authorized staging migrations, restart/recovery/backup restore,
   realistic load and real finalized-feed soak testing. None performed here.
5. Dataset retention/reconstruction guarantees after retention expiry, source
   availability auditing and strategy-specific long-history/lookback review.
6. Complete instance decision opportunities and multi-user legacy lab tenancy.
   Do not retrofit either by inventing historical evidence.
7. Durable persistence/redelivery counters, reset-before-projection archival,
   real-feed operational monitoring and verified backup procedures.

Ready for review and separately authorized staging validation of the supported
single-owner native source path. **Not production deployment ready.** Sprint 9
can build advisory decision intelligence on verified windows, while retaining
these gates and blockers. It must not treat source availability, agreement or
new capture as proof of trading profitability. XRP profitability remains
UNVERIFIED without authoritative financial evidence; funding is not replaced
with zero and no profitability calculation is introduced by this sprint.
