import { motion, useReducedMotion } from "framer-motion";
import {
  BarChart3,
  Building2,
  BrainCircuit,
  NotebookPen,
  ScanLine,
  Send,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";

export interface Stage {
  id: string;
  n: string;
  label: string;
  icon: LucideIcon;
  /** Hex accent — the page's ambient tint follows this as you scroll. */
  color: string;
  /** Tailwind text class for the same colour (Tailwind cannot read the hex). */
  textClass: string;
  borderClass: string;
  headline: string;
  body: string;
  /** Three short facts shown as a strip under the copy. */
  facts: [string, string][];
}

export const STAGES: Stage[] = [
  {
    id: "exchange",
    n: "01",
    label: "Exchange",
    icon: Building2,
    color: "#3E7BD6",
    textClass: "text-signal-soft",
    borderClass: "border-signal/40",
    headline: "It starts with a connection you control",
    body: "Nexus connects to Binance USDⓈ-M for live market data today, with more venues on the roadmap. Candles arrive over Binance's public websocket, with REST history to warm indicators up, and only closed candles are used. Duplicate, out-of-order and missing candles are detected, a stale feed pauses new entries rather than trading on a gap, and after a restart missed candles are replayed in order before trading resumes.",
    facts: [
      ["Live venue", "Binance · more on the roadmap"],
      ["Key scope", "Trade only, never withdraw"],
      ["Feed", "Closed candles · gap-checked"],
    ],
  },
  {
    id: "analysis",
    n: "02",
    label: "Analysis",
    icon: ScanLine,
    color: "#2E7BFF",
    textClass: "text-electric-soft",
    borderClass: "border-electric/40",
    headline: "Prices become a description of the market",
    body: "Every close, the strategy reads what the chart is doing rather than what it costs: trend on each timeframe it declares, swing structure, support and resistance, and the market regime. This is the layer that lets a strategy say “only in a trend” and have that mean something enforceable.",
    facts: [
      ["Extracted", "Trend · structure · S/R"],
      ["Timeframes", "As declared per strategy"],
      ["Regime", "Trend · range · volatility"],
    ],
  },
  {
    id: "ai",
    n: "03",
    label: "AI",
    icon: BrainCircuit,
    color: "#22D3EE",
    textClass: "text-aqua-soft",
    borderClass: "border-aqua/40",
    headline: "The Decision Brain scores it",
    body: "The strategy decides where a trade could be; the Decision Brain decides whether it is worth taking. It scores the setup out of 100 from eight weighted factors — higher-timeframe alignment, regime fit, momentum, reward to risk, stop safety, volatility, structure and volume — keeps the rules it passed and failed, and blocks some setups outright, such as reward to risk below 1.",
    facts: [
      ["Factors", "8 · weighted to 100"],
      ["Minimum", "60 of 100, configurable"],
      ["Output", "Score + passed and failed rules"],
    ],
  },
  {
    id: "risk",
    n: "04",
    label: "Risk",
    icon: ShieldCheck,
    color: "#2FBF71",
    textClass: "text-emerald-soft",
    borderClass: "border-emerald/40",
    headline: "Then it has to get past risk",
    body: "Every order intent passes the risk checks: open-position limit, no pyramiding, correlation, event blackouts, trading day and session, daily and weekly loss limits, cooldown after a loss, trades per day, stop validity, sizing, exposure and the venue's lot rules, then the global risk manager. Any one failing rejects the order with a written reason, and if a check cannot run, nothing trades.",
    facts: [
      ["Checks", "20 · all mandatory"],
      ["On failure", "Fails closed"],
      ["Result", "Accepted · or a reason"],
    ],
  },
  {
    id: "execution",
    n: "05",
    label: "Execution",
    icon: Send,
    color: "#C9A24B",
    textClass: "text-gold-soft",
    borderClass: "border-gold/40",
    headline: "The order fills on paper, at the live price",
    body: "Approved orders fill on the paper broker against the live Binance price. A limit entry fills only when price trades through it, and expires unfilled after a set number of candles rather than being chased; market and stop orders pay modelled spread and slippage, and every fill pays a fee. Stops and targets are managed by the engine rather than held at an exchange, and live order routing is locked.",
    facts: [
      ["Broker", "Paper"],
      ["Costs", "Spread · slippage · fees"],
      ["Live routing", "Locked"],
    ],
  },
  {
    id: "journal",
    n: "06",
    label: "Journal",
    icon: NotebookPen,
    color: "#E7CE86",
    textClass: "text-gold-soft",
    borderClass: "border-gold/30",
    headline: "Every outcome is written down and kept",
    body: "The trade closes and is kept: the decision that opened it, the reasoning at the time, the outcome and a review of what went right or wrong. Rejections are recorded too — the record of what the system nearly did is often more instructive than the record of what it did.",
    facts: [
      ["Stored", "Context · reasoning · outcome"],
      ["Rejections", "Recorded as decisions"],
      ["Where", "Journal · dashboard"],
    ],
  },
  {
    id: "analytics",
    n: "07",
    label: "Analytics",
    icon: BarChart3,
    color: "#6EA3EC",
    textClass: "text-signal-soft",
    borderClass: "border-signal/40",
    headline: "And the record tells you what is actually working",
    body: "The dashboard's analytics break results down by strategy, symbol and session, with fees already inside every paper fill. An equity curve tells you whether something is working; the breakdown tells you what — and often that one symbol in one session is carrying the rest.",
    facts: [
      ["Breakdown", "Strategy · symbol · session"],
      ["Costs", "Inside every fill"],
      ["Source", "Your paper trades"],
    ],
  },
];

/**
 * Per-stage workflow diagrams.
 *
 * Each stage gets a *different* drawing rather than the same box with a
 * different label, because the claim of the page is that seven distinct things
 * happen — and seven identical illustrations would quietly argue the opposite.
 * They animate on `active` so only the visible one is doing work.
 */
export function StageVisual({ stage, active }: { stage: Stage; active: boolean }) {
  const reduced = useReducedMotion() ?? false;
  const play = active && !reduced;

  return (
    <div className="relative flex aspect-[4/3] w-full items-center justify-center overflow-hidden rounded-3xl border border-white/[0.07] bg-black/40 backdrop-blur-sm">
      {/* stage-tinted wash */}
      <div
        className="pointer-events-none absolute inset-0 opacity-60 transition-opacity duration-700"
        style={{
          background: `radial-gradient(70% 60% at 50% 20%, ${stage.color}22, transparent 70%)`,
        }}
      />

      {stage.id === "exchange" && <ExchangeViz color={stage.color} play={play} />}
      {stage.id === "analysis" && <AnalysisViz color={stage.color} play={play} />}
      {stage.id === "ai" && <AiViz color={stage.color} play={play} />}
      {stage.id === "risk" && <RiskViz color={stage.color} play={play} />}
      {stage.id === "execution" && <ExecutionViz color={stage.color} play={play} />}
      {stage.id === "journal" && <JournalViz color={stage.color} play={play} />}
      {stage.id === "analytics" && <AnalyticsViz color={stage.color} play={play} />}

      <span className="absolute bottom-3 left-4 font-mono text-[10px] uppercase tracking-[0.2em] text-white/20">
        {stage.n} · {stage.label}
      </span>
    </div>
  );
}

/* ── Individual diagrams ─────────────────────────────────────────────── */

function ExchangeViz({ color, play }: { color: string; play: boolean }) {
  // Only Binance streams today; the other two are drawn as roadmap —
  // dashed, dimmed, and with no data flowing along their line.
  const venues = [{ name: "Binance", live: true }, { name: "Bybit", live: false }, { name: "OKX", live: false }];
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      {venues.map(({ name: v, live }, i) => {
        const y = 60 + i * 60;
        return (
          <g key={v} opacity={live ? 1 : 0.45}>
            <rect x="18" y={y - 15} width="86" height="30" rx="7" fill="#0E1219" stroke={color}
                  strokeOpacity="0.5" strokeDasharray={live ? undefined : "3 3"} />
            <text x="61" y={live ? y + 4 : y} textAnchor="middle" fill="#9FB0C4" style={{ fontSize: 10 }}>
              {v}
            </text>
            {!live && (
              <text x="61" y={y + 10} textAnchor="middle" fill="#9FB0C4" className="font-mono" style={{ fontSize: 6.5 }}>
                roadmap
              </text>
            )}
            <path d={`M104 ${y} C 148 ${y}, 148 120, 196 120`} fill="none" stroke={color} strokeOpacity="0.3"
                  strokeWidth="1.2" strokeDasharray={live ? undefined : "3 4"} />
            {play && live && (
              <circle r="2.6" fill={color}>
                <animateMotion
                  dur={`${2.2 + i * 0.4}s`}
                  repeatCount="indefinite"
                  path={`M104 ${y} C 148 ${y}, 148 120, 196 120`}
                />
              </circle>
            )}
          </g>
        );
      })}
      <rect x="196" y="94" width="106" height="52" rx="10" fill="#0A0D11" stroke={color} strokeOpacity="0.8" />
      <text x="249" y="115" textAnchor="middle" fill="#E9EEF3" style={{ fontSize: 11, fontWeight: 600 }}>
        Normalised
      </text>
      <text x="249" y="131" textAnchor="middle" fill={color} className="font-mono" style={{ fontSize: 8.5 }}>
        one candle format
      </text>
    </svg>
  );
}

