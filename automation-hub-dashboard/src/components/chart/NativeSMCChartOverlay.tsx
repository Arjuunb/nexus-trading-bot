import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EChartsOption } from "echarts";
import EChart from "./EChart";

export type Direction = "bullish" | "bearish" | "neutral";

export interface NativeCandle { timestamp: string; open: number; high: number; low: number; close: number; volume: number }
export interface NativePivot { id: string; scope: "internal" | "swing"; kind: "high" | "low"; price: number; occurred_at: string; confirmed_at: string; strength: "strong" | "weak" }
export interface NativeEvent { id: string; direction: Direction; event_type?: string; scope?: string; label?: string; level: number; occurred_at?: string; confirmed_at?: string; timestamp?: string; source_pivot_id?: string }
export interface NativeZone { id: string; direction: Direction; label?: string; top?: number; bottom?: number; high?: number; low?: number; created_at: string; active: boolean; mitigated: boolean; mitigation_at?: string | null; source_pivot_id?: string; source_structure_id?: string; lifecycle?: "active" | "invalidated" | "expired" }
/** `execution_allowed` is live routing and stays false; `paper_execution_allowed`
 *  is the separate permission to simulate, matching the Price Action engine. */
export interface NativeProposal { id: string; setup_id: string; snapshot_id: string; direction: Direction; entry: number; stop: number; target: number; rr_ratio: number; execution_allowed: false; paper_execution_allowed: true; risk_status: string }
export interface NativeSnapshot {
  id: string; candle_open: string; candle_close: string; htf_bias: number; htf_ema: number | null;
  swing_bias: number; internal_bias: number; session: string;
  dealing_range: { high: number; low: number; equilibrium: number; area: string };
  price_action: { bullish_rejection: boolean; bearish_rejection: boolean; body: number; upper_wick: number; lower_wick: number };
  active_setup_id: string | null; setup_phase: string | null; next_required_event: string;
  latest_sweep_id: string | null; event_ids: string[]; active_fvg_ids: string[]; active_ob_ids: string[]; proposal_ids: string[];
}
export interface NativeSMCChartState {
  research_id: string; execution_allowed: false; candles: NativeCandle[]; pivots: NativePivot[]; events: NativeEvent[];
  fair_value_gaps: NativeZone[]; order_blocks: NativeZone[]; proposals: NativeProposal[];
  snapshot: NativeSnapshot | null; selected_snapshot: NativeSnapshot | null; snapshot_ledger: NativeSnapshot[];
  /** A provider-supplied open candle for display. Never contributes to SMC state. */
  forming_candle?: NativeCandle | null;
  live_display?: {
    is_forming: boolean; observed_at: string; refresh_interval_seconds: number;
    candle_closes_at: string | null; last_price: number;
    price_direction?: "up" | "down" | "unchanged";
    bid?: number; ask?: number; mark?: number; funding_rate?: number | null;
    last_funding_time?: string | null; next_funding_time?: string | null; connection_state?: string; new_entries_paused?: boolean;
    health_reason?: string; quote_age_seconds?: number | null; mark_age_seconds?: number | null;
    candle_quote_deviation_bps?: number | null; reliable?: boolean; quote_source?: string;
    failing_dependency?: string | null;
    last_successful_event?: { kind: string; at: string } | null;
    retry_state?: { attempt?: number; automatic_retry?: boolean };
    price_updated_at?: string; source_mode?: "exchange_ohlcv_live_poll";
    execution_uses_closed_bars_only: true;
  };
}
export interface NativeSMCOverlayFilters {
  pivots: boolean; internal: boolean; swing: boolean; structure: boolean; liquidity: boolean;
  fvg: boolean; orderBlocks: boolean; mitigated: boolean; labels: boolean;
}

export interface SMCTradePlanOverlay {
  entry: number; stop: number; target_1: number; target_2: number;
}
export interface SMCFillOverlay {
  timestamp: string; price: number; side: string; realized_pnl?: number;
}

export interface ChartPriceViewport {
  auto: boolean;
  /** Higher values show a wider price range. */
  scale: number;
  /** Range-relative vertical displacement when auto-scale is disabled. */
  offset: number;
}

export interface ChartTimeViewport { start: number; end: number }

