# Tradexa Guardian: evidence foundation

Status: local foundation, independently runnable service, read-only Command Center, incident correlation/investigation, declared dependency map, typed operational latency baselines, optional source observers/outboxes, paper-ledger inspection, daily/weekly received-evidence reports, deduplicated in-app notices, and an owner-governed research registry. This is **not** a deployed Guardian service, complete source coverage, or a completed eight-phase PRD.

## Boundaries

- `tradexa.guardian` contains no trading commands and imports no trading runtime.
- Guardian evidence belongs in its own owner-only SQLite database, never in a PA, SMC, instance, order, or journal database. WAL permits short writes beside readers. Replayed events with an identical ID and identical content are ignored; a conflicting replay fails. SQL triggers reject updates and deletes to raw events.
- Event `timestamp` is the source's aware clock; `received_at` is assigned by the store. This distinction is necessary for clock-skew and transport-lag investigations.
- Heartbeat state is current-state data, not immutable evidence. Missing, invalid, stale, or future heartbeats produce `UNKNOWN`, not `HEALTHY`. A heartbeat is **not** proof that strategy, feed, broker, and ledger are all healthy.
- Field validation rejects common secret-bearing names and values, oversized payloads, and non-JSON evidence. This is defense in depth, **not** a guarantee against every secret pattern. Producers must emit allowlisted, redacted evidence. Never send credentials, raw HTTP headers, or unrestricted exceptions.
- The database is operational evidence, not a tamper-proof external audit log. Its host owner can still alter SQLite or remove triggers. A production deployment needs restricted filesystem access, backups, and external integrity anchors.
- The standalone WSGI service runs with `python -m tradexa.guardian.service`, binds to `127.0.0.1:8765` by default, and imports no trading workers. The optional `compose.guardian.yaml` builds a separate Guardian-only Python image, uses a separate owner-only data directory and environment file, and binds the host port to loopback. Plain `docker compose up` does not start it. This overlay has **not** been deployed or build-tested on the VPS.
- The optional `GuardianEmitter` keeps a bounded in-memory queue and sends on a daemon thread. `emit()` does no network or database I/O. Queue overflow and delivery failures have counters; delivery is **best effort**, not durable. It must never replace order, position, risk, or journal persistence.
- The standalone Command Center at `/` is a shell, not the existing trading dashboard. It reads only Guardian's authenticated health, events, incidents, decision-trace and paper-ledger endpoints. No trading mutation endpoint or order control is present. It shows `UNKNOWN` until evidence is loaded, and still shows `UNKNOWN` if a read fails.
- The incident analyzer consumes the append-only event stream with a transactional cursor. Its incident/timeline tables are Guardian-owned derived views; the raw event is never edited or deleted. It groups a shared feed outage separately from worker, journal, and execution uncertainty. Routine strategy `condition_failed` and no-setup decisions are **not** incidents. Recovery requires an explicit source verification flag; a reconnect alone does not prove closed-candle continuity.
- `/v1/health` includes counts of all active incidents, not only the recent incidents page. A fresh `HEALTHY` heartbeat cannot make the overall state green while a WARNING-or-higher incident remains OPEN or RECOVERING; the overall state becomes `DEGRADED` with `ACTIVE_INCIDENTS`. This is an observability summary, **not** an instruction to pause, resume, or modify trading. Unknown or failed component heartbeats keep their own stronger/less-certain state instead of being overwritten by the incident overlay.
- When `GUARDIAN_PUBLIC_STATUS_URL=http://app:8000/status/public` is set, a Guardian-owned thread polls the app's existing coarse public status feed. It uses **no trading, webhook, exchange, or control credential**, and never invokes a trading mutation route. Only loopback or the internal Docker service name `app` is accepted. Public details are not copied into evidence; only allowlisted component states/reason codes are recorded. The monitor covers the API, Trading Instance workers, their market data and their ledger; it does **not** prove PA/SMC feed, journal, or execution health. Warming data and zero scheduled workers are UNKNOWN, not HEALTHY. An operational public feed is not by itself a verified incident recovery.
- Optional `GET /guardian/observations` in the app is disabled unless `HUB_GUARDIAN_OBSERVER_KEY` is configured. It requires that **separate read-only key even from a signed-in user or control-key holder**. It opens the PA and SMC databases with separate query-only SQLite connections and returns at most the newest 32 committed evaluation rows for each active session, projecting only decision identity, strategy version, state, reason, and condition pass/missing keys. It never calls a strategy, feed, broker, risk gate, or lab runtime. The app startup check refuses reuse of a control/webhook/exchange credential.
- `GUARDIAN_LAB_OBSERVER_URL` plus `GUARDIAN_LAB_OBSERVER_KEY` lets Guardian poll that route; the key is not a Guardian ingestion or Command Center key. The collector deduplicates material evaluation snapshots into immutable Guardian events. It calls the probe `HEALTHY` only when the observer response is fresh and valid; **the PA/SMC components remain UNKNOWN** until source-authoritative feed/execution health telemetry exists. This observer has **bounded recent coverage only**: it cannot guarantee every decision through a long outage, and the latest saved row may already have advanced state. It does not yet satisfy the PRD's every-evaluation trace or lifecycle-timeline acceptance criteria.
- Optional `GET /guardian/evaluations?lab=PRICE_ACTION|SMC&after=N&anchor=ID` pages at most 32 committed evaluation rows across **all retained sessions**, including inactive ones. The same independent observer key and a separate query-only SQLite connection are required; neither a dashboard login nor a control key grants this route. The API verifies the saved correlation ID at the last consumed rowid before returning the next page. A missing/replaced/reordered source cursor returns `PERSISTENCE_BLOCKED` rather than silently starting again. It never calls the lab runtime, strategy, market feed, broker, or risk gate.
- Optional `GET /guardian/lab-feeds` uses the same independent observer key. It reads active PA/SMC session identity on query-only SQLite connections and each attached stream's local, time-evaluated status snapshot. It calls no exchange/network provider, chart hydration, strategy evaluation, broker, or journal. SMC reports feed `HEALTHY` only when its chart reconciler also agrees on the latest closed-candle timestamp; transport-only green remains `BLOCKED`. An intentional replay session has `UNKNOWN` live-feed health, not a false outage. The response excludes quotes, raw errors, credentials, and account data. `GUARDIAN_LAB_FEED_URL=http://app:8000/guardian/lab-feeds` enables separate `pa_feed` and `smc_feed` Guardian heartbeats. Feed health is **not** lab execution health, so `pa_lab` and `smc_lab` remain `UNKNOWN` until fuller evidence exists. A failed poll marks the probe `FAILED`.
- Set `GUARDIAN_LAB_BACKFILL_URL=http://app:8000/guardian/evaluations` together with the separate `GUARDIAN_LAB_OBSERVER_KEY` to opt in. Guardian imports one bounded page per lab per poll. It commits immutable evidence and each lab's cursor in **one transaction in Guardian's own database**. An insert failure leaves the cursor unchanged; restart replays the page without duplicate evidence. `guardian_lab_backfill=DEGRADED` means there are more retained rows to catch up, not that trading is unhealthy. It becomes `HEALTHY` only when both sources are currently caught up. This proves **retained decision identity coverage after a polling outage** while the source rows remain intact; it does **not** recover deleted evaluations, earlier state transitions overwritten before observation, order/journal truth, or feed health. The recent snapshot observer remains necessary for material updates to recent rows. Manual source VACUUM/compaction can invalidate rowid anchoring and requires a reviewed recovery procedure; never reset the cursor automatically.

- Optional `GET /guardian/smc-execution` uses the same independent observer key to read the SMC Agent intent database and isolated SMC paper broker database with query-only SQLite connections. Its version 2 contract covers all outstanding intents (maximum 64), all open Agent journal trades (maximum 64), their linked intents even when older than the terminal sample, plus the eight most recent complete and eight most recent failed intents. Overflow fails closed. It matches recorded order IDs, can discover a committed entry order by its stable execution key even when the intent did not record the order ID, and checks linked journal trade and open-position ownership. It never submits, cancels, fills, or reconciles an order. `GUARDIAN_SMC_EXECUTION_URL=http://app:8000/guardian/smc-execution` enables an independent poller that saves deduplicated observations in Guardian's database. Its probe health means only that this read succeeded, **not** that SMC trading is healthy or execution integrity is proven.
- SMC execution observations are **cross-database and non-atomic**. `BROKER_ORDER_UNRECORDED` means an entry order exists in the paper broker but the intent's order ID is empty; it must never be interpreted as “no order placed.” `FILLED_ORDER_JOURNAL_PENDING` means a filled broker entry has no linked journal trade yet. `ORDER_AWAITING_FILL` means the order has not filled; `AGENT_TRADE_PRECEDES_FILL` means an agent trade journal row exists but **no broker fill or position is proven**, even if the intent says `COMPLETE`. `JOURNAL_SIZE_EXCEEDS_BROKER_FILL` means a partially filled order has less executed size than its still-open agent trade record. `EXECUTION_UNCERTAIN`, `ORDER_ID_NOT_FOUND`, `ORDER_IDENTITY_MISMATCH`, `TRADE_ID_NOT_FOUND`, `TRADE_IDENTITY_MISMATCH`, `OPEN_TRADE_POSITION_UNVERIFIED`, and `DUPLICATE_EXECUTION_KEY` all require operator investigation and source-authoritative reconciliation. `PENDING` is **not** proof of no broker order when an intent has no recorded ID. The exporter does not declare incidents or recovery based on one racing snapshot. A durable source outbox and repeated source-authoritative verification are still required before claiming complete execution coverage.
- The incident analyzer now groups SMC execution-integrity observations by execution key. An unfilled order alone, a pending intent, or a normal no-trade decision is not an incident. A trade journal row before any fill, a journal size larger than a partial fill, and other mismatches open a **POSSIBLE**, paper-only investigation; the severity is WARNING or HIGH according to the observed relationship. The broker and journal are read separately, so even a later `CONSISTENT` read only moves the incident to `RECOVERING`. It does **not** close it as verified. The source must provide an independently validated reconciliation event before any `RECOVERED` claim. The underlying SMC agent currently finalizes a trade journal row on broker order acceptance; this known execution/journal semantic defect is not repaired by Guardian observing it.
- Identical SMC execution snapshots replay with the same Guardian event ID. A new durable intent `updated_at`, even if the projected fields look unchanged, receives a new ID so the event store does not reject the new source timestamp as a conflicting replay. Resting orders without fills are informational, not warnings.
- Authenticated `GET /v1/decision-traces` and the Command Center's decision panel provide the latest received snapshot for each observed PA/SMC session/correlation ID. Backfill and live observation of one decision are not counted twice. The scan is capped at 2,000 evidence events and the response says when that cap is reached. A `WATCHING` decision with exactly one missing condition, at least two explicit conditions, and every other condition `PASS` is labeled an **unproven near-valid research candidate**. It is not a missed-profit claim, a strategy recommendation, an outcome study, or proof of complete lifecycle coverage. Missing trace/unknown statuses never qualify. No trading rules or orders are changed.

- The production PA/SMC account wrappers install append-only `{pa,smc}_guardian_lifecycle` tables and evaluation INSERT/UPDATE triggers. They run in the source evaluation statement's transaction and capture only material state, reason, missing-condition, order-ID, or fill changes. Stored condition evidence is key/status only; no full saved payload, rolling candles, live quote, or heartbeat is copied. Immutable-row triggers reject UPDATE/DELETE. A source write error cannot commit the evaluation transition while omitting its outbox row. These are **post-install** transitions only; pre-install history is not reconstructed. This is not a broker/ledger transaction or execution certification. Standalone accounts outside the app wrapper do not install the outbox.
- Optional `GET /guardian/lifecycle?lab=PRICE_ACTION|SMC&after=N&anchor=ID` reads at most 32 source outbox rows using the independent observer key and a query-only SQLite connection. A missing table, lock, or broken cursor returns structured `PERSISTENCE_BLOCKED`. `GUARDIAN_LAB_LIFECYCLE_URL=http://app:8000/guardian/lifecycle` enables a separate Guardian importer. It commits immutable evidence with each per-lab cursor atomically and resumes after restart without duplicates. A caught-up import heartbeat proves ingestion progress, **not** lab/feed/broker health. Deploy the app image containing the trigger before enabling this collector.
- The Trading Instance `DecisionStore` now installs an immutable `guardian_decision_lifecycle` outbox in its own SQLite database. Its INSERT/UPDATE triggers capture instance-attributed strategy verdict, downstream gate/final state, blocker, execution flag, and key/status-only rule projection in the same source statement. Transient component/quote changes and repeated identical finalization create no new evidence. Pruning mutable decision rows leaves committed outbox evidence intact. It covers **only post-install, successfully persisted** decisions: the current `AutoEngine` may still continue after a decision-store failure, so this outbox cannot prove that every evaluated signal was captured or that an `executed` flag corresponds to a broker fill.
- Optional `GET /guardian/instance-decisions?after=N&anchor=ID` uses the independent observer key and query-only SQLite to export at most 32 source transitions per page. `GUARDIAN_INSTANCE_DECISION_URL=http://app:8000/guardian/instance-decisions` enables a separate Guardian importer that commits events and its cursor atomically. Invalid source cursors and persistent SQLite locks return structured `PERSISTENCE_BLOCKED`. Its `guardian_instance_decisions` heartbeat means import availability/catch-up only; it does **not** establish worker, market-data, ledger, execution, or risk health. Deploy the app outbox before enabling the collector; pre-install decision history is not reconstructed.
- Optional `GET /guardian/instance-ledger` uses the same independent observer key to read the configured **primary** Trading Instance ledger. It does not create a SQLite fallback when a configured Supabase primary fails. An unavailable, locked, malformed, or over-limit source returns structured `PERSISTENCE_BLOCKED`. The adapter reads at most 64 open instance-attributed positions, 64 open instance-attributed paper trades, and 64 OPEN/REDUCE execution links; more fails closed instead of hiding exposure. PA/SMC lab accounts, unattributed legacy rows, live venue exposure, and exchange fills are **outside its scope**. A SQLite source is read in one query-only transaction; a Supabase source uses separate bounded requests and is explicitly **non-atomic**. A non-atomic or unpaired snapshot has unknown risk (`risk_amount=null`), never a confirmed zero. A matched atomic paper pair estimates risk from position size and entry-to-stop distance; it is not proof that protective venue orders exist.
- `GUARDIAN_INSTANCE_LEDGER_URL=http://app:8000/guardian/instance-ledger` enables change-only collection into Guardian's separate immutable event store. The event and its deduplication checkpoint commit in one Guardian transaction. Heartbeats continue without creating a revision on every poll; material changes append one new observation. `guardian_instance_ledger_probe=HEALTHY` means only that the read and import succeeded. Findings such as `MISSING_STOP`, `OPEN_POSITION_TRADE_UNVERIFIED`, `EXECUTION_LINK_UNVERIFIED`, and `SESSION_MISMATCH` are possible **paper-ledger** mismatches for investigation, not live-exchange facts or commands. There is no automatic pause/recovery and no certified cross-database incident close from this observer. The remote Supabase path has local mock coverage but no production integration proof.
- `/v1/instance-ledger` joins the last change-only snapshot to the probe heartbeat. Unchanged successful reads keep it current without another immutable event; a failed or stale probe masks all current risk amounts as unknown. Cached row counts/findings remain explicitly historical. Currency is not verified by the source schema: risk is in source units only, `global_risk_amount` is always null, and PA/SMC and live accounts are never summed. The Command Center clears current risk on any failed refresh and clears all rows on disconnect.
- Significant paper-pair findings create one incident per instance. Atomic, coverage-verified SQLite rows can confirm a **paper accounting** mismatch only; non-atomic remote reads remain `POSSIBLE`. Unverified legacy execution links and ordinary no-trade states do not trigger incident spam. A later matching pair moves an existing incident to `RECOVERING`, never to certified `RECOVERED`; no broker, stop order, repair, or live exposure is certified.

## Local service contract

### SMC open Agent journal links

The SMC execution source and collector must be upgraded together: the collector
rejects the old version 1 coverage claim rather than treating it as complete open
journal coverage. Version 2 declares
`ALL_OUTSTANDING_AND_OPEN_JOURNAL_PLUS_RECENT_TERMINAL`. The execution list is a
deduplicated union, at most 144 rows: 64 outstanding, 16 recent terminal and at
most 64 additional intents linked to open trades. `extra_open_intent_count`
counts only those additional rows. `open_journal_trades` includes all open Agent
trades across retained sessions, at most 64, with the exact reverse link keys.
The linked intent bound is also enforced; no overflow produces a healthy partial
sample. Journal reads share one transaction; the broker remains a separate read.

An early durable intent may have no assigned decision/session link yet. The
collector retains those missing values explicitly; its stable execution key is
still observed, without inventing a correlation or claiming execution integrity.

`JOURNAL_INTENT_NOT_FOUND` is a missing authoritative link, not proof of no order
or a corrupt legacy journal. Guardian records it by paper account and trade ID
without inventing an execution key. `MULTIPLE_INTENTS_FOR_TRADE` reports two or
more intents claiming the same journal trade. Market identity checks cover symbol,
timeframe and direction, not just matching order IDs. `POSITION_SIDE_MISMATCH`
and `CLOSED_TRADE_POSITION_STILL_OPEN` are investigation findings only; the latter
requires the **same entry order**, not merely another position on the same symbol.
The frozen journal entry/stop/target/RR are the approved plan: slippage or subsequent
stop management does not by itself constitute an identity mismatch. This projection
does not certify their execution geometry or a complete exit chain.

Execution and open-journal link observations have versioned, account-scoped material
hashes. Execution hashes also include decision/session/market identity and the
durable intent update time. An unchanged refresh or Guardian restart imports no
duplicate evidence. Guardian write failure can leave a partially imported snapshot;
replay completes its individually immutable events idempotently and marks the probe
failed until successful. There is **no cross-file atomic import/reconciliation claim**.
Significant findings create `POSSIBLE` incidents in the existing incident API. A
later link-found read moves an existing link incident only to `RECOVERING`, never
certified `RECOVERED`. Account/record delimiters are escaped to avoid merging scopes.
Previously retained version 1 events are unchanged; unscoped legacy incidents are
not automatically resolved by version 2 evidence.

