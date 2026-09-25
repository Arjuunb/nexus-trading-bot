import { forwardRef, useState, type FocusEvent, type InputHTMLAttributes, type KeyboardEvent, type ReactNode } from "react";
import { Eye, EyeOff } from "lucide-react";
import { cn } from "@/lib/utils";
import { useFieldContext } from "./Field";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  icon?: ReactNode;
  invalid?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, icon, invalid, type, onKeyUp, onKeyDown, onBlur, "aria-describedby": ownDescribedBy, ...props }, ref) => {
    const field = useFieldContext();
    const [reveal, setReveal] = useState(false);
    const [capsLock, setCapsLock] = useState(false);
    const isPassword = type === "password";
    const inputType = isPassword ? (reveal ? "text" : "password") : type;
    const isInvalid = invalid || field?.invalid;
    const capsId = props.id ? `${props.id}-caps` : undefined;
    const describedBy = [ownDescribedBy, field?.describedBy, capsLock ? capsId : undefined]
      .filter(Boolean).join(" ") || undefined;

    // Caps Lock is the most common reason a correct password is rejected.
    const readCaps = (e: KeyboardEvent<HTMLInputElement>) => {
      if (isPassword && typeof e.getModifierState === "function") setCapsLock(e.getModifierState("CapsLock"));
    };

    return (
      <div>
        <div className="group relative">
          {icon && (
            <span className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-white/40 transition-colors duration-200 group-focus-within:text-gold/80">
              {icon}
            </span>
          )}
          <input
            ref={ref}
            type={inputType}
            aria-invalid={isInvalid || undefined}
            aria-describedby={describedBy}
            onKeyDown={(e) => { readCaps(e); onKeyDown?.(e); }}
            onKeyUp={(e) => { readCaps(e); onKeyUp?.(e); }}
            onBlur={(e: FocusEvent<HTMLInputElement>) => { setCapsLock(false); onBlur?.(e); }}
            className={cn(
              "h-11 w-full rounded-xl border bg-ink-700/60 text-sm text-white placeholder:text-white/35",
              "transition-[border-color,background-color,box-shadow] duration-200 outline-none",
              "focus:border-gold/50 focus:bg-ink-700/90 focus:ring-4 focus:ring-gold/10",
              "disabled:cursor-not-allowed disabled:opacity-60",
              icon ? "pl-10 pr-3.5" : "px-3.5",
              isPassword && "pr-12",
              isInvalid
                ? "border-loss/60 focus:border-loss/70 focus:ring-loss/10"
                : "border-line hover:border-line-strong",
              className,
            )}
            {...props}
          />
          {isPassword && (
            <button
              type="button"
              onClick={() => setReveal((r) => !r)}
              className={cn(
                "absolute right-1.5 top-1/2 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-lg",
                "text-white/45 transition-colors hover:bg-white/[0.06] hover:text-white/85",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60",
                "disabled:pointer-events-none",
              )}
              aria-label={reveal ? "Hide password" : "Show password"}
              aria-pressed={reveal}
              aria-controls={props.id}
              disabled={props.disabled}
            >
              {reveal ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
            </button>
          )}
        </div>
        {isPassword && capsLock ? (
          <p id={capsId} className="mt-1.5 text-xs text-gold-soft" role="status">
            Caps Lock is on
          </p>
        ) : null}
      </div>
    );
  },
);
Input.displayName = "Input";
