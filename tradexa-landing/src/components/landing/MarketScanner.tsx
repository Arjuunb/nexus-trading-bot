import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { cn } from "@/lib/utils";
import { useVisibleActive } from "@/lib/useVisibleActive";

/**
 * "Selectivity": Nexus scans the whole watchlist, scores each symbol, and
 * skips almost everything. A scanner runs down the list (score bar fills, TAKE
 * or SKIP lands) while the panel beside it names the gate that stopped each
 * skipped symbol. The gates are the engine's real ones; the symbols and scores
 * are an example, labelled as such. No P&L or equity is shown.
 */

type Gate = "quality" | "trend" | "regime" | "exposure";

const ROWS: { sym: string; score: number; take: boolean; gate?: Gate }[] = [
  { sym: "BTC/USDT", score: 87, take: true },
  { sym: "ETH/USDT", score: 44, take: false, gate: "quality" },
  { sym: "SOL/USDT", score: 71, take: true },
  { sym: "XRP/USDT", score: 58, take: false, gate: "trend" },
  { sym: "DOGE/USDT", score: 29, take: false, gate: "regime" },
  { sym: "LINK/USDT", score: 66, take: false, gate: "exposure" },
];

const GATES: { key: Gate | "event"; name: string; detail: string }[] = [
  { key: "quality", name: "Quality below the minimum", detail: "Score under 60, the default bar." },
  { key: "trend", name: "Against the higher timeframe", detail: "Entry fights the trend it is meant to follow." },
  { key: "regime", name: "Choppy regime", detail: "Ranging market blocks non-reversal entries." },
  { key: "exposure", name: "Exposure or correlation limit", detail: "Enough risk already open in that direction." },
  { key: "event", name: "Event blackout", detail: "A high-impact release is minutes away." },
];
const GOLD = "#EAB54F";

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
  const current = step >= 0 && step < ROWS.length ? ROWS[step] : null;
  const firedGate = current && !current.take ? current.gate : null;

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
                          style={{ background: r.take ? GOLD : "rgba(255,255,255,0.28)" }}
                          initial={false}
                          animate={{ width: scanned ? `${r.score}%` : "0%" }}
                          transition={{ duration: 0.55, ease: "easeOut" }} />
                      </div>
                      <span className="w-8 shrink-0 text-right font-mono text-[12px] text-white/55">{scanned ? r.score : "—"}</span>
                      <span className={cn("w-14 shrink-0 rounded-md border px-1.5 py-0.5 text-center text-[10px] font-bold tracking-wide transition-opacity",
                        !scanned ? "opacity-0 border-line-strong"
                          : r.take ? "border-gold/40 bg-gold/10 text-gold" : "border-line-strong bg-white/[0.03] text-white/45")}>
                        {r.take ? "TAKE" : "SKIP"}
                      </span>
                    </li>
                  );
                })}
              </ol>
            </div>
          </Reveal>

          {/* the gate that stopped each skipped symbol */}
          <Reveal delay={0.1}>
            <div className="flex h-full flex-col rounded-2xl border border-line-strong bg-ink-800/40 p-4 sm:p-6">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-sm font-semibold text-white/80">Why a setup is skipped</span>
                <span className="font-mono text-[11px] text-white/40">
                  {firedGate ? `${current?.sym} stopped here` : current?.take ? `${current.sym} cleared every gate` : "scanning…"}
                </span>
              </div>
              <p className="mb-4 text-xs text-white/40">Every skip is logged with the rule that fired, and kept.</p>
              <ul className="flex flex-col gap-2">
                {GATES.map((g) => {
                  const fired = g.key === firedGate;
                  return (
                    <li key={g.key}
                      className={cn("flex items-start gap-3 rounded-xl border px-3 py-2.5 transition-colors duration-300",
                        fired ? "border-gold/40 bg-gold/[0.06]" : "border-line bg-transparent")}>
                      <span className={cn("mt-1 h-2 w-2 shrink-0 rounded-full transition-colors duration-300",
                        fired ? "bg-gold" : "bg-white/15")} aria-hidden />
                      <span>
                        <span className={cn("block text-[13px] transition-colors", fired ? "text-white" : "text-white/70")}>{g.name}</span>
                        <span className="block text-[12px] text-white/40">{g.detail}</span>
                      </span>
                    </li>
                  );
                })}
              </ul>
              <p className="mt-auto pt-4 font-mono text-[10.5px] text-white/30">example watchlist · the gates are the engine's own</p>
            </div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}
