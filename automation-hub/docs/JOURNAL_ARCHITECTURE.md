# Journal architecture

Why it was rebuilt: [JOURNAL_AUDIT.md](JOURNAL_AUDIT.md).

## Data flow

Before — seven stores, the one the Journal read was cut off in September:

```
strategy -> decision -> SignalPipeline -> (sync fill) -> record_entry -> journal.db   <- Journal page
                                       -> (forward intent) ... quote fill -> ledger only (never journaled)
PA lab   -> PaperBrokerV2 -> price_action_paper.db (own journal)
SMC lab  -> PaperBrokerV2 -> smc_strategy_paper.db (own journal)
SMC agent -> smc_agent_journal.db (own journal, intents)
```

After — execution layers unchanged; one recorder projects their durable facts:

```
market data -> strategy -> decision -> risk -> intent -> order -> fill -> position -> exit
   (unchanged: every fact is written by the execution layer, as before)
                                  |
          paper ledger (instances, legacy engine, adaptive lab)
          PaperBrokerV2 + lab metadata (PA lab, SMC lab)
          SMC agent execution intents, decisions, reviews
          old journal.db (legacy, read only)
                                  |
                    services/journal_recorder.py  (+ journal_labs, journal_legacy)
                                  |
          trade_records.db: trade_records  decision_records  trade_record_events
                            trade_reviews  trade_notes  trade_record_corrections
                            weekly_reviews  improvement_proposals  review_runs
                                  |
          journal_stats (deterministic) -> Journal API -> Trades / Decisions /
          journal_reviews (trade + weekly)               Weekly Reviews / Memory / Notes
          journal_memory (provenance)
```

The recorder runs every 30 s (`HUB_JOURNAL_RECONCILE_S`), on startup, and
once more at shutdown; the pipeline and the forward-paper fill only wake it.
A pass is idempotent: it can run any number of times, after any crash.
A lab pass reads the broker's fills, orders and funding as one snapshot on its
own WAL connection, never through the connection (and lock) the broker's fills
and quotes use.

## Identity and integrity

