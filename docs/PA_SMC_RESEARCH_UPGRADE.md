# PA/SMC Research Upgrade Operator Addendum

This layer measures which existing PA/SMC facts survive costs. It does not
promote, tune, or execute a strategy. Live exchange routing remains disabled.

## Execution boundaries

- Trading Instances remain `FORWARD_PAPER`.
- PA and SMC remain `SIGNALS_ONLY` until an operator explicitly applies their
  existing isolated-forward-paper mode.
- A separate `shadow_research.db` owns `shadow_decisions`, `shadow_orders`,
  `shadow_fills`, `shadow_outcomes`, `shadow_mae_mfe`, and `shadow_funding`.
- Shadow tables contain no cash account, real position, margin reservation, or
  engine-capacity state.
- One source candle and feature projection is hashed once and shared by every
  A–I variant. Recomputing market history per variant is prohibited.
- Shadow and paper intents require a public quote strictly later than the
  decision. Quotes that regress in event time/sequence are ignored.

## Context definitions

Named liquidity origins are kept distinct: previous day/week, London-clock
sessions, confirmed major swings, optional equal highs/lows, and PA zones.
Equal-liquidity detection is disabled unless its ATR tolerance is explicitly
configured and frozen in the configuration hash.

Sessions use `Europe/London`: Asia 00:00–07:00, London 07:00–13:00,
London/New York overlap 13:00–16:00, New York 16:00–21:00, otherwise out of
session. UK daylight saving is applied by `zoneinfo`.

HTF context follows the shared native map in `docs/NATIVE_MTF_POLICY.md`.
For a `5m` decision it records both closed Binance USD-M `1h` primary-gate and
`4h` secondary-bias candles. At decision time T, each selected HTF
`close_timestamp` must be less than or equal to T. Decision bars are never
grouped or sampled to manufacture HTF candles.

## Costs and validation

Market buys reference ask and sells reference bid. Slippage is applied after
that executable-side quote. Spread is retained as attribution but is not
subtracted again. Net result is gross minus commission, slippage and funding.

UI states are limited to `INSUFFICIENT_SAMPLE`, `PROMISING`, `NO NET EDGE`, and
`HARMFUL`. The deterministic minimum sample is 100 closed outcomes. Ranking
uses expectancy, profit factor, maximum drawdown, stability, and sample size;
win rate is not a ranking input.

## Operations

Set `HUB_SHADOW_RESEARCH_DB` only if the default data-directory location is not
appropriate. The runtime is read-only at `/research/observatory/status`,
`/registry`, `/measurements`, and `/comparison`.

Arm at most one existing real-paper strategy per engine through the established
PA or SMC configuration controls. Do not arm a variant: A–I are permanently
shadow observations. No live venue certification is claimed by this upgrade.

The SQL migration is additive. It does not delete, vacuum, transform, or mix
existing PA/SMC journals and paper balances.

## Deterministic acceptance trace

`test_same_closed_candle_and_next_quote_create_three_isolated_positions`
uses source candle `BINANCE_USDM:BTCUSDT:5m:1788352200000` (BTCUSDT 5m,
open `2026-09-02T12:30:00Z`, close/decision `12:35:00Z`) for the Instance,
PA, and SMC consumers. The durable Instance intent identity is that candle
`alert_id`; all three books remain empty at decision time and fill only on the
shared public quote received at `12:35:00.184Z`. The resulting execution
classes are Instance `REAL_PAPER`, PA `REAL_PAPER`, and SMC `REAL_PAPER`, in
three distinct ledgers. A SHADOW observation over the same lineage records
separate deterministic decision IDs and never touches those books.

`test_shadow_rejection_is_followed_to_cost_adjusted_outcome` traces a real
research counterfactual: variant C records `HTF_MISALIGNED`, creates only a
`SHADOW_REJECTED_INTENT`, waits for the next public quote, records the fill,
MAE/MFE-compatible outcome and net R, and retains the original blocker. It has
no paper-account or position table to mutate.

## Migration and rollback

`0002_shadow_research.sql` creates only the seven `shadow_*` tables and their
indexes. Runtime startup uses the same additive `CREATE TABLE IF NOT EXISTS`
contract. Existing PR #6 PA/SMC databases require no rewrite. To disable the
observer, stop the service and omit its startup registration; preserve the
shadow database as evidence. Do not merge it into a PA, SMC, or Instance
ledger and do not use it to restore balances.
