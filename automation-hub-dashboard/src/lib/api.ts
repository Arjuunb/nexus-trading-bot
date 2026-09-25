// Live API client for the Automation Hub backend (FastAPI).
//
// Base URL is configurable via VITE_API_BASE (defaults to the local backend).
// Browser calls use the signed, HttpOnly session cookie. When the backend isn't running, hooks expose `error`
// so pages can show a "start the backend" hint instead of fake data.
import { useCallback, useEffect, useState } from "react";

// Runtime config: when the backend serves this app (single-origin on Render) it
// injects window.__HUB_CONFIG__ with apiBase="" (same origin). No control
// credential is ever included in browser JavaScript.
// Otherwise fall back to Vite build-time env (Vercel / local dev).
const _cfg = (typeof window !== "undefined" ? (window as any).__HUB_CONFIG__ : undefined) ?? {};
export const API_BASE = (_cfg.apiBase ?? (import.meta.env.VITE_API_BASE as string | undefined)) ?? "http://localhost:8000";
const requestOptions: RequestInit = { credentials: "include" };

async function requestError(res: Response, path: string): Promise<Error> {
  const prefix = `${path}: HTTP ${res.status}`;
  try {
    const payload = await res.json();
    const detail = payload?.detail ?? payload?.error;
    if (typeof detail === "string" && detail) return new Error(`${prefix} · ${detail}`);
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      return new Error(`${prefix} · ${detail.field ? `${detail.field}: ` : ""}${detail.message}`);
    }
  } catch { /* keep the safe HTTP status when the error has no JSON body */ }
  return new Error(prefix);
}

export async function apiGet<T>(path: string): Promise<T> {
  // session cookie authenticates same-origin; the secret header keeps
  // cross-origin dev (Vite -> localhost API) working behind the auth wall
  const res = await fetch(`${API_BASE}${path}`, requestOptions);
  if (!res.ok) throw await requestError(res, `GET ${path}`);
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) throw await requestError(res, `POST ${path}`);
  return res.json() as Promise<T>;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE", credentials: "include" });
  if (!res.ok) throw await requestError(res, `DELETE ${path}`);
  return res.json() as Promise<T>;
}

export async function apiPostJson<T>(path: string, body: unknown, extraHeaders: Record<string, string> = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...extraHeaders }, credentials: "include",
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await requestError(res, `POST ${path}`);
  return res.json() as Promise<T>;
}

export async function apiPatchJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" }, credentials: "include",
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await requestError(res, `PATCH ${path}`);
  return res.json() as Promise<T>;
}

/** Authenticated file download (exports). Fetches with the secret header so it
 *  works cross-origin, then triggers a browser download of the real payload. */
