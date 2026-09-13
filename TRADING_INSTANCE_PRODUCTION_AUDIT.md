# Trading Instance production audit — phase 2, 2026-09-13

Covers the additional production requirements: user isolation, concurrency,
execution ownership, database constraints, order idempotency, reconciliation,
paper-execution realism, exchange rules, clocks, candle integrity,
backpressure, rate limits, warm-up, configuration versioning, secrets,
observability, soak testing and regression coverage.

Same rules as phase 1: forward-paper only, no exchange routing enabled, no risk
control relaxed, no strategy logic changed, no history reset.

The environment caveat from phase 1 still applies — this container's egress
policy denies `fstream.binance.com`, so runtime proofs use a Binance-protocol
double at the socket boundary only.

---

## Findings, severity ranked

### P0-1 — Nothing prevented two processes trading the same instance

Two containers on one database would each claim the same instance, and each
would run a worker for it. Every order, fill, position and journal entry that
instance produced would be duplicated. There was no lease, no ownership record,
and no check: `start()` looked only at in-process state.

Proven before the fix — two managers over one SQLite file both started the same
instance. After the fix:

```
process A owns: {'worker_id': 'vm:1428:5ca0b4d2', 'process_id': 1428,
                 'host': 'vm', 'this_process': True, 'expired': False}
process B start -> REFUSED: instance c77d2d78... is already owned by worker
                   vm:1428:5ca0b4d2 (pid 1428 on vm) until 2026-...
after A stops, lease held = False
process B start after release -> OK, this_process = True
```

### P0-2 — A Start racing a Stop reported success and did nothing

`stop()` and `pause()` read their runtime under the state lock, then released
it before the slow part (joining the worker thread) and before persisting the
result. A Start landing in that window found an engine that still reported
running and returned it as a success.

```
Start returned to the caller : state=running desired_running=True
Instance actually ends as    : state=stopped desired_running=False
Worker running               : False
SILENTLY SWALLOWED START: True
```

The operator pressed Start, got a 200 with a healthy-looking instance, and
nothing was running.

The state lock could not simply be held across the transition: the engine
thread takes it from its own lifecycle callback, so holding it while joining
that thread deadlocks. Each instance now has its own lifecycle mutex, separate
from the state lock and never touched by the engine thread.

### P0-3 — Instances had no owner

`trading_instances` carried no `user_id`/`owner_id`, and no instance-scoped
endpoint verified ownership. The platform is single-owner *today* only because
`app.py` 403s a non-admin Supabase user from every instance path
(`app.py:325-327`). The moment that restriction or `HUB_MULTI_USER` changes, an
`instance_id` — a guessable hex string — is the only thing between one account's
session and another account's running worker, paper balance and trade history.

### P1-4 — Order idempotency was application code only

`webhook_events.alert_id` had a plain index, not a unique one. Autonomous alert
ids are already deterministic (`auto:<instance>:<symbol>:<timeframe>:<candle
open>:<action>`), and `DuplicateGuard` checks them — but a read-then-insert
under two threads, or a candle replayed after a reconnect while the first write
was still in flight, could pass the check twice.

### P1-5 — The paper engine never checked it could afford an order

```
account balance $10, order 100 BTC @ $60,000
  -> accepted, filled, available capital: -6,004,190.36
```

The sizing pipeline normally prevents this, but the execution engine had no
check of its own, so any path around sizing could overdraw arbitrarily.

### P1-6 — No reconciliation after restart

The runtime rebuilt itself from the database and trusted it. An open position
with no trade row, a trade row with no position, a quarantined intent, a lease
held elsewhere, or rows carrying the wrong `instance_id` all restored as
"healthy" and the strategy traded on top of them.

### P1-7 — Reconnect backoff had no jitter

`sleep(min(2 ** attempt, 30))` with no randomisation. Three instances is nine
channels; after a Binance blip they retry on the same tick, for as long as the
outage lasts. That is how a recovering venue turns into a rate-limit ban.

### P1-8 — Unbounded queues

`_Consumer.pending` was an unbounded `OrderedDict` and the notify pool an
unbounded work queue. A sink that stopped keeping up grew memory silently.

### P2-9 — Configuration edits were invisible to the running worker

Editing risk or position caps applied live; editing strategy or timeframe
rebuilt the worker. But nothing recorded *which* configuration a running worker
was built from, so the dashboard could show settings the worker had not adopted.