The source selects only bounded identity/count/quantity fields, never intent JSON,
journal prose or candle windows. It adds no source table/index or hot-path write.
Read connections use `mode=ro`, `query_only`, a 250 ms busy timeout and a 500 ms SQL
progress deadline. A blocked/unavailable/oversized source returns sanitized 503
`PERSISTENCE_BLOCKED`. WAL reads remain available during a short write; no durability
pragma is changed. The collector rejects redirects, bounds HTTP responses at 1 MiB,
checks link back-references before importing any event, and marks a failed poll
`FAILED` rather than retaining a fresh healthy probe.

This closes the **older still-open journal trade** sampling gap only. It does not
backfill every closed Agent trade or every intent transition, certify protective
exit parentage, or recover transitions missed during a polling outage. PA setup
journals, automatic SMC Lab executions outside the Agent and live accounts retain
their separate contracts. Full journal/position lifecycle coverage, verified
cross-account currency/exposure and production acceptance remain Phase 4 work.

### Isolated lab execution / risk evidence

The optional `GUARDIAN_LAB_EXECUTION_URL=http://app:8000/guardian/lab-execution`
collector uses the independent observer credential and requests `lab=PRICE_ACTION`
and `lab=SMC` separately. A failing PA read does not suppress the SMC observation.
The source reads each configured lab SQLite file using `mode=ro`, `query_only`, a
single read transaction, a 250 ms busy timeout and a 500 ms SQL progress deadline.
It never instantiates a broker/runtime or changes pragmas governing source durability.
All open positions (maximum 16) and open orders (maximum 32) are included; exceeding
these bounds fails closed. Eight recently inserted orders, every open position's
origin, order links for the 16 newest fills, and at most 128 recently inserted fills
form a bounded history sample. The final order set is bounded at 64. Insertion order
is used for the sample, not claimed as event chronology or complete historical coverage.
No rolling candle windows, quote updates, or full journal JSON are exported.

Guardian compares order quantity/remaining/filled arithmetic, sampled fill quantity
and weighted price, account/engine identity, position origin, reduce-only exit flags,
order keys and session links. All identity comparisons stay inside one paper account.
PA setup-journal links are observed separately: a setup snapshot is never presented
as proof of a finalized execution trade. SMC Agent intent/trade reconciliation remains
in its existing separate, non-atomic observer. Synthetic protective/remediation fills
may legitimately have no persisted order row. Unlinked fills and incomplete fill
history stay **UNVERIFIED**, not invented orphan orders or proof of failed protection.
The sampled order-key check cannot certify uniqueness across all retained history.

Important finding codes:

| Codes | Meaning |
| --- | --- |
| `ORDER_QUANTITY_MISMATCH`, `ORDER_STATUS_QUANTITY_MISMATCH`, `FILLED_QUANTITY_MISMATCH`, `FILLED_PRICE_MISMATCH` | Inconsistent values within one atomic paper-record snapshot; no automatic repair |
| `ORDER_IDENTITY_MISMATCH`, `FILL_ORDER_IDENTITY_MISMATCH`, `DUPLICATE_ORDER_KEY` | Observed identity conflict in the bounded sample |
| `EXIT_NOT_REDUCE_ONLY`, `ENTRY_MARKED_REDUCE_ONLY` | Persisted action/flag contradiction; paper evidence only |
| `ORDER_SESSION_LINK_UNVERIFIED`, `PA_SETUP_JOURNAL_UNVERIFIED` | Required association not established; does not mean no order was placed |
| `POSITION_ORIGIN_UNVERIFIED`, `POSITION_STOP_UNVERIFIED`, `ORDER_AVERAGE_PRICE_UNVERIFIED`, `FILL_HISTORY_INCOMPLETE` | Evidence insufficient for the corresponding claim |

Significant contradictions and missing stops produce grouped incidents scoped by
lab/account/record. Atomic contradictions confirm only a **record fact**, not its
cause or a live-venue risk. Missing stops are possible protection issues. Normal
resting/cancelled orders and incomplete historical samples do not create incident spam.
A record disappearing from the sample cannot certify recovery; these incidents
remain open for independently verified resolution.

`GET /v1/lab-execution` requires the Guardian read key and exposes separate PA/SMC
observations. An entry-to-stop amount is `size * max(0, adverse entry-to-stop distance)`
in **unverified source units**, excluding costs/gaps; trailing stops across entry are
not rejected. No mark-to-market risk, portfolio sum, currency conversion, guaranteed
protective fill or whole-position lifecycle is certified. Missing/contradictory
origin evidence masks the amount. Failed/stale probes and failed browser refreshes
mask it too. Probe `HEALTHY` means the evidence read succeeded, not that execution
is healthy. Unchanged polls/restarts create no new immutable event; material changes
append evidence and checkpoint atomically in Guardian's own database. Source history
is never rewritten, deleted, vacuumed or reset.

This slice does not complete Phase 4: authoritative cross-account currency/exposure,
correlation, every fill/exit's durable lifecycle, and full journal/reconciliation coverage
remain required. Production installation and fault/load verification remain separate.

### Retained closed SMC Agent journal observations

Optional `GUARDIAN_SMC_JOURNAL_HISTORY_URL=http://app:8000/guardian/smc-journal`
imports **recorded journal closes**, not broker execution truth. It is disabled by
default and uses the existing independent observer key. Its scope is
`CYCLIC_RETAINED_SMC_AGENT_CLOSED_TRADES`. No SMC rule, agent, journal writer,
runtime, paper broker, session, source schema/index or source transaction is changed.
Guardian never opens the journal for writing or runs its migration/constructor.

An append-only rowid cursor alone would miss an older trade that closes after the
cursor has passed it. Instead, each finite pass captures a maximum trade rowid,
reads at most **32 trade rows per 30-second poll**, then starts a new pass at zero.
Open rows advance the scan but create no immutable event. Concurrent new opens do
not extend the current pass, so they cannot starve the revisit of earlier rows.
A late close behind the cursor is observed when the next pass reaches it; detection
is **eventual**, not immediate. For a fixed N-row retained table, a pass takes
`ceil(N/32)` successful polls. Outages postpone coverage; no completeness is claimed
for a whole pass because different pages have different read snapshots.

`GET /guardian/smc-journal` accepts the exact returned `next_cursor` fields as
query parameters: `cycle`, `after`, `upper`, `origin`, `anchor`. Initial values are
zero/empty. The source uses query-only SQLite, a 250 ms busy timeout, a 500 ms SQL
progress deadline, one snapshot per page and bounded scalar projections. It never
loads candle windows, market/condition JSON, free-form trade rationale or credentials.
The first retained trade's frozen plan anchors the origin; the pass upper row and
predecessor's frozen plans anchor progression. Close fields are deliberately absent
from anchors, so closing these rows does not break continuation. A detected origin,
upper or predecessor change fails rather than silently resetting the scan. These
anchors detect changes at the sampled boundaries, **not every possible source edit**.
Lock/missing-source/malformed evidence returns a redacted 503
`PERSISTENCE_BLOCKED / SMC_JOURNAL_HISTORY_UNAVAILABLE`. Negative/out-of-range
numeric HTTP parameters return 422; malformed semantic cursors fail closed with 503.
Authentication is `X-Guardian-Observer-Key`; it grants GET only, not control access.

Closed rows become `smc_closed_journal_observed` events. Identity is SHA-256 over
the versioned namespace, source origin and original journal trade ID. Each preserves
recorded decision/order IDs, symbol/timeframe/direction, frozen entry/stop/target/RR/
size, sizing metadata, strategy fingerprint, open/close timestamps, recorded exit,
result and realised R. A frozen entry is **not** an actual fill price. Empty optional
provenance is not fabricated, and no execution key, session, account, currency,
position or exit-parent identity is inferred. Closed rows are immutable under the
existing journal writer; a changed reimport under the same event ID is a hard collision,
not an overwrite. These observations do not automatically finalize or repair journals.

The events and repeat-scan checkpoint commit atomically, with compare-and-swap,
in **Guardian's own** `observer_scan_state` and evidence tables. Failed inserts or
checkpoint writes roll back both. Restart resumes the current finite pass; repeat
passes and 100 unchanged refreshes create no additional evidence. Only the compact
checkpoint/heartbeat changes on successful polls. No source revisions are generated.
Redirects, untrusted URLs and responses exceeding 256 KiB are rejected. Collector
failure marks its probe failed; it cannot stop or gate any trading worker.

`GET /v1/smc-journal?after=0` uses the independent `X-Guardian-Key` read key and
pages imported closes by **Guardian ingestion sequence**, 32 per page. Do not use
that `after` as a source scan cursor. History remains readable during source outages.
The response includes the durable scan cursor and probe age; `SCANNING` means a
finite pass is in progress, `PASS_COMPLETED_AT_LAST_POLL` means only the last pass
finished, and missing/failed/stale evidence is `UNKNOWN`. Probe `HEALTHY` means
the bounded source read succeeded, never that the strategy or broker is healthy.
Invalid queries return 400, persistence errors 503, mutations 405. There is no UI
or verified historical count/P&L added. `full_lifecycle_verified`,
`execution_integrity_verified`, `source_history_immutable_verified`,
`currency_verified` and `net_pnl_verified` remain false.

Remaining Phase 4 gaps include authoritative broker fill/exit/position links to
every journal close, cross-account currency/risk, and production fault/load
validation. This milestone is local observation coverage,
not full lifecycle or live/exchange certification.

### Retained SMC Agent execution-intent transitions

Optional `GUARDIAN_SMC_INTENT_HISTORY_URL=http://app:8000/guardian/smc-intent-events`
imports at most 32 committed `execution_intent_events` rows per poll across all
retained sessions. This is disabled by default and uses the independent
`GUARDIAN_LAB_OBSERVER_KEY`. It does not attach to or reconcile an Agent, approve
a decision, submit/cancel an order, or alter an execution gate.

The source endpoint is GET-only, requires `X-Guardian-Observer-Key`, and reads
`settings.smc_agent_journal_db` through a separate query-only SQLite connection.
It never constructs a journal writer, broker or runtime. Reads have a 250 ms
busy timeout and a bounded query deadline; each page is one atomic read snapshot.
Missing/locked/malformed sources or changed cursor anchors return structured
`503 PERSISTENCE_BLOCKED / SMC_INTENT_HISTORY_UNAVAILABLE`, with no paths or raw
errors. Short WAL writes do not block these reads. No source database, schema,
journal triggers or retention policy is changed.

Paging uses source rowid (`after`) plus a projection hash (`anchor`), not the
event timestamp. Late-dated events remain discoverable. The first retained
transition anchors the origin; that first projection plus the last consumed
transition anchors each checkpoint. A removed/replaced/changed origin or last
consumed event fails closed without rewinding or resetting evidence. Manual
source compaction/VACUUM may invalidate rowid anchors and needs reviewed recovery.
This is not an audit of the immutability of every previously consumed source row.

Each projection preserves the original event ID, execution key, recorded state,
order ID, trade ID and timestamp. Only frozen parent identity is added: intent ID,
session, symbol, timeframe, decision candle and proposal ID. Today's parent state,
late-bound decision ID and latest order/trade links never overwrite historical
event facts. Raw JSON payloads and error text are excluded; `error_recorded` is
only a boolean. Distinct preparation events may correctly share the recorded
`EXECUTION_PENDING` state without being duplicate events.

Guardian commits its immutable projections and monotonic checkpoint together in
its own database. Event IDs hash the source origin and original event ID, so
unchanged polls, crash replay and restarts do not append duplicates. A concurrent
collector cannot skip a page: checkpoint compare-and-swap rejects its stale page
and the next poll retries from durable progress. Guardian has no trading data
volume mount and retrieves bounded JSON only (256 KiB maximum, redirects refused).

`GET /v1/smc-intent-events?after=0` uses `X-Guardian-Key` with the separate read
key and pages Guardian's own sequence, 32 events at a time. Its `next_after` is
not the source cursor; `source_cursor` separately reports import progress.
`IMPORTING` means more retained rows remain; `CAUGHT_UP_AT_LAST_POLL` means only
that no next row was observed at the last successful poll. Missing, failed or
stale probes return `UNKNOWN` while preserving cached historical evidence.
Invalid queries return 400, persistence errors 503 and mutations 405.

Recorded states are `DECISION_APPROVED`, `EXECUTION_PENDING`, `EXECUTED`,
`EXECUTION_FAILED`, `EXECUTION_UNCERTAIN`, `RECONCILED` and `COMPLETE`. They are
historical journal claims, not current execution status or verified broker truth.
No predecessor state, fill, position, cause, trade finalization or P&L is inferred.
`execution_integrity_verified`, `full_lifecycle_verified` and
`source_history_immutable_verified` remain false, including for recorded
`EXECUTED`/`RECONCILED`/`COMPLETE` events. This extends retained observation
coverage without certifying the full execution lifecycle or live routing.

### Retained SMC explicit entry-fill and journal links

`GET /v1/smc-execution-links?execution_key=<original-key>` requires the separate
Guardian read key (`X-Guardian-Key`). It reads **Guardian's own imported evidence**,
not a trading database, broker, runtime or current strategy. It uses the retained
SMC intent, SMC fill and closed Agent journal collectors already documented above.
No additional source endpoint, collector, environment flag or trading authority
is introduced. No PA record, generic producer or time/market-only match can supply
an SMC link.

Lookups are by exact original execution key, original order IDs and recorded trade
IDs using Guardian-owned partial JSON indexes. Every query is bound and indexed;
large unrelated history does not evict an older execution merely because it is no
longer recent. The view takes one query-only Guardian snapshot, including probe
ages, with 250 ms busy timeout, a one-second query deadline, 128 rows per evidence
category, a 2 MiB read budget and 16 KiB maximum per related event. Overflow sets
`truncated=true`, makes the link state `UNKNOWN` and masks quantities and the last
recorded state. Malformed or oversized evidence returns a redacted 503. No reads
append events, checkpoints, incidents, trades or orders. A missing Guardian file
is not recreated by this read path.

`EXPLICIT_ENTRY_FILL_LINKS_OBSERVED` means the captured fill records match one
recorded order ID, the original execution key and market identity, with no observed
account/link conflict and fresh caught-up intent/fill probes. The quantity and
weighted price describe **only captured entry-linked fill records**: not an open
position size, complete order fill, realized return or certified trade. Resting
orders may already have a `COMPLETE` Agent journal row but no fill; that is
`INSUFFICIENT_EVIDENCE`, never a synthetic fill. A fill arriving later is seen by
the next existing fill import without requiring another intent event.

`BROKER_FILL_UNRECORDED_ON_INTENT` preserves a discovered broker fill when the
intent has not yet recorded its order ID; its link remains `UNVERIFIED`, never
`MISSED` or "No order was placed". A recorded journal close is linked only by
explicit order **and** trade IDs plus market identity. It does not certify that
the broker exited. This retained importer/link projection does not consume the
new source-side `fill_position_json` described below. Legacy fill rows lack that
origin evidence, and the position table contains only current net positions.
The view does not invent historical associations from timestamps, prices, sizes,
symbol or P&L; it remains entry-only pending a separate provenance importer.

Conflicting keys/orders/trades/markets/directions, repeated source IDs, different
intent origins or multiple paper accounts yield `CONFLICTING_EVIDENCE` and no
quantity/price aggregate. `UNKNOWN` takes precedence for stale/failed/importing
intent or fill probes and truncated reads. Cached historical records remain
visible; a failed close-journal probe labels close evidence `UNKNOWN` without
hiding independently observed entry fills. `latest_recorded_state` is journal
history, never a verified current runtime state. All no-match findings mean
"not observed", not proof of no execution.

Important finding codes include `FILL_ORDER_LINK_CONFLICT`,
`FILL_EXECUTION_KEY_CONFLICT`, `FILL_MARKET_CONFLICT`, `FILL_SIDE_CONFLICT`,
`MULTIPLE_PAPER_ACCOUNTS`, `MULTIPLE_JOURNAL_ORIGINS`, `INTENT_IDENTITY_CONFLICT`,
`JOURNAL_ORDER_LINK_CONFLICT`, `JOURNAL_TRADE_LINK_CONFLICT`,
`JOURNAL_MARKET_CONFLICT`, `JOURNAL_DIRECTION_CONFLICT`,
`FAILED_INTENT_WITH_RECORDED_FILL` and `LINK_EVIDENCE_TRUNCATED`. They are bounded
record findings, not proven causes, incidents or recovery commands.
Missing legacy fill keys/timeframes or journal order IDs remain `UNVERIFIED`,
not confirmed identity conflicts; missing entry links mask the aggregate.

`guardian_snapshot_atomic=true` refers only to Guardian's own snapshot;
`cross_database_atomic=false` explicitly preserves the independent import boundary.
`execution_integrity_verified`, `full_lifecycle_verified`,
`position_lifecycle_verified`, `exit_link_verified`,
`paper_account_binding_verified`, `currency_verified`, `net_pnl_verified` and
`observed_quantity_is_complete` remain false. Query errors return 400, missing
read authority 401, mutations 405 and persistence/evidence failures 503. The whole
Guardian execution/journal PRD is still incomplete; durable exit/position origin
evidence, complete lifecycles and production fault acceptance remain required.

### Resumable retained PA/SMC fill evidence

`GUARDIAN_LAB_FILL_HISTORY_URL=http://app:8000/guardian/lab-fills` optionally imports
retained `v2_fills` rows, independently for each lab, using the existing dedicated
observer key. It is disabled by default. Each collector imports at most 32 rows per
30-second poll. The source uses query-only SQLite, one read transaction, a 250 ms
busy timeout and a 500 ms SQL progress deadline. Rowid keyset seeks replace full
table scans, offsets and timestamp cursors. A late-inserted fill with an old event
timestamp is still observed. No schema, broker order, position, journal or strategy
is modified on the source. No trading hot-path writes or network calls are added.

