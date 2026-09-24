import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { VENUES } from "@/site/platform";

/**
 * Thin monospace telemetry strip — the kind of status line a real trading
 * engine prints. Frames the product as a running system (paper mode, honest)
 * rather than a marketing hero. Values are representative, labelled preview.
 */

// From the one venue list, so this strip cannot claim a connection the
// footer and the Connectivity section do not.
const EXCHANGES = VENUES.slice(0, 4).map((v) => ({ name: v.name, up: v.live }));

function Seg({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-center gap-2 whitespace-nowrap", className)}>
      <span className="font-mono text-[10px] uppercase tracking-wider text-white/30">{label}</span>
      <span className="font-mono text-[12px] text-white/75">{children}</span>
    </div>
  );
}

export function EngineStatusBar() {
  return (
    <div className="container-x">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        transition={{ duration: 0.6 }}
        className="surface flex flex-wrap items-center gap-x-6 gap-y-3 px-5 py-3.5"
      >
        <Seg label="Engine">
          <span className="inline-flex items-center gap-1.5">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald opacity-60" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald" />
            </span>
            <span className="text-emerald-soft">RUNNING</span>
          </span>
        </Seg>

        <span className="h-4 w-px bg-line" />
        <Seg label="Mode">
          <span className="text-gold-soft">PAPER</span>
        </Seg>

        <span className="hidden h-4 w-px bg-line sm:block" />
        <div className="hidden items-center gap-3 sm:flex">
          <span className="font-mono text-[10px] uppercase tracking-wider text-white/30">Venues</span>
          <div className="flex items-center gap-2.5">
            {EXCHANGES.map((e) => (
              <span key={e.name} className="flex items-center gap-1.5 font-mono text-[11px]">
                <span
                  className={cn("h-1.5 w-1.5 rounded-full", e.up ? "bg-emerald" : "bg-white/25")}
                />
                <span className={e.up ? "text-white/70" : "text-white/35"}>{e.name}</span>
              </span>
            ))}
          </div>
        </div>

        <span className="hidden h-4 w-px bg-line lg:block" />
        <Seg label="Latency" className="hidden lg:flex">
          <span className="tabular">82ms</span>
        </Seg>

        <span className="hidden h-4 w-px bg-line lg:block" />
        <Seg label="Uptime" className="hidden lg:flex">
          <span className="tabular text-emerald-soft">99.9%</span>
        </Seg>

        <span className="ml-auto font-mono text-[10px] uppercase tracking-wider text-white/25">
          preview telemetry
        </span>
      </motion.div>
    </div>
  );
}
