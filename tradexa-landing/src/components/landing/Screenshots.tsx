import { useState, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { CalendarDays, Check, Layers, LayoutDashboard, NotebookText, ShieldAlert, type LucideIcon } from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { ScrollScale } from "@/components/motion/Scroll";
import { cn } from "@/lib/utils";

// A tour of the real dashboard screens. Each tab says what is actually on the
// screen and shows its layout as a wireframe: labelled blocks, no invented
// figures. Numbers belong to your own account, not to a marketing page.

interface View {
  key: string;
  label: string;
  icon: LucideIcon;
  summary: string;
  points: string[];
  wire: () => ReactNode;
}

const EASE = [0.22, 1, 0.36, 1] as const;

/** One wireframe block: a label and a few skeleton lines, assembling in order. */
function Block({ label, i, className, children }: { label: string; i: number; className?: string; children?: ReactNode }) {
  const reduced = useReducedMotion();
  return (
    <motion.div
      initial={{ opacity: 0, y: reduced ? 0 : 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: reduced ? 0 : 0.05 + i * 0.06, duration: 0.4, ease: EASE }}
      className={cn("rounded-xl border border-line bg-ink-700/70 p-3", className)}
    >
      <p className="text-[10px] uppercase tracking-wider text-white/40">{label}</p>
      <div className="mt-2">{children ?? <Lines />}</div>
    </motion.div>
  );
}

function Lines({ n = 2, widths = ["70%", "45%", "60%"] }: { n?: number; widths?: string[] }) {
  return (
    <div className="space-y-1.5">
      {Array.from({ length: n }).map((_, k) => (
        <span key={k} className="block h-1.5 rounded-full bg-white/[0.08]" style={{ width: widths[k % widths.length] }} />
      ))}
    </div>
  );
}

function Curve() {
  return (
    <svg viewBox="0 0 200 60" className="h-20 w-full" preserveAspectRatio="none" aria-hidden>
      {[15, 30, 45].map((y) => <line key={y} x1="0" x2="200" y1={y} y2={y} stroke="rgba(255,255,255,0.05)" />)}
      <path d="M0 42 C 30 40, 45 30, 70 33 S 110 20, 135 24 S 175 12, 200 14" fill="none"
            stroke="rgba(255,255,255,0.28)" strokeWidth="1.5" strokeDasharray="3 3" />
    </svg>
  );
}

const VIEWS: View[] = [
  {
    key: "dashboard",
    label: "Dashboard",
    icon: LayoutDashboard,
    summary: "Account, positions and engine state on one screen, with the reason behind anything that is stopped.",
    points: [
      "A halted or blocked system says so at the top",
      "Market-data freshness checked per timeframe",
      "Every active instance and its current decision",
    ],
    wire: () => (
      <div className="grid h-full grid-cols-4 gap-2.5">
        {["Equity", "Open positions", "Today", "Risk used"].map((l, i) => <Block key={l} label={l} i={i}><Lines n={1} /></Block>)}
        <Block label="Equity curve" i={4} className="col-span-3"><Curve /></Block>
        <Block label="Active instances" i={5}><Lines n={3} /></Block>
      </div>
    ),
  },
  {
    key: "instances",
    label: "Trading Instances",
    icon: Layers,
    summary: "Isolated instances, each with one strategy, symbol, timeframe and risk budget of its own.",
    points: [
      "Start, pause or stop each one independently",
      "A decision log and lifecycle timeline per instance",
      "Paper execution; live routing stays locked",
    ],
    wire: () => (
      <div className="grid h-full grid-cols-5 gap-2.5">
        <div className="col-span-3 space-y-2.5">
          {["Instance · strategy · symbol", "Instance · strategy · symbol", "Instance · strategy · symbol"].map((l, i) => (
            <Block key={i} label={l} i={i}>
              <div className="flex items-center gap-2">
                <span className="rounded-full border border-gold/30 px-1.5 py-px text-[9px] text-gold/80">{i === 2 ? "paused" : "running"}</span>
                <Lines n={1} />
              </div>
            </Block>
          ))}
        </div>
        <Block label="Decision log" i={3} className="col-span-2"><Lines n={6} /></Block>
      </div>
    ),
  },
  {
    key: "calendar",
    label: "Calendar",
    icon: CalendarDays,
    summary: "Realized P&L by day across every trading source, each amount in its own currency.",
    points: [
      "Daily and weekly totals from the real ledgers",
      "Profit factor, expectancy, streaks and drawdown",
      "Any day or month exported as CSV",
    ],
    wire: () => (
      <div className="grid h-full grid-cols-5 gap-2.5">
        <Block label="Month" i={0} className="col-span-3">
          <div className="grid grid-cols-7 gap-1">
            {Array.from({ length: 35 }).map((_, d) => (
              <span key={d} className={cn("aspect-square rounded-[4px] border",
                [3, 9, 10, 16, 23, 24].includes(d) ? "border-gold/30 bg-gold/[0.08]" : "border-line bg-white/[0.02]")} />
            ))}
          </div>
        </Block>
        <div className="col-span-2 space-y-2.5">
          <Block label="Day detail" i={1}><Lines n={3} /></Block>
          <Block label="By source · by strategy" i={2}><Lines n={3} /></Block>
        </div>
      </div>
    ),
  },
  {
    key: "journal",
    label: "Journal",
    icon: NotebookText,
    summary: "The decision report for every candle, and the memory of every closed trade.",
    points: [
      "Reasons for every accept and every reject, WAIT included",
      "Trade memory with similar-trade search",
      "Coaching notes on recurring mistakes",
    ],
    wire: () => (
      <div className="space-y-2.5">
        {["Decision · candle closed", "Decision · candle closed", "Closed trade · review"].map((l, i) => (
          <Block key={i} label={l} i={i}>
            <div className="flex flex-wrap gap-1.5">
              {[0, 1, 2, 3].map((k) => (
                <span key={k} className="inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-0.5">
                  <Check className={cn("h-2.5 w-2.5", k < 3 - (i % 2) ? "text-gold/80" : "text-white/25")} aria-hidden />
                  <span className="block h-1 w-8 rounded-full bg-white/[0.1]" />
                </span>
              ))}
            </div>
          </Block>
        ))}
      </div>
    ),
  },
  {
    key: "risk",
    label: "Risk & Health",
    icon: ShieldAlert,
    summary: "The limits that stop trading, and the checks that say whether the system is healthy.",
    points: [
      "Daily and weekly loss limits, exposure and correlation caps",
      "Emergency stop, safety checks and an event blackout",
      "Status monitoring and a searchable log",
    ],
    wire: () => (
      <div className="grid h-full grid-cols-2 gap-2.5">
        {["Daily loss budget", "Weekly loss budget", "Exposure", "Correlation"].map((l, i) => (
          <Block key={l} label={l} i={i}>
            <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
              <span className="block h-full rounded-full bg-white/25" style={{ width: `${[34, 22, 48, 16][i]}%` }} />
            </div>
          </Block>
        ))}
        <Block label="Guards" i={4} className="col-span-2">
          <div className="grid grid-cols-2 gap-1.5">
            {["Position sizing", "Stop on every entry", "Drawdown breaker", "Event blackout"].map((g) => (
              <span key={g} className="flex items-center gap-1.5 text-[11px] text-white/60">
                <span className="h-1.5 w-1.5 rounded-full bg-gold/70" aria-hidden /> {g}
              </span>
            ))}
          </div>
        </Block>
      </div>
    ),
  },
];

export function Screenshots() {
  const [active, setActive] = useState(VIEWS[0].key);
  const view = VIEWS.find((v) => v.key === active) ?? VIEWS[0];

  return (
    <section id="product" className="section">
      <div className="container-x">
        <SectionHeading
          link="#product"
          eyebrow="The product"
          title="One terminal for your entire operation"
          subtitle="What each screen of the dashboard holds. Layouts shown as wireframes: the numbers are yours once it runs."
        />

        <Reveal className="mt-12">
          <div role="tablist" aria-label="Dashboard screens" className="mb-6 flex flex-wrap justify-center gap-2">
            {VIEWS.map((v) => (
              <button
                key={v.key}
                role="tab"
                aria-selected={active === v.key}
                onClick={() => setActive(v.key)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-sm transition-colors duration-200",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60",
                  active === v.key
                    ? "border-gold/40 bg-gold/10 text-gold-soft"
                    : "border-line text-white/55 hover:border-line-strong hover:text-white",
                )}
              >
                <v.icon className="h-4 w-4" aria-hidden />
                {v.label}
              </button>
            ))}
          </div>
        </Reveal>

        <ScrollScale>
          <div className="glass-strong grid gap-6 rounded-3xl p-5 shadow-card sm:p-7 lg:grid-cols-[0.8fr_1.2fr] lg:items-center">
            <AnimatePresence mode="wait">
              <motion.div
                key={`${view.key}-copy`}
                role="tabpanel"
                aria-label={view.label}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={{ duration: 0.3, ease: EASE }}
              >
                <p className="flex items-center gap-2 text-sm font-semibold text-white">
                  <view.icon className="h-4 w-4 text-gold" aria-hidden /> {view.label}
                </p>
                <p className="mt-3 text-[15px] leading-relaxed text-white/60">{view.summary}</p>
                <ul className="mt-5 space-y-2.5">
                  {view.points.map((p) => (
                    <li key={p} className="flex items-start gap-2.5 text-sm text-white/70">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-gold/80" aria-hidden />
                      {p}
                    </li>
                  ))}
                </ul>
              </motion.div>
            </AnimatePresence>

            <div className="min-h-[17rem] rounded-2xl border border-line bg-ink-800/70 p-3 sm:p-4">
              <div className="mb-3 flex items-center gap-1.5 px-1" aria-hidden>
                <span className="h-2 w-2 rounded-full bg-white/15" />
                <span className="h-2 w-2 rounded-full bg-white/15" />
                <span className="h-2 w-2 rounded-full bg-white/15" />
                <span className="ml-auto font-mono text-[10px] text-white/30">{view.label.toLowerCase()} · wireframe</span>
              </div>
              <AnimatePresence mode="wait">
                <motion.div key={view.key} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>
                  {view.wire()}
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </ScrollScale>
      </div>
    </section>
  );
}
