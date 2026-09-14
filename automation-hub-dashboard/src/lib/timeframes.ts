/** One source of truth for timeframe choices.
 *
 * These lists were hardcoded separately in seventeen components and had
 * drifted: several strategy surfaces offered only 15m and above, so a 5m
 * entry — the timeframe most of this system's live trading actually runs on —
 * could not be selected at all, and Evolution had no selector whatsoever.
 *
 * The set matches what the backend can actually serve. `TF_MS` in
 * automation-hub/data/market_data_v2.py is the binding constraint: the replay
 * engine understands 2h/6h/12h/1w as well, but the data layer cannot fetch
 * them, so offering them would surface as an empty chart rather than a chart.
 * Keep this in step with that map.
 */

/** Every timeframe the data layer can fetch, ascending. */
export const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"] as const;

export type Timeframe = (typeof TIMEFRAMES)[number];

/** Timeframes a signal is executed on. Excludes 1d, which is a bias frame. */
export const ENTRY_TIMEFRAMES: readonly Timeframe[] =
  ["1m", "3m", "5m", "15m", "30m", "1h", "4h"];

/** Higher timeframes used for bias and confirmation, not entries. */
export const BIAS_TIMEFRAMES: readonly Timeframe[] = ["15m", "1h", "4h", "1d"];

/** The default entry timeframe for a new strategy, backtest or experiment.
 *  Previously 4h across most surfaces, which is far above where this system
 *  trades and meant a new strategy was analysed on a frame nobody used. */
export const DEFAULT_ENTRY_TIMEFRAME: Timeframe = "15m";

/** The default bias/confirmation frame that sits above the entry. */
export const DEFAULT_BIAS_TIMEFRAME: Timeframe = "4h";

/** Seconds per timeframe — mirrors bot/data/resample.py TF_SECONDS. */
export const TF_SECONDS: Record<Timeframe, number> = {
  "1m": 60, "3m": 180, "5m": 300, "15m": 900,
  "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400,
};

/** True when `bias` is strictly higher than `entry`. A bias frame at or below
 *  the entry frame gives no higher-timeframe context at all. */
export function isHigherTimeframe(bias: string, entry: string): boolean {
  const b = TF_SECONDS[bias as Timeframe];
  const e = TF_SECONDS[entry as Timeframe];
  return Boolean(b && e && b > e);
}
