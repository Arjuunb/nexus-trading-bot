# Journal audit — where the numbers come from (2026-09-26)

Audited at `claude/focused-gates-q5nse7` @ `8fe21ec`, clean tree. Live routing
is hard-locked (`BrokerRegistry.live_locked()` returns `True`;
`HUB_ENABLE_EXTERNAL_LIVE` defaults to `0`). Nothing in this audit changed data.

What is **proven from the code** is marked PROVEN. What can only be settled by
the production databases is marked SERVER, with the read-only script that
settles it (`scripts/journal_provenance.py`).

## The two numbers on the Journal page

| Screen element | API | Store | Table |
|---|---|---|---|
| Journaled Trades / Decision Journal "0 of 0" | `GET /journal/trades` | `data/journal_store.JournalStore` (`journal.db`) | `trade_decision_journal` |
| Evolution Memory 59 / 53 | `GET /journal/evolution` | same store, same file | `evolution_memory` |

Both read the same SQLite file. They disagree because they are different
kinds of data:

* `trade_decision_journal` holds one row per journaled trade.
* `evolution_memory` holds **running counters** per `strategy|regime|side`.
  `update_evolution()` adds 1 trade, 1 win if R > 0 and the trade's R to the
  counter. It stores **no trade ids**. A counter cannot be traced back to the
  trades that built it from this table alone. (PROVEN, `data/journal_store.py`)

## 1–5. Where the 59 LONG / 53 SHORT come from

* The only writer of `evolution_memory` is `DecisionJournal.record_exit()`,
  and it returns early unless a `trade_decision_journal` row for that
  trade id exists. **Every one of the 112 increments therefore had a
  journal row at the moment that trade closed.** (PROVEN,
  `services/decision_journal.py:303-347`)
* `record_exit()` is called only from `SignalPipeline`'s close path, with
  the net P&L of a **paper fill** from the execution engine. It is not
  called by backtests, replay, research, the PA/SMC labs or the SMC agent.
  So the 112 are **paper trades executed by `SignalPipeline`**, not backtest
  or research results. (PROVEN)
* The strategy label is `Decision Brain` and the regime is `Trending`. That
  label is used by the retired autonomous engine (`HUB_AUTO_ENGINE`, now
  `0` in the image) and by Trading Instances running the `brain` strategy.
* **Since 2026-09-02 (`e884a7f`, forward-paper intents) no Trading Instance
  trade can reach either table** (see root cause below). The 112 therefore
  closed before that date, on the synchronous paper path. (PROVEN from the
  code path; the dates are SERVER.)
* Whether each of the 112 is *still* backed by a journal row, and exactly
  which ledger trades they were, is SERVER: the script prints, per setup,
  the counter, the journal rows that can back it, the journal **events**
  whose trade row is gone (the events table keeps `trade-opened` /
  `trade-closed` details per trade id) and those trade ids' rows in the
  ledger's `paper_trades`.

Until the script shows a journal row or ledger trade behind each increment,
**the 59/53 must be presented as LEGACY / UNVERIFIED counters**, not as
verified trading history.

## 6–8. Why the Journal shows 0 — root cause

**Root cause A — forward-paper fills are never journaled (PROVEN).**
Trading Instances run `ForwardPaperExecutionEngine`. On an accepted entry the
pipeline parks an *intent* and returns at "paper order intent awaiting next
quote" (`signal_pipeline.py` ~line 995) — **before** the `record_entry()` call
at ~line 1034. The fill happens later in `ForwardPaperExecutionEngine.process_quote()`
(`execution/paper_engine.py:502`), which writes `paper_trades`, `positions`,
`paper_executions` and a fill-evidence `webhook_events` row, but never calls
the journal. When the position later closes, `record_exit()` finds no journal
row and returns `None` silently. Result: every Instance trade since
2026-09-02 (LINKUSDT, SOLUSDT, XRPUSDT, BTCUSDT…) exists in the ledger and in
nobody's Journal or Evolution Memory.

**Contributing B — every journal write swallows its exception**
(`except Exception: pass`, `signal_pipeline.py` ~544 and ~1043). A failed
write leaves no trace and nothing repairs it.

**Contributing C — no journal reconciliation exists.** Nothing ever compares
the ledger with the journal.

**Possible D — stale UI filter (SERVER/browser).** The page persists its
instance filter (`journal.instance`) in localStorage and sends it to the API
without checking the instance still exists. A deleted instance's id keeps the
list at 0 while the select shows no matching option. The script's section F
calls the API with no filter to separate "rows exist" from "filtered away".

