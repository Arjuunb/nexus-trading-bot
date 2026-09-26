import { useState } from "react";
import { Link } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import {
  BarChart3,
  FlaskConical,
  LayoutDashboard,
  NotebookPen,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import { DashboardBackdrop } from "@/components/site/backdrops";
import { useRouteMeta } from "@/site/seo";
import { routeFor, prefetchRoute } from "@/site/routes";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * /dashboard — a tour of the workspace.
 *
 * Structured as an application window with a real sidebar you can click
 * through, rather than a gallery of screenshots. The argument of the page is
 * "this is a workspace, not a chart with buttons", and the only way to make
 * that argument is to let the reader move around inside it.
 *
 * The panels are drawn from the same tokens as the product for the reasons the
 * feature screenshots are: a PNG would be a megabyte, would go stale the first
 * time a colour moved, and would not survive a 360px phone.
 */

type PanelId = "overview" | "positions" | "journal" | "lab" | "risk";

const PANELS: {
  id: PanelId;
  label: string;
  icon: typeof LayoutDashboard;
  headline: string;
  body: string;
  notes: string[];
}[] = [
  {
    id: "overview",
    label: "Overview",
    icon: LayoutDashboard,
    headline: "What the system is doing, right now",
    body: "Equity, open risk, today's decisions and the live event feed on one screen. It answers the question you actually open the app to ask, which is not 'how am I doing' but 'is anything happening that I should know about'.",
    notes: [
      "Open risk shown as a fraction of the daily budget, not as a dollar figure that means nothing without context",
      "The feed shows the engine's own events — nothing is summarised away",
      "A halted or blocked system says so at the top, not in a notification you might miss",
    ],
  },
  {
    id: "positions",
    label: "Positions",
    icon: Wallet,
    headline: "Every position, with its invalidation visible",
    body: "Entry, mark, R multiple, stop and target for every open position. The stop is the one that matters: it is set the moment the position opens and the engine manages it on every candle.",
    notes: [
      "R multiple rather than percentage, because that is the unit position sizing is expressed in",
      "Stop and target shown on every position, managed by the engine rather than an exchange",
      "Close an instance's open positions from the dashboard; the action is written to its log",
    ],
  },
  {
    id: "journal",
    label: "Journal",
    icon: NotebookPen,
    headline: "What happened, and what it taught the system",
    body: "Every closed trade with the decision that opened it, the reasoning at the time and the outcome. Rejections are here too — over a month, the record of what the system nearly did is more instructive than the record of what it did.",
    notes: [
      "Filter to rejections only, which is the view most people never think to open",
      "Skipped trades show the gate that stopped them and why",
      "Every candle's decision report is kept, WAIT included",
    ],
  },
  {
    id: "lab",
    label: "Strategy Lab",
    icon: FlaskConical,
    headline: "Prove it before it costs anything",
    body: "Backtests and parameter sweeps against historical data, through the same Decision Brain gate and fill costs the paper engine uses. A result on the data a strategy was tuned on is not evidence, so out-of-sample results sit beside it.",
    notes: [
      "Walk-forward and out-of-sample results shown next to each other",
      "Fees and slippage charged in every backtest, so costs are never left out",
      "Paper trading on live data comes next; live trading stays locked",
    ],
  },
  {
    id: "risk",
    label: "Risk console",
    icon: ShieldCheck,
    headline: "The limits, and everything they have stopped",
    body: "Daily and weekly budgets, exposure ceiling, correlation load and the blackout calendar — plus the log of every veto, with the rule that fired. This is where you find out the system has been protecting you from something you did not know about.",
    notes: [
      "Budgets shown as consumed-of-available, with the halt threshold marked",
      "Every veto names its rule; none are attributed to a generic 'risk check failed'",
      "Changing a limit is an audit-log event with the previous value recorded",
    ],
  },
];

/* ── Panel mocks ──────────────────────────────────────────────────────── */

function Bar({ pct, tone }: { pct: number; tone: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
      <motion.div
        className="h-full rounded-full"
        style={{ background: tone }}
        initial={{ width: 0 }}
        animate={{ width: `${pct}%` }}
        transition={{ duration: 0.7, ease: EASE }}
      />
    </div>
  );
}

function PanelMock({ id }: { id: PanelId }) {
  if (id === "overview") {
    return (
      <div className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          {[
            ["equity", "$128,402", "#E9EEF3"],
            ["today", "+0.82%", "#4ADE80"],
            ["open risk", "1.4 / 3.0%", "#F2C766"],
          ].map(([k, v, c]) => (
            <div key={k} className="rounded-lg border border-white/[0.07] bg-black/30 p-3">
              <p className="font-mono text-[9px] uppercase tracking-wider text-white/30">{k}</p>
              <p className="mt-1 font-mono text-[15px] tabular" style={{ color: c }}>
                {v}
              </p>
            </div>
          ))}
        </div>
        <div className="rounded-lg border border-white/[0.07] bg-black/30 p-3">
          <p className="mb-2 font-mono text-[9px] uppercase tracking-wider text-white/30">
            daily budget
          </p>
          <Bar pct={47} tone="#F2C766" />
          <p className="mt-1.5 font-mono text-[9px] text-white/25">−1.4% of −3.0% · trading</p>
        </div>
        <div className="space-y-1 rounded-lg border border-white/[0.07] bg-black/30 p-3 font-mono text-[10px]">
          {[
            ["09:14:07", "fill", "SOL/USDT 12.4 @ 148.24", "text-emerald-soft"],
            ["09:18:00", "veto", "XRP/USDT · event blackout", "text-loss-soft"],
            ["09:22:41", "eval", "BTC/USDT scored 68 · hold", "text-white/50"],
          ].map(([t, tag, msg, c]) => (
            <div key={t} className="flex gap-2">
              <span className="text-white/20">{t}</span>
              <span className="w-8 text-white/30">{tag}</span>
              <span className={cn("truncate", c)}>{msg}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (id === "positions") {
    return (
      <table className="w-full border-collapse font-mono text-[10px]">
        <thead>
          <tr className="border-b border-white/[0.07] text-left uppercase tracking-wider text-white/25">
            {["symbol", "side", "entry", "mark", "R", "protective"].map((h) => (
              <th key={h} className="px-2 py-1.5 font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[
            ["BTC/USDT", "LONG", "68,050", "68,776", "+1.15", "managed"],
            ["SOL/USDT", "LONG", "148.22", "149.86", "+0.32", "managed"],
            ["ETH/USDT", "SHORT", "3,301.4", "3,284.2", "+0.27", "managed"],
          ].map((r) => (
            <tr key={r[0]} className="border-b border-white/[0.04] last:border-0">
              <td className="px-2 py-2 text-white/75">{r[0]}</td>
              <td className="px-2 py-2">
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[9px]",
                    r[1] === "LONG" ? "bg-emerald/15 text-emerald-soft" : "bg-loss/15 text-loss-soft",
                  )}
                >
                  {r[1]}
                </span>
              </td>
              <td className="px-2 py-2 tabular text-white/45">{r[2]}</td>
              <td className="px-2 py-2 tabular text-white/70">{r[3]}</td>
              <td className="px-2 py-2 tabular text-emerald-soft">{r[4]}</td>
              <td className="px-2 py-2">
                <span className="inline-flex items-center gap-1 text-emerald-soft/80">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald" />
                  {r[5]}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }

  if (id === "journal") {
    return (
      <div className="space-y-2">
        {[
          {
            sym: "SOL/USDT",
            r: "+1.8R",
            ok: true,
            note: "Retest held above prior range high; sized normally, exited at target.",
            lesson: "Lesson: reclaim-and-hold in expanding volatility remains the highest-quality setup.",
          },
          {
            sym: "ETH/USDT",
            r: "−1.0R",
            ok: false,
            note: "Entered on the third retest after compression — momentum already spent.",
            lesson: "Lesson: cap retests at two in compressed volatility.",
          },
        ].map((e) => (
          <div key={e.sym} className="rounded-lg border border-white/[0.07] bg-black/30 p-3">
            <div className="flex items-center justify-between">
              <span className="font-mono text-[11px] text-white/70">{e.sym}</span>
              <span
                className={cn("font-mono text-[11px]", e.ok ? "text-emerald-soft" : "text-loss-soft")}
              >
                {e.r}
              </span>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-white/45">{e.note}</p>
            <p className="mt-1 text-[11px] leading-relaxed text-gold-soft/70">{e.lesson}</p>
          </div>
        ))}
        <div className="rounded-lg border border-aqua/20 bg-aqua/[0.05] px-3 py-2 font-mono text-[10px] text-aqua-soft">
          filter: rejections only · sample entries
        </div>
      </div>
    );
  }

  if (id === "lab") {
    return (
      <div className="space-y-3">
        <div className="rounded-lg border border-white/[0.07] bg-black/30 p-3">
          <p className="mb-2 font-mono text-[9px] uppercase tracking-wider text-white/30">
            threshold sweep · net expectancy
          </p>
          <div className="flex items-end gap-1.5">
            {[0.18, 0.24, 0.31, 0.29, 0.21, 0.12].map((v, i) => (
              <div key={i} className="flex-1">
                <motion.div
                  className={cn("rounded-t", i === 2 ? "bg-aqua" : "bg-aqua/35")}
                  initial={{ height: 0 }}
                  animate={{ height: v * 150 }}
                  transition={{ duration: 0.6, delay: i * 0.05, ease: EASE }}
                />
                <p className="mt-1 text-center font-mono text-[8px] text-white/25">
                  {[68, 70, 72, 74, 76, 78][i]}
                </p>
              </div>
            ))}
          </div>
          <p className="mt-2 font-mono text-[9px] text-white/25">
            sample sweep · shape only
          </p>
        </div>
        <div className="grid grid-cols-2 gap-2 font-mono text-[10px]">
          {[
            ["gross", "0.44R", "text-white/70"],
            ["net", "0.31R", "text-aqua-soft"],
            ["fees + funding", "−0.11R", "text-loss-soft"],
            ["slippage", "−0.02R", "text-loss-soft"],
          ].map(([k, v, c]) => (
            <div key={k} className="flex justify-between rounded border border-white/[0.06] px-2 py-1.5">
              <span className="text-white/30">{k}</span>
              <span className={cn("tabular", c)}>{v}</span>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {[
        ["daily budget", 47, "−1.4% of −3.0%", "#F2C766"],
        ["weekly budget", 28, "−2.1% of −7.5%", "#F2C766"],
        ["exposure", 52, "2.1x of 4.0x", "#4ADE80"],
        ["correlation load", 57, "0.34 of 0.60", "#F2C766"],
      ].map(([label, pct, note, tone]) => (
        <div key={label as string}>
          <div className="mb-1 flex justify-between font-mono text-[10px]">
            <span className="text-white/40">{label as string}</span>
            <span className="text-white/55">{note as string}</span>
          </div>
          <Bar pct={pct as number} tone={tone as string} />
        </div>
      ))}
      <div className="mt-1 space-y-1 rounded-lg border border-loss/20 bg-loss/[0.05] p-3 font-mono text-[10px]">
        <p className="text-loss-soft">recent vetoes</p>
        {[
          ["XRP/USDT", "event_risk"],
          ["ADA/USDT", "correlation"],
          ["DOGE/USDT", "quality < 60"],
        ].map(([s, rule]) => (
          <div key={s} className="flex justify-between text-white/40">
            <span>{s}</span>
            <span className="text-white/25">{rule}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── Page ─────────────────────────────────────────────────────────────── */

export default function DashboardPage() {
  const route = routeFor("/dashboard")!;
  useRouteMeta(route);
  const [active, setActive] = useState<PanelId>("overview");
  const panel = PANELS.find((p) => p.id === active)!;

  return (
    <>
      <DashboardBackdrop />

      <section className="container-x pt-32 sm:pt-40">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: EASE }}
          className="max-w-3xl"
        >
          <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.22em] text-aqua-soft">
            <LayoutDashboard className="h-3.5 w-3.5" />
            Dashboard
          </span>
          <h1 className="mt-5 text-balance text-4xl font-extrabold leading-[1.05] tracking-tight text-white sm:text-5xl lg:text-[3.5rem]">
            The workspace, not
            <br className="hidden sm:block" /> a chart with buttons
          </h1>
          <p className="mt-6 text-[17px] leading-relaxed text-white/55">
            Five panels, each answering a different question. Click through them below — a simplified sketch of the dashboard with sample data, drawn from the same design tokens as the product.
          </p>
        </motion.div>
      </section>

      {/* the application window */}
      <section className="container-x mt-14">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.08, ease: EASE }}
          className="overflow-hidden rounded-2xl border border-white/[0.09] bg-black/50 shadow-card backdrop-blur-sm"
        >
          {/* title bar */}
          <div className="flex items-center gap-2 border-b border-white/[0.07] bg-white/[0.02] px-4 py-2.5">
            <span className="flex gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full bg-white/12" />
              <span className="h-2.5 w-2.5 rounded-full bg-white/12" />
              <span className="h-2.5 w-2.5 rounded-full bg-white/12" />
            </span>
            <span className="ml-2 font-mono text-[11px] text-white/35">
              nexus · {panel.label.toLowerCase()}
            </span>
            <span className="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-emerald-soft">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald" />
              live
            </span>
          </div>

          <div className="grid sm:grid-cols-[168px_minmax(0,1fr)]">
            {/* sidebar */}
            <nav
              aria-label="Dashboard panels"
              className="flex gap-1 overflow-x-auto border-b border-white/[0.07] p-2 sm:flex-col sm:overflow-visible sm:border-b-0 sm:border-r"
            >
              {PANELS.map((p) => (
                <button
                  key={p.id}
                  onClick={() => setActive(p.id)}
                  aria-pressed={p.id === active}
                  className={cn(
                    "group relative flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-left transition-colors duration-200",
                    p.id === active ? "bg-aqua/[0.1]" : "hover:bg-white/[0.04]",
                  )}
                >
                  <p.icon
                    className={cn(
                      "h-4 w-4 shrink-0 transition-colors",
                      p.id === active ? "text-aqua-soft" : "text-white/35 group-hover:text-white/60",
                    )}
                  />
                  <span
                    className={cn(
                      "whitespace-nowrap text-[13px] transition-colors",
                      p.id === active ? "text-white" : "text-white/55 group-hover:text-white/85",
                    )}
                  >
                    {p.label}
                  </span>
                  {p.id === active && (
                    <motion.span
                      layoutId="dash-active"
                      transition={{ type: "spring", stiffness: 420, damping: 34 }}
                      className="absolute inset-y-1.5 -left-px hidden w-[2px] rounded-full bg-aqua sm:block"
                    />
                  )}
                </button>
              ))}
            </nav>

            {/* panel body */}
            <div className="min-w-0 p-4 sm:p-5">
              <AnimatePresence mode="wait">
                <motion.div
                  key={active}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -6 }}
                  transition={{ duration: 0.28, ease: EASE }}
                >
                  <PanelMock id={active} />
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </motion.div>
      </section>

      {/* what the panel is for */}
      <section className="container-x mt-8 pb-24">
        <AnimatePresence mode="wait">
          <motion.div
            key={active}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3, ease: EASE }}
            className="grid gap-8 lg:grid-cols-[1fr_1fr] lg:gap-14"
          >
            <div>
              <h2 className="text-balance text-2xl font-bold tracking-tight text-white sm:text-3xl">
                {panel.headline}
              </h2>
              <p className="mt-4 leading-relaxed text-white/55">{panel.body}</p>
            </div>
            <ul className="space-y-3 lg:pt-2">
              {panel.notes.map((n) => (
                <li key={n} className="flex gap-3 text-sm leading-relaxed text-white/55">
                  <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-aqua" />
                  <span>{n}</span>
                </li>
              ))}
            </ul>
          </motion.div>
        </AnimatePresence>

        <div className="mt-14 flex flex-wrap gap-3 border-t border-white/[0.07] pt-8">
          {[
            ["/live-trade", "See the terminal", "Chart, positions and fills on one surface"],
            ["/performance", "See the numbers", "Recorded validation and method"],
          ].map(([path, label, blurb]) => (
            <Link
              key={path}
              to={path}
              onPointerEnter={() => prefetchRoute(path)}
              className="group flex-1 rounded-xl border border-white/[0.08] bg-white/[0.02] p-5 transition-all duration-300 hover:-translate-y-0.5 hover:border-aqua/30 hover:bg-white/[0.04]"
            >
              <span className="flex items-center gap-2 text-[15px] font-medium text-white">
                <BarChart3 className="h-4 w-4 text-aqua-soft" />
                {label}
              </span>
              <span className="mt-1 block text-sm text-white/45">{blurb}</span>
            </Link>
          ))}
        </div>
      </section>
    </>
  );
}
