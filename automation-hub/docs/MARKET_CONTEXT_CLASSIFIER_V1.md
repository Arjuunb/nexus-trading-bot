# Market context classifier 1.0.0

`services/market_context_classifier.py` is a pure research classifier. It does
not read accounts, fetch prices, construct strategies, apply entry gates or
place orders. The existing `services/regime.py` execution gate and all strategy
indicators retain their existing behavior. Classification failure describes
unknown research context; it cannot authorize or reject an execution.

## Identity and reproduction

The composite classifier ID is `market_context_classifier`; the initial version
is `1.0.0`. Its definition contains the full effective indicator periods,
thresholds, history selection, publication and stale policies, session
definitions, precedence, mathematical conventions and SHA-256 hashes of the
installed IANA TZif files used by those sessions. `parameter_hash` is the
existing canonical configuration SHA-256 applied to `parameters`.

The journal registry permanently binds a classifier ID/version to one parameter
hash. Changing any threshold, session boundary or timezone rule requires a new
explicit classifier version. Versions with major number 1 use the formulas
below; a later major algorithm is not accepted by this implementation. Unknown
or noncanonical definitions are rejected, rather than replaced with current
defaults. A changed timezone database blocks frozen replay: restore the original
TZif environment or create an explicit new research version. Restart processes
after timezone upgrades; Python's timezone cache is process-local.

Each result embeds `classification_input`: the original definition, causal
cutoff, source, clocks and bounded candle OHLCV/availability facts, including
original provenance failures. `input_data_hash` hashes that detached material.
It excludes calculation time and excludes future candles. Mutable historical
caches are therefore unnecessary to reproduce a committed valid classification.
`classify_frozen_context` validates the original definition and repeats its
calculation. Classification time is an actual separately supplied aware clock;
it is not evidence of historical candle availability.

## Point-in-time selection

The project's `Bar.timestamp` is candle **open**. For timeframe duration `D`,
the exclusive close is `open + D`. An explicit Binance inclusive close may be
one millisecond earlier; it is normalized to that exclusive close. Other close
boundary differences and unaligned opens are rejected.

An input is eligible only when:

- its exclusive close plus configured minimum publication delay is at or before
  the original signal cutoff;
- its original actual `available_at` is at or before that cutoff and at or after
  its exclusive close;
- `is_closed` is explicitly true and OHLCV is finite and structurally valid;
- provided source-quality metadata is `VERIFIED`.

Closure alone never invents publication. Plain Bars without observed
availability remain unknown. Future, forming and later-published inputs are
excluded before reading numeric OHLCV. Out-of-order input is sorted. Identical
repeated observations have one effect, retaining the earliest proved receipt;
changed facts at one open time conflict. Original observer provenance failures
remain unknown even after bounded-history selection.

Forward-paper cutoff is the actual signal-generated observation clock captured
once by the integration. The legacy signal candle open and theoretical decision
close are retained as separate fields. Deferred fills retain the original frozen
signal input; their later execution clock does not change context. Historical
replay without original publication evidence remains unknown.

Selected history is the latest fixed required number of distinct opens. Entry
requires `max(EMA period + slope lag, 2*ADX period + 1,
ATR period + reference window + 1)`; defaults require **115** candles. Higher
context requires `max(HTF EMA period + HTF slope lag, ATR period + 1)`; defaults
require **51** candles. This fits existing native-context fetches without
changing their requests or entry requirements. Older input beyond the indicator
window cannot affect its EMA seed or ATR percentile.

## Sessions

Defaults apply all seven weekdays because cryptocurrency markets remain open on
weekends. Each definition supports an explicit subset of local weekdays.
Intervals include their start and exclude their end.

| Session | Local timezone | Local start | Local end |
|---|---|---|---|
| ASIA | Asia/Tokyo | 09:00 | 17:00 |
| LONDON | Europe/London | 08:00 | 17:00 |
| NEW_YORK | America/New_York | 08:00 | 17:00 |

When London and New York are both active the one canonical classification is
`LONDON_NEW_YORK_OVERLAP`. Otherwise priority is New York, London, Asia, then
`OFF_SESSION`. `active_sessions` retains additional tags, including seasonal
Asia/London overlap, without giving one episode multiple canonical groups.

UTC instants convert through IANA timezone rules, so London and New York DST
transitions and their different transition weeks are independent. An overnight
session such as 22:00–02:00 uses the local weekday on which it starts. Ambiguous
autumn local clock readings derive unambiguously from UTC; nonexistent spring
wall-clock readings never occur during conversion.