The source endpoint is `GET /guardian/lab-fills?lab=SMC&after=0&anchor=` (or
`lab=PRICE_ACTION`), authenticated with `X-Guardian-Observer-Key`. It returns a
bounded atomic page, `next_after`, `next_anchor` and `has_more`; lock, missing-source,
malformed-evidence and changed-cursor failures return a redacted 503
`PERSISTENCE_BLOCKED / LAB_FILL_HISTORY_UNAVAILABLE`. The account, first retained
fill and last consumed fill's material content are SHA-256 bound into the anchor.
Detected reset/deletion/replacement never silently resets the cursor or skips rows.
The outer application middleware denies an unconfigured observer with 401.

Each Guardian event has a stable SHA-256 identity over the schema namespace, lab,
account and original fill ID. It preserves fill/order identity, timestamp, quantity,
price, recorded costs, stop/target and optional decision-candle/quote provenance.
Empty optional legacy metadata remains empty, not fabricated. Evidence and the
page checkpoint commit atomically in Guardian's own store. A crash before commit
replays the page; a crash after commit resumes after it. Conflicting IDs, malformed
pages or a concurrently moved checkpoint fail closed. Unchanged polling/restarting
creates no extra event. Source failure in one lab does not suppress the other.

`GET /v1/lab-fills?lab=SMC&after=0`, using the separate `X-Guardian-Key` read key,
pages the imported evidence in **Guardian ingestion sequence**, 32 rows per page.
Use its `next_after` only on this endpoint, not as the source cursor. Missing or
stale evidence is `UNKNOWN`; a backlog is `IMPORTING`; a successful exhausted source
page is `CAUGHT_UP_AT_LAST_POLL`. The latter means only that the retained fill table
had no next row at that poll, **not** that execution, accounting or trading is healthy.
Read failures return 503 `PERSISTENCE_UNAVAILABLE`; invalid pagination returns 400;
mutations return 405. Cached immutable history remains readable during source outages.
The existing raw event view also shows imported fills; a dedicated history UI is not
part of this slice.

Coverage limits are intentional: these are retained fill records, not reconstructed
position lifetimes or finalized journal trades. Protective/remediation fills can
legitimately lack a persisted order row. No exit-parent or session link is inferred
from symbol, timestamps or an order-ID prefix. There is no historical total, verified
net P&L, currency aggregation or live/exchange certification. Funding and costs outside
the fill row remain outside this evidence. Historical deletes/edits between the two
checked anchor rows are not detected by this bounded reader; it is not a whole-table
cryptographic audit or a guarantee that source history was always immutable. Guardian
retains what it already observed and never repairs the broker to agree with it.

Closed-UTC daily and Monday-to-Monday weekly reports are generated independently at startup and checked hourly. Only the last closed day and week are scheduled; older windows are an explicit offline call to `GuardianReports.generate`, not an unbounded startup backfill. Each report scans at most 10,000 events / 8 MiB, records coverage/truncation, deduplicates material decision snapshots by source identity, and keeps separate owner/strategy/version/config/market groups. Source coverage is not complete: uptime, trade count, P&L, win rate, average R, drawdown, excursions, global exposure and successful interventions are null. Late source evidence creates a new immutable content-addressed revision; unchanged polls/restarts create none. Report plus its audit event commit atomically; a failure rolls both back. No strategy, backtest, model, credential or trading runtime is invoked.

In-app incident notices have a durable transactional cursor. A new WARNING-or-higher incident, severity escalation or source-verified recovery creates an immutable notice and audit event in one Guardian transaction. Repeated outage updates, reconnect-only states, normal no-setup decisions and near-valid candidates do not generate more notices. Recovery is not a Guardian intervention. Remote push/email/Telegram delivery is **not configured or implemented**; no destination or delivery secret is sent anywhere. `guardian_reports` and `guardian_notifications` heartbeats reflect these local processors, not trading health.

The research registry is **governance, not a research runner or certification system**. A hypothesis binds existing Guardian evidence IDs, exact strategy/version/commit/config identities, and one immutable candidate artifact SHA-256. Identity deduplication retains failed/rejected hypotheses across restart. Owner `SEND_TO_BACKTEST` approval is required before accepting the first result. Results must then follow historical backtest → out-of-sample → walk-forward → stress → forward-paper → statistical comparison → recommendation, without skipping. The registry validates aware completed periods, distinct and later out-of-sample data, nonoverlapping causal walk-forward fold declarations, finite samples/metrics and unchanged candidate identity. These structural checks do **not** verify artifact bytes, backtester code, strategy-copy isolation, statistical sufficiency or absence of internal lookahead. Every result remains `method_verified=false`; no backtest or paper experiment is executed by this service.

Only a separate owner key can reject a hypothesis or approve development after all seven reported stages. Reviews bind the current evidence digest, fail on stale evidence, and are immutable/idempotent. Approval ends at `APPROVED_FOR_DEVELOPMENT_NO_DEPLOYMENT`, never modifies code/risk/orders or deploys. Hypothesis/result/review plus audit event commit atomically; failure rolls all changes back. `GUARDIAN_RESEARCH_KEY` and `GUARDIAN_ADMIN_KEY` are optional, independent of ingestion/read/observer/control credentials, and disabled by default. The Command Center is still read-only; it displays provenance, reported stages and unresolved verification. Do not send exchange secrets, raw generated code, commands, file paths or live-routing requests into this registry.

Configuration requires `GUARDIAN_DB_PATH`, `GUARDIAN_SOURCE_KEYS_JSON` (a JSON object mapping each source service to its own long random key), and a distinct `GUARDIAN_READ_KEY`. Optional settings: `GUARDIAN_REQUIRED_COMPONENTS`, `GUARDIAN_BIND_HOST`, `GUARDIAN_PORT`, `GUARDIAN_PUBLIC_STATUS_URL`, `GUARDIAN_LAB_OBSERVER_URL`, `GUARDIAN_LAB_BACKFILL_URL`, `GUARDIAN_LAB_LIFECYCLE_URL`, `GUARDIAN_INSTANCE_DECISION_URL`, `GUARDIAN_INSTANCE_LEDGER_URL`, `GUARDIAN_LAB_EXECUTION_URL`, `GUARDIAN_LAB_FILL_HISTORY_URL`, `GUARDIAN_LAB_FEED_URL`, `GUARDIAN_SMC_EXECUTION_URL`, `GUARDIAN_SMC_JOURNAL_HISTORY_URL`, and `GUARDIAN_LAB_OBSERVER_KEY`. The service rejects reuse of `HUB_CONTROL_KEY` when it is present in its environment and refuses a lab observer key shared with its read/source keys. Keep the database in a dedicated owner-only directory and supply secrets through a protected environment mechanism, not source control or shell history. With the public or lab collectors enabled, their own probes are required for overall health; `pa_lab` and `smc_lab` remain `UNKNOWN` until independent lab-health telemetry exists.

| Method | Path | Authority | Result |
| --- | --- | --- | --- |
| GET | `/` and `/assets/command-center.*` | local shell only | static read-only Command Center; no evidence or key embedded |
| GET | `/healthz` | local liveness probe | `self_state=ALIVE`, `readiness_state=NOT_CHECKED`; no persistence access or health heartbeat write |
| POST | `/v1/events` | source-specific `X-Guardian-Key` | 201 appended, 200 identical replay, 403 source mismatch, 422 invalid evidence, 503 persistence unavailable |
| POST | `/v1/heartbeats` | source-specific `X-Guardian-Key` | current heartbeat for that source or a source-prefixed component |
| GET | `/v1/events?limit=50&source_service=smc_lab` | separate read key | paged recent immutable evidence |
| GET | `/v1/health` | separate read key | evidence-backed component health, with missing/stale components `UNKNOWN` |
| GET | `/v1/self-health` | separate read key | configured Guardian monitor heartbeats and read-only own-storage diagnostics; no query parameters or trading authority |
| GET | `/v1/incidents?limit=50&state=OPEN` | separate read key | derived incident summaries, filtered by state when requested |
| GET | `/v1/incidents/<incident_id>/timeline` | separate read key | ordered source event references and state transitions |
| GET | `/v1/incidents/<incident_id>/investigation` | separate read key | bounded source/receipt timeline, linked execution/decision/dependency context, failure facts and **unproven** causal candidates |
| GET | `/v1/system-map` | separate read key | declared prerequisites with independent observed states, dependency blocks and unknowns; no trading gate mutation |
| GET | `/v1/anomalies` | separate read key | causal closed-window, version/config/account-isolated typed latency observations; sparse/truncated data is insufficient evidence |
| GET | `/v1/decision-traces?limit=50&lab=SMC` | separate read key | bounded latest observed decision snapshots, deduplicated by lab/session/correlation; near-valid flags are unproven |
| GET | `/v1/instance-decision-traces?limit=50&instance_id=...` | separate read key | bounded latest post-install persisted instance gate states; strategy verdict and downstream gate result are distinct, broker fills unverified |
| GET | `/v1/instance-ledger` | separate read key | latest paper-pair snapshot and current probe age; stale/failed risk masked; no global or verified-currency exposure |
| GET | `/v1/lab-execution` | separate read key | separate PA/SMC paper order/fill/position observations, bounded integrity findings, source age and masked stale amounts |
| GET | `/v1/lab-fills?lab=SMC&after=0` | separate read key | resumable imported fill evidence, 32 rows per page, per-lab import freshness; not verified trade/P&L accounting |
| GET | `/v1/smc-journal?after=0` | separate read key | imported immutable closed Agent journal projections; late closes found by repeat scans, not verified broker exits or P&L |
| GET | `/v1/smc-intent-events?after=0` | separate read key | retained recorded Agent transitions with original execution/order/trade links; not verified broker execution or current status |
| GET | `/v1/smc-execution-links?execution_key=<original-key>` | separate read key | bounded exact retained entry-fill/order/trade links; no invented exit or position lifecycle, no accounting certification |
| GET | app `/guardian/smc-fill-transitions?after=0&anchor=` | independent `X-Guardian-Observer-Key` | query-only source page of at most 32 retained fill-position projections; not a Guardian `/v1` view or collector |
| GET | `/v1/reports` | separate read key | most recent 20 immutable closed-window evidence revisions; unknown economics remain null |
| GET | `/v1/notifications` | separate read key | newest 50 deduplicated in-app incident notices; no remote send or remediation |
| GET | `/v1/research/hypotheses` and `/v1/research/<64-hex-id>` | separate read key | bounded hypotheses/results/reviews with provenance and methods unverified |
| POST | `/v1/research/hypotheses` | optional separate research key | validated immutable hypothesis; cannot execute it |
| POST | `/v1/research/<64-hex-id>/results` | optional separate research key | source-reported result for exactly the next approved stage; no runner/certification |
| POST | `/v1/research/<64-hex-id>/review` | optional separate owner/admin key | `SEND_TO_BACKTEST`, `REJECT`, or `APPROVE_FOR_DEVELOPMENT`, bound to current `expected_digest`; no deployment |

Local browser smoke: run `node scripts/guardian_ui_smoke.cjs` with Playwright available. If its bundled Chromium is absent, set `GUARDIAN_SMOKE_CHROME` to a locally installed Chrome executable. The test serves actual Guardian assets with test-only fixtures, blocks non-local requests, injects a 503 and stale evidence, verifies retry and risk masking, checks mobile containment, and clears evidence on disconnect. No trading database, public account, or production key is involved.

## Phase 3: bounded incident intelligence

This slice reconstructs investigations on authenticated reads from immutable Guardian evidence. It adds **no refresh-driven revisions, trading database access, network work or automatic remediation**. Incident grouping retains its durable cursor; an indexed, snapshot-consistent investigation reads the latest 200 linked events plus the opening anchor and at most 500 contextual events (2 MiB per scan). Context is limited to five minutes before the first valid anchor through one hour afterwards, capped at the present. Bounds and invalid clocks are disclosed. Source time and store receipt time are both shown; late delivery never becomes reversed causal ordering.

Execution/decision context requires matching lab/instance, session, symbol, timeframe and reported venue/scope. Cross-lab feed context requires an explicit shared `metadata.dependency_id` with matching venue/scope—not just coincident timestamps or the generic word "shared". A disconnect preceding linked stale evidence is a **POSSIBLE** chain, not a verified cause. Direct failure facts retain source confidence independently of causal confidence. Future/ahead-of-receipt events remain visibly clock-invalid, never causal candidates. Ordinary condition failures/`NO_SETUP` are not failure facts. Non-atomic broker/journal observations remain possible findings.

The map separates `observed_state` from `dependency_readiness`. A failed SMC feed can yield `BLOCKED_BY_DEPENDENCY` without calling the strategy defective or blocking PA/other instance venues. Missing or stale prerequisites yield `UNKNOWN`; a healthy observer probe cannot certify its subject. The graph declares separate PA, SMC and instance broker/journal prerequisites, and distinguishes SMC Lab automatic paper from independent SMC Agent approval. Agent failure is not inherited by the automatic Lab path. Which path is selected remains unverified without authoritative configuration telemetry. This is **not discovered runtime topology**, and `OBSERVED_AVAILABLE` is not execution permission or certification. `/v1/health` still describes its configured heartbeat/incident coverage; the map separately discloses missing prerequisites.

Latency baselines compare `[cutoff-24h-5m, cutoff-5m)` to `[cutoff-5m, cutoff)`, where cutoff is the current UTC minute start. Source **and receipt** must precede the cutoff; the forming minute, future events, out-of-window evidence and late imports cannot leak into a prior analysis. Only explicit `latency_ms` with a recognized `metadata.latency_kind` is eligible: `candle_processing`, `strategy_evaluation`, `paper_fill`, `api_response`, `worker_task`. Values above seven days are excluded as invalid operational measurements. Groups separate source/component/event type, lab/instance/session, strategy/version, symbol/timeframe, venue/scope, reported 40-hex commit and 64-hex configuration hash, and metric kind. These are producer-reported identities, not independent provenance certification.

At least 20 prior and 5 current samples, complete bounded scan, and reported version/configuration identity are required. A current median above `max(50 ms, 3 × baseline p95, baseline median + 6 × baseline MAD)` yields `LATENCY_DEVIATION / WATCH`. This fixed operational heuristic is not an alpha threshold or statistical-significance claim. Otherwise the state is `NO_DEVIATION_OBSERVED` or `INSUFFICIENT_EVIDENCE`, **never HEALTHY**. Read scans are capped at 5,000 events/4 MiB and 50 displayed groups. Truncation suppresses conclusions. Existing polling/outbox adapters do not yet produce all these typed metric samples; absent metrics remain unknown. Signal/evaluation frequency, rejection distributions, resource usage and uptime baselines still need authoritative complete telemetry. Anomaly reads do not emit alerts or fit/change production strategies.

The Command Center shows the map, metric coverage and investigations as read-only text. A failed refresh clears dependency/anomaly conclusions instead of leaving a cached green/normal claim. Sparse input explicitly says that no samples do not prove normal operation. Keys remain tab-memory-only and disconnect clears all evidence.

Focused regression command from the repository root: `PYTHONPATH=automation-hub:.:sdks/python python -m pytest -q tests/test_guardian_incident_intelligence.py tests/test_guardian_service.py tests/test_guardian_incidents.py`. The 82 focused tests cover closed-window boundaries, source/owner/version isolation, sparse/truncated samples, source/receipt clocks, SMC automatic-vs-Agent path isolation, context correlation, immutable replay, WAL reads, auth, structured persistence failure and retry. Browser verification uses `scripts/guardian_ui_smoke.cjs` as described above.

The app's separate `GET /guardian/lab-feeds` source endpoint requires `X-Guardian-Observer-Key`, not the Guardian read key or a trading control credential. It is not a public Guardian `/v1` endpoint.

For the authenticated endpoints, the header is `X-Guardian-Key: <the appropriate key>`. The event body is the JSON produced by `GuardianEvent.canonical_json()`. A producer must reuse its original `event_id` on retry. Its `source_service` must match the identity bound to the presented key. Raw `received_at` is store-owned and cannot be supplied by a producer.

The local WSGI server is deliberately simple and single-process. It is not yet rate-limited or hardened for public ingress; keep it on loopback. If remote ingestion is needed later, add a TLS gateway, network policy, request limits, and load tests before exposing it.

## Optional isolated Compose packaging

`Dockerfile.guardian` copies only `tradexa/__init__.py` and `tradexa/guardian/`. The trading app, bots, exchange libraries, dashboard bundles, and trading `.env` do not enter this image. The overlay is opt-in; it does not change the default deployment. It uses the same Compose network only to read `http://app:8000/status/public`. It cannot call a trading control route or see the app's environment variables or trading data volume.

For a future staging trial, create a separate absolute host directory owned by UID/GID `10001`, mode `0700`, and copy `guardian.env.example` to a protected `.env.guardian` with **independently generated** read/source keys. Export `GUARDIAN_DATA_PATH` to that absolute directory. Validate with `docker compose -p nexus-trading-bot -f compose.yaml -f compose.guardian.yaml config --quiet` before starting only the `guardian` service. Do not print the resolved Compose config: it contains keys. Use an SSH port-forward to view the loopback-only Command Center. Do not add this overlay to a production rollout until source telemetry, recovery/availability, and data-volume persistence are acceptance-tested.

The Command Center asks for the separate read key when opened. It holds that key in the current page's memory only; disconnect clears it. Do not serve this page over plain HTTP except on loopback. The page uses a restrictive Content Security Policy and writes event strings as text rather than HTML. It shows the newest 50 Guardian events and incidents, **not** lifetime counts or proof of complete telemetry. A timeline can be opened for a visible incident; this is still local Guardian evidence, not a production root-cause guarantee.

## Required next work before production use