interface Props {
  state: NativeSMCChartState;
  /** The user-selected chart timeframe. Used only for display-range scaling. */
  timeframe?: string;
  /** Empty chart slots after the latest candle, for live-chart positioning. */
  rightOffsetBars?: number;
  /** Initial number of recent bars visible before the user pans or zooms. */
  initialVisibleBars?: number;
  filters: NativeSMCOverlayFilters;
  selectedObjectId?: string;
  /** Native object IDs selected by a read-only research trace. */
  highlightedObjectIds?: string[];
  onCandleSelect: (timestamp: string) => void;
  fitContentSignal: number;
  latestSignal?: number;
  centerTimestamp?: string;
  priceViewport?: ChartPriceViewport;
  /** Current display range, owned by the visual workspace only. */
  viewport?: ChartTimeViewport | null;
  onViewportChange?: (range: ChartTimeViewport) => void;
  /** Request one earlier exchange page only after the user reaches the left edge. */
  onHistoryNearStart?: () => void;
  historyLoading?: boolean;
  hasMoreHistory?: boolean;
  /** True while the viewport is intentionally browsing behind the live edge. */
  historicalMode?: boolean;
  onGoLive?: () => void;
  /** Versioned count of candles prepended by the read-only history page API. */
  prependedHistory?: { version: number; count: number } | null;
  onPriceAxisDrag?: (deltaY: number, shiftKey: boolean) => void;
  onResetPriceScale?: () => void;
  onChartPointerDown?: () => void;
  lightMode?: boolean;
  /** A failed feed freezes the last confirmed display price instead of inventing movement. */
  liveDataStale?: boolean;
  /** Neutral wording for consumers that reuse the closed-candle chart shell. */
  modelLabel?: string;
  /** Source-strategy plan projected verbatim; it never changes native state. */
  tradePlan?: SMCTradePlanOverlay | null;
  fillMarkers?: SMCFillOverlay[];
  height?: number | string;
}

const colorFor = (direction: Direction) => direction === "bullish" ? "#21c77a" : direction === "bearish" ? "#ef5b5b" : "#9ca3af";
const timestamp = (value: string) => value.replace("T", " ").replace("+00:00", " UTC").slice(0, 23);
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

type PriceAxisRange = { min: number; max: number };
type ChartPresentation = { option: EChartsOption; priceAxisRange: PriceAxisRange; livePrice: number | null; liveDirection: "up" | "down" | "unchanged" };

function priceDecimals(value: number): number {
  const magnitude = Math.abs(value);
  if (magnitude >= 1_000) return 1;
  if (magnitude >= 100) return 2;
  if (magnitude >= 1) return 3;
  return 5;
}

