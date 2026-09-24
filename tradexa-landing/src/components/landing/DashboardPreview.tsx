import { motion } from "framer-motion";
import { Activity, ArrowUpRight, ArrowDownRight, Cpu, Signal } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";

/**
 * Animated product preview of the running Nexus dashboard. This is a DESIGN DEMO
 * with representative sample data — it is explicitly labelled as a preview and
 * never presented as a live trading account or a real track record.
 */

// A deterministic, hand-authored equity path (demo shape, not real returns).
const EQUITY = [8, 22, 18, 34, 30, 46, 42, 58, 55, 70, 66, 82, 88];
const W = 320;
const H = 96;
const points = EQUITY.map((v, i) => {
  const x = (i / (EQUITY.length - 1)) * W;
  const y = H - (v / 100) * H;
  return [x, y] as const;
});
const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
const areaPath = `${linePath} L${W},${H} L0,${H} Z`;

const TRADES = [
  { sym: "BTC/USDT", side: "LONG", pnl: "+2.4R", up: true },
  { sym: "ETH/USDT", side: "LONG", pnl: "+1.1R", up: true },
  { sym: "SOL/USDT", side: "SHORT", pnl: "-0.6R", up: false },
];

const FEED = [
  { t: "Structure shift confirmed on 4H", tone: "gold" as const },
  { t: "Long setup graded A — executed", tone: "emerald" as const },
  { t: "Risk check passed · 0.8% sized", tone: "neutral" as const },
];