1. Package and operate Guardian as a separate process/container with its own persistent volume and credentials. Prove trading-worker and Guardian failures are isolated in a deployment environment.
2. Decide which producer facts need a durable outbox rather than the current best-effort queue. Never call the Guardian SQLite store synchronously from trading hot paths. Critical order and journal truth must remain in their existing durable ledgers.
3. Add external-ingress hardening: TLS gateway, rate limits, version negotiation, source rotation, and load tests. Do not expose the local evidence store or service publicly as-is.
4. Add source-authoritative evidence adapters for risk gates, every evaluated candle, execution, journal writes, and persistence. The PA/SMC and Trading Instance decision outboxes cover only post-install successfully persisted material transitions; they do not capture every broker order, position, protective exit, journal failure, or prior lifecycle. The `AutoEngine` currently continues after a decision-store write failure, so its outbox cannot prove complete decision coverage. Verify cross-ledger reconciliation before claiming execution or risk integrity.
5. Expand the Command Center from its bounded decision and incident views to validated execution/risk adapters and source-specific observation age. Its current shell shows required component heartbeat age, event IDs, reasons, PA/SMC and Trading Instance decision states, and derived incident timelines. A service health response alone must not claim the trading system is healthy.
6. Test worker death, Guardian death, transport backpressure, database lock/full conditions, secret redaction, replay, stale evidence, and permissions in the actual deployment configuration. Only then consider enabling production incident/recovery workflows.

No live-routing, risk, strategy, deployment, or automatic-recovery change is part of this foundation.

## Retained fill-history milestone validation (2026-10-05)

The final complete Python suite passed **4,393 tests, 15 skipped** in 337 seconds
(95 existing deprecation warnings). This slice adds 70 cases: 67 isolated PA/SMC
fill-history integration cases and three Guardian API/startup cases. The targeted
execution/architecture/SMC-lock run passed 208 cases before the final timestamp
regressions; the final history/service run passed 97 cases. Both SMC source and
behaviour protection locks pass in the full suite. Protected SMC and PA strategy
files and freeze baselines remain unchanged from `804a7c0`.

Validation covers imports beyond the prior 128-fill sample, partial/full/reducing/
protective fills, late timestamps, 100 unchanged polls, restarts, page rollback
before/during checkpoint persistence, restart after commit, concurrent collectors,
changed account/first/last source anchors, malformed or oversized evidence,
out-of-range UTC timestamps, WAL reads, persistent locks and retry, scoped keys,
bounded transport with redirect rejection, per-lab outage isolation, source data
remaining unchanged and opt-in startup/shutdown wiring. The implementation caught
and corrected blank optional metadata rejection; it preserves that metadata as
recorded. The app's existing auth denial remains intact. No UI assets changed in
this slice, and no new browser or production acceptance result is claimed.

These results validate local code and fixtures. Retained fills remain distinct
from complete position/journal lifecycles, verified P&L, currency risk and live
execution evidence. This milestone is committed locally on
`codex/guardian-foundation`; it has not been pushed or deployed.

## Previous lab-execution milestone validation (2026-10-04)

The final repository Python suite passed **4,323 tests, 15 skipped** (327 seconds;
96 existing FastAPI/aiohttp deprecation warnings), including 58 new cases in this
lab-execution slice. Run from the repository root using absolute paths:
`PYTHONPATH="$PWD/automation-hub:$PWD:$PWD/sdks/python" /private/tmp/guardian-hub-venv/bin/python -m pytest -q`.
The initial relative-path invocation failed one fresh-interpreter startup test
because its changed working directory could not import `bot`; the correct absolute
path invocation passed without changing that test or trading code. The temporary
venv path is local validation infrastructure, not a deployment requirement.

Real isolated PA/SMC broker fixtures cover resting, partial, full, reduced and
protectively closed positions; weighted fill/quantity mismatches; session and setup
journal linkage; missing protection; legitimate trailing stops; old position origins;
bounded history/query work; WAL concurrent reads; persistent locks and retry;
checkpoint rollback; 100 unchanged polls; restart deduplication; stale/future/wrong-lab
evidence; scoped authentication and redirect rejection. A failed PA collector was
also verified not to suppress the SMC collector. The Chromium fixture run passed
separate lab account rendering, risk masking on failed/stale refresh, text-only
rendering, retry, mobile containment, clearing on disconnect and zero JavaScript errors.
Both SMC source/behaviour locks passed; protected SMC, PA and strategy directories
and protection baselines have no diff from `804a7c0`. These are **local code/fixture
results**, not VPS, real source-provider, exchange, profitability, research-method
or completed-PRD certification. Nothing in this Guardian slice has been deployed.

## Retained intent-transition milestone validation (2026-10-05)

The complete local Python suite passed **4,539 tests, 15 skipped** in 330 seconds
(95 existing deprecation warnings). The broader Guardian, execution, architecture
and SMC-lock regression run passed 623 cases. This slice adds 48 cases: 45 retained
intent integration cases and three Guardian API/startup cases. Both SMC source
and behaviour freezes pass; protected SMC/PA strategy files, baselines, Agent,
journal writer, runtime and broker implementation are unchanged in this slice.

The retained-history fixture imports 135 original events across 27 decisions in
32-row pages; restart continues from the saved checkpoint and 100 unchanged polls
append nothing. Tests cover historical link preservation after late decision
binding/current-state changes, all recorded states including legacy `RECONCILED`,
distinct market/session/execution identities, late timestamps, stale/malformed/
oversized evidence, missing parent identity, source anchor corruption/reset,
WAL concurrent reads, persistent lock and retry, scoped keys, GET-only APIs,
bounded transport and redirect rejection, opt-in startup/shutdown, page rollback
before/during checkpoint writes, post-commit restart and concurrent collectors.
Source history remains unchanged by reads; corruption injections affect only
disposable fixtures. No real broker execution is inferred from these fixtures.

Focused invocation from the repository root:
`PYTHONPATH="$PWD/automation-hub:$PWD:$PWD/sdks/python" python -m pytest -q automation-hub/tests/test_guardian_smc_intent_history.py tests/test_guardian_service.py tests/test_core_architecture.py`.
This milestone is local on `codex/guardian-foundation`, not pushed or deployed.
No dashboard, live-routing, account reset, strategy tuning or automatic recovery
was added. Recorded transition coverage is not full execution/exit/P&L validation.

## Retained SMC entry-link milestone validation (2026-10-05)

The complete local Python suite passed **4,587 tests, 15 skipped** in 332 seconds
(95 existing deprecation warnings; zero failures/errors). This slice adds 48
cases: 40 bounded evidence-link tests, four real paper-broker/Agent-journal fixture
integrations and four authenticated API/failure cases. The final targeted run
passed 121 cases; broader Guardian, crash/recovery, architecture and SMC-lock
regressions passed 644 cases. Both source and behaviour freezes pass, including
all 10 decision-path freeze and 17 agent strategy-protection tests. The protected
source files and freeze baselines have no diff from `804a7c0`; the Agent, broker,
journal writer and runtime are unchanged in this slice.

Real isolated fixtures exercise a filled entry, an accepted resting order whose
fill arrives after journal finalization, a broker fill before the intent records
its order ID, and a journal close with a synthetic protective fill that cannot
prove an originating exit link. The fixtures preserve one broker entry order and
one open position after the delayed fill; before journal finalization they retain
one order/position and zero trade journal rows without reporting MISSED or no order.
The protective-close fixture has two broker fills and zero open positions, but
Guardian exposes only the explicitly linked entry fill and recorded journal IDs;
it deliberately does not certify the exit chain. These are fixture outcomes,
not production broker/reconciliation evidence.

Tests also cover partial-fill weighted prices, duplicate IDs, conflicting keys,
markets, sessions, orders, trades and accounts, missing legacy provenance,
independent probe failure, stale/importing observations, 128-row and byte bounds,
invalid arithmetic, indexed older-history reads, missing database files, query
authentication/redaction, WAL concurrent reads, persistent locks and retry, 100
unchanged reads and Guardian-store restart with no new evidence or source writes.

Focused invocation from the repository root:
`PYTHONPATH="$PWD/automation-hub:$PWD:$PWD/sdks/python" python -m pytest -q automation-hub/tests/test_guardian_smc_entry_links.py tests/test_guardian_smc_execution_links.py tests/test_guardian_service.py tests/test_core_architecture.py`.
The full invocation uses the same absolute `PYTHONPATH` with `python -m pytest -q`.
This milestone is local on `codex/guardian-foundation`, not pushed or deployed.
Backend/API and testing skills guided scoped read access and failure coverage.
No UI, strategy, trading authority, live-routing, source-schema, account-reset,
weekly-review or automatic-recovery change was made. At that milestone, complete
durable exit and position-origin evidence remained unimplemented; the subsequent
source slice below captures post-install net-position transitions, not complete
Guardian lifecycle coverage.

## Source-side SMC paper fill-position provenance

`PaperBrokerV2` now adds nullable `v2_fills.fill_position_json` through its existing
schema migration. The shared broker schema gains the column, but **only an
SMC_LAB account with SMC_LAB execution engine captures a payload**. PA_LAB and
ordinary PAPER fills retain null. Existing fills are never reconstructed,
rewritten, compacted or deleted: null/missing-column evidence exports as
`UNVERIFIED_LEGACY_FILL`. The query-only endpoint itself never installs schema.

Capture occurs around the broker's existing `_fill()` net-position mutation and
is included in the **same existing fill INSERT and broker transaction**. There is
no extra outbox row, independent commit, Guardian write, network call, agent
approval or strategy evaluation in this path. Guardian outages cannot reach the
capture helper. The payload is canonical, finite JSON capped at 8 KiB, with no
rolling candles, quote window, heartbeat, UI state, journal prose or raw errors.
It records account/fill/order/market identity, executed side/quantity/price,
reduce-only and persisted-order flags, plus the actual before/after net-position
ID, originating entry order, available original execution key/timeframe,
side/size/weighted entry price. Parent keys come from an exact entry-order ID
lookup with matching account, engine, market and direction; missing/foreign
parents never supply an invented execution key. A synthetic protective,
remediation or liquidation fill can therefore retain the original position
identity even when that position disappears at close.

`OPEN`, `INCREASE`, `REDUCE`, `CLOSE`, `REVERSE` and `UNCHANGED` describe those
recorded before/after snapshots. They are **not entry-lot allocation, complete
order execution, stop/target history, journal reconciliation, currency/P&L or
live-exchange certification**. Capture precedes any caller's later protection
update; stop/target geometry is deliberately not part of this position projection.
Partial additions keep the broker's original net-position origin; a reversal
records both original and new position IDs. Source rows remain owner/restore
mutable, not independently verified append-only or tamper-proof evidence.

The existing rollback guard now also wraps paper `process_mark()` liquidation.
A failure after position/account mutation but before fill/order persistence rolls
back the event, preserving the prior committed position and fills. Liquidation
price calculations and order, fee, size, stop/target and RR rules are unchanged.
This is a persistence safety repair, not a new liquidation model.

`GET /guardian/smc-fill-transitions?after=0&anchor=` reads
`settings.smc_paper_db` using the separate observer key, not a trading control key
or dashboard login. It takes a query-only SQLite snapshot with 250 ms busy
timeout and 500 ms SQL deadline; each page contains at most 32 fills, plus bounded
first/previous cursor anchors. Paging uses rowid and an account-scoped material
hash rather than source timestamps, so late-dated fills remain discoverable.
Changed origin/last row, a removed cursor, or an account swap fails closed without
rewinding. Owner restore/VACUUM may invalidate the cursor and requires reviewed
recovery; the API never resets it. Payload/schema/identity/quantity validation and
secret-pattern redaction run only in this read projection, never as trading gates.
WAL readers see committed evidence during short writes. Persistent locks, missing
or malformed evidence return sanitized
`503 PERSISTENCE_BLOCKED / SMC_FILL_TRANSITIONS_UNAVAILABLE`. Successful reads use
`Cache-Control: no-store` and make no source writes. Recorded payloads are labelled
`RECORDED_SOURCE_TRANSITION`, not verified execution integrity.

The response keeps `execution_integrity_verified=false` and
`full_lifecycle_verified=false`. **No Guardian collector, new environment flag,
execution-link aggregation, dashboard, incident recovery or production rollout is
added in this slice.** Next work is the independently resumable Guardian importer
and bounded exact-ID link view, preserving legacy uncertainty and per-account
isolation. Do not enable a collector until its contract and replay tests exist.
No automatic evidence cleanup/VACUUM or active-database raw copy is introduced;
any future backup must use a consistent SQLite snapshot/online backup procedure.

## Source fill-position milestone validation (2026-10-05)

The complete local Python suite passed **4,640 tests, 15 skipped** in 324 seconds
(95 existing deprecation warnings; zero failures/errors). The final suite includes
the malformed-Unicode read/API regressions and non-SMC account export rejection.

This slice adds 53 source broker/export/API cases. The final focused source,
architecture and SMC-lock run passed 117 cases; broader Guardian, paper broker,
crash/recovery, architecture and freeze regressions passed 707 cases. Source and
behaviour locks both match the unchanged baseline (all 10 decision-path freeze
and 17 Agent protection cases). Protected source files and freeze baselines have
no diff from `804a7c0`; strategy directories and Agent/runtime/journal writers
have no diff in this milestone. Backend/API and testing skills guided bounded
authenticated exports and red/green persistence-failure coverage.

The following are **isolated fixture outcomes**, not VPS/exchange evidence.
Every provenance payload below is stored with its existing fill row, not a
separate journal revision. Counts are retained broker orders/open positions/fills.

| Injected boundary or replay | Orders | Open positions | Fills | Result |
| --- | --- | --- | --- | --- |
| Formatting, fill INSERT, post-INSERT trigger or order-update failure | 1 | 0 | 0 | Accepted entry remains open; account/position/fill changes roll back together |
| Entry retry after removing fault | 1 | 1 | 1 | One captured origin; no duplicate fill |
| Protective/remediation/liquidation fill persistence failure | 1 | 1 | 1 | Original position and committed entry provenance preserved; no transaction left open |
| Close retry after removing fault | 1 | 0 | 2 | Exit retains original position/order/decision identity |
| Duplicate quote replay after broker restart | 1 | 1 | 1 | Replayed quote rejected; original captured JSON unchanged |

Other cases cover partial long/short protective exits, additions/reductions and
new same-symbol positions, reversal with distinct origin IDs, byte-identical
restart, compatible same-account snapshot restore, legacy null/pre-column data,
foreign/missing parent links, no Guardian write/network dependency, retained
pagination beyond one page and late timestamps. Read failures cover changed or
deleted cursor origins, invalid limits, account isolation, huge numbers,
credential-like metadata, malformed/deep/oversized JSON and invalid UTF-8
identities. Authentication/control-key isolation, WAL reads during uncommitted
writes, persistent lock with sanitized 503 and retry, missing-file no-creation,
and 100 unchanged source/API refreshes preserve source history. Failure injection
found and repaired the previously missing liquidation rollback guard; an invalid
Unicode regression also proved the export must reject malformed text before
response encoding. Neither change alters strategy or fill-price mathematics.

Focused invocation from the repository root:
`PYTHONPATH="$PWD/automation-hub:$PWD:$PWD/sdks/python" python -m pytest -q automation-hub/tests/test_smc_fill_position_provenance.py automation-hub/tests/test_smc_decision_path_freeze.py automation-hub/tests/test_smc_agent_cannot_touch_the_strategy.py tests/test_core_architecture.py`.
Full validation uses the same absolute `PYTHONPATH` with `python -m pytest -q`.
This milestone is local on `codex/guardian-foundation`, not pushed or deployed.
No main, production configuration, live-routing flag, account history, strategy,
dashboard, weekly-review or autonomous-recovery change is made.

## Guardian retained SMC fill-position importer and exact-ID view

The next local slice imports the source projection above without changing its
broker capture, schema, Agent, journal writer, runtime or strategy. It is off by
default. Explicitly configuring
`GUARDIAN_SMC_FILL_POSITIONS_URL=http://app:8000/guardian/smc-fill-transitions`
starts an independent read-only collector using `GUARDIAN_LAB_OBSERVER_KEY` and
the source's `X-Guardian-Observer-Key` header. This is not `HUB_CONTROL_KEY`, an
exchange key, Guardian ingestion key or Guardian API read key. The existing
startup credential separation remains in force. No deployment config was changed.

Each poll imports at most 32 fills. Guardian validates the v1 source contract,
8 KiB material payload, aware timestamps, fresh observation, account/fill/order
identities, finite numbers, snapshot shape and before/after effect. Its transport
has a 3-second timeout, internal HTTP host/port/path allowlist, no redirects and
a 384 KiB response cap (including bounded first/previous anchor snapshots).
Malformed, oversized, stale, foreign or credential-like evidence never advances
the checkpoint. No quote, rolling candle window or journal prose is imported.

The immutable event ID hashes the versioned **account ID + fill ID**; the original
execution key remains inside its recorded position origin, not an invented
current execution-state header. One existing Guardian SQLite transaction commits
the entire event page and its account/material cursor using compare-and-swap.
An event INSERT or cursor failure rolls both back. Failure after that commit but
before the separate heartbeat retains the imported facts/checkpoint and marks the
observer unavailable. Restart resumes the same source sequence; replay cannot
skip or duplicate a fill. Conflicting same-ID payloads fail rather than overwrite.
Changed/deleted origin/predecessor, account swap or invalid cursor fails closed;
there is no automatic rewind, source rewrite, cleanup or account reset.

New Guardian read endpoints, authenticated only with `X-Guardian-Key` and
`GUARDIAN_READ_KEY`, are:

- `GET /v1/smc-fill-transitions?after=0`: pages Guardian's own retained events
  by Guardian sequence, at most 32 rows. The imported source cursor, observer
  health and page are read in one query-only Guardian snapshot.
- `GET /v1/smc-position-links?execution_key=<original-key>`: indexed exact-key
  intent/order lookup, followed by exact **account ID + original position ID**
  associations. No market/time/price proximity joins and no source DB scan.

