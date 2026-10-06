# Trade Journal — audit and canonical journal architecture

The Trade Journal is the single structured record of every executed trade, from
Trading Instances, the Price Action Lab, the SMC Strategy Lab, the legacy auto
engine and webhook/manual trades. Backtests are kept in the journal too, but
labelled as a separate trading mode. Strategy logic, entry conditions,
live-trading permissions and account balances are not changed by any of this.

## 1. Audit of the previous implementation

| # | Question | Finding |
|---|---|---|
| 1 | Frontend files | `pages/JournalHub.tsx` (tabs), `pages/Journal.tsx` (14-column table, win rate and grade computed in the browser), `components/journal/DecisionJournalPanel.tsx` (9 JSON sections). Labs had their own journal views. |
| 2 | Backend endpoints | `routers/journal.py`: `/journal/trades`, `/journal/{id}`, `/journal/evolution` (decision journal); `/journal`, `/journal/from-replay` (JSON replay journal); `/trade-memory/*`; `/research/price-action/journal*`; SMC `/research/smc/journal`. |
| 3 | Storage | `journal.db`: `trade_decision_journal` (summary columns + `sections_json` blob), `trade_decision_events`, `evolution_memory`. `journal.json` (replay entries). PA lab: `pa_journal_entries` + `pa_journal_revisions` (setup-scoped, immutable). SMC lab: no stored journal, rebuilt per read. `trade_memory.db`. |
| 4 | Already recorded | symbol, side, strategy/version/instance, timeframe, entry/stop/target, size, risk amount, planned RR, net-based R, P&L, sizing receipt, gate checklist, quality gate, MFE/MAE in R when tracked. |
| 5 | Missing | TRD id, order/execution ids, venue/market type, base/quote, HTF, requested vs filled price, slippage, leverage, margin, balance/equity/available margin before entry, entry/exit/funding fee split, gross vs net, signal/order/fill timestamps, session and London time, duration, exit-reason taxonomy, partial exits, SL/TP modification history, MAE/MFE in currency, operational results, immutability and a correction audit trail. |
| 6 | Entry point | `SignalPipeline._process` called `journal.record_entry` after `paper.open`, and `record_exit` on CLOSE signals. |
| 7 | Same pathway? | Legacy engine and Trading Instances shared `DecisionJournal`. PA and SMC labs used separate brokers and separate journals. |
| 8 | Duplicates | Five journal-like systems (decision journal, replay JSON journal, PA journal, SMC derived view, trade memory). |
| 9 | Restart / reconciliation | SQLite under `HUB_DATA_DIR` survives restarts, but nothing reconciled the journal with the ledger. Four gaps: **(a)** forward-paper fills (an intent filled later by `process_quote`) were never journaled — the pipeline returned at `intent`, so `record_exit` later found nothing; **(b)** a scale-out closed the ledger row and opened a remainder row with a new id, so the final close targeted an id the journal never saw and the journal stayed open forever; **(c)** manual `/paper/close` and instance disposal bypassed the journal; **(d)** stop/target moves were never recorded. |
| 10 | Migration | `trade_decision_journal` rows (enriched from ledger `paper_trades`), PA/SMC lab fills, and replay entries (as BACKTEST). |

## 2. Data model (`data/trade_journal_store.py`, in `journal.db`)

| Table | Purpose |
|---|---|
| `journal_trades` | One canonical row per executed trade (or operational order outcome). Every field used for filtering or statistics is a first-class column: identity, entry, risk, timing, exit, result, excursions, decision summary, provenance. |
| `journal_trade_links` | `(link_type, ref) → trade_id`. Ledger trade ids, the remainder rows a partial exit creates, ledger positions, lab fills/orders, legacy journal ids, replay ids. The primary key makes "one executed trade = one canonical trade" a database constraint. |
| `journal_executions` | Every fill (`ENTRY`, `PARTIAL_EXIT`, `EXIT`), keyed by execution id — a replayed fill is a no-op. |
| `journal_fees` | `ENTRY_COMMISSION`, `EXIT_COMMISSION`, `FUNDING` per execution. |
| `journal_modifications` | Stop/target/size changes after entry, with reason (`BREAK_EVEN`, `TRAILING`, `MANUAL`, `STRATEGY`, `PARTIAL_EXIT`) and actor. Originals stay in `initial_*`. |
| `journal_snapshots` | The frozen decision at entry: decision, reason, conditions passed/failed/missing, confidence, score, bias, HTF bias, risk decision, feed health, candle/HTF freshness, strategy/execution state, strategy-aware setup fields, market context. |
| `journal_events` | The trade timeline. |
| `journal_reviews` | Agent reviews, stored apart from trade facts. |
| `journal_corrections` | Audit trail: previous value, new value, timestamp, reason, actor. |
| `journal_notes` | Manual commentary only. |
| `journal_weekly_reviews` | Persisted weekly strategy reviews (history kept). |

