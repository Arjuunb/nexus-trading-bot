import { forwardRef, type ButtonHTMLAttributes } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "ghost" | "outline";
type Size = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  fullWidth?: boolean;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-gold-sheen text-ink font-semibold shadow-gold hover:brightness-[1.06] active:brightness-95",
  secondary:
    "bg-white/[0.06] text-white border border-line-strong hover:bg-white/[0.1] backdrop-blur",
  ghost: "text-white/80 hover:text-white hover:bg-white/[0.06]",
  outline: "border border-line-strong text-white hover:bg-white/[0.05]",
};

const SIZES: Record<Size, string> = {
  sm: "h-9 px-4 text-[13px] rounded-lg",
  md: "h-11 px-5 text-sm rounded-xl",
  lg: "h-13 px-7 text-[15px] rounded-xl",
};

/** Ripple-on-press premium button with an integrated loading state. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { className, variant = "primary", size = "md", loading, fullWidth, children, disabled, ...props },
    ref,
  ) => {
    return (
      <button
        ref={ref}
        disabled={disabled || loading}
        className={cn(
          "group relative inline-flex select-none items-center justify-center gap-2 overflow-hidden whitespace-nowrap font-medium transition-all duration-200 active:scale-[0.98]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/70 focus-visible:ring-offset-2 focus-visible:ring-offset-ink",
          "disabled:pointer-events-none disabled:opacity-60",
          SIZES[size],
          VARIANTS[variant],
          fullWidth && "w-full",
          className,
        )}
        {...props}
      >
        {/* sheen sweep on hover */}
        <span className="pointer-events-none absolute inset-0 -translate-x-full bg-gradient-to-r from-transparent via-white/25 to-transparent transition-transform duration-700 group-hover:translate-x-full" />
        {loading && <Loader2 className="h-4 w-4 animate-spin" />}
        {/* inline-flex, not a plain span: Tailwind's base makes every <svg>
            display:block, so an icon inside an inline span broke onto its own
            line ("Launch Platform" with the arrow underneath). */}
        <span className="relative inline-flex items-center gap-2">{children}</span>
      </button>
    );
  },
);
Button.displayName = "Button";
