import { useEffect, useRef, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from "react";
import echarts from "../../lib/echarts";
import type { EChartsOption } from "echarts";

interface EChartProps {
  option: EChartsOption;
  height?: number | string;
  className?: string;
  style?: React.CSSProperties;
  /** Optional renderer events for inspection-only chart interactions. */
  onEvents?: Record<string, (event: unknown) => void>;
  /** Preserve chart zoom/pan state across polling refreshes. */
  preserveInteraction?: boolean;
  /** Increment to reset a zoomable chart to its full loaded window. */
  fitContentSignal?: number;
  /** The useful local analysis range restored by Fit, rather than all history. */
  fitRange?: { start: number; end: number };
  /** Increment to restore the recent/live viewport after manual navigation. */
  latestSignal?: number;
  latestStart?: number;
  /** A timestamp/object navigation target expressed as a chart viewport. */
  focusWindow?: { key: string; start: number; end: number } | null;
  /** Dragging in the price-scale gutter changes only the display y-range. */
  onPriceAxisDrag?: (deltaY: number, shiftKey: boolean) => void;
  onResetPriceScale?: () => void;
  /** Marks the viewport as manually navigated without affecting chart data. */
  onChartPointerDown?: () => void;
  /** Keeps caller-owned display bounds synchronized with native slider/pan actions. */
  onViewportChange?: (range: { start: number; end: number }) => void;
  /** Keep the viewed candles fixed when a history page is prepended. */
  prependedData?: { version: number; count: number; total: number } | null;
}

type ZoomRange = { start: number; end: number };
type ChartDrag = { pointerId: number; x: number; y: number; range: ZoomRange; direction?: "horizontal" | "vertical" };

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

/**
 * Reusable Apache ECharts wrapper.
 * - Inits once, updates option reactively.
 * - Resizes with its container via ResizeObserver.
 * - Disposes on unmount (no leaks, no console errors).
 */
export default function EChart({ option, height = "100%", className, style, onEvents, preserveInteraction = false, fitContentSignal, fitRange, latestSignal, latestStart = 0, focusWindow, onPriceAxisDrag, onResetPriceScale, onChartPointerDown, onViewportChange, prependedData }: EChartProps) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const hasRenderedRef = useRef(false);
  const lastFocusKeyRef = useRef<string | null>(null);
  const priceDragRef = useRef<{ pointerId: number; y: number } | null>(null);
  const chartDragRef = useRef<ChartDrag | null>(null);
  const zoomRangeRef = useRef<ZoomRange>({ start: 0, end: 100 });
  const fitRangeRef = useRef<ZoomRange | undefined>(fitRange);
  const latestStartRef = useRef(latestStart);

  useEffect(() => { fitRangeRef.current = fitRange; }, [fitRange]);
  useEffect(() => { latestStartRef.current = latestStart; }, [latestStart]);

  useEffect(() => {
    if (!elRef.current) return;
    const chart = echarts.init(elRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(elRef.current);

    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  const readZoomRange = (): ZoomRange => {
    const current = (chartRef.current?.getOption().dataZoom as Array<{ start?: number; end?: number }> | undefined)?.[0];
    return {
      start: typeof current?.start === "number" ? current.start : zoomRangeRef.current.start,
      end: typeof current?.end === "number" ? current.end : zoomRangeRef.current.end,
    };
  };
  const applyZoomRange = (range: ZoomRange) => {
    const span = clamp(range.end - range.start, 1, 100);
    const start = clamp(range.start, 0, 100 - span);
    const next = { start, end: start + span };
    zoomRangeRef.current = next;
    chartRef.current?.dispatchAction({ type: "dataZoom", start: next.start, end: next.end });
  };

  useEffect(() => {
    if (!chartRef.current) return;
    // Live charts refresh their candles frequently. Applying a fresh dataZoom
    // configuration on every refresh silently snaps the user back to the
    // newest bar, which makes click-and-drag panning feel broken. Set the
    // initial viewport once, then leave the current zoom/pan state in place.
    if (preserveInteraction && hasRenderedRef.current) {
      const { dataZoom: _preservedDataZoom, ...optionWithoutDataZoom } = option;
      chartRef.current.setOption(optionWithoutDataZoom, { notMerge: false, lazyUpdate: true });
    } else {
      chartRef.current.setOption(option, true);
      hasRenderedRef.current = true;
    }
  }, [option, preserveInteraction]);

  useEffect(() => {
    if (fitContentSignal === undefined) return;
    applyZoomRange(fitRangeRef.current ?? { start: 0, end: 100 });
  }, [fitContentSignal]);

  useEffect(() => {
    if (latestSignal === undefined) return;
    applyZoomRange({ start: latestStartRef.current, end: 100 });
  }, [latestSignal]);

  useEffect(() => {
    if (!focusWindow || lastFocusKeyRef.current === focusWindow.key) return;
    lastFocusKeyRef.current = focusWindow.key;
    applyZoomRange(focusWindow);
  }, [focusWindow]);

  useEffect(() => {
    if (!prependedData || prependedData.count <= 0 || prependedData.total <= prependedData.count) return;
    // ECharts expresses the time viewport as percentages.  Inserting candles
    // at index zero would otherwise shift the same percentage to an older
    // screen position. Convert the current range to indices, offset it by the
    // page length, then convert it back to the enlarged series.
    const current = readZoomRange();
    const previousTotal = prependedData.total - prependedData.count;
    const next = {
      start: ((current.start / 100) * previousTotal + prependedData.count) / prependedData.total * 100,
      end: ((current.end / 100) * previousTotal + prependedData.count) / prependedData.total * 100,
    };
    applyZoomRange(next);
  }, [prependedData?.version]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const rememberZoom = () => {
      const range = readZoomRange();
      zoomRangeRef.current = range;
      onViewportChange?.(range);
    };
    chart.on("datazoom", rememberZoom);
    return () => { chart.off("datazoom", rememberZoom); };
  });

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !onEvents) return;
    for (const [event, handler] of Object.entries(onEvents)) chart.on(event, handler);
    return () => {
      for (const [event, handler] of Object.entries(onEvents)) chart.off(event, handler);
    };
  }, [onEvents]);

  const isPriceGutter = (event: ReactMouseEvent<HTMLDivElement> | ReactPointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    return event.clientX >= bounds.right - 80;
  };
  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!isPriceGutter(event)) {
      onChartPointerDown?.();
      chartDragRef.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, range: readZoomRange() };
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.style.cursor = "grabbing";
      return;
    }
    if (!onPriceAxisDrag) return;
    priceDragRef.current = { pointerId: event.pointerId, y: event.clientY };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const chartDrag = chartDragRef.current;
    if (chartDrag?.pointerId === event.pointerId) {
      const deltaX = event.clientX - chartDrag.x;
      const deltaY = event.clientY - chartDrag.y;
      if (!chartDrag.direction && Math.max(Math.abs(deltaX), Math.abs(deltaY)) >= 4) {
        chartDrag.direction = Math.abs(deltaX) >= Math.abs(deltaY) ? "horizontal" : "vertical";
      }
      if (chartDrag.direction === "horizontal") {
        const width = Math.max(1, event.currentTarget.getBoundingClientRect().width - 80);
        const shift = (deltaX / width) * (chartDrag.range.end - chartDrag.range.start);
        // Dragging right reveals older candles; dragging left moves toward the
        // latest candles and the explicit future slots.
        applyZoomRange({ start: chartDrag.range.start - shift, end: chartDrag.range.end - shift });
        event.preventDefault();
        event.stopPropagation();
      } else if (chartDrag.direction === "vertical" && onPriceAxisDrag) {
        // A vertical drag over the price field pans the manually selected
        // y-range. It is deliberately display-only.
        onPriceAxisDrag(deltaY, true);
        chartDrag.y = event.clientY;
        event.preventDefault();
        event.stopPropagation();
      }
      return;
    }
    const active = priceDragRef.current;
    if (!active || active.pointerId !== event.pointerId || !onPriceAxisDrag) return;
    const deltaY = event.clientY - active.y;
    if (deltaY) onPriceAxisDrag(deltaY, event.shiftKey);
    priceDragRef.current = { ...active, y: event.clientY };
    event.preventDefault();
  };
  const releasePriceDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (chartDragRef.current?.pointerId === event.pointerId) {
      const moved = Boolean(chartDragRef.current.direction);
      chartDragRef.current = null;
      event.currentTarget.style.cursor = "grab";
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      if (moved) { event.preventDefault(); event.stopPropagation(); }
      return;
    }
    if (priceDragRef.current?.pointerId !== event.pointerId) return;
    priceDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const onWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    const chart = chartRef.current;
    if (!chart) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const range = readZoomRange();
    const span = range.end - range.start;
    const x = clamp((event.clientX - bounds.left) / Math.max(1, bounds.width - 80), 0, 1);
    onChartPointerDown?.();
    if (Math.abs(event.deltaX) > Math.abs(event.deltaY) && !event.ctrlKey) {
      const shift = (event.deltaX / Math.max(1, bounds.width - 80)) * span;
      applyZoomRange({ start: range.start + shift, end: range.end + shift });
    } else {
      // Keep the candle under the cursor fixed while zooming so an older
      // CHoCH/FVG can be inspected without jumping to the latest bar.
      const factor = event.deltaY > 0 ? 1.18 : 0.84;
      const nextSpan = clamp(span * factor, 1, 100);
      const anchor = range.start + span * x;
      applyZoomRange({ start: anchor - nextSpan * x, end: anchor + nextSpan * (1 - x) });
    }
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <div
      ref={elRef}
      className={className}
      style={{ width: "100%", height, touchAction: "none", cursor: "grab", ...style }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={releasePriceDrag}
      onPointerCancel={releasePriceDrag}
      onWheel={onWheel}
      onDoubleClick={(event) => { if (onResetPriceScale && isPriceGutter(event)) onResetPriceScale(); }}
    />
  );
}