| Guarantee | Mechanism |
|---|---|
| one execution → one record | `execution_key` UNIQUE (instance + session + order idempotency key, lab session + proposal, agent execution key); entry `trade_id` UNIQUE |
| no second record for the exit | exits update the entry's record; partial-exit legs are chained by the ledger's REDUCE execution row |
| duplicate decision / callback | the ledger's own idempotency (claimed alert id, fill key) + the keys above |
| restart / crash | records are rebuilt from durable facts; a crash between fill and journal is repaired by the next pass |
| journal write failure | the failed pass reports the error; the next pass writes the record |
| uncertainty | SMC agent `EXECUTION_UNCERTAIN` intents are visible records, never results |
| finalized facts immutable | trigger `trg_trade_records_immutable` aborts any change to a non-NULL fact column unless `correction_seq` advances. The application advances it only in `TradeRecordStore.correct()`, with a logged reason and actor; the trigger does not stop a raw SQL write that advances it too, and it does not guard `status`, `market_regime` or `verification`. `trg_trade_records_no_delete` forbids deleting a finalized record |
| unknown stays unknown | NULL, listed in `missing_json`; `data_completeness` FULL / PARTIAL / MINIMAL. An exit leg with an unknown P&L, fee or price leaves that result unknown. Totals sum the recorded values and say how many trades they cover (`pnl_known`, `r_known`); a profit factor needs every trade's P&L, and a drawdown with a gap is unknown |
| one lab position → one record | a lifecycle is paired from the broker's fills; when one entry order opens a second position (its remainder fills after the first closed), that position gets its own record (the order's key plus its first fill id) |
| lab costs | entry and exit commissions from the fills; funding from the broker's `v2_funding_events`, attributed by booking time to the position open when it was booked (all of one broker position's events together); net P&L is after both, and equals the broker balance change. Risk is the sum over the entry fills |
| a position that ends without an exit fill | an account restart (ledger rows `cancelled`), a logged paper reset (initial-capital change) or a replaced lab session ends its record CANCELLED with no exit price or result invented; a trade that had scaled out keeps its realized part (CLOSED, exit reason `account-restart`). A missing row alone, without that evidence, ends nothing |
| decisions | a decision links to a trade only once its order filled; an unfilled order is referenced (`source_ref.order_record_id`), not counted as a trade. Decisions, like trades, default to forward paper |
| dates | a bare date in an API filter is a London calendar day (every Journal time is shown in London time) |
| provenance | `record_origin` FORWARD_PAPER only with evidence of live data (a next-quote fill, or the decision's recorded live data source); SIMULATION on any simulated-data marker; otherwise LEGACY_MIGRATION. `verification` VERIFIED only when an execution fact backs the record |

Outcome: WIN / LOSS / BREAKEVEN (|realized R| ≤ 0.05) from net P&L and R; the
basis is stored on every record. CANCELLED / REJECTED / EXECUTION_FAILED /
EXECUTION_UNCERTAIN come from execution states, never from notes.

## Material decisions

Recorded: a strategy signal and its final state (from the decision stores),
lab proposals and their status, SMC agent decisions (repeated NOT_READY for
one setup collapse into one), and runs of data-blocked candles (one incident
per run). Not recorded: candles where nothing happened — the per-candle
archive keeps those.

## Reviews and learning

* Trade review (`journal_reviewer`, v1): rule-based quality, compliance,
  violations, mistakes, positives — its own table.
* Weekly review per scope: `smc_agent` (SMC lab + agent), `pa_agent` (PA
  lab), `instance_agent:<id>` (one per instance and strategy). Forward-paper
  records only. Validation → deterministic statistics → patterns → previous
  week → FACT / OBSERVATION / HYPOTHESIS / RECOMMENDATION, each with
  `journal_record_ids` → proposals (only at 20+ trades in the whole scope
  history, PENDING_APPROVAL) → saved once (UNIQUE agent, strategy, period,
  version) → memory marker.
* Scheduler: `review_runs` holds each (scope, week) claim; missed weeks (up to
  12) are recovered after a restart; a claim from a dead process is retried
  after 10 minutes. Weeks start `HUB_REVIEW_WEEK_START_DOW` (0 = Monday, UTC).
* Proposals: approving records a person's decision. Nothing edits a
  strategy, parameter, risk setting or source file.

## Memory

Verified memory is computed from FORWARD_PAPER records and opens to them.
The old `evolution_memory` counters are shown apart, labelled VERIFIED /
LEGACY / UNVERIFIED by how many increments still have a record, and never
enter forward-paper statistics.

## Known limits

* **Lab target shown:** `planned_take_profit` is the decision-time target. The
  lab broker re-anchors the target to the real fill and keeps that armed level
  on its position only, so the journal does not record it. `planned_rr` is the
  frozen R, which the armed target keeps.
* **An open lab trade** shows no fees, funding or realized P&L until it closes,
  although the broker has already charged them.
* **Records finalized before a fix keep their stored facts**, because finalized
  facts are immutable. This covers lab trades whose funding was recorded as 0
  and first positions whose second position was missing. A second position
  missed earlier gets its own record on the next pass. A first record whose legs
  were overwritten gets them back, and any stored fact that differs is logged as
  a DISCREPANCY. Changing a stored fact is a controlled correction, with a
  reason and an actor.
* **Supabase ledger mirror:** it never re-reads an open `paper_trades` row, so a
  remote deletion of an open trade would leave a stale copy. The app has no way
  to delete Supabase paper trades: there `reset_paper` is not available.
* **Weekly reviews** run on Monday-to-Monday UTC weeks (`HUB_REVIEW_WEEK_START_DOW`),
  as the Weekly page states, while times are shown in London time. Session
  labels are fixed UTC hour buckets.
* **Notes** list the newest 200.

* **Pass cost:** every pass re-reads each source's whole history and skips the
  finalized records: an idle PA lab pass took about 0.09 s at 2,000 trips and
  0.5 s at 10,000, off the execution path. Change-marker gates (re-read a
  source only when it changed) would make an idle pass constant-time.
* **Ledger reads** (the adaptive lab's ledger, and the main ledger on a
  SQLite deployment) still go through the ledger's own connection and lock;
  production's main ledger is read from the Supabase mirror, which execution
  does not use.
