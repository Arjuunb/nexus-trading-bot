import { motion } from "framer-motion";

/**
 * Feature-card hover mini-demos — small, hand-authored micro-illustrations that
 * teach what each capability actually does. They are REPRESENTATIVE diagrams,
 * not live data or account figures: no numbers here are pulled from a real
 * account, and none claim to be. Each plays only while its card is hovered
 * (`play`) and renders a static end-state under reduced-motion.
 */

export type DemoKind = "score" | "risk" | "equity" | "feed" | "memory";

interface DemoProps { play: boolean; reduced: boolean }

const EASE = [0.16, 1, 0.3, 1] as const;

/** Nexus Engine — scores every setup, acts only above threshold (yes AND no). */
function ScoreDemo({ play, reduced }: DemoProps) {
  const on = play || reduced;
  const bars = [
    { label: "ETH · retest", score: 82, act: true },
    { label: "SOL · chop", score: 41, act: false },
  ];
  return (
    <div className="flex flex-col gap-2.5">
      {bars.map((b, i) => (
        <div key={b.label} className="flex items-center gap-2.5">
          <span className="w-20 shrink-0 truncate font-mono text-[10.5px] text-white/45">{b.label}</span>
          <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-white/[0.06]">
            {/* threshold marker at 70 */}
            <span className="absolute inset-y-0 z-10 w-px bg-white/25" style={{ left: "70%" }} aria-hidden />
            <motion.span
              className={b.act ? "block h-full rounded-full bg-gold" : "block h-full rounded-full bg-white/25"}
              initial={reduced ? false : { width: 0 }}
              animate={{ width: on ? `${b.score}%` : 0 }}
              transition={{ duration: 0.7, ease: EASE, delay: i * 0.12 }}
            />
          </div>
          <span className={`w-10 shrink-0 text-right font-mono text-[10.5px] ${b.act ? "text-gold" : "text-white/40"}`}>
            {b.act ? "act" : "skip"}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Risk Management — every entry brackets a stop and a target before exposure. */
function RiskDemo({ play, reduced }: DemoProps) {
  const on = play || reduced;
  const rows = [
    { label: "Take-profit", pct: "+2.0R", cls: "text-emerald-soft", pos: "top-0", bar: "bg-emerald/50" },
    { label: "Entry", pct: "0.42 BTC", cls: "text-white/70", pos: "top-1/2 -translate-y-1/2", bar: "bg-gold/60" },
    { label: "Stop-loss", pct: "-1.0R", cls: "text-loss", pos: "bottom-0", bar: "bg-loss/50" },
  ];
  return (
    <div className="relative h-[64px]">
      {rows.map((r, i) => (
        <motion.div
          key={r.label}
          className={`absolute inset-x-0 flex items-center gap-2 ${r.pos}`}
          initial={reduced ? false : { opacity: 0, x: -6 }}
          animate={{ opacity: on ? 1 : 0, x: on ? 0 : -6 }}
          transition={{ duration: 0.4, ease: EASE, delay: i * 0.1 }}
        >
          <span className={`h-px flex-1 ${r.bar}`} style={{ borderTop: "1px dashed currentColor" }} />
          <span className="flex items-center gap-2 font-mono text-[10.5px]">
            <span className="text-white/45">{r.label}</span>
            <span className={r.cls}>{r.pct}</span>
          </span>
        </motion.div>
      ))}
    </div>
  );
}

/** Strategy Lab — an illustrative backtest equity curve drawing in. */
function EquityDemo({ play, reduced }: DemoProps) {
  const on = play || reduced;
  // a representative up-and-to-the-right curve (not real backtest output)
  const d = "M2,44 L14,40 L26,42 L38,34 L50,36 L62,26 L74,28 L86,18 L98,20 L110,10";
  return (
    <div className="relative">
      <svg viewBox="0 0 112 48" className="h-[56px] w-full" preserveAspectRatio="none">
        <line x1="0" y1="47" x2="112" y2="47" stroke="rgba(255,255,255,0.08)" strokeWidth="1" />
        <motion.path
          d={d}
          fill="none"
          stroke="#22C55E"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={reduced ? false : { pathLength: 0 }}
          animate={{ pathLength: on ? 1 : 0 }}
          transition={{ duration: 1.1, ease: EASE }}
        />
      </svg>
      <span className="absolute right-0 top-0 font-mono text-[10px] text-white/40">illustrative curve</span>
    </div>
  );
}

/** Intelligence Feed — representative log lines streaming in. */
function FeedDemo({ play, reduced }: DemoProps) {
  const on = play || reduced;
  const lines = [
    { t: "scan BTCUSDT · 5m", c: "text-white/45" },
    { t: "signal → LONG · conf 0.82", c: "text-gold" },
    { t: "filled 0.42 @ 64,981", c: "text-white/70" },
  ];
  return (
    <div className="flex flex-col gap-1 font-mono text-[10.5px]">
      {lines.map((l, i) => (
        <motion.div
          key={l.t}
          className="flex items-center gap-1.5"
          initial={reduced ? false : { opacity: 0, y: 4 }}
          animate={{ opacity: on ? 1 : 0, y: on ? 0 : 4 }}
          transition={{ duration: 0.35, ease: EASE, delay: i * 0.18 }}
        >
          <span className="text-gold">›</span>
          <span className={l.c}>{l.t}</span>
        </motion.div>
      ))}
    </div>
  );
}

/** Trading Memory — a completed trade distilled into a lesson. */
function MemoryDemo({ play, reduced }: DemoProps) {
  const on = play || reduced;
  return (
    <motion.div
      className="rounded-lg border border-line bg-white/[0.02] p-3"
      initial={reduced ? false : { opacity: 0, rotateX: -12, y: 6 }}
      animate={{ opacity: on ? 1 : 0, rotateX: on ? 0 : -12, y: on ? 0 : 6 }}
      transition={{ duration: 0.5, ease: EASE }}
      style={{ transformPerspective: 600 }}
    >
      <div className="flex items-center justify-between font-mono text-[10.5px]">
        <span className="text-loss">Loss · −0.8R</span>
        <span className="text-white/35">SOL · 15m</span>
      </div>
      <p className="mt-1.5 text-[11px] leading-snug text-white/60">
        <span className="text-white/40">Lesson: </span>chased the entry — wait for the retest next time.
      </p>
    </motion.div>
  );
}

const MAP: Record<DemoKind, (p: DemoProps) => JSX.Element> = {
  score: ScoreDemo,
  risk: RiskDemo,
  equity: EquityDemo,
  feed: FeedDemo,
  memory: MemoryDemo,
};

export function FeatureDemo({ kind, ...props }: { kind: DemoKind } & DemoProps) {
  const Demo = MAP[kind];
  return <Demo {...props} />;
}
