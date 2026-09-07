# Native multi-timeframe policy

The executable source of truth is `automation-hub/services/mtf_policy.py`.
Trading Instances, Decision Brain, Adaptive MTF, Price Action, SMC and the
research observer consume that module; clients display its returned contract
instead of maintaining another timeframe table.

| Entry candle | Primary HTF (entry gate) | Secondary HTF (bias only) |
| --- | --- | --- |
| `1m` | `15m` | `1h` |
| `5m` | `1h` | `4h` |
| `15m` | `1h` | `4h` |
| `1h` | `4h` | `1d` |
| `4h` | `1d` | none |

All three clocks are native Binance USD-M kline subscriptions from the shared
forward-paper market-data hub. Production decisions do not derive an HTF by
sampling or grouping entry candles.

For a decision at time `T`, an HTF candle is eligible only when its provider
open timestamp plus its native duration is less than or equal to `T`. The
forming candle never votes. Every decision stores, for both configured roles,
`htf_timeframe`, `htf_candle_id`, `htf_close_timestamp`, and `htf_bias`.

The expected 5m operator label is:

`Entry 5m · HTF 1h closed · Bias 4h closed`

`waiting` replaces `closed` until the corresponding native feed has supplied a
causally eligible candle. Missing primary data blocks the production decision;
missing secondary data remains an explicit unavailable bias and does not become
a second entry gate. Neither role is ever filled with a synthetic timeframe.

Legacy resampling helpers remain callable only through explicit test/research
flags so historical fixtures can be reproduced. They are not a REAL_PAPER
input.