function formatPrice(value: number): string {
  const digits = priceDecimals(value);
  return value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatVolume(value: number): string {
  const magnitude = Math.abs(value);
  if (magnitude >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}B`;
  if (magnitude >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (magnitude >= 1_000) return `${(value / 1_000).toFixed(2)}K`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function compactCursorTime(value: string): string {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  return new Intl.DateTimeFormat(undefined, {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
    timeZone: "UTC",
  }).format(new Date(parsed)).replace(",", "") + " UTC";
}

function useCandleCountdown(candleClosesAt?: string | null, observedAt?: string): string {
  const [clientNow, setClientNow] = useState(() => Date.now());
  const serverOffsetRef = useRef(0);

  useEffect(() => {
    const observed = observedAt ? Date.parse(observedAt) : Number.NaN;
    if (Number.isFinite(observed)) serverOffsetRef.current = observed - Date.now();
    setClientNow(Date.now());
  }, [observedAt, candleClosesAt]);

  useEffect(() => {
    if (!candleClosesAt) return undefined;
    const timer = window.setInterval(() => setClientNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [candleClosesAt]);

  const closesAt = candleClosesAt ? Date.parse(candleClosesAt) : Number.NaN;
  if (!Number.isFinite(closesAt)) return "--:--";
  const seconds = Math.max(0, Math.ceil((closesAt - (clientNow + serverOffsetRef.current)) / 1_000));
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function LivePriceTicker({ price, range, direction, candleClosesAt, observedAt, stale, lightMode }: {
  price: number; range: PriceAxisRange; direction: "up" | "down" | "unchanged";
  candleClosesAt?: string | null; observedAt?: string; stale: boolean; lightMode: boolean;
}) {
  const countdown = useCandleCountdown(candleClosesAt, observedAt);
  const boundedRange = Math.max(range.max - range.min, Number.EPSILON);
  const position = clamp((range.max - price) / boundedRange, 0.025, 0.975);
  const tone = stale ? "stale" : direction;
  return <div
    className={`smc-live-price-ticker ${tone} ${lightMode ? "light" : ""}`}
    style={{ top: `calc(32px + ${(position * 67).toFixed(3)}%)` }}
    aria-label={`${stale ? "Stale" : "Live"} price ${formatPrice(price)}, candle closes in ${countdown}`}
  >
    <b>{formatPrice(price)}</b>
    <small>{stale ? "STALE" : countdown}</small>
  </div>;
}

// Premium/discount is a visual aid in this Lab, not a new native-SMC input.
// Each value represents a comparable recent market horizon for its timeframe,
// rather than the all-history range held in a native research snapshot.
const DISPLAY_RANGE_BARS: Record<string, number> = {
  "1m": 180, "3m": 150, "5m": 120, "15m": 110, "30m": 96,
  "1h": 72, "4h": 60, "1d": 60, "1w": 52,
};

const TIMEFRAME_MS: Record<string, number> = {
  "1m": 60_000, "3m": 3 * 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000, "30m": 30 * 60_000,
  "1h": 60 * 60_000, "4h": 4 * 60 * 60_000, "1d": 24 * 60 * 60_000, "1w": 7 * 24 * 60 * 60_000,
};

export interface DisplayDealingRange {
  high: number;
  low: number;
  equilibrium: number;
  start: string;
  end: string;
  bars: number;
}

export function timeframeDisplayRange(candles: NativeCandle[], timeframe = "5m", anchorTimestamp?: string): DisplayDealingRange | null {
  if (!candles.length) return null;
  const requestedIndex = anchorTimestamp ? candles.findIndex((row) => row.timestamp === anchorTimestamp) : -1;
  const endIndex = requestedIndex >= 0 ? requestedIndex : candles.length - 1;
  const bars = Math.min(DISPLAY_RANGE_BARS[timeframe] ?? 120, endIndex + 1);
  const rangeCandles = candles.slice(endIndex - bars + 1, endIndex + 1);
  let high = Number.NEGATIVE_INFINITY;
  let low = Number.POSITIVE_INFINITY;
  for (const candle of rangeCandles) {
    high = Math.max(high, candle.high);
    low = Math.min(low, candle.low);
  }
  return { high, low, equilibrium: (high + low) / 2, start: rangeCandles[0].timestamp, end: rangeCandles[rangeCandles.length - 1].timestamp, bars: rangeCandles.length };
}

function futureChartSlots(lastTimestamp: string | undefined, timeframe: string, count: number): string[] {
  const start = lastTimestamp ? Date.parse(lastTimestamp) : Number.NaN;
  const step = TIMEFRAME_MS[timeframe] ?? TIMEFRAME_MS["5m"];
  if (!Number.isFinite(start) || count <= 0) return [];
  return Array.from({ length: count }, (_, index) => new Date(start + step * (index + 1)).toISOString());
}

function chartOption(state: NativeSMCChartState, timeframe: string, rightOffsetBars: number, initialVisibleBars: number, filters: NativeSMCOverlayFilters, selectedObjectId?: string, highlightedObjectIds: string[] = [], lightMode = false, priceViewport?: ChartPriceViewport, viewport?: ChartTimeViewport | null, liveDataStale = false, modelLabel = "native SMC", tradePlan?: SMCTradePlanOverlay | null, fillMarkers: SMCFillOverlay[] = []): ChartPresentation {
  const closedCandles = state.candles;
  // The display candle intentionally remains outside the native model's
  // snapshots. It makes the chart feel live without turning an unclosed bar
  // into market-structure evidence.
  const hasFormingCandle = Boolean(state.forming_candle);
  const candles = hasFormingCandle ? [...closedCandles, state.forming_candle!] : closedCandles;
  const candleLabels = candles.map((row) => row.timestamp);
  const futureSlots = futureChartSlots(candleLabels[candleLabels.length - 1], timeframe, rightOffsetBars);
  const labels = [...candleLabels, ...futureSlots];
  const highlightedIds = new Set(highlightedObjectIds);
  const isHighlighted = (id: string) => id === selectedObjectId || highlightedIds.has(id);
  // A pan can only happen when the visible viewport is narrower than the
  // loaded history. Open on the recent working window, with right-side space
  // for the forming candle, just as a charting terminal does.
  const initialWindowBars = Math.min(labels.length, Math.max(24, initialVisibleBars + futureSlots.length));
  const initialStart = labels.length > initialWindowBars
    ? ((labels.length - initialWindowBars) / labels.length) * 100
    : 0;
  const labelSet = new Set(candleLabels);
  const first = candleLabels[0];
  const last = candleLabels[candleLabels.length - 1];
  const activeViewport = viewport ?? { start: initialStart, end: 100 };
  const viewportStartIndex = clamp(Math.floor((activeViewport.start / 100) * labels.length), 0, Math.max(0, labels.length - 1));
  const viewportEndIndex = clamp(Math.ceil((activeViewport.end / 100) * labels.length) - 1, viewportStartIndex, Math.max(0, labels.length - 1));
  const visibleStart = labels[viewportStartIndex] ?? first;
  const visibleEnd = candleLabels[Math.min(viewportEndIndex, candleLabels.length - 1)] ?? last;
  const visibleCandles = candles.slice(viewportStartIndex, Math.min(viewportEndIndex + 1, candles.length));
  // Do not reuse snapshot.dealing_range here: it is intentionally calculated
  // from the full engine history for native-state evidence. The chart overlay
  // needs a local, timeframe-aware viewing range instead.
  const range = timeframeDisplayRange(visibleCandles, timeframe);
  const rangeEnd = range?.end === visibleCandles[visibleCandles.length - 1]?.timestamp ? visibleEnd : range?.end;
  const eventAt = (row: NativeEvent) => row.confirmed_at ?? row.timestamp;
  const inWindow = (value?: string | null) => Boolean(value && labelSet.has(value) && value >= visibleStart && value <= visibleEnd);
  const overlapsVisibleWindow = (createdAt: string, removedAt?: string | null) => createdAt <= visibleEnd && (!removedAt || removedAt >= visibleStart);
  const spanStart = (value: string) => labelSet.has(value) ? value : first;
  const spanEnd = (value?: string | null) => value && labelSet.has(value) ? value : last;

  const visiblePivots = filters.pivots ? state.pivots.filter((row) =>
    inWindow(row.occurred_at) && (row.scope === "internal" ? filters.internal : filters.swing),
  ) : [];
  const visibleEvents = state.events.filter((row) => {
    if (row.event_type) return filters.structure && (row.scope === "internal" ? filters.internal : filters.swing) && inWindow(eventAt(row));
    return filters.liquidity && inWindow(row.timestamp);
  });
  const zones = filters.fvg ? state.fair_value_gaps.filter((row) => (filters.mitigated || !row.mitigated) && overlapsVisibleWindow(row.created_at, row.mitigation_at)) : [];
  const orderBlocks = filters.orderBlocks ? state.order_blocks.filter((row) => (filters.mitigated || !row.mitigated) && overlapsVisibleWindow(row.created_at, row.mitigation_at)) : [];
  const fvgAreas = zones.map((row) => [{
    name: row.label ?? `${row.direction === "bullish" ? "Bull" : "Bear"} FVG`,
    xAxis: spanStart(row.created_at), yAxis: row.bottom,
    itemStyle: { color: row.direction === "bullish" ? "rgba(34,197,94,.17)" : "rgba(239,91,91,.17)", borderColor: isHighlighted(row.id) ? "#ffffff" : undefined, borderWidth: isHighlighted(row.id) ? 2 : 0 },
  }, { xAxis: spanEnd(row.mitigation_at), yAxis: row.top }]);
  const obAreas = orderBlocks.map((row) => [{
    name: row.label ?? `${row.direction === "bullish" ? "Bull" : "Bear"} OB`,
    xAxis: spanStart(row.created_at), yAxis: row.low,
    itemStyle: {
      color: row.mitigated
        ? row.direction === "bullish" ? "rgba(59,130,246,.035)" : "rgba(168,85,247,.035)"
        : row.direction === "bullish" ? "rgba(59,130,246,.11)" : "rgba(168,85,247,.11)",
      borderColor: isHighlighted(row.id) ? "#ffffff" : row.mitigated ? "rgba(148,163,184,.30)" : undefined,
      borderWidth: isHighlighted(row.id) ? 2 : row.mitigated ? 1 : 0,
      borderType: row.mitigated ? "dashed" : "solid",
    },
  }, { xAxis: spanEnd(row.mitigation_at), yAxis: row.high }]);
  const pivotData = visiblePivots.map((row) => ({
    name: `${row.strength === "strong" ? "Strong" : "Weak"} ${row.kind === "high" ? "High" : "Low"}`,
    value: [row.occurred_at, row.price],
    itemStyle: { color: row.scope === "swing" ? "#eab54f" : "#69b9ff", borderColor: isHighlighted(row.id) ? "#ffffff" : undefined, borderWidth: isHighlighted(row.id) ? 2 : 0 },
    metadata: `${row.id}\n${row.scope} ${row.kind} · ${row.strength}\nOccurred: ${timestamp(row.occurred_at)}\nConfirmed: ${timestamp(row.confirmed_at)}`,
  }));
  const structureData = visibleEvents.map((row) => ({
    name: row.label ?? (row.event_type ? `${row.scope === "swing" ? "S" : "I"} ${row.event_type}` : `${row.direction === "bullish" ? "SSL swept" : "BSL swept"}`),
    value: [eventAt(row)!, row.level],
    itemStyle: { color: colorFor(row.direction), borderColor: isHighlighted(row.id) ? "#ffffff" : undefined, borderWidth: isHighlighted(row.id) ? 2 : 0 },
    metadata: `${row.id}\n${row.event_type ? `${row.scope} ${row.event_type}` : "Liquidity sweep"}\nLevel: ${row.level}\nConfirmed: ${timestamp(eventAt(row)!)}`,
  }));
  const fillData = fillMarkers.filter((row) => labelSet.has(row.timestamp)).map((row) => ({
    name: row.realized_pnl ? "EXIT" : "FILL", value: [row.timestamp, row.price],
    symbol: row.realized_pnl ? "diamond" : "triangle",
    itemStyle: { color: row.realized_pnl && row.realized_pnl < 0 ? "#ef5b5b" : "#21c77a" },
    metadata: `${row.realized_pnl ? "Paper exit" : "Paper fill"}\n${row.side} · ${row.price}\n${timestamp(row.timestamp)}`,
  }));
  const rangeAreas = range ? [
    [{ name: `DISCOUNT · ${range.bars} bars`, xAxis: range.start, yAxis: range.low, itemStyle: { color: "rgba(34,197,94,.06)" } }, { xAxis: rangeEnd, yAxis: range.equilibrium }],
    [{ name: `PREMIUM · ${range.bars} bars`, xAxis: range.start, yAxis: range.equilibrium, itemStyle: { color: "rgba(239,91,91,.06)" } }, { xAxis: rangeEnd, yAxis: range.high }],
  ] : [];
  const lastCandle = candles[candles.length - 1];
  const canvas = lightMode ? "#ffffff" : "#101216";
  const axis = lightMode ? "#d1d5db" : "#2a2f38";
  const text = lightMode ? "#344054" : "#98a2b3";
  // Price bounds deliberately come from the visible time range. An order
  // block far outside the user's current view must not flatten local candles.
  const priceSample = visibleCandles.length ? visibleCandles : candles.slice(-Math.min(initialVisibleBars, candles.length));
  const candleLow = priceSample.length ? Math.min(...priceSample.map((row) => row.low)) : 0;
  const candleHigh = priceSample.length ? Math.max(...priceSample.map((row) => row.high)) : 1;
  const candleSpan = Math.max(candleHigh - candleLow, Math.abs(candleHigh) * 0.002, 1);
  const nearbyProposals = state.proposals.filter((row) => row.entry >= candleLow - candleSpan * 0.5 && row.entry <= candleHigh + candleSpan * 0.5);
  const overlayLevels = [
    ...zones.flatMap((row) => [row.bottom, row.top]),
    ...orderBlocks.flatMap((row) => [row.low, row.high]),
    ...(range ? [range.low, range.high] : []),
    ...nearbyProposals.flatMap((row) => [row.entry, row.stop, row.target]),
    ...(tradePlan ? [tradePlan.entry, tradePlan.stop, tradePlan.target_1, tradePlan.target_2] : []),
  ].filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const rawLow = Math.min(candleLow, ...overlayLevels);
  const rawHigh = Math.max(candleHigh, ...overlayLevels);
  const rawSpan = Math.max(rawHigh - rawLow, Math.abs(rawHigh) * 0.002, 1);
  const proposalLines: any[] = nearbyProposals.flatMap((row) => [
    { yAxis: row.entry, name: "ENTRY", lineStyle: { color: "#65b7ff", width: 1.5 } },
    { yAxis: row.stop, name: "STOP LOSS", lineStyle: { color: "#ef5b5b", type: "dashed" } },
    { yAxis: row.target, name: "TAKE PROFIT", lineStyle: { color: "#21c77a", type: "dashed" } },
  ]);
  if (tradePlan) proposalLines.push(
    { yAxis: tradePlan.entry, name: "M1 ENTRY", lineStyle: { color: "#65b7ff", width: 2 } },
    { yAxis: tradePlan.stop, name: "M1 STOP", lineStyle: { color: "#ef5b5b", width: 2, type: "dashed" } },
    { yAxis: tradePlan.target_1, name: "M1 T1 · 50%", lineStyle: { color: "#21c77a", width: 1.5, type: "dashed" } },
    { yAxis: tradePlan.target_2, name: "M1 T2", lineStyle: { color: "#9be7c1", width: 2, type: "dashed" } },
  );
  const manualScale = Math.min(4, Math.max(0.18, priceViewport?.scale ?? 1));
  const manualCenter = (rawHigh + rawLow) / 2 + rawSpan * (priceViewport?.offset ?? 0);
  const pricePadding = rawSpan * 0.1;
  const priceAxisRange = priceViewport && !priceViewport.auto
    ? { min: manualCenter - (rawSpan * manualScale) / 2, max: manualCenter + (rawSpan * manualScale) / 2 }
    : { min: rawLow - pricePadding, max: rawHigh + pricePadding };

  const livePrice = state.live_display?.last_price ?? lastCandle?.close ?? null;
  const liveDirection = state.live_display?.price_direction ?? "unchanged";
  const liveColor = liveDataStale ? "#7c8797" : liveDirection === "up" ? "#21c77a" : liveDirection === "down" ? "#ef5b5b" : "#9ca3af";

  return { option: {
    // Exchange price changes are factual state updates. Never animate a line
    // between them: that would fabricate movement the venue did not report.
    animation: false,
    backgroundColor: canvas,
    axisPointer: {
      link: [{ xAxisIndex: [0, 1] }],
      snap: true,
      animation: false,
      label: { backgroundColor: lightMode ? "#e9edf3" : "#171a20", color: lightMode ? "#111827" : "#e5e7eb" },
    },
    tooltip: {
      trigger: "axis",
      triggerOn: "mousemove",
      // The compact React readout below is easier to scan than a floating
      // tooltip, while ECharts still owns the coordinate-accurate crosshair.
      showContent: false,
      showDelay: 90,
      hideDelay: 120,
      transitionDuration: 0.08,
      confine: true,
      axisPointer: { type: "cross" },
      formatter: (params: any) => {
        const custom = (params as any[]).find((row) => row.data?.metadata);
        if (custom?.data?.metadata) return custom.data.metadata.split("\n").join("<br/>");
        const candle = (params as any[]).find((row) => row.seriesName === "Market candles");
        const value = Array.isArray(candle?.data) ? candle.data : candle?.data?.value as number[] | undefined;
        const status = candle?.data?.forming ? "FORMING · visual only" : `CLOSED · ${modelLabel} eligible`;
        return value ? `${candle.axisValue}<br/><b>${status}</b><br/>O ${value[0]} · H ${value[3]} · L ${value[2]} · C ${value[1]}` : "";
      },
    },
    grid: [
      { left: 58, right: 74, top: 32, height: "67%" },
      { left: 58, right: 74, top: "79%", height: "13%" },
    ],
    xAxis: [
      { id: "smc-price-x", type: "category", data: labels, boundaryGap: true, axisLine: { lineStyle: { color: axis } }, axisLabel: { show: false }, axisPointer: { label: { show: false } } },
      { id: "smc-volume-x", type: "category", gridIndex: 1, data: labels, boundaryGap: true, axisLine: { lineStyle: { color: axis } }, axisLabel: { color: text, formatter: (value: string) => value.slice(5, 16), fontSize: 10 }, axisPointer: { label: { show: true, formatter: (item: any) => compactCursorTime(String(item.value)) } } },
    ],
    yAxis: [
      { id: "smc-price-y", scale: true, position: "right", axisLine: { lineStyle: { color: axis } }, axisLabel: { color: text, fontSize: 10 }, splitLine: { lineStyle: { color: lightMode ? "#edf0f5" : "#1d222b" } }, axisPointer: { label: { show: true, formatter: (item: any) => formatPrice(Number(item.value)) } }, ...priceAxisRange },
      { id: "smc-volume-y", gridIndex: 1, position: "right", axisLabel: { color: text, fontSize: 10 }, splitLine: { show: false } },
    ],
    dataZoom: [
      // Pointer and wheel movement are handled by the reusable EChart shell
      // so React refreshes cannot replace a library-owned pan/zoom range.
      // The slider remains available for direct time-scale manipulation.
      { id: "smc-inside-zoom", type: "inside", xAxisIndex: [0, 1], start: initialStart, end: 100, zoomOnMouseWheel: false, moveOnMouseMove: false, moveOnMouseWheel: false, preventDefaultMouseMove: false, cursorGrab: "grab", cursorGrabbing: "grabbing" },
      { id: "smc-slider-zoom", type: "slider", xAxisIndex: [0, 1], start: initialStart, end: 100, bottom: "2%", height: 16, borderColor: axis, fillerColor: "rgba(105,185,255,.14)", handleStyle: { color: "#69b9ff" }, textStyle: { color: text } },
    ],
    series: [
      {
        id: "smc-candles", type: "candlestick", name: "Market candles", xAxisIndex: 0, yAxisIndex: 0,
        data: [...candles.map((row, index) => ({
          value: [row.open, row.close, row.low, row.high],
          forming: hasFormingCandle && index === candles.length - 1,
          itemStyle: hasFormingCandle && index === candles.length - 1 ? { opacity: 0.78, borderWidth: 2 } : undefined,
        })), ...futureSlots.map(() => "-")], barMaxWidth: 18,
        itemStyle: { color: "#089981", color0: "#f23645", borderColor: "#089981", borderColor0: "#f23645" },
        markArea: { silent: true, label: { show: filters.labels, color: "#bac4d5", fontSize: 10 }, data: [...rangeAreas, ...fvgAreas, ...obAreas] },
        markLine: { silent: true, symbol: "none", label: { color: "#cfd6e4", fontSize: 10 }, data: [
          ...(range ? [
            { yAxis: range.high, name: `${timeframe} range high`, lineStyle: { color: "#ef5b5b", type: "dotted" }, label: { show: filters.labels } },
            { yAxis: range.equilibrium, name: `${timeframe} equilibrium ${range.equilibrium}`, lineStyle: { color: "#eab54f", type: "dashed" }, label: { show: filters.labels } },
            { yAxis: range.low, name: `${timeframe} range low`, lineStyle: { color: "#21c77a", type: "dotted" }, label: { show: filters.labels } },
          ] : []),
          ...(livePrice !== null ? [{
            yAxis: livePrice,
            name: hasFormingCandle ? "Live exchange price" : "Last closed price",
            lineStyle: { color: liveColor, width: hasFormingCandle ? 1.5 : 1, type: "dashed" },
            // The React ticker below is the axis-attached price label. Keeping
            // ECharts' own label off avoids two conflicting price readouts.
            label: { show: false },
          }] : []),
          ...(state.live_display?.bid ? [{ yAxis: state.live_display.bid, name: "Bid", lineStyle: { color: "#21c77a", width: 1, opacity: .55, type: "dotted" }, label: { show: false } }] : []),
          ...(state.live_display?.ask ? [{ yAxis: state.live_display.ask, name: "Ask", lineStyle: { color: "#ef5b5b", width: 1, opacity: .55, type: "dotted" }, label: { show: false } }] : []),
          ...(state.live_display?.mark ? [{ yAxis: state.live_display.mark, name: "Mark", lineStyle: { color: "#eab54f", width: 1, opacity: .65, type: "dashed" }, label: { show: false } }] : []),
          ...proposalLines,
        ] },
      },
      { id: "smc-volume", type: "bar", name: "Volume", xAxisIndex: 1, yAxisIndex: 1, data: [...candles.map((row, index) => ({ value: row.volume, itemStyle: { color: row.close >= row.open ? "rgba(8,153,129,.70)" : "rgba(242,54,69,.70)", opacity: hasFormingCandle && index === candles.length - 1 ? 0.72 : 1 } })), ...futureSlots.map(() => "-")], barMaxWidth: 18 },
      { id: "smc-pivots", type: "scatter", name: "Native pivots", xAxisIndex: 0, yAxisIndex: 0, data: pivotData as any[], symbolSize: 8, label: { show: filters.labels, formatter: (row: any) => row.data.name, color: "#d7deea", fontSize: 9, position: "top" }, labelLayout: { hideOverlap: true }, z: 8 },
      { id: "smc-structure", type: "scatter", name: "Native structure", xAxisIndex: 0, yAxisIndex: 0, data: structureData as any[], symbol: "diamond", symbolSize: 11, label: { show: filters.labels, formatter: (row: any) => row.data.name, color: "#d7deea", fontSize: 9, position: "bottom" }, labelLayout: { hideOverlap: true }, z: 9 },
      { id: "smc-paper-fills", type: "scatter", name: "SMC paper fills", xAxisIndex: 0, yAxisIndex: 0, data: fillData as any[], symbolSize: 13, label: { show: true, formatter: (row: any) => row.data.name, color: "#f8fafc", fontSize: 9, position: "top" }, labelLayout: { hideOverlap: true }, z: 12 },
    ],
  } as EChartsOption, priceAxisRange, livePrice, liveDirection };
}

export default function NativeSMCChartOverlay({ state, timeframe = "5m", rightOffsetBars = 12, initialVisibleBars = 120, filters, selectedObjectId, highlightedObjectIds, onCandleSelect, fitContentSignal, latestSignal, centerTimestamp, priceViewport, viewport, onViewportChange, onHistoryNearStart, historyLoading = false, hasMoreHistory = true, historicalMode = false, onGoLive, prependedHistory, onPriceAxisDrag, onResetPriceScale, onChartPointerDown, lightMode = false, liveDataStale = false, modelLabel = "native SMC", tradePlan, fillMarkers = [], height = 700 }: Props) {
  const presentation = useMemo(() => chartOption(state, timeframe, rightOffsetBars, initialVisibleBars, filters, selectedObjectId, highlightedObjectIds, lightMode, priceViewport, viewport, liveDataStale, modelLabel, tradePlan, fillMarkers), [state, timeframe, rightOffsetBars, initialVisibleBars, filters, selectedObjectId, highlightedObjectIds, lightMode, priceViewport, viewport, liveDataStale, modelLabel, tradePlan, fillMarkers]);
  const labels = state.candles.length + (state.forming_candle ? 1 : 0) + Math.max(0, rightOffsetBars);
  const localWindowBars = Math.min(labels, Math.max(24, initialVisibleBars + rightOffsetBars));
  const currentSpanBars = viewport ? Math.max(24, Math.round(((viewport.end - viewport.start) / 100) * labels)) : localWindowBars;
  const newestWindow = useMemo(() => {
    const span = Math.min(labels, currentSpanBars);
    return { start: Math.max(0, ((labels - span) / Math.max(1, labels)) * 100), end: 100 };
  }, [labels, currentSpanBars]);
  const windowAround = useCallback((span: number) => {
    if (!centerTimestamp) return null;
    const candleIndex = state.candles.findIndex((row) => row.timestamp === centerTimestamp);
    if (candleIndex < 0 || labels <= 1) return null;
    const boundedSpan = Math.min(labels, span);
    const startIndex = Math.max(0, Math.min(labels - boundedSpan, candleIndex - Math.floor(boundedSpan / 2)));
    return { key: centerTimestamp, start: (startIndex / labels) * 100, end: (Math.min(labels, startIndex + boundedSpan) / labels) * 100 };
  }, [centerTimestamp, state.candles, labels]);
  // Selecting an evidence item keeps whatever zoom level the analyst chose.
  // Fit deliberately returns to a concise local range instead of full history.
  const focusWindow = useMemo(() => windowAround(currentSpanBars), [windowAround, currentSpanBars]);
  const fitWindow = useMemo(() => windowAround(localWindowBars), [windowAround, localWindowBars]);
  const allCandles = useMemo(() => state.forming_candle ? [...state.candles, state.forming_candle] : state.candles, [state.candles, state.forming_candle]);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const inspectedCandle = hoveredIndex !== null && hoveredIndex >= 0 && hoveredIndex < allCandles.length
    ? allCandles[hoveredIndex]
    : allCandles[allCandles.length - 1];
  const handleViewportChange = useCallback((range: ChartTimeViewport) => {
    onViewportChange?.(range);
    if (range.start <= 3.5 && hasMoreHistory && !historyLoading) onHistoryNearStart?.();
  }, [onViewportChange, onHistoryNearStart, hasMoreHistory, historyLoading]);
  const events = useMemo(() => ({
    click: (event: any) => {
      if (event?.seriesName !== "Market candles" || typeof event.dataIndex !== "number") return;
      // There is intentionally no state snapshot for the still-forming candle.
      if (event.dataIndex >= state.candles.length) return;
      const candle = state.candles[event.dataIndex];
      if (candle) onCandleSelect(candle.timestamp);
    },
    updateAxisPointer: (event: any) => {
      const axis = (event?.axesInfo ?? []).find((item: any) => item.axisDim === "x" && Number.isFinite(Number(item.value)));
      const index = axis ? Number(axis.value) : -1;
      setHoveredIndex(index >= 0 && index < allCandles.length ? index : null);
    },
    globalout: () => setHoveredIndex(null),
  }), [onCandleSelect, state.candles, allCandles.length]);
  const live = state.live_display;
  return <div className="smc-chart-canvas" style={{ height }}>
    <EChart option={presentation.option} height="100%" onEvents={events} preserveInteraction fitContentSignal={fitContentSignal} fitRange={fitWindow ?? { start: Math.max(0, ((labels - localWindowBars) / Math.max(1, labels)) * 100), end: 100 }} latestSignal={latestSignal} latestStart={newestWindow.start} focusWindow={focusWindow} onViewportChange={handleViewportChange} prependedData={prependedHistory ? { ...prependedHistory, total: labels } : null} onPriceAxisDrag={onPriceAxisDrag} onResetPriceScale={onResetPriceScale} onChartPointerDown={onChartPointerDown} style={{ borderRadius: 8 }} />
    {inspectedCandle ? <div className={`smc-ohlc-readout ${inspectedCandle.close >= inspectedCandle.open ? "bullish" : "bearish"}`} aria-live="polite"><b>{compactCursorTime(inspectedCandle.timestamp)}</b><span>O {formatPrice(inspectedCandle.open)}</span><span>H {formatPrice(inspectedCandle.high)}</span><span>L {formatPrice(inspectedCandle.low)}</span><span>C {formatPrice(inspectedCandle.close)}</span><span>Vol {formatVolume(inspectedCandle.volume)}</span>{hoveredIndex !== null ? <em>CURSOR</em> : <em>LATEST</em>}</div> : null}
    {historyLoading ? <span className="smc-history-loading">Loading history…</span> : null}
    {historicalMode && onGoLive ? <button type="button" className="smc-go-live" onClick={onGoLive}>→ Live</button> : null}
    {live && presentation.livePrice !== null ? <LivePriceTicker price={presentation.livePrice} range={presentation.priceAxisRange} direction={presentation.liveDirection} candleClosesAt={live.candle_closes_at} observedAt={live.observed_at} stale={liveDataStale} lightMode={lightMode} /> : null}
  </div>;
}
