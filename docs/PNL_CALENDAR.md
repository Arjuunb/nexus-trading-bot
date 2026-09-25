# App-wide realized P&L calendar

The Calendar answers one question: **where did the money come from, or go,
across the whole trading system?** It is a read-only analytics view over the
existing ledgers. It never writes to, merges or rewrites any of them.

Implementation: `automation-hub/services/pnl_calendar.py` (the only place P&L
is aggregated), exposed by `automation-hub/routers/calendar.py`, rendered by
`automation-hub-dashboard/src/pages/Calendar.tsx`. The frontend formats numbers;
it never adds them up.

## 1. Sources

| Source key | Where its closed trades live | Unit read | P&L as stored |
|---|---|---|---|
| `trading_instance` | main ledger `paper_trades`, `instance_id` set | one closed row | **net** of fees (`realized_pnl`, else `pnl`); `fees` stored beside it |
| `paper_trading` | main ledger `paper_trades`, `instance_id` empty (the legacy signal engine) | one closed row | net of fees |
| `adaptive_lab` | the Adaptive MTF Lab's own ledger file (`HUB_ADAPTIVE_LAB_DB`) | one closed row | net of fees |
| `pa_lab` | Price Action Lab `PaperBrokerV2`: the live broker for the active session, `pa_sessions.state_json` snapshots for ended sessions | one reducing fill | **gross** per fill (`realized_pnl`); `fee` per fill; funding in `pa_funding_events` |
| `smc_lab` | SMC Lab `PaperBrokerV2`, same layout (`smc_sessions`, `smc_funding_events`) | one reducing fill | gross per fill; fee per fill; funding separate |

The ledgers are read, never written, through their own interfaces:
`read_paper_trades()` returns every `paper_trades` row (SQLite in one query;
Supabase page by page, because a PostgREST select stops at the server's
max-rows cap, 1,000 by default, and would otherwise drop the oldest trades
without saying so). A ledger in read-only degraded mode is reported as a
source error, not as an empty history. The two labs are read by
`v2_lab_history()`, which uses each lab account's public session and broker
state plus read-only SELECTs, because the lab modules themselves are under a
source-hash freeze and are not modified.

### Deliberately not counted

| Table / store | Why |
|---|---|
| `agent_trades` (SMC agent journal) | Records the same SMC Lab executions a second time. |
| `trade_decision_journal` / events | Per-trade metadata keyed by the ledger trade id. Used only to read timeframe and exit reason; its `pnl` copy is never summed. |
| `positions`, `v2_positions`, `v2_orders` | Open state and intentions, not realizations. |
| `simulation_sessions.realized_pnl`, `instance_metrics`, account `realized_pnl` | Aggregates of the rows already counted. |
| `paper_trades` with `source='backtest'` | Backtests are not trading. Only `paper` and `live` rows count. |
| Research-mode instances | Their execution engine refuses to open positions, so they never have trades. |
| `shadow_*`, counterfactual tracker, `skipped_trades` | Hypothetical trades. |
| `adaptive_journal`, `cycle_reports` | Per-candle journals, not trades. |
| Adaptive Lab mirror mode | Reads an instance's own ledger for display; nothing is stored twice. |

## 2. Stable identity and de-duplication

Every realization has one identity string:

* ledger rows: `ledger:<paper_trades.id>` or `adaptive:<paper_trades.id>`;
* lab fills: `pa_lab:fill:<v2_fills.id>` / `smc_lab:fill:<v2_fills.id>`.

Fill and trade ids are random UUIDs written once. A lab's active session
appears both in the live broker and in its own snapshot, and a resumed session
restores its fills with their original ids; collecting by identity keeps the
first copy and drops the rest (the count of dropped copies is reported in the
API's `diagnostics`). Two different identities are never merged.

A **trade** (position) groups its realizations: for ledger rows the row is the
trade; for lab fills the trade is one position episode per symbol, from the
fill that opens it (flat to non-flat) to the fill that returns it to flat,
identified by the opening fill's id.

## 3. Money

* **Arithmetic** is `decimal.Decimal`, built from the stored value's string
  form; nothing is summed in floating point. API amounts are strings with eight
  decimal places; the UI rounds for display only.
* **Sign**: positive = profit, negative = loss, zero = break-even. Direction
  never flips a sign: every ledger stores direction-aware P&L
  (`(exit − entry) × size` for longs, `(entry − exit) × size` for shorts).
* **Net realized P&L** per realization:
  * ledger rows: the stored net value (already net of the round-trip fee);
    gross is reported as `net + fees`. Fees are **not** subtracted again.
  * lab fills: `gross − exit fee − entry-fee share − funding share`. The entry
    fill's fee and the position's funding are allocated to its exits in
    proportion to the quantity each exit closes. Funding amounts are stored
    positive when paid and negative when received, so a credit raises net P&L.
* **Gross profit** = sum of positive net realizations; **gross loss** is stored
  as a **positive magnitude** (`"40.00000000"`) and displayed with a minus sign.
* **Currency** is the settlement currency of each realization: labs declare it
  (`USDT`); ledger rows use the quote asset of the symbol (`BTCUSDT` → `USDT`),
  because their P&L is `(exit − entry) × size` in quote units. A symbol whose
  quote asset cannot be read gets `UNKNOWN`, kept separate.
* **No conversion.** No exchange-rate source exists in the application, so
  amounts in different currencies are never added together: every total is
  reported per currency. The display currency (the saved
  `portfolio.baseCurrency`, default `USDT`) is reported with
  `conversion.available = false` whenever any amount is in another currency.

## 4. Dates and time

* A realization belongs to the calendar date of its **close/realization
  timestamp** in the calendar timezone, never the entry date. A trade opened
  23 Sep 22:30 and closed 24 Sep 02:15 is on 24 Sep.
* Partial exits: lab fills realize at their own timestamps, so each exit lands
  on its own day. Ledger rows already record a partial close as its own closed
  row with its own close time, and that behaviour is preserved.
* **Timezone**: the `tz` query parameter, else the user's saved
  `region.timezone`, else `profile.timezone`, else `UTC`. An unknown zone is a
  400, not a silent fallback.
* **Time of day** (defined once, in `TIME_OF_DAY`): Morning 07:00–11:59,
  Afternoon 12:00–16:59, Evening 17:00–21:59, Night 22:00–06:59. For a given
  date, Night covers 00:00–06:59 and 22:00–23:59 of that date. Hourly totals are
  also returned.

## 5. Counting and rates

* `closed_trades` counts trades whose final realization falls in the period.
* A trade is a **win** if its total net P&L (all its realizations) is > 0, a
  **loss** if < 0, **break-even** if exactly 0. It is counted on the day of its
  final close.
* `realizations` also counts partial exits of positions still open; their P&L
  is in the day's total, but they are not closed trades yet.
* `win_rate` = wins ÷ closed trades, `null` when there are none.
* **Per-trade figures** (`avg_win`, `avg_loss`, `largest_win`, `largest_loss`,
  `expectancy`) use each closed trade's **total** net P&L over all its exits,
  so a trade scaled out across two days is one result, not two. Losses are
  positive magnitudes. `expectancy` is the mean over all closed trades,
  break-evens included.
* **Profit factor** is the period's gross profit ÷ gross loss (the two figures
  shown beside it). It is `null` when there was no loss: undefined, not
  infinite. The page shows "No losses".
