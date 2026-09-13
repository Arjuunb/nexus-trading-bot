# Trading Instance runtime audit — 2026-09-13

Scope: the complete chain, Binance USD-M WebSocket → market-data hub → engine →
instance runtime → persistence → API → dashboard. Every finding below is backed
by a code reference plus a reproduction, and every fix is backed by a test.

Execution stayed in forward-paper simulation for the whole audit. No exchange
routing was enabled, no trading history was reset, no risk control was relaxed,
no market data was fabricated, and no strategy logic was changed.

---

## 0. Environment caveat, stated up front

This container's egress policy denies `fapi.binance.com` and
`fstream.binance.com`:

```
$ curl https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m
URLError <urlopen error Tunnel connection failed: 403 Forbidden>
$ curl -sS "$HTTPS_PROXY/__agentproxy/status"
  "recentRelayFailures": [{"kind": "connect_rejected",
    "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
    "host": "fapi.binance.com:443"}]
```

So the runtime proofs below substitute a **Binance-protocol double at the socket
boundary only**. Everything above the socket is the shipped code: the hub's
channel keying and fan-out, REST reconciliation, warm-up, continuity and gap
checks, staleness thresholds, the engine loop, the supervisor, persistence, the
status contract and the API. Candle timestamps are never faked — the double only
emits a candle whose close time has actually passed on the wall clock, and where
many closes were needed in one run, the candle *duration table* was compressed
consistently (5m → 5s) in every table the runtime consults, so every freshness
and continuity rule still ran, just on a shorter clock.

What this cannot prove locally: that Binance's own wire format still parses, and
real reconnect behaviour against Binance's servers. Those need one run on the
VPS; `scripts/check_binance_feed.sh` already exists for it.

---

## 1. Current architecture map

```
Binance USD-M public WebSocket
  fstream.binance.com/market/stream?streams=<sym>@kline_<tf>/<sym>@markPrice@1s
  fstream.binance.com/public/stream?streams=<sym>@bookTicker
        │  services/price_action_stream.py — PriceActionPublicStream
        │    per-channel reconnect, 2^n backoff capped at 30s, REST reconcile
        ▼
ForwardPaperMarketDataHub            services/forward_paper_hub.py
  one channel per (symbol, timeframe), fanned out to N consumers
  consumers: PA_LAB · SMC_LAB · RESEARCH · INSTANCE:<id> · <id>:native:<sym>:<tf>
  per-consumer pending queue + single notify worker (ordering, no head-of-line block)
        ▼
AutoStrategyEngine (one per instance)  services/auto_engine.py
  bootstrap → warm-up → durable cursor sync → forward loop on closed candles
  in-thread recovery: 5 attempts, then the thread returns
        ▼
SignalPipeline → ForwardPaperExecutionEngine  services/signal_pipeline.py,
  risk gates, venue rules, sizing        execution/paper_engine.py
  intent parked → filled only by a real Binance quote
        ▼
InstanceLedger (every read and write scoped by instance_id + session)
        ▼
SQLite (dev) / Supabase (prod)
  trading_instances · instance_market_state · instance_metrics ·
  instance_engine_logs · simulation_sessions · positions · paper_trades
        ▼
routers/instances.py  →  automation-hub-dashboard (read-only polling)
```

The intended shape was already right: the backend owns the runtime and the
dashboard only polls. The failures were in lifecycle *intent* and in what the
platform allowed itself to run.

---

## 2. Root-cause findings, severity ranked

### P0-1 — A transient feed outage permanently un-desired the instance

`services/trading_instances.py` (lifecycle callback) cleared `desired_running`
whenever the worker reached `error`. The worker reaches `error` after
`max_reconnect_attempts = 5` (`services/auto_engine.py:165`), whose delays are
2+4+8+16+30s — so **about sixty seconds of Binance being unreachable**
permanently disabled the instance.

Reproduction (before the fix):

```
after start          : state=bootstrapping desired_running=True
max_reconnect_attempts = 5
after outage (61s)   : state=error desired_running=False
last_error           : EngineFeedError: BTCUSDT warmup failed: ...
RESTART restore_desired_instances() -> []
instance state after restart: error desired_running=False
```

