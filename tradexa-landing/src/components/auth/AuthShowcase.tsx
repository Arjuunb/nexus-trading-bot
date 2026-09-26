import { motion } from "framer-motion";
import { TrendingUp, ShieldCheck, Zap } from "lucide-react";
import { AuthPipeline } from "./AuthPipeline";

const STATS = [
  { icon: TrendingUp, label: "Decisions", value: "Every one journaled" },
  { icon: ShieldCheck, label: "Keys", value: "Encrypted · No withdrawals" },
  { icon: Zap, label: "Execution", value: "Paper account · live locked" },
];

/**
 * Left panel of the split auth layout — a quiet showcase of what the product
 * does. It carries no performance numbers: a sign-in page is no place for
 * returns nobody earned.
 */
export function AuthShowcase() {
  return (
    <div className="relative hidden overflow-hidden lg:flex lg:flex-col lg:justify-between">
      {/* backdrop */}
      <div className="absolute inset-0 -z-10 bg-ink-800" />
      {/* Was the landing page's grid, which made signing in look like scrolling
          further down the marketing site. A soft vertical wash instead. */}
      <div
        className="absolute inset-0 -z-10 opacity-70"
        style={{
          backgroundImage:
            "repeating-linear-gradient(180deg, rgba(255,255,255,0.022) 0 1px, transparent 1px 5px)",
        }}
      />
      <div className="absolute -left-20 top-10 -z-10 h-80 w-80 rounded-full bg-gold/10 blur-[110px]" />
      <div className="absolute bottom-0 right-0 -z-10 h-72 w-72 rounded-full bg-emerald/[0.07] blur-[120px]" />

      <div className="p-10 xl:p-14">
        <p className="eyebrow">TradeLogX Nexus</p>
        <h2 className="mt-5 max-w-md text-3xl font-bold leading-tight tracking-tight text-white xl:text-4xl">
          Automated Trading.
          <br />
          <span className="text-gold-gradient">Human Intelligence.</span>
        </h2>
        <p className="mt-4 max-w-sm text-sm leading-relaxed text-white/55">
          Analyze markets, execute strategies, and manage risk — with complete transparency over
          every decision Nexus makes.
        </p>
      </div>

      {/* how a decision flows, animated (a product description, not results) */}
      <div className="flex flex-1 items-center px-10 xl:px-14">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.8, delay: 0.2 }}
          className="w-full max-w-xl"
        >
          <AuthPipeline />
        </motion.div>
      </div>

      <div className="space-y-3 p-10 xl:p-14">
        {STATS.map((s, i) => (
          <motion.div
            key={s.label}
            initial={{ opacity: 0, x: -16 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: 0.4 + i * 0.12 }}
            className="glass flex items-center gap-3 rounded-xl px-4 py-3"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-gold/10 text-gold">
              <s.icon className="h-4 w-4" />
            </span>
            <div>
              <p className="text-sm font-medium text-white">{s.value}</p>
              <p className="text-[11px] uppercase tracking-wider text-white/40">{s.label}</p>
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}