* **Month only:** `winning_days` / `losing_days` / `breakeven_days` count
  trading days by their net; the longest winning and losing streaks count
  consecutive *trading* days (a day without trades neither extends nor breaks
  a streak); `cumulative` is the running realized total after each trading
  day, computed here so the chart never adds amounts itself.
* **Weeks:** the month view also returns one row per Monday-first week of the
  grid. A week's totals count only its days inside the month, so the weekly
  column always adds up to the month.

## 6. Drawdown

Daily max drawdown, per currency:

1. Start from beginning-of-day realized equity (taken as 0: the amount of a
   peak-to-trough decline does not depend on the starting level).
2. Apply each realization's net P&L in chronological order (by realization
   time, then identity for ties).
3. Track the running peak, starting at the beginning-of-day level.
4. Drawdown at each step = peak − current.
5. Max drawdown = the largest of those, reported as a positive magnitude.

The largest losing trade is **not** the drawdown: two losses in a row after a
win produce a drawdown larger than either loss. The monthly figure applies the
same rule to the month's chronological sequence.

## 7. Missing metadata

Nothing is invented. Fields the source did not record come back as `null` and
are listed in the realization's `missing` array; the UI shows "Not recorded".
A trade whose instance has since been deleted keeps its instance id and is
labelled "Deleted instance". Strategy comes from the trade's own `strategy_id`
(ledger) or its order metadata (labs), never from an instance's *current*
configuration, which can change between trades. Timeframe and exit reason for
ledger trades come from the decision journal entry written when the trade was
taken.

## 8. Unrealized P&L

Open positions never enter the calendar. The API reports only how many are
open per source, labelled as unrealized.

## 9. API

All three endpoints sit behind the normal session (or `X-Webhook-Secret`)
authentication and are read-only.

| Endpoint | Returns |
|---|---|
| `GET /calendar/month?year=&month=` | every day of the month (state, per-currency money, closed trades, realizations) and the month summary per currency, with best/worst day and max drawdown |
| `GET /calendar/day?date_=YYYY-MM-DD` | the day summary, source / strategy / time-of-day / hourly breakdowns, and every realization with its gross, fees, funding, net, R/R and missing fields |
| `GET /calendar/options` | the filter values that exist in the data, plus the resolved timezone and display currency |
| `GET /calendar/export.csv?start=&end=` | every realization closed in that date range (inclusive, calendar timezone, at most 400 days) as CSV, one row per exit, oldest first, with exact amounts in each row's own currency. Text cells that begin with `=`, `+`, `-` or `@` are prefixed with `'` so a spreadsheet cannot run them as formulas |

Common query parameters: `tz` (IANA zone), `currency` (display currency),
`source`, `instance`, `strategy`, `symbol`, `timeframe`, and `fresh=true` to
re-read every ledger instead of the 15-second cache. Every response carries
`diagnostics` (per-source read status, open-position counts, duplicates
dropped, collection time) and `conversion` (whether amounts in other
currencies were left unconverted, and why). A source that cannot be read is
reported there by name; the page shows it rather than presenting a partial
total as complete.

## 10. Tests

* `automation-hub/tests/test_pnl_calendar.py`: sign convention, fees and
  funding (no double subtraction), partial exits, currencies and precision,
  timezones and midnight, drawdown, duplicates, filters, legacy metadata, the
  paged Supabase read and the degraded-ledger error, per-trade statistics,
  weeks, the running total and streaks, the CSV export, and the API.
* `automation-hub-dashboard/e2e/calendar.spec.ts`: sidebar placement and
  routing, month and day views, weekly totals, statistics, the chart, CSV
  export, keyboard navigation, filters, error and partial-source states, phone
  layout and reduced motion. Its mocked
  responses (`e2e/fixtures/calendar.json`) are produced by the real service
  over real engine trades; `e2e/fixtures/generate_calendar_fixture.py`
  regenerates them.
