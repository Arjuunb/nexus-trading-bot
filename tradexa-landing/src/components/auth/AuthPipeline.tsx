import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Activity, BookOpen, CheckCircle2, ShieldCheck, XCircle, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * The sign-in / create-account showcase animation: one trade idea walking the
 * path every idea takes in the product -- signal, risk gates, journal, paper
 * fill -- with every third idea vetoed at the gates, because most ideas are.
 *
 * It describes the product, not an account: no prices, no returns, no
 * numbers. The candle strip behind it is decorative and says so.
 */

interface Stage {
  label: string;
  icon: LucideIcon;
  pass: string;
  veto?: string;
}

const STAGES: Stage[] = [
  { label: "Signal", icon: Activity,
    pass: "A strategy makes its call on a closed candle, never the forming one." },
  { label: "Risk gates", icon: ShieldCheck,
    pass: "Size, stop, exposure and loss limits are checked. Any one failing is a veto.",
    veto: "Vetoed at the risk gates: one check failed, so there is no trade." },
  { label: "Journal", icon: BookOpen,
    pass: "The decision and every reason behind it are written down before an order exists.",
    veto: "The veto and its reason are journaled too, so skipped trades can be reviewed." },
  { label: "Paper fill", icon: CheckCircle2,
    pass: "The order fills on a paper account. Live order routing stays locked.",
    veto: "No order. Nothing reaches the account — selective by design." },
];

const STEP_MS = 1800;
/** Four stages, then one beat holding the finished path. */
const TICKS_PER_IDEA = STAGES.length + 1;
/** Every third idea is vetoed. */
const VETO_EVERY = 3;

type NodeState = "idle" | "active" | "done" | "veto" | "skipped";

/** Advances one beat at a time; parks while the tab is hidden. */
function useBeat(enabled: boolean): number {
  const [beat, setBeat] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let id: number | undefined;
    const start = () => {
      if (id === undefined) id = window.setInterval(() => setBeat((b) => b + 1), STEP_MS);
    };
    const stop = () => {
      if (id !== undefined) window.clearInterval(id);
      id = undefined;
    };
    const onVisibility = () => (document.hidden ? stop() : start());
    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled]);
  return beat;
}

function nodeState(i: number, step: number, veto: boolean): NodeState {
  if (veto && i === 1 && step >= 1) return "veto";
  if (veto && i === STAGES.length - 1 && step >= i) return "skipped";
  if (i < step) return "done";
  if (i === step) return "active";
  return "idle";
}

export function AuthPipeline() {
  const reduced = useReducedMotion() ?? false;
  const beat = useBeat(!reduced);
  const idea = Math.floor(beat / TICKS_PER_IDEA);
  const step = reduced ? STAGES.length - 1 : Math.min(beat % TICKS_PER_IDEA, STAGES.length - 1);
  const veto = !reduced && idea % VETO_EVERY === VETO_EVERY - 1;
  const stage = STAGES[step];
  const caption = veto && step >= 1 && stage.veto ? stage.veto : stage.pass;
  const progress = step / (STAGES.length - 1);

  return (
    <div className="glass-strong overflow-hidden rounded-2xl shadow-card">
      <CandleStrip animate={!reduced} />

      <div className="p-5">
        <p className="mb-5 text-[11px] uppercase tracking-wider text-white/40">How every trade idea is handled</p>

        {/* Screen readers get the path once, in words; the animation is decoration. */}
        <ol className="sr-only">
          {STAGES.map((s) => <li key={s.label}>{s.label}: {s.pass}</li>)}
        </ol>

        <div className="relative" aria-hidden="true">
          {/* track between the first and last node centres (each node is
              72px wide with its 36px circle centred, so centres sit 36px in) */}
          <div className="absolute left-[36px] right-[36px] top-[18px] h-px bg-white/10">
            <motion.div
              className={cn("absolute inset-y-0 left-0", veto && step >= 1
                ? "bg-gradient-to-r from-gold/70 via-loss/60 to-loss/30"
                : "bg-gradient-to-r from-gold/40 to-gold")}
              initial={false}
              animate={{ width: `${progress * 100}%` }}
              transition={{ duration: reduced ? 0 : 0.7, ease: [0.22, 1, 0.36, 1] }}
            />
            {!reduced && (
              <motion.span
                key={idea}
                className={cn("absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full",
                  veto && step >= 1 ? "bg-loss-soft shadow-[0_0_14px_4px_rgba(248,113,113,0.45)]"
                    : "bg-gold-soft shadow-[0_0_14px_4px_rgba(234,181,79,0.55)]")}
                initial={{ left: "0%", opacity: 0 }}
                animate={{ left: `${progress * 100}%`, opacity: 1 }}
                transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
              />
            )}
          </div>

          <ol className="relative flex justify-between">
            {STAGES.map((s, i) => (
              <StageNode key={s.label} stage={s} state={reduced ? "done" : nodeState(i, step, veto)} />
            ))}
          </ol>
        </div>

        <div className="mt-5 min-h-[40px]" aria-hidden="true">
          <AnimatePresence mode="wait" initial={false}>
            <motion.p
              key={reduced ? "static" : `${idea}-${step}`}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.25 }}
              className={cn("text-[12.5px] leading-relaxed",
                veto && step >= 1 ? "text-loss-soft/90" : "text-white/60")}
            >
              {reduced
                ? "Signal, risk gates, journal, paper fill — in that order, every time. Live order routing is locked."
                : caption}
            </motion.p>
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}

