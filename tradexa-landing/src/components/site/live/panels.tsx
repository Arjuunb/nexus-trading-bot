import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/** Terminal panel chrome: a title strip, a hairline border, no rounding drama. */
export function Panel({
  title,
  icon: Icon,
  right,
  children,
  className,
}: {
  title: string;
  icon?: LucideIcon;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cn(
        "flex min-w-0 flex-col overflow-hidden rounded-lg border border-term-500/70 bg-term-800/80 backdrop-blur-sm",
        className,
      )}
    >
      <header className="flex items-center gap-2 border-b border-term-500/70 bg-term-700/60 px-3 py-2">
        {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-white/35" />}
        <h3 className="font-mono text-[10px] uppercase tracking-[0.16em] text-white/45">{title}</h3>
        <div className="ml-auto shrink-0">{right}</div>
      </header>
      <div className="min-w-0 flex-1">{children}</div>
    </section>
  );
}

/**
 * The paper account the positions sit in.
 *
 * This used to be an L2 order book with an imbalance meter. The engine neither
 * reads nor shows depth -- it decides on closed candles and fills on the paper
 * broker -- so the panel shows what the product does have: the account, the
 * risk open against it and the fees its fills have paid.
 */
const PAPER_BALANCE = 100_000;
const MAKER_FEE = 0.0002;

export function PaperAccount({ positions }: { positions: Position[] }) {
  let unrealised = 0;
  let openRisk = 0;
  let fees = 0;
  for (const p of positions) {
    const size = Number(p.size);
    const dir = p.side === "LONG" ? 1 : -1;
    unrealised += (p.mark - p.entry) * dir * size;
    openRisk += Math.abs(p.entry - p.stop) * size;
    fees += p.entry * size * MAKER_FEE;
  }
  const money = (v: number) => v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const rows: [string, string, string?][] = [
    ["starting balance", money(PAPER_BALANCE)],
    ["unrealised", `${unrealised >= 0 ? "+" : ""}${money(unrealised)}`, unrealised >= 0 ? "text-emerald-soft" : "text-loss-soft"],
    ["equity", money(PAPER_BALANCE + unrealised)],
    ["open risk", `${((openRisk / PAPER_BALANCE) * 100).toFixed(2)}%`],
    ["entry fees", money(fees)],
    ["positions", String(positions.length)],
  ];

  return (
    <div className="flex h-full flex-col">
      <div className="divide-y divide-term-500/30">
        {rows.map(([k, v, cls]) => (
          <div key={k} className="flex items-baseline justify-between px-3 py-2 font-mono text-[10px]">
            <span className="text-white/35">{k}</span>
            <span className={cn("tabular", cls ?? "text-white/70")}>{v}</span>
          </div>
        ))}
      </div>
      <div className="mt-auto border-t border-term-500/50 px-3 py-2.5">
        <div className="flex items-center gap-2 font-mono text-[9px] uppercase tracking-wider">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-gold" />
          <span className="text-white/45">paper · live routing locked</span>
        </div>
      </div>
    </div>
  );
}

export interface Position {
  symbol: string;
  side: "LONG" | "SHORT";
  size: string;
  entry: number;
  mark: number;
  stop: number;
  target: number;
  strategy: string;
}