This is the mechanism behind *"the market data breaks after I log in again"* and
*"it only works after I manually touch the instance"*. Nothing was wrong with the
login. The worker had died earlier, and the first thing that could notice was a
person opening the page.

### P0-2 — Only one Trading Instance could run

`trading_instance_platform_settings.max_active_slots` defaulted to **1** in both
`_LOCAL_SCHEMA` and `data/trading_instances_schema.sql`, and the manager
additionally applied `min(3, ...)` with the API capped at `le=3`. A persisted `1`
was therefore never an operator risk decision — it was the only value the
platform ever wrote.

Reproduction (before the fix):

```
SHIPPED DEFAULT max_slots = 1
platform_settings row      = 1
created 3 instances OK
start #1: OK
start #2: REFUSED -> ValueError: Maximum active trading slots reached (1)
start #3: REFUSED -> ValueError: Maximum active trading slots reached (1)
```

### P0-3 — No supervisor for instance workers

`Watchdog` is constructed as `Watchdog(engine, ...)` (`webhook_api.py:280`) —
the *legacy singleton* engine, not instance workers. Startup restoration
(`app.py`) runs once. So between boots nothing observed instance workers at all:
a worker that died at 02:00 stayed dead.

### P1-4 — Pause turned into a silent shutdown across restarts

