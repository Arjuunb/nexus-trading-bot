import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Radio, ScanSearch, BrainCircuit, ShieldCheck, Zap, Target } from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { cn } from "@/lib/utils";
import { useVisibleActive } from "@/lib/useVisibleActive";

/**
 * "Watch Nexus take a trade" — the third landing animation. A looping,
 * self-driving visualization of one real decision cycle: candles stream in, a
 * setup is detected, the Decision Brain scores it, the risk gate sizes it, the
 * order fills, and the trade is managed to its take-profit — the steps light up
 * in sync with the price reaching entry → target. Illustrative sample data.
 */

// Price path (x: 6→298, y inverted — smaller y = higher price). A liquidity
// sweep + reclaim, entry near the middle, then a managed push to target.
const PTS: [number, number][] = [
  [6, 112], [18, 107], [30, 113], [42, 108], [54, 115], [66, 104], [78, 111], [90, 121],
  [102, 118], [114, 112], [126, 108], [138, 99], [150, 103], [162, 91], [174, 95], [186, 83],
  [198, 87], [210, 75], [222, 69], [234, 73], [246, 61], [258, 57], [270, 49], [284, 51], [298, 44],
];
const ENTRY_I = 9;                 // entry candle index → progress ≈ 0.375
const ENTRY_X = PTS[ENTRY_I][0];
const ENTRY_Y = PTS[ENTRY_I][1];
const STOP_Y = 128;
const TARGET_Y = PTS[PTS.length - 1][1];
const D = "M " + PTS.map(([x, y]) => `${x},${y}`).join(" L ");

const STEPS = [
  { at: 0.04, icon: Radio, tag: "feed", tone: "dim", text: "Streaming BTC/USDT · 1H candles" },
  { at: 0.22, icon: ScanSearch, tag: "scan", tone: "dim", text: "Setup — bullish BOS + demand-zone retest" },
  { at: 0.34, icon: BrainCircuit, tag: "brain", tone: "gold", text: "Decision Brain — score 87 / 100 ✓" },
  { at: 0.42, icon: ShieldCheck, tag: "risk", tone: "emerald", text: "Risk gate — size 0.12 · stop 1.2% · target 2R" },
  { at: 0.50, icon: Zap, tag: "exec", tone: "emerald", text: "BUY 0.12 BTC @ $60,240" },
  { at: 0.96, icon: Target, tag: "exit", tone: "emerald", text: "Take-profit hit · +2.4R · +$412" },
] as const;

const TONE: Record<string, string> = {
  dim: "border-line-strong bg-white/[0.03] text-white/55",
  gold: "border-gold/30 bg-gold/[0.08] text-gold",
  emerald: "border-emerald/30 bg-emerald/[0.08] text-emerald",
};
const GOLD = "#C8A94B", EMERALD = "#2FBF71", RED = "#E5605B", LINE = "rgba(255,255,255,0.14)";

function dotAt(p: number): [number, number] {
  const f = p * (PTS.length - 1);
  const i = Math.min(PTS.length - 2, Math.floor(f));
  const t = f - i;
  return [PTS[i][0] + (PTS[i + 1][0] - PTS[i][0]) * t, PTS[i][1] + (PTS[i + 1][1] - PTS[i][1]) * t];
}

