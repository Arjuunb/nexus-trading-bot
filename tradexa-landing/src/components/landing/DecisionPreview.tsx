import { motion, useReducedMotion } from "framer-motion";
import { Check, Minus, BookCheck } from "lucide-react";
import { cn } from "@/lib/utils";

// The hero's product preview: what one decision looks like in Nexus. It is an
// example of the report the engine writes for every closed candle — the score,
// the factors behind it and the verdict — not an account, and it carries no
// performance figures (no equity, win rate or P&L).

const EASE = [0.22, 1, 0.36, 1] as const;
const SCORE = 72;

const FACTORS: { label: string; detail: string; pass: boolean | null }[] = [
  { label: "Higher-timeframe trend", detail: "4h up · aligned", pass: true },
  { label: "Market structure", detail: "higher high confirmed", pass: true },
  { label: "Liquidity", detail: "sweep below 43,120", pass: true },
  { label: "Volume", detail: "average, no confirmation", pass: null },
  { label: "Volatility regime", detail: "normal", pass: true },
  { label: "Risk budget", detail: "0.8% of 3% daily left", pass: true },
];

function ScoreRing({ reduced }: { reduced: boolean }) {
  const r = 34;
  const c = 2 * Math.PI * r;
  return (
    <div className="relative h-24 w-24 shrink-0">
      <svg viewBox="0 0 84 84" className="h-full w-full -rotate-90" aria-hidden>
        <circle cx="42" cy="42" r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="6" />
        <motion.circle
          cx="42" cy="42" r={r} fill="none" stroke="#EAB54F" strokeWidth="6" strokeLinecap="round"
          strokeDasharray={c}
          initial={{ strokeDashoffset: reduced ? c * (1 - SCORE / 100) : c }}
          animate={{ strokeDashoffset: c * (1 - SCORE / 100) }}
          transition={{ duration: 1.4, delay: 0.5, ease: EASE }}
        />
        {/* the minimum the setup had to clear */}
        <line x1="42" y1="4" x2="42" y2="12" stroke="rgba(255,255,255,0.35)" strokeWidth="1.5"
              transform={`rotate(${360 * 0.6} 42 42)`} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="tabular text-2xl font-bold text-white">{SCORE}</span>
        <span className="text-[10px] text-white/40">of 100</span>
      </div>
    </div>
  );
}

export function DecisionPreview() {
  const reduced = useReducedMotion() ?? false;
  const row = (i: number) => ({
    initial: { opacity: 0, x: reduced ? 0 : -8 },
    animate: { opacity: 1, x: 0 },
    transition: { delay: reduced ? 0 : 0.7 + i * 0.12, duration: 0.45, ease: EASE },
  });
  const verdictDelay = reduced ? 0 : 0.7 + FACTORS.length * 0.12 + 0.15;

  return (
    <div className="glass-strong relative overflow-hidden rounded-3xl p-5 shadow-card sm:p-6">
      <div className="pointer-events-none absolute -right-24 -top-24 h-64 w-64 rounded-full bg-gold/[0.07] blur-3xl" aria-hidden />

      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-wider text-white/40">decision report</p>
          <p className="mt-1 text-sm font-medium text-white">BTCUSDT · 1h · candle closed 14:00</p>
        </div>
        <span className="rounded-full border border-line-strong px-2.5 py-1 text-[10.5px] text-white/55">Example</span>
      </div>

      <div className="mt-5 flex items-center gap-5 rounded-2xl border border-line bg-ink-700/60 p-4">
        <ScoreRing reduced={reduced} />
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-wider text-white/40">Quality score</p>
          <p className="mt-1 text-sm leading-relaxed text-white/70">
            Clears the minimum of <b className="font-semibold text-white">60</b>. Every factor behind the
            number is listed, so a pass or a skip can be checked.
          </p>
        </div>
      </div>

      <ul className="mt-4 divide-y divide-line rounded-2xl border border-line">
        {FACTORS.map((f, i) => (
          <motion.li key={f.label} {...row(i)} className="flex flex-col gap-0.5 px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
            <span className="flex min-w-0 items-center gap-2.5">
              <span className={cn("flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                f.pass ? "border-gold/40 bg-gold/10 text-gold" : "border-line-strong text-white/40")}>
                {f.pass ? <Check className="h-3 w-3" aria-hidden /> : <Minus className="h-3 w-3" aria-hidden />}
              </span>
              <span className="truncate text-[13px] text-white/80">{f.label}</span>
              <span className="sr-only">{f.pass ? "passes" : "neutral"}</span>
            </span>
            <span className="pl-[30px] font-mono text-[11px] text-white/40 sm:shrink-0 sm:pl-0">{f.detail}</span>
          </motion.li>
        ))}
      </ul>

      <motion.div
        initial={{ opacity: 0, y: reduced ? 0 : 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: verdictDelay, duration: 0.5, ease: EASE }}
        className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-gold/25 bg-gold/[0.06] px-4 py-3"
      >
        <div className="flex items-center gap-2.5">
          <span className="rounded-md bg-gold-sheen px-2 py-0.5 text-[11px] font-bold tracking-wide text-ink">TAKE · LONG</span>
          <span className="font-mono text-[11px] text-white/55">risk 0.8% · stop 43,120 · target 2R</span>
        </div>
        <span className="flex items-center gap-1.5 text-[11px] text-white/55">
          <BookCheck className="h-3.5 w-3.5 text-gold/80" aria-hidden /> journaled with reasons
        </span>
      </motion.div>

      <p className="mt-3 text-center font-mono text-[10.5px] text-white/30">
        example of the report written for every candle · paper account · not live results
      </p>
    </div>
  );
}
