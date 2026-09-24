import { motion } from "framer-motion";
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

interface Stage {
  icon: LucideIcon;
  name: string;
  detail: string;
  metric: string;
  tone: "dim" | "gold" | "emerald";
}

const STAGES: Stage[] = [
  { icon: Radio, name: "Market Data", detail: "Streams candles from your exchange, per symbol & timeframe.", metric: "feed", tone: "dim" },
  { icon: Waypoints, name: "Structure & Trend", detail: "Reads market structure, trend shifts and confirmations.", metric: "analyze", tone: "dim" },
  { icon: BrainCircuit, name: "Decision Brain", detail: "Scores every setup 0–100. Only high-quality trades pass.", metric: "score ≥ 60", tone: "gold" },
  { icon: ShieldCheck, name: "Risk Gate", detail: "Position size, stop, take-profit and daily-loss guard.", metric: "enforced", tone: "emerald" },
  { icon: Zap, name: "Execution", detail: "Routes the order in paper or connected mode, sub-100ms.", metric: "< 100ms", tone: "emerald" },
  { icon: Database, name: "Journal & Memory", detail: "Every trade is stored, reviewed and learned from — forever.", metric: "persisted", tone: "gold" },
];

const DOT: Record<Stage["tone"], string> = {
  dim: "border-line-strong bg-white/[0.04] text-white/60",
  gold: "border-gold/30 bg-gold/10 text-gold",
  emerald: "border-emerald/30 bg-emerald/10 text-emerald",
};

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
              content, which is what kept the page 566px wide on a phone */}
          <Reveal className="min-w-0">
            <ol className="relative">
              {/* vertical rail */}
              <span className="absolute left-[1.35rem] top-4 bottom-4 w-px bg-gradient-to-b from-gold/40 via-line-strong to-emerald/40" />
              {STAGES.map((s, i) => (
                <motion.li
                  key={s.name}
                  initial={{ opacity: 0, x: -12 }}
                  whileInView={{ opacity: 1, x: 0 }}
                  viewport={{ once: true, margin: "-60px" }}
                  transition={{ delay: i * 0.09, duration: 0.5 }}
                  className="relative flex gap-4 pb-6 last:pb-0"
                >
                  <span
                    className={cn(
                      "relative z-10 flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border",
                      DOT[s.tone],
                    )}
                  >
                    <s.icon className="h-5 w-5" />
                  </span>
                  <div className="pt-1">
                    <div className="flex items-center gap-2.5">
                      <span className="font-mono text-[11px] text-white/30">
                        {String(i + 1).padStart(2, "0")}
                      </span>
                      <h3 className="text-[15px] font-semibold text-white">{s.name}</h3>
                      <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-white/45">
                        {s.metric}
                      </span>
                    </div>
                    <p className="mt-1 text-sm leading-relaxed text-white/55">{s.detail}</p>
                  </div>
                </motion.li>
              ))}
            </ol>
          </Reveal>

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
