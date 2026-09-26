import { useState, type ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { LayoutDashboard, ListOrdered, BarChart3, ShieldAlert, Wallet, type LucideIcon } from "lucide-react";
import { Reveal, SectionHeading } from "@/components/Reveal";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/utils";

interface View {
  key: string;
  label: string;
  icon: LucideIcon;
  render: () => JSX.Element;
}

const bars = [40, 62, 48, 78, 58, 88, 70, 96, 82];

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="surface h-full p-4">
      <p className="mb-3 text-[11px] uppercase tracking-wider text-white/40">{title}</p>
      {children}
    </div>
  );
}

const VIEWS: View[] = [
  {
    key: "dashboard",
    label: "Nexus Terminal",
    icon: LayoutDashboard,
    render: () => (
      <div className="grid h-full grid-cols-3 gap-3">
        <div className="col-span-2 space-y-3">
          <Panel title="Equity Curve">
            <div className="flex h-32 items-end gap-1.5">
              {bars.map((b, i) => (
                <motion.div
                  key={i}
                  initial={{ height: 0 }}
                  animate={{ height: `${b}%` }}
                  transition={{ delay: i * 0.05, duration: 0.5, ease: "easeOut" }}
                  className="flex-1 rounded-t bg-gradient-to-t from-gold/30 to-gold/70"
                />
              ))}
            </div>
          </Panel>
          <div className="grid grid-cols-3 gap-3">
            {[
              ["Win Rate", "61%"],
              ["Profit Factor", "1.8"],
              ["Max DD", "6.4%"],
            ].map(([l, v]) => (
              <div key={l} className="surface p-3">
                <p className="tabular text-lg font-semibold text-white">{v}</p>
                <p className="text-[10px] uppercase tracking-wider text-white/40">{l}</p>
              </div>
            ))}
          </div>
        </div>
        <Panel title="Open Positions">
          <div className="space-y-2">
            {["BTC", "ETH", "SOL"].map((s, i) => (
              <div key={s} className="flex items-center justify-between text-xs">
                <span className="text-white/75">{s}/USDT</span>
                <span className={cn("font-mono", i === 2 ? "text-loss-soft" : "text-emerald-soft")}>
                  {i === 2 ? "-0.4R" : "+1.6R"}
                </span>
              </div>
            ))}
          </div>
        </Panel>
      </div>
    ),
  },
  {
    key: "history",
    label: "Trade History",
    icon: ListOrdered,
    render: () => (
      <Panel title="Memory Timeline">
        <div className="divide-y divide-line">
          {[
            ["BTC/USDT", "LONG", "+2.4R", true],
            ["ETH/USDT", "LONG", "+1.1R", true],
            ["SOL/USDT", "SHORT", "-0.6R", false],
            ["BNB/USDT", "LONG", "+0.9R", true],
            ["XRP/USDT", "SHORT", "+1.3R", true],
          ].map(([sym, side, pnl, up]) => (
            <div key={sym as string} className="grid grid-cols-3 items-center py-2.5 text-xs">
              <span className="font-medium text-white/80">{sym}</span>
              <span className="text-white/50">{side}</span>
              <span className={cn("text-right font-mono", up ? "text-emerald-soft" : "text-loss-soft")}>
                {pnl}
              </span>
            </div>
          ))}
        </div>
      </Panel>
    ),
  },
  {
    key: "analytics",
    label: "Performance Analytics",
    icon: BarChart3,
    render: () => (
      <div className="grid h-full grid-cols-2 gap-3">
        <Panel title="By Session">
          <div className="space-y-2.5">
            {[
              ["London", 82],
              ["New York", 64],
              ["Tokyo", 41],
            ].map(([l, v]) => (
              <div key={l as string}>
                <div className="mb-1 flex justify-between text-[11px] text-white/60">
                  <span>{l}</span>
                  <span className="tabular">{v}%</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
                  <div className="h-full rounded-full bg-gold/70" style={{ width: `${v}%` }} />
                </div>
              </div>
            ))}
          </div>
        </Panel>
        <Panel title="Expectancy">
          <div className="flex h-full flex-col items-center justify-center">
            <p className="tabular text-3xl font-bold text-emerald-soft">+0.34R</p>
            <p className="mt-1 text-[11px] text-white/40">per trade · 180 samples</p>
          </div>
        </Panel>
      </div>
    ),
  },
  {
    key: "risk",
    label: "Risk Dashboard",
    icon: ShieldAlert,
    render: () => (
      <div className="grid h-full grid-cols-2 gap-3">
        <Panel title="Daily Loss Guard">
          <div className="flex h-full flex-col justify-center">
            <div className="h-2.5 overflow-hidden rounded-full bg-white/10">
              <div className="h-full rounded-full bg-gradient-to-r from-emerald to-gold" style={{ width: "30%" }} />
            </div>
            <p className="mt-2 text-xs text-white/60">0.9% used of 3.0% limit</p>
          </div>
        </Panel>
        <Panel title="Guards Active">
          <div className="space-y-2">
            {["Position sizing", "Stop loss", "Trailing stop", "Max exposure"].map((g) => (
              <div key={g} className="flex items-center gap-2 text-xs text-white/70">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald" />
                {g}
              </div>
            ))}
          </div>
        </Panel>
      </div>
    ),
  },
  {
    key: "portfolio",
    label: "Portfolio",
    icon: Wallet,
    render: () => (
      <Panel title="Allocation">
        <div className="flex items-center gap-6">
          <div
            className="h-28 w-28 shrink-0 rounded-full"
            style={{
              background:
                "conic-gradient(#C8A94B 0 45%, #2FBF71 45% 72%, #E5605B 72% 86%, rgba(255,255,255,0.12) 86% 100%)",
            }}
          />
          <div className="space-y-2 text-xs">
            {[
              ["BTC", "45%", "#C8A94B"],
              ["ETH", "27%", "#2FBF71"],
              ["SOL", "14%", "#E5605B"],
              ["Cash", "14%", "rgba(255,255,255,0.3)"],
            ].map(([l, v, c]) => (
              <div key={l} className="flex items-center gap-2 text-white/70">
                <span className="h-2 w-2 rounded-sm" style={{ background: c }} />
                <span className="w-10">{l}</span>
                <span className="tabular text-white/50">{v}</span>
              </div>
            ))}
          </div>
        </div>
      </Panel>
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
          subtitle="A representative look at the running dashboard. Interface preview with sample data — not a live account."
        />

        <Reveal className="mt-12">
          <div className="mb-5 flex flex-wrap justify-center gap-2">
            {VIEWS.map((v) => (
              <button
                key={v.key}
                onClick={() => setActive(v.key)}
                className={cn(
                  "inline-flex items-center gap-2 rounded-full border px-4 py-2 text-sm transition-all",
                  active === v.key
                    ? "border-gold/40 bg-gold/10 text-gold-soft"
                    : "border-line text-white/55 hover:border-line-strong hover:text-white",
                )}
              >
                <v.icon className="h-4 w-4" />
                {v.label}
              </button>
            ))}
          </div>

          <div className="glass-strong rounded-2xl p-3 shadow-card sm:p-5">
            <div className="mb-3 flex items-center justify-between px-1">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full bg-loss/70" />
                <span className="h-2.5 w-2.5 rounded-full bg-gold/70" />
                <span className="h-2.5 w-2.5 rounded-full bg-emerald/70" />
              </div>
              <Badge tone="neutral">{view.label} · preview</Badge>
            </div>
            <div className="min-h-[18rem] rounded-xl bg-ink-800/60 p-4">
              <AnimatePresence mode="wait">
                <motion.div
                  key={view.key}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                  transition={{ duration: 0.3 }}
                  className="h-full"
                >
                  {view.render()}
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