export async function apiDownload(path: string, filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, requestOptions);
  if (!res.ok) throw new Error(`GET ${path} → ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

/** The backend origin — used to redirect to the server-rendered /login on logout. */
export const APP_ORIGIN = API_BASE;

export interface BotSettings {
  editable: {
    risk_per_trade_pct: number; exposure_limit_pct: number; max_drawdown_pct: number;
    max_open_positions: number; dedup_window_s: number;
    max_daily_loss_pct: number; session_start: number; session_end: number;
    max_weekly_loss_pct: number; max_trades_per_day: number;
    max_consecutive_losses: number; cooldown_after_loss_min: number;
    trading_days_mask: number;
    entry_mode: string; daily_report_hour: number;
    min_quality_score: number; streak_risk_scaling: boolean;
    position_sizing_mode: "auto" | "fixed"; fixed_position_size: number;
  };
  readonly: {
    strategy: string; strategy_key: string; timeframe: string; symbols: string[];
    starting_cash: number; data_source: string; poll_seconds: number | null; mode: string;
    broker_connected: boolean; webhook_secret_set: boolean; telegram_configured: boolean;
  };
}

export interface GridFill { t: string; side: "BUY" | "SELL"; price: number; qty: number; pnl: number; }

/** Server-side 24/7 grid status (GET /grid/status). `active:false` = no grid. */
export interface ServerGridStatus {
  active: boolean;
  running: boolean;
  symbol?: string; timeframe?: string;
  lower?: number; upper?: number; levels?: number; geometric?: boolean;
  investment?: number; leverage?: number; fee_pct?: number; start_price?: number;
  realized?: number; unrealized?: number; net?: number; completed?: number;
  buys?: number; sells?: number; fees_paid?: number;
  inventory_lots?: number; inventory_cost?: number; last_price?: number;
  fills?: GridFill[];
  last_ts?: string | null; started_at?: string | null;
  data_source?: string | null; feed_error?: string | null;
}

export interface LiveState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refetch: () => Promise<boolean>;
}

export interface LabBotStatus {
  session_id?: string | null;
  lab: "PRICE_ACTION" | "SMC";
  account_scope: string;
  scope_label: string;
  paper_only: true;
  real_execution_allowed: false;
  strategy: { id?: string | null; model_id?: string | null; version?: string | null };
  symbol?: string | null;
  timeframe?: string | null;
  mode?: string | null;
  /** SMC only: whether the agent, not a person, approves this session's entries. */
  agent?: { attached?: boolean; is_approver?: boolean } | null;
  saved_configuration: Record<string, unknown>;
  feed: Record<string, any>;
  decision_state: string;
  execution_state: string;
  blockers: string[];
  account?: Record<string, any> | null;
  open_positions: number;
  pending_orders: number;
  positions: Array<Record<string, any>>;
  latest_closed_candle_decision?: Record<string, any> | null;
  latest_signal?: Record<string, any> | null;
  latest_order?: Record<string, any> | null;
  latest_fill?: Record<string, any> | null;
  latest_activity?: Record<string, any> | null;
  last_heartbeat?: string | null;
  performance: {
    backtest: Record<string, any>;
    forward_validation: Record<string, any>;
    live_paper: Record<string, any>;
  };
}

// ---- shared polling layer ----
// Every useLive(path) for the same path shares ONE poller: it dedupes identical
// endpoints (many panels polling /paper/account no longer fan out into N requests),
// skips a tick while a request is still in flight, backs off exponentially on
// errors, and pauses entirely while the tab is hidden. This is what kills the
// per-page request storm that made the dashboard feel laggy.
interface _Sub {
  interval: number;
  onData: (d: unknown) => void;
  onError: (e: string | null) => void;
  onLoading: (b: boolean) => void;
}
interface _Poller {
  path: string;
  subs: Set<_Sub>;
  data: unknown;
  error: string | null;
  hasData: boolean;
  inFlight: Promise<boolean> | null;
  fails: number;
  timer: ReturnType<typeof setTimeout> | null;
}
const _pollers = new Map<string, _Poller>();

function _effInterval(p: _Poller): number {
  let m = Infinity;
  p.subs.forEach((s) => { if (s.interval < m) m = s.interval; });
  return m === Infinity ? 2500 : m;
}

function _schedule(p: _Poller, delay?: number): void {
  if (p.timer) clearTimeout(p.timer);
  if (p.subs.size === 0) { p.timer = null; return; }
  const base = _effInterval(p);
  // exponential backoff on consecutive failures, capped at 60s
  const d = delay ?? (p.fails > 0 ? Math.min(base * 2 ** Math.min(p.fails, 5), 60000) : base);
  p.timer = setTimeout(() => { void _tick(p); }, d);
}

async function _tick(p: _Poller): Promise<boolean> {
  if (p.subs.size === 0) return false;
  if (typeof document !== "undefined" && document.hidden) { _schedule(p); return false; }
  if (p.inFlight) return p.inFlight;               // share the active request
  const request = (async () => {
    try {
      const d = await apiGet<unknown>(p.path);
      p.data = d; p.hasData = true; p.error = null; p.fails = 0;
      p.subs.forEach((s) => { s.onData(d); s.onError(null); s.onLoading(false); });
      return true;
    } catch (e) {
      p.error = e instanceof Error ? e.message : "request failed";
      p.fails += 1;
      p.subs.forEach((s) => { s.onError(p.error); s.onLoading(false); });
      return false;
    } finally {
      p.inFlight = null;
      _schedule(p);
    }
  })();
  p.inFlight = request;
  return request;
}

// resume promptly (and reset backoff) when the tab becomes visible again
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) _pollers.forEach((p) => { p.fails = 0; _schedule(p, 0); });
  });
}

function _subscribe(path: string, sub: _Sub): () => void {
  let p = _pollers.get(path);
  if (!p) {
    p = { path, subs: new Set(), data: null, error: null, hasData: false, inFlight: null, fails: 0, timer: null };
    _pollers.set(path, p);
  }
  const poller = p;
  poller.subs.add(sub);
  if (poller.hasData) { sub.onData(poller.data); sub.onError(poller.error); sub.onLoading(false); }
  // first subscriber (or a cold poller) kicks an immediate fetch; a later
  // subscriber to a warm poller just rides the existing cadence with cached data.
  if (poller.subs.size === 1 || !poller.timer) _schedule(poller, 0);
  return () => {
    poller.subs.delete(sub);
    if (poller.subs.size === 0) {
      if (poller.timer) clearTimeout(poller.timer);
      poller.timer = null;
      _pollers.delete(poller.path);
    }
  };
}

/** Poll a GET endpoint every `intervalMs` and expose data/error/loading.
 *  Identical paths share one deduped, backoff-aware, tab-visibility-aware poller. */
export function useLive<T>(path: string | null, intervalMs = 2500): LiveState<T> {
  const [snapshot, setSnapshot] = useState<{ path: string | null; data: T | null; error: string | null; loading: boolean }>({ path: null, data: null, error: null, loading: true });

  useEffect(() => {
    if (!path) return;
    let active = true;
    const update = (patch: Partial<LiveState<T>>) => {
      if (active) setSnapshot((previous) => ({
        ...(previous.path === path ? previous : { data: null, error: null, loading: true }),
        ...patch, path,
      }));
    };
    const sub: _Sub = {
      interval: intervalMs,
      onData: (d) => update({ data: d as T }),
      onError: (error) => update({ error }),
      onLoading: (loading) => update({ loading }),
    };
    const unsubscribe = _subscribe(path, sub);
    return () => { active = false; unsubscribe(); };
  }, [path, intervalMs]);

  const refetch = useCallback(async () => {
    if (!path) return false;
    const p = _pollers.get(path);
    if (p) {
      if (p.timer) clearTimeout(p.timer);
      p.timer = null;
      return _tick(p);
    }
    return false;
  }, [path]);

  // Never render the previous market's candles under a new request identity.
  const current = snapshot.path === path && path ? snapshot : { data: null, error: null, loading: Boolean(path) };
  return { data: current.data, error: current.error, loading: current.loading, refetch };
}

// ---- response shapes (match the FastAPI endpoints) ----
export interface PaperAccount {
  // separated, persisted concepts
  initial_capital: number;
  current_equity: number;
  available_balance: number;
  realized_pnl: number;
  fees_paid?: number;
  unrealized_pnl: number;
  last_updated: string | null;
  open_positions: number;
  persistent: boolean;
  storage?: "supabase" | "disk" | "ephemeral";
  warning: string | null;
  // legacy keys (kept for back-compat)
  starting_balance: number;
  balance: number;
}
export interface LedgerPosition {
  id: string; symbol: string; side: string; size: number;
  entry: number; stop: number | null; status: string; pnl: number;
  opened_at: string; closed_at: string | null;
  // take-profit the engine is enforcing (memory-only — present when known).
  target?: number | null;
}

/** Adjust the stop-loss and/or take-profit on an open paper position (on-chart
 *  drag). Surfaces the backend's validation message on a 4xx so the terminal can
 *  explain WHY a move was rejected (e.g. stop crossed the target). */
export async function updateStopTarget(
  symbol: string, levels: { stop?: number; target?: number },
): Promise<{ ok: boolean; symbol: string; side: string; entry: number; stop: number | null; target: number | null }> {
  const res = await fetch(`${API_BASE}/paper/stop-target`, {
    method: "POST",
    headers: { "Content-Type": "application/json" }, credentials: "include",
    body: JSON.stringify({ symbol, ...levels }),
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) detail = String(j.detail); } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json();
}
// ── multi-asset symbol universe ──
export type AssetClass = "crypto" | "stock" | "etf" | "index" | "forex" | "commodity";
export interface SymbolRow {
  symbol: string; ticker: string; name: string; asset_class: AssetClass;
  exchange: string; base: string; quote: string; type: string; session: string;
  favorite?: boolean; pinned?: boolean;
}
export interface SymbolInfo extends SymbolRow {
  found: boolean; market_status: "open" | "closed"; price_available: boolean;
  price?: number; change_24h_pct?: number; volume_24h?: number; spark?: number[];
  source?: string; note?: string;
}
export interface AssetClassCount { asset_class: AssetClass; count: number; }
export interface AssetClasses {
  asset_classes: AssetClassCount[]; crypto_source: string; synced_at: string; total: number;
}
export interface MarketPrefs {
  favorites: string[]; pinned: string[];
  watchlists: { id: string; name: string; symbols: string[] }[];
}
// ── AI Trading Intelligence ──
export interface AIRiskAnalysis {
  position_size: number; notional: number; max_loss: number; expected_profit: number;
  risk_pct: number; risk_reward: number; margin_used: number; leverage: number;
  liquidation_price: number | null; portfolio_exposure_pct: number;
  excessive: boolean; warning: string | null;
}
export interface AIAnalysis {
  available?: boolean;
  symbol: string; timeframe: string; ts: string | null; price: number | null;
  decision: "BUY" | "SELL" | "WAIT" | "SKIP"; side: string | null;
  overall_score: number; confidence_level: string; confidence_pct: number;
  engine_score: number | null; allowed: boolean; min_score: number;
  score_breakdown: { category: string; score: number; max: number }[];
  reasons: string[]; failed_checks: string[]; recommendation: string;
  risk_analysis: AIRiskAnalysis | null;
  setup: { entry: number; stop: number; target: number } | null;
  market_analysis: any; checklist: { name: string; status: string; explanation: string }[];
  data_source?: string;
}
export interface AIProfile {
  sample: number; ready: boolean; strengths: string[]; weaknesses: string[];
  avg_hold_seconds: number | null; sharpe_ratio: number | null;
  win_rate: number | null; expectancy_r: number | null; note: string;
}
export interface AIAlert {
  type: string; severity: "critical" | "warning" | "success" | "info";
  title: string; detail: string; symbol: string;
}
export interface AIAlerts { alerts: AIAlert[]; count: number; checked: string[]; }
export interface AIInsight { symbol: string; kind: string; tone: string; text: string; }
export interface AIInsights { insights: AIInsight[]; symbols: string[]; timeframe: string; }
export interface AIRecommendation {
  id: string; title: string; why: string; setting: string;
  current: number | boolean; suggested: number | boolean;
  unit: string; severity: "warning" | "suggestion";
}
export interface AIRecommendations {
  recommendations: AIRecommendation[]; count: number; ready: boolean; note: string;
}
export interface AICoach {
  sample: number; ready: boolean; trades: number; win_rate: number | null;
  expectancy_r: number | null; avg_hold_seconds: number | null;
  main_mistake: string | null; suggestion: string; risk_discipline: string;
  best_session: string | null; worst_setup: string | null; headline: string;
}
export interface TradeMemoryInsights {
  sample?: number;
  overall?: { trades?: number; win_rate?: number; expectancy?: number };
  avg_hold_seconds?: number | null;
  best_session?: { name?: string; expectancy?: number } | null;
  worst_session?: { name?: string; expectancy?: number } | null;
  by_symbol?: { name?: string; expectancy?: number; trades?: number }[];
  by_strategy?: { name?: string; expectancy?: number; trades?: number }[];
  mistakes?: { mistake?: string; count?: number; repeated?: boolean }[];
}
export interface AIConfidenceAccuracy {
  sample: number; ready: boolean; calibrated: boolean; verdict: string;
  high_conf_win_rate: number | null; low_conf_win_rate: number | null; spread_pts: number | null;
  by_confidence: { level: string; trades: number; wins: number; win_rate: number;
    avg_rr: number | null; avg_pnl: number | null }[];
}
export interface PaperTradeRow {
  id: string; alert_id: string | null; symbol: string; side: string; size: number;
  entry: number; stop: number | null; exit: number | null; pnl: number | null;
  rr: number | null; status: string; opened_at: string; closed_at: string | null;
}
export interface LogRow {
  id: string; ts: string; symbol: string; level: string; stage: string; message: string;
}
export interface AlertRow {
  id: string; ts: string; severity: string; category: string; title: string; detail: string; read: number;
}
export interface EngineStatus {
  running: boolean; symbols: string[]; timeframe: string; interval: number;
  current_symbol?: string | null;
  strategy?: string; entry_mode?: string;
  started_at: string | null; bars: number; signals: number; trades: number; rejections: number;
  state?: "created" | "starting" | "bootstrapping" | "warming" | "syncing" | "ready" |
    "running" | "data_stale" | "recovering" | "paused" | "stopped" | "error";
  reason?: string | null; recommended_action?: string;
  lifecycle_state?: string; stop_reason?: string | null; last_error?: string | null;
  last_heartbeat?: string | null; last_activity?: string | null; last_bar_ts?: string | null;
  uptime_s?: number | null; reconnect_attempt?: number; max_reconnect_attempts?: number;
  reconnect_next_at?: string | null; trading_state?: string; auto_halted?: boolean;
  halt_reason?: string | null; connected_exchange?: string; websocket_status?: string;
  market_session?: string; feed_status?: string; feed_error?: string | null;
  data_source?: string | null;
  position_sizing_mode?: "auto" | "fixed"; fixed_position_size?: number;
  symbol_selection_mode?: "auto" | "manual"; auto_symbols?: string[]; manual_symbol?: string;
  last_trade?: { action?: string; symbol?: string; side?: string; price?: number; timestamp?: string } | null;
}
export interface ControlState { state: "Active" | "Paused" | "Stopped"; }
export interface SystemStatus {
  mode: string; broker_connected: boolean; data_source: string;
  engine_running: boolean; engine_mode: string; strategy: string;
  symbols: string[]; timeframe: string; bars_processed: number;
  // Why the bar count is what it is — the same verdict /engine/diagnostics
  // reports, so a zero can be read as "waiting" or "stalled" rather than a
  // number with no context.
  bars_status?: string; bars_note?: string; bars_severity?: string;
  signals: number; trades: number; started_at: string | null;
  uptime_s: number; trading_state: string; auto_halted: boolean; halt_reason: string;
}

export function uptime(secs: number | undefined): string {
  const s = Math.max(0, Math.floor(secs ?? 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}
export interface RiskSummary {
  equity: number; realized_pnl: number; open_positions: number; max_open_positions: number;
  exposure_notional: number; exposure_pct: number; exposure_limit_pct: number;
  risk_per_trade_pct: number; rejections: number; signals: number;
  trading_state: string; engine_running: boolean;
}
export interface PositionSizeResult {
  error?: string; method: string; side: string; entry: number; stop: number; stop_distance: number;
  position_size: number; notional: number; dollar_risk: number; risk_pct_of_equity: number;
  margin_required: number; leverage: number; liquidation_estimate: number;
}
export interface CorrelationData {
  timeframe: string; lookback: number; symbols: string[]; available: string[];
  matrix: Record<string, Record<string, number | null>>;
  pairs: { a: string; b: string; correlation: number }[];
  daily_vol: Record<string, number>;
}
export interface PortfolioRisk {
  equity: number; total_exposure: number; exposure_pct: number;
  long_exposure: number; short_exposure: number; net_exposure: number;
  by_symbol: Record<string, number>; open_risk: number; portfolio_heat_pct: number;
  value_at_risk: number | null; value_at_risk_pct: number | null; var_confidence: number;
  daily_risk_used_pct: number; warnings: string[]; risk_level: string; open_positions?: number;
}
export interface AttrBucket { key: string; trades: number; net_r: number; win_rate: number; avg_r: number; }
export interface TradeExplain { id: number; result: string; rr: number | null; why: string; why_not: string; why_trust: string; }
export interface CoachReview {
  available?: boolean; error?: string; needs_download?: boolean; data_source?: string;
  symbol: string; strategy: string; trades: number; net_r?: number; headline: string;
  why_won: string[]; why_lost: string[]; common_mistakes: { mistake: string; count: number }[];
  weak_conditions: string[]; suggestions: string[]; confidence_score: number; stability_score: number;
  attribution: Record<string, AttrBucket[]>; sample_explanations?: TradeExplain[];
}
export interface CoachLeaderboard {
  timeframe: string;
  grid: { strategy: string; symbol: string; trades: number; win_rate: number; profit_factor: number; net_r: number }[];
  by_strategy: { key: string; net_r: number }[]; by_symbol: { key: string; net_r: number }[];
  best: { strategy: string; symbol: string; net_r: number } | null;
}
export interface WalkForward {
  available: boolean; error?: string; verdict: string; note: string;
  oos_net_r: number; positive_folds: number; total_folds: number; data_source?: string;
  folds: { fold: number; best_min_score: number; train_net_r: number; test_net_r: number; test_trades: number; test_pf: number }[];
}
export interface MonteCarlo {
  available: boolean; error?: string; runs: number; trades: number; prob_profit_pct: number; ruin_r?: number;
  net_r: { p5: number; median: number; p95: number; mean: number };
  max_drawdown_r: { median: number; p95: number; worst: number };
  probability_of_ruin_pct?: number; survival_probability_pct?: number;
  recovery_probability_pct?: number; expected_return_r?: number;
  paths?: {
    steps: number;
    bands: { p5: number[]; p25: number[]; median: number[]; p75: number[]; p95: number[] };
    samples: number[][];
  };
}
export interface ExecStats { trades: number; net_r: number; win_rate: number; profit_factor: number; expectancy_r: number; max_drawdown_r: number; }
export interface ExecRealism { available: boolean; error?: string; trades: number; rejected: number; partial_fills: number; slippage_cost_r: number; edge_survives: boolean; ideal: ExecStats; realistic: ExecStats; }
export interface OutOfSample {
  available: boolean; error?: string; verdict: string; note: string; split: number;
  train: { net_r: number; trades: number; profit_factor: number; win_rate: number };
  test: { net_r: number; trades: number; profit_factor: number; win_rate: number };
}
export interface SlicedPerf {
  strategy: string; timeframe: string; total_trades: number;
  by_regime: AttrBucket[]; by_session: AttrBucket[]; by_symbol: AttrBucket[];
}
export interface WatchRow {
  symbol: string; available: boolean; source?: string;
  last?: number; change_pct?: number; vol_pct?: number; spark?: number[]; bars?: number;
}
export interface Watchlist { timeframe: string; symbols: WatchRow[]; }
export interface CandleFreshness { status: string; blocker?: string; age_seconds?: number | null; allowed_age_seconds?: number; last_close?: string | null; }
// as_of/stale ride on the signal too: opportunities are rendered detached from
// their row, so one that left its age behind arrives on screen looking current.
export interface ScanSignal { symbol?: string; type: string; side: string; strength: number; detail: string; as_of?: string | null; stale?: boolean; }
export interface ScanRow { symbol: string; available: boolean; source?: string; signals: ScanSignal[]; score: number; bias?: string; last?: number; as_of?: string | null; stale?: boolean; freshness?: CandleFreshness; }
export interface ScanResult { timeframe: string; symbols: ScanRow[]; opportunities: ScanSignal[]; count: number; stale_symbols?: string[]; fresh?: boolean; }
export interface HealthCard {
  available?: boolean; error?: string; needs_download?: boolean;
  status: string; classification?: string; unhealthy: boolean; win_rate: number; profit_factor: number; expectancy: number;
  max_drawdown: number; stability_score: number; confidence_score: number; drawdown_score?: number; health_score?: number;
  trades: number; warnings: { metric: string; severity: string; detail: string }[]; reasons?: string[]; symbol?: string; strategy?: string;
}
export interface Recovery {
  drawdown_pct: number; mode: string; risk_multiplier: number; max_trades_factor: number;
  recovery_active: boolean; actions: string[]; equity?: number; peak_equity?: number;
}
export interface AlertChannelStatus { connected: boolean; note: string; }
export interface AlertChannels { telegram: AlertChannelStatus; discord: AlertChannelStatus; email: AlertChannelStatus; }
export interface AlertEvent { type: string; severity: string; title: string; detail: string; }
export interface MarketItem {
  id: string; name: string; kind: string; source: string; favorite: boolean; tags: string[];
  version: string; description: string; symbol?: string; timeframe?: string; timeframes?: string[]; clonable?: boolean;
}
export interface MarketCatalog {
  library: MarketItem[]; templates: MarketItem[]; favorites: MarketItem[]; tags: string[];
  counts: { library: number; favorites: number };
}
export interface ProdCheck { name: string; ok: boolean; level: string; detail: string; }
export interface Readiness { status: string; checks: ProdCheck[]; memory_mb: number | null; uptime_s: number | null; summary: string; data_freshness: { datasets: number; with_data: number; freshest_age_s: number | null }; }
export interface OsService { name: string; topic: string; desc: string; state: string; detail: string; }
export interface BotOsSnap { engines: number; up: number; status: string; services: OsService[]; recent_events: { topic: string; kind: string; ts: string }[]; architecture: string; }
export interface DnaProfile { preferred_market: string; preferred_volatility: string; preferred_trend: string; preferred_session: string; preferred_symbols: string[]; avoid_symbols: string[]; }
export interface MemoryBucket { key: string; trades: number; net_r: number; win_rate: number; }
export interface StrategyMemory { strategy: string; timeframe: string; sample: number; confidence: string; dna: DnaProfile; memory: Record<string, MemoryBucket | null>; by_regime: MemoryBucket[]; by_session: MemoryBucket[]; by_symbol: MemoryBucket[]; }
export interface EconFeedStatus { enabled: boolean; source: string; countries: string[]; interval_s: number; last_attempt: string | null; last_error: string | null; last_success: string | null; events: number; rows_seen?: number | null; }
export interface InstanceEventGuard {
  enabled: boolean; updated_at: string | null; calendar_connected: boolean;
  mode: "normal" | "caution" | "blackout"; halt_new_entries: boolean; risk_multiplier: number;
  next_event: { name: string; time: string; impact?: string } | null; minutes_to_event: number | null;
  window: { blackout_before_min: number; blackout_after_min: number; caution_before_min: number };
  error?: string;
}
export interface PublicTrackRecordEntry {
  id: string; strategy: string; strategy_version: string; symbol: string; timeframe: string;
  execution: "paper"; market_data: string; fill_model: string; entry_mode: string;
  risk_per_trade_pct: number; state: string; session_number: number; session_started_at: string | null;
  published_since: string | null; closed_trades: number; wins: number; losses: number;
  win_rate_pct: number; profit_factor: number | null; return_pct: number; max_drawdown_pct: number;
  longest_losing_streak: number; open_positions: number | null; last_trade_at: string | null;
  sample_note: string | null; equity_index: { t: string | null; index: number }[];
}
export interface InstancePublicRecord {
  published: boolean; since: string | null; updated_at: string | null; eligible: boolean;
  preview: PublicTrackRecordEntry | null; note: string;
}
export interface EconProtection { mode: string; risk_multiplier: number; stop_multiplier: number; halt_new_entries: boolean; minutes_to_event: number | null; next_event: { name: string; time: string } | null; actions: string[]; note: string; connected: boolean; tracked_event_types: { name: string; desc: string }[]; feed?: EconFeedStatus; manual_events?: number; }
export interface JournalEntry { id: string; symbol: string; strategy: string; side: string; result: string; rr: number | null; notes: string; emotions: string; mistakes: string[]; lessons: string[]; tags: string[]; created_at: string; snapshot: { symbol: string; timeframe: string; entry_idx: number | null; exit_idx: number | null }; }
export interface BrokerStatus { name: string; kind: string; connected: boolean; mode: string; live_enabled: boolean; note: string; }
export interface BrokerList { active: string; live_locked: boolean; brokers: BrokerStatus[]; note: string; }
export interface FillModelStatus { model: string; note: string; round_trip_cost_pct?: number; spread_pct?: number; slippage_pct?: number; reject_prob?: number; }
export interface ResearchSummary { id: string; name: string; created_at: string; verdict: string; test_gain_r: number; symbol: string; timeframe: string; }
export interface EquityPoint { t: string | null; equity: number; }
export interface EquityCurveData { starting_balance: number; points: EquityPoint[]; }
export interface EquityCurvePoint { t: string | null; equity: number; }
export interface StrategyPerformance {
  strategy: string; mode: string;
  trades: number; win_rate: number; profit_factor: number; expectancy: number;
  wins: number; losses: number; breakeven: number; gross_win: number; gross_loss: number;
  avg_win: number; avg_loss: number; best: number; worst: number;
  realized_pnl: number; starting_balance: number; balance: number;
  max_drawdown_abs: number; max_drawdown_pct: number; longest_losing_streak: number;
  sharpe_ratio: number; sortino_ratio: number;
  risk_adjusted: { sharpe_ratio: number; sortino_ratio: number; sample: number; basis: string; note: string };
  equity_curve: EquityCurvePoint[];
  recent: PaperTradeRow[];
}
// ── AI Strategy Agent: plain English → the engine's executable spec ──
// The agent is an INTERPRETER: it only emits rule types the engine implements,
// every rule is grounded in the user's own words, and anything ambiguous or
// unsupported is reported rather than assumed.
export interface AgentStatus {
  available: boolean; note: string | null;
  rule_types: string[]; rule_count: number; categories: string[];
}
export interface AgentQuestion { id: string; question: string; options: string[]; }
export interface AgentUnsupported { phrase: string; capability: string; why: string; }
export interface AgentCompleteness {
  score: number; present: string[]; missing: string[]; rule_count: number;
}
export interface AgentCompileResult {
  available: boolean;
  spec: CustomSpec | null;
  compiled?: boolean;
  errors: string[];
  warnings: string[];
  questions: AgentQuestion[];
  unsupported: AgentUnsupported[];
  completeness: AgentCompleteness | null;
  notes?: string[];
  description?: string | null;
  note?: string;
}

/** AI Strategy Review — scorecard + optimisation. Scores are `null` (not a
 *  number) wherever the sample can't support one; `score_basis` explains each
 *  formula so a rating is never presented on faith. */
export interface Scorecard {
  available: boolean; note?: string;
  rating: string | null; strategy_score: number | null;
  scores: {
    profitability: number | null; risk: number | null; consistency: number | null;
    stability: number | null; confidence: number;
  };
  score_basis: Record<string, string>;
  summary: string;
  performance: Record<string, number | null>;
  monthly: { month: string; trades: number; net_r: number; avg_r: number; win_rate: number }[];
  day_of_week: { day: string; trades: number; net_r: number; avg_r: number; win_rate: number }[];
  recovery: { max_drawdown_abs: number; trough_index: number;
              trades_to_recover: number | null; recovered: boolean } | null;
  equity_curve: { t: string | null; equity: number }[];
}
export interface OptimisationSuggestion {
  id: string; title: string; reason: string;
  evidence: { stat: string; value: unknown }[];
  expected_benefit: string; tradeoffs: string;
  confidence: "high" | "medium" | "low";
  patch: Record<string, unknown> | null;
  /** The REAL trades this suggestion would have changed — count, their net R,
   *  and a capped sample of the actual rows (never a description of them). */
  affected?: {
    count: number; net_r: number;
    sample: { side: string; entry: number; exit: number; r: number;
              result: string; exit_reason: string | null; entry_time: string;
              bars_held: number | null; regime: string | null }[];
  };
}
/** Parameter tuning — each suggestion is a variant that was actually RUN and
 *  measurably beat the current settings. `measured` carries both backtests. */
export interface TuneSuggestion extends OptimisationSuggestion {
  measured: {
    before: { trades: number; net_r: number; profit_factor: number; win_rate: number;
              max_drawdown_r: number; expectancy_r: number };
    after: { trades: number; net_r: number; profit_factor: number; win_rate: number;
             max_drawdown_r: number; expectancy_r: number };
  };
}
export interface TuneResult {
  available: boolean; note?: string | null; tested: number; range?: string;
  baseline?: { trades: number; net_r: number; profit_factor: number };
  suggestions: TuneSuggestion[];
}

/** Where a strategy stands across all nine lifecycle stages. Every state is
 *  derived from evidence that exists; "unknown" means the stage leaves no
 *  record (replay), not that it was skipped. */
export interface LifecycleStage {
  key: string; n: number; title: string; page: string; purpose: string;
  state: "done" | "ready" | "blocked" | "unknown";
  detail: string;
  evidence: { stat: string; value: unknown }[];
  action?: string | null;
  blocked_by?: string | null;
  caveat?: string;
}
export interface Lifecycle {
  available: boolean;
  strategy?: { id: string; name: string; symbol?: string; timeframe?: string };
  stages: LifecycleStage[];
  completed: number; total: number; unknown: number;
  next?: LifecycleStage | null;
  note?: string | null;
}

/** Versioning & collaboration. Changelog entries are DERIVED server-side by
 *  diffing stored version snapshots — nobody authors them, so the log cannot
 *  drift from the strategy it describes. */
export interface ChangelogEntry {
  v: number | "current"; at?: string; name?: string;
  changes: string[];
  detail?: { changed: boolean; rules: unknown[]; fields: unknown[]; summary: string[] } | null;
}
export interface Changelog {
  available: boolean; entries: ChangelogEntry[]; versions?: number;
  note?: string; id?: string;
}
export interface StrategyComment {
  id: string; author: string; body: string;
  version?: number | string | null; reply_to?: string | null;
  at: string; edited_at?: string | null; deleted?: boolean;
}
export interface StrategyShares {
  strategy_id: string; owner?: string | null;
  grants: Record<string, { role: string; at: string; by: string }>;
  me?: string; my_role?: string | null; roles?: string[];
}

/** A published marketplace listing. `verified` metrics are produced by a
 *  backtest the SERVER ran — the publish endpoint accepts no numbers — and
 *  `history` is append-only, so a strategy that got worse shows that it did. */
export interface ListingSnapshot {
  version: string; ran_at: string; range: string;
  symbol?: string; timeframe?: string;
  metrics: Record<string, number>;
}
export interface Listing {
  id: string; publisher: string; name: string; description: string; version: string;
  symbol?: string; timeframe?: string; side?: string; tags: string[];
  verified: ListingSnapshot;
  risk: {
    risk_per_trade_pct?: number; stop?: string; target?: string;
    max_drawdown_r?: number; max_consecutive_losses?: number; losses?: number;
  };
  history: ListingSnapshot[];
  publish_count: number;
  rating: { count: number; average: number | null; note?: string; distribution?: Record<string, number> };
  reviews: { user: string; stars: number; comment?: string; at?: string }[];
  imports: number; published_at: string; updated_at: string;
  spec?: CustomSpec;
}
export interface ListingsResponse {
  listings: Listing[]; count: number; sort: string;
  me?: string | null; following: string[];
}

/** AI Monitoring Agent — live behaviour measured against this strategy's own
 *  backtest. Advisory only: `auto_modify` is always false and every finding
 *  carries a recommendation a human performs. */
export interface MonitorFinding {
  key: string;
  severity: "critical" | "warning" | "info";
  title: string;
  detail: string;
  evidence: { stat: string; value: string }[];
  recommendation: string;
}
export interface MonitorMetrics {
  total_trades: number; win_rate: number; profit_factor: number; net_r: number;
  expectancy_r: number; max_drawdown_r: number; span_days?: number;
}
export interface MonitorResult {
  available: boolean;
  status: "no_baseline" | "warming_up" | "in_line" | "info" | "warning" | "critical";
  auto_modify: false;
  findings: MonitorFinding[];
  baseline?: MonitorMetrics | null;
  live?: MonitorMetrics | null;
  note?: string | null;
  range?: string; symbol?: string; timeframe?: string;
  data_source?: string | null;
  volatility?: {
    band?: { p10: number; median: number; p90: number; samples: number } | null;
    current_atr_pct?: number | null;
    note?: string;
  };
}

/** The continuous monitor's last evaluation, served without recomputation.
 *  `last_check` is the reading's own timestamp — a stale one is visibly stale. */
export interface MonitorStatus {
  running: boolean; interval_s: number; cooldown_s: number;
  last_check: string | null;
  result: (MonitorResult & {
    strategy?: string; baseline_cached?: boolean;
    baseline_computed_at?: string | null; checked_at?: string;
    /** Set when the feed did not honour the strategy's timeframe. Live and
     *  backtest both read the same feed, so the comparison still holds — what is
     *  wrong is the label on it. */
    timeframe_warning?: string;
  }) | null;
}

export interface StrategyFullReview {
  available: boolean; note?: string; bars_requested?: number;
  scorecard?: Scorecard;
  evidence?: EvidenceReview;
  suggestions?: OptimisationSuggestion[];
}
export interface SuggestionApplyResult {
  available: boolean; note?: string;
  spec?: CustomSpec;
  comparison?: { rows: { metric: string; before: number | null; after: number | null; delta: number | null }[] };
  before?: { scorecard: Scorecard }; after?: { scorecard: Scorecard };
}

/** Evidence-based review — every finding carries the statistic behind it, and
 *  dimensions the trade data cannot support are declared in `not_derivable`. */
export interface EvidenceFinding { claim: string; stat: string; value: unknown; }
export interface EvidenceSession {
  session: string; trades: number; net_r: number; avg_r: number; win_rate: number;
}
export interface EvidenceExit {
  exit_reason: string; trades: number; net_r: number; avg_r: number; win_rate: number;
}
export interface EvidenceReview {
  available: boolean; note?: string; trades_reviewed?: number;
  strengths: EvidenceFinding[]; weaknesses: EvidenceFinding[];
  sessions: EvidenceSession[]; best_session?: EvidenceSession | null;
  exit_reasons: EvidenceExit[]; most_common_loss?: EvidenceExit | null;
  direction?: { better_side: string; long_net_r: number; short_net_r: number;
                long_trades: number; short_trades: number } | null;
  hold_times?: { avg_bars_winners: number; avg_bars_losers: number } | null;
  risk?: Record<string, unknown> | null;
  not_derivable: { question: string; why: string }[];
  bars_requested?: number;
  headline?: Record<string, number | null>;
}

export interface CustomRule { type: string; negate?: boolean; source?: string; [k: string]: unknown; }
// ── no-code strategy builder ──
export interface BlockParam { name: string; type: "number" | "select"; default: unknown; label: string; options?: string[]; }
export interface BlockDef { type: string; label: string; desc: string; params: BlockParam[]; }
export interface BlockCategory { key: string; label: string; blocks: BlockDef[]; }
export interface SessionDef { key: string; label: string; start: number; end: number; }
export interface BlockCatalog {
  categories: BlockCategory[];
  config: { logic: string[]; risk: BlockParam[]; stop: BlockParam[]; target: BlockParam[]; exit: BlockParam[]; sessions: SessionDef[]; };
}
export interface AIStrategyReview {
  complexity: string; rule_count: number; risk_level: string; expected_behaviour: string;
  strengths: string[]; weaknesses: string[]; improvements: string[];
  estimated_confidence: number; confidence_level: string; summary: string;
  warnings: { level: string; message: string }[];
}
export interface CustomSpec {
  id?: string; name: string; market?: string; symbol: string; timeframe: string;
  side: "long" | "short";
  entry: { op: "AND" | "OR"; rules: CustomRule[] };
  stop: { type: "atr" | "pct"; mult?: number; period?: number; pct?: number };
  target: { type: "rr" | "pct"; rr?: number; pct?: number };
  risk_per_trade_pct: number; max_trades_per_day?: number;
  session?: { start: number; end: number } | null;
  exit?: { op?: "AND" | "OR"; rules?: CustomRule[]; ai_exit?: boolean;
           breakeven_at_r?: number; trail_atr?: number; time_stop_bars?: number };
  created_at?: string; updated_at?: string;
  favorite?: boolean; folder?: string; tags?: string[];
  versions?: StrategyVersion[];
}
export interface StrategyVersion {
  v: number; at: string; name?: string; spec: Record<string, unknown>;
}
export interface SimTrade {
  side: string; entry: number; exit: number; stop: number; target: number;
  r: number; result: string; reason: string; entry_time: string; exit_time: string;
  // brain tags (present when the quality filter ran)
  score?: number; grade?: string; regime?: string; htf_bias?: string; setup_type?: string;
  exit_reason?: string; bars_held?: number; passed?: string[]; failed?: string[];
}
export interface BlockedTrade {
  time: string; side: string; score: number; regime: string; htf_bias: string;
  reason: string; blocks: string[];
}
export interface SimDiagnosis {
  summary: string; headline_problem: string;
  avg_quality_score?: number | null; avg_losing_setup_score?: number | null;
  loss_reasons: Record<string, number>; blocked_reasons: Record<string, number>;
  blocked_count?: number;
  worst_regime?: { name: string; trades: number; net_r: number; win_rate: number } | null;
  worst_session?: { name: string; trades: number; net_r: number; win_rate: number } | null;
  exit_pattern?: Record<string, number>; stop_hit_losses?: number;
  avg_win_to_loss?: number | null; overtrading: boolean; trades_per_day?: number;
  choppy_markets: boolean; recommendations: string[];
}
export interface SimResult {
  results: {
    simulation: boolean; total_trades: number; win_rate: number; wins: number; losses: number;
    profit_factor: number; net_r: number; net_pct: number; max_drawdown_r: number; max_drawdown_pct: number;
    avg_rr: number; avg_win_r: number; avg_loss_r: number; best_r: number; worst_r: number;
    max_consecutive_wins: number; max_consecutive_losses: number; end_balance: number; span_days: number;
    equity_curve: { t: string | null; equity: number }[]; trades: SimTrade[];
    // new metrics + brain output
    expectancy_r?: number; sharpe?: number; recovery_factor?: number; avg_hold_bars?: number;
    long_net_r?: number; short_net_r?: number; long_trades?: number; short_trades?: number;
    blocked_count?: number; blocked?: BlockedTrade[]; diagnosis?: SimDiagnosis;
  };
  warnings: { level: string; message: string }[];
  sizing?: {
    model: string; equity: number; risk_pct: number; entry: number; stop_distance: number;
    risk_dollars: number; position_size: number; notional: number; leverage_x: number;
  };
  brain?: { quality_filter: boolean; min_score: number; blocked_count: number };
  description: string; data_source: string; symbol: string; timeframe: string; label: string;
}

export interface CompareResult {
  strategy: string; data_source: string; symbol: string; timeframe: string;
  metrics: { total_trades: number; win_rate: number; profit_factor: number; net_r: number; max_drawdown_r: number; avg_r: number };
}

export interface NotifStatus {
  telegram_configured: boolean; notify_trades: boolean; notify_risk: boolean;
  email: string; discord: string;
}

export interface StrategyHealthData {
  strategy: string;
  health: {
    status: "Healthy" | "Degrading" | "Unhealthy";
    recent: { n: number; win_rate: number; profit_factor: number; expectancy: number; avg_rr: number; max_drawdown: number; consecutive_losses: number };
    previous: { n: number; win_rate: number; profit_factor: number; expectancy: number; avg_rr: number; max_drawdown: number; consecutive_losses: number };
    warnings: { metric: string; severity: string; detail: string }[];
  };
  brain: { blocked: number; taken: number; total: number; block_rate: number; top_reasons: Record<string, number> };
  breakdown: {
    by_symbol: { name: string; trades: number; win_rate: number; net_pnl: number; blocked: number }[];
    by_session: { name: string; trades: number; win_rate: number; net_pnl: number }[];
  };
}

export interface EngineDiagnostics {
  status: string; headline: string; detail: string; severity: "info" | "warning" | "critical";
  running: boolean; mode: string; timeframe: string; data_source: string | null;
  bars: number; signals: number; trades: number; rejections: number;
  last_bar_ts: string | null; last_activity_age_s: number | null;
}

export interface ReplayCandle { t: string; o: number; h: number; l: number; c: number; v: number; }
export interface ReplayMarker { idx: number; price: number; type: string; side: "bull" | "bear"; }

/** What the ACTIVE strategy actually uses — declared by the strategy engine so
 *  the chart shows only the bot's real inputs (item #1: strategy-based
 *  annotations). Every listed overlay key exists in ReplayData.overlays. */
export interface ReplayViz {
  title: string;
  explain: string;
  used: { label: string; detail: string }[];
  overlays: string[];
  osc: "none" | "rsi" | "macd" | "atr";
  structure: boolean;
  zones: boolean;
  crossovers: boolean;
  supertrend: boolean;
  volume: boolean;
}
export interface ReplayFrame {
  regime: string; market_regime?: string; trends: Record<string, string>; trigger: string;
  score: number; breakdown: Record<string, number> | null; blocked: boolean;
  reason: string; vol_ratio: number;
}
export interface ReplayEvent { idx: number; kind: string; text: string; }
export interface ReplayTrade {
  id: number; symbol: string; side: "long" | "short"; entry_idx: number; entry: number;
  sl: number; tp: number; tp1: number | null; tp1_idx: number | null;
  partial?: boolean; status?: string;
  score: number; breakdown: Record<string, number>;
  entry_reasons: string[]; exit_idx: number | null; exit: number | null;
  exit_reason: string | null; result: string; rr: number | null; loss_analysis: string | null;
  mtf?: { aligned: boolean; reason: string }; regime?: string; bars_held?: number | null;
}
export interface ReplayStats {
  symbol: string; trades: number; win_rate: number; profit_factor: number; net_r: number;
  max_drawdown_r: number; avg_rr: number; expectancy_r: number; best_r: number; worst_r: number;
  long_trades: number; short_trades: number; long_net_r: number; short_net_r: number;
  max_consecutive_wins: number; max_consecutive_losses: number; current_streak: number;
}
/** Every series is candle-aligned; null marks the indicator's warm-up window. */
export interface ReplayOverlays {
  ema8: (number | null)[]; ema20: (number | null)[]; ema30: (number | null)[]; ema50: (number | null)[];
  sma20: (number | null)[]; sma50: (number | null)[]; vwap: (number | null)[];
  bb_upper: (number | null)[]; bb_mid: (number | null)[]; bb_lower: (number | null)[];
  rsi: (number | null)[]; atr: (number | null)[];
  macd: (number | null)[]; macd_signal: (number | null)[]; macd_hist: (number | null)[];
}
export interface ReplayData {
  meta: { symbol: string; timeframe: string; data_source: string; bars: number;
          start: string | null; end: string | null; htf_available: Record<string, boolean>;
          strategy?: string; data_source_label?: string; data_is_real?: boolean;
          data_warning?: string | null; needs_download?: boolean; note?: string;
          /** Replay supports only 1m/3m/5m/15m execution. A coarser strategy is
           *  run at 15m — a DIFFERENT strategy from the one backtested — so the
           *  swap is reported rather than applied silently. */
          requested_timeframe?: string; timeframe_substituted?: boolean;
          timeframe_note?: string | null;
          viz?: ReplayViz;
          debug?: { strategy_id: string; strategy_class: string; candles_loaded: number;
                    warmup_bars: number; trades_generated: number; data_source: string;
                    mtf_timeframes: string[]; gate_timeframes?: string[]; indicators?: string[]; memory_filter?: string[];
                    computed_at: string; error: string | null } };
  candles: ReplayCandle[];
  overlays: ReplayOverlays | Record<string, (number | null)[]>;
  markers: ReplayMarker[];
  zones: { type: string; price?: number; left_idx?: number; top?: number; bottom?: number }[];
  frames: ReplayFrame[];
  events: ReplayEvent[];
  trades: ReplayTrade[];
  stats: ReplayStats;
}

export interface SentimentData {
  available: boolean; mood: string | null; risk_mode: string; fear_greed: number | null;
  fear_greed_label?: string; btc_dominance?: number | null; total_mcap_usd?: number | null;
  confidence?: { long: number; short: number; note: string };
  social?: Record<string, string>; note: string;
}
export interface Lesson {
  id: string; symbol: string; strategy: string; lesson: string; suggested_fix: string;
  confidence: number; evidence: string; status: string; tested: boolean; created_at: string;
}
export interface Upgrade {
  id: string; strategy: string; symbol: string; title: string; reason: string; evidence: string;
  expected_benefit: string; risk: string; backtest_required: boolean; confidence: number;
  status: string; created_at: string; apply?: Record<string, number | boolean> | null;
  auto_applicable?: boolean;
}
export interface StrategyVersion {
  id: string; strategy: string; version: number; label: string; note: string;
  params: any; stats: Record<string, number>;
  gates: { backtest: boolean; simulation: boolean; paper: boolean; live_unlocked: boolean };
  created_at: string;
}
export interface VersionCompare { strategy: string; versions: StrategyVersion[]; best: string | null; }
export interface Experiment {
  symbol: string; timeframe: string; data_source: string; verdict: string; note: string;
  train_gain_r: number; test_gain_r: number; warnings: string[];
  a: { label: string; train: any; test: any };
  b: { label: string; train: any; test: any };
}
export interface MarketContext {
  fear_greed: { available: boolean; value: number | null; label: string | null; mood: string | null };
  btc_dominance: { available: boolean; value: number | null };
  total_mcap_usd: { available: boolean; value: number | null };
  eth_btc: { available: boolean; trend: string | null; change_30d_pct?: number; ratio?: number; note?: string };
  funding_rate: { available: boolean; value: number | null; symbol: string; note?: string };
  open_interest: { available: boolean; value: number | null; symbol: string; note?: string };
  news: { available: boolean; connected: boolean; headlines: { title: string; url: string; published: string }[]; note?: string };
  liquidations: { available: boolean; connected: boolean; note: string };
  economic_calendar: { available: boolean; connected: boolean; note: string };
  sentiment_summary: string;
  providers: ProviderStatus[];
  provider_debug?: ProviderDebug[];
  last_updated?: string;
}
export interface ProviderStatus { id: string; label: string; needs_key: boolean; connected: boolean; }
export interface ProviderDebug {
  id: string; label: string; connected: boolean; status: string;
  last_update: string | null; freshness_s: number | null; error: string | null;
}

export interface EvoDashboard {
  sentiment: { available: boolean; mood: string | null; risk_mode: string; fear_greed: number | null };
  lessons_weekly: number; lessons_total: number;
  lesson_status: Record<string, number>; upgrade_status: Record<string, number>;
  workflow: string[]; live_rule: string;
}

export interface ControlOptions {
  strategies: string[]; symbols: string[]; timeframes: string[]; modes: string[];
  default_tuning: ControlTuning;
}
export interface ControlTuning {
  min_score: number; rr: number; trend_filter: boolean; volume_filter: boolean;
  regime_filter: boolean; session_filter: boolean; max_trades_per_day: number; cooldown_after_loss: number;
  max_consecutive_losses: number;
}
export interface ControlSimResult {
  strategy: string; symbol: string; timeframe: string; data_source: string;
  available: boolean; error?: string; mtf_gate?: string[];
  warning: { level: string; message: string } | null;
  results?: SimResult["results"];
}
export interface ControlCompare {
  a: ControlSimResult; b: ControlSimResult; winner: "A" | "B"; summary: string; error?: string;
}

export interface ControlAutoTune {
  available: boolean; error?: string; strategy: string; symbol: string; timeframe: string;
  best_tuning: ControlTuning; verdict: string; note: string;
  train: any; validation: any; baseline_test: any; baseline_train: any;
  trials: { min_score: number; rr: number; trades: number; net_r: number; profit_factor: number }[];
}

export interface StrategyInfo { key: string; label: string; desc: string; }
export interface StrategyList { active: string; timeframe: string; strategies: StrategyInfo[]; }
export interface LiveBot {
  id: string; symbol: string; name: string; strategy: string; timeframe: string;
  status: "Running" | "Paused" | "Stopped"; open: boolean; side: string | null;
  size: number; entry: number; num_trades: number; win_rate: number; realized_pnl: number;
}

/** Short "HH:MM:SS" from an ISO timestamp. */
export function hhmmss(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = iso.includes("T") ? iso.split("T")[1] : iso;
  return t.slice(0, 8);
}