function AnalysisViz({ color, play }: { color: string; play: boolean }) {
  const bars = [40, 62, 55, 78, 70, 96, 88, 110, 100, 128, 118, 140];
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      {/* range box */}
      <motion.rect
        x="30" y="70" width="260" height="52" rx="4"
        fill={`${color}10`} stroke={color} strokeOpacity="0.4" strokeDasharray="4 4"
        initial={{ opacity: 0 }} animate={{ opacity: play ? 1 : 0.35 }} transition={{ duration: 0.6, delay: 0.5 }}
      />
      {bars.map((b, i) => {
        const x = 34 + i * 21;
        const up = i % 3 !== 1;
        return (
          <g key={i}>
            <line x1={x} x2={x} y1={200 - b - 14} y2={200 - b + 16} stroke={up ? "#2FBF71" : "#E5605B"} strokeWidth="1" opacity="0.8" />
            <rect x={x - 4} y={200 - b - 6} width="8" height="16" fill={up ? "#2FBF71" : "#E5605B"} opacity="0.85" rx="1" />
          </g>
        );
      })}
      {/* trend line drawn in */}
      <motion.path
        d="M34 168 L286 60"
        fill="none" stroke={color} strokeWidth="1.6"
        initial={{ pathLength: 0 }} whileInView={{ pathLength: 1 }} viewport={{ once: true }}
        transition={{ duration: 1.2, ease: [0.22, 1, 0.36, 1] }}
      />
      <text x="30" y="62" fill={color} className="font-mono" style={{ fontSize: 8.5 }}>range · prior high</text>
      <text x="196" y="52" fill={color} className="font-mono" style={{ fontSize: 8.5 }}>trend · 4h</text>
    </svg>
  );
}