function StageNode({ stage, state }: { stage: Stage; state: NodeState }) {
  const Icon = state === "veto" ? XCircle : stage.icon;
  const lit = state === "active" || state === "done";
  return (
    <li className="flex w-[72px] flex-col items-center gap-2">
      <motion.span
        className={cn(
          "relative flex h-9 w-9 items-center justify-center rounded-full border bg-ink-800 transition-colors duration-300",
          state === "active" && "border-gold/70 text-gold-soft",
          state === "done" && "border-gold/40 text-gold",
          state === "veto" && "border-loss/60 text-loss-soft",
          state === "skipped" && "border-white/10 text-white/20",
          state === "idle" && "border-white/10 text-white/35",
        )}
        animate={state === "active" || state === "veto" ? { scale: [1, 1.12, 1] } : { scale: 1 }}
        transition={{ duration: 0.45 }}
      >
        {(state === "active" || state === "veto") && (
          <motion.span
            className={cn("absolute inset-0 rounded-full", state === "veto" ? "bg-loss/20" : "bg-gold/20")}
            initial={{ opacity: 0.8, scale: 1 }}
            animate={{ opacity: 0, scale: 1.8 }}
            transition={{ duration: 1.1, ease: "easeOut" }}
          />
        )}
        <Icon className="relative h-4 w-4" />
      </motion.span>
      <span className={cn("text-center text-[11.5px] font-medium transition-colors duration-300",
        lit ? "text-white/85" : state === "veto" ? "text-loss-soft" : "text-white/40",
        state === "skipped" && "line-through decoration-white/20")}>
        {stage.label}
      </span>
    </li>
  );
}

// ─────────────────────────── decorative candle strip ───────────────────────────
const CANDLES = 48;
const PITCH = 14;
const HEIGHT = 128;

function seeded(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** One periodic cycle of made-up candles, so two copies scroll seamlessly. */
function buildStrip() {
  const rnd = seeded(11);
  const noise = Array.from({ length: CANDLES }, () => rnd() - 0.5);
  const close = (i: number) => {
    const k = ((i % CANDLES) + CANDLES) % CANDLES;
    return 64 + 12 * Math.sin((2 * Math.PI * k) / CANDLES)
      + 10 * Math.sin((6 * Math.PI * k) / CANDLES + 1.3) + 8 * noise[k];
  };
  const candles = Array.from({ length: CANDLES }, (_, i) => {
    const open = close(i - 1), end = close(i);
    return { open, close: end, high: Math.max(open, end) + 2 + rnd() * 5, low: Math.min(open, end) - 2 - rnd() * 5 };
  });
  // EMAs warmed over three cycles, so the values wrap without a jump.
  const ema = (period: number) => {
    const alpha = 2 / (period + 1);
    let value = close(0);
    const out: number[] = [];
    for (let i = 0; i < CANDLES * 3; i++) {
      value = alpha * close(i) + (1 - alpha) * value;
      if (i >= CANDLES * 2) out.push(value);
    }
    return out;
  };
  const path = (values: number[]) =>
    Array.from({ length: CANDLES * 2 + 1 }, (_, i) =>
      `${i ? "L" : "M"}${(i * PITCH + PITCH / 2).toFixed(1)} ${(HEIGHT - values[i % CANDLES]).toFixed(1)}`).join(" ");
  return { candles, fast: path(ema(9)), slow: path(ema(33)) };
}

const STRIP = buildStrip();

function CandleStrip({ animate }: { animate: boolean }) {
  const width = CANDLES * PITCH;
  return (
    <div
      className="relative h-32 border-b border-white/[0.06]"
      style={{ maskImage: "linear-gradient(90deg, transparent, black 12%, black 88%, transparent)",
               WebkitMaskImage: "linear-gradient(90deg, transparent, black 12%, black 88%, transparent)" }}
    >
      <span className="absolute right-4 top-3 z-10 text-[10px] uppercase tracking-wider text-white/25">
        Illustration
      </span>
      <svg aria-hidden="true" className="h-full w-full" viewBox={`0 0 ${width} ${HEIGHT}`} preserveAspectRatio="xMinYMid slice">
        <line x1="0" x2={width} y1={HEIGHT - 44} y2={HEIGHT - 44} stroke="rgba(234,181,79,0.22)" strokeDasharray="3 5" />
        <motion.g
          initial={false}
          animate={animate ? { x: [0, -width] } : { x: 0 }}
          transition={animate ? { duration: 80, ease: "linear", repeat: Infinity } : { duration: 0 }}
        >
          {[0, 1].map((copy) => (
            <g key={copy} transform={`translate(${copy * width} 0)`}>
              {STRIP.candles.map((c, i) => {
                const up = c.close >= c.open;
                const x = i * PITCH + PITCH / 2;
                const colour = up ? "rgba(74,222,128,0.45)" : "rgba(248,113,113,0.40)";
                return (
                  <g key={i}>
                    <line x1={x} x2={x} y1={HEIGHT - c.high} y2={HEIGHT - c.low} stroke={colour} strokeWidth="1" />
                    <rect x={x - 3.5} width="7" rx="1"
                      y={HEIGHT - Math.max(c.open, c.close)}
                      height={Math.max(1.5, Math.abs(c.close - c.open))}
                      fill={colour} />
                  </g>
                );
              })}
            </g>
          ))}
          <path d={STRIP.slow} fill="none" stroke="rgba(176,184,196,0.45)" strokeWidth="1.4" />
          <path d={STRIP.fast} fill="none" stroke="rgba(234,181,79,0.75)" strokeWidth="1.6" />
        </motion.g>
      </svg>
    </div>
  );
}
