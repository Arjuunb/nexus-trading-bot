import { AnimatePresence, motion } from "framer-motion";
import { AlertCircle } from "lucide-react";
import { createContext, useContext, type ReactNode } from "react";
import { cn } from "@/lib/utils";

interface FieldContextValue {
  /** Ids of the elements that describe the control (error, description). */
  describedBy?: string;
  invalid: boolean;
}

const FieldContext = createContext<FieldContextValue | null>(null);

/** Lets a control inside a Field wire itself to the Field's error and help text. */
export const useFieldContext = () => useContext(FieldContext);

interface FieldProps {
  label: string;
  htmlFor?: string;
  error?: string;
  hint?: ReactNode;
  /** Help text under the control, announced with it by screen readers. */
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** Labelled form field with animated inline validation. The error and the
 *  description are linked to the control with aria-describedby, and the error
 *  region is live, so a screen reader hears a message when it appears. */
export function Field({ label, htmlFor, error, hint, description, children, className }: FieldProps) {
  const errorId = htmlFor ? `${htmlFor}-error` : undefined;
  const descriptionId = htmlFor && description ? `${htmlFor}-description` : undefined;
  const describedBy = [error ? errorId : undefined, descriptionId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-center justify-between">
        <label htmlFor={htmlFor} className="text-[13px] font-medium text-white/70">
          {label}
        </label>
        {hint}
      </div>
      <FieldContext.Provider value={{ describedBy, invalid: !!error }}>{children}</FieldContext.Provider>
      {description ? <div id={descriptionId}>{description}</div> : null}
      <div aria-live="polite">
        <AnimatePresence mode="wait" initial={false}>
          {error && (
            <motion.p
              key={error}
              id={errorId}
              initial={{ opacity: 0, y: -4, height: 0 }}
              animate={{ opacity: 1, y: 0, height: "auto" }}
              exit={{ opacity: 0, y: -4, height: 0 }}
              transition={{ duration: 0.18 }}
              className="flex items-center gap-1.5 text-xs text-loss"
            >
              <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              {error}
            </motion.p>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
