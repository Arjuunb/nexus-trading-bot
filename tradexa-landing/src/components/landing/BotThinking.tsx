import { useEffect, useRef, useState } from "react";
import { motion, useInView, useReducedMotion } from "framer-motion";
import { CheckCircle2, Cpu } from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";

/**
 * "Watch the Decision Brain think" — a cinematic, looping demonstration of the
 * bot's reasoning: it streams each analysis step, fills an AI decision
 * checklist row-by-row, ramps a confidence ring, and stamps a verdict. Pauses
 * when off-screen and honours reduced-motion. Hand-authored REPRESENTATIVE
 * demo — clearly labelled, never presented as a live market feed.
 */

const GOLD = "#C8A94B", EMERALD = "#2FBF71";

interface Row { key: string; label: string; value: string; tone: "emerald" | "gold" }
// Each reasoning line optionally reveals one checklist row when it completes.
const STEPS: { t: string; row?: Row }[] = [
  { t: "Scanning BTCUSDT · 5m…" },
  { t: "Reading higher-timeframe trend…", row: { key: "htf", label: "Higher-timeframe trend", value: "Bullish", tone: "emerald" } },
  { t: "Checking EMA 8 / 33 alignment…", row: { key: "ema", label: "EMA alignment", value: "Confirmed", tone: "emerald" } },
  { t: "Analysing market structure…", row: { key: "bos", label: "Market structure", value: "BOS ✓", tone: "emerald" } },
  { t: "Detecting liquidity sweep…", row: { key: "sweep", label: "Liquidity sweep", value: "Detected", tone: "emerald" } },
  { t: "Measuring volume…", row: { key: "vol", label: "Volume", value: "Above average", tone: "emerald" } },
  { t: "Evaluating risk…", row: { key: "risk", label: "Risk / trade", value: "0.8%", tone: "gold" } },
  { t: "Calculating position size…", row: { key: "size", label: "Position size", value: "0.42 BTC", tone: "gold" } },
  { t: "Confidence 92% — setup confirmed" },
  { t: "Trade approved · LONG" },
];
const CONF_STEP = 8, DONE_STEP = 9;
const TOTAL = STEPS.length + 3; // trailing pause before the loop restarts

export function BotThinking() {
  const ref = useRef<HTMLDivElement | null>(null);
  const inView = useInView(ref, { amount: 0.35 });
  const reduced = useReducedMotion();
  const [step, setStep] = useState(0);

  useEffect(() => {
    if (reduced) { setStep(DONE_STEP); return; }
    if (!inView) return; // pause off-screen for performance
    const id = window.setInterval(() => setStep((s) => (s + 1) % TOTAL), 620);
    return () => window.clearInterval(id);
  }, [inView, reduced]);

  const rows = STEPS.filter((s) => s.row).map((s) => s.row!) as Row[];
  const conf = step >= CONF_STEP ? 92 : 0;
  const approved = step >= DONE_STEP;

  // confidence ring geometry
  const R = 30, C = 2 * Math.PI * R;

  return (
    <section id="brain" className="section">
      <div className="container-x">
        <SectionHeading
          eyebrow="Decision Brain"
          title="Watch the bot think — before it ever trades"
          subtitle="Every candle runs the same reasoning: trend, structure, liquidity, volume, risk. The engine says “no” far more than “yes”. This is a representative demo of that process — not a live feed."
        />

        <Reveal className="mt-14">
          <div ref={ref} className="glass mx-auto max-w-4xl overflow-hidden rounded-3xl border border-line">
            {/* terminal header */}
            <div className="flex items-center gap-3 border-b border-line px-5 py-3">
              <span className="flex gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full bg-loss/70" />
                <span className="h-2.5 w-2.5 rounded-full bg-gold/70" />
                <span className="h-2.5 w-2.5 rounded-full bg-emerald/70" />
              </span>
              <Cpu className="h-4 w-4 text-gold" />
              <span className="text-sm font-semibold text-white/90">Decision Brain</span>
              <span className="text-xs text-white/40">BTCUSDT · 5m</span>
              <Badge tone="neutral" className="ml-auto">Representative demo</Badge>
            </div>

            <div className="grid gap-0 md:grid-cols-2">
              {/* left — reasoning stream */}
              <div className="min-h-[300px] border-b border-line p-5 font-mono text-[13px] md:border-b-0 md:border-r">
                {STEPS.map((s, i) => {
                  const done = step > i || (reduced ?? false);
                  const active = step === i && !reduced;
                  const pending = step < i && !reduced;
                  const isVerdict = i >= CONF_STEP;
                  return (
                    <div key={i} className={cn(
                      "flex items-center gap-2 py-1 transition-opacity duration-300",
                      pending ? "opacity-25" : "opacity-100",
                    )}>
                      <span className={cn("shrink-0",
                        isVerdict ? "text-gold" : done ? "text-emerald" : "text-white/30")}>
                        {done ? (isVerdict ? "★" : "✓") : active ? "▸" : "·"}
                      </span>
                      <span className={cn(
                        isVerdict ? "font-semibold text-white" : "text-white/70")}>
                        {s.t}
                        {active && <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-gold align-middle" />}
                      </span>
                    </div>
                  );
                })}
              </div>

              {/* right — decision checklist + confidence + verdict */}
              <div className="flex flex-col gap-3 p-5">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-white/40">AI decision</p>
                <div className="flex flex-col gap-1.5">
                  {rows.map((r) => {
                    const stepIdx = STEPS.findIndex((s) => s.row?.key === r.key);
                    const shown = step > stepIdx || (reduced ?? false);
                    return (
                      <motion.div key={r.key}
                        initial={false}
                        animate={{ opacity: shown ? 1 : 0.2, x: shown ? 0 : 6 }}
                        transition={{ duration: 0.3 }}
                        className="flex items-center justify-between rounded-lg border border-line bg-white/[0.02] px-3 py-2 text-[13px]">
                        <span className="text-white/60">{r.label}</span>
                        <span className={cn("font-semibold", r.tone === "gold" ? "text-gold" : "text-emerald-soft")}>
                          {shown ? r.value : "—"}
                        </span>
                      </motion.div>
                    );
                  })}
                </div>

                {/* confidence ring + verdict */}
                <div className="mt-auto flex items-center gap-4 pt-2">
                  <div className="relative h-[72px] w-[72px] shrink-0">
                    <svg viewBox="0 0 72 72" className="h-full w-full -rotate-90">
                      <circle cx="36" cy="36" r={R} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="6" />
                      <motion.circle cx="36" cy="36" r={R} fill="none" stroke={approved ? EMERALD : GOLD} strokeWidth="6"
                        strokeLinecap="round" strokeDasharray={C}
                        initial={false}
                        animate={{ strokeDashoffset: C - (conf / 100) * C }}
                        transition={{ duration: 0.7, ease: "easeOut" }} />
                    </svg>
                    <div className="absolute inset-0 grid place-items-center">
                      <span className="text-lg font-bold text-white">{conf}%</span>
                    </div>
                  </div>
                  <div>
                    <p className="text-[11px] uppercase tracking-wider text-white/40">Recommendation</p>
                    <motion.div
                      initial={false}
                      animate={{ opacity: approved ? 1 : 0.3, scale: approved ? 1 : 0.96 }}
                      transition={{ duration: 0.35 }}
                      className={cn("mt-1 inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-bold",
                        approved ? "border-emerald/40 bg-emerald/10 text-emerald-soft" : "border-line text-white/40")}>
                      <CheckCircle2 className="h-4 w-4" />
                      {approved ? "LONG · Trade approved" : "Evaluating…"}
                    </motion.div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
