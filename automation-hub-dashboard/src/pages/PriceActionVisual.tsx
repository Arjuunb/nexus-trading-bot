import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import NativeSMCChartOverlay, {
  type NativeCandle, type NativeSMCChartState, type NativeSMCOverlayFilters,
} from "../components/chart/NativeSMCChartOverlay";
import { apiDownload, apiGet, apiPostJson } from "../lib/api";
import { useApp } from "../app-context";
import Modal from "../components/common/Modal";
import ResearchComparisonPanel from "../components/research/ResearchComparisonPanel";

type Mode = "live" | "replay";
type BottomTab = "positions" | "orders" | "trades" | "setups" | "rejected" | "rulebook" | "journal" | "learning" | "session" | "connection";
type ChartPreset = "clean" | "structure" | "zones" | "strategy" | "trades" | "debug";
type Direction = "bullish" | "bearish";
interface Swing { id: string; kind: "high" | "low"; price: number; occurred_at: string; confirmed_at: string; label: string }
interface Zone { id: string; role: "support" | "resistance"; low: number; high: number; created_at: string; confirmed_at: string; active: boolean; flipped: boolean; invalidated_at?: string | null; expiration_reason?: string | null; last_touch_at?: string | null; touch_count?: number; source_swing_ids?: string[] }
interface PAEvent { id: string; event_type: string; direction: Direction | "neutral"; level: number; occurred_at: string; confirmed_at: string; zone_id?: string | null; pattern?: string | null; reasons: string[] }
interface Condition { key: string; status: "PASS" | "MISSING"; detail: string; object_id?: string | null }
interface Trace { strategy_id: string; direction: Direction; state: string; conditions: Condition[]; missing_conditions: string[]; setup_id?: string | null; next_required_event: string }
interface Setup { id: string; strategy_id: string; direction: Direction; phase: string; created_at: string; zone_id?: string | null; trigger_event_id?: string | null; invalidation_reason?: string | null; reasons: string[]; missing_conditions: string[] }
interface Proposal { id: string; setup_id: string; strategy_id: string; direction: Direction; entry: number; stop: number; target: number; rr_ratio: number; signal_at: string; execution_allowed: false; paper_execution_allowed: true }
interface PASnapshot { candle_open: string; candle_close: string; structure_bias: Direction | "neutral"; pattern?: string | null; patterns?: { name: string; direction: Direction | "neutral" }[]; proposal_ids: string[]; strategy_traces: Trace[] }
interface PAMetrics { closed: number; wins: number; losses: number; unfilled: number; cancelled?: number; rejected?: number; gross_r?: number; net_r: number; costs_r: number; by_strategy?: Record<string, Omit<PAMetrics, "by_strategy">> }
interface DataIdentity { request_id?: string | null; session_id: string; mode: string; symbol: string; timeframe: string }
interface PAState {
  research_id: string; research_only: true; execution_allowed: false; paper_execution_allowed: true;
  symbol: string; timeframe: string; candles: NativeCandle[]; swings: Swing[]; zones: Zone[]; events: PAEvent[];
  setups: Setup[]; proposals: Proposal[]; snapshot: PASnapshot | null; selected_snapshot: PASnapshot | null;
  snapshot_ledger?: PASnapshot[];
  orders: Record<string, any>[]; trades: Record<string, any>[];
  metrics: PAMetrics; metrics_scope?: Record<string, any>; data_identity?: DataIdentity;
  forming_candle?: NativeCandle | null; live_display?: NativeSMCChartState["live_display"] & { last_update?: string | null; last_candle_update?: string | null; last_quote_update?: string | null; last_mark_update?: string | null; last_closed_update?: string | null; transport_state?: string; health_reason?: string; reliable?: boolean; quote_source?: string; candle_age_seconds?: number | null; quote_age_seconds?: number | null; mark_age_seconds?: number | null; closed_candle_age_seconds?: number | null; candle_quote_deviation_bps?: number | null; failing_dependency?: string | null; last_successful_event?: { kind: string; at: string } | null; retry_state?: { attempt?: number; automatic_retry?: boolean } };
  replay?: { cursor: number; total: number; future_candles_visible: false; has_next: boolean };
  data_provenance?: Record<string, string | number | boolean>;
  mtf_policy?: { label: string; available_entry_timeframes: string[] };
}
interface PaperState {
  account_scope: string; currency: "USDT"; execution_mode: "PAPER"; real_funds: false; live_execution_allowed: false;
  session: { id: string; started_at: string; status: string; mode?: "LIVE_PAPER" | "HISTORICAL"; starting_balance: number; symbol?: string; timeframe?: string; operating_mode?: OperatingMode; execution_config?: { strategy_id?: string; risk_pct?: number } };
  account: { starting_balance: number; balance: number; equity: number; unrealized_pnl: number; fees_paid: number; free_margin: number; leverage: number };
  positions: Record<string, any>[]; orders: Record<string, any>[]; trades: Record<string, any>[];
  candidates: { proposal_id: string; source_proposal_id: string; status: string; payload: Record<string, any> }[];
  order_metadata: { order_id: string; status: string; config: { proposal: Proposal; entry: number; stop: number; target: number } }[];
  activity: Record<string, any>[];
  position_remediations?: Record<string, any>[];
  entry_control?: { readiness_recheck_required?: boolean; entry_pause_reason?: string };
  order_audit?: { session_id: string; pending_paper_orders: number; pending_strategy_orders: number; pending_manual_orders: number; duplicate_strategy_orders: Record<string, any>[]; discrepancies: Record<string, any>[]; manual_orders_are_never_auto_cancelled: boolean };
}
interface PAJournalEntry {
  revision_no: number;
  identity: { journal_entry_id: string; session_id: string; strategy_id: string; strategy_version: string; configuration_fingerprint: string; dataset_fingerprint: string; symbol: string; timeframe: string; direction: string; research_partition: string };
  market_context: { data_health_state: string; data_health_reason?: string; zone_role?: string | null };
  setup: { setup_id: string; state: string; trigger_classification?: string | null; state_transitions: Record<string, any>[] };
  order_risk: { requested_entry?: number | null; actual_simulated_fill?: number | null; stop?: number | null; target?: number | null; commission?: number | null; bid_ask_fill?: { bid?: number; ask?: number; mark?: number; source?: string } | null; fill_quote_evidence_status?: string; funding?: { amount_usdt?: number | null; normalized_r?: number | null; event_count: number; coverage: string } };
  outcome: { status: string; result: string; net_r?: number | null; gross_r?: number | null; costs_r?: number | null; exit_timestamp?: string | null; exit_reason?: string | null; maximum_favourable_excursion?: number | null; maximum_adverse_excursion?: number | null; bars_to_entry?: number | null; bars_in_trade?: number | null; excursion_model?: string | null };
  review: { learning_classification: string; classification_explanation: string; include_in_research_statistics: boolean; researcher_notes?: string; tags?: string[] };
  chart_state: { candle_timestamp?: string | null; zone_id?: string | null; event_id?: string | null; entry?: number | null; stop?: number | null; target?: number | null };
}
interface PAJournalResponse {
  entries: PAJournalEntry[];
  statistics: { setups: number; completed: number; wins: number; losses: number; net_r: number; expectancy_r: number };
  real_execution_allowed: false;
}
interface PALearningAnalysis {
  classifications: Record<string, number>;
  patterns: { dimension: string; segment: string; strategy_id: string; direction: string; symbol: string; timeframe: string; setups: number; completed: number; net_expectancy_r: number; uncertainty: string; candidate_eligible: boolean; confounding_factors: string[] }[];
  minimum_pattern_sample: number; warning: string; active_strategy_mutated: false; real_execution_allowed: false;
}
interface PALearningCandidate {
  id: string; parent_strategy_version: string; rule_key: string; rule_value: unknown;
  status: string; expected_benefit: string; live_execution_allowed: false;
  shadow_report?: { observations: number; shared_signals: number; baseline_only_signals: number;
    candidate_only_signals: number; changed_entries: number; changed_stops: number;
    avoided_losses: number; missed_winners: number; additional_costs_r: number;
    net_effect_r: number; net_oos_effect_status: string; drawdown_effect_r: number;
    trade_count_change: number; official_paper_account_affected: false };
}
interface RulebookZone { id: string; kind: "support" | "resistance"; lower: number; upper: number; centre: number; origin: string; retired: boolean; created_at: string; creation_atr: number }
interface RulebookSetup { id: string; strategy_id: string; direction: string; state: string; zone: RulebookZone; setup_atr15: number; created_at: string; cancel_price?: number | null; confirm_slots_used: number; confirm_window_start?: string | null; rejection_open_time?: string | null; breakout_open_time?: string | null; blocker?: string | null; evidence: Record<string, any> }
interface RulebookPlan {
  accepted: boolean; direction: string; strategy_id: string; entry_bound: number;
  stop: number; target?: number | null; stop_distance: number; stop_distance_atr: number;
  net_rr?: number | null; costs_loss: number; costs_win: number; quantity?: number | null;
  planned_loss?: number | null; zone_id: string; blocker?: string | null;
  evidence: Record<string, any>;
}
interface RulebookState {
  symbol: string; rulebook_version: string; strategies: string[];
  regime: string; regime_evidence: Record<string, any>;
  timeframes: { context: string; setup: string; confirm: string };
  last_closed: Record<string, string>;
  zones: RulebookZone[]; retired_zones: RulebookZone[]; consumed_zone_ids: string[];
  pending_setup: RulebookSetup | null; history: RulebookSetup[];
  decision: { state?: string | null; blocker?: string | null; evidence: Record<string, any>; confirmed: boolean };
  plan: RulebookPlan | null;
  real_execution_allowed: false; paper_execution_allowed: false;
  state?: string; required_context_bars?: number; available_context_bars?: number;
}
interface RulebookManifest {
  version: string; engine_sha256: string; pure_engine: boolean; reads_clock: boolean;
  real_order_path: boolean; volume_used_for_signals: boolean; status_note: string;
  instance_strategy_id: string; timeframes: { context: string; setup: string; confirm: string };
}
type JournalFilters = { strategy_id: string; direction: string; result: string; trigger_type: string; zone_type: string; touch_count: string; regime: string; rule_compliance: string; data_quality: string; entry_model: string; partition: string; strategy_version: string; date_from: string; date_to: string };
type OperatingMode = "signals_only" | "manual_approval" | "automatic";