Strategy-specific detail lives in JSON on the snapshot (`setup_json`,
`market_context_json`). Everything that is filtered or aggregated is a column.

### Integrity

SQLite triggers enforce, regardless of which code path writes:

- identity facts (symbol, direction, strategy, mode, …) cannot change once set;
- entry and risk facts are frozen once `entry_locked = 1`;
- exit facts are frozen once `finalised_at` is set. Finalising a trade also sets `entry_locked`, so a closed trade's entry facts are frozen too;
- locks cannot be released, and journal trades cannot be deleted;
- snapshots, executions, fees, modifications, events, reviews, corrections and notes are append-only.

The only way to change a guarded fact is `TradeJournalStore.correct()` (API:
`POST /journal/v2/trades/{ref}/corrections`). It writes one
`journal_corrections` row per field and bumps `correction_seq` in the same
transaction; the trigger accepts the update only when that row exists.
Factory Reset is the single exception that clears the tables, through the
store's own reset.

## 3. Write path (`services/trade_journal.py`)

```
strategy / decision engine ─┐
risk manager / sizing ──────┼─► SignalPipeline.register_decision ─► PENDING trade + frozen snapshot
account state ──────────────┘                     │  journal_trade_id rides in the order's sizing_context
                                                  ▼
PaperExecutionEngine.open / process_quote ─► on_entry_fill ─► OPEN (fill, slippage, fees, session, risk)
PaperExecutionEngine.update_management ────► on_protection_change ─► modification rows
PaperExecutionEngine.reduce ───────────────► on_partial_exit ─► PARTIALLY_CLOSED, remainder linked
PaperExecutionEngine.close (any caller) ───► on_exit_fill ─► CLOSED, result, review agent
```

- The decision is registered after every gate has passed and before the order. Forward-paper intents carry the journal id in their persisted sizing context, so a fill after a restart still joins its decision.
- Rejected orders become `REJECTED`, exceptions become `EXECUTION_FAILED`, and a suppressed duplicate intent becomes `CANCELLED`. A pending order that neither fills nor cancels within 24h becomes `EXECUTION_UNCERTAIN`. All four are operational events: `is_operational = 1`, `counts_in_stats = 0`. They are never losses.
- The engine hooks are wrapped so a journal failure is logged and never blocks execution. Reconciliation repairs anything a crash interrupted.
- For forward fills, the legacy decision journal (which feeds trade memory) is back-filled from the frozen snapshot. That closes gap (a) for trade memory too.
- The pipeline resolves a remainder ledger row to its original id before closing the legacy journal, which closes gap (b).

### Sources and modes

| Source | `trade_source` | `trading_mode` |
|---|---|---|
| Trading Instance (forward) | `TRADING_INSTANCE` | `FORWARD_PAPER` |
| Trading Instance (replay) | `TRADING_INSTANCE` | `SIMULATION` |
| Legacy auto engine / webhook | `AUTO_ENGINE` / `WEBHOOK` | `FORWARD_PAPER` (live data) or `SIMULATION` |
| Price Action / SMC lab, live paper session | `PRICE_ACTION_LAB` / `SMC_LAB` | `ISOLATED_FORWARD_PAPER` |
| Price Action / SMC lab, replay session | same | `SIMULATION` |
| Replay journal | `BACKTEST_REPLAY` | `BACKTEST` |
| Live (locked) | — | `LIVE` |
| Legacy rows without provenance | — | `UNKNOWN` (never guessed) |

## 4. Reconciliation and ingestion (`services/journal_ingest.py`)

