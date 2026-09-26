import { useMemo } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

export type ScreenKind =
  | "decision"
  | "score"
  | "scanner"
  | "risk"
  | "equity"
  | "fill"
  | "memory"
  | "analytics"
  | "feed";

/**
 * Product screenshots, drawn rather than captured.
 *
 * A PNG of the app would be a megabyte, would go stale the first time a colour
 * token moved, and would sit at one fixed resolution inside a card that has to
 * survive a 360px phone. These are built from the same tokens as the product,
 * so they scale, they stay in sync, and they cost a few hundred bytes each.
 *
 * They depict representative state — never live data, and never a specific
 * account. Labels, factors and costs are the engine's real ones; the numbers
 * are examples, except the "equity" sketch, which carries the Decision Brain's
 * recorded test-set result from STRATEGY_VALIDATION_REPORT.md.
 */

const TITLES: Record<ScreenKind, string> = {
  decision: "engine · decision record",
  score: "engine · quality score",
  scanner: "scanner · on-demand scan",
  risk: "risk · checks",
  equity: "lab · untouched test set",
  fill: "execution · paper fill",
  memory: "memory · lessons",
  analytics: "analytics · attribution",
  feed: "feed · live events",
};

/** Chrome shared by every mock: a window bar with three dots and a path. */
function Frame({ kind, children }: { kind: ScreenKind; children: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-xl border border-line bg-ink-800/80 shadow-card">
      <div className="flex items-center gap-2 border-b border-line bg-white/[0.02] px-3 py-2">
        <span className="flex gap-1.5">
          <span className="h-2 w-2 rounded-full bg-white/15" />
          <span className="h-2 w-2 rounded-full bg-white/15" />
          <span className="h-2 w-2 rounded-full bg-white/15" />
        </span>
        <span className="truncate font-mono text-[10px] tracking-tight text-white/35">
          {TITLES[kind]}
        </span>
      </div>
      <div className="p-3">{children}</div>
    </div>
  );
}

function Row({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "gold" | "up" | "down" | "blue";
}) {
  const toneClass = {
    neutral: "text-white/70",
    gold: "text-gold-soft",
    up: "text-emerald-soft",
    down: "text-loss-soft",
    blue: "text-signal-soft",
  }[tone];
  return (
    <div className="flex items-baseline justify-between gap-3 py-[3px]">
      <span className="truncate text-[10px] text-white/35">{label}</span>
      <span className={cn("shrink-0 font-mono text-[11px] tabular", toneClass)}>{value}</span>
    </div>
  );
}

function Bar({ pct, tone = "gold" }: { pct: number; tone?: "gold" | "blue" | "up" | "down" }) {
  const bg = { gold: "bg-gold", blue: "bg-signal", up: "bg-emerald", down: "bg-loss" }[tone];
  return (
    <div className="h-1 w-full overflow-hidden rounded-full bg-white/[0.06]">
      <motion.div
        className={cn("h-full rounded-full", bg)}
        initial={{ width: 0 }}
        animate={{ width: `${pct}%` }}
        transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
      />
    </div>
  );
}

/** Deterministic pseudo-series so a mock looks the same on every render. */
function series(seed: number, n: number, drift: number) {
  const out: number[] = [];
  let v = 50;
  let s = seed;
  for (let i = 0; i < n; i++) {
    s = (s * 1103515245 + 12345) % 2147483648;
    v += ((s / 2147483648) - 0.45) * 9 + drift;
    out.push(v);
  }
  return out;
}

function Sparkline({ seed, drift, tone }: { seed: number; drift: number; tone: string }) {
  const pts = useMemo(() => {
    const s = series(seed, 44, drift);
    const min = Math.min(...s);
    const max = Math.max(...s);
    const span = max - min || 1;
    return s
      .map((v, i) => `${(i / (s.length - 1)) * 100},${34 - ((v - min) / span) * 30}`)
      .join(" ");
  }, [seed, drift]);

  return (
    <svg viewBox="0 0 100 36" preserveAspectRatio="none" className="h-16 w-full">
      <polyline points={pts} fill="none" stroke={tone} strokeWidth="1.1" vectorEffect="non-scaling-stroke" />
      <polyline
        points={`${pts} 100,36 0,36`}
        fill={tone}
        opacity="0.1"
        stroke="none"
      />
    </svg>
  );
}

