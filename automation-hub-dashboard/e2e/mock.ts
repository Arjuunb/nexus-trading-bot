import type { Page, Route } from "@playwright/test";
import { readFileSync } from "node:fs";

/** Deterministic mock backend for the E2E audit. Intercepts every request to
 *  the API host (:8000) and returns plausible JSON so pages render without a
 *  live backend. Endpoints with nested access get exact shapes; everything
 *  else defaults to {} or [] so the SPA's "loading/empty" states show. */

const SETTINGS = {
  editable: {
    risk_per_trade_pct: 0.01, exposure_limit_pct: 0.05, max_drawdown_pct: 0.2,
    max_open_positions: 3, dedup_window_s: 300, max_daily_loss_pct: 0,
    session_start: 0, session_end: 24, max_weekly_loss_pct: 0,
    max_trades_per_day: 0, max_consecutive_losses: 0, cooldown_after_loss_min: 0,
    trading_days_mask: 127, entry_mode: "limit", daily_report_hour: 8,
  },
  readonly: {
    strategy: "Decision Brain", strategy_key: "brain", timeframe: "4h",
    symbols: ["BTCUSDT", "ETHUSDT"], starting_cash: 10000, data_source: "synthetic",
    poll_seconds: null, mode: "paper", broker_connected: false,
    webhook_secret_set: true, telegram_configured: false, min_quality_score: 60,
  },
};

const SYSTEM = {
  engine_running: false, strategy: "Decision Brain", timeframe: "4h",
  trading_state: "active", mode: "paper", auto_halted: false, halt_reason: "",
};

const RISK = {
  equity: 10000, realized_pnl: 0, open_positions: 0, exposure_pct: 0,
  trading_state: "active", rejections: 0, exposure_limit_pct: 0.05,
};

const PERF = {
  strategy: "Decision Brain", mode: "replay",
  win_rate: 58, profit_factor: 1.7, trades: 24, wins: 14, losses: 10, breakeven: 0,
  net_r: 12, equity_curve: [{ t: null, equity: 10000 }, { t: "2026-07-05T09:00:00Z", equity: 10300 }],
  max_drawdown_pct: 8.2, realized_pnl: 300, expectancy: 12.5, best: 150, worst: -80,
  avg_win: 60, avg_loss: -35, gross_win: 840, gross_loss: 540, longest_losing_streak: 3,
  sharpe_ratio: 0.42, sortino_ratio: 1.06,
  risk_adjusted: { sharpe_ratio: 0.42, sortino_ratio: 1.06, sample: 24, basis: "per-trade R", note: "per-trade ratios (not annualised)" },
  starting_balance: 10000, balance: 10300, recent: [],
};
const STRAT_HEALTH = {
  strategy: "Decision Brain",
  // scorecard fields (used by the Risk Manager health card)
  classification: "Healthy", health_score: 72, drawdown_score: 80, reasons: [],
  health: { status: "Healthy", recent: { n: 24, win_rate: 58, profit_factor: 1.7, expectancy: 12.5, avg_rr: 1.4, max_drawdown: 8.2, consecutive_losses: 3 },
    previous: { n: 20, win_rate: 55, profit_factor: 1.5, expectancy: 10, avg_rr: 1.3, max_drawdown: 9, consecutive_losses: 2 }, warnings: [] },
  brain: { blocked: 12, taken: 24, total: 36, block_rate: 33.3, top_reasons: {} },
  breakdown: {
    by_symbol: [{ name: "BTCUSDT", trades: 14, win_rate: 60, net_pnl: 200, blocked: 5 },
                { name: "ETHUSDT", trades: 10, win_rate: 55, net_pnl: 100, blocked: 7 }],
    by_session: [{ name: "London", trades: 12, win_rate: 62, net_pnl: 180 },
                 { name: "New York", trades: 12, win_rate: 54, net_pnl: 120 }],
  },
};
const WALK_FORWARD = {
  available: true, oos_net_r: 6.4, positive_folds: 3, total_folds: 4,
  folds: [{ train_net_r: 8, test_net_r: 2 }, { train_net_r: 6, test_net_r: 1.5 },
          { train_net_r: 7, test_net_r: 3 }, { train_net_r: 5, test_net_r: -0.1 }],
};
const PA_CANDLES = Array.from({ length: 80 }, (_, index) => ({
  timestamp: new Date(Date.UTC(2026, 0, 1, 0, index * 5)).toISOString(),
  open: 100 + index * .1, high: 101 + index * .1, low: 99 + index * .1,
  close: 100.5 + index * .1, volume: 1000,
}));
export const PA_CHART = {
  research_id: "PRICE_ACTION_NATIVE_V1_RESEARCH", research_only: true,
  execution_allowed: false, paper_execution_allowed: true, symbol: "BTCUSDT", timeframe: "5m",
  candles: PA_CANDLES, swings: [], zones: [], events: [], setups: [], proposals: [], orders: [], trades: [],
  metrics: { closed: 0, wins: 0, losses: 0, unfilled: 0, cancelled: 0, rejected: 0, gross_r: 0, net_r: 0, costs_r: 0,
    by_strategy: { PA1_SR_REJECTION: { closed: 0, wins: 0, losses: 0, unfilled: 0, gross_r: 0, net_r: 0, costs_r: 0 } } },
  metrics_scope: { dataset_start: PA_CANDLES[0].timestamp, dataset_end: PA_CANDLES.at(-1)?.timestamp,
    configuration_id: "mock-price-action-config", cost_model: { funding_coverage: "NOT_APPLIED_TO_VISUAL_ENGINE_METRICS" } },
  snapshot: { candle_open: PA_CANDLES.at(-1)?.timestamp, candle_close: PA_CANDLES.at(-1)?.timestamp,
    structure_bias: "neutral", pattern: null, proposal_ids: [], strategy_traces: [] },
  selected_snapshot: null, forming_candle: { ...PA_CANDLES.at(-1), close: 109 },
  live_display: { is_forming: true, observed_at: "2026-01-01T07:00:00Z", last_update: "2026-01-01T07:00:00Z",
    refresh_interval_seconds: 0, candle_closes_at: "2026-01-01T07:05:00Z", last_price: 109,
    bid: 108.9, ask: 109.1, mark: 109, funding_rate: .0001, next_funding_time: "2026-01-01T08:00:00Z",
    connection_state: "SYNCHRONIZED", transport_state: "CONNECTED", reliable: true,
    health_reason: "candles, bid/ask and mark are reconciled and fresh",
    quote_source: "PUBLIC_WEBSOCKET", candle_age_seconds: .2, quote_age_seconds: .1,
    mark_age_seconds: .3, closed_candle_age_seconds: 120, candle_quote_deviation_bps: 2,
    new_entries_paused: false, execution_uses_closed_bars_only: true },
  data_provenance: { exchange: "Binance USDⓈ-M Futures", closed_candles_used: 80 },
};
export const PA_PAPER = {
  account_scope: "PRICE_ACTION_VISUAL_LAB_ONLY", currency: "USDT", execution_mode: "PAPER",
  real_funds: false, live_execution_allowed: false,
  session: { id: "pa-session-1", started_at: "2026-01-01T00:00:00Z", status: "active", mode: "LIVE_PAPER",
    starting_balance: 10000, symbol: "BTCUSDT", timeframe: "5m", operating_mode: "signals_only",
    execution_config: { strategy_id: "PA1_SR_REJECTION", risk_pct: .5 } },
  account: { starting_balance: 10000, balance: 10000, equity: 10000, unrealized_pnl: 0,
    fees_paid: 0, free_margin: 10000, leverage: 1 }, positions: [], orders: [], trades: [],
  candidates: [], order_metadata: [], activity: [],
  order_audit: { session_id: "pa-session-1", pending_paper_orders: 0, pending_strategy_orders: 0,
    pending_manual_orders: 0, duplicate_strategy_orders: [], discrepancies: [],
    manual_orders_are_never_auto_cancelled: true },
};

