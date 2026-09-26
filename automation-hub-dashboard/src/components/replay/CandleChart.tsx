import { useEffect, useRef } from "react";
import echarts from "../../lib/echarts";
import type { EChartsOption } from "echarts";
import type { ReplayData } from "../../lib/api";

export interface ChartToggles {
  ema8: boolean; ema20: boolean; ema30: boolean; ema50: boolean;
  sma20: boolean; sma50: boolean; vwap: boolean; bb: boolean; volume: boolean;
  structure: boolean; zones: boolean;
  osc: "none" | "rsi" | "macd" | "atr";
  supertrend?: boolean;   // draw the strategy's ATR Supertrend line
  crossovers?: boolean;   // draw the strategy's EMA-cross markers
}

/** Extra price-pane overlay lines the ACTIVE strategy uses but that aren't one
 *  of the fixed toggles above (e.g. a custom EMA/SMA period). Each key must be
 *  present in data.overlays — the series is server-computed and causal. */
export interface ExtraLine { key: string; name: string; color: string; dashed?: boolean; }

/** Horizontal grid lines overlaid on the price pane (Grid strategy tester).
 *  buy = below price (green), sell = above (red), band edges emphasised. */
export interface GridLine { price: number; side: "buy" | "sell"; edge?: boolean; }

/** A user-drawn horizontal price level — persisted per symbol/timeframe. These
 *  are the trader's own annotations (clearly distinct from strategy-computed
 *  overlays); nothing here is invented by the app. */
export interface PriceLine { id: string; price: number; color: string; label: string; alert?: boolean; }

export type ChartType = "candles" | "line" | "area";
export type DrawTool = "none" | "trend" | "rect" | "fib";

/** A two-point drawing on the price pane. Points are DATA coords [candleIndex,
 *  price], so ECharts positions them correctly through zoom/pan automatically.
 *  These are the trader's own drawings — never strategy data. */
export interface Shape { id: string; kind: "trend" | "rect" | "fib"; p1: [number, number]; p2: [number, number]; color: string; }

/** The LIVE open paper position's real, engine-enforced levels. Drawn as
 *  draggable SL/TP handles — every value comes from the backend (ledger stop +
 *  engine target), never invented. Dragging commits back through the API. */
export interface LiveLevels { side: "long" | "short"; entry: number; stop: number | null; target: number | null; }

/** An actual fill from the application's paper ledger, rendered separately
 * from a replay strategy marker. Times and prices are supplied by the backend;
 * the chart only anchors them to the nearest displayed candle. */
export interface PaperFillMarker {
  id: string;
  side: "long" | "short";
  entry: number;
  openedAt: string | null;
  exit: number | null;
  closedAt: string | null;
  pnl: number | null;
}

/** Chart appearance settings (persisted). Colours affect real candles only. */
export interface ChartSettings { upColor: string; downColor: string; grid: boolean; crosshair: boolean; priceLine: boolean; volProfile: boolean; paperFills: boolean; }
export const DEFAULT_SETTINGS: ChartSettings = { upColor: "#089981", downColor: "#f23645", grid: true, crosshair: true, priceLine: true, volProfile: false, paperFills: true };

const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

interface Props {
  data: ReplayData;
  index: number; // current replay bar (inclusive); chart shows [0..index]
  toggles: ChartToggles;
  height?: number;
  extraLines?: ExtraLine[];
  gridLines?: GridLine[];
  chartType?: ChartType;
  drawings?: PriceLine[];
  shapes?: Shape[];
  drawTool?: DrawTool;
  onAddShape?: (p1: [number, number], p2: [number, number]) => void;
  settings?: ChartSettings;
  liveLevels?: LiveLevels | null;
  paperFills?: PaperFillMarker[];
  onPaperFillSelect?: (tradeId: string) => void;
  onCommitLevel?: (kind: "stop" | "target", price: number) => void;
}

const fmt = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 2 });
const volFmt = (v: number) => (v >= 1_000_000 ? (v / 1e6).toFixed(1) + "M" : v >= 1000 ? (v / 1000).toFixed(0) + "K" : v.toFixed(0));
const WINDOW = 130; // visible candles by default — proper spacing instead of cramming all
const num = (a: (number | null)[] | undefined, end: number) => (a ? a.slice(0, end) : []);

