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
      "The engine runs the same eight stages on every candle close: normalise the feed, extract structure, build a feature vector, run the model ensemble, arbitrate a decision, size it, check it against the risk envelope, and either route it or write down why it did not. Every one of those stages is recorded, so a decision can be replayed months later with the exact inputs it saw.",
    bullets: [
      "Structure extraction: trend state, ranges, liquidity pockets, session context",
      "Ensemble scoring with per-model attribution, not a single opaque number",
      "A written rationale attached to every accept and every reject",
      "Deterministic replay from the stored feature vector",
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
    title: "Conviction scoring",
    summary: "A single 0–100 score with the nine checks that produced it shown alongside.",
    detail:
      "Setups are not binary. Each candidate is scored against nine weighted qualifications — trend agreement, structure quality, volatility regime, liquidity, session, correlation load, news proximity, historical analogue performance and risk headroom. The score is the weighted result, and the breakdown is always visible, so a 62 can be understood rather than merely obeyed.",
    bullets: [
      "Nine weighted checks, each independently inspectable",
      "Threshold is configurable per strategy and per symbol",
      "Near-miss setups are logged with the check that failed them",
    ],
    keywords: ["confidence", "score", "quality", "threshold", "selectivity", "conviction"],
    specs: [
      { label: "Range", value: "0–100" },
      { label: "Checks", value: "9" },
      { label: "Default bar", value: "72" },
    ],
    screen: "score",
  },
  {
    id: "market-scanner",
    icon: ListFilter,
    category: "intelligence",
    title: "Market scanner",
    summary: "Continuously ranks your whole watchlist so attention goes where the setup is.",
    detail:
      "The scanner keeps a live ranking across every symbol you follow, updated on each close. Instead of watching twelve charts, you watch one ordered list — and the ordering is the same conviction score the engine trades on, so what floats to the top is what it is closest to acting on.",
    bullets: [
      "Watchlists of any size, ranked on the live score",
      "Per-symbol regime tag: trending, ranging, expanding, compressed",
      "Alerting on rank crossings, not just price levels",
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
      "A breakout strategy in a chopping range is not a strategy, it is a donation. Regime detection classifies volatility state and directional persistence on multiple horizons, and strategies declare which regimes they are allowed to operate in. Outside those, they stand down rather than degrade.",
    bullets: [
      "Volatility state across three horizons",
      "Directional persistence and mean-reversion pressure",
      "Per-strategy regime allowlists enforced at decision time",
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
      "Risk is not a setting the engine consults politely — it is a separate service with veto power, and an order that fails any of its thirteen responsibilities is never created. It sits after sizing and before routing, which means there is no code path from a model output to an exchange that skips it.",
    bullets: [
      "Thirteen enforced responsibilities, each independently testable",
      "Daily loss cap, exposure ceiling and correlation limits",
      "Vetoes are logged with the specific rule that fired",
      "Fails closed: an unavailable risk service blocks trading",
    ],
    keywords: ["stop loss", "drawdown", "position size", "limits", "veto", "exposure", "safety"],
    specs: [
      { label: "Responsibilities", value: "13" },
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
      "Automatic reduction as open exposure accumulates",
      "Exchange lot and notional constraints respected exactly",
    ],
    keywords: ["sizing", "risk per trade", "leverage", "notional", "kelly", "lot size"],
  },
  {
    id: "drawdown-guard",
    icon: TrendingUp,
    category: "risk",
    title: "Drawdown guard",
    summary: "Halts new entries when the day, the week or the strategy is already down.",
    detail:
      "Three independent budgets — daily, weekly and per-strategy — each with its own ceiling. Breaching one stops new entries under it while leaving open positions managed to their existing exits, and the halt is recorded with the number that triggered it so the resumption is a decision rather than a drift.",
    bullets: [
      "Daily, weekly and per-strategy loss budgets",
      "Open positions continue to be managed after a halt",
      "Manual resume, with the breach recorded in the audit log",
    ],
    keywords: ["daily loss", "circuit breaker", "halt", "kill switch", "drawdown", "budget"],
  },
  {
    id: "strategy-lab",
    icon: FlaskConical,
    category: "research",
    title: "Nexus Strategy Lab",
    summary: "Backtest and optimise against years of history before a dollar is at risk.",
    detail:
      "The Lab runs a strategy over historical data with the same engine, the same risk service and the same execution model the live system uses, so the result is a rehearsal rather than a simulation of a different program. Parameter sweeps report the whole surface, not just the best cell, because a peak surrounded by cliffs is an overfit and should look like one.",
    bullets: [
      "Same engine and risk path as live — no research-only shortcuts",
      "Parameter sweeps rendered as a surface, not a leaderboard",
      "Walk-forward windows with out-of-sample segregation",
      "Fees, funding and slippage modelled per venue",
    ],
    keywords: ["backtest", "optimisation", "walk forward", "historical", "sweep", "research"],
    specs: [
      { label: "History", value: "Multi-year" },
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
      "Paper mode consumes the same live feed and produces the same decisions, orders and journal entries as production — only the venue is simulated. It is the step between a backtest that looked good and capital that is actually exposed, and it is the default for a newly promoted strategy.",
    bullets: [
      "Live feed, simulated fills with modelled slippage",
      "Identical journal and analytics output to live",
      "Promotion to live is a flag, not a rewrite",
    ],
    keywords: ["paper", "simulation", "demo", "dry run", "sandbox", "testnet"],
  },
  {
    id: "strategy-versioning",
    icon: GitBranch,
    category: "research",
    title: "Strategy versioning",
    summary: "Every parameter change is a version with its own performance record.",
    detail:
      "Strategies are versioned like code. Changing a threshold creates a new version rather than mutating the old one, and performance is attributed to the version that produced it — so a strategy that looks profitable overall cannot hide the fact that the current parameters have never made money.",
    bullets: [
      "Immutable versions with a derived change log",
      "Performance attributed per version, not per strategy",
      "Rollback to any prior version without losing its history",
    ],
    keywords: ["version", "history", "changelog", "rollback", "parameters", "git"],
  },
  {
    id: "smart-execution",
    icon: Timer,
    category: "execution",
    title: "Smart execution",
    summary: "Chooses order type and pacing from live book conditions, then reports the slippage.",
    detail:
      "An order is not simply sent. The router reads spread, depth and recent volatility to choose between passive and aggressive placement, splits size when the book is thin, and measures realised slippage against the decision price on every fill — which is the number that tells you whether the edge survived contact with the market.",
    bullets: [
      "Passive, aggressive and split placement chosen per order",
      "Depth-aware sizing against the visible book",
      "Realised slippage measured against decision price",
      "Retry and reconciliation on partial fills",
    ],
    keywords: ["orders", "slippage", "routing", "limit", "market", "fills", "execution"],
    specs: [
      { label: "Placement", value: "Adaptive" },
      { label: "Measured", value: "Per fill" },
    ],
    screen: "book",
  },
  {
    id: "exchange-support",
    icon: Building2,
    category: "execution",
    title: "Exchange connectivity",
    summary: "One internal interface for every venue. Live Binance market data today.",
    detail:
      "Venue differences — symbol formats, precision rules, rate limits, funding conventions — are absorbed at the adapter boundary, so strategies are written once and the same code runs anywhere. Adding a venue does not touch strategy or risk logic.",
    bullets: [
      "Binance USDⓈ-M live market data; Bybit, OKX and Hyperliquid on the roadmap",
      "Per-venue precision, lot and rate-limit rules encoded",
      "One internal order interface across all adapters",
    ],
    keywords: ["binance", "bybit", "okx", "hyperliquid", "ccxt", "venue", "broker", "api"],
  },
  {
    id: "positions",
    icon: Wallet,
    category: "execution",
    title: "Position management",
    summary: "Stops, targets and trailing logic maintained on the venue, not in a script.",
    detail:
      "Protective orders are placed at the exchange the moment a position opens, so a lost connection does not become an unprotected position. Trailing logic updates them as structure moves, and every amendment is journalled with the reason it was made.",
    bullets: [
      "Exchange-resident stop and target orders",
      "Structure-aware trailing, not fixed-percentage",
      "Partial exits at defined R multiples",
      "Every amendment journalled with its trigger",
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
      "A trade does not end at the exit. The outcome, the conditions it was taken in, what went wrong and the lesson drawn are stored together and consulted when a similar setup appears — which is how the system stops repeating a mistake it has already paid for.",
    bullets: [
      "Outcome, context, mistake and lesson stored per trade",
      "Similar-setup recall at decision time",
      "Searchable by symbol, regime, outcome or lesson",
    ],
    keywords: ["journal", "memory", "learning", "lessons", "review", "history", "notes"],
    screen: "memory",
  },
  {
    id: "coaching",
    icon: Sparkles,
    category: "memory",
    title: "Coaching notes",
    summary: "Turns recurring patterns in your record into specific, actionable corrections.",
    detail:
      "Across enough trades, mistakes stop being individual and start being habits. Coaching identifies the recurring ones — entering late in a specific regime, holding past invalidation on a specific symbol — and states the correction in terms of the rule that would have prevented it.",
    bullets: [
      "Pattern detection across the full trade record",
      "Corrections phrased as rules, not encouragement",
      "Dismissable, with dismissals themselves tracked",
    ],
    keywords: ["coach", "feedback", "improve", "habits", "psychology", "mistakes"],
  },
  {
    id: "analytics",
    icon: LineChart,
    category: "memory",
    title: "Performance analytics",
    summary: "Attribution by strategy, symbol, regime, session and hour of day.",
    detail:
      "A single equity curve tells you that something is working. Attribution tells you what. Results are decomposed across every dimension the system records, so an overall profit that is entirely one symbol in one session is visible as exactly that.",
    bullets: [
      "Expectancy, hit rate and R-multiple distribution",
      "Attribution across strategy, symbol, regime, session, hour",
      "Cost drag broken out from gross performance",
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
      "The feed is the running narration of the engine: every evaluation, decision, veto, order and fill in order, with the reasoning attached. It is what turns an automated system from something you hope is working into something you can watch working.",
    bullets: [
      "Live evaluation, decision, veto, order and fill events",
      "Filterable by severity, strategy or symbol",
      "Replayable from any point in the retained window",
    ],
    keywords: ["logs", "live", "stream", "events", "monitoring", "feed", "activity"],
    screen: "feed",
  },
  {
    id: "scheduler",
    icon: CalendarClock,
    category: "operations",
    title: "Scheduler",
    summary: "Trading windows, blackout periods and session rules enforced automatically.",
    detail:
      "Not every hour is worth trading. The scheduler encodes which sessions a strategy runs in, which windows are blacked out around scheduled events, and when the system flattens rather than carries — so time-based discipline does not depend on someone being awake.",
    bullets: [
      "Per-strategy session and weekday windows",
      "Event blackout windows with configurable padding",
      "Scheduled flatten-and-stand-down",
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
      "Keys are encrypted with per-tenant data keys under a managed master key, decrypted only inside the execution service, and never logged or returned by any API. Connections are rejected outright if the key carries withdrawal permission, so the worst case of a compromise is unwanted trading, not a drained account.",
    bullets: [
      "Envelope encryption with per-tenant data keys",
      "Withdrawal-enabled keys rejected at connection time",
      "IP allowlisting on every venue that supports it",
      "Rotation without downtime",
    ],
    keywords: ["security", "keys", "encryption", "custody", "kms", "permissions", "withdrawal"],
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
    summary: "Normalised candles and book snapshots with gap detection built in.",
    detail:
      "Feeds are normalised into one internal representation, checked for gaps and duplicate candles, and backfilled from a secondary source when a venue drops data. A strategy therefore never silently trades a hole in its own history.",
    bullets: [
      "Multi-venue normalisation into one candle format",
      "Gap and duplicate detection with automatic backfill",
      "Timeframe requests honoured exactly as asked",
    ],
    keywords: ["data", "candles", "ohlcv", "feed", "websocket", "backfill", "timeframe"],
  },
  {
    id: "reporting",
    icon: ClipboardList,
    category: "operations",
    title: "Scheduled reports",
    summary: "Daily and weekly digests of what happened, delivered where you read.",
    detail:
      "A digest of the period's decisions, fills, vetoes and attribution, delivered on a schedule you set. It is deliberately the same data the dashboard shows — a report that reconciles with the product is a report you can act on.",
    bullets: [
      "Daily and weekly digests with attribution",
      "Email, Discord and webhook delivery",
      "Reconciles exactly with in-product analytics",
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
