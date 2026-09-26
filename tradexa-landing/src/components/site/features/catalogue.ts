import {
  Activity,
  BrainCircuit,
  Building2,
  CalendarClock,
  ClipboardList,
  Database,
  FlaskConical,
  Gauge,
  GitBranch,
  KeyRound,
  LineChart,
  ListFilter,
  NotebookText,
  Radio,
  Repeat,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Timer,
  TrendingUp,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import type { ScreenKind } from "./ScreenMock";

export type CategoryId =
  | "intelligence"
  | "risk"
  | "research"
  | "execution"
  | "memory"
  | "operations";

export interface Category {
  id: CategoryId;
  label: string;
  /** Short line under the category in the rail. */
  note: string;
}

export const CATEGORIES: Category[] = [
  { id: "intelligence", label: "Intelligence", note: "Reading the market" },
  { id: "risk", label: "Risk", note: "What never gets through" },
  { id: "research", label: "Research", note: "Proving it first" },
  { id: "execution", label: "Execution", note: "Getting filled well" },
  { id: "memory", label: "Memory", note: "Learning from itself" },
  { id: "operations", label: "Operations", note: "Running it day to day" },
];

export interface FeatureSpec {
  label: string;
  value: string;
}

export interface FeatureEntry {
  id: string;
  icon: LucideIcon;
  category: CategoryId;
  title: string;
  /** Card-level summary — one sentence, no marketing adjectives. */
  summary: string;
  /** Expanded body — the "what this actually does" paragraph. */
  detail: string;
  /** Bulleted specifics revealed on expand. */
  bullets: string[];
  /** Extra search terms that are not in the visible copy. */
  keywords: string[];
  specs?: FeatureSpec[];
  screen?: ScreenKind;
  /** Marks the two entries that get a wider grid cell. */
  wide?: boolean;
}

/**
 * The capability catalogue.
 *
 * The landing page shows six representative cards, which is the right number
 * for someone deciding whether to keep scrolling and the wrong number for
 * someone deciding whether to buy. This is the complete list, written to be
 * searched rather than read top to bottom — hence the `keywords` field, which
 * carries the words people actually type ("stop loss", "drawdown", "CCXT")
 * even when the visible copy phrases it differently.
 */
export const FEATURES: FeatureEntry[] = [
  {
    id: "nexus-engine",
    icon: BrainCircuit,
    category: "intelligence",
    title: "Nexus Engine",
    summary:
      "Reads market structure, scores every setup, and acts only on the entries that clear its bar.",
    detail:
      "Every closed candle takes the same path: verified data, higher-timeframe context, the strategy's signal, the Decision Brain's quality score, sizing, the risk checks and a paper fill — or a written reason there was none. Every candle's decision report is kept, WAIT included.",
    bullets: [
      "Trend, structure and regime read on the timeframes each strategy declares",
      "A 0–100 quality score with the factors behind it, not an opaque number",
      "A written reason attached to every accept and every reject",
      "A decision report for every candle, WAIT included",
    ],
    keywords: ["ai", "model", "signal", "decision", "inference", "llm", "ensemble"],
    specs: [
      { label: "Stages", value: "8" },
      { label: "Evaluated on", value: "Every close" },
      { label: "Rationale", value: "Always" },
    ],
    screen: "decision",
    wide: true,
  },
  {
    id: "conviction-scoring",
    icon: Gauge,
    category: "intelligence",
    title: "Quality scoring",
    summary: "A single 0–100 score with the eight weighted factors that produced it.",
    detail:
      "Setups are not binary. The Decision Brain scores each one on eight weighted factors — higher-timeframe alignment, regime fit, momentum, reward to risk, stop safety, volatility, structure and volume — and blocks some outright, such as reward to risk below 1. The breakdown is kept with the decision, so a 62 can be understood rather than merely obeyed.",
    bullets: [
      "Eight weighted factors, each visible in the breakdown",
      "Minimum score configurable, 60 by default",
      "Rejected setups logged with the rules they failed",
    ],
    keywords: ["confidence", "score", "quality", "threshold", "selectivity", "conviction"],
    specs: [
      { label: "Range", value: "0–100" },
      { label: "Factors", value: "8" },
      { label: "Default minimum", value: "60" },
    ],
    screen: "score",
  },
  {
    id: "market-scanner",
    icon: ListFilter,
    category: "intelligence",
    title: "Market scanner",
    summary: "Ranks your watchlist by setup type, so attention goes where the setup is.",
    detail:
      "The scanner ranks the symbols you choose, on the timeframe you pick, by the setups it finds — breakouts, liquidity sweeps, high volume, strong momentum, trend continuations and pullbacks. Instead of watching twelve charts you look at one ordered list. It runs when you ask it to.",
    bullets: [
      "Any symbol list, on the timeframe you choose",
      "Ranked by breakout, sweep, volume, momentum, continuation and pullback setups",
      "Runs on demand",
    ],
    keywords: ["screener", "watchlist", "ranking", "symbols", "scan", "alerts"],
    screen: "scanner",
  },
  {
    id: "regime-detection",
    icon: Activity,
    category: "intelligence",
    title: "Regime detection",
    summary: "Classifies the market it is in before choosing how to behave in it.",
    detail:
      "A breakout strategy in a chopping range is not a strategy, it is a donation. The regime detector classifies the market as trending, ranging, or in low, high or extreme volatility. Regime fit is part of every quality score, and a choppy or unclear regime blocks trades that are not reversals.",
    bullets: [
      "Trending, ranging and volatility states",
      "Regime fit weighted into every quality score",
      "Choppy regimes block non-reversal trades",
    ],
    keywords: ["volatility", "trend", "range", "chop", "market state", "regime"],
  },
  {
    id: "risk-engine",
    icon: ShieldCheck,
    category: "risk",
    title: "Risk engine",
    summary: "A mandatory veto that every order passes through before it can exist.",
    detail:
      "Risk is not a setting the engine consults politely. Every order intent passes the risk checks after sizing — open positions, correlation, event blackouts, sessions, daily and weekly loss limits, cooldowns, exposure and the venue's lot rules — and an order that fails any of them is never created. If the global risk manager cannot be reached, nothing trades.",
    bullets: [
      "Twenty checks on the one path to an order",
      "Daily and weekly loss limits, exposure and correlation limits",
      "Every rejection logged with the rule that fired",
      "Fails closed: if a check cannot run, trading stops",
    ],
    keywords: ["stop loss", "drawdown", "position size", "limits", "veto", "exposure", "safety"],
    specs: [
      { label: "Checks", value: "20" },
      { label: "Posture", value: "Fail closed" },
      { label: "Bypass", value: "None" },
    ],
    screen: "risk",
    wide: true,
  },
  {
    id: "position-sizing",
    icon: SlidersHorizontal,
    category: "risk",
    title: "Position sizing",
    summary: "Size derived from the stop distance and account risk, never from a fixed lot.",
    detail:
      "Every position is sized so that the distance to its invalidation costs a fixed fraction of equity. A tight stop takes a larger position and a wide one takes a smaller position, so the loss is the same either way — which is what makes a run of losses survivable instead of compounding.",
    bullets: [
      "Risk-per-trade expressed in equity percent, not units",
      "Capped by per-position and total exposure limits",
      "Binance lot and notional rules applied before any order",
    ],
    keywords: ["sizing", "risk per trade", "leverage", "notional", "kelly", "lot size"],
  },
  {
    id: "drawdown-guard",
    icon: TrendingUp,
    category: "risk",
    title: "Drawdown guard",
    summary: "Halts new entries when the account is already down — never the exits.",
    detail:
      "A drawdown circuit breaker halts new entries until someone resumes them, and a consecutive-loss stop does the same. Optional daily and weekly loss limits block entries until the day or week turns over. Open positions keep being managed to their exits through any halt, and the halt carries the reason that triggered it, so resuming is a decision rather than a drift.",
    bullets: [
      "Drawdown circuit breaker and consecutive-loss stop",
      "Optional daily and weekly loss limits",
      "Exits never blocked; a halt waits for a manual resume",
    ],
    keywords: ["daily loss", "circuit breaker", "halt", "kill switch", "drawdown", "budget"],
  },
  {
    id: "strategy-lab",
    icon: FlaskConical,
    category: "research",
    title: "Nexus Strategy Lab",
    summary: "Backtest and optimise against historical data before a dollar is at risk.",
    detail:
      "The Lab runs a strategy over historical data with the same Decision Brain gate and the same fill costs the paper engine uses, so the result is a rehearsal rather than a simulation of a different program. Walk-forward and out-of-sample results sit side by side, because a result on the data a strategy was tuned on is not evidence.",
    bullets: [
      "Same quality gate and costs as paper trading",
      "Parameter sweeps in the Optimization Lab",
      "Walk-forward windows with out-of-sample results",
      "A fee on every simulated fill; spread and slippage on market and stop orders",
    ],
    keywords: ["backtest", "optimisation", "walk forward", "historical", "sweep", "research"],
    specs: [
      { label: "Data", value: "Historical" },
      { label: "Costs", value: "Modelled" },
      { label: "Mode", value: "Walk-forward" },
    ],
    screen: "equity",
  },
  {
    id: "paper-trading",
    icon: Repeat,
    category: "research",
    title: "Paper trading",
    summary: "Runs live data through the full path with simulated fills.",
    detail:
      "Paper mode consumes the live Binance feed and produces real decisions, orders and journal entries — only the fills are simulated. It is the step between a backtest that looked good and capital that is actually exposed, and today it is how every strategy runs: live order routing is locked.",
    bullets: [
      "Live feed, simulated fills with modelled costs",
      "The same journal and analytics as every instance",
      "Live routing locked until a venue passes safety review",
    ],
    keywords: ["paper", "simulation", "demo", "dry run", "sandbox", "testnet"],
  },
  {
    id: "strategy-versioning",
    icon: GitBranch,
    category: "research",
    title: "Strategy versioning",
    summary: "Every strategy result carries the version that produced it.",
    detail:
      "Built-in strategies carry immutable versions pinned by a signal fingerprint: changing how one behaves means a new version, never an edit in place. Custom strategies keep version snapshots, and every paper record stores the strategy version that produced it — so a result can always be traced to the exact rules behind it.",
    bullets: [
      "Immutable, fingerprinted versions for built-in strategies",
      "Version snapshots for custom strategies",
      "Every paper record stamped with its strategy version",
    ],
    keywords: ["version", "history", "changelog", "rollback", "parameters", "git"],
  },
  {
    id: "smart-execution",
    icon: Timer,
    category: "execution",
    title: "Paper execution",
    summary: "Fills simulated against the live Binance price, with costs included.",
    detail:
      "Approved orders fill on the paper broker against the live Binance price. A limit entry fills at its price only when the market trades through it, and expires unfilled rather than being chased; market and stop orders pay modelled spread and slippage; every fill pays a fee. Live order routing is locked.",
    bullets: [
      "Limit entries fill at their price, with a 0.02% maker fee",
      "Market and stop orders pay spread, slippage and a 0.04% taker fee",
      "Unfilled limit entries expire instead of being chased",
      "Live order routing locked",
    ],
    keywords: ["orders", "slippage", "routing", "limit", "market", "fills", "execution"],
    specs: [
      { label: "Entries", value: "Limit or market" },
      { label: "Costs", value: "Every fill" },
    ],
    screen: "fill",
  },
  {
    id: "exchange-support",
    icon: Building2,
    category: "execution",
    title: "Exchange connectivity",
    summary: "One internal interface for every venue. Live Binance market data today.",
    detail:
      "Venue specifics — symbol formats and lot and notional rules — are handled where data and orders enter, so strategy and risk logic stay the same. Binance USDⓈ-M is the live market-data venue today; more venues are on the roadmap.",
    bullets: [
      "Binance USDⓈ-M live market data; Bybit, OKX and Hyperliquid on the roadmap",
      "Binance lot and notional rules applied to every order",
      "One order path for every instance",
    ],
    keywords: ["binance", "bybit", "okx", "hyperliquid", "ccxt", "venue", "broker", "api"],
  },
  {
    id: "positions",
    icon: Wallet,
    category: "execution",
    title: "Position management",
    summary: "Stops, targets and optional trade management, handled by the engine.",
    detail:
      "Every position opens with a stop and a target, and the engine manages both. Break-even moves, scale-outs and trailing stops are available but switched off by default, because out-of-sample testing did not support them. A stop or target changed by hand on the chart is applied safely alongside the engine.",
    bullets: [
      "Stop and target set on every entry",
      "Break-even, scale-out and trailing available, off by default",
      "Manual stop and target adjustment from the chart",
    ],
    keywords: ["stop", "take profit", "trailing", "positions", "exit", "partial", "breakeven"],
  },
  {
    id: "trading-memory",
    icon: NotebookText,
    category: "memory",
    title: "Trading memory",
    summary: "Every closed trade becomes permanent, searchable memory.",
    detail:
      "A trade does not end at the exit. The outcome, the conditions it was taken in and a reflection on what went right or wrong are stored together, searchable in plain language and by similarity — so you can find every trade like the one in front of you.",
    bullets: [
      "Outcome, context and reflection stored per trade",
      "Similar-trade search across your record",
      "Searchable by symbol, outcome or a plain-language question",
    ],
    keywords: ["journal", "memory", "learning", "lessons", "review", "history", "notes"],
    screen: "memory",
  },
  {
    id: "coaching",
    icon: Sparkles,
    category: "memory",
    title: "Coaching notes",
    summary: "Turns your trade record into a mentor-style review.",
    detail:
      "The coach reviews a set of trades and explains why they won or lost, the recurring mistakes, the conditions to avoid and what to do next — with a breakdown of which session, regime, setup and side made or lost money.",
    bullets: [
      "Why trades won or lost, in plain language",
      "Recurring mistakes and conditions to avoid",
      "Breakdown by session, regime, setup and side",
    ],
    keywords: ["coach", "feedback", "improve", "habits", "psychology", "mistakes"],
  },
  {
    id: "analytics",
    icon: LineChart,
    category: "memory",
    title: "Performance analytics",
    summary: "Expectancy, win rate and drawdown, broken down by strategy, symbol and session.",
    detail:
      "A single equity curve tells you that something is working. The breakdown tells you what. The dashboard's analytics split results by strategy, symbol and session, so an overall profit that is entirely one symbol in one session is visible as exactly that.",
    bullets: [
      "Expectancy, win rate, profit factor and drawdown",
      "Breakdown by strategy, symbol and session",
      "Fees already inside every paper result",
    ],
    keywords: ["stats", "pnl", "sharpe", "expectancy", "reporting", "metrics", "attribution"],
    screen: "analytics",
  },
  {
    id: "intelligence-feed",
    icon: Radio,
    category: "operations",
    title: "Intelligence feed",
    summary: "A live, timestamped stream of everything the system is doing and why.",
    detail:
      "The feed is the running narration of the engine: evaluations, decisions, rejections, orders and fills in order, with the reasoning attached. It is what turns an automated system from something you hope is working into something you can watch working.",
    bullets: [
      "Decision, rejection, order and fill events with reasons",
      "A per-instance event log and lifecycle timeline",
      "Every candle's decision report kept",
    ],
    keywords: ["logs", "live", "stream", "events", "monitoring", "feed", "activity"],
    screen: "feed",
  },
  {
    id: "scheduler",
    icon: CalendarClock,
    category: "operations",
    title: "Scheduler",
    summary: "Trading windows, event blackouts and session rules enforced automatically.",
    detail:
      "Not every hour is worth trading. Session hours and allowed weekdays decide when new entries may open, and the event guard blocks new entries around high-impact macro releases — so time-based discipline does not depend on someone being awake.",
    bullets: [
      "Session hours and allowed trading days",
      "Event blackouts around high-impact releases",
      "Open positions still managed outside the window",
    ],
    keywords: ["schedule", "cron", "sessions", "hours", "blackout", "news", "timing"],
  },
  {
    id: "api-keys",
    icon: KeyRound,
    category: "operations",
    title: "Key custody",
    summary: "Exchange keys stored envelope-encrypted, with withdrawals structurally impossible.",
    detail:
      "Keys are encrypted with AES-256-GCM under per-tenant data keys, which are wrapped by a master key kept only in the server's environment. They are decrypted only in memory when needed, and never logged or returned by any API. Before a key is stored, Binance is asked what it may do, and a key that can withdraw or transfer funds is refused, so the worst case of a compromise is unwanted trading, not a drained account.",
    bullets: [
      "Envelope encryption with per-tenant data keys",
      "Withdrawal- and transfer-enabled keys refused before storage",
      "Missing IP restriction flagged on every key",
      "Replace, re-check or revoke a key in one step",
    ],
    keywords: ["security", "keys", "encryption", "custody", "permissions", "withdrawal", "vault"],
  },
  {
    id: "audit-log",
    icon: ScrollText,
    category: "operations",
    title: "Audit log",
    summary: "Append-only record of every action, by whom, from where.",
    detail:
      "Configuration changes, key rotations, manual overrides, halts and resumes are written to an append-only log with actor, source address and prior value. Nothing in the product can delete or amend an entry, which is what makes the log worth consulting after an incident.",
    bullets: [
      "Append-only, with no product-level delete path",
      "Actor, source address and before/after values",
      "Exportable for external retention",
    ],
    keywords: ["audit", "compliance", "log", "trail", "accountability", "siem"],
  },
  {
    id: "data-pipeline",
    icon: Database,
    category: "operations",
    title: "Market data pipeline",
    summary: "Closed candles, checked for gaps and duplicates before anything uses them.",
    detail:
      "Candles arrive from Binance's public websocket with REST history for warm-up. Duplicate, out-of-order and missing candles are detected, a stale feed pauses new entries, and after a restart missed candles are replayed in order before trading resumes — so a strategy never silently trades a hole in its own history.",
    bullets: [
      "Closed candles only; the forming candle is display-only",
      "Gap, duplicate and out-of-order detection",
      "Missed candles replayed in order after a restart",
    ],
    keywords: ["data", "candles", "ohlcv", "feed", "websocket", "backfill", "timeframe"],
  },
  {
    id: "reporting",
    icon: ClipboardList,
    category: "operations",
    title: "Scheduled reports",
    summary: "A daily report of what happened, delivered to Telegram.",
    detail:
      "A digest of the day's decisions and results, delivered on schedule to Telegram, with alerts available on Discord and email. It is built from the same data the dashboard shows, so the report and the product agree.",
    bullets: [
      "Daily report to Telegram",
      "Alerts to Discord, email or Telegram",
      "Built from the same data as the dashboard",
    ],
    keywords: ["reports", "email", "digest", "discord", "webhook", "notifications", "summary"],
  },
];

/** Count per category, used by the rail. */
export function countsByCategory(): Record<CategoryId, number> {
  return FEATURES.reduce(
    (acc, f) => {
      acc[f.category] += 1;
      return acc;
    },
    {
      intelligence: 0,
      risk: 0,
      research: 0,
      execution: 0,
      memory: 0,
      operations: 0,
    } as Record<CategoryId, number>,
  );
}

/**
 * Free-text match across everything a person might reasonably type.
 *
 * Deliberately not fuzzy: a search that returns "Scheduler" for "stop loss"
 * because two letters happen to line up is worse than returning nothing, since
 * the reader then has to evaluate results instead of trusting them.
 */
export function matches(f: FeatureEntry, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const terms = q.split(/\s+/);
  const haystack = [
    f.title,
    f.summary,
    f.detail,
    f.category,
    ...f.bullets,
    ...f.keywords,
    ...(f.specs?.map((s) => `${s.label} ${s.value}`) ?? []),
  ]
    .join(" ")
    .toLowerCase();
  return terms.every((t) => haystack.includes(t));
}