The existing `/v1/smc-execution-links` entry-only contract is unchanged. The new
position view reads at most 128 intent rows and 128 position rows, at most 2 MiB
of unique event JSON, and rejects individual events over 16 KiB. It has a 1-second
SQL deadline and 250 ms busy timeout. Read endpoints never recreate a missing
Guardian DB or install a schema, and perform no writes. WAL reads remain
available during short writes; persistent locks return sanitized
`503 PERSISTENCE_UNAVAILABLE`. Invalid cached evidence returns sanitized
`503 FILL_POSITION_EVIDENCE_UNAVAILABLE`. Retry after lock release works without
resetting evidence. Missing/stale/failed/importing probes produce `UNKNOWN`, not
a trading-system health certificate. `CAUGHT_UP_AT_LAST_POLL` means only that
the last source page was read successfully, not that an exchange is safe.

Position-link states deliberately describe **recorded relationships**, not
verified current positions or complete trade lifecycle:

| State | Meaning |
| --- | --- |
| `EXPLICIT_POSITION_LINKS_OBSERVED` | Retained intent/order and account/position-origin IDs agree in the available records |
| `EXPLICIT_POSITION_EXIT_LINKS_OBSERVED` | Those IDs also occur on a recorded opposing reduce-only REDUCE/CLOSE fill |
| `INSUFFICIENT_EVIDENCE` | Legacy/missing intent, order or origin evidence cannot establish all requested relationships |
| `CONFLICTING_EVIDENCE` | Recorded account, session, order, trade, market, origin or reduce-only relationships contradict |
| `UNKNOWN` | Source observation is unavailable/stale/importing or bounded evidence is truncated |

A scale-in decision's exact order fill remains visible as
`EXPLICIT_ORDER_ID_OBSERVED`, but it does **not** adopt the net position's earlier
origin or certify allocation of the later exit to that decision. A reversal
retains both old/new position IDs; the old origin is not joined to later exits
of the new position. A non-reduce-only reversal is not labelled a protective exit.
Same-symbol later positions and same-position strings in another account are
not merged. `UNVERIFIED_LEGACY_FILL` stays null/unverified; history is never
reconstructed. Missing evidence is never reported as MISSED or no order placed.

All current-position, exit-verification, position/full-lifecycle, execution-
integrity, paper-account binding, net-P&L and currency certification flags remain
**false**. Retained source snapshots are not source immutability proof, stop/
target history, entry-lot allocation, current exposure or a verified journal
close. Guardian's own snapshot is atomic; different source databases are not.
Conflicts/truncation suppress observed exit conclusions. Cached rows remain
historical when source health is unknown.

Only Guardian-owned partial indexes are installed. No trading data volume or
trading dependency is added to Guardian's standalone image, and Guardian is not
called by the broker hot path. Backend/API and testing skills guided bounded
authenticated reads and crash/lock/replay tests. No UI, incident recovery,
weekly-review, alpha, trading permission, live routing or production change is
part of this local milestone.

## Fill-position importer milestone validation (2026-10-05)

The complete final local Python suite passed **4,737 tests, 15 skipped** in
305.50 seconds, with zero failures/errors and 95 deprecation warnings. No
background worker warning occurred in this final full run. Its JUnit evidence
is `/private/tmp/guardian-smc-fill-position-import-complete-suite.xml`.

This slice adds 97 regression cases: real isolated broker/Agent-journal import
fixtures, standalone contract/view/HTTP tests, and opt-in monitor/read-contract
coverage. The final narrowly focused run passed 137 cases; broader Guardian,
paper-broker, crash-boundary and protection-lock checks passed 685 cases. The
unchanged source-provenance, architecture and both SMC-lock systems passed 117
cases (including all 10 decision-path and 17 Agent protection checks).

The broader run emitted one background SMC test-worker warning in addition to
five deprecation warnings. The affected source-observer lock test passed in
isolation with only deprecation warnings; no warning filter or runtime change
was used to suppress it. This is recorded separately from importer correctness,
not treated as production feed/reconciliation evidence.

Key **temporary fixture** results (orders/open positions/fills, never VPS counts):

| Boundary or replay | Broker counts | Guardian result |
| --- | --- | --- |
| Guardian event or cursor INSERT failure | 1 / 1 / 1 | Zero imported events, zero checkpoint; retry imports one |
| Failure after page commit, before heartbeat | 1 / 1 / 1 | One retained event/checkpoint; restart imports zero duplicates |
| Synthetic protective close and 100 unchanged polls/reads after Guardian restart | 1 / 0 / 2 | Two retained events, one recorded exit association; broker/journal dumps unchanged |
| Partial fills, separate scale decision, reductions/close and later same-symbol entry | 4 / 1 / 7 | Original origin has six rows/two reduce-only exits; later position excluded and scale decision not assigned original net ownership |
| Reversal followed by new-position protective close | 2 / 0 / 3 | Old/new origin IDs separate; new exit never joined to old origin |

Other regressions cover 32/32/6 retained paging across Guardian restart, late
source timestamps, compare-and-swap races, conflicting same-ID replay, changed
source account/origin/predecessor with no reset, null legacy evidence, stale/
failed/importing probes, exact account isolation, impossible reduce-only
relationships, conflicting session/market/order/trade IDs, oversized rows and
128-row truncation, indexed older-history reads, numeric booleans/overflow/NaN,
invalid Unicode, raw credential-like fields, independent-key auth, no redirects,
missing-file no-creation, persistent SQLite lock/retry, WAL reads during short
writes, and 100 read refreshes with no evidence or cursor writes. Standalone
Guardian service imports do not load source execution/services/bot packages.

## Source-side SMC paper exit-trigger evidence (2026-10-07)

This next local slice closes one source-data gap, not the complete lifecycle
certification gap. Fill-position records establish exact position origins but
did not retain the actual exit trigger. The broker now adds nullable
`v2_fills.fill_exit_json`, schema v1 / scope `SMC_PAPER_EXIT_FILL`, for an actual
fill that reduces an opposing **SMC_LAB** net position. Both the durable account
type and execution engine must be SMC_LAB. Entry/addition fills and PA/PAPER
accounts remain null. Existing fills stay null; no history is reconstructed.

The bounded pure source formatter has no Guardian, network, journal, runtime or
strategy dependency. It records, in the **same existing fill INSERT and broker
transaction**:

- Account, fill, order, original position, entry-order and original execution IDs.
  Missing/foreign parent links leave the original execution key/timeframe unknown.
- Actual fill quantity/price, the closed portion of a netting/reversal fill and
  the raw pre-spread/slippage reference price. The new position created by a
  reversal does not adopt the old origin.
- The stored position stop/target/trailing/peak snapshot immediately before the
  fill, plus the effective stop/peak used by the existing candle trigger logic.
  A computed trailing stop is not misrepresented as the stored static stop.
- Literal order type/limit/stop/trailing fields and whether that order is
  persisted or a synthetic broker exit.
- One input observation (OHLC or bid/ask, optional identified input timestamp
  and quote ID), **not** quotes per refresh or a rolling candle window. Missing
  input time is unknown, never inferred from the fill wall clock.

Trigger kinds describe which existing broker branch produced the fill:

| Kind | Recorded meaning |
| --- | --- |
| `POSITION_STOP_LOSS` | Stored position stop triggered; includes adverse gaps |
| `POSITION_TAKE_PROFIT` | Stored target triggered, with the existing stop-first policy when both hit |
| `POSITION_TRAILING_STOP` | Candle-computed trailing stop dominated the stored static stop |
| `ORDER_TRAILING_STOP` | Explicit persisted trailing-close order triggered |
| `ORDER_REDUCE_ONLY` | Explicit opposing reduce-only order filled; not inferred to be a manual or protective stop |
| `NETTING_FILL` | Non-reduce-only opposing order reduced/reversed the original net position; not labelled protective |
| `LEGACY_POSITION_REMEDIATION` | Existing explicit reconciled-mark paper remediation |
| `PAPER_LIQUIDATION` | Existing isolated paper liquidation estimate, not Binance liquidation evidence |

No stop, target, RR, risk, entry, signal, alpha, fill-price, fee, participation,
stop-first, liquidation or trailing calculation is changed. Current broker
event/API fields are unchanged; the retained fill row gains only the nullable
JSON field. Export/restore retains captured bytes; old snapshots without the
field remain null. The source database is still owner-mutable/restorable, so
these snapshots are not a claim that source history is tamper-proof.

Serialization is capped at **8 KiB per actual exit fill**, with finite JSON
numbers and no extra table, network call or additional commit. Encoding/INSERT/
order-update failure rolls the whole existing event transaction back: fill,
position, account and tick cursor agree. There is no independently committed
evidence write after execution, and no deletion/compensation to hide a mismatch.
The strict read decoder rejects malformed/oversized/deep or duplicate-field
JSON, invalid Unicode/numbers/flags, contradictory size/direction, and incorrect
trigger relationships. This validator is not used to approve trades.

This is **source capture only**. Guardian's existing fill-position importer and
entry/position views are unchanged and do not import this new field. A bounded,
authenticated source projection, retained Guardian import and exact-ID exit
view remain the next milestone. Stop-move history, journal-close/exit IDs,
current exposure, entry-lot allocation, full lifecycle, source immutability,
account/currency binding and net-P&L certification remain unverified. No journal
write ordering, Agent approvals, strategy, trading authority, dashboard,
weekly-review, deployment configuration or VPS state is changed.

### Exit-provenance local validation

The new regression file contains **74 cases**. Focused capture/architecture/
SMC-freeze checks passed **138 cases**. The broader broker, source provenance,
Guardian import/link/service, crash-boundary, architecture and freeze run passed
**387 cases** before the final two decoder/bound regressions were added, with
five existing FastAPI/Starlette deprecation warnings and no worker warning.
Both source-level and behaviour-level SMC protections pass, and protected files
and freeze baseline remain byte-identical to the validated `804a7c0` source.

The final complete local Python suite passed **4,811 tests, 15 skipped** in
330.40 seconds, with zero failures/errors and 93 existing deprecation warnings.
No background worker warning occurred. JUnit evidence is
`/private/tmp/guardian-smc-exit-provenance-complete-suite.xml`. The full run
includes all final exit-provenance cases and both SMC protection systems.
The working branch is `codex/guardian-foundation`; this is a local-only
milestone, not pushed or deployed, and main is unchanged.

All counts below are disposable local fixtures, **not VPS evidence**. Order
counts mean persisted `v2_orders` rows; synthetic protective exits are identified
by their fill order IDs but do not create persisted order rows.

| Injected boundary or replay | Orders / open positions / fills | Evidence |
| --- | --- | --- |
| Exit encoding or INSERT failure, candle/tick/mark/remediation | 1 / 1 / 1 | Zero exit snapshots; original position/account/cursor retained; retry yields 1 / 0 / 2 and one exit snapshot |
| Explicit close formatting or order-update failure | 2 / 1 / 1 | Close order still open; retry yields 2 / 0 / 2 and one exact close-order snapshot |
| Hard process exit before fill INSERT or immediately after uncommitted INSERT | 1 / 1 / 1 after restart | Uncommitted exit rolled back; original origin retained; retry produces one exit snapshot |
| Hard process exit immediately after broker commit | 1 / 0 / 2 after restart | Existing exit bytes, fill ID and original execution key retained; no second fill on three more restarts |
| Duplicate quote + 100 post-close read/candle refreshes after restart | 1 / 0 / 2 | No additional fills/snapshots and no retained evidence rewrite |
| 100 no-trigger candles and zero-volume exit attempt | 1 / 1 / 1 | No snapshot without an actual fill |

Other regressions cover long/short stop/target precedence, tick versus candle
triggers, stored versus computed trailing stops, partial closes, explicit
trailing orders, scale-ins/reversals, market/engine/account isolation, legacy
schema and snapshot compatibility, missing origin links, invalid read contracts,
bounded formatting and failed oversized quote with unchanged tick cursor.
Paired SMC/PAPER fixtures preserve identical fill/fee/P&L numbers and account
results for candle, tick and liquidation paths. Backend/API and testing skills
guided bounded contracts and failure-first tests, without adding trading
authority or claiming complete journal reconciliation.

Focused invocation from the repository root:
`PYTHONPATH="$PWD/automation-hub:$PWD:$PWD/sdks/python" python -m pytest -q automation-hub/tests/test_guardian_smc_fill_position_import.py tests/test_guardian_smc_fill_positions.py tests/test_guardian_service.py`.
Complete validation uses the same absolute `PYTHONPATH` with `python -m pytest -q`.
Protected strategy files and freeze baselines remain byte-unchanged from
`804a7c0`; no source broker/Agent/runtime/journal/strategy writer changed from
the prior local `304ff1c` milestone. This remains a local-only read-model phase,
not a production rollout or complete execution/journal/lifecycle certification.

## Read-only SMC exit export and retained import (2026-10-07)

This local milestone builds on source-only exit capture at `5d935e6`. It adds
authenticated, bounded reads and replay-safe observation in the independent
Guardian database. It does **not** change source capture, paper execution,
strategy rules, approval modes, the SMC Agent, journal finalization or runtime
gates. No code is pushed or deployed, main is unchanged, and no live routing or
deployment environment is enabled or modified.

### Data and authority contract

- Source: `GET /guardian/smc-exit-fills?after=0&anchor=` reads the configured
  `settings.smc_paper_db` using `mode=ro`, `query_only`, a 250 ms busy timeout,
  a 0.5 s SQLite progress deadline and a single snapshot transaction. Only an
  independently configured `X-Guardian-Observer-Key` can authorize this GET;
  the control/webhook key cannot. It never constructs a broker or journal,
  installs a source schema/index, reconstructs evidence, or performs network
  or trading work. Duplicate/unknown query fields are rejected.
- The page contains **at most 32 fill rows**, ordered by source SQLite row ID,
  including rows with null exit capture. This preserves cursor coverage, not
  a count of actual exits. A captured payload is capped at 8 KiB; source
  payload reads are length-checked and prefix-bounded before JSON decoding.
  Present exit evidence must agree exactly with the existing atomic
  `fill_position_json` original position, IDs, quantity, price, flags and
  REDUCE/CLOSE/REVERSE effect. SMC account and engine identity must match.
- `RECORDED_SOURCE_EXIT` means retained exit-trigger facts passed this read
  contract. `UNVERIFIED_NO_EXIT_CAPTURE` means **unknown evidence**, including
  entry/addition and legacy fills; it is not proof of an entry, no exit, no
  order, or a missed trade. Synthetic exit order IDs stay literal; no persisted
  order, manual-close reason, input timestamp, origin or journal ID is invented.
- Source locks, missing data, changed cursor identity or malformed/contradictory
  evidence return a sanitized HTTP 503 / `PERSISTENCE_BLOCKED` with code
  `SMC_EXIT_EVIDENCE_UNAVAILABLE`. Short WAL writes remain readable. Persistent
  locks fail closed and can be retried after release, without resetting data.

The optional `GUARDIAN_SMC_EXIT_FILLS_URL` defaults **off**. When explicitly
configured to the internal `http://app:8000/guardian/smc-exit-fills` read route,
the independent collector uses the observer credential, a 3 s HTTP timeout,
no redirects and a 384 KiB response bound. Root/page/row contracts, timestamps,
numbers, IDs, exit semantics and duplicate JSON fields are validated. Source
and standalone exit decoders agree on all tested actual broker branches;
Guardian's image imports no execution, services, broker or strategy package.
No source database mount or trading credential is added to Guardian.

Each observed row has event type `smc_exit_evidence_observed` and a stable ID
derived from **account ID + fill ID**. The same fill in another account is not
deduplicated into it. Changed evidence under the same ID cannot overwrite the
immutable event. Events and the compare-and-swap checkpoint commit in one
Guardian-owned transaction. The checkpoint binds account, first row and
previous row material; changed/deleted anchor rows stop import rather than
silently reset it. It is **not** a tamper-proof certificate for intervening
source history, which remains owner-mutable/restorable.

Restart/replay uses the committed cursor: failed event/cursor writes roll back
both, and a failed heartbeat after page commit does not lose or duplicate that
page. A failing/stale observer masks freshness but retains historical facts.
Persistent Guardian persistence failure can age observation to UNKNOWN; it
cannot approve, reject, reconcile or alter a trade. This importer runs outside
the trading process and has no synchronous trading dependency.

### Guardian read surface

`GET /v1/smc-exit-fills?after=0` requires the separate `X-Guardian-Key` read
credential and returns no-store, indexed pages of at most 32 retained events,
plus the source checkpoint and an atomic Guardian-local heartbeat snapshot.
Read refreshes write no evidence or cursor and never create a missing store.
Oversized, ambiguous, unsafe or contradictory cached evidence returns sanitized
503 / `EXIT_EVIDENCE_UNAVAILABLE`; unavailable SQLite returns
`PERSISTENCE_UNAVAILABLE`. Writes to this route are not supported.

`history_state` describes the importer, **not the bot or current protection**:

| State | Meaning |
| --- | --- |
| `CAUGHT_UP_AT_LAST_POLL` | A fresh successful observation reached the source tail at that poll |
| `IMPORTING` | More source rows remain at the latest fresh poll |
| `UNKNOWN` | Missing, failed, invalid or stale observation; retained rows remain historical |

All certification flags remain false: execution integrity, full lifecycle,
source immutability, position lifecycle, journal close, current protection,
paper-account binding, currency binding and net P&L. The API does not join
exit IDs to journal outcomes or infer closure by nearby time/price. Counts of
this stream are **observed fill records**, not completed trades or actual exit
counts. Exact original entry execution/order/position IDs are retained inside
the source evidence without becoming a current runtime verdict.

### Exit-import local validation

Failure-first tests cover malformed/oversized/ambiguous evidence, independent
read credentials, replay collisions and races, partial close and reversal
origin preservation, all actual exit kinds, tick/candle/mark sources, legacy
null/pre-column data, account/engine isolation, 32-row paging, late timestamps,
missing stores, WAL reads, persistent lock/retry, import commit boundaries,
restart and 100 refreshes without source writes or duplicate events.

