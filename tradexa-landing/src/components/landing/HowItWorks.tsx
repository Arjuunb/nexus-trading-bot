import { motion, useReducedMotion } from "framer-motion";
import { Link2, SlidersHorizontal, Play, LineChart, type LucideIcon } from "lucide-react";
import { SectionHeading } from "@/components/Reveal";
import { useActiveStep, useSectionProgress } from "@/components/motion/Scroll";
import { cn } from "@/lib/utils";

interface Step {
  n: string;
  icon: LucideIcon;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  { n: "01", icon: Link2, title: "Connect Market Data", body: "Nexus streams live Binance market data. More venues are on the roadmap." },
  { n: "02", icon: SlidersHorizontal, title: "Configure Strategy", body: "Pick a strategy, set your risk limits, choose symbols and timeframe." },
  { n: "03", icon: Play, title: "Run on Paper", body: "Start it in paper mode. The engine watches the market for you; live routing stays locked." },
  { n: "04", icon: LineChart, title: "Watch It Learn", body: "Every trade, decision and lesson is remembered — its understanding of your trading compounds." },
];

export function HowItWorks() {
  const reduced = useReducedMotion();
  const { ref, progress } = useSectionProgress(["start 80%", "center 45%"]);
  const active = useActiveStep(progress, STEPS.length);
  return (
    <section id="how" className="section">
      <div className="container-x">
        <SectionHeading
          link="/how-it-works"
          eyebrow="Workflow"
          title="Running in four steps"
          subtitle="From connecting market data to watching it learn — no code, no guesswork."
        />

        <div ref={ref} className="relative mt-16">
          {/* The connecting line fills as the steps scroll into place, and
              each step lights when the line reaches it. */}
          <div aria-hidden className="absolute left-[12.5%] right-[12.5%] top-[2.75rem] hidden h-px bg-line-strong lg:block">
            <motion.span
              className="absolute inset-0 origin-left bg-gradient-to-r from-gold/40 via-gold to-gold/40"
              style={reduced ? undefined : { scaleX: progress }}
            />
          </div>

          <ol className="grid gap-8 lg:grid-cols-4">
            {STEPS.map((s, i) => {
              const lit = i <= active;
              return (
                <li key={s.n} className="relative flex flex-col items-center text-center">
                  <motion.div
                    className="relative z-10 flex h-[5.5rem] w-[5.5rem] items-center justify-center"
                    initial={false}
                    animate={reduced ? undefined : { y: lit ? 0 : 10, opacity: lit ? 1 : 0.5 }}
                    transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
                  >
                    <div className={cn("absolute inset-0 rounded-2xl border bg-ink-700 transition-colors duration-500",
                      lit ? "border-gold/35" : "border-line")} />
                    <s.icon className={cn("relative h-7 w-7 transition-colors duration-500", lit ? "text-gold" : "text-white/35")} aria-hidden />
                    <span className={cn(
                      "absolute -right-1 -top-1 flex h-6 w-6 items-center justify-center rounded-full text-[11px] font-bold transition-colors duration-500",
                      lit ? "bg-gold-sheen text-ink" : "bg-ink-500 text-white/50",
                    )}>
                      {i + 1}
                    </span>
                  </motion.div>
                  <p className="mt-5 font-mono text-xs text-gold/60">{s.n}</p>
                  <h3 className="mt-1 text-lg font-semibold text-white">{s.title}</h3>
                  <p className="mt-2 max-w-xs text-sm leading-relaxed text-white/55">{s.body}</p>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
    </section>
  );
}
