// Response shapes of /calendar/month, /calendar/day and /calendar/options
// (automation-hub/routers/calendar.py). Every amount is an exact decimal
// string in the currency it is keyed by; the page formats, never sums.

export type DayState = "none" | "profit" | "loss" | "breakeven" | "mixed";
export type SourceKey = "trading_instance" | "pa_lab" | "smc_lab" | "adaptive_lab" | "paper_trading";

export interface Money {
  net: string;
  gross_profit: string;
  /** Positive magnitude. */
  gross_loss: string;
  fees: string;
  /** Positive = paid, negative = received. */
  funding: string;
  closed_trades: number;
  realizations: number;
  wins: number;
  losses: number;
  breakeven: number;
  /** Percent, or null when no trade closed. */
  win_rate: string | null;
  /** Positive magnitude. */
  max_drawdown: string;
  state: "profit" | "loss" | "breakeven";
  /** Gross profit / gross loss of the period; null when there was no loss. */
  profit_factor: string | null;
  /** Per closed trade, using each trade's total net over all its exits. */
  avg_win: string | null;
  avg_loss: string | null;          // positive magnitude
  largest_win: string | null;
  largest_loss: string | null;      // positive magnitude
  expectancy: string | null;
  // month summaries only
  best_day?: { date: string; net: string } | null;
  worst_day?: { date: string; net: string } | null;
  trading_days?: number;
  winning_days?: number;
  losing_days?: number;
  breakeven_days?: number;
  longest_winning_streak?: number;
  longest_losing_streak?: number;
  /** Running realized total after each trading day, computed by the backend. */
  cumulative?: { date: string; net: string; cumulative: string }[];
}

export type ByCurrency = Record<string, Money>;

export interface SourceStatus {
  ok: boolean;
  error?: string;
  realizations?: number;
  open_positions?: number;
  skipped?: Record<string, number>;
}

export interface Diagnostics {
  sources: Record<string, SourceStatus>;
  duplicates_dropped: number;
  collected_at: string;
}

export interface Conversion {
  display_currency: string;
  needed: boolean;
  available: boolean;
  unconverted: string[];
  note: string;
}

export interface MonthDay {
  date: string;
  state: DayState;
  by_currency: ByCurrency;
  closed_trades: number;
  realizations: number;
}

/** One Monday-first row of the grid; totals count only its days in the month. */
export interface MonthWeek {
  start: string;
  from: string;
  to: string;
  state: DayState;
  by_currency: ByCurrency;
  closed_trades: number;
  realizations: number;
}

export interface MonthResponse {
  year: number;
  month: number;
  days: MonthDay[];
  weeks: MonthWeek[];
  summary: ByCurrency;
  currencies: string[];
  timezone: string;
  filters: Record<string, string>;
  diagnostics: Diagnostics;
  conversion: Conversion;
}

export interface Breakdown {
  key: string[];
  by_currency: ByCurrency;
  closed_trades: number;
  realizations: number;
  avg_rr: string | null;
}

export interface TimeBucket {
  key: string;
  label: string;
  start: string;
  end: string;
  realizations: number;
  by_currency: ByCurrency;
}

export interface HourRow {
  hour: number;
  bucket: string;
  realizations: number;
  by_currency: ByCurrency;
}

export interface Trade {
  id: string;
  trade_id: string;
  source: SourceKey | string;
  source_label: string;
  account: string;
  currency: string;
  closed_at: string;
  opened_at: string | null;
  gross: string;
  fees: string;
  funding: string;
  net: string;
  pnl_basis: string;
  symbol: string | null;
  side: "long" | "short" | null;
  instance_id: string | null;
  instance_name: string | null;
  strategy: string | null;
  timeframe: string | null;
  entry_price: string | null;
  exit_price: string | null;
  quantity: string | null;
  rr: string | null;
  rr_basis: string | null;
  exit_reason: string | null;
  final: boolean;
  partial: boolean;
  missing: string[];
  duration_s: number | null;
  outcome: "win" | "loss" | "breakeven" | null;
}

export interface DayResponse {
  date: string;
  summary: ByCurrency;
  currencies: string[];
  state: DayState;
  sources: Breakdown[];
  strategies: Breakdown[];
  time_of_day: TimeBucket[];
  hourly: HourRow[];
  trades: Trade[];
  timezone: string;
  filters: Record<string, string>;
  diagnostics: Diagnostics;
  conversion: Conversion;
}

export interface OptionsResponse {
  sources: { key: SourceKey; label: string; trades: number }[];
  instances: { id: string; name: string | null; source: string }[];
  strategies: string[];
  symbols: string[];
  timeframes: string[];
  diagnostics: Diagnostics;
  timezone: string;
  display_currency: string;
}

export interface CalendarFilters {
  source: string;
  instance: string;
  strategy: string;
  symbol: string;
  timeframe: string;
}

export const EMPTY_FILTERS: CalendarFilters = { source: "", instance: "", strategy: "", symbol: "", timeframe: "" };
