import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Terminal } from "lucide-react";
import { cn } from "@/lib/utils";
import { useVisibleActive } from "@/lib/useVisibleActive";

/**
 * Engine log terminal — a monospace, rolling feed of the kind of decisions the
 * Nexus actually makes (feed → structure → brain score → risk gate → execution →
 * memory). Sample data, clearly labelled a preview. This is the element that
 * makes the page read as a working automation engine, not a brochure.
 */

type Tone = "dim" | "gold" | "emerald" | "loss";

interface LogLine {
  tag: string;
  tone: Tone;
  text: string;
}

// A realistic decision cycle, in order. It loops — one honest pass of the engine.
const POOL: LogLine[] = [
  { tag: "feed", tone: "dim", text: "BTCUSDT 4h candle closed · O 43,780 H 43,960 C 43,912" },
  { tag: "scan", tone: "dim", text: "market structure: higher-high confirmed · trend = up" },
  { tag: "brain", tone: "gold", text: "quality 72/100 · acceptable · rr 2.4 · htf aligned" },
  { tag: "risk", tone: "emerald", text: "risk gate passed · size 0.8% · stop 43,120 · tp 45,650" },
  { tag: "exec", tone: "emerald", text: "LONG BTCUSDT filled @ 43,912 · mode=paper · id 0x8f2a" },
  { tag: "feed", tone: "dim", text: "ETHUSDT 4h candle closed · O 2,270 H 2,291 C 2,284" },
  { tag: "brain", tone: "loss", text: "quality 41/100 · weak · below the 60 minimum · skip" },
  { tag: "risk", tone: "dim", text: "SOLUSDT signal · max open positions (3) reached · hold" },
  { tag: "exec", tone: "emerald", text: "BTCUSDT take-profit hit · +2.4R realized · closed" },
  { tag: "mem", tone: "gold", text: "trade stored to memory · pattern trend-long · London" },
];

const TAG_COLOR: Record<Tone, string> = {
  dim: "text-white/40",
  gold: "text-gold-soft",
  emerald: "text-emerald-soft",
  loss: "text-loss-soft",
};

const CLOCK = ["09:04:12", "09:04:19", "09:05:03", "09:05:04", "09:05:05",
  "09:08:00", "09:08:11", "09:09:37", "10:22:41", "10:22:42"];

export function LiveTerminal({ className }: { className?: string }) {
  const [lines, setLines] = useState<{ id: number; line: LogLine; ts: string }[]>([]);
  const idx = useRef(0);
  const uid = useRef(0);
  const root = useRef<HTMLDivElement | null>(null);
  const active = useVisibleActive(root);

  const push = useCallback(() => {
    const i = idx.current % POOL.length;
    setLines((prev) => {
      const next = [...prev, { id: uid.current++, line: POOL[i], ts: CLOCK[i] }];
      return next.slice(-7);
    });
    idx.current += 1;
  }, []);

  // Seed on mount, always. The terminal must never render empty — including
  // for someone who prefers reduced motion, or while it is off screen and the
  // roll below is paused.
  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    for (let s = 0; s < (reduce ? 7 : 4); s++) push();
  }, [push]);

  useEffect(() => {
    if (!active) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return;                    // seeded above; a static log is the point
    const iv = window.setInterval(push, 1900);
    return () => window.clearInterval(iv);
  }, [active, push]);

  return (
    <div ref={root} className={cn("surface overflow-hidden", className)}>
      {/* terminal header */}
      <div className="flex items-center justify-between border-b border-line bg-ink-800/60 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Terminal className="h-3.5 w-3.5 text-gold/70" />
          <span className="font-mono text-[12px] text-white/70">engine.log</span>
          <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-white/40">
            preview
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald opacity-60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald" />
          </span>
          <span className="font-mono text-[10px] uppercase tracking-wider text-emerald-soft">live</span>
        </div>
      </div>

      {/* log body */}
      <div className="h-[15.5rem] space-y-1 overflow-hidden p-4 font-mono text-[12px] leading-relaxed">
        <AnimatePresence initial={false}>
          {lines.map(({ id, line, ts }) => (
            <motion.div
              key={id}
              layout
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.25 }}
              className="flex items-start gap-2.5 whitespace-nowrap"
            >
              <span className="shrink-0 text-white/25">{ts}</span>
              <span className={cn("w-11 shrink-0 uppercase", TAG_COLOR[line.tone])}>{line.tag}</span>
              <span className="min-w-0 truncate text-white/70">{line.text}</span>
            </motion.div>
          ))}
        </AnimatePresence>
        <div className="flex items-center gap-2 pt-0.5">
          <span className="text-gold/70">›</span>
          <span className="inline-block h-3.5 w-1.5 animate-pulse bg-gold/60" />
        </div>
      </div>
    </div>
  );
}