The focused new import/service/architecture/freeze run passed **209 tests**.
The broader Guardian/broker/provenance/crash-boundary/architecture/freeze run
passed **898 tests** in 22.14 seconds, with five existing deprecation warnings.
JUnit evidence: `/private/tmp/guardian-smc-exit-import-targeted.xml`.
The complete local Python suite passed **4,930 tests, 15 skipped**, with zero
failures/errors and 96 deprecation warnings in 359.00 seconds. No background
worker warning occurred. Complete JUnit evidence:
`/private/tmp/guardian-smc-exit-import-complete-suite.xml`.

All counts below are disposable stop-exit fixtures, **not production evidence**.
Their source broker has 1 persisted order / 0 open positions / 2 fills after
the exit. The observer never alters any of those source counts.

| Import boundary / replay | Guardian records / source checkpoint | Result |
| --- | --- | --- |
| Failed event insert or checkpoint write | 0 / 0 | Both roll back; retry imports exactly 2 rows |
| Heartbeat failure after committed page | 2 / 2 | Freshness becomes UNKNOWN; restart resumes without duplicating the page |
| Restart + 100 repeated polls/read refreshes | 2 / 2 | Zero new evidence rows and zero source writes |
| Changed/deleted cursor anchor | Prior records / prior checkpoint | Import stops; no reset, overwrite or inferred current health |

The **119 additional cases** comprise 39 paired source/import tests, 77
standalone contract/failure tests and 3 service route/opt-in-monitor cases.
The broader and complete runs include them and both unchanged SMC freezes.

Both SMC freeze systems pass. Protected strategy files and baseline remain
byte-identical to `804a7c0`; execution, Agent, runtime and journal writer files
remain unchanged from the preceding `5d935e6` capture milestone. Backend/API
and testing skills guided bounded contracts and failure-first validation, not
trading or strategy authority.

The next lifecycle slice is an exact-ID retained exit association with entry
position and journal evidence. Stop-move history, journal-close source IDs,
current exposure and full-account/currency/net-P&L proof still require their
own source evidence and acceptance tests. This read export/import milestone
does not complete the whole Guardian PRD or certify VPS operation.

## Exact-ID SMC exit associations (2026-10-07)

This local phase adds `GET /v1/smc-exit-links?execution_key=<key>` to the
independent Guardian service. It requires the separate `X-Guardian-Key` read
credential; source ingestion/control credentials cannot authorize the read.
The route is GET-only, no-store, and does not alter source writers, strategy,
approval modes, paper orders, positions, journal finalization or runtime gates.
No push, deployment, live routing or environment/configuration change is part
of this phase. Backend/API and testing skills guided bounded contracts and
failure-first tests, not additional trading authority.

### What the association proves, and what it does not

The view joins **retained facts by exact IDs only**: execution intent -> original
entry order -> original account/position identity -> exit account/fill ID ->
the matching fill-position transition. An observed origin requires an OPEN or
REVERSE transition whose new position names the same entry execution key and
order. Closing snapshots alone do not invent an entry. Exit and transition
must agree on sequence, timestamp, fill/order/market/side, quantity, actual
price, flags and the entire original position snapshot. Synthetic protective
order IDs remain literal, not persisted-order claims. Partial exits preserve
their recorded original size; a reversal's exit stays with the old execution,
not the new incoming execution. No residual/current exposure is calculated.

The four independent existing imports remain optional and off unless separately
configured: intent history, fill-position transitions, exit fills and closed
journal history. Guardian reads only its own evidence database, with no source
mount, broker construction, network access or trading credentials on this GET.
Source export/import and broker/journal/runtime/strategy files are unchanged.

**The journal has no exit-fill ID, exit-order ID or position ID.** The API can
associate a closed journal's entry order/trade ID with the intent's recorded
order/trade ID, symbol/timeframe and original direction. It cannot certify
that the journal close belongs to a particular exit. `decision_id` is not
assumed to equal `execution_key`. Intent and closed-journal origin digests use
different namespaces; equal or different hashes do not prove physical database
identity. Close time/price, same symbol or a nearby candle never establish a
relationship. All verification flags remain false, including journal close,
exit lifecycle, current execution/position/protection, full lifecycle, source
immutability, paper-account/currency binding and net P&L.

| Root `link_state` | Meaning |
| --- | --- |
| `EXPLICIT_EXIT_POSITION_LINKS_OBSERVED` | All selected exit candidates have consistent retained account, fill, original entry order/key, position and transition IDs |
| `INSUFFICIENT_EVIDENCE` | Missing intent/entry/transition/exit capture, nullable legacy origin or no observed exit; not proof that no order exists |
| `CONFLICTING_EVIDENCE` | Contradictory market/order/position/key/account/trade/source-origin facts; rows retain facts but cannot present an explicit association |
| `UNKNOWN` | Missing/failed/degraded/stale intent/position/exit observation, or truncated evidence |

`observed_exit_fill_ids` is masked on stale/failed/truncated/conflicting evidence;
historical exit payloads remain visible. Journal freshness is separately
reported, and cannot erase a valid retained broker-exit association. A stale
journal association has `observation_state=UNKNOWN`. A recorded
`EXECUTION_UNCERTAIN` remains uncertain, not COMPLETE, MISSED or "No order was
placed". `JOURNAL_EXIT_IDS_NOT_CAPTURED` is always reported. Per-row association
labels are descriptions of IDs, not reconciliation/trading authority.

### Bounded read and failure behavior

One `mode=ro`, `query_only` SQLite transaction reads events and all four
heartbeats from Guardian's own database. `guardian_snapshot_atomic=true` does
not imply `cross_database_atomic=true`. The combined unique-event budget is
128 rows / 2 MiB across **all** queries, with a 16 KiB per-event bound checked
before JSON decoding, a 250 ms busy timeout and a 1 second progress/wall
deadline. Rows retrieved through several indexes count only once. Fixed exact
partial indexes find old keys outside the recent event window; the new indexes
are installed only in Guardian's store initialization, never on a source or GET.

Every cached envelope is revalidated against its collector contract, including
schema, stable event ID, timestamp, scope, source/identity and false verification
flags. Duplicate JSON fields, unknown authority fields, unsafe credential-like
material or invalid/oversized evidence fail closed with sanitized 503
`EXIT_LINK_EVIDENCE_UNAVAILABLE`. Missing/locked/interrupted SQLite returns
503 `PERSISTENCE_UNAVAILABLE`; no missing database is created. Short WAL writes
remain readable; lock release permits retry without deletion or reset.

### Local validation

The additional tests cover exact IDs and their conflicts, unknown/missing/legacy
evidence, health masking across four probes, source contract corruption, total
row/byte budgets, fixed-index old-key reads, wall deadline, independent auth,
query validation, locks/retry and 100 concurrent-WAL read refreshes without
writes. Real disposable paper-broker/journal fixtures pass through all four
source exporters/collectors for long/short stops, targets, tick exits, trailing
stops, partial fills, explicit reductions, reversals, remediation and liquidation.
Entry/addition null exit captures are not counted as exits. Delayed journal
close is eventually observed without inferring an exit; Guardian restart and
100 reads preserve source SQL dumps, orders, positions, fills and journal rows.

The focused **89 additional cases** passed in 3.36 seconds: 75 standalone
contract/failure/read tests and 14 real-source integration cases. JUnit:
`/private/tmp/guardian-smc-exit-links-new-cases.xml`. The broader
Guardian/broker/provenance/crash-boundary/freeze run passed **949 tests** in
24.95 seconds with five deprecation warnings; the subsequent dedicated wall
deadline regression also passed. Broader JUnit:
`/private/tmp/guardian-smc-exit-links-targeted.xml`.

The complete local suite passed **5,019 tests, 15 skipped**, with zero failures
or errors, 93 deprecation warnings, successful process exit and no background
worker warning in 324.01 seconds. Complete JUnit:
`/private/tmp/guardian-smc-exit-links-complete-suite.xml`.

| Disposable source fixture | Persisted broker orders / open positions / fills | Journal trades | Read-model result |
| --- | --- | --- | --- |
| Completed stop/target/tick/trailing/mark/liquidation | 1 / 0 / 2 | 1 closed | Exact original exit IDs; journal-close proof remains false |
| Partial stop then final close | 1 / 0 / 3 | 1 closed | Two exit fills retain their original position and closed portions |
| Explicit reduce order | 2 / 0 / 2 | 1 closed | Exit linked to original entry, not the reduction decision |
| Netting reversal | 2 / 1 / 2 | 1 open | Old-origin exit retained; new position is not assigned to old execution |
| Broker fill before journal finalization | 1 / 1 / 1 | 0 | Recorded execution remains uncertain; no "no order" claim |
| Delayed journal close, Guardian restart and 100 reads | 1 / 0 / 2 unchanged | 1 closed unchanged | No duplicate evidence, source writes, orders, positions or trades |

Both SMC source and behavior freezes passed. Protected files and freeze
baselines are byte-identical to `804a7c0`; all source execution, Agent, runtime,
journal, strategy and data files are unchanged from `4f81783`. Only Guardian's
own read/store/API, the two new test files and this document changed.

This is not production evidence or completion of the whole Guardian PRD. The
next source-evidence gaps include journal-close exit IDs and stop-move history;
these are not reconstructed or silently added to source writers in this phase.

## Read-only SMC Agent stop-move history (2026-10-07)

This local milestone follows exit-ID associations at `1629867`. It exposes
existing `agent_stop_moves` records to Guardian. It does not change the Agent's
stop policy, broker protection, source journal writes, approvals, strategy,
runtime, live-routing settings or deployment configuration.

### Source contract

`GET /guardian/smc-stop-moves?after=0&anchor=` uses only the configured
`settings.smc_agent_journal_db`. Authorization requires the independent
`X-Guardian-Observer-Key`; control/webhook credentials cannot authorize it.
Duplicate/unknown query parameters are rejected. Reads use `mode=ro`,
`query_only`, a 250 ms busy timeout, a 0.5 s SQLite progress deadline and one
snapshot. Each page contains at most 32 records, ordered by source row ID, not
candle or wall time. No source table/index/migration, Agent/broker constructor,
network call or trading action occurs. Legacy databases without the table
remain unavailable; GET does not migrate them.

Fields: record/trade ID, source sequence, recorded/candle time, symbol, from/to
stop price, reason code, reported progress R and `applied`, plus reason/error
presence flags. Free-form reason/error text is not exported. Text projections
are bounded before export; malformed/unsafe evidence fails closed. Account,
session, execution, order and position identities are not invented from trade
ID, symbol, price or timestamps.

`REPORTED_APPLIED` / `REPORTED_NOT_APPLIED` describe what the existing journal
says, not independent broker confirmation. Missing/orphan history is not
classified as MISSED, no order, failed execution or unprotected exposure. An
empty retained table does not prove no stop moved. Source locks, missing
tables/files, malformed records and changed anchors return sanitized 503
`PERSISTENCE_BLOCKED / SMC_STOP_HISTORY_UNAVAILABLE`. Short WAL writes remain
readable; lock release permits retry. No deletion, VACUUM, backfill or evidence
rewrites occur.

### Independent import and read view

`GUARDIAN_SMC_STOP_MOVES_URL` defaults off. When explicitly configured to
`http://app:8000/guardian/smc-stop-moves`, its observer uses the independent
`GUARDIAN_LAB_OBSERVER_KEY`, a three-second timeout, no redirects and a
128 KiB response cap. Its own required freshness probe is added only when
enabled. The monitor stops with Guardian; observation failures do not change
trading gates or source state.

Stable event identity is SHA-256 over the stop-history namespace, first-record
origin fingerprint and original stop-record ID. Legitimate attempts with
identical trade IDs/prices remain distinct. Replay is idempotent; changed
same-ID evidence conflicts. Event append and compare-and-swap checkpoint
advancement use one Guardian-owned transaction. Post-commit heartbeat failure
retains progress, becomes UNKNOWN and resumes without duplication. Source
replacement/changed anchors fail closed; no automatic cursor reset occurs.
The namespace is not certification of source identity or immutability.

`GET /v1/smc-stop-moves?after=0` uses the separate `X-Guardian-Key` read key
and serves at most 32 retained events. It reads only Guardian's database:
read-only connection, 250 ms busy timeout, one-second progress deadline,
indexed paging, one atomic event/checkpoint/heartbeat snapshot and an 8 KiB
per-event bound. Full canonical headers/types are revalidated, including
rejecting numeric `0` instead of boolean `False`. Malformed evidence returns
sanitized 503 `STOP_HISTORY_EVIDENCE_UNAVAILABLE`; SQLite failure returns
`PERSISTENCE_UNAVAILABLE`. GET is `no-store`, cannot mutate state, and cannot
create a missing store.

`CAUGHT_UP_AT_LAST_POLL` means only that the retained page ended at the last
successful poll; `IMPORTING` means rows remain. Missing/failed/stale probes
produce `UNKNOWN` while keeping history readable. Broker application, current
protection, complete stop coverage, account/trade binding, execution integrity,
full lifecycle and source immutability remain explicitly unverified.

### Local proof and remaining boundary

Failure-first tests cover applied/failed/orphan records, late timestamps,
pagination, restart/100 reads and polls, legitimate identical attempts,
replay conflicts, compare-and-swap races, event/checkpoint/heartbeat failures,
source replacement, transport bounds, credential separation, lock/retry,
query validation, indexed WAL reads and unchanged source database dumps.

The disposable broker fixture retains exactly **1 order / 1 open position /
1 fill / 1 journal trade** before/after import. Its actual stop stays **90**
even when the journal reports a move to **100**. Guardian neither applies
that move nor certifies it; the original trade plan remains unchanged.

Focused validation: **182 passed**, including SMC source/behavior freezes and
architecture guards, `/private/tmp/guardian-smc-stop-targeted.xml`.
The final complete suite passed **5,091 tests, 15 skipped**, with zero failures
or errors, 95 deprecation warnings and successful process exit in 333.88
seconds. Evidence: `/private/tmp/guardian-smc-stop-complete-suite.xml`.
The 69 new stop-history cases plus three service/startup cases all passed.
Both SMC protection freezes passed. Protected files and both freeze manifests
are byte-unchanged from `804a7c0`; Agent/runtime/journal/execution/data writers
are unchanged from `1629867`. `git diff --check` also passed.

Exact changed files:

- `automation-hub/services/guardian_smc_stop_read_model.py`
- `automation-hub/routers/guardian_observer.py`
- `automation-hub/app.py` (GET-only observer authentication allowlist)
- `tradexa/guardian/smc_stop_moves.py`
- `tradexa/guardian/store.py` (Guardian-owned index/read page only)
- `tradexa/guardian/service.py`
- `automation-hub/tests/test_guardian_smc_stop_moves.py`
- `tests/test_guardian_service.py`
- `tests/test_core_architecture.py` (one documented read-contract dependency)
- `docs/GUARDIAN_FOUNDATION.md`

Backend/API and testing skills guided the bounded read contract and
failure-first tests. This is local-only: no push, deployment, main changes,
source writer changes or live routing.

Remaining gaps: source stop application and journal recording are separate
operations; the runtime can suppress a recording failure after a broker
operation. This observer cannot certify complete history or broker
application. Agent journal `close_trade()` has only fixture/test callers;
closed records also lack exact exit IDs. Automatic journal finalization or
changes to stop persistence require a separately scoped repair, not an
observability patch.

## Guardian-only consistent backup / recovery rehearsal (2026-10-07)

This local milestone follows stop-history observation at `4f8ee06`. The
owner-run `python -m tradexa.guardian.backup` tool does not start Guardian,
construct/migrate a store, use credentials, contact a network, run an Agent,
change trading gates, or inspect a PA/SMC/instance ledger. It is **not** a
production backup schedule, trading-account restore, service cutover or
external audit anchor.

### Consistent snapshot contract

`backup --source PATH --output NEW_PATH` opens the existing Guardian database
with SQLite `mode=ro`, `query_only`, a 250 ms busy timeout and a progress
deadline. One read transaction pins the committed source snapshot. SQLite's
online backup API copies that snapshot, including committed but
uncheckpointed WAL records. Concurrent WAL commits cannot put a newer import
cursor into a backup containing only older events; uncommitted transactions
are excluded. No source checkpoint, journal-mode change, VACUUM, retention,
backfill, rewrite or deletion occurs. A DELETE-mode source may hold off writes
during this bounded read; WAL is preferred for a running Guardian.

The entire known Guardian schema and all its present tables are included:
events, heartbeats, observer cursors/snapshot/scan state, incident history and
analysis cursor, notifications and cursor, reports, research records/reviews
and SQLite sequence state. Unknown tables/views, missing core columns or
missing/fake core immutability guards fail closed rather than silently lose
data. Older/future incompatible schemas require deliberate compatibility
review. This is SQLite/schema preservation, not semantic certification of
every event or evidence of current trading safety.

Before publication, the new private copy is converted to DELETE journal mode
**after** online backup, because backup also copies a source WAL header.
It is therefore a self-contained SQLite file, not a main file needing a
separately copied WAL. SQLite integrity/foreign-key checks, exact schema,
typed streaming logical SHA-256 and counts of every table must agree with
the pinned source. Integer, real, text, blob and NULL values retain distinct
identities. A separate file SHA-256 is returned for transport verification.
No payload text, saved exceptions, credentials or filesystem paths are
included in the structured receipt.

Default limits are 30 seconds and 4 GiB for both SQLite pages and the
streamed digest input, with a 2 MiB per-value bound. Oversize fails, never
truncates. The owner may explicitly raise the time/size limits up to 300
seconds / 64 GiB. SQL/progress/copy/hash loops check a cooperative deadline;
this is not a guarantee that a stalled filesystem syscall can be preempted.
Copying uses 128-page steps and requires estimated copy size plus a 64 MiB
free-space reserve before starting; reserve exhaustion during copy fails.
Long pinned WAL snapshots can retain incoming WAL traffic: choose a quiet
window, monitor disk space and leave headroom on the source filesystem too.

Source and output must be owner-controlled regular files/directories;
existing source files are owner-only, regular and singly linked. Output
directories must already exist, be owned by the invoking UID and have mode
0700. Nothing is automatically chmodded/chowned. A generated mode-0600
temporary file is validated and fsynced, then published with an atomic
create-if-absent hard link (not `os.replace`) and directory fsync. Existing
outputs, dangling symlinks, SQLite sidecar filenames or existing destination
sidecars are rejected. Even an output-creation race cannot overwrite history.