`JournalSync` runs once at boot and then every `HUB_JOURNAL_SYNC_INTERVAL`
seconds (default 60; `HUB_JOURNAL_SYNC=0` disables the timer). The boot pass
runs on the sync thread, so a large first import never delays startup. Every
step is idempotent and isolated from the others.

**Execution never waits for an import.** Live fill hooks and imports share the
recorder lock. An import reads its source history before taking the lock, then
takes it for one item at a time (a lab trip, a ledger or legacy row, a replay
entry), re-checking that item under the lock. Python locks are not fair: a
thread that releases a lock and takes it straight back usually wins over one
already waiting. So between items an import first lets every waiting hook
through (`TradeJournalRecorder.bulk_item`). A hook therefore waits for at most
one item's writes. Measured: a paper signal-to-open took 12.5 ms during a 300-trip
lab import (4.7 ms idle), against 6.7 s before. A forward-paper fill price was
unchanged with or without a concurrent 1,000-entry replay import.
`journal.db` runs in WAL mode with `synchronous=NORMAL`, the convention for
runtime databases, so hook commits do not fsync. The legacy decision journal
shares the file and keeps working.

1. **Legacy migration**: maps each `trade_decision_journal` row once. Fields it did not record stay NULL; for example, leverage is never invented for history. It runs at boot and on an explicit sync. Both histories are read before the recorder lock is taken, and the lock is taken per row, so a sync never makes a live fill hook wait.
2. **Ledger reconciliation**:
   - creates records for ledger trades the journal never saw (`RECONCILED_FROM_LEDGER`, no snapshot);
   - links partial-exit remainder rows to their parent;
   - applies ledger closes the live hook missed.

   Rows younger than two minutes are left to the live hooks. The ledger is read before the recorder lock is taken, so a live fill hook never waits behind a history read.
3. **Lab ingestion**: rebuilds round trips from each lab's stored `v2_fills` (scale-ins, partial exits and reversals), and joins the order metadata and the setup each lab froze at placement.
   - **One trip, one record**: a trip is identified by its first fill. One entry order can open two positions: on the participation-limited candle path, its remainder can fill after the first position already closed. Each position is then its own trade; the `LAB_ORDER` link points at the order's first trip only.
   - **Entry facts** (quantity, average entry, risk, planned RR) follow every entry fill until the entry order settles (filled, cancelled, expired or rejected) or the trip closes. A scale-out before the order has finished filling does not freeze them.
   - **Stop and target** are the levels the lab broker actually armed on the position. With a frozen target R, `PaperBrokerV2` re-anchors the target to the position's entry after each entry fill. That entry is re-averaged over the size still open, so an exit between entry fills changes the weights. The journal replays that averaging and applies the same rule (`PaperBrokerV2._resolved_protection`) to the same stored inputs, so `initial_target` and `planned_rr` match the broker. The order's pre-fill target is kept in the snapshot as `risk.pre_fill_target`.
   - **Funding** goes to the trip whose position the broker charged. A lab broker holds one position per symbol, so a funding event belongs to the trip on its symbol that opened at or before it and before the next trip on that symbol opened. All events of one broker `position_id` stay together. This covers funding booked between the exit quote's receipt and its processing. A lab trade with no funding booked records `0`, because the lab broker books every funding charge; the Trading Instance engine does not model funding, so its trades keep NULL.
   - **Exit reason** is inferred from the protective fill price and labelled `INFERRED_FROM_FILL_PRICE`.
   - **Steady state**: one query fetches the already-finalised lab fills and those trips are skipped, so a pass does not grow with closed history. Lab evidence (PA excursions, the SMC candidate evaluation) is read one record at a time, only for trips being created or finalised.
4. **Replay journal**: imported as `BACKTEST`, R only. One query fetches the replay ids already imported; there is no per-entry lookup.

### Idle passes read no history

Each source is re-read only when its own change marker moved since the last successful pass:

| Source | Marker (indexed reads, independent of history size) | Also re-read when |
|---|---|---|
| Engine ledger (SQLite) | newest `paper_trades` row and newest `paper_executions` row (rowid + id); every open, reduce and close writes one | the previous pass left rows inside the 2-minute grace window, or it is the first pass |
| Engine ledger (Supabase, production) | newest opening and newest close in `paper_trades` (two one-row PostgREST reads) | as above |
| Each lab | newest `v2_fills` row and newest `v2_funding_events` row | one of its trades is still waiting for its entry order to complete |
| Replay journal | file modification time and size | — |