const STRATEGIES = ["PA1_SR_REJECTION", "PA2_TREND_PULLBACK", "PA3_FLIP_RETEST", "PA4_FALSE_BREAK_REVERSAL"];
const PA_MODE_LABEL: Record<string, string> = {
  signals_only: "Signals only", manual_approval: "Manual approval", automatic: "Automatic paper",
};
const TABS: BottomTab[] = ["positions", "orders", "trades", "setups", "rejected", "rulebook", "journal", "learning", "session", "connection"];
const money = (value?: number) => Number(value ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const stamp = (value?: string | null) => value ? value.replace("T", " ").replace("+00:00", " UTC").slice(0, 22) : "—";
const pretty = (value: string) => value.replace(/^PA\d_/, "").replace(/_/g, " ");
const age = (value?: number | null) => value == null ? "—" : value < 1 ? "<1s" : `${value.toFixed(value < 10 ? 1 : 0)}s`;
const CLEAN_VISIBLE_BARS = 72;
const ACTIVE_PAPER_PLAN_STATUSES = new Set(["ORDER_PENDING", "PARTIALLY_FILLED", "ENTERED"]);
const OPEN_BROKER_ORDER_STATUSES = new Set(["open", "partially_filled", "triggered"]);
const TERMINAL_SETUP_PHASES = new Set(["STOPPED", "TARGET_HIT", "CANCELLED", "INVALIDATED", "EXPIRED", "DATA_PAUSED", "LIQUIDATED_PAPER", "CLOSED_OTHER"]);
const PRESET_FILTERS: Record<ChartPreset, NativeSMCOverlayFilters> = {
  clean: { pivots: false, internal: true, swing: true, structure: false, liquidity: false, fvg: false, orderBlocks: true, mitigated: false, labels: false },
  structure: { pivots: true, internal: true, swing: true, structure: true, liquidity: false, fvg: false, orderBlocks: false, mitigated: false, labels: false },
  zones: { pivots: false, internal: true, swing: true, structure: false, liquidity: false, fvg: false, orderBlocks: true, mitigated: false, labels: false },
  strategy: { pivots: false, internal: true, swing: true, structure: true, liquidity: false, fvg: false, orderBlocks: true, mitigated: false, labels: true },
  trades: { pivots: false, internal: true, swing: true, structure: false, liquidity: false, fvg: false, orderBlocks: true, mitigated: false, labels: true },
  debug: { pivots: true, internal: true, swing: true, structure: true, liquidity: true, fvg: false, orderBlocks: true, mitigated: true, labels: true },
};

function clusteredEvents(rows: PAEvent[], limit: number): PAEvent[] {
  const seen = new Set<string>();
  const result: PAEvent[] = [];
  for (const row of [...rows].reverse()) {
    const key = `${row.confirmed_at}|${row.event_type}|${row.zone_id ?? "market"}`;
    if (seen.has(key)) continue;
    seen.add(key); result.push(row);
    if (result.length >= limit) break;
  }
  return result.reverse();
}

function chartState(state: PAState, paper: PaperState | null, preset: ChartPreset,
                    focusedSetup?: Setup | null): NativeSMCChartState {
  const last = state.candles[state.candles.length - 1];
  const high = state.candles.length ? Math.max(...state.candles.map((row) => row.high)) : 0;
  const low = state.candles.length ? Math.min(...state.candles.map((row) => row.low)) : 0;
  const snap = state.snapshot ? {
    id: `pa-chart-${state.snapshot.candle_open}`, candle_open: state.snapshot.candle_open, candle_close: state.snapshot.candle_close,
    htf_bias: 0, htf_ema: null, swing_bias: state.snapshot.structure_bias === "bullish" ? 1 : state.snapshot.structure_bias === "bearish" ? -1 : 0,
    internal_bias: 0, session: "24/7", dealing_range: { high, low, equilibrium: (high + low) / 2, area: "native zones" },
    price_action: { bullish_rejection: false, bearish_rejection: false, body: last ? Math.abs(last.close - last.open) : 0, upper_wick: 0, lower_wick: 0 },
    active_setup_id: focusedSetup?.id ?? null, setup_phase: focusedSetup?.phase ?? null, next_required_event: "See strategy trace", latest_sweep_id: null,
    event_ids: state.events.map((row) => row.id), active_fvg_ids: [], active_ob_ids: state.zones.filter((row) => row.active).map((row) => row.id), proposal_ids: state.snapshot.proposal_ids,
  } : null;
  const candleByTimestamp = new Map(state.candles.map((row) => [row.timestamp, row]));
  const patternEvents = preset === "debug" ? (state.snapshot_ledger ?? []).flatMap((snapshot) => (snapshot.patterns ?? []).map((pattern, index) => {
    const candle = candleByTimestamp.get(snapshot.candle_open);
    return { id: `pattern-${snapshot.candle_open}-${index}`, event_type: pattern.name,
      direction: pattern.direction, level: candle?.close ?? 0, occurred_at: snapshot.candle_open,
      confirmed_at: snapshot.candle_close, zone_id: null, pattern: pattern.name, reasons: ["completed-candle pattern metadata"] };
  })) : [];
  const cleanCandles = state.candles.slice(-120);
  const cleanHigh = cleanCandles.length ? Math.max(...cleanCandles.map((row) => row.high)) : high;
  const cleanLow = cleanCandles.length ? Math.min(...cleanCandles.map((row) => row.low)) : low;
  const cleanSpan = Math.max(cleanHigh - cleanLow, Math.abs(last?.close ?? 0) * .002, 1);
  const latestPrice = state.forming_candle?.close ?? last?.close ?? 0;
  const nearbyActiveZones = state.zones
    .filter((row) => row.active && row.high >= cleanLow - cleanSpan * .25 && row.low <= cleanHigh + cleanSpan * .25)
    .sort((left, right) => {
      const leftDistance = Math.abs((left.high + left.low) / 2 - latestPrice);
      const rightDistance = Math.abs((right.high + right.low) / 2 - latestPrice);
      return leftDistance - rightDistance || right.confirmed_at.localeCompare(left.confirmed_at);
    });
  let cleanZones = [
    ...nearbyActiveZones.filter((row) => row.role === "support").slice(0, 4),
    ...nearbyActiveZones.filter((row) => row.role === "resistance").slice(0, 4),
  ].sort((left, right) => left.confirmed_at.localeCompare(right.confirmed_at));
  const focusedZone = focusedSetup?.zone_id ? state.zones.find((row) => row.id === focusedSetup.zone_id) : null;
  if (focusedZone && !cleanZones.some((row) => row.id === focusedZone.id)) cleanZones = [...cleanZones, focusedZone];
  const recentStart = state.candles[Math.max(0, state.candles.length - 120)]?.timestamp;
  const researchPlans = focusedSetup
    ? state.proposals.filter((row) => row.setup_id === focusedSetup.id)
    : preset === "debug" ? state.proposals
    : preset === "trades" ? state.proposals.filter((row) => !recentStart || row.signal_at >= recentStart).slice(-8)
    : state.proposals.filter((row) => !recentStart || row.signal_at >= recentStart).slice(-1);
  const paperPlans = (paper?.order_metadata ?? [])
    .filter((row) => (preset === "debug" || ACTIVE_PAPER_PLAN_STATUSES.has(row.status)) &&
      (!focusedSetup || row.config.proposal.setup_id === focusedSetup.id))
    .map((row) => ({
    ...row.config.proposal, id: `paper-${row.order_id}`, snapshot_id: `paper-${row.order_id}`,
    entry: row.config.entry, stop: row.config.stop, target: row.config.target,
    risk_status: `PAPER_${row.status}`,
  }));
  const focusObjectIds = new Set([
    focusedSetup?.zone_id, focusedSetup?.trigger_event_id,
    ...(focusedZone?.source_swing_ids ?? []),
  ].filter(Boolean));
  const visibleSwings = focusedSetup
    ? state.swings.filter((row) => focusObjectIds.has(row.id))
    : preset === "debug" ? state.swings : state.swings.slice(-24);
  const visibleEvents = focusedSetup
    ? state.events.filter((row) => focusObjectIds.has(row.id) || row.zone_id === focusedSetup.zone_id)
    : clusteredEvents(state.events, preset === "debug" ? 240 : preset === "structure" ? 64 : 20);
  const chartZones = focusedZone ? [focusedZone] : preset === "debug" ? state.zones : cleanZones;
  return {
    research_id: state.research_id, execution_allowed: false, candles: state.candles,
    pivots: visibleSwings.map((row) => ({ ...row, scope: "swing", strength: row.label === "HH" || row.label === "LL" ? "strong" : "weak" })),
    events: [...visibleEvents, ...patternEvents].map((row) => ({ ...row, scope: "swing", label: pretty(row.event_type) })),
    fair_value_gaps: [],
    order_blocks: chartZones.map((row) => ({ id: row.id, direction: row.role === "support" ? "bullish" : "bearish", label: `${row.active ? "ACTIVE" : row.expiration_reason ? "EXPIRED" : "INVALIDATED"} · ${row.flipped ? "FLIPPED " : ""}${row.role.toUpperCase()}`, low: row.low, high: row.high, created_at: row.confirmed_at, active: row.active, mitigated: !row.active, mitigation_at: row.invalidated_at, lifecycle: row.active ? "active" : row.expiration_reason ? "expired" : "invalidated" })),
    proposals: [...researchPlans.map((row) => ({ ...row, snapshot_id: `pa-chart-${row.signal_at}`, risk_status: "SIGNAL_ONLY" })), ...paperPlans],
    snapshot: snap, selected_snapshot: snap, snapshot_ledger: snap ? [snap] : [],
    forming_candle: state.forming_candle, live_display: state.live_display,
  };
}

function DataTable({ rows, empty }: { rows: Record<string, any>[]; empty: string }) {
  if (!rows.length) return <div className="pa-empty">{empty}</div>;
  const columns = Object.keys(rows[0]).slice(0, 8);
  return <div className="pa-table-wrap"><table className="pa-table"><thead><tr>{columns.map((key) => <th key={key}>{pretty(key)}</th>)}</tr></thead><tbody>{rows.map((row, i) => <tr key={String(row.id ?? i)}>{columns.map((key) => <td key={key}>{typeof row[key] === "number" ? Number(row[key]).toLocaleString(undefined, { maximumFractionDigits: 6 }) : String(row[key] ?? "—")}</td>)}</tr>)}</tbody></table></div>;
}

function JournalPanel({ journal, selected, selectedId, onSelect, filters, onFilters, sessionId, symbol, timeframe }: {
  journal: PAJournalResponse | null; selected: PAJournalEntry | null; selectedId: string;
  onSelect: (id: string) => void; filters: JournalFilters;
  onFilters: (next: JournalFilters) => void;
  sessionId?: string; symbol: string; timeframe: string;
}) {
  const stats = journal?.statistics;
  const queryFilters = { ...filters, date_to: filters.date_to ? `${filters.date_to}T23:59:59Z` : "" };
  const query = new URLSearchParams({ session_id: sessionId ?? "", symbol, timeframe,
    ...Object.fromEntries(Object.entries(queryFilters).filter(([, value]) => value)) });
  return <div className="pa-governance">
    <div className="pa-governance-bar"><b>Immutable setup journal · {symbol} {timeframe}</b>
      <select aria-label="Journal strategy filter" value={filters.strategy_id} onChange={(event) => onFilters({ ...filters, strategy_id: event.target.value })}><option value="">All strategies</option>{STRATEGIES.map((row) => <option key={row} value={row}>{pretty(row)}</option>)}</select>
      <select aria-label="Journal direction filter" value={filters.direction} onChange={(event) => onFilters({ ...filters, direction: event.target.value })}><option value="">Long &amp; short</option><option value="bullish">Long</option><option value="bearish">Short</option></select>
      <select aria-label="Journal result filter" value={filters.result} onChange={(event) => onFilters({ ...filters, result: event.target.value })}><option value="">All results</option>{["open", "won", "lost", "cancelled", "rejected", "unfilled"].map((row) => <option key={row}>{row}</option>)}</select>
      <select aria-label="Journal trigger filter" value={filters.trigger_type} onChange={(event) => onFilters({ ...filters, trigger_type: event.target.value })}><option value="">All triggers</option>{["zone_rejection", "flip_retest", "false_break_reclaim"].map((row) => <option key={row}>{row}</option>)}</select>
      <select aria-label="Journal zone filter" value={filters.zone_type} onChange={(event) => onFilters({ ...filters, zone_type: event.target.value })}><option value="">All zones</option><option value="support">Support</option><option value="resistance">Resistance</option></select>
      <select aria-label="Journal touch-count filter" value={filters.touch_count} onChange={(event) => onFilters({ ...filters, touch_count: event.target.value })}><option value="">Any touch</option><option value="1">First touch</option><option value="2">2 touches</option><option value="3">3 touches</option></select>
      <select aria-label="Journal regime filter" value={filters.regime} onChange={(event) => onFilters({ ...filters, regime: event.target.value })}><option value="">All regimes</option><option value="bullish">Bullish</option><option value="bearish">Bearish</option><option value="neutral">Neutral</option></select>
      <select aria-label="Journal compliance filter" value={filters.rule_compliance} onChange={(event) => onFilters({ ...filters, rule_compliance: event.target.value })}><option value="">Any compliance</option><option value="true">Rule valid</option><option value="false">Rule violating</option></select>
      <select aria-label="Journal partition filter" value={filters.partition} onChange={(event) => onFilters({ ...filters, partition: event.target.value })}><option value="">All partitions</option>{["development", "validation", "untouched_oos", "paper_forward"].map((row) => <option key={row}>{row}</option>)}</select>
      <select aria-label="Journal data-quality filter" value={filters.data_quality} onChange={(event) => onFilters({ ...filters, data_quality: event.target.value })}><option value="">All data health</option>{["SYNCHRONIZED", "HISTORICAL_REPLAY", "STALE_CANDLES", "DATA_PAUSED"].map((row) => <option key={row}>{row}</option>)}</select>
      <select aria-label="Journal entry-model filter" value={filters.entry_model} onChange={(event) => onFilters({ ...filters, entry_model: event.target.value })}><option value="">All entries</option><option value="confirmation">Confirmation</option><option value="close">Close</option><option value="retracement_50">50% retracement</option></select>
      <input aria-label="Journal strategy-version filter" value={filters.strategy_version} onChange={(event) => onFilters({ ...filters, strategy_version: event.target.value })} placeholder="Version" />
      <input aria-label="Journal start-date filter" type="date" value={filters.date_from} onChange={(event) => onFilters({ ...filters, date_from: event.target.value })} />
      <input aria-label="Journal end-date filter" type="date" value={filters.date_to} onChange={(event) => onFilters({ ...filters, date_to: event.target.value })} />
      <button className="pa-export" onClick={() => void apiDownload(`/research/price-action/journal/export?${query.toString()}`, `price-action-journal-${sessionId ?? "all"}.json`)}>Export evidence</button>
    </div>
    <div className="pa-journal-stats"><span>Setups<b>{stats?.setups ?? 0}</b></span><span>Completed<b>{stats?.completed ?? 0}</b></span><span>W / L<b>{stats?.wins ?? 0} / {stats?.losses ?? 0}</b></span><span>Net R<b>{Number(stats?.net_r ?? 0).toFixed(2)}</b></span><span>Expectancy<b>{Number(stats?.expectancy_r ?? 0).toFixed(3)}R</b></span><span>Execution<b>PAPER ONLY</b></span></div>
    <div className="pa-journal-grid"><div className="pa-table-wrap"><table className="pa-table"><thead><tr><th>Opened</th><th>Strategy</th><th>Direction</th><th>State</th><th>Result</th><th>Net R</th><th>Health</th><th>Partition</th></tr></thead><tbody>{(journal?.entries ?? []).map((row) => <tr key={row.identity.journal_entry_id} className={selectedId === row.identity.journal_entry_id ? "is-selected" : ""} onClick={() => onSelect(row.identity.journal_entry_id)}><td>{stamp(row.setup.state_transitions[0]?.timestamp as string)}</td><td>{pretty(row.identity.strategy_id)}</td><td>{row.identity.direction}</td><td>{row.setup.state}</td><td>{row.outcome.result}</td><td>{row.outcome.net_r == null ? "—" : row.outcome.net_r.toFixed(3)}</td><td>{row.market_context.data_health_state}</td><td>{pretty(row.identity.research_partition)}</td></tr>)}</tbody></table>{!journal?.entries.length ? <div className="pa-empty">No setup evidence matches these configuration-scoped filters.</div> : null}</div>
      <div className="pa-journal-detail">{selected ? <><h3>{pretty(selected.identity.strategy_id)} · {selected.identity.direction}</h3><p><b>{selected.review.learning_classification}</b>{selected.review.classification_explanation}</p><dl><dt>Setup</dt><dd>{selected.setup.setup_id}</dd><dt>Version / revision</dt><dd>{selected.identity.strategy_version} / r{selected.revision_no}</dd><dt>Config</dt><dd>{selected.identity.configuration_fingerprint}</dd><dt>Dataset</dt><dd>{selected.identity.dataset_fingerprint}</dd><dt>Decision candle</dt><dd>{stamp(selected.chart_state.candle_timestamp)}</dd><dt>Zone / event</dt><dd>{selected.chart_state.zone_id ?? "—"} / {selected.chart_state.event_id ?? "—"}</dd><dt>Entry / fill</dt><dd>{selected.order_risk.requested_entry ?? "—"} / {selected.order_risk.actual_simulated_fill ?? "—"}</dd><dt>Fill quote</dt><dd>{selected.order_risk.bid_ask_fill ? `${selected.order_risk.bid_ask_fill.bid} / ${selected.order_risk.bid_ask_fill.ask} · mark ${selected.order_risk.bid_ask_fill.mark}` : `— · ${pretty(selected.order_risk.fill_quote_evidence_status ?? "not available")}`}</dd><dt>Stop / target</dt><dd>{selected.order_risk.stop ?? "—"} / {selected.order_risk.target ?? "—"}</dd><dt>Gross / costs / net</dt><dd>{selected.outcome.gross_r ?? "—"} / {selected.outcome.costs_r ?? "—"} / {selected.outcome.net_r ?? "—"} R</dd><dt>MFE / MAE</dt><dd>{selected.outcome.maximum_favourable_excursion ?? "—"} / {selected.outcome.maximum_adverse_excursion ?? "—"} R</dd><dt>Bars to entry / in trade</dt><dd>{selected.outcome.bars_to_entry ?? "—"} / {selected.outcome.bars_in_trade ?? "—"}</dd><dt>Funding</dt><dd>{selected.order_risk.funding?.amount_usdt == null ? `— · ${pretty(selected.order_risk.funding?.coverage ?? "not available")}` : `${selected.order_risk.funding.amount_usdt.toFixed(4)} USDT / ${selected.order_risk.funding.normalized_r?.toFixed(4) ?? "—"}R`}</dd><dt>Included in research</dt><dd>{selected.review.include_in_research_statistics ? "YES" : `NO · ${pretty(selected.review.learning_classification)}`}</dd><dt>Reviewer notes</dt><dd>{selected.review.researcher_notes || "—"}</dd><dt>Tags</dt><dd>{selected.review.tags?.join(", ") || "—"}</dd></dl><h4>Lifecycle</h4><ol>{selected.setup.state_transitions.map((row, index) => <li key={String(row.id ?? index)}><b>{String(row.to_phase ?? row.current_phase ?? "STATE")}</b>{String(row.reason ?? row.reason_code ?? "")}</li>)}</ol></> : <div className="pa-empty">Select a journal record to inspect its immutable lifecycle.</div>}</div>
    </div>
  </div>;
}

const BLOCKER_NOTES: Record<string, string> = {
  NET_RR_TOO_LOW: "The plan is complete and does not pay. Over 2025 this refused 32 of 55 confirmations at a median of 0.21R against a 2.50R gate, so it is usually the room to the target, not the threshold.",
  TARGET_UNAVAILABLE: "No unexpired opposing zone to aim at, so no reward can be measured. The same shortage as NET_RR_TOO_LOW, further along.",
  STOP_DISTANCE_INVALID: "The stop is outside the ATR band. Every 2025 instance was above the 2.50 ATR15 ceiling, never below the floor.",
  REGIME_NOT_ALIGNED: "The 1H regime does not permit this direction. It held on 47% of 2025's 5M candles, which were TRANSITION.",
  REJECTION_FAILED: "Price reached the zone but the candle did not meet the rejection test.",
  CONFIRMATION_EXPIRED: "The rejection was never confirmed inside its three 5M candles.",
  ZONE_CONSUMED: "This zone has already produced its setup and may not produce another.",
  NO_ELIGIBLE_ZONE: "No zone was in reach of price on this candle.",
  WARMING_UP: "Fewer than 200 closed 1H candles. The regime classifier refuses to leave UNKNOWN below that, so nothing can be decided yet.",
  HTF_NOT_READY: "The 1H or 15M frame has not closed a candle the engine has not already seen.",
  MISSING_CANDLE: "A gap in the closed-candle series. The engine will not decide across one.",
  STALE_HTF_CANDLE: "The higher-timeframe candle is too old to be treated as current.",
  NON_REAL_DATA: "The candles offered were not from a validated real provider. Chapter 2 forbids deciding on them.",
  EXISTING_EXPOSURE: "A position is already open for this symbol.",
};

function RulebookPanel({ state, manifest, error, loading, onReload, symbol }: {
  state: RulebookState | null; manifest: RulebookManifest | null; error: string | null;
  loading: boolean; onReload: () => void; symbol: string;
}) {
  const plan = state?.plan ?? null;
  const evidence = plan?.evidence ?? {};
  const blocker = plan?.blocker ?? state?.decision.blocker ?? null;
  const room = Number(evidence.target_room ?? NaN);
  const needed = Number(evidence.required_room_for_min_rr ?? NaN);
  // The one ratio the 2025 replay showed decides almost every refusal. Shown
  // whenever both sides exist, not only when it is the named blocker.
  const roomShare = Number.isFinite(room) && Number.isFinite(needed) && needed > 0
    ? room / needed : null;

  if (error) return <div className="pa-governance"><div className="pa-learning-warning"><b>Rulebook engine unavailable</b><span>{error}</span><button onClick={onReload}>Retry</button></div></div>;
  if (!state) return <div className="pa-empty">{loading ? "Running the rulebook engine over the cached candles…" : "No rulebook evaluation yet."}</div>;
  if (state.state === "WARMING_UP") return <div className="pa-empty">Warming up: {state.available_context_bars} of {state.required_context_bars} required 1H candles. The engine will not decide on a partial window.</div>;

  return <div className="pa-governance">
    <div className="pa-learning-warning">
      <b>Nexus PA rulebook v{state.rulebook_version} · READ ONLY</b>
      <span>{manifest?.status_note ?? "Research hypothesis, not a proven edge."}</span>
      <span>
        Pure engine {manifest?.pure_engine ? "YES" : "—"} · reads clock {manifest?.reads_clock ? "YES" : "NO"} ·
        volume in signals {manifest?.volume_used_for_signals ? "YES" : "NO"} ·
        order path {manifest?.real_order_path ? "YES" : "NONE"} ·
        engine sha256 {manifest?.engine_sha256?.slice(0, 16) ?? "—"}
      </span>
      <span>This panel evaluates cached closed candles. It creates no session, writes no journal and proposes no order.</span>
    </div>

    <div className="pa-journal-stats">
      <span>Regime<b>{state.regime}</b></span>
      <span>Decision<b>{state.decision.state ?? "—"}</b></span>
      <span>Blocker<b>{blocker ?? "none"}</b></span>
      <span>Active zones<b>{state.zones.length}</b></span>
      <span>Consumed<b>{state.consumed_zone_ids.length}</b></span>
      <span>Strategies<b>{state.strategies.length}</b></span>
    </div>

    {blocker ? <div className="pa-order-audit">
      <b>{blocker}</b>
      <span>{BLOCKER_NOTES[blocker] ?? "See the measured evidence below."}</span>
      {roomShare != null ? <span>
        Room to the target is {(roomShare * 100).toFixed(0)}% of the room this gate needs
        ({room.toFixed(2)} of {needed.toFixed(2)}, target_room / required_room_for_min_rr)
      </span> : null}
      <small>A setup that never appears and a setup that appears and is refused are different problems. The blocker is what tells them apart.</small>
    </div> : null}

    <div className="pa-learning-grid">
      <section><h3>Plan arithmetic</h3>{plan ? <dl>
        <dt>Verdict</dt><dd>{plan.accepted ? "ACCEPTED" : `REFUSED · ${plan.blocker ?? "—"}`}</dd>
        <dt>Strategy / direction</dt><dd>{plan.strategy_id} · {plan.direction}</dd>
        <dt>Entry / stop / target</dt><dd>{plan.entry_bound} / {plan.stop} / {plan.target ?? "—"}</dd>
        <dt>Stop distance</dt><dd>{plan.stop_distance.toFixed(2)} ({plan.stop_distance_atr.toFixed(2)} ATR15, |E−S| / ATR15)</dd>
        <dt>Net RR</dt><dd>{plan.net_rr == null ? "—" : plan.net_rr.toFixed(3)} <small>(|T−E| − costs_win) / (|E−S| + costs_loss)</small></dd>
        <dt>Costs loss / win</dt><dd>{plan.costs_loss.toFixed(2)} / {plan.costs_win.toFixed(2)}</dd>
        <dt>Fees as share of risk</dt><dd>{evidence.cost_share_of_risk == null ? "—" : `${(Number(evidence.cost_share_of_risk) * 100).toFixed(0)}%`} <small>costs_loss / (|E−S| + costs_loss)</small></dd>
        <dt>Target capped by</dt><dd>{evidence.target_zone_id ?? "—"}</dd>
        <dt>Room to target / needed</dt><dd>{Number.isFinite(room) ? room.toFixed(2) : "—"} / {Number.isFinite(needed) ? needed.toFixed(2) : "—"}</dd>
        <dt>Quantity / planned loss</dt><dd>{plan.quantity ?? "—"} / {plan.planned_loss == null ? "—" : `${plan.planned_loss.toFixed(3)} USDT`}</dd>
      </dl> : <div className="pa-empty">No plan on this candle. {state.decision.blocker ? `Blocked earlier: ${state.decision.blocker}.` : "No setup reached the plan stage."}</div>}</section>

      <section><h3>Regime evidence · {state.timeframes.context}</h3><DataTable rows={Object.entries(state.regime_evidence ?? {}).map(([measure, value]) => ({ measure, value: typeof value === "number" ? Number(value.toFixed(4)) : String(value) }))} empty="No regime evidence on this candle." /></section>

      <section><h3>Pending setup</h3>{state.pending_setup ? <dl>
        <dt>Strategy</dt><dd>{state.pending_setup.strategy_id} · {state.pending_setup.direction}</dd>
        <dt>State</dt><dd>{state.pending_setup.state}</dd>
        <dt>Zone</dt><dd>{state.pending_setup.zone.id} ({state.pending_setup.zone.lower.toFixed(2)}–{state.pending_setup.zone.upper.toFixed(2)})</dd>
        <dt>Setup ATR15</dt><dd>{state.pending_setup.setup_atr15.toFixed(2)}</dd>
        <dt>Confirm slot</dt><dd>{state.pending_setup.confirm_slots_used} of 3</dd>
        <dt>Cancel price</dt><dd>{state.pending_setup.cancel_price ?? "—"}</dd>
      </dl> : <div className="pa-empty">No setup is waiting. {state.decision.blocker ? BLOCKER_NOTES[state.decision.blocker] ?? state.decision.blocker : ""}</div>}</section>
    </div>

    <div className="pa-shadow-comparison">
      <section><h3>Zone registry · {symbol} {state.timeframes.setup}</h3><DataTable rows={state.zones.map((zone) => ({ id: zone.id, kind: zone.kind, lower: zone.lower, upper: zone.upper, origin: zone.origin, consumed: state.consumed_zone_ids.includes(zone.id) ? "YES" : "no", created: stamp(zone.created_at) }))} empty="No unexpired zones. Zones expire 72 setup candles after creation." /></section>
      <section><h3>Recent setups</h3><DataTable rows={state.history.slice().reverse().map((setup) => ({ created: stamp(setup.created_at), strategy: setup.strategy_id, direction: setup.direction, state: setup.state, zone: setup.zone.id, blocker: setup.blocker ?? "—" }))} empty="No setups raised in this window." /></section>
    </div>

    <div className="pa-chart-foot">
      <span>Last closed: {Object.entries(state.last_closed).map(([tf, at]) => `${tf} ${stamp(at)}`).join(" · ")}</span>
      <button className="pa-export" onClick={onReload}>Re-evaluate</button>
      <b>RESEARCH · NO ORDER PATH</b>
    </div>
  </div>;
}

function LearningPanel({ analysis, candidates }: { analysis: PALearningAnalysis | null; candidates: PALearningCandidate[] }) {
  const classifications = Object.entries(analysis?.classifications ?? {}).map(([classification, count]) => ({ classification, count }));
  const signalRows = candidates.map((row) => ({
    candidate: row.id.slice(0, 14), status: row.status,
    observations: row.shadow_report?.observations ?? 0,
    baseline_only: row.shadow_report?.baseline_only_signals ?? 0,
    candidate_only: row.shadow_report?.candidate_only_signals ?? 0,
    shared: row.shadow_report?.shared_signals ?? 0,
    changed_entry_stop: `${row.shadow_report?.changed_entries ?? 0} / ${row.shadow_report?.changed_stops ?? 0}`,
  }));
  const outcomeRows = candidates.map((row) => ({
    candidate: row.id.slice(0, 14), avoided_losses: row.shadow_report?.avoided_losses ?? 0,
    missed_winners: row.shadow_report?.missed_winners ?? 0,
    additional_costs_r: row.shadow_report?.additional_costs_r ?? 0,
    net_effect_r: row.shadow_report?.net_effect_r ?? 0,
    drawdown_effect_r: row.shadow_report?.drawdown_effect_r ?? 0,
    trade_count_change: row.shadow_report?.trade_count_change ?? 0,
    evidence_scope: row.shadow_report?.net_oos_effect_status ?? "NO_SHADOW_EVIDENCE",
  }));
  return <div className="pa-governance">
    <div className="pa-learning-warning"><b>Governed research only</b><span>{analysis?.warning ?? "Waiting for journal evidence."}</span><span>No automatic parameter mutation · no automatic promotion · no live execution.</span></div>
    <div className="pa-learning-grid">
      <section><h3>Outcome classification</h3><DataTable rows={classifications} empty="No classified setup evidence yet." /></section>
      <section><h3>Pattern evidence</h3><DataTable rows={(analysis?.patterns ?? []).map((row) => ({ dimension: pretty(row.dimension), segment: pretty(row.segment), strategy: pretty(row.strategy_id), market: `${row.symbol} ${row.timeframe}`, sample: row.completed, expectancy_r: row.net_expectancy_r, uncertainty: row.uncertainty, eligible_hypothesis: row.candidate_eligible ? "YES" : "NO" }))} empty="Minimum-sample pattern evidence is not available yet." /></section>
      <section><h3>Candidate registry</h3><DataTable rows={candidates.map((row) => ({ candidate: row.id.slice(0, 14), parent: row.parent_strategy_version, isolated_rule: row.rule_key, value: JSON.stringify(row.rule_value), status: row.status, live_execution: "DISABLED" }))} empty="No manually governed candidate has been proposed." /></section>
    </div>
    <div className="pa-shadow-comparison"><section><h3>Baseline vs candidate signals</h3><DataTable rows={signalRows} empty="No shadow observations yet." /></section><section><h3>Shadow outcome deltas</h3><DataTable rows={outcomeRows} empty="No shadow outcomes yet." /></section></div>
    <div className="pa-learning-warning"><b>Shadow interpretation</b><span>Avoided losses, missed winners, costs, drawdown and trade-count deltas are isolated PAPER observations—not untouched OOS evidence.</span><span>Official account affected: NO</span></div>
  </div>;
}

export default function PriceActionVisual() {
  const { toast } = useApp();
  const [mode, setMode] = useState<Mode>("live");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("5m");
  const [contracts, setContracts] = useState(["BTCUSDT", "ETHUSDT", "SOLUSDT"]);
  const [state, setState] = useState<PAState | null>(null);
  const [paper, setPaper] = useState<PaperState | null>(null);
  const [sessions, setSessions] = useState<{ id: string; status: string; symbol: string; timeframe: string; started_at: string }[]>([]);
  const [selectedSession, setSelectedSession] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [cursor, setCursor] = useState(200);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState(1);
  const [tab, setTab] = useState<BottomTab>("positions");
  const [fitSignal, setFitSignal] = useState(0);
  const [latestSignal, setLatestSignal] = useState(0);
  const [visibleBars, setVisibleBars] = useState(CLEAN_VISIBLE_BARS);
  const [chartPreset, setChartPreset] = useState<ChartPreset>("clean");
  const [selectedSetupId, setSelectedSetupId] = useState("");
  const [focusedSetupId, setFocusedSetupId] = useState("");
  const [marketBusy, setMarketBusy] = useState(false);
  const [strategyBusy, setStrategyBusy] = useState(false);
  const strategySaveInFlight = useRef(false);
  const [pendingMarket, setPendingMarket] = useState<{ symbol: string; timeframe: string; mode: Mode } | null>(null);
  const [activeStrategy, setActiveStrategy] = useState(STRATEGIES[0]);
  const [operatingMode, setOperatingMode] = useState<OperatingMode | "">("");
  const [riskPct, setRiskPct] = useState("0.5");
  const [filters, setFilters] = useState<NativeSMCOverlayFilters>(PRESET_FILTERS.clean);
  const [order, setOrder] = useState({ side: "buy", type: "market", quantity: "0.001", limit_price: "", stop_loss: "", take_profit: "" });
  const [journal, setJournal] = useState<PAJournalResponse | null>(null);
  const [selectedJournalId, setSelectedJournalId] = useState("");
  const [journalFilters, setJournalFilters] = useState<JournalFilters>({ strategy_id: "", direction: "", result: "", trigger_type: "", zone_type: "", touch_count: "", regime: "", rule_compliance: "", data_quality: "", entry_model: "", partition: "", strategy_version: "", date_from: "", date_to: "" });
  const [rulebook, setRulebook] = useState<RulebookState | null>(null);
  const [rulebookManifest, setRulebookManifest] = useState<RulebookManifest | null>(null);
  const [rulebookError, setRulebookError] = useState<string | null>(null);
  const [rulebookLoading, setRulebookLoading] = useState(false);
  const [learning, setLearning] = useState<PALearningAnalysis | null>(null);
  const [learningCandidates, setLearningCandidates] = useState<PALearningCandidate[]>([]);
  const [remediationSymbol, setRemediationSymbol] = useState("");
  const [remediationPhrase, setRemediationPhrase] = useState("");
  const [remediationAcknowledged, setRemediationAcknowledged] = useState(false);
  const [remediating, setRemediating] = useState(false);
  const [controlsOpen, setControlsOpen] = useState(() => typeof window === "undefined" || window.innerWidth > 820);
  const identityInitialized = useRef(false);
  const chartRequestSequence = useRef(0);
  const chartInFlight = useRef<{ key: string; sequence: number } | null>(null);
  const marketSwitchSequence = useRef(0);
  const marketSwitchQueue = useRef<Promise<void>>(Promise.resolve());

  const [identitySession, setIdentitySession] = useState<PaperState["session"] | null>(null);
  const savedSession = paper?.session.id ? paper.session : identitySession;
  const sessionLoaded = Boolean(savedSession?.id && savedSession.operating_mode);
  const applyPaperIdentity = useCallback((next: { session: PaperState["session"] }) => {
    const nextMode: Mode = next.session.mode === "HISTORICAL" ? "replay" : "live";
    setSymbol(next.session.symbol ?? "BTCUSDT");
    setTimeframe(next.session.timeframe ?? "5m");
    setMode(nextMode);
    setOperatingMode(next.session.operating_mode ?? "");
    setActiveStrategy(next.session.execution_config?.strategy_id ?? STRATEGIES[0]);
    setRiskPct(String(next.session.execution_config?.risk_pct ?? .5));
  }, []);

  const loadPaper = useCallback(async () => {
    try {
      const next = await apiGet<PaperState>("/research/price-action/paper");
      setPaper(next);
      setSelectedSession((current) => current || next.session.id);
      if (!identityInitialized.current) {
        identityInitialized.current = true;
        applyPaperIdentity(next);
      }
    } catch { /* chart remains usable */ }
  }, [applyPaperIdentity]);
  useEffect(() => {
    let cancelled = false;
    const hydrate = async () => {
      try {
        const payload = await apiGet<{ session: PaperState["session"] }>("/research/price-action/session");
        if (cancelled || !payload.session.id) return;
        setIdentitySession(payload.session);
        if (!identityInitialized.current) {
          identityInitialized.current = true;
          applyPaperIdentity(payload);
        }
      } catch { /* Keep saved mode explicitly unavailable. */ }
    };
    void hydrate();
    void apiGet<{ sessions: typeof sessions }>("/research/price-action/sessions").then(row => {
      if (!cancelled) setSessions(row.sessions);
    }).catch(() => undefined);
    const timer = window.setInterval(() => void hydrate(), 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [applyPaperIdentity]);
  const loadGovernance = useCallback(async () => {
    if (!paper?.session.id) return;
    const queryFilters = { ...journalFilters,
      date_to: journalFilters.date_to ? `${journalFilters.date_to}T23:59:59Z` : "" };
    const query = new URLSearchParams({
      session_id: paper.session.id, symbol, timeframe,
      ...Object.fromEntries(Object.entries(queryFilters).filter(([, value]) => value)),
    });
    try {
      const [journalState, analysis, candidates] = await Promise.all([
        apiGet<PAJournalResponse>(`/research/price-action/journal?${query.toString()}`),
        apiGet<PALearningAnalysis>("/research/price-action/learning/analysis?minimum_sample=30"),
        apiGet<{ candidates: PALearningCandidate[] }>("/research/price-action/learning/candidates"),
      ]);
      setJournal(journalState); setLearning(analysis); setLearningCandidates(candidates.candidates);
      setSelectedJournalId((current) => current && journalState.entries.some((row) => row.identity.journal_entry_id === current)
        ? current : journalState.entries[0]?.identity.journal_entry_id ?? "");
    } catch { /* research governance panels remain independently unavailable */ }
  }, [paper?.session.id, symbol, timeframe, journalFilters]);
  const loadChart = useCallback(async () => {
    if (!savedSession?.id) return;
    const key = `${savedSession.id}:${mode}:${symbol}:${timeframe}:${cursor}`;
    // A slow response must be allowed to complete, not superseded every 3s.
    if (chartInFlight.current?.key === key) return;
    const sequence = ++chartRequestSequence.current;
    chartInFlight.current = { key, sequence };
    const requestId = `${savedSession.id}:${mode}:${symbol}:${timeframe}:${sequence}`;
    setLoading(true);
    const path = mode === "live"
      ? `/research/price-action/live-chart?symbol=${symbol}&timeframe=${timeframe}&window=800&visible=500&request_id=${encodeURIComponent(requestId)}`
      : `/research/price-action/sessions/current/replay/step?symbol=${symbol}&timeframe=${timeframe}&cursor=${cursor}&limit=3000`;
    try {
      const next = mode === "live" ? await apiGet<PAState>(path) : await apiPostJson<PAState>(path, {});
      if (sequence !== chartRequestSequence.current) return;
      const identity = next.data_identity;
      const expectedSessionMode = mode === "live" ? "LIVE_PAPER" : "HISTORICAL";
      const matches = next.symbol === symbol && next.timeframe === timeframe &&
        identity?.session_id === savedSession.id && identity.symbol === symbol &&
        identity.timeframe === timeframe && identity.mode === expectedSessionMode &&
        (mode !== "live" || identity.request_id === requestId);
      if (!matches) throw new Error("Ignored stale market-data response with a different session, symbol or timeframe identity");
      setState(next); setError(null);
    } catch (reason) {
      if (sequence === chartRequestSequence.current) {
        setError(reason instanceof Error ? reason.message : "Price Action data unavailable");
      }
    } finally {
      if (chartInFlight.current?.sequence === sequence) chartInFlight.current = null;
      if (sequence === chartRequestSequence.current) setLoading(false);
    }
  }, [mode, symbol, timeframe, cursor, savedSession?.id]);

  const loadRulebook = useCallback(async () => {
    setRulebookLoading(true);
    setRulebookError(null);
    try {
      const [next, attestation] = await Promise.all([
        apiGet<RulebookState>(`/research/pa-rulebook/state?symbol=${encodeURIComponent(symbol)}`),
        apiGet<RulebookManifest>("/research/pa-rulebook/manifest"),
      ]);
      setRulebook(next);
      setRulebookManifest(attestation);
    } catch (exc) {
      // Named, not swallowed: "no cached candles" and "the engine crashed" are
      // different problems and the panel must not show the same blank for both.
      setRulebookError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setRulebookLoading(false);
    }
  }, [symbol]);

  // Only when the tab is open. This runs the engine over ~600 candles per call
  // and nothing else on the page needs the result.
  useEffect(() => { if (tab === "rulebook") void loadRulebook(); }, [tab, loadRulebook]);

  useEffect(() => { void apiGet<{ contracts: string[] }>("/research/price-action/contracts?limit=500").then((row) => setContracts(row.contracts)).catch(() => undefined); }, []);
  useEffect(() => { if (!identityInitialized.current || !savedSession?.id) return; void loadChart(); if (mode !== "live") return; const timer = window.setInterval(() => void loadChart(), 3_000); return () => window.clearInterval(timer); }, [loadChart, mode, savedSession?.id]);
  useEffect(() => { void loadPaper(); const timer = window.setInterval(() => void loadPaper(), 5_000); return () => window.clearInterval(timer); }, [loadPaper]);
  useEffect(() => { void loadGovernance(); const timer = window.setInterval(() => void loadGovernance(), 8_000); return () => window.clearInterval(timer); }, [loadGovernance]);
  useEffect(() => {
    if (mode !== "replay" || !replayPlaying) return;
    const timer = window.setInterval(() => setCursor((value) => {
      const total = state?.replay?.total ?? value;
      if (value >= total) { setReplayPlaying(false); return value; }
      return Math.min(total, value + 1);
    }), Math.max(40, 1000 / replaySpeed));
    return () => window.clearInterval(timer);
  }, [mode, replayPlaying, replaySpeed, state?.replay?.total]);
  const displayed = useMemo(() => state ? {
    ...state,
    setups: state.setups.filter((row) => row.strategy_id === activeStrategy),
    proposals: state.proposals.filter((row) => row.strategy_id === activeStrategy),
  } : null, [state, activeStrategy]);
  const setupChoices = useMemo(() => (displayed?.setups ?? []).slice(-100).reverse(), [displayed?.setups]);
  const selectedSetup = useMemo(() => setupChoices.find((row) => row.id === selectedSetupId) ??
    setupChoices.find((row) => !TERMINAL_SETUP_PHASES.has(row.phase)) ?? setupChoices[0] ?? null,
  [setupChoices, selectedSetupId]);
  const focusedSetup = useMemo(() => setupChoices.find((row) => row.id === focusedSetupId) ?? null,
    [setupChoices, focusedSetupId]);
  const chart = useMemo(() => displayed ? chartState(displayed, paper, chartPreset, focusedSetup) : null,
    [displayed, paper, chartPreset, focusedSetup]);
  const traces = state?.snapshot?.strategy_traces.filter((row) => row.strategy_id === activeStrategy) ?? [];
  const ready = traces.filter((row) => row.state === "ORDER_PENDING");
  const rejected = traces.filter((row) => row.state !== "ORDER_PENDING").map((row) => ({ strategy: row.strategy_id, direction: row.direction, missing: row.missing_conditions.join(", "), next: row.next_required_event }));
  const pendingPaperOrders = (paper?.orders ?? []).filter((row) => OPEN_BROKER_ORDER_STATUSES.has(String(row.status)));
  const researchOrderRows = (state?.orders ?? []).map((row) => ({ scope: "RESEARCH ENGINE · NOT PAPER BROKER", ...row }));
  const tradeRows = [
    ...(paper?.trades ?? []).map((row) => ({ scope: "PAPER", ...row })),
    ...(state?.trades ?? []).map((row) => ({ scope: "RESEARCH", ...row })),
  ];
  const selectedMetrics = state?.metrics.by_strategy?.[activeStrategy];
  const aggregateMetrics = state?.metrics;
  const healthState = error ? "ERROR" : marketBusy ? "CONNECTING" : mode === "replay" ? "REPLAY" : state?.live_display?.connection_state ?? "CONNECTING";
  const feedReliable = !error && !marketBusy && (mode === "replay" ||
    (state?.live_display?.reliable === true && state.live_display.new_entries_paused !== true));
  const lastClosed = state?.candles[state.candles.length - 1];
  const marketSelection = pendingMarket ?? { symbol, timeframe, mode };
  const focusedObjectIds = focusedSetup ? [focusedSetup.id, focusedSetup.zone_id, focusedSetup.trigger_event_id].filter((row): row is string => Boolean(row)) : [];
  const selectedJournal = journal?.entries.find((row) => row.identity.journal_entry_id === selectedJournalId) ?? null;
  const legacyPosition = paper?.positions.find((row) =>
    row.protection_status === "LEGACY_UNPROTECTED_REQUIRES_CLOSE_OR_PROTECTION") ?? null;

  const submitOrder = async () => {
    try {
      await apiPostJson("/research/price-action/paper/orders", {
        symbol, side: order.side, type: order.type, quantity: Number(order.quantity),
        limit_price: order.type === "limit" && order.limit_price ? Number(order.limit_price) : null,
        stop_price: order.type === "stop" && order.limit_price ? Number(order.limit_price) : null,
        stop_loss: order.stop_loss ? Number(order.stop_loss) : null,
        take_profit: order.take_profit ? Number(order.take_profit) : null,
      });
      toast("Price Action paper order accepted", "success"); setTab("orders"); await loadPaper();
    } catch (reason) { toast(reason instanceof Error ? reason.message : "Order failed", "error"); }
  };
  const setLeverage = async (leverage: number) => {
    try { setPaper(await apiPostJson<PaperState>("/research/price-action/paper/leverage", { leverage })); toast(`Price Action leverage set to ${leverage}x`, "success"); }
    catch (reason) { toast(reason instanceof Error ? reason.message : "Leverage update failed", "error"); }
  };
  const toggleLayer = (key: keyof NativeSMCOverlayFilters) => setFilters((row) => ({ ...row, [key]: !row[key] }));
  const changeVisibleBars = (value: number) => {
    setVisibleBars(value);
    window.requestAnimationFrame(() => setFitSignal((signal) => signal + 1));
  };
  const applyPreset = (preset: ChartPreset) => {
    setChartPreset(preset);
    setFilters(PRESET_FILTERS[preset]);
    if (preset !== "strategy") setFocusedSetupId("");
    if (preset === "clean") setVisibleBars(CLEAN_VISIBLE_BARS);
    window.requestAnimationFrame(() => {
      setFitSignal((signal) => signal + 1);
      setLatestSignal((signal) => signal + 1);
    });
  };
  const focusSelectedSetup = () => {
    if (!selectedSetup) return;
    setFocusedSetupId(selectedSetup.id);
    applyPreset("strategy");
  };
  const selectJournal = (journalId: string) => {
    setSelectedJournalId(journalId);
    const entry = journal?.entries.find((row) => row.identity.journal_entry_id === journalId);
    if (!entry || !setupChoices.some((row) => row.id === entry.setup.setup_id)) return;
    setActiveStrategy(entry.identity.strategy_id);
    setSelectedSetupId(entry.setup.setup_id); setFocusedSetupId(entry.setup.setup_id);
    applyPreset("strategy");
  };
  /** Save one strategy change the moment it is made.
   *
   * The strategy and mode pickers used to be drafts that only an "Apply"
   * press sent, so a strategy picked without Apply was never saved and the
   * page went back to the saved one. Every field NOT being changed is sent as
   * the server holds it -- the endpoint fills a missing field with its
   * default -- and a refusal puts the controls back to what is saved.
   */
  const saveStrategy = async (change: { strategy_id?: string; operating_mode?: OperatingMode; risk_pct?: number }, what: string) => {
    if (!savedSession?.id || !savedSession.operating_mode || strategySaveInFlight.current || marketBusy) return;
    strategySaveInFlight.current = true;
    setStrategyBusy(true);
    try {
      const updated = await apiPostJson<PaperState>("/research/price-action/sessions/current/configuration", {
        mode: savedSession.mode ?? "LIVE_PAPER", symbol: savedSession.symbol, timeframe: savedSession.timeframe,
        operating_mode: savedSession.operating_mode,
        strategy_id: savedSession.execution_config?.strategy_id ?? STRATEGIES[0],
        risk_pct: savedSession.execution_config?.risk_pct ?? .5, ...change,
      });
      setPaper(updated);
      setOperatingMode(updated.session.operating_mode ?? "");
      setActiveStrategy(updated.session.execution_config?.strategy_id ?? STRATEGIES[0]);
      setRiskPct(String(updated.session.execution_config?.risk_pct ?? .5));
      toast(`Saved: ${what}. The bot now trades ${pretty(updated.session.execution_config?.strategy_id ?? STRATEGIES[0])} on ${updated.session.symbol} ${updated.session.timeframe}.`, "success");
    } catch (reason) {
      setOperatingMode(savedSession.operating_mode ?? "");
      setActiveStrategy(savedSession.execution_config?.strategy_id ?? STRATEGIES[0]);
      setRiskPct(String(savedSession.execution_config?.risk_pct ?? .5));
      toast(`Not saved: ${reason instanceof Error ? reason.message : "the server refused the change"}`, "error");
    } finally {
      strategySaveInFlight.current = false;
      setStrategyBusy(false);
    }
  };
  const saveRisk = () => {
    const value = Number(riskPct);
    if (!savedSession?.id || !Number.isFinite(value) || value === savedSession.execution_config?.risk_pct) return;
    void saveStrategy({ risk_pct: value }, `risk ${value}% per trade`);
  };
  const approve = async (proposalId: string) => {
    try { await apiPostJson(`/research/price-action/paper/candidates/${proposalId}/approve`, {}); toast("Paper candidate approved", "success"); await loadPaper(); }
    catch (reason) { toast(reason instanceof Error ? reason.message : "Approval failed", "error"); }
  };
  const resetSession = async () => {
    const phrase = window.prompt("Reset this Price Action paper session? Type RESET PRICE ACTION PAPER exactly. Prior session snapshots and audit records are preserved.");
    if (phrase !== "RESET PRICE ACTION PAPER") return;
    try {
      const updated = await apiPostJson<PaperState>("/research/price-action/paper/reset", { confirmation: phrase });
      identityInitialized.current = true; chartRequestSequence.current += 1;
      setPaper(updated); applyPaperIdentity(updated); setState(null);
      toast("Fresh Price Action paper session created", "success");
    }
    catch (reason) { toast(reason instanceof Error ? reason.message : "Reset failed", "error"); }
  };
  const sessionAction = async (action: "start" | "resume" | "duplicate" | "end") => {
    try {
      let updated: PaperState;
      if (action === "start") updated = await apiPostJson<PaperState>("/research/price-action/sessions", { mode: mode === "live" ? "LIVE_PAPER" : "HISTORICAL", symbol, timeframe, starting_balance: paper?.account.starting_balance ?? 10_000, operating_mode: operatingMode, strategy_id: activeStrategy, risk_pct: Number(riskPct) });
      else if (action === "end") { await apiPostJson("/research/price-action/sessions/current/end", {}); await loadPaper(); toast("Session ended and snapshotted", "success"); return; }
      else updated = await apiPostJson<PaperState>(`/research/price-action/sessions/${selectedSession}/${action}`, {});
      identityInitialized.current = true; chartRequestSequence.current += 1;
      setPaper(updated); applyPaperIdentity(updated); setState(null);
      setSelectedSession(updated.session.id); await loadPaper(); toast(`Session ${action} complete`, "success");
    } catch (reason) { toast(reason instanceof Error ? reason.message : `Session ${action} failed`, "error"); }
  };
  const confirmMarketChange = (nextSymbol: string, nextTimeframe: string, nextMode: Mode = mode) => {
    if (nextSymbol === symbol && nextTimeframe === timeframe && nextMode === mode) return;
    // Never while a strategy save is in flight: this request resends the saved
    // mode/strategy/risk, and the copy it holds would undo that save.
    if (strategySaveInFlight.current) return;
    const exposed = Boolean((paper?.positions.length ?? 0) + (paper?.orders.filter((row) => ["open", "partially_filled", "triggered"].includes(String(row.status))).length ?? 0));
    if (exposed) {
      toast("Close or cancel this Price Action session's paper exposure before changing its market, timeframe or mode", "error");
      return;
    }
    const sequence = ++marketSwitchSequence.current;
    setPendingMarket({ symbol: nextSymbol, timeframe: nextTimeframe, mode: nextMode });
    setMarketBusy(true);
    marketSwitchQueue.current = marketSwitchQueue.current.catch(() => undefined).then(async () => {
      if (sequence !== marketSwitchSequence.current) return;
      try {
        const updated = await apiPostJson<PaperState>("/research/price-action/sessions/current/configuration", {
          mode: nextMode === "live" ? "LIVE_PAPER" : "HISTORICAL", symbol: nextSymbol,
          timeframe: nextTimeframe, operating_mode: savedSession?.operating_mode,
          strategy_id: savedSession?.execution_config?.strategy_id,
          risk_pct: savedSession?.execution_config?.risk_pct,
        });
        if (sequence !== marketSwitchSequence.current) return;
        chartRequestSequence.current += 1;
        setPaper(updated); setSymbol(nextSymbol); setTimeframe(nextTimeframe); setMode(nextMode);
        setState(null); setError(null); setCursor(200); setFocusedSetupId("");
        toast(`Price Action session synchronized to ${nextSymbol} ${nextTimeframe}`, "success");
      } catch (reason) {
        if (sequence === marketSwitchSequence.current) toast(reason instanceof Error ? reason.message : "Market synchronization failed", "error");
      } finally {
        if (sequence === marketSwitchSequence.current) { setPendingMarket(null); setMarketBusy(false); }
      }
    });
  };

  const reconcileOrders = async () => {
    try {
      const result = await apiPostJson<{ actions: Record<string, any>[]; after: PaperState["order_audit"] }>("/research/price-action/paper/orders/reconcile", {});
      toast(result.actions.length ? `${result.actions.length} stale strategy order record(s) reconciled` : "Pending paper orders are already reconciled", "success");
      await loadPaper();
    } catch (reason) { toast(reason instanceof Error ? reason.message : "Order reconciliation failed", "error"); }
  };

  const closeLegacyPosition = async () => {
    if (!remediationSymbol || remediationPhrase !== "CLOSE LEGACY PAPER POSITION" ||
        !remediationAcknowledged) return;
    setRemediating(true);
    try {
      const result = await apiPostJson<{
        paper: PaperState;
        readiness: { ready: boolean; blockers: string[] };
        remediation_id: string;
        journal_id: string;
      }>(`/research/price-action/paper/positions/${remediationSymbol}/legacy-remediation-close`, {
        confirmation: remediationPhrase,
        acknowledge_missing_historical_protection: remediationAcknowledged,
      });
      setPaper(result.paper);
      setRemediationSymbol(""); setRemediationPhrase(""); setRemediationAcknowledged(false);
      await Promise.all([loadPaper(), loadGovernance(), loadChart()]);
      toast(result.readiness.ready
        ? "Legacy PAPER position closed; all readiness gates passed"
        : `Legacy PAPER position closed; execution remains blocked: ${result.readiness.blockers.join(" · ")}`,
      result.readiness.ready ? "success" : "error");
    } catch (reason) {
      toast(reason instanceof Error ? reason.message : "Legacy position remediation failed", "error");
    } finally { setRemediating(false); }
  };

  return <div className="pa-lab">
    <header className="pa-titlebar">
      <div><span className="pa-kicker">ISOLATED FORWARD-PAPER</span><h1>Price Action Visual Lab</h1><p>Live Binance USD-M data · simulated orders · no exchange routing</p></div>
      <button type="button" className="pa-controls-toggle" onClick={() => setControlsOpen((open) => !open)} aria-expanded={controlsOpen}>Controls</button>
      <div className="pa-safety"><b>{!sessionLoaded ? "LOADING SESSION" : savedSession?.operating_mode === "signals_only" ? "SIGNALS_ONLY" : "ISOLATED_FORWARD_PAPER"}</b><span>LIVE ROUTING DISABLED</span></div>
    </header>
    <div className={`pa-health-scope ${feedReliable ? "is-healthy" : "is-stale"}`}>
      <b>PRICE ACTION SESSION</b><span>Candles / quote / mark: {healthState}</span>
      <span>Decision readiness: {feedReliable ? "CLOSED-BAR ELIGIBLE" : "PAUSED · FAIL CLOSED"}</span>
      <span>Paper execution: {feedReliable ? "ELIGIBLE UNDER SAVED MODE" : "BLOCKED"}</span>
      <small>This page and footer report only the isolated Price Action account.</small>
    </div>

    <div className="pa-workspace">
      <aside className={`pa-sidebar ${controlsOpen ? "is-open" : ""}`} aria-label="Price Action controls">
        <section><h2>PA session market</h2><label>Binance USDⓈ-M contract<select aria-label="Price Action session symbol" disabled={marketBusy || strategyBusy} value={marketSelection.symbol} onChange={(event) => confirmMarketChange(event.target.value, timeframe)}>{contracts.map((row) => <option key={row}>{row}</option>)}</select></label><div className="pa-segment"><button disabled={marketBusy || strategyBusy} className={marketSelection.mode === "live" ? "active" : ""} onClick={() => confirmMarketChange(symbol, timeframe, "live")}>Live paper</button><button disabled={marketBusy || strategyBusy} className={marketSelection.mode === "replay" ? "active" : ""} onClick={() => confirmMarketChange(symbol, timeframe, "replay")}>Replay</button></div><small className="pa-context-note">Independent Price Action research session. The global header remains the selected Trading Instance context.</small>{marketBusy ? <small className="pa-syncing">Synchronizing session, feed and chart…</small> : null}</section>
        <section><h2>Strategy &amp; execution</h2><p className="pa-saved-config" data-testid="pa-saved-configuration">{savedSession?.id ? <><b>{pretty(savedSession.execution_config?.strategy_id ?? STRATEGIES[0])}</b><span>{savedSession.symbol} {savedSession.timeframe} · {PA_MODE_LABEL[savedSession.operating_mode ?? ""] ?? "—"} · risk {savedSession.execution_config?.risk_pct ?? .5}%</span></> : "Loading…"}</p><label>Strategy<select aria-label="Price Action strategy" disabled={strategyBusy || marketBusy || !sessionLoaded} value={activeStrategy} onChange={(event) => { const next = event.target.value; setActiveStrategy(next); setFocusedSetupId(""); void saveStrategy({ strategy_id: next }, `strategy ${pretty(next)}`); }}>{STRATEGIES.map((id) => <option key={id} value={id}>{pretty(id)}</option>)}</select></label><label>Mode<select aria-label="Paper operating mode" disabled={strategyBusy || marketBusy || !sessionLoaded} value={operatingMode} onChange={(event) => { const next = event.target.value as OperatingMode; setOperatingMode(next); void saveStrategy({ operating_mode: next }, `mode ${PA_MODE_LABEL[next]}`); }}>{!sessionLoaded && <option value="">Loading saved mode…</option>}<option value="signals_only">Signals only</option><option value="manual_approval">Manual approval</option><option value="automatic">Automatic paper</option></select></label><label>Risk per trade %<input aria-label="Price Action risk per trade" value={riskPct} disabled={strategyBusy || marketBusy || !sessionLoaded} onChange={(event) => setRiskPct(event.target.value)} onBlur={saveRisk} onKeyDown={(event) => { if (event.key === "Enter") saveRisk(); }} inputMode="decimal" /></label><small>Changes save instantly.</small></section>
        <section><h2>Chart layers</h2><label className="pa-layer-mode">Preset<select aria-label="Chart layer preset" value={chartPreset} onChange={(event) => applyPreset(event.target.value as ChartPreset)}>{(["clean", "structure", "zones", "strategy", "trades", "debug"] as ChartPreset[]).map((preset) => <option key={preset} value={preset}>{pretty(preset)}</option>)}</select></label>{([['pivots','Swings'], ['structure','Events'], ['orderBlocks','S/R zones'], ['mitigated','Invalidated · lifecycle'], ['labels','Labels']] as [keyof NativeSMCOverlayFilters, string][]).map(([key, label]) => <label className="pa-check" key={key}><input type="checkbox" checked={filters[key]} onChange={() => toggleLayer(key)} /><span>{label}</span></label>)}<label className="pa-layer-mode">Selected setup<select aria-label="Selected Price Action setup" value={selectedSetup?.id ?? ""} onChange={(event) => setSelectedSetupId(event.target.value)}><option value="">Latest relevant setup</option>{setupChoices.map((row) => <option key={row.id} value={row.id}>{pretty(row.strategy_id)} · {row.direction} · {row.phase}</option>)}</select></label><button className="pa-focus-setup" disabled={!selectedSetup} onClick={focusSelectedSetup}>Focus selected setup</button><small>Presets affect rendering only. All zones, setups, orders and trades remain in the audit records below.</small></section>
        <section><h2>Virtual account</h2><div className="pa-account"><span>Balance<b>{money(paper?.account.balance)} USDT</b></span><span>Equity<b>{money(paper?.account.equity)} USDT</b></span><span>Open P&amp;L<b className={(paper?.account.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}>{money(paper?.account.unrealized_pnl)}</b></span><span>Free margin<b>{money(paper?.account.free_margin)}</b></span></div><label>Isolated leverage<select value={paper?.account.leverage ?? 1} onChange={(event) => void setLeverage(Number(event.target.value))}>{Array.from({ length: 20 }, (_, index) => index + 1).map((value) => <option key={value} value={value}>{value}×</option>)}</select></label><small>Persistent and isolated from every other paper account.</small></section>
        <section className="pa-legend"><h2>Chart truth</h2><span><i className="confirmed" />Confirmed</span><span><i className="provisional" />Forming · display only</span><span><i className="invalid" />Invalidated</span></section>
      </aside>

      <main className="pa-main">
        <div className="pa-toolbar"><div className="pa-symbol"><i className={feedReliable && !error ? "live" : "stale"} />{symbol}<span>PA SESSION · PERPETUAL</span></div><div className="pa-timeframes">{(state?.mtf_policy?.available_entry_timeframes ?? ["5m"]).map((row) => <button key={row} disabled={marketBusy || strategyBusy} className={row === marketSelection.timeframe ? "active" : ""} onClick={() => confirmMarketChange(symbol, row)}>{row}</button>)}</div><label className="pa-view-bars">View<select aria-label="Visible chart candles" value={visibleBars} onChange={(event) => changeVisibleBars(Number(event.target.value))}>{[48, 72, 120, 240].map((value) => <option key={value} value={value}>{value} bars</option>)}</select></label><button onClick={() => setFitSignal((n) => n + 1)}>Fit</button><button onClick={() => setLatestSignal((n) => n + 1)}>Latest</button><button type="button" className="pa-clean-view" onClick={() => applyPreset("clean")}>Clean view</button><span className={`pa-mode-chip ${feedReliable ? "" : "is-stale"}`}>{mode === "live" ? healthState : "REPLAY"}</span></div>
        {mode === "replay" ? <div className="pa-replay"><button onClick={() => setCursor(1)}>Restart</button><button onClick={() => setReplayPlaying((value) => !value)}>{replayPlaying ? "Pause" : "Play"}</button><button onClick={() => setCursor((n) => Math.max(1, n - 1))}>◀</button><input aria-label="Replay candle cursor" type="range" min="1" max={state?.replay?.total ?? 1000} value={Math.min(cursor, state?.replay?.total ?? cursor)} onChange={(event) => setCursor(Number(event.target.value))} /><button disabled={!state?.replay?.has_next} onClick={() => setCursor((n) => n + 1)}>▶</button><select aria-label="Replay speed" value={replaySpeed} onChange={(event) => setReplaySpeed(Number(event.target.value))}>{[1, 2, 5, 10, 25, 100].map((value) => <option key={value} value={value}>{value === 100 ? "Maximum" : `${value}×`}</option>)}</select><span>Candle {state?.replay?.cursor ?? cursor} / {state?.replay?.total ?? "—"} · future bars hidden</span></div> : null}
        <div className="pa-chart-shell">
          <div className="pa-chart-head"><div><b>{symbol} · {timeframe}</b><span>{state?.mtf_policy?.label ?? "Native MTF context loading"}</span><span>{state?.data_provenance?.exchange ?? "Binance USDⓈ-M Futures"} · session {savedSession?.id?.slice(0, 8) ?? "loading"}</span></div><div><span>{selectedMetrics ? `${pretty(activeStrategy)} · Net ${selectedMetrics.net_r.toFixed(2)}R · Execution R ${Number(selectedMetrics.gross_r ?? 0).toFixed(2)}R · Commission ${selectedMetrics.costs_r.toFixed(2)}R · ${selectedMetrics.wins}W/${selectedMetrics.losses}L · ${selectedMetrics.unfilled} unfilled` : `${pretty(activeStrategy)} · metric scope unavailable`}</span><span>{state?.snapshot?.structure_bias?.toUpperCase() ?? "NEUTRAL"}</span><b>{ready.length ? `${ready.length} READY` : "WAIT"}</b></div></div>
          {aggregateMetrics ? <div className="pa-metric-scope"><b>Selected strategy shown above</b><span>All PA1–PA4 aggregate remains {aggregateMetrics.net_r.toFixed(2)}R across {aggregateMetrics.closed} closed trades; it is not the selected-strategy result.</span><span>Dataset {String(state?.metrics_scope?.dataset_start ?? "—")} → {String(state?.metrics_scope?.dataset_end ?? "—")}</span><span>Config {String(state?.metrics_scope?.configuration_id ?? "—").slice(0, 12)} · funding {String(state?.metrics_scope?.cost_model?.funding_coverage ?? "—")}</span></div> : null}
          {error ? <div className="pa-error"><b>Market data unavailable</b><span>{error}</span><button onClick={() => void loadChart()}>Retry</button></div> : null}
          {!chart ? <div className="pa-loading">{loading ? "Loading and reconciling Binance market streams…" : "No identity-verified candle state"}</div> : <NativeSMCChartOverlay state={chart} timeframe={timeframe} rightOffsetBars={8} initialVisibleBars={visibleBars} filters={filters} highlightedObjectIds={focusedObjectIds} centerTimestamp={focusedSetup?.created_at} onCandleSelect={() => undefined} fitContentSignal={fitSignal} latestSignal={latestSignal} modelLabel="native price action" height="clamp(520px, 58vh, 680px)" liveDataStale={!feedReliable || Boolean(error)} />}
          <div className={`pa-stream-truth ${feedReliable ? "is-healthy" : "is-stale"}`}><b>{healthState}</b><span>{mode === "replay" ? "Historical replay is isolated from live streams" : error ?? state?.live_display?.health_reason ?? "Waiting for identity-bound feed reconciliation"}</span><span>Transport {state?.live_display?.transport_state ?? "—"} · entries {!feedReliable ? "PAUSED" : "ELIGIBLE ON CLOSED BARS"}</span></div>
          <div className="pa-market-readout"><span>Last completed candle<b>{lastClosed ? `${stamp(lastClosed.timestamp)} · C ${lastClosed.close.toLocaleString()}` : "—"}</b><small>Age {age(state?.live_display?.closed_candle_age_seconds)}</small></span><span>Forming candle · display only<b>{state?.forming_candle ? `${stamp(state.forming_candle.timestamp)} · O ${state.forming_candle.open.toLocaleString()} H ${state.forming_candle.high.toLocaleString()} L ${state.forming_candle.low.toLocaleString()} C ${state.forming_candle.close.toLocaleString()}` : "Not available"}</b><small>Stream age {age(state?.live_display?.candle_age_seconds)}</small></span><span>Live bid / ask<b>{state?.live_display?.bid?.toLocaleString() ?? "—"} / {state?.live_display?.ask?.toLocaleString() ?? "—"}</b><small>Age {age(state?.live_display?.quote_age_seconds)}</small></span><span>Mark price<b>{state?.live_display?.mark?.toLocaleString() ?? "—"}</b><small>Age {age(state?.live_display?.mark_age_seconds)} · deviation {state?.live_display?.candle_quote_deviation_bps?.toFixed(2) ?? "—"} bps</small></span></div>
          <div className="pa-chart-foot"><span><i className={feedReliable ? "live" : "stale"} />{mode === "live" ? `Binance · ${healthState}` : "Verified historical cache"}</span><span>Updated {stamp(state?.live_display?.last_update)}</span><span>Quote source {state?.live_display?.quote_source ?? "—"}</span><span>Closed candles used: {String(state?.data_provenance?.closed_candles_used ?? state?.candles.length ?? 0)}</span><span>Forming candle excluded from strategy: {state?.forming_candle ? "YES" : "N/A"}</span><b>PAPER · NO LIVE EXECUTION PATH</b></div>
        </div>

        <div className="pa-bottom">
          <nav>{TABS.map((row) => <button key={row} className={tab === row ? "active" : ""} onClick={() => setTab(row)}>{row}<em>{row === "positions" ? paper?.positions.length ?? 0 : row === "orders" ? pendingPaperOrders.length : row === "trades" ? tradeRows.length : row === "setups" ? state?.setups.length ?? 0 : row === "rejected" ? rejected.length : row === "journal" ? journal?.statistics.setups ?? 0 : row === "learning" ? learningCandidates.length : row === "rulebook" ? (rulebook?.zones.length ?? "") : ""}</em></button>)}</nav>
          <div className={`pa-bottom-body ${tab === "journal" || tab === "learning" || tab === "rulebook" ? "is-governance" : ""}`}>
            {tab === "positions" ? <>
              {legacyPosition ? <div className="pa-order-audit">
                <b>LEGACY / UNPROTECTED PAPER POSITION</b>
                <span>{String(legacyPosition.symbol)} · {String(legacyPosition.side)} · {String(legacyPosition.size)}</span>
                <span>Stop loss, take profit, risk and R:R are unverified and will not be invented.</span>
                <small>This cleanup closes only the isolated paper position at the current reconciled mark reference. The original position and remediation evidence remain in the immutable journal.</small>
                <button type="button" className="btn-danger" onClick={() => setRemediationSymbol(String(legacyPosition.symbol))}>Close legacy paper position</button>
              </div> : null}
              <DataTable rows={paper?.positions ?? []} empty="No open Price Action paper positions." />
            </> : null}
            {tab === "orders" ? <><div className="pa-order-ticket"><select aria-label="Paper order side" value={order.side} onChange={(e) => setOrder({ ...order, side: e.target.value })}><option value="buy">Buy / Long</option><option value="sell">Sell / Short</option></select><select aria-label="Paper order type" value={order.type} onChange={(e) => setOrder({ ...order, type: e.target.value })}><option value="market">Market</option><option value="limit">Limit</option><option value="stop">Stop</option></select><input aria-label="Paper order quantity" value={order.quantity} onChange={(e) => setOrder({ ...order, quantity: e.target.value })} inputMode="decimal" placeholder="Quantity" />{order.type !== "market" ? <input aria-label="Paper order trigger or limit price" value={order.limit_price} onChange={(e) => setOrder({ ...order, limit_price: e.target.value })} inputMode="decimal" placeholder="Trigger / limit price" /> : null}<input aria-label="Paper order stop loss" value={order.stop_loss} onChange={(e) => setOrder({ ...order, stop_loss: e.target.value })} inputMode="decimal" placeholder="Stop loss · required" /><input aria-label="Paper order take profit" value={order.take_profit} onChange={(e) => setOrder({ ...order, take_profit: e.target.value })} inputMode="decimal" placeholder="Take profit · required" /><button onClick={() => void submitOrder()}>Place protected paper order</button><button className="pa-reconcile" onClick={() => void reconcileOrders()}>Reconcile strategy orders</button><span>PAPER · {pendingPaperOrders.length} pending</span></div><div className="pa-order-audit"><b>Pending paper audit</b><span>Total {paper?.order_audit?.pending_paper_orders ?? pendingPaperOrders.length}</span><span>Strategy {paper?.order_audit?.pending_strategy_orders ?? 0}</span><span>Manual {paper?.order_audit?.pending_manual_orders ?? 0}</span><span>Duplicates {paper?.order_audit?.duplicate_strategy_orders.length ?? 0}</span><span>Discrepancies {paper?.order_audit?.discrepancies.length ?? 0}</span><small>Every new paper entry requires durable stop-loss and take-profit protection.</small></div>{(paper?.candidates ?? []).filter((row) => row.status === "PENDING_APPROVAL").map((row) => <div className="pa-order-ticket" key={row.proposal_id}><b>{String(row.payload.proposal?.strategy_id ?? "Strategy")} · {String(row.payload.proposal?.direction ?? "—")}</b><span>{String(row.payload.reason ?? "Awaiting approval")}</span><button onClick={() => void approve(row.source_proposal_id)}>Approve paper order</button></div>)}<h3 className="pa-table-heading">Pending paper broker orders</h3><DataTable rows={pendingPaperOrders} empty="No pending paper broker orders." /><h3 className="pa-table-heading">Research-engine orders · not paper broker orders</h3><DataTable rows={researchOrderRows} empty="No normalized research-engine orders in this candle window." /></> : null}
            {tab === "trades" ? <DataTable rows={tradeRows} empty="No completed paper fills or normalized research outcomes." /> : null}
            {tab === "setups" ? <DataTable rows={(state?.setups ?? []) as unknown as Record<string, any>[]} empty="No confirmed setups in the visible engine state." /> : null}
            {tab === "rejected" ? <DataTable rows={rejected} empty="No rejected or waiting strategy traces." /> : null}
            {tab === "rulebook" ? <RulebookPanel state={rulebook} manifest={rulebookManifest} error={rulebookError} loading={rulebookLoading} onReload={() => void loadRulebook()} symbol={symbol} /> : null}
            {tab === "journal" ? <JournalPanel journal={journal} selected={selectedJournal} selectedId={selectedJournalId} onSelect={selectJournal} filters={journalFilters} onFilters={setJournalFilters} sessionId={paper?.session.id} symbol={symbol} timeframe={timeframe} /> : null}
            {tab === "learning" ? <LearningPanel analysis={learning} candidates={learningCandidates} /> : null}
            {tab === "session" ? <><div className="pa-session"><span>Session ID<b>{paper?.session.id ?? "—"}</b></span><span>Started<b>{stamp(paper?.session.started_at)}</b></span><span>Starting balance<b>{money(paper?.session.starting_balance)} USDT</b></span><span>Status<b>{paper?.session.status?.toUpperCase() ?? "—"}</b></span><span>Operating mode<b>{sessionLoaded ? pretty(savedSession?.operating_mode ?? "") : "Loading"}</b></span></div><div className="pa-order-ticket"><select aria-label="Saved Price Action session" value={selectedSession} onChange={(event) => setSelectedSession(event.target.value)}>{sessions.map((row) => <option key={row.id} value={row.id}>{row.symbol} · {row.timeframe} · {row.status} · {stamp(row.started_at)}</option>)}</select><button onClick={() => void sessionAction("start")}>Start new</button><button disabled={!selectedSession} onClick={() => void sessionAction("resume")}>Resume</button><button disabled={!selectedSession} onClick={() => void sessionAction("duplicate")}>Duplicate</button><button disabled={!paper?.session.id} onClick={() => void sessionAction("end")}>End</button><button className="pa-export" onClick={() => void apiDownload("/research/price-action/paper/export", `price-action-session-${paper?.session.id ?? "current"}.json`)}>Export</button><button className="btn-danger" onClick={() => void resetSession()}>Reset</button></div><DataTable rows={paper?.activity ?? []} empty="No session audit events yet." /></> : null}
            {tab === "connection" ? <div className="pa-session"><span>Exchange<b>Binance USDⓈ-M Futures</b></span><span>Overall health<b>{healthState}</b></span><span>Transport<b>{state?.live_display?.transport_state ?? (mode === "replay" ? "ISOLATED" : "CONNECTING")}</b></span><span>Candle stream<b>{age(state?.live_display?.candle_age_seconds)}</b></span><span>Bid / ask stream<b>{age(state?.live_display?.quote_age_seconds)}</b></span><span>Mark stream<b>{age(state?.live_display?.mark_age_seconds)}</b></span><span>Failing dependency<b>{state?.live_display?.failing_dependency ?? "None"}</b></span><span>Last successful event<b>{state?.live_display?.last_successful_event ? `${state.live_display.last_successful_event.kind} · ${state.live_display.last_successful_event.at}` : "—"}</b></span><span>Retry state<b>{state?.live_display?.retry_state?.automatic_retry ? `automatic · attempt ${state.live_display.retry_state.attempt ?? 0}` : "—"}</b></span><span>Reconciliation<b>{state?.live_display?.health_reason ?? "—"}</b></span><span>New entries<b>{!feedReliable ? "PAUSED · FAIL CLOSED" : "CLOSED BARS ONLY"}</b></span><span>Real execution<b>DISABLED</b></span></div> : null}
          </div>
        </div>
        <ResearchComparisonPanel engine="PA" />
      </main>
    </div>
    <Modal open={Boolean(remediationSymbol)} title="Close legacy PAPER position?" onClose={() => {
      if (remediating) return;
      setRemediationSymbol(""); setRemediationPhrase(""); setRemediationAcknowledged(false);
    }}>
      <p>This is a <b>paper-account cleanup</b>. It will close the unprotected {remediationSymbol} paper position using the current reconciled Binance mark as the reference. The paper broker’s configured spread, slippage and fee model still applies.</p>
      <p>No historical stop loss, take profit, risk amount or R:R will be reconstructed. The original position snapshot and the completed remediation will be preserved in the audit and immutable journal.</p>
      <label className="pa-check"><input type="checkbox" checked={remediationAcknowledged} onChange={(event) => setRemediationAcknowledged(event.target.checked)} /><span>I understand the historical protection is unavailable and this action is PAPER only.</span></label>
      <label>Type <b>CLOSE LEGACY PAPER POSITION</b><input aria-label="Legacy remediation confirmation" value={remediationPhrase} onChange={(event) => setRemediationPhrase(event.target.value)} autoComplete="off" /></label>
      <p className="dim">Closing the position does not bypass readiness. Market data, saved Automatic Paper mode, balance, risk, stop and exit gates will be recalculated before any future entry is eligible.</p>
      <div className="modal-actions">
        <button type="button" className="btn btn-soft" disabled={remediating} onClick={() => { setRemediationSymbol(""); setRemediationPhrase(""); setRemediationAcknowledged(false); }}>Cancel</button>
        <button type="button" className="btn btn-danger" disabled={remediating || !remediationAcknowledged || remediationPhrase !== "CLOSE LEGACY PAPER POSITION"} onClick={() => void closeLegacyPosition()}>{remediating ? "Closing paper position…" : "Close PAPER position"}</button>
      </div>
    </Modal>
  </div>;
}