function AiViz({ color, play }: { color: string; play: boolean }) {
  const nodes = [
    { y: 62, label: "trend" },
    { y: 120, label: "reward:risk" },
    { y: 178, label: "regime" },
  ];
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      {nodes.map((n, i) => (
        <g key={n.label}>
          <circle cx="66" cy={n.y} r="20" fill="#0A0D11" stroke={color} strokeOpacity="0.55" />
          <text x="66" y={n.y + 3} textAnchor="middle" fill="#9FB0C4" style={{ fontSize: 7.5 }}>
            {n.label}
          </text>
          <path d={`M88 ${n.y} C 130 ${n.y}, 140 120, 176 120`} fill="none" stroke={color} strokeOpacity={0.25 + i * 0.12} strokeWidth={0.9 + i * 0.5} />
          {play && (
            <circle r="2.4" fill={color}>
              <animateMotion dur={`${1.8 + i * 0.35}s`} repeatCount="indefinite" path={`M88 ${n.y} C 130 ${n.y}, 140 120, 176 120`} />
            </circle>
          )}
        </g>
      ))}
      <circle cx="206" cy="120" r="32" fill="#0A0D11" stroke={color} strokeWidth="1.6" />
      <motion.circle
        cx="206" cy="120" r="32" fill="none" stroke={color} strokeWidth="1.6"
        initial={{ opacity: 0.5, scale: 1 }}
        animate={play ? { opacity: [0.5, 0, 0.5], scale: [1, 1.35, 1] } : {}}
        transition={{ duration: 2.4, repeat: Infinity }}
        style={{ transformOrigin: "206px 120px" }}
      />
      <text x="206" y="118" textAnchor="middle" fill="#fff" style={{ fontSize: 17, fontWeight: 700 }}>84</text>
      <text x="206" y="132" textAnchor="middle" fill={color} className="font-mono" style={{ fontSize: 7 }}>quality</text>
      <text x="266" y="124" textAnchor="middle" fill="#4FD98E" className="font-mono" style={{ fontSize: 9 }}>▸ ≥ 60</text>
    </svg>
  );
}

function RiskViz({ color, play }: { color: string; play: boolean }) {
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      <text x="24" y="42" fill="#9FB0C4" style={{ fontSize: 10 }}>order intent</text>
      {/* one slat per reject stage in services/signal_pipeline.py */}
      {Array.from({ length: 20 }).map((_, i) => {
        const x = 30 + i * 13;
        const blocked = i === 12;
        return (
          <motion.rect
            key={i}
            x={x} y="70" width="9" height="100" rx="2"
            fill={blocked ? "#E5605B" : color}
            initial={{ opacity: 0.25 }}
            animate={play ? { opacity: blocked ? [0.3, 1, 0.3] : [0.25, 0.7, 0.25] } : { opacity: 0.4 }}
            transition={{ duration: 2, repeat: Infinity, delay: i * 0.09 }}
          />
        );
      })}
      <text x="30" y="188" fill={color} className="font-mono" style={{ fontSize: 8.5 }}>20 checks · any one rejects</text>
      <text x="30" y="204" fill="#E5605B" className="font-mono" style={{ fontSize: 8.5 }}>1 veto · order never created</text>
      <path d="M24 50 L292 50" stroke={color} strokeOpacity="0.25" strokeDasharray="3 3" />
    </svg>
  );
}