### P2-10 — `/instances/runtime/metrics` was unreachable

Introduced in this phase and caught by its own smoke test:
`/instances/{instance_id}/metrics` was registered first, so FastAPI captured
the literal `runtime` as an instance id and answered 404.

### P2-11 — A migration-ordering bug, introduced and caught here

Adding `CREATE INDEX ... ON trading_instances(owner_id, ...)` to the schema
script made **every existing database fail to open**: `executescript()` runs
before the `ensure_column` pass that adds the column. The test suite caught it
immediately. Indexes over migrated columns now live with the migration.

### Cleared — audited and found sound

| Area | Verdict |
|---|---|
| **Clocks** | Every runtime timestamp is `datetime.now(timezone.utc)`. Zero occurrences of `datetime.now()` or `utcnow()` in `services/`, `execution/`, `data/` — now enforced by a test. Candle freshness is measured from the candle's **close**, not its open, so a just-closed 5m candle reads 0s old. |
| **Exchange rules** | `tickSize`, `stepSize`, `minQty`, `minNotional`, precision all read live from `/fapi/v1/exchangeInfo`, cached 1h, filtered on `status == "TRADING"` and `contractType == "PERPETUAL"`. Nothing numeric is hardcoded. |
| **Candle integrity** | Forming candles removed before any closed-candle strategy sees them; gaps raise rather than being stepped over; duplicates and out-of-order bars are counted and ignored; HTF is native, and `htf_bias` fail-closes to neutral rather than resampling LTF. |
| **Secrets** | `.env` and `.env.*` are gitignored and untracked. No secret is returned in any response or written to a log. Live routing is a stub that raises `NotImplementedError` and is not wired to instances at all. |
| **Warm-up** | Each strategy declares `warmup_required`; the engine refuses to leave warm-up until the provider has supplied that many valid closed candles from the durable cursor, and fails closed rather than replaying or guessing. |
| **Concurrent starts** | Already idempotent within a process — four concurrent Starts produced one worker. |
| **Delete while running** | Already refused. |

---

## User / account isolation

| Surface | Before | Now |
|---|---|---|
| `trading_instances` ownership | no column | `owner_id`, default `__owner__` |
| Instance lookup | `_instances[id]` | `instance_for(id, owner_id)`; mismatch raises `KeyError` |
| List / snapshot | all instances | `snapshot(owner_id=...)` |
| `GET/PATCH/DELETE /instances/{id}` and all sub-routes | id only | ownership verified, **404** on mismatch |
| `POST /instances/{id}/{action}` | id only | ownership verified |
| Paper account, orders, trades, journal, positions | already `instance_id`-scoped | unchanged, now owner-scoped above |
| Worker | no ownership record | lease with `worker_id`/`process_id`/`host` |

404 rather than 403 is deliberate: a different answer for "exists but is not
yours" would let one account enumerate another's instance ids.

**Behaviour is unchanged while `HUB_MULTI_USER` is off** — `_owner()` returns
the owner tenant for everyone, every existing instance carries it, and an admin
sees exactly what they saw before. Deliberately *not* wired to `app._tenant()`,
which returns the Supabase UUID even while multi-user is off and would re-home
every existing instance. The tenancy switch stays the one thing that changes
this.

---

## One instance = one execution owner

```
instance_worker_leases
  instance_id PK -> trading_instances(id) ON DELETE CASCADE
  worker_id, process_id, host, started_at, heartbeat_at, lease_expires_at
```

* Claimed in `start()` before any worker or feed is built.
* Renewed by the supervisor on every sweep; a worker that **loses** its lease
  stops itself rather than becoming a second execution owner.
* Released on stop, delete, and graceful shutdown — so a clean restart hands
  over immediately.
* After a crash the successor waits out the TTL (default 120s,
  `HUB_WORKER_LEASE_TTL_SECONDS`), because a crashed process might still be
  running. Restoration reports `blocked` with `WORKER_LEASE_HELD` and the
  supervisor retries.
* An unreadable expiry **fails closed** — unknown never means "not expired".
* Exposed via `worker_ownership()`, `/instances/{id}/status` and
  `/instances/runtime/health`.

---

## Database constraints

