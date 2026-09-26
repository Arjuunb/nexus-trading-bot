import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useReducedMotion } from "framer-motion";
import { useVisibleActive } from "@/lib/useVisibleActive";

export interface Candle {
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  /** Sequence index, used only for the axis labels. */
  t: number;
}

/**
 * A deterministic market simulation.
 *
 * The terminal needs to *move* — a static screenshot of a chart makes the
 * claim "live" and then immediately contradicts it. But it must not claim to
 * be real: nothing here touches an exchange, and every visitor sees the same
 * opening series because the walk is seeded.
 *
 * One clock drives the whole page. Four panels each running their own
 * `setInterval` would drift apart within seconds, and a decision that updates
 * on a different beat from the candle it belongs to reads as broken rather
 * than fast.
 */

/** Mulberry32 — small, fast, and identical across browsers. */
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const COUNT = 56;
const BASE = 68_400;

function seedCandles(): Candle[] {
  const r = rng(20260730);
  const out: Candle[] = [];
  let price = BASE;
  for (let i = 0; i < COUNT; i++) {
    // Amplitude is tuned so 56 bars cover roughly two percent — the range a
    // 15m BTC chart actually shows, and wide enough that the position's stop
    // and target sit on the same axis as the candles rather than off it.
    const drift = (r() - 0.46) * 300;
    const o = price;
    const c = o + drift;
    const wick = Math.abs(drift) * (0.4 + r() * 0.9) + 18;
    out.push({
      o,
      c,
      h: Math.max(o, c) + wick * r(),
      l: Math.min(o, c) - wick * r(),
      v: 0.3 + r() * 0.7,
      t: i,
    });
    price = c;
  }
  return out;
}

export interface TapeState {
  candles: Candle[];
  price: number;
  /** Change over the visible window, in percent. */
  changePct: number;
  /** Increments once per candle close — panels use it to advance in step. */
  epoch: number;
}

/**
 * @param ref The terminal's container. The tape only advances while that
 *   container is on screen in a foreground tab — a 900ms interval that
 *   re-renders four panels is not something to leave running in a background
 *   tab, and `setInterval` (unlike rAF) is not throttled there.
 */
export function useTape(ref: RefObject<Element | null>): TapeState {
  const reduced = useReducedMotion() ?? false;
  const active = useVisibleActive(ref);
  const initial = useMemo(seedCandles, []);
  const [candles, setCandles] = useState<Candle[]>(initial);
  const [epoch, setEpoch] = useState(0);
  const rand = useRef(rng(991));
  const ticks = useRef(0);

  useEffect(() => {
    if (reduced || !active) return;
    const id = window.setInterval(() => {
      ticks.current += 1;
      const closing = ticks.current % 6 === 0;

      setCandles((prev) => {
        const next = prev.slice();
        const last = { ...next[next.length - 1] };
        const r = rand.current;
        const step = (r() - 0.48) * 165;
        last.c = last.c + step;
        last.h = Math.max(last.h, last.c);
        last.l = Math.min(last.l, last.c);
        last.v = Math.min(1, last.v + r() * 0.08);
        next[next.length - 1] = last;

        if (!closing) return next;

        // A new candle opens at the previous close and the window scrolls,
        // which is what gives the chart its left-to-right drift.
        next.push({ o: last.c, c: last.c, h: last.c, l: last.c, v: 0.15 + r() * 0.2, t: last.t + 1 });
        return next.slice(-COUNT);
      });

      if (closing) setEpoch((e) => e + 1);
    }, 900);
    return () => window.clearInterval(id);
  }, [reduced, active]);

  const price = candles[candles.length - 1].c;
  const first = candles[0].o;
  const changePct = ((price - first) / first) * 100;

  return { candles, price, changePct, epoch };
}