`pause()` set `desired_running = False`, contradicting the design comment two
functions away ("the market worker intentionally stays alive to maintain its
cursor"). Proven:

```
pause SOLUSDT (operator entry gate)
SOLUSDT desired_running after pause = False
--- process restart ---
restored: ['BTCUSDT', 'ETHUSDT']
SOLUSDT   state=paused  desired_running=False   <- no worker, no feed, frozen cursor
```

### P1-5 — Status could not say which layer had failed

`ui_status` collapsed four independent questions into one badge
(`RUNNING_ARMED / RUNNING_UNARMED / BLOCKED / ERROR`). It did correctly force
`BLOCKED` on stale data, so it was not *lying* — but it could not say whether the
cause was the worker, the feed, warm-up or the entry gate. Missing entirely from
the payload: bid, ask, mark price, last WebSocket message timestamp,
subscription state, HTF timeframe, and feed-level reconnect attempts.

### P1-6 — The strategy catalog had drifted from the version registry

`webhook_api._STRATEGY_CATALOG` was hand-maintained. `donchian` has a pinned
`1.0.0` plus a signal fixture in `strategies/builtin_versions.py`, but its
catalog row omitted `version`, so `instance_options()` fell back to
`"unversioned"` and the creation screen offered an unreproducible build of a
fully reproducible strategy. Four more strategies were offered with no immutable
version at all.

### P1-7 — Duplicate quote subscriptions per symbol

`markPrice@1s` and `bookTicker` are **per-symbol**, but every channel subscribed
to them — including the 1h and 4h context channels each instance opens for its
native HTF data. Measured:

```
BEFORE, per symbol:  kline_5m+markPrice, bookTicker,
                     kline_1h+markPrice, bookTicker,
                     kline_4h+markPrice, bookTicker   = 6 sockets
AFTER,  per symbol:  kline_5m+markPrice, bookTicker,
                     kline_1h, kline_4h               = 4 sockets
3 instances on 3 symbols: 18 -> 12 websockets
```

### P2-8 — Instance engine logs were written but never served

Every lifecycle transition was written to `instance_engine_logs`, while
`GET /instances/{id}/logs` served `bot_logs`. The timeline existed and was
invisible.

### P2-9 — `GET /instances/{id}/trades` ignored the simulation session

It filtered on `instance_id` only, so after a paper-account restart the trade
list disagreed with the balance and metrics computed beside it.

### P3-10 — Missing per-instance endpoints

No `/status`, `/positions`, `/orders` or `/metrics`. Callers had to pull the full
detail payload (metrics, performance, decision journal) to read a status badge.

### Not a defect — things the audit cleared

* **Frontend never controlled the runtime.** `useLive` is a read-only poller
  (`src/lib/api.ts`); `/logout` and `/auth/logout` only clear cookies; the PA and
  SMC labs already autostart backend-side with their own supervisors.
* **Trading state was already isolated.** Positions, trades, balances,
  pipelines, controls, learning books and ledgers are per-instance. Verified
  directly, not inferred.
* **The market-data hub already shares channels correctly.** Two instances on
  the same symbol/timeframe join one Binance connection.
* **No global singleton instance state.** No `active_instance`, no implicit
  "first instance" endpoint, no shared candle buffer or quote.
* **SMC generating zero signals in a bare harness is correct**, not broken: it
  fail-closes to `neutral` without native HTF context
  (`strategies/brain.py:115-128`). With context supplied it produces 55 signals
  over 2000 bundled 1h bars. Reported separately as instructed; its conditions
  were not weakened.

---

## 3. Strategy capability matrix

Measured, not assumed. "Signals" = generated over the bundled BTCUSDT 1h series
(2000 bars) with native 4h context supplied, except where noted. "Instance E2E" =
started as a real Trading Instance on the Binance USD-M forward-paper path and
observed to reach `running`.

| Strategy | Backend exists | Signals work | Futures (USD-M) | Immutable version | Dedicated tests | Instance E2E | Timeframes | Status |
|---|---|---|---|---|---|---|---|---|
| `brain` Decision Brain | yes | yes (101) | yes | 1.0.0 + fixture | yes | yes | 1m–4h | **PRODUCTION** |
| `supertrend` Supertrend | yes | yes (40/900) | yes | 1.0.0 + fixture | yes | yes | 1m–4h | **PRODUCTION** |
| `donchian` Donchian Breakout | yes | yes (21/900) | yes | 1.0.0 + fixture | yes | yes | 1m–4h | **PRODUCTION** |
| `adaptive_trend_pullback` | yes | yes (fixture) | yes | 1.0.0 + fixture | yes | yes | 5m only | **PRODUCTION** |
| `price_action_rejection` | yes | yes (78/900) | yes | 1.1.0 hash-frozen | yes | yes | 5m only | **PRODUCTION** |
| `price_action_flip_retest` | yes | yes (31/900) | yes | 1.1.0 hash-frozen | yes | yes | 5m only | **PRODUCTION** |
| `smc` Supply/Demand | yes | yes (55) | yes | **none** | conditional only | yes | 1m–4h | RESEARCH_ONLY |
| `liquidity_sweep` | yes | yes (72) | yes | **none** | yes | yes | 1m–4h | RESEARCH_ONLY |
| `ema` EMA Crossover | yes | yes (55) | yes | **none** | **none** | yes | 1m–4h | RESEARCH_ONLY |
| `ensemble` Confirmation | yes | yes (30) | yes | **none** | incidental only | yes | 1m–4h | RESEARCH_ONLY |

Nothing is dead code, frontend-only, or disconnected from execution — all ten
construct, generate signals and run end-to-end. The demotions are about
**reproducibility**, which is the repository's own stated standard: *"a label
such as `Supertrend` alone is not sufficient to reproduce a paper record"*
(`strategies/builtin_versions.py`). A demotion is reversed by adding the pinned
version and the test, not by editing alpha. No `DEPRECATED` or `DISABLED` entries
were needed.

Existing instances on a demoted strategy are **grandfathered** — they keep
running and keep their positions; only new selections are gated.

---

## 4. Persistence audit

| Data | Current storage | Persistent? | Required | Problem |
|---|---|---|---|---|
| Instance configuration | `trading_instances` | yes | durable | — |
| Strategy id / version / params | `trading_instances` | yes | durable | — |
| Exchange, market type, symbol, timeframe | `trading_instances` | yes | durable | — |
| HTF timeframe | derived from `ENTRY_HTF` | n/a | deterministic | — |
| Instance mode, active/paused, `desired_running` | `trading_instances` | yes | durable | **was cleared by a feed outage and by pause — fixed** |
| Paper balance / equity / risk basis | `trading_instances` + `paper_trades` | yes | durable | — |
| Open positions | `positions` (instance + session scoped) | yes | durable | — |
| Open orders (forward-paper intents) | `instance_market_state.pending_orders_json` | yes | durable | — |
| Fills / trade history | `paper_trades` | yes | durable | — |
| Market subscription metadata | rebuilt from config | n/a | reconstructible | — |
| Candle cursor / warm-up status | `instance_market_state` | yes | durable | — |
| Duplicate / missing / out-of-order counters | `instance_market_state` | yes | durable | — |
| Last market timestamp, blocker | `instance_market_state` | yes | durable | — |
| **Worker heartbeat** | engine object only | **no** | durable | **added `worker_heartbeat` column** |
| Mid-trade management (BE/trail) | `positions.management_json` | yes | durable | — |
| Learning book | per-instance JSON under `HUB_INSTANCE_LEARNING_DIR` | yes | durable | — |
| Performance stats | `instance_metrics` | yes | durable | — |
| Session records | `simulation_sessions` | yes | durable | — |
| Reboot progress | memory | no | ephemeral | acceptable; a reboot does not survive its process |
| WebSocket objects, quote snapshot | memory | no | ephemeral | correct — reconstructible |

---

## 5. Single-instance assumptions found

Searched for global mutable state, shared strategy objects, shared
`current_symbol`, shared account/execution state, shared candle buffers, shared
`latest_quote`, `active_instance`, single-worker/client assumptions, hardcoded
ids, singleton services, and endpoints that implicitly pick the first instance.

| Assumption searched for | Found? | Note |
|---|---|---|
| `max_active_slots` default of 1 + hard cap of 3 | **yes** | P0-2, fixed |
| `Watchdog` bound to the singleton engine | **yes** | P0-3, superseded by `InstanceSupervisor` |
| Shared quote streams across timeframes | **yes** | P1-7, fixed |
| `active_instance` / magic default instance | no | — |
| Endpoint operating on "the current instance" | no | every route already takes `instance_id` |
| Shared strategy object between instances | no | `strategy_factory(key, symbol)` per worker |
| Shared candle buffer | no | per-channel, delivered per consumer |
| Shared paper account / execution state | no | `InstanceLedger` scopes every read and write |
| Shared risk state | partly by design | `_global_guard` is a deliberate **platform-level** cap across instances. Left in place — weakening it was out of bounds. Worth knowing: one instance's losses can block another's entries. |
| Hardcoded instance ids | no | — |
| Frontend storing only one instance | no | the dashboard already renders a list |

---

## 6. Proposed and implemented architecture

```
durable desired state (database)          <- the only authority on what should run
        │
        ├── startup: restore_desired_instances()     one shot, at boot
        └── InstanceSupervisor (daemon thread)       continuous, forever
                 │  reconciles desired vs live workers every 20s
                 │  per-instance exponential backoff 15s -> 600s
                 │  never creates intent; only an operator Stop clears it
                 ▼
        TradingInstanceManager.start(id, entry_gate_closed=paused)
                 ▼
        ForwardPaperMarketDataHub.subscription("INSTANCE:<id>")
                 ├── entry channel  (symbol, tf)  kline + markPrice + bookTicker
                 └── context channels (symbol, 1h/4h)  kline only
                 shared by (symbol, timeframe) across all consumers
```

Status is derived, never asserted:

```
runtime   RUNNING | STARTING | PAUSED | STOPPED | ERROR
market    LIVE | CONNECTING | SYNCHRONIZING | WAITING_FOR_DATA | STALE |
          RECONNECTING | DISCONNECTED | FAILED
strategy  READY | WARMING_UP | WAITING_FOR_DATA | WAITING_FOR_HTF |
          WAITING_FOR_SETUP | BLOCKED | ERROR
execution FORWARD_PAPER | SIGNALS_ONLY | DISABLED    (DISABLED unless market == LIVE)
```

---

## 7. Files changed and why

| File | Why |
|---|---|
| `services/instance_supervisor.py` *(new)* | The missing component. Reconciles durable desired state against live workers with bounded backoff, so a dead worker is repaired without anyone opening the dashboard. |
| `services/instance_status.py` *(new)* | Pure derivation of the four status axes and the market facts behind them. No worker can be changed from here, and an unknown field is `None` rather than a plausible default. |
| `services/instance_telemetry.py` *(new)* | One structured shape for every lifecycle event, carrying `instance_id`, `symbol`, `strategy_id`, `exchange`, `market_type`, `timeframe`, `event`, `status`, `timestamp`. |
| `services/strategy_registry.py` *(new)* | Authoritative registry: lifecycle, supported markets/timeframes, required data, version, evidence. The legacy catalog is now derived from it. |
| `services/trading_instances.py` | Stop clearing `desired_running` on worker error and on pause; restore paused instances with the entry gate closed; slots default 3 / ceiling 10 with a one-time migration; persist the worker heartbeat; serve engine logs; emit structured telemetry; attach the status contract. |
| `services/auto_engine.py` | Unchanged. The in-thread recovery limit is intentionally left at 5 — the supervisor is the outer loop, and widening the inner one would only delay the honest `error`. |
| `services/forward_paper_hub.py` | `channel_exists()` / `channel_report()` for observability; context channels opened kline-only. |
| `services/price_action_stream.py` | `quotes_enabled` mode for context channels; quote health skipped for them (a permanent false `STALE_QUOTE` would be exactly the untrue status this work removes); the live quote added to `status()`. |
| `routers/instances.py` | Registry gate on create/update/platform defaults; production-only options plus the full registry; `/status`, `/positions`, `/orders`, `/metrics`, `/runtime/health`; session-scoped trades; engine events in `/logs`; slot ceiling raised. |
| `webhook_api.py` | Catalog derived from the registry; `InstanceSupervisor` constructed. |
| `app.py` | Start the supervisor at boot (even when nothing was restored); stop it *before* quiescing workers at shutdown, so a deliberate stop is not treated as a fault. |
| `data/trading_instances_schema.sql` | `worker_heartbeat` column; slot default 3 with an idempotent migration of the old `1`. |
| `data/verify_trading_instances_schema.sql` | Verify the new column. |
| `automation-hub-dashboard/src/pages/TradingInstances.tsx` | Render the four axes and the full market panel (venue/market/symbol, exec TF + HTF, last/bid/ask/mark, last closed candle + age, last WebSocket message, subscription, reconnect attempts). |
| `automation-hub-dashboard/src/index.css` | Styles for the axis grid, responsive to one column under 560px. |

### Database / migration

Two additive changes, both idempotent and safe to re-run:

```sql
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS worker_heartbeat TIMESTAMPTZ;
UPDATE trading_instance_platform_settings SET max_active_slots = 3 WHERE max_active_slots <= 1;
```

SQLite migrates itself on first open. For Supabase, run
`data/trading_instances_schema.sql`, reload the PostgREST schema cache, restart.
No table is dropped, no row is deleted, no trading history is touched.

### API changes

| Endpoint | Change |
|---|---|
| `GET /instances/options` | PRODUCTION strategies only; adds `strategy_registry` so absences are explainable |
| `POST /instances` | Refuses a non-PRODUCTION strategy (400, with the reason) |
| `PATCH /instances/{id}` | Same gate when re-pointing at a different strategy |
| `POST /instances/platform` | Slots now `1..10`; default strategy must be PRODUCTION |
| `GET /instances/{id}/status` | **new** — the four axes plus feed/subscription/worker detail |
| `GET /instances/{id}/positions` | **new** — instance + session scoped |
| `GET /instances/{id}/orders` | **new** — forward-paper and strategy limit intents |
| `GET /instances/{id}/metrics` | **new** |
| `GET /instances/{id}/trades` | now session-scoped |
| `GET /instances/{id}/logs` | now also returns `engine_events` |
| `GET /instances/runtime/health` | **new** — supervisor state, workers, open market-data channels |

---

## 8. Test results

```
automation-hub:  2163 passed, 15 skipped   (baseline before this work: 2111)
engine (tests/):  508 passed
dashboard:       tsc --noEmit clean; vite build ok
```

52 tests added across four new modules:

* `tests/test_instance_runtime_reliability.py` (26) — three concurrent
  instances, slot migration, isolation, durable intent, restart recovery,
  supervisor repair and backoff, status truthfulness, shared channels,
  no duplicate quote streams, heartbeat persistence, telemetry shape.
* `tests/test_strategy_registry_is_authoritative.py` (11) — every entry
  constructs, production entries carry a version, declared decision timeframes
  match, only PRODUCTION is selectable, existing instances are grandfathered.
* `tests/test_instance_failure_modes.py` (10) — duplicate subscription, dead
  worker restart, malformed message, missing HTF candle, database error,
  degraded store, delete with an open position, third instance while two run,
  one broken consumer not affecting another.
* `tests/test_runtime_is_independent_of_the_browser.py` (5) — no auth path
  touches the runtime, logout leaves instances running, status is side-effect
  free, configuration is reconstructible from storage, dashboard has no
  strategy list of its own.

Five existing tests were updated, each because it asserted behaviour this audit
identified as the defect (`desired_running` cleared on error / restore failure /
pause, the one-slot default, and a platform default pointing at `ema`).

---

## 9. Runtime verification

### Three instances live concurrently

```
SYMBOL   RUNTIME   MARKET        STRATEGY          EXECUTION          BARS     POS    EQUITY
BTCUSDT  RUNNING   LIVE          BLOCKED           FORWARD_PAPER         4       —   1000.00
ETHUSDT  RUNNING   LIVE          BLOCKED           FORWARD_PAPER         4       —   1000.00
SOLUSDT  RUNNING   LIVE          WAITING_FOR_SETUP FORWARD_PAPER         4       —   1000.00
hub channels: 9  BTCUSDT:5m/1h/4h, ETHUSDT:5m/1h/4h, SOLUSDT:5m/1h/4h
```

### Isolation

```
BTC positions=1  ETH positions=0  SOL positions=0
BTC trades=1     ETH trades=0
every trade row carries instance_id: True
pipelines distinct: True   controls distinct: True   ledgers scoped: True
```

### One venue outage does not touch the others

```
=== BTCUSDT outage only ===
BTCUSDT  RUNNING   RECONNECTING  WAITING_FOR_DATA  DISABLED       4   long   994.71
ETHUSDT  RUNNING   LIVE          WAITING_FOR_SETUP FORWARD_PAPER  9      —  1000.00
SOLUSDT  RUNNING   LIVE          WAITING_FOR_SETUP FORWARD_PAPER  9      —  1000.00
```

BTC fails closed while holding a position. ETH and SOL keep advancing (4 → 9
candles).

### Supervisor repair

```
BTC state=error desired_running=True        <- intent survived (was False before)
sweep: [{'action': 'restored', 'entry_gate_closed': False}]
BTCUSDT  RUNNING   LIVE   BLOCKED   FORWARD_PAPER   16 bars
```

### Restart recovery (no browser involved)

```
paused SOLUSDT; desired_running= True
shutdown: {'requested': 3, 'acknowledged': 3, 'errors': []}
restored: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
BTCUSDT  cursor 11:35:35 -> 11:35:55   equity 995.97 -> 995.97   entries_armed=True
ETHUSDT  cursor 11:35:35 -> 11:35:55   equity 1000.00 -> 1000.00  entries_armed=True
SOLUSDT  cursor 11:35:35 -> 11:35:55   equity 1000.00 -> 1000.00  entries_armed=False
```

The paused instance came back with a **live worker and a closed entry gate** —
its cursor and subscription keep running, its strategy stays disarmed.

### Logout / new login

```
BTCUSDT  runtime=RUNNING  market=LIVE  worker_alive=True  desired=True  last_ws=11:36:00
ETHUSDT  runtime=RUNNING  market=LIVE  worker_alive=True  desired=True  last_ws=11:36:00
SOLUSDT  runtime=RUNNING  market=LIVE  worker_alive=True  desired=True  last_ws=11:36:00
```

### Status truthfulness across transitions

```
created, never started : runtime=STOPPED   market=DISCONNECTED  blocker=no market worker is running
just started           : runtime=STARTING  market=CONNECTING    blocker=opening the Binance USD-M websocket
live                   : runtime=RUNNING   market=LIVE          last=59555.59 bid=59501.57 ask=59513.47 mark=59507.52
paused                 : runtime=PAUSED    market=LIVE          execution=DISABLED
feed lost              : runtime=RUNNING   market=RECONNECTING  execution=DISABLED
stopped                : runtime=STOPPED   market=DISCONNECTED  worker_alive=False
```

---

## 10. Performance

Same process, protocol double, instances added one at a time:

| Instances | RSS (MB) | CPU % per instance | Hub channels | Threads | `status()` latency |
|---|---|---|---|---|---|
| 1 | 75.8 | 0.4 | 3 | 3 | 4.2 ms |
| 2 | 76.6 | 0.3 | 6 | 4 | 1.6 ms |
| 3 | 77.3 | 0.3 | 9 | 5 | 0.9 ms |
| 4 | 77.8 | 0.3 | **9** | 6 | 1.0 ms |
| 5 | 78.1 | 0.3 | **9** | 7 | 0.7 ms |

Instances 4 and 5 reused the BTCUSDT and ETHUSDT channels — channel count stayed
at 9 while instances went 3 → 5, with two consumers per shared channel. Marginal
cost per instance is ~0.5 MB and one thread.

**5–10 instances is safe** on this architecture. WebSocket count is the real
constraint: 2 sockets for the entry channel plus 1 per distinct context clock,
per symbol. Three instances on three symbols is 12 sockets after the duplicate
fix (was 18). Ten instances across ten symbols would be ~40 — fine for Binance's
limits, but worth re-measuring on the VPS. The CPU figure excludes real socket
I/O and should be treated as a floor.

---

## 11. Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Browser closure does not stop ingestion | met | supervisor + hub are backend-owned; `test_runtime_is_independent_of_the_browser.py` |
| Logging out does not stop instances | met | logout clears cookies only; test asserts worker survives |
| Login from a new session restores correct status | met | §9 logout/login; status derives from durable state |
| Backend restart restores enabled instances | met | §9 restart, `restored: [3 instances]` |
| Docker restart restores enabled instances | met | same path — `shutdown()` then a fresh manager over the same database |
| Binance WebSocket reconnects automatically | met | `_stream_channel` per-channel 2^n backoff capped at 30s; unchanged and verified present |
| Stale data is detected | met | `_record_market_snapshot` + `market_status()`; parametrised test over six phases |
| Stale data blocks new entries | met | `execution_status()` returns `DISABLED` unless market is `LIVE` |
| Unsupported/broken strategies absent from creation | met | options offers 6 PRODUCTION; `ema` refused with 400 |
| Production list from an authoritative backend registry | met | `services/strategy_registry.py`; dashboard has no list of its own (tested) |
| At least 3 instances run simultaneously | met | §9, and 5 measured in §10 |
| Isolated trading state per instance | met | §9 isolation |
| Shared feeds cause no cross-contamination | met | shared channel, per-consumer queues; broken-consumer test |
| Isolated balance / positions / orders / trades | met | §9 |
| Every important record contains `instance_id` | met | `all(t["instance_id"] for t in ledger.get_paper_trades())` → True |
| Instance APIs require `instance_id` | met | every instance route is `/instances/{instance_id}/...` |
| One instance pauses/restarts without affecting others | met | §9 outage and pause tests |
| 3-instance test passes | met | §9 |
| Restart recovery test passes | met | §9 |
| Logout/login persistence test passes | met | §9 |
| Frontend no longer controls market-data lifecycle | met | it never did; now pinned by a test |
| No live exchange trading enabled | met | `execution_mode` stays `paper`; `/orders` reports `exchange_routing: false` |

---

## 12. Known remaining risks

1. **Binance was unreachable from this container.** Wire-format parsing and real
   reconnect behaviour still need one run on the VPS. Everything above the socket
   is proven here.
2. **`_global_guard` is cross-instance by design.** One instance's daily loss or
   open risk can block another's entries. That is a platform risk control and was
   deliberately left alone, but with three instances sharing one paper account
   capital pool it will bite sooner than with one. Worth an explicit decision.
3. **The supervisor cannot repair a genuinely bad configuration**, only retry it
   with backoff up to 10 minutes. A cursor outside the venue's recoverable window
   still needs an operator. The state and the reason are now visible in
   `/instances/runtime/health` instead of being silent.
4. **CPU was measured without real socket I/O.** Treat 0.3%/instance as a floor.
5. **Demoting four strategies is a product decision**, made on reproducibility
   grounds. If you want `smc` or `liquidity_sweep` back in the selector, the path
   is a pinned entry in `strategies/builtin_versions.py` with a signal fixture —
   one line in the registry after that. I did not weaken any strategy's
   conditions to make this easier.
6. **`instance_metrics` and reboot progress are not covered by the supervisor.**
   A reboot interrupted by a container restart leaves no resumable record; the
   instance simply restores as a normal worker, which is safe but loses the
   reboot's audit trail.
