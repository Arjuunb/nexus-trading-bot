import { useMemo, useState } from "react";
import type { Candle } from "./useTape";
import { cn } from "@/lib/utils";

const UP = "#22C55E";
const DOWN = "#EF4444";

/**
 * A candlestick chart drawn as SVG.
 *
 * Charting libraries are between 40 and 200KB for a panel that shows one
 * symbol on one timeframe with no interaction beyond a crosshair — which is
 * more than this entire page. Fifty-six candles is a handful of rects and a
 * viewBox, and it inherits the palette instead of being themed against it.
 *
 * The price axis is derived from the visible window on every render, so the
 * chart re-scales as the series drifts rather than clipping at a fixed range.
 */
export function CandleChart({
  candles,
  entry,
  stop,
  target,
}: {
  candles: Candle[];
  /** Overlays for the open position, drawn as horizontal rules. */
  entry?: number;
  stop?: number;
  target?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const W = 720;
  const H = 300;
  const PAD_R = 62; // room for the price axis
  const VOL_H = 46;
  const plotW = W - PAD_R;
  const plotH = H - VOL_H - 12;

  const { min, max, bars, maxVol } = useMemo(() => {
    const loC = Math.min(...candles.map((c) => c.l));
    const hiC = Math.max(...candles.map((c) => c.h));
    const span = hiC - loC || 1;

    // An overlay widens the axis only while it is near the traded range. A stop
    // several spans away would otherwise compress every candle into a flat line
    // in order to keep itself on screen — which is exactly backwards, since the
    // candles are the subject and the rule is the annotation. Out-of-range
    // overlays are simply not drawn, the way a terminal scrolls them off.
    const near = (p?: number) => (p !== undefined && p > loC - span && p < hiC + span ? p : null);
    const overlays = [near(entry), near(stop), near(target)].filter((v): v is number => v !== null);

    const lo = Math.min(loC, ...overlays);
    const hi = Math.max(hiC, ...overlays);
    const pad = (hi - lo) * 0.1;
    return {
      min: lo - pad,
      max: hi + pad,
      bars: candles.length,
      maxVol: Math.max(...candles.map((c) => c.v)),
    };
  }, [candles, entry, stop, target]);

  const y = (p: number) => plotH - ((p - min) / (max - min)) * plotH + 6;
  const slot = plotW / bars;
  const bodyW = Math.max(2.5, slot * 0.62);

  const gridLines = useMemo(() => {
    const out: number[] = [];
    for (let i = 0; i <= 4; i++) out.push(min + ((max - min) / 4) * i);
    return out;
  }, [min, max]);

  const last = candles[candles.length - 1];
  const hovered = hover !== null ? candles[hover] : null;

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full select-none"
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label="Simulated candlestick chart with an open long position overlay"
      >
        {/* horizontal grid + price axis */}
        {gridLines.map((p) => (
          <g key={p}>
            <line x1="0" x2={plotW} y1={y(p)} y2={y(p)} stroke="rgba(255,255,255,0.05)" strokeWidth="1" />
            <text
              x={plotW + 8}
              y={y(p) + 3}
              fill="rgba(255,255,255,0.3)"
              className="font-mono tabular"
              style={{ fontSize: 9 }}
            >
              {p.toFixed(0)}
            </text>
          </g>
        ))}

        {/* position overlays — only while they are on the current axis */}
        {inView(entry, min, max) && (
          <Rule y={y(entry!)} w={plotW} color="#F2C766" label={`entry ${entry!.toFixed(0)}`} />
        )}
        {inView(stop, min, max) && (
          <Rule y={y(stop!)} w={plotW} color={DOWN} label={`stop ${stop!.toFixed(0)}`} dashed />
        )}
        {inView(target, min, max) && (
          <Rule y={y(target!)} w={plotW} color={UP} label={`target ${target!.toFixed(0)}`} dashed />
        )}

        {/* candles */}
        {candles.map((c, i) => {
          const x = i * slot + slot / 2;
          const up = c.c >= c.o;
          const color = up ? UP : DOWN;
          const top = y(Math.max(c.o, c.c));
          const bottom = y(Math.min(c.o, c.c));
          const isLast = i === bars - 1;
          return (
            <g
              key={i}
              onMouseEnter={() => setHover(i)}
              opacity={hover === null || hover === i ? 1 : 0.55}
              className="transition-opacity duration-150"
            >
              {/* generous invisible hit area — 4px candles are unhittable */}
              <rect x={x - slot / 2} y={0} width={slot} height={plotH + 12} fill="transparent" />
              <line x1={x} x2={x} y1={y(c.h)} y2={y(c.l)} stroke={color} strokeWidth="1" />
              <rect
                x={x - bodyW / 2}
                y={top}
                width={bodyW}
                height={Math.max(1, bottom - top)}
                fill={up ? `${color}dd` : color}
                stroke={color}
                strokeWidth="0.6"
              />
              {isLast && (
                <circle cx={x} cy={y(c.c)} r="2.6" fill={color}>
                  <animate attributeName="opacity" values="1;0.25;1" dur="1.6s" repeatCount="indefinite" />
                </circle>
              )}
              {/* volume */}
              <rect
                x={x - bodyW / 2}
                y={H - (c.v / maxVol) * VOL_H}
                width={bodyW}
                height={(c.v / maxVol) * VOL_H}
                fill={color}
                opacity="0.22"
              />
            </g>
          );
        })}

        {/* live price tag on the axis */}
        <g>
          <rect
            x={plotW + 2}
            y={y(last.c) - 8}
            width={PAD_R - 4}
            height="16"
            rx="3"
            fill={last.c >= last.o ? UP : DOWN}
          />
          <text
            x={plotW + 6}
            y={y(last.c) + 4}
            fill="#070708"
            className="font-mono tabular"
            style={{ fontSize: 9.5, fontWeight: 700 }}
          >
            {last.c.toFixed(1)}
          </text>
        </g>

        {/* crosshair */}
        {hover !== null && (
          <line
            x1={hover * slot + slot / 2}
            x2={hover * slot + slot / 2}
            y1="0"
            y2={H}
            stroke="rgba(255,255,255,0.18)"
            strokeDasharray="3 3"
            strokeWidth="1"
          />
        )}
      </svg>

      {/* OHLC readout — top-left, the way a terminal does it */}
      <div className="pointer-events-none absolute left-2 top-1 flex flex-wrap gap-x-3 font-mono text-[10px]">
        {(["o", "h", "l", "c"] as const).map((k) => {
          const c = hovered ?? last;
          return (
            <span key={k} className="text-white/30">
              {k.toUpperCase()}{" "}
              <span className={cn("tabular", c.c >= c.o ? "text-emerald-soft" : "text-loss-soft")}>
                {c[k].toFixed(1)}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

function inView(p: number | undefined, min: number, max: number): p is number {
  return p !== undefined && p >= min && p <= max;
}

function Rule({
  y,
  w,
  color,
  label,
  dashed,
}: {
  y: number;
  w: number;
  color: string;
  label: string;
  dashed?: boolean;
}) {
  return (
    <g>
      <line
        x1="0"
        x2={w}
        y1={y}
        y2={y}
        stroke={color}
        strokeWidth="1"
        strokeDasharray={dashed ? "4 4" : undefined}
        opacity="0.75"
      />
      <text x="4" y={y - 4} fill={color} className="font-mono" style={{ fontSize: 8.5 }} opacity="0.85">
        {label}
      </text>
    </g>
  );
}
