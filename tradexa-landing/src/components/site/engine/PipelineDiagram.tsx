import { useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

/**
 * The engine's eight stages, as an animated signal path.
 *
 * The landing page states that a pipeline exists. This is the pipeline: a
 * horizontal bus with packets travelling it, where the currently-selected
 * stage is the one the reader is inspecting on the right. The path is one SVG
 * so the packets follow the *same* geometry as the drawn line — animating a
 * separate absolutely-positioned dot would drift out of alignment the moment
 * the container resized.
 */

export interface Stage {
  id: string;
  label: string;
  /** Two or three words shown under the node. */
  role: string;
  /** Inspector copy. */
  detail: string;
  /** What this stage writes down (shown as "records"). */
  budget: string;
  io: { in: string; out: string };
}

/**
 * The path every closed candle takes in a Trading Instance, as the code runs
 * it (services/auto_engine.py, strategies/brain.py, services/signal_pipeline.py,
 * services/fill_model.py). This used to describe an ensemble of three models,
 * an "analogue recall" model, an arbiter, a separate risk service with
 * thirteen rules, venue order routing and per-stage latency budgets — none of
 * which exist. Stages 4 to 7 only run when the strategy produces a signal.
 */
export const STAGES: Stage[] = [
  {
    id: "ingest",
    label: "Data",
    role: "Closed candles",
    detail:
      "Binance USDⓈ-M candles arrive over one shared websocket hub, with REST history to warm indicators up. A candle is only used once it has closed, and a stale or out-of-order feed pauses new entries rather than trading on it.",
    budget: "market state · freshness",
    io: { in: "Binance stream + history", out: "closed, verified candles" },
  },
  {
    id: "structure",
    label: "Context",
    role: "Higher timeframes",
    detail:
      "Strategies that declare higher timeframes are given 15m, 1h or 4h context built from the same feed, so a 5-minute decision can be checked against the trend it sits inside.",
    budget: "multi-timeframe evidence",
    io: { in: "closed candles", out: "multi-timeframe context" },
  },
  {
    id: "features",
    label: "Strategy",
    role: "Signal or wait",
    detail:
      "The strategy you chose — seven are in production, four are research-only — reads the candle and its context and returns a signal with entry, stop and target, or WAIT with the reason.",
    budget: "strategy decision report",
    io: { in: "candles + context", out: "signal · or WAIT + reason" },
  },
  {
    id: "ensemble",
    label: "Quality",
    role: "Decision Brain",
    detail:
      "The Decision Brain scores the setup from 0 to 100 on eight weighted factors — higher-timeframe alignment, regime fit, momentum, reward:risk, stop safety, volatility, structure and volume — and blocks outright on reward:risk below 1 or a stop that is too tight or too wide.",
    budget: "score · passed and failed rules",
    io: { in: "signal", out: "quality score + checklist" },
  },
  {
    id: "arbiter",
    label: "Sizing",
    role: "Position maths",
    detail:
      "Size comes from the distance to the stop and the risk you set per trade, then is rounded to Binance's lot and notional rules. A size the venue would reject is refused here.",
    budget: "order intent",
    io: { in: "accepted signal", out: "sized order intent" },
  },
  {
    id: "sizing",
    label: "Risk",
    role: "Checks every order",
    detail:
      "Every intent passes the risk checks: max open positions, no pyramiding, correlation, daily and weekly loss limits, cooldown after a loss, trades per day, session and trading day, exposure, and the global risk manager. Any one failing rejects the order with a written reason; if a check cannot run, nothing trades.",
    budget: "accepted · or rejected with reason",
    io: { in: "order intent", out: "approved · or rejected" },
  },
  {
    id: "risk",
    label: "Paper",
    role: "Simulated fill",
    detail:
      "Approved orders fill on the paper broker against the live Binance price. Limit entries fill at their price when it trades through (0.02% maker fee); market and stop orders pay half of a 0.04% spread, 0.03% slippage and a 0.04% taker fee. Stops and targets are managed by the engine. Live order routing is locked.",
    budget: "fills · positions",
    io: { in: "approved order", out: "simulated fill" },
  },
  {
    id: "route",
    label: "Record",
    role: "Written down",
    detail:
      "Every candle ends in a decision report — WAIT included — and every trade is kept in the ledger with the decision that opened it, so the dashboard can show why each order happened or didn't.",
    budget: "decision report · ledger",
    io: { in: "fill · or no trade", out: "report + trade history" },
  },
];

export function PipelineDiagram({
  activeId,
  onSelect,
  /** False parks the travelling packets — the caller gates this on whether the
   *  diagram is on screen in a foreground tab. */
  flowing = true,
}: {
  activeId: string;
  onSelect: (id: string) => void;
  flowing?: boolean;
}) {
  const reduced = useReducedMotion() ?? false;
  const animate = flowing && !reduced;
  const activeIndex = Math.max(0, STAGES.findIndex((s) => s.id === activeId));

  return (
    <div className="relative">
      {/* the bus line, behind the nodes */}
      <div aria-hidden className="pointer-events-none absolute inset-x-0 top-[26px] hidden h-px md:block">
        <div className="h-px w-full bg-gradient-to-r from-transparent via-electric/25 to-transparent" />
        {animate && (
          <>
            {[0, 1, 2].map((i) => (
              <motion.span
                key={i}
                className="absolute top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-aqua shadow-[0_0_10px_2px_rgba(34,211,238,0.7)]"
                initial={{ left: "0%", opacity: 0 }}
                animate={{ left: ["0%", "100%"], opacity: [0, 1, 1, 0] }}
                transition={{
                  duration: 3.4,
                  delay: i * 1.13,
                  repeat: Infinity,
                  ease: "linear",
                  times: [0, 0.08, 0.92, 1],
                }}
              />
            ))}
          </>
        )}
      </div>

      <ol className="relative grid grid-cols-2 gap-x-3 gap-y-6 sm:grid-cols-4 md:grid-cols-8 md:gap-x-1">
        {STAGES.map((s, i) => {
          const active = s.id === activeId;
          const passed = i < activeIndex;
          return (
            <li key={s.id}>
              <button
                onClick={() => onSelect(s.id)}
                aria-current={active ? "step" : undefined}
                className="group flex w-full flex-col items-center gap-2 text-center"
              >
                <span className="relative flex h-[52px] w-full items-center justify-center">
                  {/* node */}
                  <span
                    className={cn(
                      "relative flex h-9 w-9 items-center justify-center rounded-lg border font-mono text-[11px] transition-all duration-300",
                      active
                        ? "border-aqua/70 bg-aqua/15 text-aqua-soft shadow-[0_0_26px_-4px_rgba(34,211,238,0.8)]"
                        : passed
                          ? "border-electric/40 bg-electric/10 text-electric-soft"
                          : "border-graphite-500 bg-graphite-700 text-white/35 group-hover:border-electric/40 group-hover:text-electric-soft",
                    )}
                  >
                    {String(i + 1).padStart(2, "0")}
                    {active && animate && (
                      <span className="absolute inset-0 rounded-lg border border-aqua/60 motion-safe:animate-ping-ring" />
                    )}
                  </span>
                </span>
                <span
                  className={cn(
                    "text-[13px] font-medium transition-colors",
                    active ? "text-white" : "text-white/55 group-hover:text-white/85",
                  )}
                >
                  {s.label}
                </span>
                <span className="text-[10px] leading-tight text-white/25">{s.role}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/**
 * The architecture diagram: services, the direction data moves between them,
 * and the one path that has no bypass.
 *
 * Drawn as SVG rather than boxes-and-CSS-lines because the connectors need to
 * carry the flowing dash that makes the direction legible, and a border cannot
 * do that.
 */
export function ArchitectureDiagram() {
  const reduced = useReducedMotion() ?? false;
  const [hover, setHover] = useState<string | null>(null);

  const boxes: { id: string; x: number; y: number; w: number; h: number; label: string; sub: string; tone: "edge" | "core" | "guard" | "store" }[] = [
    { id: "venues", x: 8, y: 96, w: 108, h: 52, label: "Binance hub", sub: "ws · rest warm-up", tone: "edge" },
    { id: "bus", x: 148, y: 96, w: 104, h: 52, label: "Worker", sub: "one per instance", tone: "core" },
    { id: "engine", x: 284, y: 30, w: 118, h: 60, label: "Strategy + Brain", sub: "signal · score", tone: "core" },
    { id: "memory", x: 284, y: 154, w: 118, h: 60, label: "Ledger", sub: "trades · decisions", tone: "store" },
    { id: "risk", x: 436, y: 96, w: 104, h: 52, label: "Risk checks", sub: "every order", tone: "guard" },
    { id: "exec", x: 572, y: 96, w: 104, h: 52, label: "Paper broker", sub: "simulated fills", tone: "edge" },
  ];

  const TONES = {
    edge: { stroke: "#26262B", fill: "#121214", text: "#B0B8C4" },
    core: { stroke: "#EAB54F", fill: "#1F1A10", text: "#F2C766" },
    guard: { stroke: "#22C55E", fill: "#0F2A1D", text: "#4ADE80" },
    store: { stroke: "#B0B8C4", fill: "#17181B", text: "#D4D9E0" },
  } as const;

  const edges: { from: string; to: string; d: string; label?: string }[] = [
    { from: "venues", to: "bus", d: "M116 122 H148" },
    { from: "bus", to: "engine", d: "M252 122 C268 122 268 60 284 60" },
    { from: "bus", to: "memory", d: "M252 122 C268 122 268 184 284 184" },
    { from: "memory", to: "engine", d: "M343 154 V90", label: "loss streak" },
    { from: "engine", to: "risk", d: "M402 60 C420 60 420 122 436 122" },
    { from: "risk", to: "exec", d: "M540 122 H572", label: "approved only" },
    { from: "exec", to: "bus", d: "M624 148 C624 214 200 214 200 148" },
  ];

  return (
    <div className="overflow-x-auto">
      <svg viewBox="0 0 690 232" className="min-w-[640px] w-full" role="img"
           aria-label="Architecture: the shared Binance hub feeds one worker per Trading Instance; the worker runs the strategy and the Decision Brain, keeps trades and decisions in the ledger, and every order passes the risk checks before the paper broker fills it.">
        <defs>
          <marker id="nx-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M0 0 L8 4 L0 8 z" fill="#4A4A52" />
          </marker>
        </defs>

        {edges.map((e) => {
          const lit = hover === e.from || hover === e.to;
          return (
            <g key={`${e.from}-${e.to}`}>
              <path
                d={e.d}
                fill="none"
                stroke={lit ? "#EAB54F" : "#2A2A2F"}
                strokeWidth={lit ? 1.6 : 1.2}
                markerEnd="url(#nx-arrow)"
                className="transition-[stroke] duration-300"
              />
              {!reduced && (
                <path
                  d={e.d}
                  fill="none"
                  stroke="#B0B8C4"
                  strokeWidth="1.4"
                  strokeDasharray="3 21"
                  opacity={lit ? 0.9 : 0.45}
                  className="motion-safe:animate-dash-flow"
                />
              )}
              {e.label && (
                <text
                  x={e.d.includes("H572") ? 556 : 349}
                  y={e.d.includes("H572") ? 112 : 124}
                  textAnchor="middle"
                  className="fill-white/30 font-mono"
                  style={{ fontSize: 7.5 }}
                >
                  {e.label}
                </text>
              )}
            </g>
          );
        })}

        {boxes.map((b) => {
          const t = TONES[b.tone];
          const lit = hover === b.id;
          return (
            <g
              key={b.id}
              onMouseEnter={() => setHover(b.id)}
              onMouseLeave={() => setHover((h) => (h === b.id ? null : h))}
              className="cursor-default"
            >
              <rect
                x={b.x}
                y={b.y}
                width={b.w}
                height={b.h}
                rx="9"
                fill={t.fill}
                stroke={t.stroke}
                strokeWidth={lit ? 1.8 : 1}
                opacity={lit ? 1 : 0.92}
                className="transition-all duration-300"
              />
              <text x={b.x + b.w / 2} y={b.y + b.h / 2 - 3} textAnchor="middle" fill="#E9EEF3" style={{ fontSize: 11, fontWeight: 600 }}>
                {b.label}
              </text>
              <text x={b.x + b.w / 2} y={b.y + b.h / 2 + 12} textAnchor="middle" fill={t.text} className="font-mono" style={{ fontSize: 8 }}>
                {b.sub}
              </text>
            </g>
          );
        })}

        <text x="488" y="184" textAnchor="middle" fill="#4ADE80" className="font-mono" style={{ fontSize: 8 }}>
          no bypass path exists
        </text>
      </svg>
    </div>
  );
}
