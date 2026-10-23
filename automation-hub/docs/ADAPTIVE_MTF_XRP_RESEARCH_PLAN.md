# Adaptive MTF XRP research plan — prepared, not executed

**22 trades / 40.9% win rate / PF 1.13 / +$0.84 net P&L: UNVERIFIED.**
There are no authorized production records proving this exact cohort here.
No numerical results, old parameters, costs or history are backfilled.
This plan is separate from release readiness and does not delay packaging.

## Freeze before research

Strategy `adaptive_trend_pullback`, source version `1.0.0`; XRPUSDT,
Binance USDⓈ-M perpetual. Preserve the actual strategy rules and risk settings.
Source defaults currently declare 5m entry, 15m pullback, 1h trend/regime and
optional 4h context, EMA 20/50, ATR/ADX 14 and per-timeframe minimums. These are
**source observations, not the original reported run's effective settings**.

Require a contemporaneous immutable effective-configuration snapshot/fingerprint,
instance/account ID, execution mode, exact start/end, dataset revision/retained
bytes, venue, source/build/classifier versions and original cost model.
Original fingerprint, overrides, timeframe/risk settings, account, date range,
dataset and fees/funding/slippage are currently UNKNOWN. Current UI settings
cannot reconstruct them. Keep missing facts unknown and block verification.

## Methodology using existing services

1. Obtain separately authorized read-only original ledger/journal/evidence and
   completed episode legs with entries, partial exits, closure, quantities,
   actual risk, fees and funding. Reconcile episode P&L with authoritative fills;
   one final closed position episode is one statistical trade. No ledger writes.
2. Freeze the baseline and retained dataset. Verify half-open UTC boundaries,
   complete eligible candles, strategy-specific reviewed warm-up, closed HTF
   context and data availability. Warm-up produces no evaluated trades.
3. Submit an explicitly approved bounded campaign through existing durable
   workers. Separate historical, chronological validation/final holdout,
   anchored/rolling frozen-config walk-forward and any future-paper evidence.
   Record holdout/search history across campaigns; missing provenance is UNKNOWN.
4. Record exact observed/modelled fee, funding, spread/slippage assumptions.
   Stress realistic scenarios separately. Unknown funding is never zero and
   blocks verified net-profit claims. Do not assert optimisation occurred.
5. Report episode count, win rate, PF, gross/fees/funding/net, R expectancy,
   drawdown, concentrated returns, regime/session groups and window consistency.
   Use seeded episode Monte Carlo with temporal/exchangeability limitations;
   distributions are hypothetical research, not realized trading performance.
6. Apply existing versioned qualification/comparison policy PASS/FAIL/UNKNOWN.
   Insufficient cost/independence/sample evidence blocks qualification. Preserve
   failed experiments and immutable result/decision references.

Do not optimise repeatedly on the same holdout. A human-reviewed paper-trading
trial requires a separate explicit authorization, isolated existing account and
complete costs/lineage. This plan does not start it, promote a candidate, place
orders or claim future profitability. Needed access: exact authoritative
contemporaneous evidence plus the original immutable dataset, not new settings.