| Constraint | Purpose |
|---|---|
| `UNIQUE(alert_id, instance_id, status)` on `webhook_events` | order idempotency. Status is in the key because one order legitimately moves through pending → accepted; what must never repeat is the same stage for the same deterministic key |
| `instance_worker_leases.instance_id` PK + FK ON DELETE CASCADE | one execution owner; no orphan lease |
| `idx_positions_instance (instance_id, status)` | the hot status read (SQLite was missing what Supabase already had) |
| `idx_paper_trades_instance (instance_id, simulation_session_id)` | ditto |
| `idx_webhook_instance (instance_id, received_at)` | ditto |
| `idx_instance_owner (owner_id, created_at)` | owner-scoped listing |
| already present | `paper_executions.execution_id` PK (the duplicate-fill guard), `UNIQUE(instance_id, session_number)` on `simulation_sessions`, FKs from `instance_market_state`/`instance_metrics`/`instance_engine_logs` |

A database that already contains duplicates cannot take the unique index. That
is **reported, not swallowed**: `ledger.duplicate_order_constraint` records
`enforced: false` with sample offending rows, and reconciliation raises it as a
warning so an operator knows the durable guarantee is absent.

---

## Order idempotency and correlation

```
signal      strategy emits at candle close
   ↓        alert_id = auto:<instance_id>:<symbol>:<timeframe>:<candle_open>:<action>
decision    decision_store, keyed <instance>:<version>:<symbol>:<timeframe>:<candle>
   ↓
order intent  parked with alert_id + decision_timestamp + sizing_context
   ↓          DB: webhook_events(alert_id, instance_id, status='pending')  [UNIQUE]
paper order   filled only by a Binance quote strictly later than the decision
   ↓          execution_id = paper:<action>:<alert_id>
fill          webhook_events(<alert_id>:fill:<quote timestamp>, 'accepted')  [UNIQUE]
   ↓          paper_executions(execution_id)  [PK — the duplicate-fill guard]
position      positions(instance_id, simulation_session_id)
   ↓
journal       journal_context carries instance_id, session, strategy, version,
              exchange, instrument, fill model, candle_id
```

Every stage carries the instance and the candle. A replayed candle produces the
same key at every stage, and the constraint — not a check — makes the second
write fail. `DuplicateOrderIntent` is raised, and the pipeline reports it as
`dedup`, never as an error and never as a second order.

---

## Reconciliation

Runs at startup (with `expect_worker=False`) and on demand via
`GET /instances/{id}/reconciliation`. Findings are advisory in one direction
only — a discrepancy blocks; nothing here repairs, because a reconciliation
that silently fixed its own findings would hide the thing it exists to surface.

| Check | Detects |
|---|---|
| `instance_exists` | worker running for a deleted instance |
| `ledger_readable` | a database that cannot answer is not "healthy" |
| `position_without_trade` | open position with no trade row |
| `trade_without_position` | open trade row with no position |
| `worker_matches_state` | durable state says RUNNING, no worker exists |
| `worker_lease_readable` / `single_execution_owner` | ambiguous or foreign ownership |
| `pending_orders_readable` / `intent_ownership` | quarantined intents |
| `records_carry_instance_id` | cross-instance contamination |
| `order_idempotency_constraint` | the unique index is not installed (warning) |

A blocked instance reports `runtime_status: BLOCKED`, `execution_status:
DISABLED`, keeps its restart intent, and is **not** retried by the supervisor.

---

## Paper execution realism

| Mechanic | Modelled | Detail |
|---|---|---|
| Bid/ask spread | yes | buys fill above, sells below; half-spread + slippage + latency drift |
| Next-quote fill rule | yes | an entry is an **intent**; it fills only on a quote strictly later than the decision timestamp, at that quote's price |
| Maker vs taker | yes | a resting limit fills AT the limit and pays the maker fee |
| Fees | yes | charged per side; `realized = gross − fees`, verified by test |
| Slippage | yes | configurable, deterministic per `execution_id` |
| Partial fills | supported | `partial_fill_prob` (default 0) |
| Order rejection | yes | `reject_prob`, plus the venue-rule and balance gates |
| Insufficient balance | **added** | notional > available capital is rejected |
| minQty / stepSize / tickSize / minNotional | yes | live from `exchangeInfo`, enforced in the pipeline (`GATE_REJECTED: VENUE_RULES`) |
| Stop-loss / take-profit | yes, **on closed candles** | evaluated against the candle's low/high; a gap-through fills at the open, otherwise at the stop level |
| Leverage | **no** | reported as `None`; the account is modelled unleveraged |
| Margin / liquidation | **no** | not modelled |
| Funding | **no** | funding rate is carried in the quote but not accrued |