export function ScreenMock({ kind, play = true }: { kind: ScreenKind; play?: boolean }) {
  const reduced = useReducedMotion() ?? false;
  const active = play && !reduced;

  return (
    <Frame kind={kind}>
      {kind === "decision" && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[11px] text-white/70">BTC/USDT · 4h</span>
            <span className="rounded-full border border-emerald/30 bg-emerald/10 px-2 py-0.5 font-mono text-[9px] text-emerald-soft">
              PAPER FILL
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-3">
            <Row label="quality" value="81" tone="gold" />
            <Row label="regime" value="trending" />
            <Row label="size" value="0.21 BTC" />
            <Row label="risk" value="0.25% eq" tone="blue" />
          </div>
          <div className="rounded-lg border border-line bg-black/30 p-2">
            <p className="text-[10px] leading-relaxed text-white/45">
              4h trend agrees, reward : risk 2.4, regime fits the setup. Risk halved to
              0.25% of equity — the last two trades were losses.
            </p>
          </div>
        </div>
      )}

      {kind === "score" && (
        <div className="space-y-1.5">
          {/* the Decision Brain's eight factors: points earned of the weight */}
          {[
            ["higher timeframe", 20, 22],
            ["regime fit", 15, 18],
            ["reward : risk", 10, 14],
            ["momentum", 8, 12],
            ["stop safety", 9, 10],
            ["volatility", 7, 10],
            ["structure", 6, 8],
            ["volume", 3, 6],
          ].map(([label, got, of]) => (
            <div key={label as string}>
              <div className="mb-0.5 flex items-center justify-between">
                <span className="text-[10px] text-white/40">{label as string}</span>
                <span className="font-mono text-[10px] tabular text-white/60">{got as number}/{of as number}</span>
              </div>
              <Bar pct={((got as number) / (of as number)) * 100}
                   tone={(got as number) / (of as number) >= 0.75 ? "gold" : "blue"} />
            </div>
          ))}
          <div className="mt-1 flex items-baseline justify-between border-t border-line pt-2">
            <span className="text-[10px] uppercase tracking-[0.16em] text-white/35">score · min 60</span>
            <span className="font-mono text-lg font-semibold tabular text-gold">78</span>
          </div>
        </div>
      )}

      {kind === "scanner" && (
        <div className="space-y-1">
          {[
            ["SOL/USDT", 84, "breakout", "up"],
            ["BTC/USDT", 79, "pullback", "up"],
            ["ETH/USDT", 61, "volume", "neutral"],
            ["DOGE/USDT", 48, "sweep", "down"],
            ["LINK/USDT", 33, "momentum", "down"],
          ].map(([sym, score, setup, tone], i) => (
            <motion.div
              key={sym as string}
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: active ? i * 0.05 : 0, duration: 0.3 }}
              className="flex items-center gap-2 rounded-md px-1.5 py-1 odd:bg-white/[0.02]"
            >
              <span className="w-3 font-mono text-[9px] text-white/25">{i + 1}</span>
              <span className="w-20 font-mono text-[10px] text-white/70">{sym as string}</span>
              <div className="flex-1">
                <Bar pct={score as number} tone={tone === "up" ? "gold" : tone === "down" ? "down" : "blue"} />
              </div>
              <span className="w-6 text-right font-mono text-[10px] tabular text-white/50">{score as number}</span>
              <span className="w-12 text-right font-mono text-[9px] text-white/25">{setup as string}</span>
            </motion.div>
          ))}
        </div>
      )}

      {kind === "risk" && (
        <div className="space-y-2">
          <div className="grid grid-cols-3 gap-2">
            {[
              ["daily", "-0.8%", "of -3.0%", 27],
              ["exposure", "6.2%", "of 10%", 62],
              ["positions", "2", "of 3", 67],
            ].map(([k, v, of, pct]) => (
              <div key={k as string} className="rounded-lg border border-line bg-black/25 p-2">
                <p className="text-[9px] uppercase tracking-wider text-white/30">{k as string}</p>
                <p className="mt-0.5 font-mono text-sm tabular text-white">{v as string}</p>
                <p className="mb-1.5 font-mono text-[9px] text-white/25">{of as string}</p>
                <Bar pct={pct as number} tone="blue" />
              </div>
            ))}
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-loss/25 bg-loss/[0.07] px-2 py-1.5">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-loss" />
            <span className="font-mono text-[10px] text-loss-soft">VETO</span>
            <span className="truncate text-[10px] text-white/45">
              ADA/USDT long — correlated positions limit reached
            </span>
          </div>
        </div>
      )}

      {kind === "equity" && (
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] text-white/35">Decision Brain 1.0.0</span>
            <span className="text-[10px] text-loss-soft/80">insufficient evidence</span>
          </div>
          <Sparkline seed={7} drift={-0.7} tone="#C9A24B" />
          <div className="grid grid-cols-4 gap-2 border-t border-line pt-2">
            {[
              ["net", "−2.98R"],
              ["win rate", "33.3%"],
              ["PF", "0.81"],
              ["trades", "15"],
            ].map(([k, v]) => (
              <div key={k}>
                <p className="text-[9px] text-white/30">{k}</p>
                <p className="font-mono text-[11px] tabular text-white/80">{v}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {kind === "fill" && (
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-x-3">
            <Row label="order" value="limit buy" />
            <Row label="size" value="0.42 BTC" />
            <Row label="limit" value="68,408.0" />
            <Row label="filled" value="68,408.0" tone="up" />
            <Row label="liquidity" value="maker" />
            <Row label="fee 0.02%" value="5.75 USDT" tone="blue" />
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-line bg-black/30 px-2 py-1.5">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-gold" />
            <span className="truncate text-[10px] text-white/45">
              paper fill on the live Binance price · live routing locked
            </span>
          </div>
        </div>
      )}

      {kind === "memory" && (
        <div className="space-y-2">
          <div className="rounded-lg border border-line bg-black/25 p-2">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[10px] text-white/60">ETH/USDT · revenge trades</span>
              <span className="font-mono text-[10px] text-loss-soft">-1.0R</span>
            </div>
            <p className="mt-1 text-[10px] leading-relaxed text-white/40">
              <span className="text-white/60">Pattern:</span> entries taken soon after a loss
              kept losing — 5 of the last 6.
            </p>
            <p className="mt-1 text-[10px] leading-relaxed text-white/40">
              <span className="text-gold-soft/80">Correction:</span> risk on those entries cut
              to 0.5× · lapses in 14 days unless confirmed.
            </p>
          </div>
          <div className="flex items-center gap-2 rounded-lg border border-gold/20 bg-gold/[0.05] px-2 py-1.5">
            <span className="font-mono text-[9px] uppercase tracking-wider text-gold-soft">similar</span>
            <span className="truncate text-[10px] text-white/45">
              4 similar trades in your record · 1 won
            </span>
          </div>
        </div>
      )}

      {kind === "analytics" && (
        <div className="space-y-2">
          {[
            ["London session", 68, "up"],
            ["New York session", 41, "up"],
            ["Asia session", 22, "down"],
          ].map(([label, pct, tone]) => (
            <div key={label as string}>
              <div className="mb-1 flex justify-between">
                <span className="text-[10px] text-white/40">{label as string}</span>
                <span className={cn("font-mono text-[10px] tabular", tone === "up" ? "text-emerald-soft" : "text-loss-soft")}>
                  {tone === "up" ? "+" : "−"}
                  {pct as number}
                </span>
              </div>
              <Bar pct={pct as number} tone={tone === "up" ? "up" : "down"} />
            </div>
          ))}
          <p className="border-t border-line pt-2 text-[10px] leading-relaxed text-white/35">
            81% of net profit came from two symbols in one session.
          </p>
        </div>
      )}

      {kind === "feed" && (
        <div className="space-y-1 font-mono text-[10px]">
          {[
            ["09:15:00", "eval", "SOL/USDT scored 84 · min 60", "text-white/55"],
            ["09:15:00", "risk", "checks passed · 0.5% equity", "text-signal-soft"],
            ["09:15:00", "order", "limit 148.22 · 12.4 SOL", "text-white/55"],
            ["09:20:00", "fill", "148.22 · maker · paper", "text-emerald-soft"],
            ["09:30:00", "veto", "XRP/USDT · daily loss limit", "text-loss-soft"],
          ].map(([t, tag, msg, cls], i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: active ? i * 0.08 : 0, duration: 0.3 }}
              className="flex gap-2"
            >
              <span className="text-white/20">{t as string}</span>
              <span className="w-9 shrink-0 text-white/30">{tag as string}</span>
              <span className={cn("truncate", cls as string)}>{msg as string}</span>
            </motion.div>
          ))}
        </div>
      )}
    </Frame>
  );
}
