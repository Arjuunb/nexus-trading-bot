import { useRef } from "react";
import { motion, useInView, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

/**
 * The Decision Brain's quality score, drawn from its own definition.
 *
 * This used to show three models (structure, momentum, "analogue recall")
 * weighted by an arbiter into ROUTE / HOLD / VETO. The engine has no such
 * ensemble. What it has is TradeBrain (automation-hub/strategies/brain.py):
 * one 0-100 score summed from eight weighted components, grade bands, and a
 * set of hard blocks that refuse a setup whatever its score. The weights and
 * rules below are copied from that file.
 */

const FACTORS = [
  { key: "htf", label: "Higher-timeframe alignment", weight: 22, note: "the bigger trend agrees with the side" },
  { key: "regime", label: "Regime fit", weight: 18, note: "the regime suits the setup" },
  { key: "rr", label: "Reward : risk", weight: 14, note: "≥ 2 scores best; below 1 is blocked" },
  { key: "momentum", label: "Momentum", weight: 12, note: "RSI supports the side, not exhausted" },
  { key: "stop", label: "Stop safety", weight: 10, note: "neither absurdly tight nor wide" },
  { key: "vol", label: "Volatility", weight: 10, note: "ATR% in a tradeable band" },
  { key: "structure", label: "Structure", weight: 8, note: "price on the right side of the structural EMA" },
  { key: "volume", label: "Volume", weight: 6, note: "participation confirms the move" },
];

const BANDS = [
  { range: "80 – 100", label: "High", cls: "border-emerald/40 bg-emerald/10 text-emerald-soft" },
  { range: "60 – 79", label: "Acceptable", cls: "border-aqua/40 bg-aqua/10 text-aqua-soft" },
  { range: "0 – 59", label: "Weak", cls: "border-loss/40 bg-loss/10 text-loss-soft" },
];

const BLOCKS = [
  "Reward : risk below 1.0",
  "Stop too tight or too wide",
  "Volatility far too low to trade",
  "Strong higher-timeframe trend against a non-reversal trade",
  "Choppy or unclear regime for a non-reversal trade",
  "Losing-streak cooldown",
];

export function DecisionCore() {
  const reduced = useReducedMotion() ?? false;
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { once: true, margin: "-80px" });
  const grow = inView || reduced;

  return (
    <div ref={ref} className="grid gap-6 lg:grid-cols-[1.25fr_0.75fr]">
      {/* the eight components, as their share of the 100 points */}
      <div className="rounded-3xl border border-graphite-500/60 bg-graphite-800/40 p-5 backdrop-blur-sm sm:p-7">
        <div className="flex items-baseline justify-between">
          <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-white/35">Quality score</span>
          <span className="font-mono text-[11px] text-white/35">weights sum to 100</span>
        </div>
        <ul className="mt-5 space-y-3.5">
          {FACTORS.map((f, i) => (
            <li key={f.key}>
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-[13px] text-white/80">{f.label}</span>
                <span className="font-mono text-[12px] tabular text-aqua-soft">{f.weight}</span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-graphite-600/70">
                <motion.div
                  className="h-full rounded-full bg-gradient-to-r from-electric to-aqua"
                  initial={{ width: reduced ? `${(f.weight / 22) * 100}%` : "0%" }}
                  animate={{ width: grow ? `${(f.weight / 22) * 100}%` : "0%" }}
                  transition={{ duration: 0.9, delay: reduced ? 0 : 0.1 + i * 0.07, ease: [0.22, 1, 0.36, 1] }}
                />
              </div>
              <p className="mt-1 text-[11px] text-white/35">{f.note}</p>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex flex-col gap-4">
        <div className="rounded-3xl border border-graphite-500/60 bg-graphite-800/40 p-5 sm:p-6">
          <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-white/35">Grades</span>
          <div className="mt-4 space-y-2">
            {BANDS.map((b) => (
              <div key={b.label} className="flex items-center justify-between gap-3">
                <span className="font-mono text-[12px] tabular text-white/60">{b.range}</span>
                <span className={cn("rounded-md border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider", b.cls)}>
                  {b.label}
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="rounded-3xl border border-loss/25 bg-loss/[0.04] p-5 sm:p-6">
          <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-loss-soft">Blocked whatever the score</span>
          <ul className="mt-3 space-y-2">
            {BLOCKS.map((b) => (
              <li key={b} className="flex gap-2.5 text-[13px] leading-snug text-white/60">
                <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-loss-soft" />
                {b}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