## 9. Can a restart lose a journal record?

Yes. On the synchronous path the ledger commits the fill first; the journal
insert runs afterwards. A crash between them loses the journal entry for good
(no repair). On the forward path nothing is journaled at all. The journal's
`trade_decision_events` are separate inserts, so a crash can also leave a
partial timeline.

## 10. Can reconciliation create duplicates?

There is no journal reconciliation today. But `record_exit()` is **not
idempotent**: a second call for the same trade re-runs `close_trade()` and
increments `evolution_memory` again. The pipeline currently rejects a second
close before reaching it ("Close signal with no open position"), so this is
latent, not observed. The legacy global pipeline also picks `_open_tid` as
the first open trade on the symbol from an **unscoped** ledger, which could
be another instance's trade; it is inert while `HUB_AUTO_ENGINE=0`.

## 11. Separate journal implementations (PROVEN)

| Surface | Store | File | Trade identity |
|---|---|---|---|
| Journal page / Evolution | `data/journal_store.JournalStore` | `journal.db` | ledger `paper_trades.id` |
| Memory / Notes tabs | `trade_memory_store` | `trade_memory.db` | composed from the journal at close |
| Human journal entries | `services/journal.JournalStore` | JSON | replay trades |
| Decisions tab | `cycle_store` (per-candle reports) | `cycles.db` | instance + candle |
| Decision Archive | `decision_store` | `decisions.db` | signal decisions (accepted/rejected) |
| Skipped trades | `skipped_store` | `skipped.db` | rejections |
| PA Lab | `PriceActionPaperAccount` + `PriceActionJournalStore` | `price_action_paper.db`, `price_action_research.db` | proposal / order |
| SMC Lab | `SMCPaperAccount.journal()` | `smc_strategy_paper.db` | `proposal_id` |
| SMC Agent | `SMCAgentJournal` | `smc_agent_journal.db` | `agent_trades.id`, `execution_intents.execution_key` (UNIQUE) |
| Adaptive lab | `AdaptiveJournal` + its own instance ledger | `adaptive_lab_journal.db`, `adaptive_lab.db` | ledger trade id |

Instances and the old engine share one journal; the labs and the agent each
have their own; none of them is linked to another.

## 12. Can every completed trade be reconstructed from structured data?

* **Instances:** yes for the execution facts, from the ledger alone —
  `webhook_events[alert_id].payload` (the pipeline payload frozen at decision
  time: strategy, timeframe, regime, score, reason, gate steps, sizing,
  provenance), the `…:fill:…` evidence row (bid, ask, spread, slippage, fee,
  fill time), `paper_trades` (entry, exit, P&L, fees, R, risk at entry, equity
  before), `paper_executions` (OPEN/REDUCE/CLOSE → trade + position) and the
  close event's payload (exit reason, MFE/MAE in R). Strategy-specific
  condition evidence is only in cycle reports and the decision store.
* **SMC Lab / PA Lab:** their journals freeze conditions, MTF evidence, plan
  and fills at decision time; P&L comes from the broker's fills.
* **SMC Agent:** `agent_decisions` / `agent_trades` store conditions, plan,
  gates and market snapshot as JSON; intents are keyed and durable.

## 13. Important information that exists only as text

The brain's `reason`, journal event `detail` strings (`"win · +1.23R · PnL …"`),
review `coach` text, evolution `note`, SMC/PA notes, skipped reasons, and the
exit reason on non-instance paths.

## 14. Information that exists but is not linked to the Journal

Decision store (every accepted/rejected signal with rules), cycle reports
(per-candle conditions), skipped store, the ledger's decision payloads and
fill evidence, `paper_executions`, PA/SMC lab journals, SMC agent decisions,
trades, reviews and intents, and the adaptive lab journal.

## Consequences for the repair

1. Build the canonical record from **durable execution facts** (ledger,
   lab journals, agent intents) with a projector that is safe to re-run,
   plus prompt hooks. Never from current market data.
2. Key it by the execution identity the ledger already enforces
   (`alert_id` / `paper_trades.id`, `execution_key`), with UNIQUE
   constraints, so reconciliation cannot duplicate.
3. Leave `evolution_memory` in place and label its counters by what can
   be proven; derive new memory only from canonical records with ids.
4. Do not touch strategy code: the frozen lab files are read through their
   public `journal()` / `state()` output.