export function Positions({ positions }: { positions: Position[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] border-collapse font-mono text-[10px]">
        <thead>
          <tr className="border-b border-term-500/50 text-left uppercase tracking-wider text-white/25">
            {["symbol", "side", "size", "entry", "mark", "pnl", "R", "strategy"].map((h) => (
              <th key={h} className="px-3 py-1.5 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const dir = p.side === "LONG" ? 1 : -1;
            const pnlPct = ((p.mark - p.entry) / p.entry) * 100 * dir;
            const risk = Math.abs(p.entry - p.stop);
            const r = risk ? ((p.mark - p.entry) * dir) / risk : 0;
            const win = pnlPct >= 0;
            return (
              <tr key={p.symbol} className="border-b border-term-500/25 last:border-0 odd:bg-white/[0.015]">
                <td className="px-3 py-2 text-white/75">{p.symbol}</td>
                <td className="px-3 py-2">
                  <span
                    className={cn(
                      "rounded px-1.5 py-0.5 text-[9px]",
                      p.side === "LONG"
                        ? "bg-emerald/15 text-emerald-soft"
                        : "bg-loss/15 text-loss-soft",
                    )}
                  >
                    {p.side}
                  </span>
                </td>
                <td className="px-3 py-2 tabular text-white/50">{p.size}</td>
                <td className="px-3 py-2 tabular text-white/50">{p.entry.toFixed(1)}</td>
                <td className="px-3 py-2 tabular text-white/70">{p.mark.toFixed(1)}</td>
                <td className={cn("px-3 py-2 tabular", win ? "text-emerald-soft" : "text-loss-soft")}>
                  {win ? "+" : ""}
                  {pnlPct.toFixed(2)}%
                </td>
                <td className={cn("px-3 py-2 tabular", win ? "text-emerald-soft" : "text-loss-soft")}>
                  {r >= 0 ? "+" : ""}
                  {r.toFixed(2)}
                </td>
                <td className="px-3 py-2 text-white/30">{p.strategy}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The decision panel.
 *
 * Its job on this page is narrower than on /engine: not to explain how the
 * decision is made, but to show that at any instant there *is* one, in words,
 * attached to a symbol and a number. The engine page owns the mechanism.
 */
export function DecisionPanel({ epoch, price }: { epoch: number; price: number }) {
  const decisions = useMemo(
    () => [
      {
        symbol: "BTC/USDT",
        action: "HOLD",
        score: 68,
        tone: "hold" as const,
        lines: [
          "Trend intact on 4h; 1h momentum flattening.",
          "Position already open — no pyramiding into it.",
        ],
      },
      {
        symbol: "SOL/USDT",
        action: "PAPER LONG",
        score: 84,
        tone: "route" as const,
        lines: [
          "4h trend agrees; reward : risk 2.6; regime fits the setup.",
          "Risk checks passed · sized to 0.5% of equity at the stop.",
        ],
      },
      {
        symbol: "DOGE/USDT",
        action: "VETO",
        score: 48,
        tone: "veto" as const,
        lines: [
          "Quality score 48, below the minimum of 60.",
          "Volatility below the tradeable band; regime is a tight range.",
        ],
      },
    ],
    [],
  );

  const d = decisions[epoch % decisions.length];
  const tone = {
    route: { cls: "border-emerald/40 bg-emerald/10 text-emerald-soft", ring: "#22C55E" },
    hold: { cls: "border-line-strong bg-white/[0.04] text-white/60", ring: "#8A929C" },
    veto: { cls: "border-loss/40 bg-loss/10 text-loss-soft", ring: "#EF4444" },
  }[d.tone];

  return (
    <div className="p-3">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[11px] text-white/70">{d.symbol}</span>
        <span className={cn("rounded border px-2 py-0.5 font-mono text-[9px] tracking-[0.12em]", tone.cls)}>
          {d.action}
        </span>
      </div>

      <div className="mt-3 flex items-center gap-3">
        <svg viewBox="0 0 44 44" className="h-11 w-11 shrink-0">
          <circle cx="22" cy="22" r="17" fill="none" stroke="#17171A" strokeWidth="5" />
          <motion.circle
            cx="22"
            cy="22"
            r="17"
            fill="none"
            stroke={tone.ring}
            strokeWidth="5"
            strokeLinecap="round"
            transform="rotate(-90 22 22)"
            strokeDasharray={2 * Math.PI * 17}
            animate={{ strokeDashoffset: 2 * Math.PI * 17 * (1 - d.score / 100) }}
            transition={{ duration: 0.8, ease: [0.22, 1, 0.36, 1] }}
          />
          <text x="22" y="26" textAnchor="middle" fill="#fff" style={{ fontSize: 13, fontWeight: 700 }}>
            {d.score}
          </text>
        </svg>
        <div className="min-w-0">
          <AnimatePresence mode="wait">
            <motion.ul
              key={d.symbol}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.3 }}
              className="space-y-1"
            >
              {d.lines.map((l) => (
                <li key={l} className="text-[11px] leading-relaxed text-white/50">
                  {l}
                </li>
              ))}
            </motion.ul>
          </AnimatePresence>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 border-t border-term-500/50 pt-2.5 font-mono text-[9px]">
        {[
          ["minimum", "60"],
          ["mark", price.toFixed(0)],
          ["mode", "paper"],
        ].map(([k, v]) => (
          <div key={k}>
            <p className="text-white/25">{k}</p>
            <p className="tabular text-white/60">{v}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

export interface TimelineEvent {
  t: string;
  kind: "eval" | "risk" | "order" | "fill" | "amend" | "veto";
  text: string;
}

const KIND_STYLE: Record<TimelineEvent["kind"], { dot: string; label: string }> = {
  eval: { dot: "bg-white/40", label: "text-white/45" },
  risk: { dot: "bg-signal", label: "text-signal-soft" },
  order: { dot: "bg-gold", label: "text-gold-soft" },
  fill: { dot: "bg-emerald", label: "text-emerald-soft" },
  amend: { dot: "bg-aqua", label: "text-aqua-soft" },
  veto: { dot: "bg-loss", label: "text-loss-soft" },
};

/**
 * Execution timeline.
 *
 * New events arrive at the top and push the rest down, because the question a
 * timeline answers is "what just happened", not "what happened first". The list
 * is capped so the panel cannot grow the page while you are looking at it.
 */
export function ExecutionTimeline({ epoch }: { epoch: number }) {
  const reduced = useReducedMotion() ?? false;
  const script: TimelineEvent[] = useMemo(
    () => [
      { t: "+0.00s", kind: "eval", text: "SOL/USDT 15m close · quality 84 · minimum 60" },
      { t: "+0.06s", kind: "risk", text: "risk checks passed · 0.5% equity at the stop" },
      { t: "+0.07s", kind: "order", text: "paper limit buy 12.4 SOL @ 148.22" },
      { t: "+15m", kind: "fill", text: "filled 12.4 @ 148.22 · maker · fee 0.02%" },
      { t: "+15m", kind: "amend", text: "stop 143.10 · target 161.80 set · engine-managed" },
      { t: "+30m", kind: "eval", text: "DOGE/USDT 15m close · quality 48" },
      { t: "+30m", kind: "veto", text: "below the 60 minimum · reason recorded" },
    ],
    [],
  );

  const [events, setEvents] = useState<TimelineEvent[]>(() => script.slice(0, 4).reverse());

  useEffect(() => {
    if (reduced) return;
    setEvents((prev) => {
      const next = script[(epoch + 4) % script.length];
      return [{ ...next, t: stamp(epoch) }, ...prev].slice(0, 7);
    });
  }, [epoch, reduced, script]);

  return (
    <div className="relative p-3">
      {/* the spine */}
      <span aria-hidden className="absolute bottom-4 left-[18px] top-4 w-px bg-term-500" />
      <ul className="space-y-2.5">
        <AnimatePresence initial={false}>
          {events.map((e, i) => {
            const s = KIND_STYLE[e.kind];
            return (
              <motion.li
                key={`${e.t}-${e.text}-${i}`}
                layout
                initial={{ opacity: 0, y: -8 }}
                animate={{ opacity: i === 0 ? 1 : 0.55 + Math.max(0, 0.45 - i * 0.09), y: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
                className="relative flex gap-3 pl-1"
              >
                <span className="relative mt-[5px] flex h-2 w-2 shrink-0 items-center justify-center">
                  <span className={cn("h-2 w-2 rounded-full", s.dot)} />
                  {i === 0 && !reduced && (
                    <span className={cn("absolute h-2 w-2 rounded-full motion-safe:animate-ping-ring", s.dot)} />
                  )}
                </span>
                <div className="min-w-0 font-mono text-[10px]">
                  <span className="text-white/20">{e.t}</span>
                  <span className={cn("ml-2 uppercase tracking-wider", s.label)}>{e.kind}</span>
                  <p className="mt-0.5 truncate text-white/50">{e.text}</p>
                </div>
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ul>
    </div>
  );
}

function stamp(epoch: number) {
  const base = 9 * 3600 + 14 * 60;
  const s = base + epoch * 7;
  const hh = String(Math.floor(s / 3600) % 24).padStart(2, "0");
  const mm = String(Math.floor(s / 60) % 60).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}
