import { Check, Circle } from "lucide-react";
import { PASSWORD_RULES, passwordStrength } from "@/lib/validation";
import { cn } from "@/lib/utils";

const SEGMENT = ["bg-white/10", "bg-loss", "bg-gold", "bg-emerald", "bg-emerald-soft"];
const TEXT = ["text-white/45", "text-loss", "text-gold-soft", "text-emerald-soft", "text-emerald-soft"];

/**
 * Strength meter plus the rule checklist, both computed from PASSWORD_RULES —
 * the same rules the form validates. Each rule is marked by an icon and a
 * word, not by colour alone, and the strength label is announced politely.
 */
export function PasswordStrength({ password }: { password: string }) {
  const { score, label } = passwordStrength(password);
  return (
    <div className="mt-2.5 space-y-2.5">
      <div className="flex items-center gap-2" aria-hidden={!password}>
        <div className="flex flex-1 gap-1">
          {[1, 2, 3, 4].map((i) => (
            <span
              key={i}
              className={cn(
                "h-1 flex-1 rounded-full transition-colors duration-300",
                password && i <= score ? SEGMENT[score] : "bg-white/10",
              )}
            />
          ))}
        </div>
        <span className={cn("w-20 text-right text-[11px] font-medium", password ? TEXT[score] : "text-white/35")} aria-live="polite">
          <span className="sr-only">Password strength: </span>
          {password ? label : "Not set"}
        </span>
      </div>
      <ul className="grid gap-1 sm:grid-cols-2" aria-label="Password requirements">
        {PASSWORD_RULES.map((rule) => {
          const met = rule.test(password);
          return (
            <li
              key={rule.key}
              className={cn(
                "flex items-start gap-1.5 text-[11.5px] leading-snug transition-colors duration-200",
                met ? "text-white/75" : "text-white/40",
                !rule.required && "sm:col-span-2",
              )}
            >
              {met ? (
                <Check className="mt-px h-3.5 w-3.5 shrink-0 text-emerald-soft" aria-hidden="true" />
              ) : (
                <Circle className="mt-px h-3.5 w-3.5 shrink-0 text-white/25" aria-hidden="true" />
              )}
              <span>
                {rule.label}
                <span className="sr-only">{met ? " — done" : " — not yet"}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Live "do the two passwords match" line under a confirm field. */
export function PasswordMatch({ password, confirm }: { password: string; confirm: string }) {
  if (!confirm) return null;
  const match = confirm === password;
  return (
    <p className={cn("mt-1.5 flex items-center gap-1.5 text-xs", match ? "text-emerald-soft" : "text-white/45")} aria-live="polite">
      {match ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Circle className="h-3.5 w-3.5 text-white/25" aria-hidden="true" />}
      {match ? "Passwords match" : "Doesn't match yet"}
    </p>
  );
}