export function DashboardPreview() {
  return (
    <div className="relative">
      {/* Soft glow behind the panel. The inset is smaller than the page gutter
          on phones — at -inset-6 the glow reached 4px past the viewport and gave
          the whole landing page a horizontal scrollbar. */}
      <div
        aria-hidden
        className="pointer-events-none absolute -inset-4 -z-10 rounded-[2rem] bg-gold/[0.07] blur-3xl sm:-inset-6"
      />

      <motion.div
        initial={{ opacity: 0, y: 30, rotateX: 8 }}
        animate={{ opacity: 1, y: 0, rotateX: 0 }}
        transition={{ duration: 0.9, ease: [0.22, 1, 0.36, 1], delay: 0.2 }}
        className="glass-strong rounded-2xl p-4 shadow-card"
        style={{ perspective: 1000 }}
      >
        {/* window chrome */}
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-loss/70" />
            <span className="h-2.5 w-2.5 rounded-full bg-gold/70" />
            <span className="h-2.5 w-2.5 rounded-full bg-emerald/70" />
          </div>
          <Badge tone="neutral" className="gap-1.5">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald opacity-60" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald" />
            </span>
            Live preview · demo data
          </Badge>
        </div>

        {/* engine control line */}
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-lg border border-line bg-ink-800/50 px-3 py-2 font-mono text-[11px]">
          <span className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald" />
            <span className="text-emerald-soft">ENGINE RUNNING</span>
          </span>
          <span className="text-white/30">·</span>
          <span className="text-gold-soft">PAPER</span>
          <span className="text-white/30">·</span>
          <span className="text-white/50">BTC · ETH · SOL</span>
          <span className="text-white/30">·</span>
          <span className="text-white/50">4h</span>
          <span className="ml-auto text-white/35">nexus v4</span>
        </div>

        {/* equity curve */}
        <div className="surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <p className="text-[11px] uppercase tracking-wider text-white/40">Equity Curve</p>
              <p className="tabular text-xl font-semibold text-white">
                +18.4<span className="text-sm text-emerald">%</span>
              </p>
            </div>
            <Badge tone="emerald">
              <ArrowUpRight className="h-3 w-3" /> Trending
            </Badge>
          </div>
          <svg viewBox={`0 0 ${W} ${H}`} className="h-24 w-full" preserveAspectRatio="none">
            <defs>
              <linearGradient id="eq-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#2FBF71" stopOpacity="0.35" />
                <stop offset="1" stopColor="#2FBF71" stopOpacity="0" />
              </linearGradient>
            </defs>
            <motion.path
              d={areaPath}
              fill="url(#eq-fill)"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 1.1, duration: 0.6 }}
            />
            <motion.path
              d={linePath}
              fill="none"
              stroke="#4FD98E"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              initial={{ pathLength: 0 }}
              animate={{ pathLength: 1 }}
              transition={{ delay: 0.5, duration: 1.4, ease: "easeInOut" }}
            />
          </svg>
        </div>

        {/* stat row */}
        <div className="mt-3 grid grid-cols-3 gap-3">
          <MiniStat icon={Signal} label="Win Rate" value="61%" tone="gold" />
          <MiniStat icon={Activity} label="Active" value="3" tone="emerald" />
          <MiniStat icon={Cpu} label="Latency" value="82ms" tone="neutral" />
        </div>

        {/* trades + feed */}
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div className="surface p-3">
            <p className="mb-2 text-[11px] uppercase tracking-wider text-white/40">Active Trades</p>
            <div className="space-y-2">
              {TRADES.map((t) => (
                <div key={t.sym} className="flex items-center justify-between text-[12px]">
                  <span className="font-medium text-white/80">{t.sym}</span>
                  <span
                    className={cn(
                      "flex items-center gap-0.5 font-mono",
                      t.up ? "text-emerald-soft" : "text-loss-soft",
                    )}
                  >
                    {t.up ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
                    {t.pnl}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="surface p-3">
            <p className="mb-2 text-[11px] uppercase tracking-wider text-white/40">AI Decision Feed</p>
            <div className="space-y-2">
              {FEED.map((f, i) => (
                <motion.div
                  key={f.t}
                  initial={{ opacity: 0, x: 8 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 1.3 + i * 0.25 }}
                  className="flex items-start gap-1.5 text-[11px] leading-tight text-white/70"
                >
                  <span
                    className={cn(
                      "mt-1 h-1.5 w-1.5 shrink-0 rounded-full",
                      f.tone === "gold" && "bg-gold",
                      f.tone === "emerald" && "bg-emerald",
                      f.tone === "neutral" && "bg-white/40",
                    )}
                  />
                  {f.t}
                </motion.div>
              ))}
            </div>
          </div>
        </div>

        {/* risk meter */}
        <div className="surface mt-3 p-3">
          <div className="mb-1.5 flex items-center justify-between text-[11px]">
            <span className="uppercase tracking-wider text-white/40">Risk Meter</span>
            <span className="text-emerald-soft">Protected · 0.9% of 3% daily</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-white/10">
            <motion.div
              className="h-full rounded-full bg-gradient-to-r from-emerald via-gold to-loss"
              initial={{ width: 0 }}
              animate={{ width: "30%" }}
              transition={{ delay: 1, duration: 1, ease: "easeOut" }}
            />
          </div>
        </div>
      </motion.div>

      {/* floating chips — anchored to sit OUTSIDE the panel (above / below-right)
          so they never overlap the dashboard content. The anchor offset and
          the float are on separate elements: framer-motion writes its own
          inline transform for `y`, which replaced the Tailwind translate when
          both sat on one element and dropped the chips onto the panel. */}
      <div className="absolute left-4 top-0 hidden -translate-y-[135%] lg:block">
        <motion.div
          animate={{ y: [0, -8, 0] }}
          transition={{ duration: 5, repeat: Infinity, ease: "easeInOut" }}
          className="glass-strong flex items-center gap-2 rounded-xl px-3 py-2 shadow-card"
        >
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full rounded-full bg-emerald opacity-60 motion-safe:animate-ping" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald" />
          </span>
          <span className="text-[11px] text-white/80">Market Status · Open</span>
        </motion.div>
      </div>
      <div className="absolute -right-4 bottom-0 hidden translate-y-[60%] lg:block">
        <motion.div
          animate={{ y: [0, 10, 0] }}
          transition={{ duration: 6, repeat: Infinity, ease: "easeInOut", delay: 1 }}
          className="glass-strong rounded-xl px-3 py-2 shadow-card"
        >
          <p className="text-[10px] uppercase tracking-wider text-white/40">Execution</p>
          <p className="tabular text-sm font-semibold text-gold-soft">&lt; 100ms</p>
        </motion.div>
      </div>
    </div>
  );
}

function MiniStat({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof Signal;
  label: string;
  value: string;
  tone: "gold" | "emerald" | "neutral";
}) {
  return (
    <div className="surface p-2.5">
      <Icon
        className={cn(
          "mb-1 h-3.5 w-3.5",
          tone === "gold" && "text-gold",
          tone === "emerald" && "text-emerald",
          tone === "neutral" && "text-white/50",
        )}
      />
      <p className="tabular text-base font-semibold text-white">{value}</p>
      <p className="text-[10px] uppercase tracking-wider text-white/40">{label}</p>
    </div>
  );
}
