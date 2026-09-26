import { useEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

/**
 * Counts to a target whenever the target changes.
 *
 * `useCountUp` in the shared hooks fires once when it scrolls into view, which
 * is right for a statistic that never moves. This gauge is re-scored every
 * time the reader picks a different setup, so it needs to re-run — and it needs
 * to interrupt cleanly rather than queue, or two quick clicks race each other
 * to different values.
 */
function useAnimatedNumber(target: number, duration = 1100) {
  const reduced = useReducedMotion() ?? false;
  const [value, setValue] = useState(reduced ? target : 0);
  const frame = useRef<number>();
  const from = useRef(0);

  useEffect(() => {
    if (reduced) {
      setValue(target);
      return;
    }
    const start = performance.now();
    const origin = from.current;
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      const v = origin + (target - origin) * eased;
      setValue(v);
      from.current = v;
      if (p < 1) frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => {
      if (frame.current) cancelAnimationFrame(frame.current);
    };
  }, [target, duration, reduced]);

  return value;
}

/**
 * The conviction gauge.
 *
 * A 240° dial rather than a full ring: the gap at the bottom is where the
 * threshold marker and the verdict live, and a closed ring gives them nowhere
 * to sit that is not on top of the arc. Tick marks every ten points make the
 * distance to the bar readable without reading the number.
 */
export function ConvictionGauge({
  score,
  threshold = 60,
  verdict,
  size = 300,
}: {
  score: number;
  threshold?: number;
  verdict: "accepted" | "rejected";
  size?: number;
}) {
  const value = useAnimatedNumber(score);
  const reduced = useReducedMotion() ?? false;

  const SPAN = 240; // degrees of sweep
  const START = 150; // degrees, measured clockwise from 3 o'clock
  const R = 108;
  const CX = 128;
  const CY = 128;

  const polar = (deg: number, r: number) => {
    const rad = (deg * Math.PI) / 180;
    return { x: CX + r * Math.cos(rad), y: CY + r * Math.sin(rad) };
  };

  const arcPath = (fromPct: number, toPct: number, r: number) => {
    const a0 = START + (SPAN * fromPct) / 100;
    const a1 = START + (SPAN * toPct) / 100;
    const p0 = polar(a0, r);
    const p1 = polar(a1, r);
    const large = a1 - a0 > 180 ? 1 : 0;
    return `M ${p0.x} ${p0.y} A ${r} ${r} 0 ${large} 1 ${p1.x} ${p1.y}`;
  };

  const accepted = verdict === "accepted";
  const stroke = accepted ? "#F2C766" : "#A9832F";

  return (
    <div className="relative mx-auto" style={{ width: size, maxWidth: "100%" }}>
      <svg viewBox="0 0 256 256" className="w-full" role="img" aria-label={`Quality score ${Math.round(score)} of 100, minimum ${threshold}`}>
        <defs>
          <linearGradient id="nx-gauge" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#F2C766" />
            <stop offset="55%" stopColor="#EAB54F" />
            <stop offset="100%" stopColor="#C99A3A" />
          </linearGradient>
          <filter id="nx-gauge-glow" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="4" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* track */}
        <path d={arcPath(0, 100, R)} fill="none" stroke="#1B1710" strokeWidth="14" strokeLinecap="round" />

        {/* ten-point ticks */}
        {Array.from({ length: 11 }).map((_, i) => {
          const deg = START + (SPAN * (i * 10)) / 100;
          const outer = polar(deg, R + 13);
          const inner = polar(deg, R + (i % 5 === 0 ? 6 : 9));
          return (
            <line
              key={i}
              x1={inner.x}
              y1={inner.y}
              x2={outer.x}
              y2={outer.y}
              stroke={i * 10 >= threshold ? "rgba(242,199,102,0.4)" : "rgba(255,255,255,0.12)"}
              strokeWidth={i % 5 === 0 ? 1.6 : 1}
            />
          );
        })}

        {/* the qualifying band, from threshold to 100 */}
        <path
          d={arcPath(threshold, 100, R)}
          fill="none"
          stroke="#EAB54F"
          strokeOpacity="0.16"
          strokeWidth="14"
        />

        {/* the score arc */}
        <motion.path
          d={arcPath(0, Math.max(0.5, value), R)}
          fill="none"
          stroke={accepted ? "url(#nx-gauge)" : stroke}
          strokeWidth="14"
          strokeLinecap="round"
          filter={reduced ? undefined : "url(#nx-gauge-glow)"}
        />

        {/* threshold marker */}
        {(() => {
          const deg = START + (SPAN * threshold) / 100;
          const a = polar(deg, R - 12);
          const b = polar(deg, R + 12);
          return (
            <g>
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#E9EEF3" strokeWidth="2" opacity="0.85" />
              <text
                x={polar(deg, R + 26).x}
                y={polar(deg, R + 26).y + 3}
                textAnchor="middle"
                fill="rgba(255,255,255,0.45)"
                className="font-mono"
                style={{ fontSize: 9 }}
              >
                {threshold}
              </text>
            </g>
          );
        })()}

        {/* readout */}
        <text
          x={CX}
          y={CY + 6}
          textAnchor="middle"
          fill="#fff"
          className="tabular"
          style={{ fontSize: 58, fontWeight: 800, letterSpacing: "-0.03em" }}
        >
          {Math.round(value)}
        </text>
        <text
          x={CX}
          y={CY + 28}
          textAnchor="middle"
          fill="rgba(255,255,255,0.35)"
          className="font-mono"
          style={{ fontSize: 9, letterSpacing: "0.22em" }}
        >
          CONVICTION
        </text>
      </svg>

      {/* verdict plate, sitting in the dial's gap */}
      <div className="absolute inset-x-0 bottom-[6%] flex justify-center">
        <motion.span
          key={verdict + score}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, delay: 0.5 }}
          className={cn(
            "rounded-full border px-4 py-1.5 font-mono text-[11px] tracking-[0.18em]",
            accepted
              ? "border-gold/50 bg-gold/10 text-gold-soft shadow-[0_0_30px_-8px_rgba(234,181,79,0.8)]"
              : "border-white/12 bg-white/[0.03] text-white/45",
          )}
        >
          {accepted ? "QUALIFIED" : "DECLINED"}
        </motion.span>
      </div>
    </div>
  );
}