An idle pass therefore costs those watermark reads, one status-indexed check, and the stale-`PENDING` check (which runs every pass). The first pass after a restart always does the full, idempotent reconciliation. If a marker cannot be read, that source gets a full pass (logged): a marker problem costs speed, never a missed reconciliation.

## 5. Sessions and time (`services/journal_sessions.py`)

Sessions are classified in each centre's local time, with DST computed by rule (no tzdata dependency):

| Session | Hours (local time) |
|---|---|
| Asia (Tokyo) | 09:00–18:00 |
| London | 08:00–17:00 |
| New York | 08:00–17:00 |
| London/New York overlap | both open |
| Outside main sessions | none open |

The configured trading window is stored separately as `in_preferred_session`. The model version is stored on every trade.

## 6. Analytics (`services/journal_analytics.py`) and API (`routers/journal_v2.py`)

All statistics are computed server-side over the filtered journal.

**Mode rule.** When no mode is given, the server picks one mode (forward paper if it has trades). Mixing modes requires an explicit `modes=` list or `ALL`. Responses report `modes_applied`, `mixed_modes` and `mode_warning`.

**Trades**

| Endpoint | Returns |
|---|---|
| `GET /journal/v2/meta` | modes with counts, default mode, facets, column catalogue |
| `GET /journal/v2/trades` (+ `.csv`) | filtered, paged trades |
| `GET /journal/v2/trades/{ref}` | full detail: facts, snapshot, executions, fees, modifications, timeline, reviews, notes, corrections, links, missing fields, the strategy's overall context in the same mode |
| `GET /journal/v2/trades/{ref}/timeline` | the trade timeline |
| `GET` / `POST /journal/v2/trades/{ref}/reviews` | read reviews / run the review agent |
| `POST /journal/v2/trades/{ref}/reviews/external` | store a review from another agent |
| `POST /journal/v2/trades/{ref}/notes` | add a note |
| `GET` / `POST /journal/v2/trades/{ref}/corrections` | read / apply corrections |

**Analytics**

| Endpoint | Returns |
|---|---|
| `GET /journal/v2/summary` | dashboard cards |
| `GET /journal/v2/performance/{strategies,comparison,sessions,symbols,directions,leverage,rr,excursions,trend}` | individual breakdowns |
| `GET /journal/v2/analytics` | everything in one call |

**Weekly review and maintenance**

| Endpoint | Returns |
|---|---|
| `GET` / `POST /journal/v2/weekly-review`, `GET /journal/v2/weekly-reviews` | compute / save a weekly review; review history |
| `POST /journal/v2/sync`, `GET /journal/v2/sync/status` | run sync now; sync status |

Writes require the control credential. Signed-in sessions supply it automatically.

**Filters**, all combinable: `modes`, `date_from`, `date_to`, `strategy`, `instance_id`, `lab`, `trade_source`, `symbol`, `direction`, `result` (`WINS`, `LOSSES`, `OPEN`, `OPERATIONAL` or an exact result), `session`, `timeframe`, `leverage_min/max`, `rr_min/max`, `realised_r_min/max`, `pnl_min/max`, `rule_violation`, `exit_reason`, `status`.

- **London time.** Every time on screen is London time. A bare `date_from` / `date_to` is a London calendar day, and a weekly review covers a London ISO week (Monday 00:00 London time), the same weeks the trend table uses. A trade belongs to the week it closed in; an order that never filled belongs to the week it was placed in.
- **Rule check.** `rule_violation=false` means the rule check ran and passed. A trade whose check has not run (NULL) is in neither group.
- **Unknown stays unknown.** Totals (net P&L, gross profit/loss, fees, funding) sum the recorded values only. When none was recorded, the total is NULL (shown as "—"), never `0`. The equity curve has no point for a trade without a recorded P&L.
- **CSV export** carries every matching trade.
- **Query plans.** The API always sends a trading mode, so every list is bounded by the trading-mode index. Symbol, instance and status filters have their own indexes. A date range or a strategy is evaluated within the mode's rows.
- **Dashboard reads** (trade lists, counts, facets) use a separate WAL reader connection and decode rows outside any lock, so a poll over a long history never holds the lock the fill hooks need.