The last three are gaps, not bugs, and are now pinned by a test so they stay
visible rather than becoming assumptions. **The stop asymmetry is worth your
attention:** entries fill on quotes (sub-second), exits on candle close (up to
5 minutes later), and the exit price used is the stop level itself rather than
a worse price. Against real markets that is *optimistic* for stops. Changing it
would change measured strategy performance, which was explicitly out of scope,
so it is reported rather than altered.

---

## Backpressure and rate limits

* Candle backlog capped at `MAX_PENDING_CANDLES = 256`. Candles are **never
  dropped** — a strategy evaluating a gap it never detected is worse than a
  late candle — so the consumer is reported unreliable instead, which already
  closes its entry gate. `backlog_exceeded` is tracked separately from
  `last_error`, which the next delivery attempt overwrites.
* Quote queue capped at `MAX_PENDING_QUOTES = 512`, and quotes past it **are**
  dropped and counted: a quote is only meaningful at the price it carried.
* Websocket read queue was already bounded (`max_queue=2048`).
* Queue depth, peak depth, quote-queue depth and dropped counts are exposed
  per instance and aggregated platform-wide.
* Reconnect backoff now uses full jitter within the same exponential window.
* REST warm-up is paged and stops at the live edge; the hub shares one channel
  per `(symbol, timeframe)` so instance count does not multiply REST calls.

---

## Observability

`GET /instances/runtime/metrics`:

```
active_instances  running_workers  desired_workers  blocked_instances
market_connections  active_subscriptions  reconnect_count  stale_feed_count
strategy_evaluation_count  signals_generated  orders_generated  orders_rejected
duplicate_candles  missing_candles  queue_depth  dropped_quotes
consumers_behind_the_feed  max_market_message_age_seconds
process{rss_mb, threads, pid}  supervisor{...}  market_data_channels[...]
instances[...]  (per-instance, including processing_latency_seconds)
```

The counter that matters most is `strategy_evaluation_count`. A strategy that
looked at 288 candles and found nothing, and a strategy that was never handed a
candle, both produce zero trades. Nothing in P&L tells them apart; this does.

---

## Soak test

`scripts/soak_instances.py` — reproducible, exits non-zero on regression.

```bash
# against the real venue, on a host that can reach fstream.binance.com
python scripts/soak_instances.py --hours 6

# offline, with the candle clock compressed 60x (every continuity, staleness
# and gap rule still runs, on a shorter clock)
python scripts/soak_instances.py --minutes 20 --simulated --clock-scale 60
```

25-minute three-instance run, this environment:

```
    elapsed   rss_mb  thr  workers  chans  queue  drop  recon  stale  evals  dup_lease  dup_order
         60s    76.8    9        3      9      0     0      0      0     36          0          0
        ...
       1500s    77.7    9        3      9      0     0      0      0    900          0          0

VERDICT
  [PASS] memory stable                RSS 76.8 -> 77.7 MB (+0.9)
  [PASS] no worker duplication        one execution owner per instance throughout
  [PASS] no duplicate orders          no repeated (alert_id, instance_id, status)
  [PASS] no untagged trades           every trade row carries an instance_id
  [PASS] queues bounded               peak candle queue depth 0
  [PASS] channels stable              market connections [9]
  [PASS] no reconciliation failures   durable records stayed coherent
  [PASS] strategies were evaluated    900 closed candles evaluated

  8/8 checks passed over 25 samples
```

---

## Regression coverage

CI fails if any of these is reintroduced.

