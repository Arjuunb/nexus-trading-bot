import { motion } from "framer-motion";
import { Link2, SlidersHorizontal, Play, LineChart, type LucideIcon } from "lucide-react";
import { RevealGroup, SectionHeading } from "@/components/Reveal";

interface Step {
  n: string;
  icon: LucideIcon;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  { n: "01", icon: Link2, title: "Connect Market Data", body: "Nexus streams live Binance market data. More venues are on the roadmap." },
  { n: "02", icon: SlidersHorizontal, title: "Configure Strategy", body: "Pick a strategy, set your risk limits, choose symbols and timeframe." },
  { n: "03", icon: Play, title: "Go Live", body: "Deploy in paper mode. The engine watches the market for you; live routing stays locked." },
  { n: "04", icon: LineChart, title: "Watch It Learn", body: "Every trade, decision and lesson is remembered — its understanding of your trading compounds." },
];

export function HowItWorks() {
  return (
    <section id="how" className="section">
      <div className="container-x">
        <SectionHeading
          link="/how-it-works"
          eyebrow="Workflow"
          title="Live in four steps"
          subtitle="From connecting an exchange to watching it learn — no code, no guesswork."
        />

        <div className="relative mt-16">
          {/* connecting line (desktop) with a traveling signal pulse */}
          <div className="absolute left-0 right-0 top-[2.75rem] hidden h-px bg-gradient-to-r from-transparent via-line-strong to-transparent lg:block">
            <motion.span
              aria-hidden
              className="absolute -top-[2.5px] h-1.5 w-10 rounded-full bg-gradient-to-r from-transparent via-gold to-transparent"
              animate={{ left: ["-4%", "104%"] }}
              transition={{ duration: 5, repeat: Infinity, ease: "linear" }}
            />
          </div>

          {/* Staggered by the group, not by per-item delay: with delay={i*0.1}
              each card counted from the moment IT entered view, so on a slow
              scroll the fourth step sat visible and blank for 400ms. One
              trigger on the parent keeps the steps arriving in order. */}
          <RevealGroup className="grid gap-8 lg:grid-cols-4" stagger={0.08}>
            {STEPS.map((s, i) => (
              <div key={s.n}>
                <div className="relative flex flex-col items-center text-center lg:items-start lg:text-left">
                  <div className="relative z-10 flex h-[5.5rem] w-[5.5rem] items-center justify-center">
                    <div className="absolute inset-0 rounded-2xl border border-line bg-ink-700" />
                    <div className="absolute inset-0 rounded-2xl bg-gold/[0.05]" />
                    <s.icon className="relative h-7 w-7 text-gold" />
                    <span className="absolute -right-1 -top-1 flex h-6 w-6 items-center justify-center rounded-full bg-gold-sheen text-[11px] font-bold text-ink">
                      {i + 1}
                    </span>
                  </div>
                  <p className="mt-5 font-mono text-xs text-gold/60">{s.n}</p>
                  <h3 className="mt-1 text-lg font-semibold text-white">{s.title}</h3>
                  <p className="mt-2 max-w-xs text-sm leading-relaxed text-white/55">{s.body}</p>
                </div>
              </div>
            ))}
          </RevealGroup>
        </div>
      </div>
    </section>
  );
}