const SMC_SNAPSHOT = {
  id: "smc-snapshot-1", candle_open: PA_CANDLES.at(-1)?.timestamp,
  candle_close: PA_CANDLES.at(-1)?.timestamp, htf_bias: 1, htf_ema: 104,
  swing_bias: 1, internal_bias: 1, session: "london",
  dealing_range: { high: 110, low: 98, equilibrium: 104, area: "discount" },
  price_action: { bullish_rejection: true, bearish_rejection: false, body: 1, upper_wick: .2, lower_wick: .8 },
  active_setup_id: null, setup_phase: null, next_required_event: "Await liquidity sweep",
  latest_sweep_id: null, event_ids: [], active_fvg_ids: [], active_ob_ids: [], proposal_ids: [],
};
const SMC_SOURCE = {
  strategy_id: "SMC_SOURCE_V1", version: "SMC_SOURCE_V1.0.0-paper-draft",
  state: "WATCHING", next_required_event: "Await liquidity sweep", selected_candidate_id: null,
  paper_only: true, execution_allowed: false, native_object_ids: [], missing_conditions: ["liquidity_sweep"],
  model: { id: "SMC_M1_SWEEP_REVERSAL", label: "M1 Sweep Reversal", status: "ACTIVE",
    narrative: "Sweep reversal", ordered_rules: ["HTF bias", "location", "sweep"] },
  ordered_condition_results: [], trade_plan: null,
};
export const SMC_CHART = {
  research_id: "SMC_NATIVE_V1_RESEARCH", execution_allowed: false, candles: PA_CANDLES,
  pivots: [], events: [], fair_value_gaps: [], order_blocks: [], proposals: [], setups: [],
  snapshot: SMC_SNAPSHOT, selected_snapshot: SMC_SNAPSHOT, snapshot_ledger: [SMC_SNAPSHOT],
  forming_candle: { ...PA_CANDLES.at(-1), close: 109 }, source_strategy: SMC_SOURCE,
  strategy_ladder: { candidates: [] },
  live_display: { is_forming: true, observed_at: "2026-01-01T07:00:00Z", refresh_interval_seconds: 0,
    candle_closes_at: "2026-01-01T07:05:00Z", last_price: 109, bid: 108.9, ask: 109.1, mark: 109,
    connection_state: "SYNCHRONIZED", reliable: true, new_entries_paused: false,
    health_reason: "candles, bid/ask and mark are reconciled and fresh", quote_age_seconds: .1,
    candle_quote_deviation_bps: 2, execution_uses_closed_bars_only: true },
  data_provenance: { venue: "Binance USDⓈ-M Futures", market: "BTC/USDT:USDT",
    last_closed_candle: PA_CANDLES.at(-1)?.timestamp, observed_at: "2026-01-01T07:00:00Z",
    closed_candles_loaded: 80, closed_candles_visible: 80, forming_candle_excluded: true,
    execution_allowed: false },
};
export const SMC_PAPER = {
  paper_only: true, real_execution_allowed: false,
  session: { id: "smc-session-1", mode: "LIVE_PAPER", symbol: "BTCUSDT", timeframe: "5m",
    operating_mode: "signals_only", model_id: "SMC_M1_SWEEP_REVERSAL", risk_pct: .5 },
  account: { balance: 10000, equity: 10000, available_margin: 10000, used_margin: 0,
    open_risk: 0, unrealized_pnl: 0, leverage: 1 },
  positions: [], orders: [], trades: [], candidates: [], activity: [], funding_events: [],
};

const JOURNAL_FULL = {
  trade_id: "t1234567abcdef", mode: "paper", symbol: "BTCUSDT", side: "long",
  strategy: "Decision Brain", timeframe: "4h", entry: 100, stop: 95, target: 115,
  exit: 115, size: 2, risk_amount: 10, planned_rr: 3, actual_rr: 3, pnl: 30,
  result: "win", confidence: 0.8, brain_score: 0.5, regime: "Trending",
  grade: "A", status: "closed",
  events: [
    { ts: "2026-07-05T08:00:00Z", kind: "setup-detected", detail: "aligned long" },
    { ts: "2026-07-05T08:00:01Z", kind: "risk-check-passed", detail: "1% risk" },
    { ts: "2026-07-05T08:00:02Z", kind: "trade-opened", detail: "long 2 @ 100" },
    { ts: "2026-07-05T09:00:00Z", kind: "exit-triggered", detail: "take-profit" },
    { ts: "2026-07-05T09:00:01Z", kind: "trade-closed", detail: "+30" },
  ],
  sections: {
    entry_decision: { main_reason: "Aligned 4-vote long", strategy_setup: "Trend pullback",
      higher_timeframe_trend: "up", confidence_score: 0.8, final_decision_score: 0.5 },
    checklist: {
      entry_reads: [
        { name: "EMA trend (fast vs slow)", status: "Passed", detail: "EMA12>EMA26" },
        { name: "Fair-value gap (FVG)", status: "Not checked" },
      ],
      risk_gates: [
        { rule: "daily_loss", name: "Daily loss limit", status: "Passed", detail: "today +0" },
        { rule: "exposure", name: "Exposure cap", status: "Passed", detail: "within 5%" },
      ],
    },
    market_snapshot: { price: 100, rsi: 58, atr: 1.2, regime: "Trending", trend_direction: "up" },
    risk_check: { risk_per_trade: "1%", final_risk_decision: "approved" },
    exit_decision: { exit_reason: "take-profit", exit_price: 115, actual_rr: 3, pnl: 30, result: "win" },
    review: { grade: "A", quality: "good", entry_valid: true, risk_valid: true, exit_valid: true,
      followed_strategy: true, mistake: "", improvement: "Repeatable — keep taking this setup." },
    evolution: { learned: "Aligned trend longs pay in Trending regime", strength: "early signal (3 trades)",
      take_similar_again: true, confidence_direction: "hold", rule_weight_hint: "no change yet",
      guardrails: ["Risk is never increased automatically.",
        "The Risk Manager and Safety Center are never bypassed.",
        "Insights under 30 trades are early signals; 50+ trades are needed for stronger changes."] },
  },
};
const JOURNAL_TRADES = { trades: [{
  trade_id: "t1234567abcdef", created_at: "2026-07-05T08:00:00Z", closed_at: "2026-07-05T09:00:01Z",
  mode: "paper", symbol: "BTCUSDT", side: "long", strategy: "Decision Brain", timeframe: "4h",
  execution_mode: "paper", market_data_mode: "live", market_data_source: "native exchange candles",
  exchange: "kraken", instance_id: null, instance_name: null, position_id: "p1234567",
  strategy_id: "decision-brain", strategy_name: "Decision Brain", strategy_version: "1.0.0",
  entry: 100, exit: 115, pnl: 30, planned_rr: 3, actual_rr: 3, result: "win", grade: "A", status: "closed",
}] };
const JOURNAL_EVOLUTION = { setups: [{
  setup_key: "Brain|Trending|long", strategy: "Decision Brain", regime: "Trending", side: "long",
  trades: 3, wins: 2, net_r: 4, stage: "early-signal", note: "Early signal — needs 30+ trades." }] };

