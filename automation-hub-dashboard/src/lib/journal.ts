/** Types and formatting for the canonical Journal (automation-hub/routers/journal_records.py).
 *  Every number shown comes from the API's deterministic statistics; a value
 *  the API did not compute renders as "—", never as zero. */

export type Outcome = "WIN" | "LOSS" | "BREAKEVEN" | "CANCELLED" | "REJECTED" | "BLOCKED"
  | "EXECUTION_FAILED" | "EXECUTION_UNCERTAIN";

export interface RecordRow {
  journal_record_id: string; execution_key: string;
  record_source: string; record_origin: string; verification: string;
  operating_mode: string; data_completeness: string; status: string; outcome: Outcome | null;
  trade_id: string | null; instance_id: string | null; lab_id: string | null; agent_id: string | null;
  strategy_id: string | null; strategy_name: string | null; strategy_version: string | null;
  symbol: string | null; timeframe: string | null; side: string | null;
  trading_session: string | null; setup_type: string | null; market_regime: string | null;
  signal_detected_at: string | null; position_opened_at: string | null; position_closed_at: string | null;
  planned_entry: number | null; planned_stop_loss: number | null; planned_take_profit: number | null;
  planned_rr: number | null; risk_amount: number | null; risk_percent: number | null;
  actual_entry: number | null; actual_exit: number | null; exit_reason: string | null;
  net_pnl: number | null; fees: number | null; realized_r: number | null; achieved_rr: number | null;
  trade_duration_s: number | null; execution_status: string | null;
  reviewed?: boolean; compliance?: string | null;
}

export interface Kpis {
  trades: number; net_pnl: number | null; total_r: number | null; profit_factor: number | null;
  profit_factor_note: string | null; win_rate: number | null; rule_compliance: number | null;
  reviewed: number; sample_warning: string | null; wins: number; losses: number; breakevens: number;
}

export interface TimelineStage { stage: string; at: string | null; status: string; detail?: string | null }

export interface Review {
  trade_review_id: string; agent_id: string; review_version: number; reviewed_at: string;
  setup_quality: string | null; execution_quality: string | null;
  risk_compliance: string | null; strategy_compliance: string | null;
  rule_violations: { rule: string; detail: string }[] | null; mistakes: string[] | null;
  positive_behaviours: string[] | null; review_tags: string[] | null;
  observations: string[] | null; recommendations: string[] | null; basis: Record<string, unknown> | null;
}

export interface Note { note_id: string; journal_record_id: string | null; created_at: string; author: string;
  text: string; tags: string[] | null; symbol?: string | null; strategy_name?: string | null }

export interface FullRecord extends RecordRow {
  decision_id: string | null; signal_id: string | null; intent_id: string | null; order_id: string | null;
  position_id: string | null; session_id: string | null; exchange: string | null; market_type: string | null;
  htf_timeframe: string | null; htf_bias: string | null;
  decision_created_at: string | null; intent_created_at: string | null; order_submitted_at: string | null;
  order_acknowledged_at: string | null; entry_filled_at: string | null; exit_signal_at: string | null;
  exit_submitted_at: string | null; exit_filled_at: string | null; journal_finalized_at: string | null;
  decision_latency_ms: number | null; execution_latency_ms: number | null;
  setup: Record<string, unknown> | null; evidence: Record<string, unknown> | null;
  signal_price: number | null; quantity: number | null; leverage: number | null;
  balance_before: number | null; equity_before: number | null; available_balance_before: number | null;
  risk_check: Record<string, unknown> | null;
  requested_entry: number | null; requested_quantity: number | null; filled_quantity: number | null;
  bid: number | null; ask: number | null; spread: number | null; slippage: number | null;
  order_type: string | null; fill_model: string | null;
  gross_pnl: number | null; funding: number | null; mae_r: number | null; mfe_r: number | null;
  max_trade_drawdown: number | null;
  legs: Record<string, unknown>[] | null; missing: string[] | null; source_ref: Record<string, unknown> | null;
  finalized: number; created_at: string; updated_at: string;
  timeline: TimelineStage[]; corrections: Record<string, unknown>[]; reviews: Review[]; notes: Note[];
  decisions: DecisionRow[];
}

export interface DecisionRow {
  decision_record_id: string; decision_key: string; record_source: string; record_origin: string;
  instance_id: string | null; lab_id: string | null; agent_id: string | null;
  strategy_id: string | null; strategy_name: string | null; strategy_version: string | null;
  symbol: string | null; timeframe: string | null; side: string | null; candle_time: string | null;
  decided_at: string; signal: string | null; decision_type: string; status: string | null;
  blocker: string | null; reason: string | null;
  conditions_passed: unknown[] | null; conditions_missing: unknown[] | null;
  market_data_state: string | null; evidence: Record<string, unknown> | null;
  source_ref: Record<string, unknown> | null; journal_record_id: string | null; trade_id: string | null;
  trade?: FullRecord | null;
}

export interface Finding { text: string; journal_record_ids: string[] }

export const dash = "—";

export function num(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function price(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return dash;
  const digits = Math.abs(v) >= 1000 ? 2 : Math.abs(v) >= 1 ? 4 : 6;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function money(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return dash;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function rMult(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}R`;
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return `${(v * 100).toFixed(digits)}%`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return dash;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function whenFull(iso: string | null | undefined): string {
  if (!iso) return dash;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return dash;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function latency(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return dash;
  return ms < 1000 ? `${Math.round(ms)} ms` : duration(ms / 1000);
}

export type Tone = "green" | "red" | "amber" | "blue" | "purple" | "gold" | "default";

export function outcomeTone(o: string | null | undefined): Tone {
  switch (o) {
    case "WIN": return "green";
    case "LOSS": return "red";
    case "BREAKEVEN": return "default";
    case "EXECUTION_UNCERTAIN": return "amber";
    case "EXECUTION_FAILED": case "REJECTED": return "red";
    default: return "default";
  }
}

export function statusTone(s: string | null | undefined): Tone {
  switch (s) {
    case "OPEN": return "blue";
    case "PENDING": return "amber";
    case "CLOSED": return "default";
    case "EXECUTION_UNCERTAIN": return "amber";
    case "EXECUTION_FAILED": case "REJECTED": return "red";
    default: return "default";
  }
}

export function originTone(o: string | null | undefined): Tone {
  return o === "FORWARD_PAPER" ? "blue" : o === "LEGACY_MIGRATION" ? "amber" : "purple";
}

export const SOURCE_LABEL: Record<string, string> = {
  INSTANCE: "Instance", LEGACY_ENGINE: "Legacy engine", ADAPTIVE_LAB: "Adaptive lab",
  PA_LAB: "PA lab", SMC_LAB: "SMC lab", AGENT: "Agent", MANUAL: "Manual",
};

export const humanize = (s: string | null | undefined) =>
  s ? s.toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase()) : dash;