export function TradeInAction() {
  const [p, setP] = useState(0);
  const raf = useRef<number | null>(null);
  const start = useRef<number | null>(null);
  const held = useRef(0);                  // cycle position kept across pauses
  const root = useRef<HTMLElement | null>(null);
  const active = useVisibleActive(root);
  const DURATION = 7600;   // ms per full cycle
  const HOLD = 0.14;       // fraction of the cycle held at the end before looping

  useEffect(() => {
    // The old loop kept scheduling frames while the tab was hidden and ran
    // regardless of scroll position; the document.hidden branch also reset the
    // baseline every frame, so returning to the tab restarted the cycle. Gating
    // on visibility stops the work outright and resumes where it left off.
    if (!active) return;
    start.current = null;
    const tick = (ts: number) => {
      if (start.current === null) start.current = ts - held.current;
      held.current = (ts - start.current) % DURATION;
      setP(Math.min(1, (held.current / DURATION) / (1 - HOLD)));       // reach 1, then hold
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); };
  }, [active]);

  const entered = p >= STEPS[3].at;
  const [dx, dy] = dotAt(p);
  const done = p >= 0.99;

  return (
    <section id="trade-in-action" className="section" ref={root}>
      <div className="container-x">
        <SectionHeading
          link="/live-trade"
          eyebrow="How it trades"
          title="Watch Nexus take a trade"
          subtitle="One decision cycle, start to finish — a setup is spotted, scored, risk-sized, executed, and managed to target. Every step mirrors the engine's logic, shown here with sample data."
        />

        <div className="mt-14 grid gap-6 lg:grid-cols-[1.15fr_1fr] lg:gap-10">
          {/* animated chart */}
          <Reveal>
            <div className="rounded-2xl border border-line-strong bg-ink-800/40 p-4 sm:p-6">
              <div className="mb-3 flex items-center justify-between text-xs text-white/45">
                <span className="font-medium text-white/70">BTC/USDT · 1H · paper · sample data</span>
                <span className={cn("rounded-full border px-2 py-0.5 font-mono transition-colors",
                  done ? "border-emerald/40 text-emerald" : "border-line-strong text-white/50")}>
                  {done ? "closed +2.4R" : entered ? "in trade" : "scanning"}
                </span>
              </div>
              <svg viewBox="0 0 304 150" className="w-full" style={{ aspectRatio: "304 / 150" }}>
                {/* target / entry / stop levels (appear at entry) */}
                <motion.g initial={false} animate={{ opacity: entered ? 1 : 0 }} transition={{ duration: 0.4 }}>
                  <line x1={ENTRY_X} y1={TARGET_Y} x2={304} y2={TARGET_Y} stroke={EMERALD} strokeWidth={1} strokeDasharray="3 3" opacity={0.55} />
                  <line x1={ENTRY_X} y1={ENTRY_Y} x2={304} y2={ENTRY_Y} stroke={GOLD} strokeWidth={1} strokeDasharray="3 3" opacity={0.6} />
                  <line x1={ENTRY_X} y1={STOP_Y} x2={304} y2={STOP_Y} stroke={RED} strokeWidth={1} strokeDasharray="3 3" opacity={0.5} />
                  <text x={300} y={TARGET_Y - 3} textAnchor="end" fill={EMERALD} fontSize={7} className="font-mono">TP</text>
                  <text x={300} y={STOP_Y - 3} textAnchor="end" fill={RED} fontSize={7} className="font-mono">SL</text>
                </motion.g>
                {/* the price line, revealed as the trade progresses */}
                <path d={D} fill="none" stroke={LINE} strokeWidth={1} />
                <path d={D} fill="none" stroke={done ? EMERALD : GOLD} strokeWidth={2}
                  pathLength={1} strokeDasharray={1} strokeDashoffset={1 - p}
                  strokeLinecap="round" strokeLinejoin="round" style={{ transition: "stroke 0.4s" }} />
                {/* entry marker */}
                <motion.g initial={false} animate={{ opacity: entered ? 1 : 0, scale: entered ? 1 : 0.6 }}
                  transition={{ type: "spring", stiffness: 260, damping: 18 }} style={{ transformOrigin: `${ENTRY_X}px ${ENTRY_Y}px` }}>
                  <circle cx={ENTRY_X} cy={ENTRY_Y} r={3.5} fill={GOLD} />
                  <circle cx={ENTRY_X} cy={ENTRY_Y} r={6} fill="none" stroke={GOLD} strokeWidth={1} opacity={0.5} />
                </motion.g>
                {/* live price dot */}
                <circle cx={dx} cy={dy} r={3} fill={done ? EMERALD : "#fff"} />
                <circle cx={dx} cy={dy} r={7} fill={done ? EMERALD : "#fff"} opacity={0.18} />
              </svg>
            </div>
          </Reveal>

          {/* synchronized steps */}
          <Reveal delay={0.1}>
            <ol className="flex flex-col gap-2.5">
              {STEPS.map((s, i) => {
                const active = p >= s.at;
                const Icon = s.icon;
                return (
                  <motion.li key={s.tag}
                    initial={false}
                    animate={{ opacity: active ? 1 : 0.35, x: active ? 0 : -6 }}
                    transition={{ duration: 0.3 }}
                    className={cn("flex items-center gap-3 rounded-xl border px-3.5 py-3 transition-colors",
                      active ? TONE[s.tone] : "border-line-strong bg-transparent text-white/40")}>
                    <span className={cn("grid h-8 w-8 shrink-0 place-items-center rounded-lg border",
                      active ? "border-current/30" : "border-line-strong")}>
                      <Icon className="h-4 w-4" />
                    </span>
                    <div className="min-w-0">
                      <div className="font-mono text-[10px] uppercase tracking-wider opacity-60">{s.tag}</div>
                      <div className="truncate text-[13px] font-medium">{s.text}</div>
                    </div>
                    {active && i === STEPS.length - 1 && (
                      <span className="ml-auto shrink-0 text-xs font-semibold">win</span>
                    )}
                  </motion.li>
                );
              })}
            </ol>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