function ExecutionViz({ color, play }: { color: string; play: boolean }) {
  // A resting limit entry between its stop and target. There is no depth
  // ladder here: the engine fills on the paper broker against the live price
  // and never reads an order book.
  const levels = [
    { y: 56, label: "target 69,590.0", fill: "#4FD98E" },
    { y: 184, label: "stop 67,820.0", fill: "#F07E7A" },
  ];
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      <text x="30" y="30" fill="#9FB0C4" style={{ fontSize: 9.5 }}>live quote · paper fill</text>
      {levels.map((l) => (
        <g key={l.label}>
          <path d={`M30 ${l.y} L290 ${l.y}`} stroke={l.fill} strokeOpacity="0.55" strokeDasharray="4 4" />
          <text x="290" y={l.y - 6} textAnchor="end" fill={l.fill} className="font-mono" style={{ fontSize: 8.5 }}>
            {l.label}
          </text>
        </g>
      ))}
      <motion.rect
        x="30" y="110" width="252" height="20" rx="4" fill={`${color}22`} stroke={color} strokeOpacity="0.7"
        animate={play ? { opacity: [0.55, 1, 0.55] } : {}} transition={{ duration: 1.8, repeat: Infinity }}
      />
      <text x="40" y="124" fill={color} className="font-mono" style={{ fontSize: 9 }}>limit fill 0.42 @ 68,408.0 · maker</text>
      <text x="30" y="214" fill="#9FB0C4" className="font-mono" style={{ fontSize: 8.5 }}>fee 0.02% · no spread or slippage at the limit</text>
    </svg>
  );
}

function JournalViz({ color, play }: { color: string; play: boolean }) {
  const cards = [
    { sym: "SOL/USDT", r: "+1.8R", ok: true },
    { sym: "ETH/USDT", r: "−1.0R", ok: false },
    { sym: "BTC/USDT", r: "+0.4R", ok: true },
  ];
  return (
    <div className="relative w-full max-w-[340px] space-y-2 px-6">
      {cards.map((c, i) => (
        <motion.div
          key={c.sym}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: play ? 1 : 0.6, y: 0 }}
          transition={{ duration: 0.5, delay: i * 0.12 }}
          className="rounded-xl border border-white/[0.08] bg-black/50 p-3"
          style={{ marginLeft: i * 10 }}
        >
          <div className="flex items-center justify-between">
            <span className="font-mono text-[10px] text-white/60">{c.sym}</span>
            <span className={`font-mono text-[10px] ${c.ok ? "text-emerald-soft" : "text-loss-soft"}`}>{c.r}</span>
          </div>
          <p className="mt-1 font-mono text-[9px] leading-relaxed text-white/30">
            {c.ok ? "lesson: retest held — size normally" : "mistake: third retest in compression"}
          </p>
        </motion.div>
      ))}
      <p className="pt-1 font-mono text-[9px]" style={{ color }}>
        recalled at the next similar setup
      </p>
    </div>
  );
}

function AnalyticsViz({ color, play }: { color: string; play: boolean }) {
  // The breakdowns the dashboard offers. Bar widths are decorative: this
  // diagram used to print results for strategies that do not exist.
  const rows: [string, number, boolean][] = [
    ["by strategy", 82, true],
    ["by symbol", 64, true],
    ["by session", 46, true],
    ["risk & drawdown", 58, true],
    ["fees inside every fill", 36, true],
  ];
  return (
    <div className="relative w-full max-w-[320px] space-y-2.5 px-6">
      {rows.map(([label, pct, up], i) => (
        <div key={label}>
          <div className="mb-1 flex justify-between font-mono text-[9px]">
            <span className="text-white/35">{label}</span>
            <span className="text-white/25">{up ? "·" : ""}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
            <motion.div
              className="h-full rounded-full"
              style={{ background: color, opacity: 0.55 }}
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.8, delay: play ? i * 0.08 : 0, ease: [0.22, 1, 0.36, 1] }}
            />
          </div>
        </div>
      ))}
      <p className="pt-1 font-mono text-[9px]" style={{ color }}>
        a breakdown, not a single curve
      </p>
    </div>
  );
}