/** TradingView-style candlestick: price axis (right), time axis (bottom),
 *  zoom/scroll/pan, crosshair, current-price tag, OHLC readout, toggleable
 *  indicators (EMA/SMA/VWAP/Bollinger), entry/exit markers, SL/TP lines +
 *  risk/reward zones, a volume pane and an optional oscillator pane (RSI /
 *  MACD / ATR). Renders ONLY candles up to `index` — the future is never drawn.
 *  Every indicator drawn here is a real, server-computed causal series. */
export default function CandleChart({ data, index, toggles, height = 520, extraLines, gridLines, chartType = "candles", drawings, shapes, drawTool = "none", onAddShape, settings = DEFAULT_SETTINGS, liveLevels, paperFills, onPaperFillSelect, onCommitLevel }: Props) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  // refs so the persistent zr click handler always sees the latest tool/callback
  const toolRef = useRef<DrawTool>(drawTool);
  const addRef = useRef<Props["onAddShape"]>(onAddShape);
  const paperFillSelectRef = useRef<Props["onPaperFillSelect"]>(onPaperFillSelect);
  const commitRef = useRef<Props["onCommitLevel"]>(onCommitLevel);
  const pendingRef = useRef<[number, number] | null>(null);
  useEffect(() => { toolRef.current = drawTool; if (drawTool === "none") pendingRef.current = null; }, [drawTool]);
  useEffect(() => { addRef.current = onAddShape; }, [onAddShape]);
  useEffect(() => { paperFillSelectRef.current = onPaperFillSelect; }, [onPaperFillSelect]);
  useEffect(() => { commitRef.current = onCommitLevel; }, [onCommitLevel]);

  useEffect(() => {
    if (!elRef.current) return;
    const chart = echarts.init(elRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(elRef.current);
    // two-click drawing: convert pixel → [candleIndex, price] on the price grid
    chart.getZr().on("click", (e: any) => {
      if (toolRef.current === "none") return;
      const pt = chart.convertFromPixel({ gridIndex: 0 }, [e.offsetX, e.offsetY]) as number[] | null;
      if (!pt || pt.length < 2 || !isFinite(pt[0]) || !isFinite(pt[1])) return;
      const p: [number, number] = [Math.round(pt[0]), pt[1]];
      if (!pendingRef.current) { pendingRef.current = p; return; }
      const p1 = pendingRef.current; pendingRef.current = null;
      addRef.current?.(p1, p);
    });
    // Ledger-fill markers are an audit entry point: a click opens the exact
    // corresponding journal, never a reconstructed or inferred record.
    chart.on("click", (event: any) => {
      const tradeId = event?.data?.paperTradeId;
      if (typeof tradeId === "string" && tradeId) paperFillSelectRef.current?.(tradeId);
    });
    return () => { ro.disconnect(); chart.dispose(); chartRef.current = null; };
  }, []);

  // resize when the container height changes (e.g. fullscreen)
  useEffect(() => { chartRef.current?.resize(); }, [height]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const end = Math.min(index + 1, data.candles.length);
    const c = data.candles.slice(0, end);
    if (c.length === 0) { chart.clear(); return; }
    const ov = data.overlays as Record<string, (number | null)[]>;
    const cats = c.map((b) => b.t.replace("T", " ").slice(5, 16));
    const ohlc = c.map((b) => [b.o, b.c, b.l, b.h]); // ECharts: open,close,low,high
    const vol = c.map((b) => ({ value: b.v, itemStyle: { color: b.c >= b.o ? settings.upColor : settings.downColor } }));

    const last = c[c.length - 1];
    const prev = c[c.length - 2];
    const chg = prev ? ((last.c - prev.c) / prev.c) * 100 : 0;
    const upCol = last.c >= last.o ? settings.upColor : settings.downColor;

    // ---- dynamic pane layout: price + optional volume + optional oscillator ----
    const hasVol = toggles.volume;
    const hasOsc = toggles.osc !== "none";
    const sub = (hasVol ? 1 : 0) + (hasOsc ? 1 : 0);
    // Give the price pane most of the height and keep gaps tight so the chart
    // fills the card instead of leaving blank bands between panes.
    const L = sub === 0 ? { price: [4, 88] as [number, number] }
      : sub === 1 ? { price: [4, 70] as [number, number], a: [78, 16] as [number, number] }
        : { price: [4, 58] as [number, number], a: [66, 12] as [number, number], b: [81, 14] as [number, number] };
    const seg = (k: "price" | "a" | "b") => (L as any)[k] as [number, number];
    const grids: any[] = [{ left: 56, right: 64, top: `${seg("price")[0]}%`, height: `${seg("price")[1]}%` }];
    let volGrid = -1, oscGrid = -1;
    if (hasVol) { volGrid = grids.length; const s = seg("a"); grids.push({ left: 56, right: 64, top: `${s[0]}%`, height: `${s[1]}%` }); }
    if (hasOsc) { oscGrid = grids.length; const s = seg(hasVol ? "b" : "a"); grids.push({ left: 56, right: 64, top: `${s[0]}%`, height: `${s[1]}%` }); }

    const xAxis: any[] = grids.map((_, gi) => ({
      type: "category", data: cats, gridIndex: gi, boundaryGap: true,
      axisLine: { lineStyle: { color: "#2a2a2f" } }, axisTick: { show: false },
      axisLabel: gi === grids.length - 1 ? { color: "#8a93a6", fontSize: 10, hideOverlap: true } : { show: false },
      axisPointer: { label: { show: gi === grids.length - 1 } },
    }));
    const yAxis: any[] = [{ scale: true, position: "right", gridIndex: 0,
      axisLabel: { color: "#8a93a6", fontSize: 10, formatter: (v: number) => fmt(v) },
      splitLine: { show: settings.grid, lineStyle: { color: "#161618" } } }];
    if (hasVol) yAxis.push({ scale: true, position: "right", gridIndex: volGrid, name: "Vol",
      nameTextStyle: { color: "#5b6478", fontSize: 9 },
      axisLabel: { color: "#5b6478", fontSize: 9, formatter: (v: number) => volFmt(v) }, splitLine: { show: false } });
    if (hasOsc) yAxis.push({ scale: toggles.osc !== "rsi", min: toggles.osc === "rsi" ? 0 : undefined,
      max: toggles.osc === "rsi" ? 100 : undefined, position: "right", gridIndex: oscGrid,
      name: toggles.osc.toUpperCase(), nameTextStyle: { color: "#5b6478", fontSize: 9 },
      axisLabel: { color: "#5b6478", fontSize: 9 }, splitLine: { show: false } });

    // ---- markers: structure + entry, plus closed-trade exits ----
    const STRUCT = new Set(["Sweep", "BOS/CHoCH", "FVG"]);
    const showMarker = (t: string) =>
      t === "EMA Cross" ? !!toggles.crossovers
        : STRUCT.has(t) ? toggles.structure
          : true;
    const markPts = data.markers
      .filter((m) => m.idx < end && showMarker(m.type))
      .map((m) => ({
        name: m.type, coord: [m.idx, m.price],
        symbol: m.type === "Entry" ? "pin" : m.type === "EMA Cross" ? "triangle" : "circle",
        symbolSize: m.type === "Entry" ? 26 : m.type === "TP1" ? 14 : m.type === "EMA Cross" ? 11 : 9,
        symbolRotate: m.type === "EMA Cross" && m.side === "bear" ? 180 : 0,
        itemStyle: { color: m.side === "bull" ? "#089981" : "#f23645" },
        label: { show: m.type === "Entry", formatter: m.type, position: "top", color: "#cfd6e4", fontSize: 9 },
      }));
    for (const t of data.trades) {
      if (t.exit_idx !== null && t.exit_idx <= index && t.exit !== null) {
        markPts.push({
          name: "Exit", coord: [t.exit_idx, t.exit], symbol: "diamond", symbolSize: 13,
          itemStyle: { color: t.result === "Winner" ? "#089981" : "#f23645" },
          label: { show: false, formatter: "", position: "bottom", color: "#cfd6e4", fontSize: 9 },
        } as any);
      }
    }
    // Real paper fills from the ledger. These intentionally do not reuse the
    // strategy replay markers above: a real engine order is evidence, whereas
    // a replay marker is a simulation. Only markers whose timestamp belongs to
    // the currently visible chart window are drawn.
    const nearestCandle = (ts: string | null): number | null => {
      const target = ts ? Date.parse(ts) : Number.NaN;
      if (!Number.isFinite(target)) return null;
      const first = Date.parse(c[0].t);
      const lastTs = Date.parse(c[c.length - 1].t);
      // Do not pin a real fill from outside this historical window to the
      // nearest edge candle — that would manufacture a misleading marker.
      if (!Number.isFinite(first) || !Number.isFinite(lastTs) || target < first || target > lastTs) return null;
      let best = -1;
      let distance = Number.POSITIVE_INFINITY;
      for (let i = 0; i < c.length; i += 1) {
        const candleTs = Date.parse(c[i].t);
        if (!Number.isFinite(candleTs)) continue;
        const next = Math.abs(candleTs - target);
        if (next < distance) { best = i; distance = next; }
      }
      return best >= 0 ? best : null;
    };
    if (settings.paperFills) {
      for (const trade of paperFills ?? []) {
        const entryIdx = nearestCandle(trade.openedAt);
        if (entryIdx != null) {
          const isLong = trade.side === "long";
          markPts.push({
            name: isLong ? "Paper BUY" : "Paper SELL", coord: [entryIdx, trade.entry],
            symbol: "triangle", symbolSize: 13, symbolRotate: isLong ? 0 : 180,
            paperTradeId: trade.id,
            itemStyle: { color: isLong ? settings.upColor : settings.downColor },
            label: { show: true, formatter: isLong ? "BUY" : "SELL", position: isLong ? "bottom" : "top",
              color: "#fff", fontSize: 8, fontWeight: 700 },
          } as any);
        }
        const exitIdx = nearestCandle(trade.closedAt);
        if (exitIdx != null && trade.exit != null) {
          const profitable = (trade.pnl ?? 0) >= 0;
          markPts.push({
            name: "Paper Exit", coord: [exitIdx, trade.exit], symbol: "diamond", symbolSize: 11,
            paperTradeId: trade.id,
            itemStyle: { color: profitable ? settings.upColor : settings.downColor },
            label: { show: true, formatter: profitable ? "EXIT +" : "EXIT −", position: "top",
              color: "#fff", fontSize: 8, fontWeight: 700 },
          } as any);
        }
      }
    }

    const active = [...data.trades].reverse().find((t) => t.entry_idx <= index && (t.exit_idx === null || t.exit_idx > index));
    const markLines: any[] = settings.priceLine ? [
      { yAxis: last.c, symbol: "none", lineStyle: { color: "#5b6478", type: "dashed", opacity: 0.6 },
        label: { formatter: fmt(last.c), position: "end", color: "#fff", backgroundColor: upCol, padding: [2, 4], fontSize: 10, borderRadius: 3 } },
    ] : [];
    const tradeAreas: any[] = [];
    // Backtest overlay's active trade — shown ONLY when there is no LIVE position
    // to draw (the live SL/TP below take priority so the two never overlap).
    if (active && !liveLevels) {
      const beMoved = active.tp1_idx !== null && active.tp1_idx <= index;
      const slLevel = beMoved ? active.entry : active.sl;
      markLines.push(
        { yAxis: active.entry, symbol: "none", lineStyle: { color: "#8a93a6" }, label: { formatter: "Entry " + fmt(active.entry), color: "#8a93a6", fontSize: 9 } },
        { yAxis: slLevel, symbol: "none", lineStyle: { color: "#f23645", type: "dashed" }, label: { formatter: (beMoved ? "BE " : "SL ") + fmt(slLevel), color: "#f23645", fontSize: 9 } },
        { yAxis: active.tp, symbol: "none", lineStyle: { color: "#089981", type: "dashed" }, label: { formatter: "TP " + fmt(active.tp), color: "#089981", fontSize: 9 } },
      );
      if (active.tp1 !== null)
        markLines.push({ yAxis: active.tp1, symbol: "none", lineStyle: { color: "#089981", type: "dotted", opacity: 0.7 }, label: { formatter: "TP1", color: "#089981", fontSize: 9 } });
      tradeAreas.push([{ xAxis: active.entry_idx, yAxis: active.entry, itemStyle: { color: "rgba(242,54,69,0.10)" } }, { xAxis: end - 1, yAxis: slLevel }]);
      tradeAreas.push([{ xAxis: active.entry_idx, yAxis: active.entry, itemStyle: { color: "rgba(8,153,129,0.10)" } }, { xAxis: end - 1, yAxis: active.tp }]);
    }
    // LIVE position — the real, engine-enforced entry / SL / TP. Solid lines
    // (drag handles are placed after layout). Every value is backend-sourced.
    if (liveLevels) {
      const ll = liveLevels;
      markLines.push({ yAxis: ll.entry, symbol: "none", lineStyle: { color: "#8a93a6", width: 1 },
        label: { formatter: "Entry " + fmt(ll.entry), position: "insideStartTop", color: "#8a93a6", fontSize: 9 } });
      if (ll.stop != null) {
        markLines.push({ yAxis: ll.stop, symbol: "none", lineStyle: { color: "#f23645", width: 1.4 },
          label: { formatter: "SL " + fmt(ll.stop), position: "insideStartTop", color: "#f23645", fontSize: 9 } });
        tradeAreas.push([{ yAxis: ll.entry, itemStyle: { color: "rgba(242,54,69,0.08)" } }, { yAxis: ll.stop }]);
      }
      if (ll.target != null) {
        markLines.push({ yAxis: ll.target, symbol: "none", lineStyle: { color: "#089981", width: 1.4 },
          label: { formatter: "TP " + fmt(ll.target), position: "insideStartTop", color: "#089981", fontSize: 9 } });
        tradeAreas.push([{ yAxis: ll.entry, itemStyle: { color: "rgba(8,153,129,0.08)" } }, { yAxis: ll.target }]);
      }
    }
    if (toggles.zones) for (const z of data.zones) {
      if (z.price !== undefined)
        markLines.push({ yAxis: z.price, symbol: "none", lineStyle: { color: z.type === "support" ? "#089981" : "#f23645", type: "dotted", opacity: 0.5 }, label: { formatter: z.type === "support" ? "S" : "R", color: "#8a93a6", fontSize: 9 } });
    }
    // Grid strategy overlay — horizontal buy/sell levels across the price pane.
    for (const g of gridLines ?? []) {
      const col = g.side === "buy" ? "#089981" : "#f23645";
      markLines.push({ yAxis: g.price, symbol: "none",
        lineStyle: { color: col, type: g.edge ? "solid" : "dashed", width: g.edge ? 1.4 : 1, opacity: g.edge ? 0.85 : 0.45 },
        label: g.edge ? { formatter: fmt(g.price), position: "start", color: col, fontSize: 9 } : { show: false } });
    }
    // User-drawn horizontal price levels (the trader's own annotations, persisted).
    for (const d of drawings ?? []) {
      markLines.push({ yAxis: d.price, symbol: "none",
        lineStyle: { color: d.color, type: "solid", width: 1.2, opacity: 0.9 },
        label: { formatter: (d.alert ? "🔔 " : "") + (d.label ? d.label + "  " : "") + fmt(d.price), position: "start",
          color: "#fff", backgroundColor: d.color, padding: [1, 4], borderRadius: 3, fontSize: 9 } });
    }
    // User-drawn shapes (trend / rectangle / fibonacci) — data-coord based, so
    // ECharts keeps them anchored through zoom/pan. The trader's own drawings.
    const shapeAreas: any[] = [];
    for (const s of shapes ?? []) {
      const [x1, y1] = s.p1, [x2, y2] = s.p2;
      if (s.kind === "trend") {
        markLines.push([
          { coord: [x1, y1], symbol: "none", lineStyle: { color: s.color, width: 1.6 } },
          { coord: [x2, y2], symbol: "none" },
        ] as any);
      } else if (s.kind === "rect") {
        shapeAreas.push([
          { coord: [x1, y1], itemStyle: { color: s.color + "18", borderColor: s.color, borderWidth: 1 } },
          { coord: [x2, y2] },
        ]);
      } else if (s.kind === "fib") {
        for (const r of FIB) {
          const lvl = y1 + (y2 - y1) * r;
          markLines.push([
            { coord: [x1, lvl], symbol: "none",
              lineStyle: { color: s.color, width: r === 0 || r === 1 ? 1.3 : 0.9, type: r === 0 || r === 1 ? "solid" : "dashed", opacity: 0.8 },
              label: { formatter: `${r.toFixed(3)}  ${fmt(lvl)}`, position: "start", color: s.color, fontSize: 8.5 } },
            { coord: [x2, lvl], symbol: "none" },
          ] as any);
        }
      }
    }
    const zoneAreas = (toggles.zones ? data.zones : [])
      .filter((z) => z.left_idx !== undefined && z.left_idx <= index).slice(-8)
      .map((z) => ([
        { xAxis: z.left_idx, yAxis: z.top, itemStyle: { color: z.type === "demand" ? "rgba(8,153,129,0.12)" : "rgba(242,54,69,0.12)" } },
        { xAxis: end - 1, yAxis: z.bottom },
      ]));

    // ---- primary price series (candles | line | area) — same real OHLC data ----
    const primaryMarks = {
      markPoint: { data: markPts as any, silent: true },
      markLine: { symbol: "none", data: markLines as any, silent: true },
      markArea: { silent: true, data: [...zoneAreas, ...tradeAreas, ...shapeAreas] as any },
    };
    const series: any[] = [];
    if (chartType === "line" || chartType === "area") {
      series.push({
        type: "line", data: c.map((b) => b.c), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, smooth: false,
        lineStyle: { color: "#7cb9e8", width: 1.5 },
        ...(chartType === "area" ? {
          areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(124,185,232,0.28)" }, { offset: 1, color: "rgba(124,185,232,0.02)" }]) },
        } : {}),
        ...primaryMarks,
      });
    } else {
      series.push({
        type: "candlestick", data: ohlc, xAxisIndex: 0, yAxisIndex: 0, barMaxWidth: 14,
        itemStyle: { color: settings.upColor, color0: settings.downColor, borderColor: settings.upColor, borderColor0: settings.downColor },
        ...primaryMarks,
      });
    }

    // ---- Volume Profile: real volume-by-price from the visible candles ----
    if (settings.volProfile && c.length > 2) {
      const lo = Math.min(...c.map((b) => b.l));
      const hi = Math.max(...c.map((b) => b.h));
      const N = 24;
      const step = (hi - lo) / N || 1;
      const buckets = new Array(N).fill(0);
      for (const b of c) {
        const b0 = Math.max(0, Math.min(N - 1, Math.floor((b.l - lo) / step)));
        const b1 = Math.max(0, Math.min(N - 1, Math.floor((b.h - lo) / step)));
        const per = b.v / (b1 - b0 + 1);
        for (let k = b0; k <= b1; k++) buckets[k] += per;
      }
      const maxV = Math.max(...buckets, 1);
      const vpData = buckets.map((v, k) => [lo + (k + 0.5) * step, v, step]);
      series.push({
        type: "custom", xAxisIndex: 0, yAxisIndex: 0, silent: true, z: 1, data: vpData as any,
        renderItem: (_params: any, api: any) => {
          const price = api.value(0), vol = api.value(1), bsize = api.value(2);
          const cs = _params.coordSys as { x: number; y: number; width: number; height: number };
          const yA = api.coord([0, price + bsize / 2])[1];
          const yB = api.coord([0, price - bsize / 2])[1];
          const h = Math.max(1, Math.abs(yB - yA) - 1);
          const w = (vol / maxV) * (cs.width * 0.16);
          const isPoc = vol >= maxV * 0.999;
          return {
            type: "rect",
            shape: { x: cs.x + cs.width - w, y: Math.min(yA, yB), width: w, height: h },
            style: { fill: isPoc ? "rgba(234,181,79,0.38)" : "rgba(124,185,232,0.16)" },
          };
        },
      });
    }
    const legend: string[] = [];
    const line = (key: string, name: string, color: string, opt: any = {}) => {
      series.push({ type: "line", data: num(ov[key], end), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false,
        smooth: true, lineStyle: { color, width: 1.3, ...(opt.lineStyle || {}) }, name, ...opt });
      legend.push(name);
    };
    if (toggles.supertrend && ov.supertrend) line("supertrend", "Supertrend", "#eab54f", { smooth: false, lineStyle: { width: 1.6 } });
    for (const el of extraLines ?? []) {
      if (ov[el.key]) line(el.key, el.name, el.color, el.dashed ? { smooth: false, lineStyle: { width: 1.2, type: "dashed" } } : {});
    }
    if (toggles.ema8) line("ema8", "EMA8", "#22d3ee");
    if (toggles.ema20) line("ema20", "EMA20", "#3b82f6");
    if (toggles.ema30) line("ema30", "EMA30", "#a855f7");
    if (toggles.ema50) line("ema50", "EMA50", "#f59e0b");
    if (toggles.sma20) line("sma20", "SMA20", "#60a5fa", { smooth: false, lineStyle: { width: 1.1, type: "dashed" } });
    if (toggles.sma50) line("sma50", "SMA50", "#fbbf24", { smooth: false, lineStyle: { width: 1.1, type: "dashed" } });
    if (toggles.vwap) line("vwap", "VWAP", "#eab54f", { smooth: false, lineStyle: { width: 1.1, type: "dashed" } });
    if (toggles.bb) {
      line("bb_upper", "BB", "#5b6478", { lineStyle: { width: 1, opacity: 0.8 } });
      series.push({ type: "line", data: num(ov.bb_mid, end), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, smooth: true, lineStyle: { color: "#5b6478", width: 0.8, type: "dotted", opacity: 0.7 }, name: "BBmid" });
      series.push({ type: "line", data: num(ov.bb_lower, end), xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, smooth: true, lineStyle: { color: "#5b6478", width: 1, opacity: 0.8 }, name: "BBlo",
        areaStyle: { color: "rgba(91,100,120,0.06)", origin: "start" } });
    }
    if (hasVol) series.push({ type: "bar", data: vol as any, xAxisIndex: volGrid, yAxisIndex: volGrid, barMaxWidth: 14 });

    // ---- oscillator pane ----
    if (toggles.osc === "rsi") {
      series.push({ type: "line", data: num(ov.rsi, end), xAxisIndex: oscGrid, yAxisIndex: oscGrid, showSymbol: false, smooth: true,
        lineStyle: { color: "#eab54f", width: 1.2 }, name: "RSI",
        markLine: { symbol: "none", silent: true, data: [
          { yAxis: 70, lineStyle: { color: "#f23645", type: "dashed", opacity: 0.5 }, label: { formatter: "70", color: "#8a93a6", fontSize: 9 } },
          { yAxis: 30, lineStyle: { color: "#089981", type: "dashed", opacity: 0.5 }, label: { formatter: "30", color: "#8a93a6", fontSize: 9 } },
          { yAxis: 50, lineStyle: { color: "#5b6478", type: "dotted", opacity: 0.4 }, label: { show: false } },
        ] } });
    } else if (toggles.osc === "macd") {
      const hist = num(ov.macd_hist, end).map((v) => v === null ? null : ({ value: v, itemStyle: { color: v >= 0 ? "#089981" : "#f23645" } }));
      series.push({ type: "bar", data: hist as any, xAxisIndex: oscGrid, yAxisIndex: oscGrid, barMaxWidth: 6, name: "Hist" });
      series.push({ type: "line", data: num(ov.macd, end), xAxisIndex: oscGrid, yAxisIndex: oscGrid, showSymbol: false, smooth: true, lineStyle: { color: "#3b82f6", width: 1.2 }, name: "MACD" });
      series.push({ type: "line", data: num(ov.macd_signal, end), xAxisIndex: oscGrid, yAxisIndex: oscGrid, showSymbol: false, smooth: true, lineStyle: { color: "#f59e0b", width: 1.1 }, name: "Signal" });
    } else if (toggles.osc === "atr") {
      series.push({ type: "line", data: num(ov.atr, end), xAxisIndex: oscGrid, yAxisIndex: oscGrid, showSymbol: false, smooth: true, lineStyle: { color: "#22d3ee", width: 1.2 }, name: "ATR", areaStyle: { color: "rgba(34,211,238,0.08)" } });
    }

    const startV = Math.max(0, end - WINDOW);
    const allX = grids.map((_, gi) => gi);
    const option: EChartsOption = {
      backgroundColor: "transparent",
      animation: false,
      title: {
        left: 58, top: 4, textStyle: { color: upCol, fontSize: 11, fontWeight: 500 },
        text: `${data.meta.symbol} ${data.meta.timeframe}   O ${fmt(last.o)}  H ${fmt(last.h)}  L ${fmt(last.l)}  C ${fmt(last.c)}  Vol ${volFmt(last.v)}   ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`,
      },
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#1e1e21" } },
      legend: legend.length ? { data: legend, top: 4, right: 64, itemWidth: 14, itemHeight: 8,
        textStyle: { color: "#8a93a6", fontSize: 10 }, icon: "roundRect" } : undefined,
      grid: grids,
      xAxis, yAxis,
      dataZoom: [
        { type: "inside", xAxisIndex: allX, startValue: startV, endValue: end - 1, zoomOnMouseWheel: true, moveOnMouseMove: true, minValueSpan: 20 },
        { type: "slider", xAxisIndex: allX, startValue: startV, endValue: end - 1, height: 18, bottom: 6,
          backgroundColor: "rgba(30,36,56,0.4)", fillerColor: "rgba(139,92,246,0.15)", borderColor: "#2a2a2f",
          handleStyle: { color: "#eab54f" }, textStyle: { color: "#8a93a6", fontSize: 9 }, dataBackground: { lineStyle: { color: "#2a2a2f" }, areaStyle: { color: "#161618" } } },
      ],
      tooltip: {
        trigger: "axis", axisPointer: { type: settings.crosshair ? "cross" : "line", crossStyle: { color: "#5b6478" } },
        backgroundColor: "rgba(13,18,32,0.96)", borderColor: "#2a2a2f", textStyle: { color: "#e6eaf2", fontSize: 11 },
        formatter: (ps: any) => {
          const i = ps[0].dataIndex; const b = c[i]; if (!b) return "";
          const up = b.c >= b.o;
          let s = `<b>${cats[i]}</b><br/>O ${fmt(b.o)}  H ${fmt(b.h)}<br/>L ${fmt(b.l)}  <span style="color:${up ? "#089981" : "#f23645"}">C ${fmt(b.c)}</span><br/>Vol ${volFmt(b.v)}`;
          const rsi = ov.rsi?.[i]; const atr = ov.atr?.[i]; const macd = ov.macd?.[i];
          if (toggles.osc === "rsi" && rsi != null) s += `<br/>RSI ${rsi.toFixed(1)}`;
          if (toggles.osc === "atr" && atr != null) s += `<br/>ATR ${fmt(atr)}`;
          if (toggles.osc === "macd" && macd != null) s += `<br/>MACD ${macd.toFixed(2)}`;
          return s;
        },
      },
      series,
    };
    chart.setOption(option, true);

    // ---- draggable SL/TP handles for the LIVE position ----
    // Placed AFTER layout (pixel coords need the axes resolved). Each handle is
    // a small right-rail chip pinned to its price line; dragging vertically and
    // releasing converts the drop pixel back to a price and commits via the API.
    if (liveLevels && commitRef.current) {
      const ll = liveLevels;
      const railX = Math.max(60, chart.getWidth() - 62);
      const mkHandle = (kind: "stop" | "target", price: number | null, color: string, text: string) => {
        if (price == null || !isFinite(price)) return null;
        const px = chart.convertToPixel({ gridIndex: 0 }, [end - 1, price]) as number[] | null;
        if (!px || !isFinite(px[1])) return null;
        return {
          type: "group", id: `lvl-${kind}`, draggable: true, z: 120,
          x: railX, y: px[1], cursor: "ns-resize",
          children: [
            { type: "rect", shape: { x: 0, y: -9, width: 56, height: 18, r: 3 }, style: { fill: color, opacity: 0.95 } },
            { type: "text", style: { text, x: 5, y: 0, fill: "#fff", fontSize: 10, fontWeight: 600, textVerticalAlign: "middle" as const } },
          ],
          ondrag() { (this as any).x = railX; },  // lock to the right rail
          ondragend() {
            const y = (this as any).y as number;
            const back = chart.convertFromPixel({ gridIndex: 0 }, [railX, y]) as number[] | null;
            const np = back && isFinite(back[1]) ? back[1] : null;
            if (np != null && np > 0) commitRef.current?.(kind, np);
          },
        };
      };
      const handles = [
        mkHandle("stop", ll.stop, "#f23645", "SL ⇕"),
        mkHandle("target", ll.target, "#089981", "TP ⇕"),
      ].filter(Boolean);
      chart.setOption({ graphic: handles }, { replaceMerge: ["graphic"] } as any);
    } else {
      chart.setOption({ graphic: [] }, { replaceMerge: ["graphic"] } as any);
    }
  }, [data, index, toggles, height, chartType, drawings, shapes, settings, liveLevels, paperFills]);

  return <div ref={elRef} style={{ width: "100%", height, cursor: drawTool !== "none" ? "crosshair" : undefined }} />;
}