## Trend formula

Reuse the existing first-close-seeded EMA and existing Wilder ADX implementation
without modifying either. Entry EMA period is 20, slope lag is three candles,
ADX period is 14 and ATR period is 14. ATR is the project's **simple moving
average** of true range, not a newly introduced Wilder ATR:

`TR_i = max(high_i-low_i, abs(high_i-close_(i-1)), abs(low_i-close_(i-1)))`

`ATR_t = mean(last 14 TR)`

`slope = (EMA_t - EMA_(t-3)) / (3 * ATR_t)`

Price bias is the sign of `close_t - EMA_t`. HTF direction is positive only
when both `HTF EMA_t - HTF EMA_(t-1)` and `HTF close_t - HTF EMA_t` are positive,
negative only when both are negative, and otherwise neutral. HTF EMA period is
50. The separate one-candle HTF lag preserves compatibility with accepted native
history lengths.

| Output | Exact rule, applied in this order |
|---|---|
| UNKNOWN | Required context/quality is invalid, zero ATR or nonfinite derived indicators |
| RANGE | ADX < 20 or absolute normalized slope < 0.05; also conflicting price/slope bias |
| STRONG_BULL | Positive price/slope bias, ADX >= 25, slope >= 0.20 and positive HTF direction |
| BULL | Positive price/slope bias after RANGE check, without all strong criteria |
| STRONG_BEAR | Negative price/slope bias, ADX >= 25, slope <= -0.20 and negative HTF direction |
| BEAR | Negative price/slope bias after RANGE check, without all strong criteria |

`trend_strength` and `adx_value` both expose the actual ADX in [0,100].
`ema_normalized_slope`, EMA and HTF direction make the rules explainable. These
defaults are descriptive research boundaries, with no optimization claim.

## Volatility formula

Normalize each historical ATR by that candle's close: `a_i = ATR_i / close_i`.
Use the **previous 100** available observations, excluding the current candle.
The percentile of current `a_t` uses midrank ties:

`percentile = 100 * (count(a_i < a_t) + 0.5 * count(a_i == a_t)) / 100`

| Output | Percentile |
|---|---|
| LOW | < 25 |
| NORMAL | >= 25 and < 75 |
| HIGH | >= 75 and <= 95 |
| EXTREME | > 95 |
| UNKNOWN | Required history/quality invalid or current ATR is zero/nonfinite |

Historical zero ATR observations are allowed; current zero ATR cannot support
normalized trend strength. Outliers are retained rather than clipped. Positive
price-scale changes preserve the normalized percentile. The rolling reference
calculation is linear and uses the same true-range definition as existing ATR.
Financial metrics retain Decimal calculation elsewhere; these indicator floats
are price context, never financial ledger writes.

## Quality and limitations

The combined context quality is `VALID`, `INSUFFICIENT_HISTORY`, `STALE_DATA`,
`MISSING_HTF`, `GAPPED_CANDLES` or `UNKNOWN`. Missing source/publication,
contradictory close metadata, explicit unknown provenance and conflicts take
UNKNOWN precedence. An available last close older than 1.5 timeframe intervals
is stale. Any noncontiguous open in the required history is gapped; no candle is
filled or interpolated. Incomplete required data leaves both trend and volatility
UNKNOWN. Session classification can still be known from a valid signal clock.

`structure_regime` remains `UNKNOWN`; complex structure inference is experimental
and is not obtained by changing strategy logic. Research context quality is
separate from authoritative evidence completeness, costs and sample confidence.
Valid context alone cannot verify profitability.

Tests cover boundaries, DST transition weeks/folds/gaps, overnight start weekdays,
trend and volatility thresholds, source failures, conflicting delivery, exact
frozen replay, late publication, HTF alignment, insufficient/gapped/stale data,
zero and extreme inputs, parameter/version isolation and the mandatory five
candle C3 look-ahead regression.

## Isolated benchmark

One hundred local synthetic benchmark iterations with 1,000 entry and 250 HTF
candles retained 115 and 51 candles respectively. Median classification was
**4.96 ms**, p95 **5.19 ms**, maximum **5.78 ms**, and serialized output was
**40,744 bytes** on this cloud workspace. This is an implementation measurement,
not authoritative trading performance or a production latency guarantee. Input
receipt sorting dominates the bounded calculation; no database or network call
occurs. Raw measurements: `/tmp/nexus-sprint2-classifier-benchmark.json`.
