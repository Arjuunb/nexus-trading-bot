import { useEffect, useRef, useState } from "react";
import { motion, useInView, useReducedMotion } from "framer-motion";
import {
  BrainCircuit, ShieldCheck, Calculator, Send, CheckCircle2, Activity, Target, Flag,
} from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";

/**
 * "From signal to closed trade" — an animated pulse travels the execution
 * lifecycle, lighting each stage as it lands. These are the REAL paper-engine
 * stages (decision → risk → sizing → order → fill → manage → close); live
 * broker routing is on the roadmap and labelled as such. Representative demo,
 * off-screen-paused, reduced-motion-aware — never a live feed.
 */

interface Stage { icon: typeof Send; label: string; value: string; tone?: "gold" | "emerald" }
const STAGES: Stage[] = [
  { icon: BrainCircuit, label: "Bot decision", value: "LONG signal" },
  { icon: ShieldCheck, label: "Risk check", value: "0.8% · passed" },
  { icon: Calculator, label: "Position size", value: "0.42 BTC", tone: "gold" },
  { icon: Send, label: "Order", value: "limit @ 64,980" },
  { icon: CheckCircle2, label: "Filled", value: "0.42 @ 64,981", tone: "emerald" },
  { icon: Activity, label: "Monitoring", value: "SL / TP live" },
  { icon: Target, label: "Take-profit", value: "hit @ 66,300", tone: "emerald" },
  { icon: Flag, label: "Closed", value: "+$412 · +2.0R", tone: "emerald" },
];
const TOTAL = STAGES.length + 2; // trailing pause before the loop restarts

export function ExecutionFlow() {
  const ref = useRef<HTMLDivElement | null>(null);
  const inView = useInView(ref, { amount: 0.3 });
  const reduced = useReducedMotion();
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (reduced) { setStep(STAGES.length); return; }
    if (!inView) return;
    const id = window.setInterval(() => setStep((s) => (s + 1) % TOTAL), 560);
    return () => window.clearInterval(id);
  }, [inView, reduced]);

  return (
    <section id="execution" className="section">
      <div className="container-x">
        <SectionHeading
          eyebrow="Execution"
          title="From signal to closed trade"
          subtitle="Every trade follows the same path — decision, risk, sizing, order, fill, management, close. This is a representative demo of the paper lifecycle; live broker routing is on the roadmap."
        />

        <Reveal className="mt-14">
          <div ref={ref} className="glass rounded-3xl border border-line p-5 sm:p-8">
            <div className="mb-4 flex items-center justify-between">
              <span className="text-xs text-white/40">BTCUSDT · 5m</span>
              <Badge tone="neutral">Representative demo · paper</Badge>
            </div>

            <div className="grid grid-cols-2 gap-x-3 gap-y-6 sm:grid-cols-4 lg:grid-cols-8">
              {STAGES.map((s, i) => {
                const done = step > i || (reduced ?? false);
                const active = step === i && !reduced;
                const Icon = s.icon;
                const valTone = s.tone === "gold" ? "text-gold" : s.tone === "emerald" ? "text-emerald-soft" : "text-white/70";
                return (
                  <div key={i} className="relative flex flex-col items-center text-center">
                    {/* connector to the previous node (fills as the pulse passes) */}
                    {i > 0 && (
                      <span className="pointer-events-none absolute right-1/2 top-6 hidden h-px w-full lg:block" aria-hidden>
                        <span className="block h-full w-full bg-line" />
                        <motion.span
                          className="absolute inset-y-0 left-0 bg-gold"
                          initial={false}
                          animate={{ width: done ? "100%" : "0%" }}
                          transition={{ duration: 0.4 }}
                        />
                      </span>
                    )}
                    <motion.div
                      initial={false}
                      animate={{
                        scale: active ? 1.08 : 1,
                        borderColor: done ? "rgba(47,191,113,0.5)" : active ? "rgba(200,169,75,0.6)" : "rgba(255,255,255,0.10)",
                      }}
                      transition={{ duration: 0.3 }}
                      className={cn(
                        "relative z-10 grid h-12 w-12 place-items-center rounded-2xl border",
                        done ? "bg-emerald/[0.08]" : active ? "bg-gold/[0.10]" : "bg-white/[0.02]",
                      )}
                    >
                      <Icon className={cn("h-5 w-5", done ? "text-emerald" : active ? "text-gold" : "text-white/40")} />
                      {active && (
                        <motion.span
                          className="absolute inset-0 rounded-2xl border border-gold"
                          animate={{ scale: [1, 1.5], opacity: [0.5, 0] }}
                          transition={{ duration: 1, repeat: Infinity, ease: "easeOut" }}
                        />
                      )}
                    </motion.div>
                    <motion.div
                      initial={false}
                      animate={{ opacity: done || active ? 1 : 0.35 }}
                      transition={{ duration: 0.3 }}
                      className="mt-2"
                    >
                      <p className="text-[11px] font-semibold text-white/80">{s.label}</p>
                      <p className={cn("font-mono text-[10.5px]", done || active ? valTone : "text-white/30")}>{s.value}</p>
                    </motion.div>
                  </div>
                );
              })}
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