| Bug | Test |
|---|---|
| Browser-owned market feed | `test_runtime_is_independent_of_the_browser.py` (5) |
| Single-instance assumption | `test_three_instances_run_concurrently_by_default`, `test_a_persisted_single_slot_is_migrated_to_the_supported_default` |
| Duplicate workers | `test_a_second_process_cannot_run_the_same_instance`, `test_a_worker_that_loses_its_lease_stops_itself`, `test_concurrent_starts_create_exactly_one_worker` |
| Swallowed Start | `test_a_start_racing_a_stop_is_not_silently_swallowed` |
| Stale-data trading | `test_stale_market_data_fails_entries_closed`, `test_market_status_distinguishes_every_transport_phase` |
| Unsupported strategy in the selector | `test_strategy_registry_is_authoritative.py` (11) |
| Cross-instance contamination | `test_instance_trading_state_is_isolated`, `test_records_carry_instance_id` via reconciliation |
| Missing `instance_id` | soak check + `records_carry_instance_id` |
| Failed restart restoration | `test_restart_restores_running_and_paused_instances`, `test_a_feed_outage_does_not_un_desire_the_instance` |
| Duplicate order creation | `test_the_database_refuses_a_second_order_for_one_idempotency_key`, `test_a_replayed_candle_cannot_create_a_second_order` |
| Cross-account access | `test_api_routes_refuse_another_owners_instance`, `test_every_lifecycle_action_verifies_ownership` |
| Unfunded order | `test_an_order_larger_than_the_account_is_rejected` |
| Naive clock | `test_trading_code_never_calls_a_naive_clock` |
| Unbounded queue | `test_a_consumer_that_stops_keeping_up_is_bounded_and_reported` |
| Lockstep reconnects | `test_reconnect_backoff_is_jittered_so_channels_do_not_retry_in_lockstep` |
| Route shadowing | `test_runtime_routes_are_not_swallowed_by_the_instance_id_parameter` |

---

## Review round — fifteen defects found in this branch, eight of them mine

A high-effort review of the accumulated diff found real bugs, including
several introduced by the earlier phases of this work. All are fixed and
pinned; each entry names the mechanism, not just the symptom.

| # | Defect | Why it mattered |
|---|---|---|
| R-1 | `claim_worker_lease` was a read-then-upsert | The "exactly one execution owner" guarantee did not exist. Two processes could both read "no live lease" and both run an unconditional `ON CONFLICT DO UPDATE`; the second simply won. The predicate now lives in the write, with a rowcount check. **Eight threads across eight connections now yield one winner and seven refusals.** |
| R-2 | Losing a lease called `stop()` | Wrote `desired_running=False` into the row every process reads, so nothing would restart the instance once a split brain resolved — the exact failure the supervisor exists to end. Now `halt_runtime()`, which gives up the worker without touching durable intent. |
| R-3 | A config edit's rebuild called `stop()` | An edit during a brief venue outage left the instance un-desired, unsupervised and dark. Same fix. |
| R-4 | `start()` wrote `"starting"` over `state="paused"` | The first restart restored the entry gate correctly; the **second** read `"running"` and re-armed a strategy the operator had deliberately disarmed. |
| R-5 | The duplicate check ran *after* `paper.open()` | A replayed candle opened a real second position and only then hit the constraint, leaving an open position with no webhook row, no journal entry, and a caller told the cycle produced nothing. The key is now claimed *before* the fill and promoted to the order's final row. |
| R-6 | A claim that never became an order stayed | Would make a legitimate retry of the same candle look like a duplicate and silently suppress a trade that never happened. Released on rejection. |
| R-7 | The balance guard's refusal was anonymous | Surfaced as "Order rejected at fill (execution model)" — indistinguishable from a random simulated rejection, which is how a systematic stop hides. Now carries `GATE_REJECTED: INSUFFICIENT_PAPER_CAPITAL`. (The ordinary pipeline path was never affected: the exposure gate caps a position at 5% of equity long before it.) |
| R-8 | `open()` re-derived the guard's `FillResult` with `float(entry)` | Crashed the decision cycle on exactly the unparseable input the guard exists to catch. |
| R-9 | A kline-only channel was handed to a quote consumer | It would receive candles and never a quote, so every parked intent sat unfilled forever while the feed read as SYNCHRONIZED. Such a channel is now upgraded in place, keeping its consumers. |
| R-10 | `stop()` nulled the notifier; `start()` never rebuilt it | A subscription stopped and restarted silently dropped every quote thereafter, uncounted. |
| R-11 | Reconciliation keyed open trades by symbol | Two positions and one trade row on one pair reconciled clean — the exact discrepancy the check exists to catch. It counts now. |
| R-12 | The supervisor iterated the live instance dict unlocked | A concurrent create/delete would abandon the whole sweep, including any dead worker it was about to repair. |
| R-13 | `SUPERVISOR_ERROR` wrote nowhere | `log_event` skips a payload with no `instance_id`, so a supervisor failing on every tick left no trace anywhere. |
| R-14 | `/instances/runtime/health` was unscoped | The one route left handing any caller every instance_id on the deployment. |
| R-15 | The "one-time" slot migration ran every start | One is a legal deliberate choice now, and the unguarded `UPDATE ... WHERE max_active_slots<=1` reverted that operator's decision on every restart. Marked applied. |