// --- permanent trade memory (all 8 categories, honesty markers preserved) ---
const MEM_SECTIONS = {
  trade_information: { trade_id: "T1", date: "2026-07-05", time_utc: "09:00:00 UTC", exchange: "kraken",
    symbol: "BTCUSDT", direction: "Long", entry: 100, exit: 115, stop_loss: 95, take_profit: 115,
    position_size: 2, risk_pct: 0.1, planned_rr: 3, actual_rr: 3,
    fees: "0.00 (paper — fees not modeled)", duration: "2h 0m" },
  market_context: { trend: "trend", market_structure: "Not checked", session: "London", volatility: 0.8,
    atr: "not captured", volume: "not captured", liquidity: "not captured", support: "not captured",
    resistance: "not captured", funding_rate: "not captured", fear_greed_index: "not captured",
    btc_dominance: "not captured" },
  technical_analysis: { ema_fast: 101, ema_slow: 99, rsi: 61, macd: "Not checked", vwap: "Not checked",
    bollinger_bands: "Not checked", order_blocks: "Not checked", fair_value_gaps: "Not checked",
    supply_demand: "Not checked", break_of_structure: "Not checked", change_of_character: "Not checked" },
  strategy: { name: "Decision Brain", version: "not captured", timeframe: "15m", setup_grade: "A",
    confidence_score: 70, brain_score: 72, regime: "trend", htf_bias: "not captured" },
  execution: { why_opened: "EMA crossover long", why_closed: "take-profit",
    conditions_passed: ["EMA fast over slow"], conditions_failed: ["None — all evaluated gates passed."] },
  emotion_journal: { manual_notes: "" },
  trade_outcome: { result: "win", profit: 30, loss: 0, pnl: 30, actual_rr: 3,
    mistakes: "None — trade followed the plan.", lessons_learned: "Aligned trend longs pay in this regime",
    improvement_notes: "Repeat the disciplined process." },
  ai_reflection: { what_went_well: "Disciplined A-grade win — the plan was followed and it paid (3R).",
    what_went_wrong: "Nothing mechanical — the stop did its job; the setup simply failed.",
    what_to_repeat: "Repeat the disciplined process. This setup is worth taking again within existing risk limits.",
    what_to_never_do_again: "No hard rule was broken; keep the same discipline.",
    basis: "Composed from the trade's real review + evolution memory (no invented insight)." },
};
const MEM_ROW = { trade_id: "T1", closed_at: "2026-07-05T11:00:00Z", symbol: "BTCUSDT", side: "long",
  strategy: "Decision Brain", timeframe: "15m", result: "win", grade: "A", pnl: 30, actual_rr: 3,
  session: "London", weekday: "Friday", notes: "", sections: MEM_SECTIONS };
const MEM_INSIGHTS = {
  sample: 8, overall: { trades: 8, win_rate: 62.5, expectancy: 12.5, avg_rr: 1.4, pnl: 100 },
  sharpe_ratio: 0.5, sortino_ratio: 1.1, max_drawdown_abs: 40, avg_hold_seconds: 7200,
  by_symbol: [{ symbol: "BTCUSDT", trades: 8, win_rate: 62.5, expectancy: 12.5, avg_rr: 1.4, pnl: 100 }],
  by_strategy: [{ strategy: "Decision Brain", trades: 8, win_rate: 62.5, expectancy: 12.5, avg_rr: 1.4, pnl: 100 }],
  by_session: [{ session: "London", trades: 6, win_rate: 66.7, expectancy: 15, avg_rr: 1.5, pnl: 90 }],
  by_weekday: [{ weekday: "Friday", trades: 5, win_rate: 60, expectancy: 10, avg_rr: 1.3, pnl: 50 }],
  by_setup_grade: [{ grade: "A", trades: 5, win_rate: 80, expectancy: 20, avg_rr: 1.8, pnl: 100 }],
  mistakes: [{ mistake: "Chased the entry after the move started.", count: 2, loss_attributed: -35, repeated: true }],
  winning_patterns: [{ grade: "A", trades: 5, win_rate: 80, expectancy: 20, avg_rr: 1.8, pnl: 100 }],
  evidence_note: "8 closed trades. Early sample — treat breakdowns as signals, not proof.",
  coaching: [{ statement: "You perform 27% better during the London session (+15.000R vs +12.500R overall, 6 trades).",
    stage: "early-signal", metric: null }],
};
const MEM_REVIEWS = { reviews: [{ period: "nightly", period_key: "2026-07-05",
  created_at: "2026-07-05T23:59:00Z",
  report: { overall: { trades: 3, win_rate: 66.7, expectancy: 14 }, sharpe_ratio: 0.5, max_drawdown_abs: 20 } }] };
const MEM_SIMILAR = { similar: [{ trade_id: "T2", symbol: "ETHUSDT", side: "long", result: "win", similarity: 0.92 }] };
const MEM_ASK = { query: "show all losing BTC trades", kind: "filter",
  answer: "Found 1 loss BTCUSDT trades.", trades: [MEM_ROW] };

// exact shapes keyed by pathname substring (first match wins)
const SECURITY_STATUS = {
  audit: { available: true, head: { seq: 3, hash: "a199502f59b7fa8ee0f865b9ca8f3aea73eca81b189ac1102806a46c4813bd7c", ts: "2026-09-24T12:00:00Z" } },
  redaction: { active: true, live_secrets_guarded: 5 },
  vault: { configured: true, encryption: "AES-256-GCM envelope (per-tenant data keys)", master_key_id: "24072e65c0260f9f",
    active_keys: 1, problem: "", scope_check_venues: ["binance"] },
  keys: [{ id: "k1", venue: "binance", label: "Main", key_hint: "…9Q2x", status: "active", created_at: "2026-09-24T12:00:00Z",
    retired_at: null, scope: { allowed: true, refusals: [], warnings: [], read_only: false, can_trade: ["enableFutures"],
      ip_restricted: true, checked_at: 0 } }],
  audit_export: { configured: true, problem: "", destination: "https://siem.example.com", interval_s: 900,
    last_exported_seq: 3, head_seq: 3, pending: 0, last_success_at: "2026-09-24T12:00:00Z", last_attempt_at: "2026-09-24T12:00:00Z", last_error: "" },
  backups: { encrypting: true, count: 3, keep: 7, unencrypted_kept: 1, problem: "",
    latest: { snapshot: "20260924T080000Z", encrypted: true, bytes: 2_400_000, files: 14 } },
  live_routing_locked: true,
};
const AUDIT_ENTRY = (seq: number, actor: string, action: string, status: number) => ({
  seq, ts: "2026-09-24T12:00:00Z", kind: "request", actor, auth: "session", ip: "127.0.0.1", method: "POST",
  path: action.split(" ")[1], status, action, detail: "{\"body\": {\"password\": \"[redacted]\"}}", hash: `${seq}`.repeat(64).slice(0, 64),
});
const SECURITY_AUDIT = {
  entries: [AUDIT_ENTRY(3, "admin", "POST /settings", 200), AUDIT_ENTRY(2, "anonymous", "POST /settings", 401),
    AUDIT_ENTRY(1, "admin", "POST /login", 303)],
  head: SECURITY_STATUS.audit.head,
};

