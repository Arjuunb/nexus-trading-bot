// Canonical Trade Journal client helpers (/journal/v2/*).
//
// The backend computes every statistic; these helpers only build query
// strings and format values for display. Missing values render as "—" and are
// never replaced with a guess.

export type JournalFilters = {
  modes: string[];            // [] = let the server choose ONE mode; ["ALL"] = every mode
  date_from: string; date_to: string;
  strategy: string; instance_id: string; lab: string; symbol: string;
  direction: string; result: string; session: string; timeframe: string;
  leverage_min: string; leverage_max: string; rr_min: string; rr_max: string;
  pnl_min: string; pnl_max: string; rule_violation: string; exit_reason: string;
};

export const EMPTY_FILTERS: JournalFilters = {
  modes: [], date_from: "", date_to: "", strategy: "", instance_id: "", lab: "", symbol: "",
  direction: "", result: "", session: "", timeframe: "", leverage_min: "", leverage_max: "",
  rr_min: "", rr_max: "", pnl_min: "", pnl_max: "", rule_violation: "", exit_reason: "",
};

export function filterQuery(f: JournalFilters, extra: Record<string, string> = {}): string {
  const qs = new URLSearchParams();
  if (f.modes.length) qs.set("modes", f.modes.join(","));
  for (const [key, value] of Object.entries(f)) {
    if (key === "modes" || value === "" || value == null) continue;
    qs.set(key, String(value));
  }
  for (const [key, value] of Object.entries(extra)) if (value !== "") qs.set(key, value);
  return qs.toString();
}

export function activeFilterCount(f: JournalFilters): number {
  return Object.entries(f).filter(([k, v]) => k !== "modes" && v !== "").length;
}

export type JournalTrade = {
  trade_id: string; trade_ref: string; source_system: string; order_id: string | null;
  execution_id: string | null; position_id: string | null; instance_id: string | null;
  instance_name: string | null; instance: string | null; bot_id: string | null; lab_id: string | null;
  strategy_id: string | null; strategy_name: string | null; strategy_family: string | null;
  strategy_version: string | null; trade_source: string | null; trading_mode: string;
  exchange: string | null; market_type: string | null; symbol: string; base_asset: string | null;
  quote_asset: string | null; direction: "LONG" | "SHORT"; timeframe: string | null;
  htf_timeframe: string | null; status: string;
  signal_at: string | null; order_created_at: string | null; entry_filled_at: string | null;
  requested_entry_price: number | null; entry_price: number | null; entry_slippage: number | null;
  entry_slippage_cost: number | null; quantity: number | null; notional_value: number | null;
  margin_used: number | null; leverage: number | null; leverage_source: string | null;
  account_balance_before: number | null; account_equity_before: number | null;
  available_margin_before: number | null;
  initial_stop: number | null; initial_target: number | null; current_stop: number | null;
  current_target: number | null; stop_distance: number | null; target_distance: number | null;
  risk_amount: number | null; risk_pct: number | null; planned_reward: number | null;
  planned_rr: number | null; max_allowed_risk_pct: number | null; max_allowed_risk_amount: number | null;
  risk_rule_status: string | null;
  entry_weekday: string | null; entry_session: string | null; entry_hour_london: number | null;
  entry_at_london: string | null; exit_at_london: string | null; in_preferred_session: boolean | null;
  exit_reason: string | null; exit_reason_source: string | null; exit_at: string | null;
  exit_price: number | null; closed_quantity: number | null; partial_exit_count: number;
  gross_pnl: number | null; fees_total: number | null; funding_total: number | null;
  slippage_cost_total: number | null; net_pnl: number | null; pnl_pct: number | null;
  return_on_margin_pct: number | null; gross_r: number | null; realised_r: number | null;
  duration_s: number | null; result: string | null; result_reason: string | null;
  is_operational: boolean; counts_in_stats: boolean; finalised_at: string | null;
  mfe_price: number | null; mae_price: number | null; mfe_amount: number | null; mae_amount: number | null;
  mfe_r: number | null; mae_r: number | null; excursion_source: string | null;
  decision: string | null; setup_score: number | null; confidence: number | null;
  htf_bias: string | null; market_regime: string | null; rule_violation: boolean | null;
  rule_violation_count: number | null; data_completeness: string | null;
  entry_at_display: string | null; exit_at_display: string | null; duration_display: string | null;
  session_label: string | null; created_at: string;
};