Only this invocation's unpublished generated temporary file is removed on
an ordinary failure. Existing/published snapshots, source DB/WAL/SHM/history
and other invocations' artifacts are never deleted. If publication succeeds
but directory sync fails, `SNAPSHOT_PUBLICATION_UNCERTAIN` preserves the
published file; do not treat it as absent. Cleanup failure reports
`SNAPSHOT_PARTIAL_RETAINED`. A hard process/host crash can leave private
`.guardian-snapshot-*.partial` artifacts, including a duplicate hard link
around publication. No automatic sweep occurs. Preserve and review those
specific files before any cleanup; incomplete files are not published
backups. This tool is not protection from the filesystem owner or host admin
altering a file; `external_authenticity_verified=false` stays explicit.

### Verification and isolated recovery

`verify --source SNAPSHOT [--expected-digest HEX]` is read-only and refuses a
live WAL database or any existing SQLite sidecar. It validates the standalone
snapshot without migrating/creating a database. Without a trusted external
digest it proves structural consistency only, not historical authenticity.

`restore --source SNAPSHOT --output NEW_PATH --expected-digest HEX` requires
the original trusted logical digest, checks it **before** copying, then
performs the same full copy/validation and no-overwrite publication. It never
replaces the service database, changes a configured path, starts a container
or creates trading records. All receipts keep
`trading_integrity_verified=false`, `external_authenticity_verified=false`
and `service_cutover_performed=false`. The original execution identities,
unknown/failed evidence and import checkpoints are retained, not reset to
HEALTHY. Reopening the disposable restored Guardian and resuming from its
checkpoint preserves event-ID replay deduplication.

Owner workflow (substitute existing **Guardian-only** paths; run as its
database-owning UID, not with trading credentials):

```sh
python -m tradexa.guardian.backup backup \
  --source /absolute/guardian/events.db \
  --output /absolute/private-backups/guardian-snapshot.db

# Keep the returned logical_sha256 receipt in an independent trusted location.
SNAPSHOT_DIGEST=THE_LOGICAL_SHA256_FROM_THE_TRUSTED_RECEIPT
python -m tradexa.guardian.backup verify \
  --source /absolute/private-backups/guardian-snapshot.db \
  --expected-digest "$SNAPSHOT_DIGEST"
python -m tradexa.guardian.backup restore \
  --source /absolute/private-backups/guardian-snapshot.db \
  --output /absolute/private-recovery/guardian-rehearsal.db \
  --expected-digest "$SNAPSHOT_DIGEST"
```

Use a new filename for each run. Do not switch the service to the rehearsal
path automatically: a restored snapshot may lag newly received evidence;
cutover requires explicit approval, downtime/replay planning and independent
deployment validation. Offline owner receipts are not inserted into the
source event store, because this operation must not mutate its snapshot.
Remote retention, encryption, externally stored integrity anchors, schedule,
RPO/RTO and VPS recovery drills still require operator policy and proof.

### Local validation

Failure-first tests exercise real uncheckpointed WAL commits, a real writer
commit during page-by-page backup, event/cursor atomicity, uncommitted writes,
all known tables, preserved uncertain/failed records, restored checkpoint
resume, repeated restoration and replay, immutability, sidecars, symlinks,
hardlinks, output races, locks/retry, corruption/digest mismatch, disk
exhaustion, byte/value/time bounds, copy/validation/sync/publication failures,
truthful uncertain publication, partial-cleanup failure and sanitized CLI
receipts. Sentinel PA/SMC/Agent/instance histories remain byte-unchanged.
No trading worker is imported and no store constructor is invoked by the tool.

The **69 new backup/recovery cases** passed. Broader targeted validation
passed **308 tests**, including both SMC protection systems and the P0
crash-boundary tests, with five deprecation warnings in 8.83 seconds.
Evidence: `/private/tmp/guardian-backup-targeted.xml`.
The complete local suite passed **5,160 tests, 15 skipped**, with zero failures
or errors and 94 deprecation warnings in 332.94 seconds. Complete evidence:
`/private/tmp/guardian-backup-complete-suite.xml`.
SMC source and behavior freezes passed. All protected decision-path files
and both freeze manifests are byte-unchanged from `804a7c0`. All strategy,
Agent/runtime/journal, execution/broker and data writers are unchanged from
`4f8ee06`. `git diff --check` passed.

Testing skill guided the failure-first recovery cases. This phase modifies
only `tradexa/guardian/backup.py`, `tests/test_guardian_backup.py` and this
document. It remains local-only: no push, deployment, main changes, production
data operation, strategy/Agent/runtime/journal/broker change or live routing.

## Guardian self-monitoring / truthful liveness (2026-10-07)

### Defect and separation of authority

Previously, each `GET /healthz` called
`record_heartbeat("guardian", "HEALTHY")`. Repeated container-health polling
could overwrite a retained FAILED heartbeat and manufacture freshness even
when the background self-monitor stopped. The regression reproduced that
write before the repair.

`/healthz` is now **HTTP-process liveness only**: 200, `self_state=ALIVE`,
`readiness_state=NOT_CHECKED`, and
`platform_state=UNKNOWN_UNTIL_EVIDENCE_CHECKED`. It performs no SQLite or
filesystem operation. A readable HTTP server can remain alive during a
persistence outage; that is deliberately not a readiness certificate.
The existing container healthcheck only checks HTTP success, so its health
means liveness, not working collectors, healthy trading or complete evidence.
Only the existing background processors write their actual heartbeat result.
Polls cannot refresh missing/stale heartbeats or erase FAILED ones.

### Bounded read contract

`GET /v1/self-health` requires the existing independent `X-Guardian-Key`
**read** credential. Source/control/research/admin credentials grant no access.
It accepts no query parameters or caller-selected filesystem path. Invalid
queries return 400, unauthorized reads 401, and mutation methods 405, without
inspecting storage. Successful diagnostics return **200 even when the reported
state is UNKNOWN/BLOCKED/FAILED**; operators must inspect the JSON, not infer
readiness from the HTTP status. Responses are `Cache-Control: no-store`.

The reader selects only the `guardian` and `guardian_*` components from the
configured required-component list, capped at 128 valid unique names. These
are retained reports from configured local monitors, not a thread/process
inventory. Other source health such as `smc_lab` is deliberately excluded
from this own-service view. Each configured monitor missing a report, older
than 90 seconds, ahead of the clock by more than five seconds, or carrying
malformed evidence is UNKNOWN. A recently successful heartbeat does not prove
the processor is still running within that freshness interval.
`guardian_storage` is reserved for the computed own-storage diagnostic and
cannot be configured as a reported monitor, avoiding a name collision that
could hide an independent heartbeat failure.

The configured **Guardian-owned** database is opened with `mode=ro`,
`query_only=ON`, a 250 ms busy timeout, a 500 ms SQL progress/deadline bound,
and one read transaction. Only bounded heartbeat fields and journal-mode
metadata are selected; no event/candle/trade payloads or full table counts
are loaded. Missing/corrupt schema, lock/deadline and unavailable files are
reported with sanitized codes, not raw SQLite exceptions. A released lock
is retried on the next read without rebuilding the database. A short WAL
write does not require this reader to acquire the writer lock.
Oversized metadata cells fail closed under a 4 KiB SQLite value/record limit.
State/timestamp values are preserved or rejected, never clipped into valid
evidence; embedded NULs cannot manufacture a HEALTHY heartbeat.

Storage metadata covers only the database and its own WAL/SHM/rollback sidecars
and the filesystem containing that path. Symlinked parents/files, hardlinks,
nonregular files, wrong-owner files and group/world-readable database/sidecars
are rejected as `UNSAFE_STORAGE_PATH`. The existing requirement remains an
owner-only database directory and stable path under the Guardian UID; this is
not an adversarial filesystem-swap proof or a scan of other ledgers/host disks.
Filesystem metadata and the SQLite snapshot are sampled separately and are
explicitly **not atomic together**. The response publishes counts, not local
paths, credentials, event prose or arbitrary heartbeat reasons; only the
documented local monitor reason codes are exposed.

### States and fixed operational thresholds

Storage HEALTHY means only that this bounded read succeeded, the database
uses WAL, and the following **observed** headroom/pressure tests pass. It is
not proof of future write durability, whole-database integrity or VPS health.

| Observation after a successful database/metadata read | Storage state / reason |
| --- | --- |
| Read-only filesystem | BLOCKED / FILESYSTEM_READ_ONLY |
| Available bytes <= 64 MiB | BLOCKED / LOW_DISK_HEADROOM |
| Reported available inodes = 0 | BLOCKED / LOW_INODE_HEADROOM |
| Available bytes > 64 MiB and <= 256 MiB | DEGRADED / LOW_DISK_HEADROOM |
| Reported available inodes 1-15 | DEGRADED / LOW_INODE_HEADROOM |
| WAL size >= 256 MiB | DEGRADED / WAL_PRESSURE |
| Non-WAL journal mode | DEGRADED / JOURNAL_MODE_NOT_WAL |
| Read and all observed headroom tests pass | HEALTHY / READABLE_WITH_OBSERVED_HEADROOM |
| File/metadata missing or invalid | UNKNOWN / STORAGE_METADATA_UNAVAILABLE or UNSAFE_STORAGE_PATH |
| SQLite busy/locked/read deadline | UNKNOWN / GUARDIAN_DB_READ_BLOCKED |
| Other unavailable/corrupt SQLite read | UNKNOWN / GUARDIAN_DB_READ_FAILED |

These byte thresholds are fixed Guardian operational warnings, not trading
parameters, percentages of a VPS plan, or a new entry-risk policy. A filesystem
that does not report inode capacity returns `filesystem_available_inodes=null`,
not a false zero. WAL pressure is **not** a corruption diagnosis. Partial
metadata remains visible during a failed read, but storage readiness stays
UNKNOWN when the reader cannot complete its bounded check.

The own-service aggregate uses FAILED > BLOCKED > DEGRADED > UNKNOWN > HEALTHY,
preserving known failures alongside missing evidence.
`/v1/health` includes the same diagnostics and a `guardian_storage` component:
overall green cannot conceal stale/failed Guardian monitors or unverified
storage. Existing upstream component states are not rewritten. An observed
storage BLOCKED is a **Guardian finding**, never proof that an exchange order
was blocked and never an automatic trading pause. Active-incident masking
remains in place. Both `automatic_action_allowed` and
`trading_integrity_verified` remain false; `write_durability_verified=false`.

There is no cleanup, checkpoint, VACUUM, database creation, retention job,
backup scheduler, recovery action, network call or source/broker mutation.
CPU/RAM, remote disks, actual dropped/queued producer events, investigation
duration, false-alert rates and production availability baselines remain
unimplemented/unverified. This is the own-health/read-only storage portion of
PRD self-monitoring, not completion of its full infrastructure scope.

### Local validation

Failure-first tests cover the false-health overwrite, missing/stale/failed
monitors, independent source states, auth/method/query boundaries, free-space
and inode thresholds, filesystem/SQL errors, locks and retry, corruption,
unsafe paths, SQL time bounds, reason redaction, a committed WAL failure and
recovery, and 100 repeated authenticated requests during a short uncommitted
WAL write. Polling creates no events or new heartbeat versions and does not
modify unrelated paper history. The broader validation also reruns Guardian
backup/recovery, service/incidents/reports/deployment contracts, both SMC
protection systems and P0 crash-boundary cases.

The **60 new regression cases** passed. Broader targeted validation passed
**263 tests**, with zero failures, skips or warnings, in 5.94 seconds.
Evidence: `/private/tmp/guardian-self-health-targeted.xml`.
After the final malformed-heartbeat/name-collision hardening, the complete
local suite passed **5,220 tests, 15 skipped**, with zero failures/errors and
95 deprecation warnings in 308.73 seconds. Complete evidence:
`/private/tmp/guardian-self-health-complete-suite.xml`.
SMC source and behavior freezes passed. All four protected decision-path
files and both freeze manifests are byte-unchanged from `804a7c0`. All strategy,
Agent/runtime/journal, data and execution/broker code is unchanged from
`4f8ee06`; app/dashboard, SDK and deployment files are unchanged from the
phase's starting commit `d64f5e2`. `git diff --check` passed.

Exact changed files:

- `tradexa/guardian/self_health.py`
- `tradexa/guardian/service.py`
- `tests/test_guardian_self_health.py`
- `tests/test_guardian_service.py`
- `docs/GUARDIAN_FOUNDATION.md`

The backend/API and testing skills guided the separate authenticated,
read-only diagnostic contract and failure-first verification. This phase is
local-only and changes only the Guardian service/diagnostic module, its tests,
and this document; no strategy, Agent, runtime, journal, broker, deployment
configuration or live-routing control is modified.

## Retained processing backlog diagnostics (2026-10-07)

### Scope and evidence gap

A fresh `guardian_incident_engine` or `guardian_notifications` heartbeat only
reports that its latest scan succeeded. The existing processors scan finite
batches, so HEALTHY does not prove their retained queues are caught up.
This phase adds read-only progress diagnostics; it does not change those
processors, their cursor writes, incident classification, notification policy,
any trading gate, strategy, execution Agent or broker.

`GET /v1/pipeline-health` requires the independent `X-Guardian-Key` read
credential. Producer, research, admin and unrelated control credentials cannot
read it. There are no accepted query parameters, caller-supplied paths or
mutation methods (401 unauthorized, 400 invalid query, 405 mutation).
Diagnostics return 200 with `Cache-Control: no-store`, including during a
reported UNKNOWN/FAILED state: JSON state, not HTTP success, is the evidence.
`/healthz` remains process liveness only and performs no persistence operation.

Two retained queues are checked in one query-only SQLite transaction:

| Component | Durable cursor | Retained queue being counted |
| --- | --- | --- |
| `guardian_incident_engine` | `guardian_analysis_cursor.incidents_v1.last_event_sequence` | Actual `events` rows after that cursor |
| `guardian_notifications` | `guardian_notification_cursor.in_app_v1.last_update_sequence` | Actual `guardian_incident_updates` rows after that cursor |

The reader counts selected rows, **never** latest sequence minus cursor.
Sequence gaps are legitimate and are not evidence of dropped or queued events.
A positive cursor must reference a retained anchor, and a cursor ahead of the
retained latest row, a missing cursor, invalid numeric type, negative cursor or
invalid retained sequence is UNKNOWN. It never resets or advances a cursor.
Notification LEFT JOINs retain queued updates whose source event/incident is
missing; those links are explicitly unverified rather than silently counted as
processed. These checks are not full-history integrity or retention proofs.

### Bounds, age semantics and states

The configured Guardian database path uses the existing owner-only,
regular-file/no-symlink/no-hardlink checks. SQLite opens `mode=ro`,
`query_only=ON`, with 250 ms busy timeout and 500 ms SQL read/progress deadline.
No persistence constructor, network call, schema creation, write test,
checkpoint, VACUUM, cleanup or payload/summary/prose deserialization occurs.
Each queue selects at most 5,000 metadata rows plus one overflow sentinel,
with a 1 MiB returned-metadata budget; SQL values/records are capped at 4 KiB.
Actual event payloads up to the existing evidence size bound remain unselected.
Returned state/timestamp cells are validated, never clipped into valid evidence.
The metadata budget is a byte estimate of selected fields, not a host-memory
or SQLite page-I/O measurement. The deadline may return UNKNOWN on a slow read.

`pending_count` is exact for a complete bounded queue read. Overflow returns
`pending_count=null`, `pending_count_lower_bound`, `truncated=true` and
`evidence_complete=false`, not a false complete count. Missing/corrupt/unsafe
storage, missing schema, busy/locked/deadline failures return sanitized UNKNOWN
diagnostics without exposing paths or exception prose. A later released lock
can be retried; reads never repair or erase the evidence.

For incidents, `first_pending_received_age_seconds` uses the actual Guardian
`events.received_at` of the **first pending row by sequence**, not the source
event timestamp, not the minimum over an unbounded history, and not network
ingestion latency. Historical source evidence newly received does not create
a false incident-processing delay. Future/malformed/naive receipt times are
unverified, not negative delays or HEALTHY evidence.

For notices, `guardian_incident_updates.observed_at` is the **original event
receipt time**, not insertion into the notification queue. It is exposed only
as `first_pending_evidence_received_age_seconds`; notification
`queue_residency_seconds=null` and no time-based notice warning is inferred.
Old backfilled incidents therefore cannot manufacture a notification-delay
alarm. A notification queue's actual row count can still show pressure.

| `processing_state` | Meaning, separate from monitor readiness |
| --- | --- |
| `CAUGHT_UP` | No retained rows after a verified cursor in this snapshot |
| `PENDING` | Some retained rows, below the fixed warning thresholds |
| `LAGGING` | At least 1,000 pending rows, an overflow, or incident first-pending receipt age >= 60 seconds |
| `UNKNOWN` | Required cursor/link/time/storage evidence cannot be verified |

These fixed thresholds are Guardian operational warnings, not trading rules
or SLAs proven on the VPS. Queue processing state is separate from the two
reported monitor heartbeats (90-second freshness bound, 5-second future
tolerance). A caught-up queue with a missing/stale heartbeat is still UNKNOWN
readiness; a reported FAILED/BLOCKED/DEGRADED monitor cannot be hidden by an
empty queue. Small pending queues may be HEALTHY **within these warning
thresholds**, but are always explicitly PENDING, not CAUGHT_UP. Known backlog
pressure is DEGRADED, not proof of source failure or automatic trading pause.

`/v1/health` includes `pipeline_health` and replaces only the two Guardian-owned
component read-model entries with their observed queue/heartbeat state. It
does not write stored heartbeats or upstream states. Aggregation preserves
FAILED > BLOCKED > DEGRADED > UNKNOWN > HEALTHY. A subsequent unavailable
pipeline read cannot erase an already reported failure. Active-incident
masking remains unchanged. Each pipeline snapshot is atomic, but the overall
health response combines separate reads and is **not** a whole-platform atomic
certificate; its pre-existing database reads can still return 503 on failure.