## 7. Reviews

- **Trade review agent** (`services/journal_review.py`): runs on every close. It writes setup quality, execution quality, risk management, outcome, mistakes, what went well/wrong, improvement and rule violations, from journal facts only. Reviews are separate rows and cannot change the trade.
- **Weekly review**: observations from the journal, gated on sample size, for example *"SMC Lab had a 100% win rate during the London session but only 0% during New York"*. It covers:
  - strategy profitability, session splits and symbol expectancy;
  - long/short, planned vs realised RR and drawdown;
  - leverage, rule violations and repeated mistakes;
  - week-over-week change.

  It reports; it never changes strategy logic.

## 8. UI (`automation-hub-dashboard`)

Journal tabs:

- **Trades**: mode selector, combined filters, ten summary cards, a column-customisable table, and a trade drawer with twelve sections: Summary, Execution, Risk, Strategy Setup, Decision Snapshot, Market Context, Exit & Result, Fees, Performance, Timeline, Agent Review, Raw Audit Data.
- **Analytics**: comparison, equity curve, trend, per-strategy and per-instance stats, sessions, hour of day, symbols, long/short, leverage, RR, MAE/MFE.
- **Weekly Review**
- **Decision Journal**: the legacy view, kept.
- **Decisions**, **Memory**, **Notes**

## 9. Known limits (stated, not hidden)

- **Leverage and margin on instances:** the Trading Instance paper engine is unleveraged cash. Leverage is recorded as `1x` with `leverage_source = UNLEVERAGED_CASH_MODEL`, and margin equals notional.
- **Funding on instances:** not modelled by the instance engine, so `funding_total` is NULL there, not 0, including in totals. Lab funding is recorded.
- **Dashboard cost on long histories:** the summary and analytics endpoints recompute over the whole filtered history on each poll: about 1.8 s of CPU per request at 20,000 trades in one mode, 0.15 s at 2,000. A poll holds no lock a fill hook needs, so execution only shares the CPU: the worst hook store call during three 20,000-trade polls took 71 ms. Narrow the date range on very long histories.
- **MFE/MAE coverage:** recorded where the execution path tracked them (closed-bar extremes for instances; research-engine R for PA). Elsewhere they are NULL and excluded from excursion statistics.
- **SMC instance strategy:** its structure-break condition does not distinguish BOS from CHoCH, and the journal says so.
- **Account equity before entry:** recorded only when it is knowable, i.e. no other position open, or the lab captured it at placement.
- **Migrated legacy records:** keep NULL for every field they never captured. Records without provenance stay `UNKNOWN` mode.
- **Legs repaired by reconciliation:** when a live hook was missed, the ledger row supplies the fee amount but not the commission rate, so that leg's fee `rate` is NULL. A `paper_trades` row carries no position id, so the remainder is linked by its ledger trade id only, with no `LEDGER_POSITION` link.
- **Stop/target edits on a lab position** (`set_protection`) change the broker position in place and leave no fill, so the journal does not see them. A lab trade's `current_stop` / `current_target` therefore stay at the armed levels. The SMC Lab audits a manual edit (`paper_position_protection_changed`). The PA Lab's endpoint edits the broker position directly, with no audit row. **Follow-up** ([issue 30](https://github.com/Arjuunb/nexus-trading-bot/issues/30)): record each protection change at the source and ingest it as a `journal_modifications` row.
- **R definitions:** the journal's R is measured against the original risk at entry (`|entry fill − initial stop| × quantity`). `realised_r` is net P&L over that risk, and `gross_r` is gross P&L over it. The ledger's own `paper_trades.rr` column is a different figure: gross R against the stop *at close*, rounded to 3 dp. The two agree only while the stop has not moved.
- **Factory reset** clears the journal together with the ledger, as the audited operator reset always has. No other path deletes a journal record.
- **Paper account reset** (an initial-capital change with `reset_trades`) deletes every paper trade and position, across instances. The journal ends each open paper-engine trade as `CANCELLED` / `SIMULATION_RESET`: operational, with no fabricated fill, keeping realised partial exits. Lab trades and pending orders are not touched.