export type Scope = { modes_applied: string[]; mixed_modes: boolean; mode_warning: string | null };

export type Metrics = {
  sample: number; sample_warning: string | null; total_trades: number; wins: number; losses: number;
  break_even: number; win_rate: number | null; net_pnl: number | null; gross_profit: number | null;
  gross_loss: number | null; profit_factor: number | "INF" | null; avg_win?: number | null;
  avg_loss?: number | null; avg_r: number | null; expectancy: number | null; expectancy_r?: number | null;
  max_drawdown: number | null; avg_duration_s: number | null; avg_leverage: number | null;
  avg_risk_pct: number | null; avg_planned_rr: number | null; total_fees: number | null;
  best_trade?: { trade_ref: string; net_pnl: number; symbol: string } | null;
  worst_trade?: { trade_ref: string; net_pnl: number; symbol: string } | null;
  avg_position_size?: number | null; operational_events?: number; open_trades?: number;
};

export type GroupRow = Metrics & { key: string; label: string };

export type BestRef = { key: string; label: string; net_pnl: number | null; avg_r: number | null;
  trades: number; win_rate: number | null } | null;

export type Summary = Scope & {
  net_pnl: number | null; total_trades: number; win_rate: number | null;
  profit_factor: number | "INF" | null; avg_r: number | null; max_drawdown: number | null;
  best_strategy: BestRef; worst_strategy: BestRef; best_session: BestRef; best_symbol: BestRef;
  open_trades: number; pending_orders: number; operational_events: number; total_fees: number | null;
  rule_violations: number; sample_warning: string | null;
};

export const MODE_LABELS: Record<string, string> = {
  BACKTEST: "Backtest", SIMULATION: "Simulation", FORWARD_PAPER: "Forward Paper",
  ISOLATED_FORWARD_PAPER: "Isolated Forward Paper", LIVE: "Live", UNKNOWN: "Unverified", ALL: "All Modes",
};

export const RESULT_TONE: Record<string, "green" | "red" | "amber" | "blue" | "default"> = {
  WIN: "green", PARTIAL_WIN: "green", LOSS: "red", PARTIAL_LOSS: "red", BREAK_EVEN: "amber",
  CANCELLED: "default", REJECTED: "default", EXECUTION_FAILED: "red", EXECUTION_UNCERTAIN: "amber",
};

export const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
export const dash = "—";

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (!isNum(v)) return dash;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function fmtPrice(v: number | null | undefined): string {
  if (!isNum(v)) return dash;
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 8;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtMoney(v: number | null | undefined, signed = true): string {
  if (!isNum(v)) return dash;
  const s = `$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return v < 0 ? `-${s}` : signed && v > 0 ? `+${s}` : s;
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  return isNum(v) ? `${v.toFixed(digits)}%` : dash;
}

export function fmtR(v: number | null | undefined): string {
  return isNum(v) ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}R` : dash;
}

export function fmtRR(v: number | null | undefined): string {
  return isNum(v) ? `1:${v.toFixed(2)}` : dash;
}

export function fmtPF(v: number | "INF" | null | undefined): string {
  if (v === "INF") return "∞";
  return isNum(v) ? v.toFixed(2) : dash;
}

export function fmtLev(v: number | null | undefined): string {
  return isNum(v) ? `${v % 1 === 0 ? v.toFixed(0) : v.toFixed(2)}x` : dash;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (!isNum(seconds)) return dash;
  const s = Math.round(seconds);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return dash;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Europe/London" }) + " London";
}

export const tone = (v: number | null | undefined) => (isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "dim");

export const label = (key: string | null | undefined) =>
  (key ?? "").toLowerCase().replace(/[_-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
