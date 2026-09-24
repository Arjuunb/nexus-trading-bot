import { useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import {
  BrainCircuit,
  ShieldCheck,
  History,
  Radio,
  NotebookText,
  Building2,
  type LucideIcon,
} from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { FeatureDemo, type DemoKind } from "@/components/landing/FeatureDemos";
import { cn } from "@/lib/utils";
import { VENUES } from "@/site/platform";

interface Feature {
  icon: LucideIcon;
  title: string;
  body: string;
  points?: string[];
  exchanges?: { name: string; soon?: boolean }[];
  demo?: DemoKind;
  span?: boolean;
}

const FEATURES: Feature[] = [
  {
    icon: BrainCircuit,
    title: "Nexus Engine",
    body: "Reads market structure, scores every setup, and acts only on high-quality entries — explaining every yes and every no.",
    demo: "score",
    span: true,
  },
  {
    icon: ShieldCheck,
    title: "Risk Management",
    body: "Discipline enforced on every trade, before capital is ever exposed.",
    points: ["Maximum daily loss", "Position sizing", "Stop loss", "Take profit", "Trailing stop"],
    demo: "risk",
  },
  {
    icon: History,
    title: "Nexus Strategy Lab",
    body: "Backtest and optimise your strategies against years of historical data before risking a single dollar.",
    demo: "equity",
  },
  {
    icon: Radio,
    title: "Nexus Intelligence Feed",
    body: "See everything as it happens.",
    points: ["Real-time logs", "Performance", "Positions", "PnL"],
    demo: "feed",
  },
  {
    icon: NotebookText,
    title: "Trading Memory",
    body: "Every completed trade becomes permanent memory — the outcome, the mistake, the lesson and the coaching that sharpen the next decision.",
    demo: "memory",
  },
  {
    icon: Building2,
    title: "Exchange Support",
    body: "Live Binance market data today. More venues as each integration passes safety review.",
    exchanges: VENUES.slice(0, 4).map((v) => ({ name: v.name, soon: !v.live })),
  },
];

export function Features() {
  const reduced = useReducedMotion() ?? false;
  const [hovered, setHovered] = useState<number | null>(null);

  return (
    <section id="features" className="section">
      <div className="container-x">
        <SectionHeading
          link="/features"
          eyebrow="Capabilities"
          title="Built to remember, decide and protect"
          subtitle="A long-term trading companion — disciplined execution today, and a memory of your trading that compounds over time."
        />

        <div className="mt-14 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map((f, i) => {
            const isHovered = hovered === i;
            return (
              <Reveal key={f.title} delay={(i % 3) * 0.08} className={cn(f.span && "lg:col-span-1")}>
                <Card
                  interactive
                  className="group h-full p-6"
                  onMouseEnter={() => setHovered(i)}
                  onMouseLeave={() => setHovered((h) => (h === i ? null : h))}
                >
                  <div className="mb-5 inline-flex h-11 w-11 items-center justify-center rounded-xl border border-gold/20 bg-gold/[0.08] text-gold transition-transform duration-300 group-hover:scale-110">
                    <f.icon className="h-5 w-5" />
                  </div>
                  <h3 className="text-lg font-semibold text-white">{f.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-white/55">{f.body}</p>

                  {f.points && (
                    <ul className="mt-4 grid grid-cols-1 gap-1.5">
                      {f.points.map((p) => (
                        <li key={p} className="flex items-center gap-2 text-sm text-white/70">
                          <span className="h-1 w-1 rounded-full bg-gold" />
                          {p}
                        </li>
                      ))}
                    </ul>
                  )}

                  {f.exchanges && (
                    <div className="mt-4 flex flex-wrap gap-2">
                      {f.exchanges.map((e) => (
                        <Badge key={e.name} tone={e.soon ? "neutral" : "gold"}>
                          {e.name}
                          {e.soon && <span className="text-white/40">· Roadmap</span>}
                        </Badge>
                      ))}
                    </div>
                  )}

                  {/* hover-revealed micro-demo — representative, never live data */}
                  {f.demo && (
                    <div
                      className="grid transition-[grid-template-rows,opacity] duration-500 ease-out motion-reduce:transition-none"
                      style={{
                        gridTemplateRows: isHovered || reduced ? "1fr" : "0fr",
                        opacity: isHovered || reduced ? 1 : 0,
                      }}
                    >
                      <div className="overflow-hidden">
                        <div className="mt-5 border-t border-line pt-4">
                          <FeatureDemo kind={f.demo} play={isHovered} reduced={reduced} />
                        </div>
                      </div>
                    </div>
                  )}

                  {/* gold hover underglow */}
                  <motion.div className="pointer-events-none absolute inset-x-0 bottom-0 h-px bg-gradient-to-r from-transparent via-gold/50 to-transparent opacity-0 transition-opacity duration-300 group-hover:opacity-100" />
                </Card>
              </Reveal>
            );
          })}
        </div>
      </div>
    </section>
  );
}
