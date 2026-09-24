import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { cn } from "@/lib/utils";
import { useVisibleActive } from "@/lib/useVisibleActive";

/**
 * "Selectivity" — the fourth landing animation. Nexus scans the whole
 * watchlist, scores each symbol, and skips almost everything: an animated
 * scanner runs down the list (score bar fills, TAKE / SKIP verdict lands), while
 * an illustrative equity curve draws in beside it. Reinforces the core promise —
 * fewer, higher-quality trades. Sample data, clearly labelled.
 */

const ROWS = [
  { sym: "BTC/USDT", score: 87, take: true },
  { sym: "ETH/USDT", score: 44, take: false },
  { sym: "SOL/USDT", score: 71, take: true },
  { sym: "XRP/USDT", score: 38, take: false },
  { sym: "DOGE/USDT", score: 29, take: false },
  { sym: "LINK/USDT", score: 52, take: false },
];
const GOLD = "#C8A94B", EMERALD = "#2FBF71";

// illustrative equity curve (x 4→300, y inverted — up and to the right, with dips)
const EQ = [
  [4, 118], [30, 112], [52, 116], [78, 100], [104, 106], [128, 88], [152, 94],
  [178, 74], [200, 82], [226, 60], [250, 66], [276, 46], [300, 40],
];
const EQ_D = "M " + EQ.map(([x, y]) => `${x},${y}`).join(" L ");
const EQ_AREA = EQ_D + ` L 300,130 L 4,130 Z`;

export function MarketScanner() {
  const [step, setStep] = useState(-1);
  const timer = useRef<number | null>(null);
  const root = useRef<HTMLElement | null>(null);
  // Only step while the section is on screen and the tab is in front. This
  // used to tick every 850ms for the life of the page, re-rendering a table
  // nobody was looking at.
  const active = useVisibleActive(root);

  useEffect(() => {
    if (!active) return;
    const total = ROWS.length + 2;                 // rows + a short pause before looping
    timer.current = window.setInterval(() => setStep((s) => (s + 1) % total), 850);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [active]);

  const scannedTake = ROWS.filter((r, i) => i <= step && r.take).length;
  const scannedAll = Math.min(step + 1, ROWS.length);

  return (
    <section id="selectivity" className="section" ref={root}>
      <div className="container-x">
        <SectionHeading
          link="/selectivity"
          eyebrow="Selectivity"
          title="Watches everything. Trades almost nothing."
          subtitle="Every candle, Nexus scores your whole watchlist and takes only the setups that clear its quality bar — discipline you can't override on a bad day."
        />

        <div className="mt-14 grid gap-6 lg:grid-cols-[1fr_1fr] lg:gap-10">
          {/* scanner */}
          <Reveal>
            <div className="rounded-2xl border border-line-strong bg-ink-800/40 p-4 sm:p-6">
              <div className="mb-4 flex items-center justify-between text-xs text-white/45">
                <span className="font-medium text-white/70">Watchlist scan · demo</span>
                <span className="font-mono">{scannedTake} of {scannedAll || 0} taken</span>
              </div>
              <ol className="flex flex-col gap-2">
                {ROWS.map((r, i) => {
                  const scanned = i <= step;
                  const scanning = i === step;
                  return (
                    <li key={r.sym}
                      className={cn("flex items-center gap-3 rounded-xl border px-3 py-2.5 transition-colors duration-300",
                        scanning ? "border-white/25 bg-white/[0.05]"
                          : scanned ? "border-line-strong bg-white/[0.02]" : "border-line-strong bg-transparent opacity-45")}>
                      <span className="w-24 shrink-0 font-mono text-[13px] text-white/80">{r.sym}</span>
                      <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-white/[0.06]">
                        <motion.span className="absolute inset-y-0 left-0 rounded-full"
                          style={{ background: r.take ? EMERALD : GOLD, opacity: r.take ? 1 : 0.5 }}
                          initial={false}
                          animate={{ width: scanned ? `${r.score}%` : "0%" }}
                          transition={{ duration: 0.55, ease: "easeOut" }} />
                      </div>
                      <span className="w-8 shrink-0 text-right font-mono text-[12px] text-white/55">{scanned ? r.score : "—"}</span>
                      <span className={cn("w-14 shrink-0 rounded-md border px-1.5 py-0.5 text-center text-[10px] font-bold tracking-wide transition-opacity",
                        !scanned ? "opacity-0 border-line-strong"
                          : r.take ? "border-emerald/40 bg-emerald/10 text-emerald" : "border-line-strong bg-white/[0.03] text-white/45")}>
                        {r.take ? "TAKE" : "SKIP"}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </div>
          </Reveal>

          {/* illustrative equity curve */}
          <Reveal delay={0.1}>
            <div className="flex h-full flex-col rounded-2xl border border-line-strong bg-ink-800/40 p-4 sm:p-6">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-sm font-semibold text-white/80">Compounding the good ones</span>
                <span className="rounded-full border border-emerald/30 bg-emerald/10 px-2 py-0.5 text-[10px] font-mono text-emerald">equity ↗</span>
              </div>
              <p className="mb-3 text-xs text-white/40">Illustrative simulated equity — not a performance guarantee.</p>
              <svg viewBox="0 0 304 134" className="mt-auto w-full" style={{ aspectRatio: "304 / 134" }}>
                <defs>
                  <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={EMERALD} stopOpacity="0.28" />
                    <stop offset="100%" stopColor={EMERALD} stopOpacity="0" />
                  </linearGradient>
                </defs>
                {[40, 70, 100].map((y) => <line key={y} x1={4} y1={y} x2={300} y2={y} stroke="rgba(255,255,255,0.06)" strokeWidth={1} />)}
                <motion.path d={EQ_AREA} fill="url(#eqfill)"
                  initial={{ opacity: 0 }} whileInView={{ opacity: 1 }} viewport={{ once: true }} transition={{ delay: 0.8, duration: 0.6 }} />
                <motion.path d={EQ_D} fill="none" stroke={EMERALD} strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round"
                  initial={{ pathLength: 0 }} whileInView={{ pathLength: 1 }} viewport={{ once: true }} transition={{ duration: 1.6, ease: "easeInOut" }} />
                <motion.circle cx={300} cy={40} r={3.5} fill={EMERALD}
                  initial={{ opacity: 0 }} whileInView={{ opacity: 1 }} viewport={{ once: true }} transition={{ delay: 1.5, duration: 0.3 }} />
              </svg>
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
