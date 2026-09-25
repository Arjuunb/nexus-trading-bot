import { motion, useReducedMotion } from "framer-motion";
import { GridTexture } from "@/components/site/backdrops";
import {
  Radio,
  Waypoints,
  BrainCircuit,
  ShieldCheck,
  Zap,
  Database,
  type LucideIcon,
} from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { LiveTerminal } from "./LiveTerminal";
import { cn } from "@/lib/utils";
import { useActiveStep, useSectionProgress } from "@/components/motion/Scroll";

interface Stage {
  icon: LucideIcon;
  name: string;
  detail: string;
  metric: string;
}

const STAGES: Stage[] = [
  { icon: Radio, name: "Market Data", detail: "Closed candles from Binance, per symbol and timeframe, checked for gaps.", metric: "feed" },
  { icon: Waypoints, name: "Structure & Trend", detail: "Reads market structure, trend shifts and confirmations.", metric: "analyze" },
  { icon: BrainCircuit, name: "Decision Brain", detail: "Scores every setup 0–100. Only high-quality setups pass.", metric: "score ≥ 60" },
  { icon: ShieldCheck, name: "Risk Gate", detail: "Position size, stop, take-profit and the daily-loss guard.", metric: "enforced" },
  { icon: Zap, name: "Execution", detail: "Places the order on the paper broker. Live routing is locked.", metric: "paper" },
  { icon: Database, name: "Journal & Memory", detail: "Every trade is stored, reviewed and learned from.", metric: "persisted" },
];

export function EnginePipeline() {
  return (
    <section id="engine" className="section relative">
      {/* a technical section, so the grid belongs */}
      <GridTexture />
      <div className="container-x">
        <SectionHeading
          link="/engine"
          eyebrow="Decision Engine"
          title="A disciplined pipeline, not a black box"
          subtitle="Every candle runs the same deterministic path — from market data to a journaled trade. You can see each step, and why it fired or didn't."
        />

        <div className="mt-14 grid gap-6 lg:grid-cols-[1fr_1fr] lg:gap-10">
          {/* pipeline — min-w-0 lets each grid column shrink below its
              content, which is what kept the page 566px wide on a phone.
              The steps light in order as the list scrolls through the
              viewport, and the rail fills behind them. */}
          <PipelineSteps />

          {/* live engine log */}
          <Reveal delay={0.15} className="min-w-0 lg:sticky lg:top-24 lg:self-start">
            <LiveTerminal />
            <p className="mt-3 px-1 font-mono text-[11px] leading-relaxed text-white/35">
              // representative engine output · paper mode · not a live account
            </p>
          </Reveal>
        </div>
      </div>
    </section>
  );
}

function PipelineSteps() {
  const reduced = useReducedMotion();
  const { ref, progress } = useSectionProgress(["start 75%", "end 55%"]);
  const active = useActiveStep(progress, STAGES.length);
  return (
    <div ref={ref} className="min-w-0">
      <ol className="relative">
        {/* the rail, and the gold fill that follows the scroll */}
        <span aria-hidden className="absolute left-[1.35rem] top-4 bottom-4 w-px bg-line-strong" />
        <motion.span
          aria-hidden
          className="absolute left-[1.35rem] top-4 bottom-4 w-px origin-top bg-gradient-to-b from-gold to-gold/40"
          style={reduced ? undefined : { scaleY: progress }}
        />
        {STAGES.map((s, i) => {
          const lit = i <= active;
          return (
            <li key={s.name} className="relative flex gap-4 pb-6 last:pb-0">
              <span
                className={cn(
                  "relative z-10 flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border transition-colors duration-500",
                  lit ? "border-gold/40 bg-gold/10 text-gold" : "border-line-strong bg-ink-700 text-white/35",
                )}
              >
                <s.icon className="h-5 w-5" aria-hidden />
              </span>
              <div className={cn("pt-1 transition-opacity duration-500", lit ? "opacity-100" : "opacity-45")}>
                <div className="flex items-center gap-2.5">
                  <span className="font-mono text-[11px] text-white/30">{String(i + 1).padStart(2, "0")}</span>
                  <h3 className="text-[15px] font-semibold text-white">{s.name}</h3>
                  <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-white/45">
                    {s.metric}
                  </span>
                </div>
                <p className="mt-1 text-sm leading-relaxed text-white/55">{s.detail}</p>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
