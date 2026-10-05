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

- Optional `GET /guardian/smc-execution` uses the same independent observer key to read the SMC Agent intent database and isolated SMC paper broker database with query-only SQLite connections. It observes all outstanding intents (at most 64; more fails closed) plus the eight most recent complete and eight most recent failed intents. It matches recorded order IDs, can discover a committed entry order by its stable execution key even when the intent did not record the order ID, and checks linked journal trade and open-position ownership. It never submits, cancels, fills, or reconciles an order. `GUARDIAN_SMC_EXECUTION_URL=http://app:8000/guardian/smc-execution` enables an independent poller that saves deduplicated observations in Guardian's database. Its probe health means only that this read succeeded, **not** that SMC trading is healthy or execution integrity is proven.
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

Configuration requires `GUARDIAN_DB_PATH`, `GUARDIAN_SOURCE_KEYS_JSON` (a JSON object mapping each source service to its own long random key), and a distinct `GUARDIAN_READ_KEY`. Optional settings: `GUARDIAN_REQUIRED_COMPONENTS`, `GUARDIAN_BIND_HOST`, `GUARDIAN_PORT`, `GUARDIAN_PUBLIC_STATUS_URL`, `GUARDIAN_LAB_OBSERVER_URL`, `GUARDIAN_LAB_BACKFILL_URL`, `GUARDIAN_LAB_LIFECYCLE_URL`, `GUARDIAN_INSTANCE_DECISION_URL`, `GUARDIAN_INSTANCE_LEDGER_URL`, `GUARDIAN_LAB_EXECUTION_URL`, `GUARDIAN_LAB_FILL_HISTORY_URL`, `GUARDIAN_LAB_FEED_URL`, `GUARDIAN_SMC_EXECUTION_URL`, and `GUARDIAN_LAB_OBSERVER_KEY`. The service rejects reuse of `HUB_CONTROL_KEY` when it is present in its environment and refuses a lab observer key shared with its read/source keys. Keep the database in a dedicated owner-only directory and supply secrets through a protected environment mechanism, not source control or shell history. With the public or lab collectors enabled, their own probes are required for overall health; `pa_lab` and `smc_lab` remain `UNKNOWN` until independent lab-health telemetry exists.

| Method | Path | Authority | Result |
| --- | --- | --- | --- |
| GET | `/` and `/assets/command-center.*` | local shell only | static read-only Command Center; no evidence or key embedded |
| GET | `/healthz` | local liveness probe | `self_state`, deliberately no claim that trading is healthy |
| POST | `/v1/events` | source-specific `X-Guardian-Key` | 201 appended, 200 identical replay, 403 source mismatch, 422 invalid evidence, 503 persistence unavailable |
| POST | `/v1/heartbeats` | source-specific `X-Guardian-Key` | current heartbeat for that source or a source-prefixed component |
| GET | `/v1/events?limit=50&source_service=smc_lab` | separate read key | paged recent immutable evidence |
| GET | `/v1/health` | separate read key | evidence-backed component health, with missing/stale components `UNKNOWN` |
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

## PRD completion boundary

Still required before calling the whole Guardian PRD complete: validated every-evaluation/source-version coverage (including failed persistence), complete PA/SMC and all-instance execution/journal/exit lifecycles beyond bounded current snapshots, currency-verified isolated risk/correlation, infrastructure and other-agent telemetry, production typed latency samples and frequency/distribution/resource baselines, runtime-verified dependencies and source-proven causal/recovery chains, actual isolated causal research runners and statistical tests, a bounded model/provider integration, explicitly approved operational-recovery targets, remote notification delivery, the one-item authenticated trading-app integration, load/retention/backups, and deployment fault/availability acceptance. Local read models, reported research results and green unit tests cannot substitute for any of these proofs. No credentials, remote destination, recovery policy, model provider or production deployment is inferred from the PRD.