`producer_queue_depth`, `producer_dropped_count` and
`producer_ingestion_delay_seconds` remain null: those process-local metrics
are not durably observed by this reader. `remote_delivery_verified`,
`full_history_verified`, `trading_integrity_verified` and
`automatic_action_allowed` remain false. No backlog is treated as an SMC
signal, order approval, forced retry or reconciliation instruction.

### Local validation and changed files

The backend/API and testing skills guided the independent read-only contract
and failure-first tests. The **65 new regression cases** cover actual gapped
counts for both queues, count/age bounds, historical evidence, malformed
metadata/cursors/anchors, truncated reads, missing source links, timestamp
semantics, heartbeat masking, auth/method/query boundaries, SQL authorization
and progress time bounds, locks/retry and 100 reads during an uncommitted WAL
write. Repeated reads do not append notices/events or process/checkpoint
cursors. Restarted existing processors drain retained queues idempotently,
without reader-created incidents or duplicated notices.

Broader targeted validation passed **328 tests**, zero failures/skips/warnings,
in 5.62 seconds, including both SMC freezes, P0 crash boundaries, service,
incidents, notices/reports, deployment contract and isolated backup/recovery.
Evidence: `/private/tmp/guardian-pipeline-targeted.xml`.

The complete local suite passed **5,285 tests, 15 skipped**, zero failures/errors
and 94 deprecation warnings in 318.02 seconds. Complete evidence:
`/private/tmp/guardian-pipeline-complete-suite.xml`.
Both SMC source and behavior freezes passed. All four protected decision-path
files and both freeze manifests are byte-unchanged from `804a7c0`; all strategy,
Agent/runtime/journal, data and execution/broker code is unchanged from
`4f8ee06`. App/dashboard, SDK and deployment files are unchanged from this
phase's starting commit `b8b09e4`. `git diff --check` passed.

Exact changed files:

- `tradexa/guardian/pipeline_health.py`
- `tradexa/guardian/service.py`
- `tests/test_guardian_pipeline_health.py`
- `tests/test_guardian_service.py`
- `docs/GUARDIAN_FOUNDATION.md`

This phase remains local-only. No push, deployment, main/environment change,
source ingestion/emitter change, strategy/Agent/runtime/journal/broker change
or live-routing enablement is part of it. Producer backlog/drop telemetry,
production load/latency acceptance, full infrastructure self-monitoring and
remote delivery remain unimplemented/unverified.

## Opt-in producer transport diagnostics (2026-10-08)

### What this phase adds, and what it does not

The preceding backlog view measures only **retained Guardian rows**. It cannot
infer events dropped before receipt, emitter queue depth, or an HTTP delivery
attempt's latency. `GuardianEmitter` now offers a coherent local `diagnostics()`
snapshot and optional background publication of typed transport reports.
`GET /v1/producer-health` reads those retained reports independently.

Repository inspection found no trading runtime constructing this optional
emitter: current references are its library and tests. This phase does **not**
wire it into strategy/Agent/runtime code or enable it through environment or
deployment changes. `publish_diagnostics=False` remains the default. Therefore
no live PA/SMC/instance producer coverage or production drop-count evidence is
claimed. Missing reports remain UNKNOWN/null. The existing
`/v1/pipeline-health` producer fields remain null, since its retained queue
counts must not be confused with a particular producer's self-reported metrics.

### Process identity, atomic accounting and publication

Every emitter receives an independent UUID process epoch. Cumulative counters
start at zero for that epoch, not for a trading account, session or strategy.
A restart creates another epoch; counters are never pooled across epochs or
parallel workers sharing a source. This identity is for telemetry only and
does not change decision IDs, broker keys, orders, positions or journals.

Queue admission, removal, completion and local snapshots use one short lock,
with this invariant:

```text
enqueued = delivered + delivery_failed + queued_events + in_flight_events
```

An event sender remains outside that lock. Serialization occurs outside it,
and event admission performs no network/SQLite I/O. Queue capacity remains
bounded; full admission increments `backpressure_dropped`, invalid/post-close
admission increments `invalid`, and a failed send increments `delivery_failed`.
The existing five-key `counters()` interface is retained. A timed-out close
returns false and preserves an in-flight event, rather than pretending it
flushed; later sender completion remains accurately counted.

The local snapshot contains epoch/report sequence, uptime, capacity, actual
queued/in-flight event counts, pending age, last completed attempt latency,
accepting/worker-alive flags, counters and separate diagnostic-send failures.
It exposes no endpoint, key, error text, raw event or trade evidence. Pending
age and attempt latency use the producer's monotonic clock. The latter includes
queue waiting and HTTP-response time, including failed completed attempts;
it is **not** a network-only, exchange or verified database-ingestion latency.
Reported durations cannot predate the current process epoch.

When explicitly opted in, the existing sender thread publishes
`producer_transport_observed` through the same authenticated `/v1/events`
endpoint, initially, at a default 30-second interval and once on graceful
drain. Diagnostics bypass the source-event queue and its counters, preventing
feedback/self-amplification. They use stable event identities
`transport_<epoch>_<report-sequence>` and do not retry ambiguous sends blindly.
Diagnostic-send exceptions/failures increment their own counter and cannot
kill the event sender. HTTP 200/201 is an acknowledged telemetry attempt, not
proof that every source event reached its canonical ledger or was analyzed.

Publication interval is bounded to 1-300 seconds, queue capacity to 1-65,536,
HTTP timeout to >0 and <=30 seconds, and close timeout to 0-30 seconds.
Custom injected senders can violate the HTTP timeout contract, so a bounded
close explicitly reports when one remains stuck. No extra worker/thread,
scheduler, external destination or credential is created. HTTP redirects are
now rejected: producer credentials cannot follow a `Location` header to a
different address. This also applies to ordinary event delivery.

### Typed ingestion and authenticated read contract

Transport evidence has its own strict schema version. Required keys and
counter names must match exactly; bool-as-integer, nonfinite/oversized values,
invalid epochs, impossible accounting, capacity violations and inconsistent
age/latency are rejected. A transport report cannot claim a strategy, order,
execution or other trading identity. `/v1/events` retains existing per-source
authentication and immutability, additionally validating this known report
type before persistence. A replay of the exact event is idempotent and does
not refresh its stored receipt timestamp; conflicting/invalid reports return
422, source spoofing 403 and wrong credentials 401.

`GET /v1/producer-health` requires the independent `X-Guardian-Key` read key;
producer credentials cannot read it. There are no query parameters, supplied
paths or mutation methods (400 invalid query, 405 mutation). It returns 200,
`Cache-Control: no-store`, with truthful diagnostic states even when UNKNOWN.
This endpoint has no authority to overwrite source/component heartbeats,
change the aggregate trading health, submit orders or force a recovery.

The reader checks only configured source names (up to 128 bounded unique
identifiers). It selects at most two reports per source, each <=4 KiB, in one
query-only SQLite snapshot with 250 ms busy timeout, 500 ms progress/read
deadline and regular owner-only/non-symlink database checks. The Guardian-owned
partial index `events_producer_transport(source_service,sequence)` is created
by normal store initialization, not by GET requests; it changes no trading
schema or existing event content. No full count, whole-history scan, source
network request, cleanup, checkpoint, retention or automatic repair runs.

| Latest observed process report | Diagnostic state |
| --- | --- |
| No report, stale/future/invalid clock, malformed evidence or unavailable storage | UNKNOWN; current metrics null |
| Producer reports stopped/not accepting or worker not alive | BLOCKED, for this producer transport only |
| Epoch cumulative invalid/drop/event-send/report-send failures observed | DEGRADED; retained loss is not silently cleared |
| Queue full or oldest pending event age >=60 seconds | DEGRADED / PRODUCER_BACKLOG_PRESSURE |
| Fresh coherent report without observed loss/pressure | HEALTHY / LATEST_PROCESS_REPORT_WITHIN_WARNING_THRESHOLDS |

Both Guardian receipt and producer report timestamps must be within 90 seconds
(5-second future tolerance). This is a freshness filter, **not synchronized
clock certification**. The adjacent reports of the same epoch must progress
in sequence/uptime/cumulative counters and keep capacity constant; regression
is UNKNOWN. `counter_history_verified` only checks that adjacent pair; it does
not verify all reports. Across epochs no monotonic counter comparison is
invented. Only the latest observed process is shown; this is not an expected
worker inventory, nor proof that an older/parallel process stopped.

`producer_inventory_verified`, `source_clock_verified`,
`all_events_delivered_verified`, `full_history_verified`,
`trading_integrity_verified` and `automatic_action_allowed` remain false.
`network_ingestion_delay_ms` remains null. Missing/stale metrics must not be
filled with zero or promoted to whole-source/platform HEALTHY.
Reports stay as immutable Guardian evidence. They do not generate an incident
merely because a telemetry counter changed, or alter existing notification
policy. Production workload/retention acceptance remains required before
choosing a report interval for real sources; no history is deleted here.

### Local validation

Backend/API and testing skills guided the source/read authority separation,
versioned contract and failure-first tests. The **70 new regression cases**
cover restart epochs, coherent concurrent queue snapshots, blocked senders,
backpressure/loss accounting, post-close admission, bounded configuration,
diagnostic-send failure isolation, redirect credential protection, clocks,
regression/shape/duration validation, read/write authentication, immutable
replay, bounded query-only reads, partial-index use, WAL snapshots, storage
lock/retry, unsafe/corrupt/missing storage and read time limits.

Broader targeted validation passed **398 tests**, zero failures/skips/warnings,
in 6.37 seconds, including both SMC freezes, P0 crash boundaries, service,
incidents/reports, retained queues, backup/recovery and deployment contracts.
Evidence: `/private/tmp/guardian-transport-targeted.xml`.

The complete suite passed: **5,355 passed, 15 skipped, 95 warnings**, no
failures/errors, in **333.00 seconds**. Evidence:
`/private/tmp/guardian-transport-complete-suite.xml`. Both SMC source and
behaviour freezes passed in the targeted and complete runs; the protection
modules contributed 27 passing cases in each. The reported warnings are
dependency/FastAPI deprecations, not transport test failures.

Protected SMC files and both freeze baselines remain byte-identical to
`804a7c0`. All trading backend/dashboard, broker, SDK and deployment files
remain unchanged from the starting `ae7ef0d` checkout. Canonical services,
strategies, data/execution and broker files also remain unchanged from
`4f8ee06`. `git diff --check` passed. No trading strategy was retuned.

Exact changed files:

- `tradexa/guardian/emitter.py`
- `tradexa/guardian/transport_health.py`
- `tradexa/guardian/service.py`
- `tradexa/guardian/store.py`
- `tests/test_guardian_transport_health.py`
- `docs/GUARDIAN_FOUNDATION.md`

Local-only: no push, deployment, environment/main changes, native producer
activation, strategy/Agent/runtime/journal/broker edits or live-routing change.
This finishes the optional transport contract and local verification portion,
not production producer telemetry or the entire Guardian PRD.

## Bounded HTTP ingestion admission (2026-10-08)

The standalone Guardian service now admits authenticated `/v1/events` and
`/v1/heartbeats` writes through separate per-process token/capacity budgets.
Global **per route class** and per-configured-source limits are both enforced
atomically under a short local lock. No body parsing, network or persistence
runs while that lock is held. Admission does not wait or create a queue.
Unconfigured sources cannot allocate buckets; the inventory is bounded to 128
valid unique names. Existing independent source/read/research/admin credentials
retain their authority.

Unauthorized requests remain 401 and do not read a body or spend a source's
budget. Authenticated overload is rejected **before body/database work** with
429 `INGESTION_LIMITED`, allowlisted `reasons`, `retry_after_seconds`,
`request_persisted=false` and a `Retry-After` header. Reasons are
`GLOBAL_RATE_LIMIT`, `SOURCE_RATE_LIMIT` or `WRITE_CAPACITY_FULL`; rate reasons
can coexist. `request_persisted=false` refers to this rejected request only,
**not** proof that a prior retry's event/order does not exist. `Retry-After` is
guidance to try again, not a guarantee that capacity will be free.

Events and heartbeats have independent budgets/capacity, so an event flood
does not itself spend heartbeat admission. Reads, `/healthz` and diagnostics
do not spend either budget. This does **not** guarantee read/heartbeat latency
when the underlying single-process WSGI server, SQLite lock, host or gateway
is unavailable; deployment acceptance and request/connection timeouts remain.

An admitted request spends a token even if its body is malformed, its source
identity is spoofed or persistence fails. Its capacity lease is always released
on validation/persistence/response failure. Failed requests are not refunded
into a flood loop. Denied requests do not spend tokens. Monotonic refill is
capped at the configured burst; a backwards clock cannot grant capacity.
No changes to immutable event IDs/content, stored receipt timestamps or replay
semantics are made. A committed append followed by a lost HTTP acknowledgement
retries as `ALREADY_PRESENT`, without a duplicate or freshness rewrite.

### Protective defaults and configuration

These are **local protective defaults**, not measured workload/latency SLAs.
Environment overrides use the exact prefix `GUARDIAN_INGESTION_` plus the
following suffixes. They are strictly positive integer strings; unknown prefix
keys, zero/disable values, invalid ranges and inconsistent source/global limits
fail startup **before store creation or thread/server startup**.

| Suffix | Default | Meaning |
| --- | --- | --- |
| `EVENT_RATE` | 60 | Global event requests/second |
| `EVENT_BURST` | 256 | Global event token capacity |
| `EVENT_SOURCE_RATE` | 20 | Each configured source's event requests/second |
| `EVENT_SOURCE_BURST` | 128 | Each source's event token capacity |
| `EVENT_INFLIGHT` | 4 | Concurrent admitted event requests |
| `HEARTBEAT_RATE` | 10 | Global heartbeat requests/second |
| `HEARTBEAT_BURST` | 32 | Global heartbeat token capacity |
| `HEARTBEAT_SOURCE_RATE` | 2 | Each source's heartbeat requests/second |
| `HEARTBEAT_SOURCE_BURST` | 8 | Each source's heartbeat token capacity |
| `HEARTBEAT_INFLIGHT` | 1 | Concurrent admitted heartbeat requests |

Rates are bounded to 1-1000/second, bursts to 1-2000 and concurrent leases to
1-16 per route class. Source rate/burst cannot exceed the corresponding global
rate/burst. No active environment or deployment configuration changed here.
No producer is enabled and no trading hot-path dependency is introduced.

This limits **HTTP producer admission**, not all Guardian writes: in-process
collectors, analysis/reports/research and heartbeats written internally remain
outside these budgets. It is not a byte-rate limit, disk-retention policy,
unauthenticated DoS defense, TLS gateway, shared multi-process quota or durable
critical-event outbox. Existing HTTP body-size validation remains in force for
admitted requests. Keep the service on loopback; public exposure is not approved.
Best-effort emitters count a 429 as a failed delivery, never an acknowledged
event, and do not gain retry/trading authority from this change.

### Admission diagnostics and validation

Read-key-only `GET /v1/ingestion-health` returns a no-store, bounded in-memory
snapshot: process epoch/uptime, numeric route/source limits, available tokens,
admitted/rate-limited/capacity-limited requests and current leases. There are no
paths, keys, errors, raw bodies or trading payloads. No query/reset/mutation is
accepted (400/405); unauthorized readers receive 401. This endpoint never opens
SQLite or overwrites platform/component health. Its `HEALTHY`/`DEGRADED` status
is scoped to local HTTP admission. Admitted request counts are **not** counts of
persisted/verified events. Cumulative overload remains visible for the epoch.

Counters are explicitly volatile and restart under a new epoch; existing
Guardian evidence is preserved. `history_persistent`, `persistence_verified`,
`producer_coverage_verified`, `trading_integrity_verified` and
`automatic_action_allowed` remain false. There is no certified producer
inventory, complete history, broker fill, recovery or trading permission.

Backend/API and testing skills guided strict admission/error contracts and
failure-first tests. Regression coverage includes quotas, concurrent requests,
heartbeats/read isolation, clocks/configuration, sanitized persistence failure,
capacity release, lost acknowledgement, immutable retry, restarts and startup
fail-closed behavior. Local validation: **483 passed** in the targeted suite;
the complete suite finished with **5,440 passed, 15 skipped, 94 warnings**
(dependency/FastAPI deprecations), with zero failures or errors. Both SMC source
and behavior protection checks passed; the protected strategy files and freeze
manifests remain unchanged. `git diff --check` passed.

Files changed in this admission phase: `tradexa/guardian/ingestion.py`,
`tradexa/guardian/service.py`, `tests/test_guardian_ingestion_admission.py`,
`docs/GUARDIAN_FOUNDATION.md` and `docs/GUARDIAN_REMAINING_WORK.md`.

This is local Guardian-only work: no trading strategy/Agent/runtime/broker or
source ledger edits, main/environment change, push, deployment or live routing.
For the remaining **eight major workstreams**, see
[GUARDIAN_REMAINING_WORK.md](GUARDIAN_REMAINING_WORK.md). They are not eight
remaining commits; several need owner-selected external integrations and
production acceptance.

## PRD completion boundary

Still required before calling the whole Guardian PRD complete: validated every-evaluation/source-version coverage (including failed persistence), complete PA/SMC and all-instance execution/journal/exit lifecycles beyond bounded current snapshots, currency-verified isolated risk/correlation, infrastructure and other-agent telemetry, production typed latency samples and frequency/distribution/resource baselines, runtime-verified dependencies and source-proven causal/recovery chains, actual isolated causal research runners and statistical tests, a bounded model/provider integration, explicitly approved operational-recovery targets, remote notification delivery, the one-item authenticated trading-app integration, load/retention/backups, and deployment fault/availability acceptance. Local read models, reported research results and green unit tests cannot substitute for any of these proofs. No credentials, remote destination, recovery policy, model provider or production deployment is inferred from the PRD.