The acceptance proof re-run after these fixes shows R-4 visibly resolved: a
paused instance now reports `runtime=PAUSED` after a restart, where it
previously came back reporting RUNNING.

---

## Closing the last two spec gaps

**A refused delete now has a route forward.** Refusing to delete an instance
holding an open paper position is correct, but the operator was told no and
left to work it out. `GET /instances/{id}/open-positions` names each blocking
position with its unrealised P&L at the last observed mark, and
`POST /instances/{id}/close-open-positions` realises them behind an explicit
confirmation. It never invents a price: a position the runtime cannot price is
left open and reported, because closing it at a guess would write a fabricated
result into the history. The guard stays deliberately broad — any open
position blocks, in any simulation session — so a position left from an
earlier paper session cannot be discarded with the instance.

**Warm-up is a declared contract.** Every registry entry now states how many
entry candles it needs and which lookback that figure comes from, and three
tests check the claim against the strategy itself: the engine must warm to at
least the declared number, and the declared number must exceed the strategy's
longest actual lookback. Only the two price-action engines declared anything
before; the other eight relied on the engine's generic 150 covering their
indicators — true, but unstated and untested.

---

## Test results

```
automation-hub:  2234 passed, 15 skipped
                 (original baseline 2111 -> phase 1: 2163 -> phase 2: 2220 -> review: 2234)
engine (tests/):  508 passed
dashboard:       tsc --noEmit clean; vite build ok
soak (25 min):   8/8 checks over 25 samples
soak (12 min, after the review fixes): 8/8 checks over 12 samples
```

123 tests added across both phases and the review round.

One observation from the post-fix soak worth recording rather than hiding: the
last two samples reported `stale_feed_count: 3`. That run shared the host with
the acceptance proof, and on the compressed clock a "5m" candle is 5 seconds
with a 7.5-second staleness threshold — so CPU contention genuinely made the
feeds briefly stale, and the metric correctly said so. On a real 5m clock the
equivalent margin is seven and a half minutes. It is not a pass/fail criterion
in the soak for that reason; treat a non-zero value on the VPS run as real.

---

## Migration

Additive and idempotent; safe to re-run. SQLite migrates itself on first open.
For Supabase run `data/trading_instances_schema.sql`, reload the PostgREST
schema cache, restart, then `data/verify_trading_instances_schema.sql` should
return no rows.

```sql
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT '__owner__';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS config_revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS worker_heartbeat TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS instance_worker_leases (...);
CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_alert_instance_unique
  ON webhook_events(alert_id, instance_id, status);
UPDATE trading_instance_platform_settings SET max_active_slots = 3 WHERE max_active_slots <= 1;
```

No table is dropped, no row deleted, no trading history touched.

---

## Remaining risks

1. **Binance is unreachable from this container.** Wire-format parsing, real
   reconnect behaviour and real rate-limit headroom still need one run on the
   VPS: `python scripts/soak_instances.py --hours 6`.
2. **The unique idempotency index may not install on an existing database**
   that already contains duplicate `(alert_id, instance_id, status)` rows. It
   reports `enforced: false` with samples rather than failing the boot; check
   `/instances/{id}/reconciliation` after deploying and de-duplicate if so.
3. **Leverage, margin, liquidation and funding are not modelled.** Instances
   run on USD-M perpetuals but the paper account is unleveraged cash. Results
   are therefore not comparable to a leveraged live account.
4. **Stops resolve on candle close, at the stop level.** Optimistic against
   real slippage. Deliberately unchanged — fixing it changes measured
   performance, which was out of scope.
5. **Lease TTL is a real trade-off.** After a hard crash an instance waits up
   to `HUB_WORKER_LEASE_TTL_SECONDS` (120s) before the successor may take over.
   Shorter risks two owners during a GC pause; longer means slower recovery.
6. **`_global_guard` remains cross-instance** (carried from phase 1): one
   instance's daily loss can block another's entries. A deliberate platform
   control, left alone, but it bites sooner with three instances sharing one
   capital pool.
7. **Ownership enforcement is dormant until `HUB_MULTI_USER` is on.** The
   check, the column and the tests exist; with the flag off every caller is the
   owner. Turning it on is a separate decision with its own migration question:
   which existing instances belong to whom.
