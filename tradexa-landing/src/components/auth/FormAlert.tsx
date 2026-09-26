import { motion } from "framer-motion";
import { AlertCircle, CheckCircle2, Info } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

type Tone = "error" | "success" | "info";

const STYLE: Record<Tone, string> = {
  error: "border-loss/30 bg-loss/[0.08] text-loss-soft",
  success: "border-emerald/30 bg-emerald/[0.08] text-emerald-soft",
  info: "border-gold/25 bg-gold/[0.06] text-gold-soft",
};
const ICON = { error: AlertCircle, success: CheckCircle2, info: Info };

/** Form-level message that stays until the next attempt (a toast would vanish
 *  before it is read). Errors interrupt a screen reader; the rest wait. */
export function FormAlert({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  const Icon = ICON[tone];
  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: 0.2 }}
      role={tone === "error" ? "alert" : "status"}
      className={cn("flex items-start gap-2.5 rounded-xl border px-3.5 py-3 text-[13px] leading-relaxed", STYLE[tone], className)}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div>{children}</div>
    </motion.div>
  );
}