const SHAPES: [string, unknown][] = [
  ["/security/status", SECURITY_STATUS],
  ["/security/checkup", { checked_at: "2026-09-24T12:00:00+00:00", counts: { pass: 2, warn: 1, fail: 1 }, total: 4, checks: [
    { id: "master_key", title: "Master key for secrets", status: "pass", detail: "Exchange keys, webhook secrets and backups are sealed with AES-256-GCM (master key 1a2b3c4d).", fix: "" },
    { id: "two_factor", title: "Two-factor sign-in", status: "warn", detail: "Your account signs in with a password alone.", fix: "Turn on two-factor in Settings → Security and store the recovery codes." },
    { id: "defaults", title: "No default credentials", status: "fail", detail: "Still on the development default: control key (HUB_CONTROL_KEY).", fix: "Set them to long random values in .env and redeploy." },
    { id: "live_routing", title: "Live order routing locked", status: "pass", detail: "Every strategy trades on a paper account; no order reaches an exchange.", fix: "" },
  ] }],
  ["/security/webhooks", { webhooks: [{ id: "whk_1", url: "https://example.com/nexus-events", events: ["decision.accepted", "decision.rejected"],
    description: "", created_at: "2026-09-24T12:00:00Z", active: true }], event_types: ["decision.accepted", "decision.rejected"] }],
  ["/security/api-keys", { keys: [{ id: "a1b2c3d4", name: "research notebook", scopes: ["read"], version: "2026-09-24",
    hint: "nxs_a1b2c3d4_…", created_at: "2026-09-24T12:00:00Z", last_used_at: null, revoked_at: null, active: true }],
    scopes: ["read", "control"], versions: ["2026-09-24"] }],
  ["/security/audit/verify", { ok: true, entries: 3, first_bad_seq: null, reason: "", head_hash: SECURITY_STATUS.audit.head.hash }],
  ["/security/audit", SECURITY_AUDIT],
  ["/security/backups/", { ok: true, snapshot: "20260924T080000Z", encrypted: true, databases: { "ledger.db": { ok: true }, "audit.db": { ok: true } } }],
  ["/research/price-action/live-chart", PA_CHART],
  ["/research/price-action/contracts", { exchange: "Binance USDⓈ-M Futures", contracts: ["BTCUSDT", "ETHUSDT"], timeframes: ["5m"], real_execution_allowed: false }],
  ["/research/price-action/sessions/current/configuration", PA_PAPER],
  ["/research/price-action/sessions", { sessions: [PA_PAPER.session], real_execution_allowed: false }],
  ["/research/price-action/paper", PA_PAPER],
  ["/user/settings", { namespace: "settings-center", data: {
    general: { density: "comfortable", sidebar_default: "expanded" },
  } }],
  ["/instances/options", {
    symbols: ["BTCUSDT", "ETHUSDT"], timeframes: ["1m", "5m", "15m", "1h", "4h"],
    strategies: [{ key: "brain", label: "Decision Brain", versions: ["1.0"] }],
    fill_models: [{ key: "RealisticFill", label: "Realistic" }, { key: "PerfectFill", label: "Ideal" }],
    execution_defaults: { max_open_positions: 3, max_quick_risk_pct: 0.01 },
    platform_defaults: {
      symbol: "BTCUSDT", timeframe: "5m", strategy: "brain", capital: 1000,
      risk_per_trade_pct: 0.005, max_open_positions: 3, entry_mode: "limit", fill_model: "RealisticFill",
    },
  }],
  ["/instances", {
    instances: [], active_slots: 0, max_active_slots: 8, total_current_equity: 10000,
    paper_account_capital: 10000, available_paper_capital: 10000,
    current_global_risk_amount: 0, max_global_risk_amount: 500,
    total_open_positions: 0, global_risk_status: "healthy",
    global_risk_message: "Within configured limits", market_data_status: "idle",
  }],
  ["/controls/state", { state: "Active" }],
  // Trading modes + approvals (§7, §11) — GET shapes (POSTs auto-echo 200)
  ["/engine/mode", { mode: "semi", modes: ["full", "semi", "signal"], pending_approvals: 1 }],
  ["/approvals", { mode: "semi",
    pending: [{ id: 7, symbol: "BTCUSDT", side: "BUY", entry: 65000, stop: 63500,
      target: 69500, confidence: 0.82, planned_rr: 3.0, brain_score: 78,
      timeframe: "4h", strategy: "Decision Brain", status: "pending",
      reason: "Trend + demand reclaim; structure shift confirmed on 4H." }],
    recent: [{ id: 6, symbol: "ETHUSDT", side: "SELL", entry: 3200, stop: 3260,
      target: 3080, confidence: 0.6, planned_rr: 2.0, brain_score: 64,
      timeframe: "4h", strategy: "Decision Brain", status: "rejected",
      reject_reason: "manual" }] }],
  ["/risk/presets", { active: "balanced", presets: {
    conservative: { risk_per_trade_pct: 0.005, max_open_positions: 2, max_daily_loss_pct: 0.02, max_drawdown_pct: 0.10, exposure_limit_pct: 0.10 },
    balanced: { risk_per_trade_pct: 0.01, max_open_positions: 3, max_daily_loss_pct: 0.03, max_drawdown_pct: 0.15, exposure_limit_pct: 0.15 },
    aggressive: { risk_per_trade_pct: 0.02, max_open_positions: 5, max_daily_loss_pct: 0.05, max_drawdown_pct: 0.25, exposure_limit_pct: 0.30 } } }],
  // journal: /trades and /evolution must precede the single-journal fallback
  // Explainable Trading cycle reports: /1 must precede the list fragment
  ["/engine/cycles/1", { id: 1, ts: "2026-07-05T09:00:00Z", symbol: "BTCUSDT",
    timeframe: "5m", price: 100.2, decision: "SKIP", score: 54,
    report: {
      ts: "2026-07-05T09:00:00Z", symbol: "BTCUSDT", timeframe: "5m", price: 100.2,
      decision: "SKIP", side: "long", score: 54,
      market_analysis: { available: true, bias: "Neutral",
        trend: { ema8_vs_ema33: "above", swing_highs: "Higher High", swing_lows: "Higher Low" },
        structure: { state: "consolidation", break_of_structure: "none", change_of_character: false },
        volume: { label: "below average" }, volatility: { label: "medium" },
        liquidity: { sweep: "none detected" }, last_candle: "no notable pattern" },
      checklist: [
        { name: "EMA alignment", status: "PASS", explanation: "EMA8 above EMA33 for a long setup" },
        { name: "Risk:reward >= 2.0", status: "FAIL", explanation: "planned RR 1.40:1 — minimum is 2.0" },
        { name: "Volume confirmation", status: "FAIL", explanation: "below average (x0.72 vs 20-bar avg)" },
        { name: "Session allowed", status: "PASS", explanation: "UTC hour 9 in window 0-24" }],
      scores: { available: true, trend: 16, structure: 8, supply_demand: 10,
        volume: 6, risk: 14, total: 54, label: "skip-quality", engine_score: 48 },
      reasons: ["Blocked at the brain gate: Score 48/100 below minimum 60",
        "\u274c Risk:reward only 1.4 — minimum required is 2.0",
        "\u274c Volume below average"],
      recommendation: "Wait for a pullback toward the zone — a closer entry improves the RR." } }],
  ["/engine/cycles", { total: 42, cycles: [
    { id: 1, ts: "2026-07-05T09:00:00Z", symbol: "BTCUSDT", timeframe: "5m",
      price: 100.2, decision: "SKIP", score: 54 },
    { id: 2, ts: "2026-07-05T08:55:00Z", symbol: "ETHUSDT", timeframe: "5m",
      price: 2001.4, decision: "WAIT", score: 41 }] }],
  ["/journal/trades", JOURNAL_TRADES],
  ["/journal/evolution", JOURNAL_EVOLUTION],
  ["/journal/t1234567abcdef", JOURNAL_FULL],
  // trade memory: specific paths precede the single-memory fallback (/{id})
  ["/trade-memory/growth", { available: true,
    totals: { trades: 21, wins: 13, losses: 8, breakeven: 0, win_rate: 61.9,
      net_pnl: 412.5, net_r: 14.2, expectancy_r: 0.676, best_r: 3.0, worst_r: -1.2,
      avg_win_r: 2.1, avg_loss_r: -1.0, profit_factor: 3.4 },
    streaks: { current: 3, longest_win: 5, longest_loss: 2 },
    span: { first: "2026-06-02T09:00:00Z", last: "2026-07-05T09:00:00Z" },
    monthly: [{ month: "2026-06", trades: 12, net_r: 8.4, win_rate: 58.3 },
              { month: "2026-07", trades: 9, net_r: 5.8, win_rate: 66.7 }],
    by_strategy: [{ name: "Decision Brain", trades: 16, win_rate: 62.5, net_r: 11.0 },
                  { name: "Supertrend", trades: 5, win_rate: 60.0, net_r: 3.2 }],
    by_symbol: [{ name: "BTCUSDT", trades: 12, win_rate: 66.7, net_r: 9.1 },
                { name: "ETHUSDT", trades: 9, win_rate: 55.6, net_r: 5.1 }],
    grades: { A: 6, B: 10, C: 5 },
    sample_note: "early sample — fewer than 30 remembered trades; treat every number as provisional" }],
  ["/trade-memory/trades", { trades: [MEM_ROW], total: 8 }],
  ["/trade-memory/ask", MEM_ASK],
  ["/trade-memory/insights", MEM_INSIGHTS],
  ["/trade-memory/mistakes", { mistakes: MEM_INSIGHTS.mistakes }],
  ["/trade-memory/reviews", MEM_REVIEWS],
  ["/trade-memory/similar/", MEM_SIMILAR],
  ["/trade-memory/T1", MEM_ROW],
  ["/settings", SETTINGS],
  ["/replay/run", {
    meta: { symbol: "BTCUSDT", timeframe: "1h", data_source: "demo", data_source_label: "demo sample",
      bars: 4, start: null, end: null, htf_available: {}, strategy: "Decision Brain", data_warning: null,
      viz: { title: "Decision Brain · multi-factor", explain: "trend EMAs + RSI + structure/zones",
        used: [{ label: "EMA 20 / EMA 50", detail: "trend" }, { label: "RSI (14)", detail: "momentum" },
          { label: "Structure + zones", detail: "confluence" }],
        overlays: ["ema20", "ema50"], osc: "rsi", structure: true, zones: true,
        crossovers: false, supertrend: false, volume: true } },
    candles: [
      { t: "2026-07-16T10:00:00", o: 60000, h: 60400, l: 59800, c: 60240, v: 1200 },
      { t: "2026-07-16T11:00:00", o: 60240, h: 60800, l: 60100, c: 60700, v: 1500 },
      { t: "2026-07-16T12:00:00", o: 60700, h: 61400, l: 60600, c: 61300, v: 1800 },
      { t: "2026-07-16T13:00:00", o: 61300, h: 61900, l: 61200, c: 61800, v: 1400 }],
    overlays: { ema8: [null, 60300, 60700, 61200], ema30: [null, 60200, 60500, 60900] },
    markers: [{ idx: 1, price: 60240, type: "Entry", side: "bull" }],
    zones: [], events: [
      { idx: 0, kind: "scan", text: "Scanning market conditions" },
      { idx: 1, kind: "entry", text: "Long entered @ 60,240 — BOS + sweep" },
      { idx: 3, kind: "exit", text: "Take profit reached (+2.4R)" }],
    frames: [
      { regime: "trending", trends: { "4H": "up" }, trigger: "", score: 40, breakdown: null, blocked: true, reason: "score below threshold", vol_ratio: 1.0 },
      { regime: "trending", trends: { "4H": "up" }, trigger: "BOS", score: 87, breakdown: { htf: 20, structure: 18 }, blocked: false, reason: "", vol_ratio: 1.2 },
      { regime: "trending", trends: { "4H": "up" }, trigger: "", score: 70, breakdown: null, blocked: false, reason: "", vol_ratio: 1.1 },
      { regime: "trending", trends: { "4H": "up" }, trigger: "", score: 66, breakdown: null, blocked: false, reason: "", vol_ratio: 1.0 }],
    trades: [{ id: 1, symbol: "BTCUSDT", side: "long", entry_idx: 1, entry: 60240, sl: 59700, tp: 61800,
      tp1: null, tp1_idx: null, score: 87, breakdown: { htf: 20 }, entry_reasons: ["HTF bullish", "BOS confirmed"],
      exit_idx: 3, exit: 61800, exit_reason: "target", result: "win", rr: 2.4, loss_analysis: null }],
    stats: { symbol: "BTCUSDT", trades: 1, win_rate: 100, profit_factor: 99, net_r: 2.4, max_drawdown_r: 0,
      avg_rr: 2.4, expectancy_r: 2.4, best_r: 2.4, worst_r: 2.4, long_trades: 1, short_trades: 0,
      long_net_r: 2.4, short_net_r: 0, max_consecutive_wins: 1, max_consecutive_losses: 0, current_streak: 1 } }],
  ["/strategies/registry", { strategies: [] }],
  ["/scanner/scan", { count: 0, opportunities: [], symbols: [] }],
  ["/control/compare", { winner: "A",
    a: { strategy: "Decision Brain", timeframe: "4h", results: { total_trades: 10, win_rate: 55, profit_factor: 1.6, net_r: 8, max_drawdown_pct: 12 } },
    b: { strategy: "EMA Cross", timeframe: "4h", results: { total_trades: 12, win_rate: 48, profit_factor: 1.2, net_r: 4, max_drawdown_pct: 15 } } }],
  ["/execution/realism", { available: true, edge_survives: true, rejected: 0, partial_fills: 0, slippage_cost_r: 0,
    ideal: { net_r: 10, profit_factor: 1.8, win_rate: 55, expectancy_r: 0.3 },
    realistic: { net_r: 8, profit_factor: 1.6, win_rate: 53, expectancy_r: 0.25 } }],
  ["/strategy/list", { active: "brain", timeframe: "4h", strategies: [
    { key: "brain", label: "Decision Brain", desc: "Multi-factor trend" },
    { key: "supertrend", label: "Supertrend", desc: "ATR trend-following" },
    { key: "smc", label: "SMC (Smart Money)", desc: "Liquidity + structure" }] }],
  ["/strategy/health", STRAT_HEALTH],
  ["/risk/portfolio", { available: false, positions: [], allocations: [], correlations: [], concentration: [] }],
  ["/production/readiness", { checks: [] }],
  ["/safety/live-readiness", { live_allowed: false, hard_locked: true,
    locked_reason: "Live execution is locked by design in this build — paper mode only.",
    default_mode: "paper", passed: 2, total: 6, requirements: [
      { key: "paper_record", label: "Paper trading track record", passed: false, detail: "0 closed paper trades (need ≥ 30)" },
      { key: "emergency_stop_tested", label: "Emergency stop tested", passed: false, detail: "never run" },
      { key: "max_daily_loss", label: "Max daily loss configured", passed: false, detail: "disabled" },
      { key: "max_drawdown", label: "Max drawdown configured", passed: true, detail: "20.00% circuit breaker" },
      { key: "broker_connected", label: "Live broker connection verified", passed: false, detail: "no live broker connected (paper only)" },
      { key: "decision_logging", label: "Decision logging enabled", passed: true, detail: "every trade is journaled" },
    ] }],
  ["/safety/test-emergency-stop", { ok: true, verified: true, prior_state: "Active", state_after: "Active", tested_at: "2026-07-05T09:00:00Z" }],
  ["/skipped/trades", { trades: [
    { id: 2, ts: "2026-07-05T09:10:00Z", symbol: "ETHUSDT", side: "SELL", stage: "risk_guard", category: "risk",
      status: "rejected", reason: "Max open positions (3) reached", entry: 2000, stop: 2100, target: null,
      strategy: "Decision Brain", timeframe: "4h", snapshot: { price: 2000, rsi: 71, regime: "Ranging" } },
    { id: 1, ts: "2026-07-05T09:05:00Z", symbol: "BTCUSDT", side: "BUY", stage: "controls", category: "safety",
      status: "rejected", reason: "Trading paused — entry blocked", entry: 100, stop: 95, target: 115,
      strategy: "Decision Brain", timeframe: "4h", snapshot: {} },
  ] }],
  ["/validation/paper", {
    sample_size: 24, min_review: 30, min_evidence: 50,
    metrics: { win_rate: 58, profit_factor: 1.7, expectancy: 12.5, max_drawdown_pct: 8.2, avg_rr: 1.3, sharpe_ratio: 0.42, sortino_ratio: 1.06 },
    best_symbol: { name: "BTCUSDT", net_pnl: 200 }, worst_symbol: { name: "ETHUSDT", net_pnl: -50 },
    best_strategy: { name: "Decision Brain", net_r: 12 }, worst_strategy: { name: "Decision Brain", net_r: 12 },
    skipped_total: 7, skipped_by_category: [{ category: "safety", count: 5 }, { category: "risk", count: 2 }],
    safety: { live_allowed: false, hard_locked: true, passed: 3, total: 6 },
    live_review: { eligible: false, stage: "insufficient-sample",
      reasons: ["Need ≥ 30 closed paper trades (have 24).", "Safety guards incomplete: max_daily_loss."],
      note: "Live trading stays LOCKED regardless of this verdict. This is human-review eligibility only — it never auto-enables real-money trading." },
  }],
  ["/skipped/summary", { stages: [{ stage: "risk_guard", count: 1 }, { stage: "controls", count: 1 }] }],
  ["/health/bot", {
    engine: { running: true, mode: "paper", strategy: "Decision Brain", symbols: ["BTCUSDT", "ETHUSDT"],
      timeframe: "4h", bars_processed: 150, signals: 4, trades: 1, rejections: 2, uptime_s: 320, started_at: "2026-07-05T09:00:00Z" },
    data_source: "synthetic / replay",
    broker: { connected: false, active: "paper", live_locked: true, note: "paper execution only — no live venue connected" },
    last_candle: { symbol: "BTCUSDT", ts: "2026-07-05T09:05:00Z" },
    last_signal: { symbol: "BTCUSDT", side: "long", entry: 100, ts: "2026-07-05T09:04:00Z" },
    last_rejected: { symbol: "ETHUSDT", side: "SELL", stage: "risk_guard", reason: "Max open positions (3) reached", ts: "2026-07-05T09:03:00Z" },
    open_positions: 1, daily_pnl: 30,
    risk: { equity: 10000, exposure_pct: 0.02, exposure_limit_pct: 0.05, open_positions: 1, max_open_positions: 3,
      trading_state: "Active", auto_halted: false, halt_reason: "", max_drawdown_pct: 0.2 },
    watchdog: { running: true, findings: [], last_heartbeat: "2026-07-05T09:05:30Z" },
    errors: [],
  }],
  ["/bot-os", { services: [] }],
  ["/alerts/channels", { channels: [] }],
  ["/alerts/check", { ok: true, issues: [] }],
  ["/econ/protection", { mode: "normal", actions: [], next_event: null, minutes_to_event: null }],
  ["/market/context", { fear_greed: { available: false }, btc_dominance: { available: false }, total_mcap_usd: { available: false }, eth_btc: { available: false }, funding_rate: { available: false }, open_interest: { available: false }, liquidations: { available: false }, econ_calendar: { available: false }, news: { available: false, connected: false, headlines: [] }, provider_debug: [] }],
  ["/paper/equity-curve", { points: [] }],

  ["/system/status", SYSTEM],
  ["/risk/summary", RISK],
  ["/risk/portfolio", { available: false }],
  ["/risk/recovery", { available: false }],
  ["/strategy/performance", PERF],
  ["/lab/walk-forward", WALK_FORWARD],
  ["/paper/account", { initial_capital: 10000, current_equity: 10300, available_balance: 10300,
    realized_pnl: 300, unrealized_pnl: 0, last_updated: "2026-07-05T09:00:00Z", open_positions: 0,
    persistent: true, storage: "supabase", warning: null, starting_balance: 10000, balance: 10300 }],
  ["/auth/status", { authenticated: true, user: "admin", signup_open: false }],
  ["/notifications/status", { notify_trades: true, notify_risk: true, configured: false }],
  ["/execution/fill-model", { model: "perfect" }],
  ["/engine/status", { running: false, symbols: ["BTCUSDT"], entry_mode: "limit", timeframe: "4h", strategy: "Decision Brain" }],
  ["/control/options", { symbols: ["BTCUSDT", "ETHUSDT"], timeframes: ["1h", "4h", "1d"],
    strategies: ["Decision Brain"], default_tuning: {} }],
  ["/markets/watchlist", { rows: [], any_real: false }],
  ["/symbols/asset-classes", { asset_classes: [{ asset_class: "crypto", count: 2 }, { asset_class: "stock", count: 1 }],
    crypto_source: "fallback (seed list)", synced_at: "2026-07-16T00:00:00Z", total: 3 }],
  ["/symbols/search", { query: "b", results: [
    { symbol: "BTC/USDT", ticker: "BTCUSDT", name: "Bitcoin", asset_class: "crypto", exchange: "Binance", base: "BTC", quote: "USDT", type: "spot", session: "24/7" },
    { symbol: "BTC/USD", ticker: "BTCUSD", name: "Bitcoin", asset_class: "crypto", exchange: "Binance", base: "BTC", quote: "USD", type: "spot", session: "24/7" },
    { symbol: "AAPL", ticker: "AAPL", name: "Apple Inc.", asset_class: "stock", exchange: "NASDAQ", base: "AAPL", quote: "USD", type: "equity", session: "regular" }] }],
  ["/symbols/info", { found: true, symbol: "BTC/USDT", ticker: "BTCUSDT", name: "Bitcoin", asset_class: "crypto",
    exchange: "Binance", base: "BTC", quote: "USDT", type: "spot", session: "24/7", market_status: "open",
    price_available: false, note: "mock", favorite: false, pinned: false }],
  ["/symbols", { count: 2, symbols: [
    { symbol: "BTC/USDT", ticker: "BTCUSDT", name: "Bitcoin", asset_class: "crypto", exchange: "Binance",
      base: "BTC", quote: "USDT", type: "spot", session: "24/7", favorite: true, pinned: false },
    { symbol: "ETH/USDT", ticker: "ETHUSDT", name: "Ethereum", asset_class: "crypto", exchange: "Binance",
      base: "ETH", quote: "USDT", type: "spot", session: "24/7", favorite: false, pinned: false }] }],
  ["/market/prefs", { favorites: ["BTCUSDT"], pinned: [], watchlists: [{ id: "w1", name: "Crypto", symbols: ["BTCUSDT"] }] }],
  ["/ai/analyze", { available: true, symbol: "BTCUSDT", timeframe: "1h", ts: "2026-07-16T00:00:00Z", price: 60000,
    decision: "BUY", side: "long", overall_score: 82, confidence_level: "High", confidence_pct: 82, engine_score: 84,
    allowed: true, min_score: 60,
    score_breakdown: [{ category: "Trend", score: 18, max: 20 }, { category: "Market Structure", score: 17, max: 20 },
      { category: "Volume", score: 15, max: 20 }, { category: "Risk Management", score: 18, max: 20 },
      { category: "Confirmation", score: 14, max: 20 }],
    reasons: ["Higher-timeframe trend agrees", "BOS confirmed", "Volume above average"], failed_checks: [],
    recommendation: "Trade placed — manage per plan.",
    risk_analysis: { position_size: 0.01, notional: 6000, max_loss: 100, expected_profit: 200, risk_pct: 1,
      risk_reward: 2, margin_used: 600, leverage: 10, liquidation_price: 54300, portfolio_exposure_pct: 60,
      excessive: false, warning: null },
    setup: { entry: 60000, stop: 59000, target: 62000 },
    market_analysis: { available: true, bias: "Bullish", trend: { strength_label: "strong" },
      structure: { state: "trending up", break_of_structure: "bullish", change_of_character: false },
      liquidity: { sweep: "none detected" }, volume: { label: "above average" }, volatility: { label: "normal" } },
    checklist: [] }],
  ["/ai/profile", { sample: 12, ready: true, strengths: ["Best in the London session (+0.6R)."],
    weaknesses: ["Repeated mistake: entered before confirmation (×4)"], avg_hold_seconds: 3600,
    sharpe_ratio: 1.1, win_rate: 57, expectancy_r: 0.3, note: "Profile updates automatically as trades close." }],
  ["/ai/confidence-accuracy", { sample: 24, ready: true, calibrated: true,
    verdict: "Well calibrated: high-confidence setups win 72% vs 41% for low-confidence (+31 pts).",
    high_conf_win_rate: 72, low_conf_win_rate: 41, spread_pts: 31,
    by_confidence: [{ level: "Very High", trades: 6, wins: 5, win_rate: 83, avg_rr: 1.8, avg_pnl: 40 },
      { level: "High", trades: 8, wins: 5, win_rate: 62, avg_rr: 1.2, avg_pnl: 20 },
      { level: "Medium", trades: 4, wins: 2, win_rate: 50, avg_rr: 0.4, avg_pnl: 5 },
      { level: "Low", trades: 4, wins: 1, win_rate: 25, avg_rr: -0.3, avg_pnl: -12 },
      { level: "Very Low", trades: 2, wins: 1, win_rate: 50, avg_rr: 0.1, avg_pnl: 2 }] }],
  ["/ai/alerts", { count: 2, checked: ["BTCUSDT", "ETHUSDT"], alerts: [
    { type: "strong_setup", severity: "success", title: "Strong setup — BTCUSDT", detail: "BUY at score 88/100.", symbol: "BTCUSDT" },
    { type: "outside_session", severity: "info", title: "Outside trading session", detail: "Entries held until in-session.", symbol: "" }] }],
  ["/ai/insights", { timeframe: "1h", symbols: ["BTCUSDT", "ETHUSDT"], insights: [
    { symbol: "BTCUSDT", kind: "trend", tone: "green", text: "BTC is trending strongly (bullish)." },
    { symbol: "ETHUSDT", kind: "volume", tone: "default", text: "ETH volume is decreasing (below its 20-bar average)." }] }],
  ["/ai/coach", { sample: 7, ready: true, trades: 7, win_rate: 57, expectancy_r: 0.2, avg_hold_seconds: 5400,
    main_mistake: "entered before confirmation", suggestion: "Wait for BOS before entering.",
    risk_discipline: "Excellent", best_session: "London", worst_setup: "counter-trend fade",
    headline: "You've taken 7 trades at a 57% win rate." }],
  ["/trade-memory/insights", { sample: 7, overall: { trades: 7, win_rate: 57, expectancy: 0.2 }, avg_hold_seconds: 5400,
    best_session: { name: "London", expectancy: 0.6 }, worst_session: { name: "Asia", expectancy: -0.3 },
    by_symbol: [{ name: "BTCUSDT", expectancy: 0.5, trades: 4 }], by_strategy: [{ name: "Decision Brain", expectancy: 0.4, trades: 7 }],
    mistakes: [{ mistake: "entered before confirmation", count: 4, repeated: true }] }],
  ["/strategy/blocks", { categories: [
    { key: "indicators", label: "Indicators", blocks: [
      { type: "ema_cross", label: "EMA Crossover", desc: "Fast EMA vs slow EMA", params: [
        { name: "fast", type: "number", default: 20, label: "fast" }, { name: "slow", type: "number", default: 50, label: "slow" },
        { name: "dir", type: "select", default: "above", label: "dir", options: ["above", "below"] }] }] },
    { key: "market_structure", label: "Market Structure", blocks: [
      { type: "bos", label: "Break of Structure", desc: "BOS", params: [{ name: "dir", type: "select", default: "up", label: "dir", options: ["up", "down"] }] }] }],
    config: { logic: ["AND", "OR", "NOT"], risk: [], stop: [], target: [], exit: [],
      sessions: [{ key: "any", label: "Any", start: 0, end: 24 }, { key: "london", label: "London", start: 7, end: 16 }] } }],
  ["/strategy/templates", { templates: [
    { id: "smc", name: "Smart Money Concepts", description: "BOS + sweep", side: "long", symbol: "BTCUSDT", timeframe: "4h",
      entry: { op: "AND", rules: [{ type: "bos", dir: "up" }] }, stop: { type: "atr", mult: 1.5 }, target: { type: "rr", rr: 3 }, risk_per_trade_pct: 0.01 }] }],
  ["/strategy/ai-review", { complexity: "simple", rule_count: 1, risk_level: "conservative",
    expected_behaviour: "~20 trades, 55% win rate, profit factor 1.4 in simulation.",
    strengths: ["Healthy reward:risk target (3.0R)."], weaknesses: ["Only one entry condition."],
    improvements: ["Add a confirmation."], estimated_confidence: 62, confidence_level: "Medium",
    summary: "Go long when a bullish BOS occurs.", warnings: [] }],
  ["/ai/confidence-levels", { levels: [{ level: "Very High", min_score: 85 }] }],
  ["/strategy/league", { available: false, detail: "no data (mock)" }],
  ["/report/daily", { report: {}, text: "Daily report — mock", telegram_configured: false }],
  ["/performance/track-record", { live: { trades: 0 }, verdict: "insufficient-live-trades", detail: "mock" }],
  ["/execution/quality", { overall: { fills: 0 } }],
  ["/data/integrity", { verdict: "empty", series: [] }],
  ["/learning/report", { active_adjustments: {}, evolution: [], lessons: [] }],
  ["/counterfactual/report", { total_saved_r: 0, open_virtual_trades: 0, rules: {} }],
  ["/shadow/report", { active: false, note: "mock" }],
  ["/retune/report", { ran: false, note: "mock" }],
  ["/ops/watchdog", { running: true, findings: [], last_heartbeat: null }],
  ["/ops/storage", { data_dir: "/logs", persistent: true, files: {}, warning: null }],
  ["/evolution/dashboard", { sentiment: { available: false }, workflow: [], lessons_weekly: 0,
    lessons_total: 0, upgrade_status: {}, live_rule: "mock" }],
  ["/market/context", { fear_greed: { available: false }, news: { available: false, connected: false, headlines: [] } }],
  ["/paper/equity-curve", { points: [] }],
  ["/bots/live", []],
  ["/system/why-no-trades", { reasons: [] }],
  ["/news/world", { available: false, headlines: [], snapshot: {} }],
];

