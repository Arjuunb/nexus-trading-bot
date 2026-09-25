import { useRef } from "react";
import { motion, useInView, useReducedMotion } from "framer-motion";
import { Activity } from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";

/**
 * "Connectivity" — honest by design. Nexus runs on LIVE Binance market data
 * today (real, streaming); broker execution for other venues is on the roadmap.
 * We deliberately do NOT animate a "connected" state that doesn't exist — the
 * live venue streams, the rest are clearly marked planned. Consistent with the
 * platform's real, paper-first status.
 */

interface Venue { name: string; live: boolean; note: string }
const VENUES: Venue[] = [
  { name: "Binance", live: true, note: "Live market data" },
  { name: "Bybit", live: false, note: "Roadmap" },
  { name: "OKX", live: false, note: "Roadmap" },
  { name: "Kraken", live: false, note: "Roadmap" },
  { name: "Coinbase", live: false, note: "Roadmap" },
  { name: "Interactive Brokers", live: false, note: "Roadmap" },
];

export function Connectivity() {
  const ref = useRef<HTMLDivElement | null>(null);
  const inView = useInView(ref, { amount: 0.3 });
  const reduced = useReducedMotion();
  const animate = inView && !reduced;

  return (
    <section id="connectivity" className="section">
      <div className="container-x">
        <SectionHeading
          eyebrow="Connectivity"
          title="Real market data — honestly scoped"
          subtitle="Nexus streams live Binance market data today. Broker execution for more venues is on the roadmap — we won't show a connection that isn't there."
        />

        <Reveal className="mt-14">
          <div ref={ref} className="glass mx-auto max-w-5xl rounded-3xl border border-line p-6 sm:p-8">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {VENUES.map((v) => (
                <motion.div
                  key={v.name}
                  initial={false}
                  animate={{ opacity: 1 }}
                  className={cn(
                    "relative overflow-hidden rounded-2xl border p-4",
                    v.live ? "border-emerald/30 bg-emerald/[0.05]" : "border-line bg-white/[0.02]",
                  )}
                >
                  {/* streaming data pulse — ONLY on the live venue */}
                  {v.live && animate && (
                    <motion.span
                      aria-hidden
                      className="pointer-events-none absolute inset-y-0 left-0 w-16 bg-gradient-to-r from-transparent via-emerald/15 to-transparent"
                      initial={{ x: "-20%" }}
                      animate={{ x: "260%" }}
                      transition={{ duration: 2.4, repeat: Infinity, ease: "linear" }}
                    />
                  )}
                  <div className="relative flex items-center justify-between">
                    <span className={cn("text-sm font-semibold", v.live ? "text-white" : "text-white/70")}>
                      {v.name}
                    </span>
                    <span className="flex items-center gap-1.5">
                      <span className={cn("relative h-2 w-2 rounded-full", v.live ? "bg-emerald" : "bg-white/25")}>
                        {v.live && animate && (
                          <motion.span
                            className="absolute inset-0 rounded-full bg-emerald"
                            animate={{ scale: [1, 2.4], opacity: [0.6, 0] }}
                            transition={{ duration: 1.6, repeat: Infinity, ease: "easeOut" }}
                          />
                        )}
                      </span>
                    </span>
                  </div>
                  <div className="relative mt-2">
                    {v.live ? (
                      <Badge tone="emerald" className="gap-1"><Activity className="h-3 w-3" /> {v.note}</Badge>
                    ) : (
                      <Badge tone="neutral">{v.note}</Badge>
                    )}
                  </div>
                  {v.live && (
                    <p className="relative mt-3 font-mono text-[11px] text-white/40">candles · funding · OI · streaming</p>
                  )}
                </motion.div>
              ))}
            </div>
            <p className="mt-6 text-center text-xs text-white/40">
              API keys are scoped to read + trade only — never withdrawals. Live execution unlocks per venue as integrations pass safety review.
            </p>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
