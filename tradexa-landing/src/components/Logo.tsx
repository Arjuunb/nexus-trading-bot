import { useId } from "react";
import { cn } from "@/lib/utils";

/**
 * TradeLogX Nexus mark — a two-tone "N" monogram (silver ascent + gold descent)
 * inside a gold/steel ring, echoing an up/down market move. Matches the official
 * brand logo and the app favicon. Institutional: no cartoon robot, no crypto coin.
 */
export function LogoMark({ className }: { className?: string }) {
  // Gradient ids are per instance: a page often holds two marks (a mobile one
  // hidden with display:none and a desktop one), and a url(#id) that resolves
  // into the hidden copy paints nothing -- the mark lost its "N".
  const uid = useId().replace(/:/g, "");
  const split = `nxSplitL-${uid}`;
  const ring = `nxRingL-${uid}`;
  return (
    <svg viewBox="0 0 96 96" fill="none" className={cn("h-8 w-8", className)} aria-hidden="true">
      <defs>
        <linearGradient id={split} x1="24" y1="0" x2="72" y2="0" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#E9EEF3" />
          <stop offset=".46" stopColor="#AEB7C2" />
          <stop offset=".54" stopColor="#E7C766" />
          <stop offset="1" stopColor="#C6961F" />
        </linearGradient>
        <linearGradient id={ring} x1="14" y1="14" x2="82" y2="82" gradientUnits="userSpaceOnUse">
          <stop stopColor="#E7C766" /><stop offset=".5" stopColor="#8A929C" /><stop offset="1" stopColor="#C6961F" />
        </linearGradient>
      </defs>
      <circle cx="48" cy="48" r="41" stroke={`url(#${ring})`} strokeWidth="2.6" opacity="0.85" />
      <path d="M31 70 V32 M31 32 L65 70 M65 70 V26" stroke={`url(#${split})`} strokeWidth="11" strokeLinecap="butt" strokeLinejoin="miter" />
      <path d="M31 14 L40 30 H22 Z" fill="#E9EEF3" />
      <path d="M65 82 L56 66 H74 Z" fill="#C6961F" />
    </svg>
  );
}

export function Logo({ className, compact }: { className?: string; compact?: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <LogoMark />
      {!compact && (
        <span className="flex flex-col leading-none">
          <span className="text-[15px] font-bold tracking-tight text-white">TradeLogX</span>
          <span className="text-[10px] font-medium uppercase tracking-[0.28em] text-gold/80">
            Nexus
          </span>
        </span>
      )}
    </span>
  );
}