const ARRAY_HINTS = ["/paper/trades", "/paper/positions", "/ledger/", "/strategy/custom", "/bots",
  "/research", "/brokers", "/journal", "/logs", "/commits", "/evolution/lessons",
  "/evolution/upgrades", "/alerts"];

function bodyFor(pathname: string): unknown {
  for (const [frag, shape] of SHAPES) if (pathname.includes(frag)) return shape;
  if (ARRAY_HINTS.some((h) => pathname.includes(h))) return [];
  return {};
}

// /calendar/* responses produced by the real calendar service over real
// engine trades (see e2e/fixtures/generate_calendar_fixture.py). Other months
// answer as months with no closed trades, exactly as the API would.
export const CALENDAR = JSON.parse(readFileSync(new URL("./fixtures/calendar.json", import.meta.url), "utf8"));

function calendarMonth(url: URL) {
  const year = Number(url.searchParams.get("year")); const month = Number(url.searchParams.get("month"));
  const source = url.searchParams.get("source");
  if (year === 2026 && month === 9) return source ? CALENDAR.month_by_source[source] : CALENDAR.month;
  const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const iso = (t: Date) => t.toISOString().slice(0, 10);
  const first = new Date(Date.UTC(year, month - 1, 1));
  const last = new Date(Date.UTC(year, month - 1, days));
  const weeks = [];
  for (let start = new Date(first.getTime() - ((first.getUTCDay() + 6) % 7) * 864e5); start <= last;
    start = new Date(start.getTime() + 7 * 864e5)) {
    const end = new Date(start.getTime() + 6 * 864e5);
    weeks.push({ start: iso(start), from: iso(start < first ? first : start), to: iso(end > last ? last : end),
      state: "none", by_currency: {}, closed_trades: 0, realizations: 0 });
  }
  return {
    year, month, summary: {}, currencies: [], timezone: CALENDAR.month.timezone, filters: {}, weeks,
    days: Array.from({ length: days }, (_, i) => ({
      date: `${year}-${String(month).padStart(2, "0")}-${String(i + 1).padStart(2, "0")}`,
      state: "none", by_currency: {}, closed_trades: 0, realizations: 0 })),
    diagnostics: CALENDAR.month.diagnostics,
    conversion: { display_currency: "USDT", needed: false, available: false, unconverted: [], note: "" },
  };
}

