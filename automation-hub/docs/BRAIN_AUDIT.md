# Bot Brain Audit — why most simulated trades lose

Measured, not guessed. All numbers below come from running the **real**
`strategies/custom.simulate()` over the bundled BTCUSDT history
(`data.market_data.get_bars`). Reproduce with `python -m pytest
automation-hub/tests/test_trade_brain.py` or the snippet in this PR description.

## What I inspected

`strategies/custom.py` (rule eval + `simulate()` + `_results()`),
`strategies/custom_adapter.py` (live bridge), `webhook_api.py`
(`/strategy/custom/simulate`), `services/regime.py`,
`services/market_quality.py`, `bot/data/indicators.py`.

## Measured loss causes (root causes, ranked)

Typical user-built strategies, simulated on real bars:

| Strategy                              | Trades | Win% | PF   | Net R |
|---------------------------------------|-------:|-----:|-----:|------:|
| short breakout-down only (counter-trend) | 25  | 20%  | 0.36 | −13.3 |
| breakout + RR 3.0 + 1.0×ATR stop      |   41   | 17%  | 0.58 | −15.2 |
| Bollinger below-lower long (buy dips) |   38   | 42%  | 1.03 |  +0.8 |
| breakout-20 only (no confirmation)    |   36   | 44%  | 1.13 |  +2.7 |
| EMA20>50 only (trend aligned)         |   75   | 51%  | 1.46 | +17.5 |

The losers share concrete, fixable causes:

1. **No market-regime gate in the simulator.** `simulate()` evaluates only the
   user's entry tree. The engine already has `RegimeDetector` (efficiency ratio
   + ATR%) and `RegimeGate`, but the simulator never calls them, so trend
   setups fire in chop and counter-trend setups fire against a strong trend.
2. **Historical finding: no higher-timeframe confirmation.** REAL_PAPER now
   consumes the shared provider-native primary HTF gate; legacy aggregation is
   available only behind an explicit research/test flag.
3. **No trade-quality scoring.** Every matched signal is taken regardless of
   location, RR realism, volatility, momentum or stop safety. There is no
   "is this setup actually good?" filter.
4. **Greedy reward:risk with tight stops.** RR 3.0 on a 1.0×ATR stop collapses
   win rate to ~17%; nothing warns or blocks at trade time.
5. **Fixed stop/target exits only.** No break-even, no trailing, no time stop —
   winners that reach +1.4R round-trip back to a full −1R loss.
6. **No "avoid bad trades" accounting.** Blocked/avoided setups are invisible,
   so there's no way to see that *not* trading was the right call.

Costs/realism are actually fine: `simulate()` already applies fee+slippage
correctly (cost in R = `cost·entry·2/risk`), uses conservative same-bar
stop-before-target fills, enters at bar close, and checks exits only on later
bars (no lookahead). So realism is **not** the problem — decision quality is.

## The fix (this PR) — brain first, UI second

- `strategies/brain.py` — a pure, testable **TradeBrain**: regime fit,
  higher-timeframe alignment (native provider candles in REAL_PAPER),
  market structure, volatility band, volume, momentum, RR quality, distance to
  support/resistance, stop safety, and a losing-streak penalty → a **0–100
  trade-quality score** plus hard blocks, each with a reason.
- `simulate()` gains an opt-in `brain`/`min_score`: sub-threshold or blocked
  setups are skipped and recorded in a **blocked-trade log** with the reason,
  regime and score. Taken trades carry their score, regime, HTF bias and the
  rule pass/fail checklist.
- Improved exits (opt-in via `spec["exit"]`): break-even after +1R, ATR
  trailing, time stop.
- `strategies/diagnosis.py` — a post-simulation **diagnosis report**
  (loss-reason breakdown, worst regime/session, overtrading & chop detection,
  stop-hit pattern, RR weakness, recommendations).
- Upgraded metrics: expectancy, Sharpe, recovery factor, avg hold time,
  long-vs-short split (existing keys preserved).
- `strategies/optimize.py` — careful **train/test optimisation** of only the
  brain knobs (min score, RR, ATR stop). It validates on an unseen slice and
  reports `overfit` unless out-of-sample also improves — never sells train-set
  numbers as reliable.
- The `/strategy/custom/simulate` endpoint enables the brain by default and
  returns `diagnosis` + `blocked`. The React Simulation page surfaces the
  score, regime, blocked trades, rule checklist and loss-reason breakdown.

Nothing existing is removed; the raw `simulate()` behaviour is unchanged when
no brain is passed, so the current test suite stays valid.
