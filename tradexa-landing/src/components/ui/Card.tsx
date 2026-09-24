import type { HTMLAttributes, MouseEvent } from "react";
import { cn } from "@/lib/utils";

/**
 * Hairline surface card. `interactive` adds the hover lift and a gold light
 * that follows the pointer across the surface and its border (`.spotlight`,
 * index.css). The pointer position is written to two CSS variables, so the
 * light moves without a React render per mouse event.
 */
export function Card({
  className,
  interactive,
  onMouseMove,
  ...props
}: HTMLAttributes<HTMLDivElement> & { interactive?: boolean }) {
  const track = (event: MouseEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    event.currentTarget.style.setProperty("--mx", `${event.clientX - box.left}px`);
    event.currentTarget.style.setProperty("--my", `${event.clientY - box.top}px`);
    onMouseMove?.(event);
  };
  return (
    <div
      className={cn(
        "surface relative overflow-hidden",
        interactive &&
          "spotlight transition-all duration-300 hover:border-line-strong hover:shadow-card hover:-translate-y-0.5",
        className,
      )}
      onMouseMove={interactive ? track : onMouseMove}
      {...props}
    />
  );
}