function calendarDay(url: URL) {
  const date = url.searchParams.get("date_") ?? "";
  return CALENDAR.days[date] ?? {
    date, summary: {}, currencies: [], state: "none", sources: [], strategies: [], hourly: [], trades: [],
    time_of_day: CALENDAR.days["2026-09-20"].time_of_day, timezone: CALENDAR.month.timezone, filters: {},
    diagnostics: CALENDAR.month.diagnostics,
    conversion: { display_currency: "USDT", needed: false, available: false, unconverted: [], note: "" },
  };
}

export async function mockApi(page: Page) {
  let paPaper: any = structuredClone(PA_PAPER);
  await page.route(
    (url) => url.host === "localhost:8000",
    async (route: Route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/forward-validation") {
        return route.fulfill({ json: {
          stage_status: "BLOCKED", verdict: "NO_ELIGIBLE_CANDIDATES", validation_started_at: null,
          active_experiments: [], candidates: [],
          candidate_counts: { "FORWARD PAPER ELIGIBLE": 0, "RESEARCH ONLY": 0, REJECTED: 0 },
          historical_evidence: {
            exchange: "Binance", instrument: "USD-M", timeframe: "5m",
            start_utc: "2026-07-01T00:00:00Z", end_utc: "2026-08-01T00:00:00Z",
            candles_per_symbol: 8928, symbols: ["BTCUSDT"], bundle_sha256: "mock-evidence-bundle",
            forward_venue: "Kraken", exact_venue_parity: "Not established",
          },
          forward_evidence: { experiments: 0, counted_candles: 0, decisions: 0, trades: 0, note: "No forward evidence." },
          next_action: "Review frozen evidence before starting an experiment.",
        } });
      }
      if (url.pathname === "/calendar/options") return route.fulfill({ json: CALENDAR.options });
      if (url.pathname === "/calendar/month") return route.fulfill({ json: calendarMonth(url) });
      if (url.pathname === "/calendar/day") return route.fulfill({ json: calendarDay(url) });
      if (url.pathname === "/calendar/export.csv") {
        return route.fulfill({ status: 200, contentType: "text/csv; charset=utf-8",
          headers: { "Content-Disposition": `attachment; filename="realized-pnl_${url.searchParams.get("start")}_${url.searchParams.get("end")}.csv"` },
          body: "closed_at,net\n" });
      }
      if (url.pathname === "/research/price-action/journal") {
        return route.fulfill({ json: { entries: [], real_execution_allowed: false,
          statistics: { setups: 0, completed: 0, wins: 0, losses: 0, net_r: 0, expectancy_r: 0 } } });
      }
      if (url.pathname === "/research/price-action/learning/analysis") {
        return route.fulfill({ json: { classifications: {}, patterns: [], minimum_pattern_sample: 30,
          warning: "Insufficient evidence", active_strategy_mutated: false, real_execution_allowed: false } });
      }
      if (url.pathname === "/research/price-action/learning/candidates") {
        return route.fulfill({ json: { candidates: [] } });
      }
      if (url.pathname.includes("/research/price-action/live-chart")) {
        const symbol = url.searchParams.get("symbol") ?? paPaper.session.symbol;
        const timeframe = url.searchParams.get("timeframe") ?? paPaper.session.timeframe;
        const chart: any = structuredClone(PA_CHART);
        chart.symbol = symbol; chart.timeframe = timeframe;
        chart.data_identity = { request_id: url.searchParams.get("request_id"),
          session_id: paPaper.session.id, mode: paPaper.session.mode ?? "LIVE_PAPER", symbol, timeframe };
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(chart) });
      }
      if (url.pathname.endsWith("/research/price-action/bot-status")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          lab: "PRICE_ACTION", account_scope: "PRICE_ACTION_VISUAL_LAB_ONLY",
          scope_label: "Price Action session · isolated paper ledger", paper_only: true,
          real_execution_allowed: false, strategy: { id: "PA1_SR_REJECTION", version: "1.1.0" },
          symbol: "BTCUSDT", timeframe: "5m", mode: "automatic", saved_configuration: {},
          feed: { state: "SYNCHRONIZED", reliable: true, health_reason: "reconciled",
            last_successful_event: { kind: "mark_price", at: "2026-08-24T21:30:00Z" }, retry_state: { attempt: 0 } },
          decision_state: "POSITION_OPEN", execution_state: "ELIGIBLE_ON_CONFIRMED_CLOSED_CANDLE",
          blockers: [], account: { balance: 9932.58, equity: 9935.17, realized_pnl: -67.42, unrealized_pnl: 2.59 },
          open_positions: 1, pending_orders: 0, positions: [],
          latest_closed_candle_decision: { correlation_id: "pa-corr", state: "POSITION_OPEN",
            candle_time: "2026-08-24T21:30:00Z", reason: "protected paper entry filled", missing_conditions: [] },
          latest_signal: { correlation_id: "pa-corr" }, latest_order: { order_id: "pa-order", status: "ENTERED" },
          latest_fill: { order_id: "pa-order", created_at: "2026-08-24T21:30:01Z" },
          last_heartbeat: "2026-08-24T21:30:02Z", performance: {
            backtest: { scope: "BACKTEST", closed_trades: 10, win_rate: .5, profit_factor: 1.1, average_realized_rr: .2 },
            forward_validation: { scope: "FORWARD_VALIDATION", closed_trades: 2, win_rate: .5, profit_factor: 1, average_realized_rr: 0 },
            live_paper: { scope: "LIVE_PAPER", closed_trades: 19, win_rate: .42, profit_factor: .8, average_realized_rr: -.2, maximum_drawdown: 75 },
          },
        }) });
      }
      if (url.pathname.endsWith("/research/smc/bot-status")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          lab: "SMC", account_scope: "SMC_STRATEGY_LAB_ONLY",
          scope_label: "SMC Strategy Lab session · isolated paper ledger", paper_only: true,
          real_execution_allowed: false, strategy: { id: "SMC_SOURCE_V1", model_id: "SMC_M1_SWEEP_REVERSAL", version: "1.0.0" },
          symbol: "BTCUSDT", timeframe: "5m", mode: "signals_only", saved_configuration: {},
          feed: { state: "SYNCING", reliable: false, health_reason: "candle histories differ",
            failing_dependency: "COMPLETED_CANDLE_RECONCILIATION", retry_state: { attempt: 2 } },
          decision_state: "SYNCING", execution_state: "BLOCKED",
          blockers: ["market data is not synchronized", "saved operating mode is not Automatic paper"],
          account: { balance: 10000, equity: 10000, realized_pnl: 0, unrealized_pnl: 0 },
          open_positions: 0, pending_orders: 0, positions: [], latest_closed_candle_decision: null,
          latest_signal: null, latest_order: null, latest_fill: null, last_heartbeat: null,
          performance: { backtest: { available: false, reason: "not attached" },
            forward_validation: { available: false, reason: "not attached" },
            live_paper: { closed_trades: 0, win_rate: null, profit_factor: null, average_realized_rr: null, maximum_drawdown: 0 } },
        }) });
      }
      if (url.pathname.endsWith("/research/smc/live-chart")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SMC_CHART) });
      }
      if (url.pathname.endsWith("/research/smc/chart")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SMC_CHART) });
      }
      if (url.pathname.endsWith("/research/smc/paper")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SMC_PAPER) });
      }
      if (url.pathname.endsWith("/research/smc/strategy-models")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          strategy_id: "SMC_SOURCE_V1", strategy_version: SMC_SOURCE.version, paper_only: true,
          real_execution_allowed: false, models: [SMC_SOURCE.model],
        }) });
      }
      if (url.pathname.endsWith("/research/smc/strategy-v1/evaluate")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SMC_SOURCE) });
      }
      if (url.pathname.endsWith("/research/smc/sessions")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ sessions: [SMC_PAPER.session], paper_only: true, real_execution_allowed: false }) });
      }
      if (url.pathname.endsWith("/research/smc/journal")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ journal: [], paper_only: true, real_execution_allowed: false }) });
      }
      if (url.pathname.includes("/research/smc/review-sample")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ sample: [] }) });
      if (url.pathname.includes("/research/smc/reviews")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ reviews: [] }) });
      if (url.pathname.endsWith("/research/smc/pine-reference")) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ reference_id: "mock", status: "REFERENCE_ONLY", language: "pine", sha256: "0".repeat(64), execution_allowed: false, notice: "Reference only", content: "// mock" }) });
      if (url.pathname.endsWith("/research/price-action/paper") && route.request().method() === "GET") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(paPaper) });
      }
      if (url.pathname.endsWith("/research/price-action/paper/orders/reconcile") && route.request().method() === "POST") {
        const audit = structuredClone(paPaper.order_audit);
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          actions: [], before: audit, after: audit, records_deleted: 0, manual_orders_changed: 0,
        }) });
      }
      if (url.pathname.includes("/research/price-action/sessions/current/configuration") && route.request().method() === "POST") {
        const body = route.request().postDataJSON() as Record<string, any>;
        paPaper = { ...paPaper, session: { ...paPaper.session,
          symbol: body.symbol ?? paPaper.session.symbol, timeframe: body.timeframe ?? paPaper.session.timeframe,
          mode: body.mode ?? paPaper.session.mode, operating_mode: body.operating_mode ?? paPaper.session.operating_mode,
          execution_config: { ...paPaper.session.execution_config,
            strategy_id: body.strategy_id ?? paPaper.session.execution_config.strategy_id,
            risk_pct: body.risk_pct ?? paPaper.session.execution_config.risk_pct } } };
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(paPaper) });
      }
      // POST/PUT actions succeed with an echo so save/toggle flows show success
      if (route.request().method() !== "GET") {
        return route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify({ ok: true, saved: true, ...(bodyFor(url.pathname) as object) }) });
      }
      return route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify(bodyFor(url.pathname)) });
    },
  );
}
