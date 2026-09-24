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
    body: "Nexus connects to Binance USDⓈ-M for live market data today, with more venues on the roadmap. Feeds arrive over websockets and are normalised into one internal representation — the same candle, the same book, whatever the venue calls it. Gaps and duplicate frames are detected and backfilled before anything downstream sees them, so no strategy silently trades a hole in its own history.",
    facts: [
      ["Live venue", "Binance · more on the roadmap"],
      ["Key scope", "Trade only, never withdraw"],
      ["Feed", "Normalised · gap-checked"],
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
    body: "Every close, the system extracts what the chart is doing rather than what it costs: trend state on each declared timeframe, swing structure, ranges and their edges, liquidity pockets, and the volatility regime. This is the layer that lets a strategy say “only in expanding volatility” and have that mean something enforceable.",
    facts: [
      ["Extracted", "Trend · structure · liquidity"],
      ["Timeframes", "As declared per strategy"],
      ["Regime", "Classified on 3 horizons"],
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
    headline: "Three models score it, an arbiter decides",
    body: "The structure, the regime and your open exposure become one feature vector. A structure model, a regime-conditioned momentum model and an analogue-recall model each score it independently and return an attribution rather than a verdict. The arbiter weighs them by how well calibrated each has been in this regime, applies the threshold, and writes down its reasoning in plain language.",
    facts: [
      ["Models", "3 · scored independently"],
      ["Threshold", "72 of 100, configurable"],
      ["Output", "Decision + written rationale"],
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
    body: "A separate service with thirteen responsibilities and veto power. Daily budget, weekly budget, exposure ceiling, correlation load, schedule and blackout windows, venue health, position count, and the sizing itself — any one failing means the order is never created. It fails closed: if risk is unreachable, nothing trades. There is no code path from a model output to an exchange that skips it.",
    facts: [
      ["Rules", "13 · all mandatory"],
      ["On failure", "Fails closed"],
      ["Bypass", "None exists"],
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
    headline: "The order is placed the way the book allows",
    body: "Placement is chosen from live conditions — passive when the spread is wide enough to earn, aggressive when it is not, split when depth is thin. Protective stops and targets go to the venue the moment the position exists, so a dropped connection is never an unprotected position. Realised slippage is measured against the decision price on every fill.",
    facts: [
      ["Placement", "Adaptive per order"],
      ["Protection", "Resident at the venue"],
      ["Slippage", "Measured, not assumed"],
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
    body: "The trade closes and becomes memory: the conditions it was taken in, the reasoning at the time, the outcome, what went wrong and the lesson drawn. Rejections are journalled too — the record of what the system nearly did is usually more instructive than the record of what it did. Next time a similar setup appears, this is what gets consulted.",
    facts: [
      ["Stored", "Context · reasoning · outcome"],
      ["Rejections", "Journalled as decisions"],
      ["Recall", "At the next similar setup"],
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
    body: "Results are decomposed by strategy, symbol, regime, session and hour, with cost drag broken out from gross performance. An equity curve tells you something is working; attribution tells you what — and usually that a comfortable overall profit is one symbol in one session carrying four that are not.",
    facts: [
      ["Attribution", "5 dimensions"],
      ["Costs", "Separated from gross"],
      ["Feeds back", "Into model calibration"],
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
    { y: 62, label: "structure" },
    { y: 120, label: "momentum" },
    { y: 178, label: "analogue" },
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
      <text x="206" y="132" textAnchor="middle" fill={color} className="font-mono" style={{ fontSize: 7 }}>arbiter</text>
      <text x="266" y="124" textAnchor="middle" fill="#4FD98E" className="font-mono" style={{ fontSize: 9 }}>▸ route</text>
    </svg>
  );
}

function RiskViz({ color, play }: { color: string; play: boolean }) {
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      <text x="24" y="42" fill="#9FB0C4" style={{ fontSize: 10 }}>order intent</text>
      {/* thirteen slats */}
      {Array.from({ length: 13 }).map((_, i) => {
        const x = 30 + i * 20;
        const blocked = i === 8;
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
      <text x="30" y="188" fill={color} className="font-mono" style={{ fontSize: 8.5 }}>13 checks · all must pass</text>
      <text x="30" y="204" fill="#E5605B" className="font-mono" style={{ fontSize: 8.5 }}>1 veto · order never created</text>
      <path d="M24 50 L292 50" stroke={color} strokeOpacity="0.25" strokeDasharray="3 3" />
    </svg>
  );
}

function ExecutionViz({ color, play }: { color: string; play: boolean }) {
  const asks = [0.4, 0.7, 0.3];
  const bids = [0.6, 0.35, 0.8];
  return (
    <svg viewBox="0 0 320 240" className="relative w-full max-w-[380px]">
      {asks.map((w, i) => (
        <g key={`a${i}`}>
          <rect x={170 - w * 130} y={40 + i * 22} width={w * 130} height="16" fill="#E5605B" opacity="0.22" rx="2" />
          <text x="178" y={52 + i * 22} fill="#F07E7A" className="font-mono" style={{ fontSize: 8.5 }}>
            {(68412.5 - i * 1.5).toFixed(1)}
          </text>
        </g>
      ))}
      <motion.rect
        x="30" y="110" width="252" height="20" rx="4" fill={`${color}22`} stroke={color} strokeOpacity="0.7"
        animate={play ? { opacity: [0.55, 1, 0.55] } : {}} transition={{ duration: 1.8, repeat: Infinity }}
      />
      <text x="40" y="124" fill={color} className="font-mono" style={{ fontSize: 9 }}>filled 0.42 @ 68,408.2 · slip 1.3bp</text>
      {bids.map((w, i) => (
        <g key={`b${i}`}>
          <rect x={170 - w * 130} y={142 + i * 22} width={w * 130} height="16" fill="#2FBF71" opacity="0.22" rx="2" />
          <text x="178" y={154 + i * 22} fill="#4FD98E" className="font-mono" style={{ fontSize: 8.5 }}>
            {(68406.5 - i * 1.5).toFixed(1)}
          </text>
        </g>
      ))}
      <text x="30" y="30" fill="#9FB0C4" style={{ fontSize: 9.5 }}>depth-aware placement</text>
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
  const rows: [string, number, boolean][] = [
    ["structure-v4", 82, true],
    ["breakout-v2", 54, true],
    ["meanrev-v1", 28, false],
    ["London", 71, true],
    ["Asia", 19, false],
  ];
  return (
    <div className="relative w-full max-w-[320px] space-y-2.5 px-6">
      {rows.map(([label, pct, up], i) => (
        <div key={label}>
          <div className="mb-1 flex justify-between font-mono text-[9px]">
            <span className="text-white/35">{label}</span>
            <span className={up ? "text-emerald-soft" : "text-loss-soft"}>
              {up ? "+" : "−"}
              {pct}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
            <motion.div
              className="h-full rounded-full"
              style={{ background: up ? "#2FBF71" : "#E5605B" }}
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.8, delay: play ? i * 0.08 : 0, ease: [0.22, 1, 0.36, 1] }}
            />
          </div>
        </div>
      ))}
      <p className="pt-1 font-mono text-[9px]" style={{ color }}>
        attribution, not a single curve
      </p>
    </div>
  );
}
