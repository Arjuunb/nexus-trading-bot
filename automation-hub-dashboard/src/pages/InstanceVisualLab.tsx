import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGet } from "../lib/api";

/**
 * One page for every Trading Instance strategy, with the strategy's own
 * analysis drawn on the chart.
 *
 * The rule this file exists to keep: nothing here computes a trading feature.
 * Zones, swings, structure breaks, fair value gaps and moving averages all
 * arrive from /research/instance-visual/features, which reads them out of the
 * strategy object the instance run loop is driving. The forming candle is the
 * one exception and it is deliberately inert: it comes straight from Binance
 * for display, is labelled FORMING, and can never produce a marker, because
 * the strategies decide on closed candles only.
 */

type GateState = "PASS" | "FAIL" | "WAITING" | "NOT_APPLICABLE";

export interface Gate { id: string; stage: string; label: string; detail: string;
  state: GateState; blocker: string; explanation: string }
interface InstanceRow {
  instance_id: string; name: string; strategy_id: string; strategy_label?: string;
  strategy_version?: string; symbol: string; timeframe: string; htf?: string | null;
  venue: string; operating_mode?: string; runtime_state?: string;
  market_data_state?: string; market_data_mode?: string;
  last_closed_candle?: string | null; position_open: boolean;
  has_visual_adapter: boolean;
}
interface StrategyVisual {
  strategy_id: string; module: string; features: string[]; overlays: string[];
  entry_trigger: string; invalidation: string; stop_model: string;
  target_model: string; risk_requirements: string; notes: string;
}
interface Feed {
  exchange?: string; symbol?: string; timeframe?: string; htf_primary?: string | null;
  last_price?: number | null; bid?: number | null; ask?: number | null;
  mark_price?: number | null; spread?: number | null;
  last_closed_candle?: string | null; last_quote_timestamp?: string | null;
  data_age_seconds?: number | null; quote_age_seconds?: number | null;
  seconds_to_candle_close?: number | null; candle_period_seconds?: number | null;
  data_source?: string | null; transport_state?: string | null;
  subscription_state?: string | null; reliable?: boolean | null;
  health_reason?: string | null; failing_dependency?: string | null;
  market_status?: string | null; current_blocker?: string | null;
}
export interface Position {
  symbol?: string; side?: string; size?: number; entry?: number;
  stop?: number | null; target?: number | null; mark?: number | null;
  unrealized_pnl?: number | null; current_r?: number | null;
  risk_amount?: number | null; opened_at?: string | null;
}
interface LabState {
  instance: InstanceRow; strategy: StrategyVisual; decision_state: string;
  blocker: string | null; blocker_explanation: string; blocker_at?: string | null;
  required_next: string | null; gates: Gate[]; pipeline: string[];
  current_stage: string; position: Position | null; feed: Feed;
  last_closed_candle?: string | null; data_source?: string | null;
}
export interface Overlay {
  kind: string; feature: string; id: string; provenance: Record<string, any>;
  lower?: number; upper?: number; price?: number; label?: string;
  points?: { t: string; v: number }[]; status?: string; direction?: string;
  created_at?: string; occurred_at?: string; confirmed_at?: string;
  [key: string]: any;
}
interface Features {
  overlays: Overlay[]; declared_features: string[]; withheld_features: string[];
  strategy_id: string;
}
export interface TimelineEvent {
  id: number; timestamp: string; candle_identity: string; symbol: string;
  timeframe: string; strategy: string; side: string; regime: string;
  htf_bias: string; decision: string; final_state: string; gate_stage: string;
  blocker: string | null; blocker_explanation: string; reason: string;
  passed_rules: string[]; failed_rules: string[];
  components: Record<string, any>; executed: boolean;
}
interface Timeline {
  events: TimelineEvent[]; coverage: string;
  focus: { last_accepted: TimelineEvent | null; last_rejected: TimelineEvent | null;
           last_trade: TimelineEvent | null };
}
export interface Candle { t: string; o: number; h: number; l: number; c: number; v?: number }
/** The platform's one freshness verdict, as services/market_data_freshness.py
 *  returns it. Never recomputed here -- a second opinion about the same candle
 *  is how a page ends up disagreeing with the bot it is watching. */
interface Freshness {
  status: string; blocker: string; age_seconds: number | null;
  allowed_age_seconds: number; interval_seconds: number; last_close: string | null;
}
interface Attempt { source: string; error?: string; freshness?: Freshness }
interface Candles {
  candles: Candle[]; source: string; symbol: string; timeframe: string;
  instance_timeframe?: string; strategy_timeframes?: string[];
  aligned_with_overlays?: boolean; market_data_state?: string | null;
  freshness?: Freshness; attempts?: Attempt[]; venue?: string;
  strategy_series_behind?: boolean;
}
/** Offered in the chart header. A frame the strategy holds is marked as such. */
const TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"];
const VIEWS = [60, 120, 240, 480];
const FRAME_SECONDS: Record<string, number> = {
  "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
  "1h": 3600, "4h": 14400, "1d": 86400,
};
/** Refetch often enough that a closed candle shows up while it is still news,
 *  and never faster than 10s. The backend reuses its venue read while the
 *  series is fresh, so a poll between closes costs nothing at the exchange. */
const cadence = (frame: string) =>
  Math.max(10000, Math.min((FRAME_SECONDS[frame] ?? 300) * 100, 60000));
const age = (seconds?: number | null) => {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
};

const STAGE_LABEL: Record<string, string> = {
  MARKET_DATA: "Market data", FEATURES: "Features", CONTEXT: "Context",
  SETUP: "Setup", CONFIRMATION: "Confirmation", STRATEGY_ACCEPT: "Strategy accept",
  RISK_CHECK: "Risk check", ORDER_INTENT: "Order intent", PAPER_BROKER: "Paper broker",
  FILL: "Fill", POSITION: "Position", EXIT: "Exit",
};
const BLOCKED_STATES = new Set([
  "DATA_BLOCKED", "WAITING_FOR_DATA", "WAITING_FOR_HTF", "RISK_BLOCKED",
  "SIGNAL_REJECTED", "PAUSED", "STOPPED", "SIGNALS_ONLY",
]);
const MARK: Record<GateState, string> = {
  PASS: "✓", FAIL: "✗", WAITING: "…", NOT_APPLICABLE: "–" };

/** Feature -> the toggle group it belongs to. Only declared ones are offered. */
export const TOGGLE_GROUP: Record<string, string> = {
  support: "S/R", resistance: "S/R", supply: "Supply/Demand", demand: "Supply/Demand",
  fvg: "FVG", liquidity: "Liquidity", liquidity_sweep: "Liquidity",
  bos: "Structure", choch: "Structure", swing_high_low: "Structure",
  ema: "EMA", donchian_channel: "EMA", supertrend: "EMA", atr_band: "EMA",
  rejection_candle: "Candles", dominant_candle: "Candles",
  poi: "S/R", zone_flip: "S/R", opposing_zone_target: "S/R",
  regime: "HTF", htf_bias: "HTF", trend_structure: "HTF",
};
const ZONE_FEATURES = new Set(["support", "resistance", "supply", "demand", "fvg", "poi"]);

const stamp = (value?: string | null) =>
  value ? value.replace("T", " ").replace("+00:00", " UTC").slice(0, 22) : "—";
const num = (value?: number | null, digits = 2) =>
  value === null || value === undefined ? "—"
    : Number(value).toLocaleString(undefined, { minimumFractionDigits: digits,
                                                maximumFractionDigits: digits });
const clock = (seconds?: number | null) => {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
};

function FeedPanel({ feed, forming }: { feed: Feed; forming: Candle | null }) {
  const [left, setLeft] = useState(feed.seconds_to_candle_close ?? null);
  useEffect(() => { setLeft(feed.seconds_to_candle_close ?? null); },
            [feed.seconds_to_candle_close]);
  useEffect(() => {
    if (left === null) return;
    const id = setInterval(() => setLeft((v) => (v === null ? null : Math.max(0, v - 1))), 1000);
    return () => clearInterval(id);
  }, [left === null]);

  const stale = feed.market_status && feed.market_status !== "LIVE";
  return <div className={`ivl-feed ${stale ? "is-stale" : ""}`}>
    <span>Last<b>{num(forming?.c ?? feed.last_price)}</b></span>
    <span>Bid<b>{num(feed.bid)}</b></span>
    <span>Ask<b>{num(feed.ask)}</b></span>
    <span>Spread<b>{num(feed.spread)}</b></span>
    <span>Mark<b>{num(feed.mark_price)}</b></span>
    <span>Candle closes in<b>{clock(left)}</b></span>
    <span>Feed<b>{feed.subscription_state || feed.market_status || "—"}</b>
      <small>{feed.transport_state || ""}</small></span>
    <span>Candle age<b>{feed.data_age_seconds ?? "—"}s</b>
      <small>quote {feed.quote_age_seconds ?? "—"}s</small></span>
    <span>Last closed<b>{stamp(feed.last_closed_candle)}</b></span>
    <span>Source<b>{feed.data_source || "—"}</b></span>
  </div>;
}
interface ChartProps {
  candles: Candle[]; forming: Candle | null; overlays: Overlay[];
  events: TimelineEvent[]; position: Position | null; enabled: Set<string>;
  showDecisionMarkers: boolean; focus: string | null; view: number; fit: boolean;
  unavailable: string | null;
  onPick: (event: TimelineEvent) => void; onPickOverlay: (overlay: Overlay) => void;
}

const W = 1400, H = 460, PAD = 10, RIGHT = 74, BOTTOM = 18;

/** The grid and axes, drawn whether or not there is anything to plot.
 *
 * A chart that disappears when its feed drops tells an operator nothing about
 * why. The frame stays, the scale goes blank, and the reason is written across
 * it -- the same way the instance itself fails closed and says so. */
function Frame({ ticks }: { ticks: { y: number; label: string }[] }) {
  return <g className="ivl-grid">
    <rect x={PAD} y={PAD} width={W - PAD - RIGHT} height={H - PAD * 2 - BOTTOM} />
    {(ticks.length ? ticks : [0.2, 0.4, 0.6, 0.8].map((f) => ({
      y: PAD + f * (H - PAD * 2 - BOTTOM), label: "" }))).map((tick, i) => (
      <g key={`${tick.label}-${i}`}>
        <line x1={PAD} x2={W - RIGHT} y1={tick.y} y2={tick.y} />
        {tick.label ? <text x={W - RIGHT + 4} y={tick.y + 3}>{tick.label}</text> : null}
      </g>
    ))}
  </g>;
}

/** Shared by every lab that draws an instance-path strategy: one chart, one
 * implementation (see the Adaptive MTF lab). */
export function Chart({ candles, forming, overlays, events, position, enabled,
                 showDecisionMarkers, focus, view, fit, unavailable,
                 onPick, onPickOverlay }: ChartProps) {
  const all = forming && candles[candles.length - 1]?.t !== forming.t
    ? [...candles, forming] : candles;
  const series = fit ? all : all.slice(-view);

  if (!series.length) {
    return <svg className="ivl-chart is-empty" viewBox={`0 0 ${W} ${H}`} role="img"
                aria-label="Instance chart — no candles available">
      <Frame ticks={[]} />
      <text className="ivl-chart-void" x={(W - RIGHT) / 2} y={H / 2 - 4}
            textAnchor="middle">
        {unavailable ? "NO REAL CANDLES — FAILING CLOSED" : "WAITING FOR CLOSED CANDLES"}
      </text>
      <text className="ivl-chart-void-sub" x={(W - RIGHT) / 2} y={H / 2 + 18}
            textAnchor="middle">
        {unavailable
          ? "The Visual Lab will not substitute sample or synthetic candles."
          : "The chart draws only candles the instance has actually closed."}
      </text>
    </svg>;
  }

  // Shape first, feature second, and bounds required. An overlay whose feature
  // looks zone-ish but carries no bounds would render as NaN geometry and take
  // the whole chart with it, so it is never treated as a rectangle.
  const zoneLines = overlays.filter((o) =>
    (o.kind === "zone" || o.kind === "box")
    && ZONE_FEATURES.has(o.feature)
    && Number.isFinite(o.lower) && Number.isFinite(o.upper)
    && enabled.has(TOGGLE_GROUP[o.feature]));
  const lines = overlays.filter((o) => o.kind === "line" && enabled.has(TOGGLE_GROUP[o.feature]));
  const markers = overlays.filter((o) => o.kind === "marker"
    && Number.isFinite(o.price)
    && enabled.has(TOGGLE_GROUP[o.feature]));

  const prices: number[] = [];
  series.forEach((c) => { prices.push(c.h, c.l); });
  zoneLines.forEach((z) => { prices.push(z.lower as number, z.upper as number); });
  markers.forEach((m) => prices.push(m.price as number));
  lines.forEach((l) => (l.points ?? []).forEach((pt) => {
    if (Number.isFinite(pt.v)) prices.push(pt.v);
  }));
  if (position?.entry) prices.push(position.entry);
  if (position?.stop) prices.push(position.stop);
  if (position?.target) prices.push(position.target);
  const top = Math.max(...prices), bottom = Math.min(...prices);
  const span = (top - bottom) || 1;
  const PLOT = H - PAD * 2 - BOTTOM;
  const step = (W - PAD - RIGHT) / series.length;
  const y = (p: number) => PAD + ((top - p) / span) * PLOT;
  const x = (i: number) => PAD + i * step + step / 2;
  const key = (t: string) => t.slice(0, 16);
  const at = new Map(series.map((c, i) => [key(c.t), i]));
  const nearest = (t?: string) => (t ? at.get(key(t)) : undefined);

  // A price scale on the right and a time scale underneath: without them a
  // candle chart is a picture, not a reading.
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const price = top - f * span;
    return { y: y(price), label: num(price, span > 100 ? 0 : 2) };
  });
  const timeTicks = series.length < 2 ? [] :
    [0, 0.25, 0.5, 0.75, 1]
      .map((f) => Math.min(series.length - 1, Math.round(f * (series.length - 1))))
      .filter((index, i, arr) => arr.indexOf(index) === i)
      .map((index) => ({ x: x(index), label: series[index].t.slice(5, 16).replace("T", " ") }));

  const level = (price: number, cls: string, label: string, from?: number) =>
    <g className={`ivl-level ${cls}`} key={`${cls}-${label}-${price}`}>
      <line x1={from !== undefined ? x(from) : PAD} x2={W - RIGHT} y1={y(price)} y2={y(price)} />
      <text x={W - RIGHT + 4} y={y(price) + 3}>{label} {num(price)}</text>
    </g>;

  return <svg className="ivl-chart" viewBox={`0 0 ${W} ${H}`} role="img"
              aria-label="Instance chart with runtime strategy overlays">
    <Frame ticks={ticks} />
    {timeTicks.map((tick) => (
      <text key={tick.label} className="ivl-time-tick" x={tick.x} y={H - 4}
            textAnchor="middle">{tick.label}</text>
    ))}

    {/* Zones first, so candles and markers sit on top of them. */}
    {zoneLines.map((zone) => {
      const start = nearest(zone.created_at) ?? 0;
      const invalid = zone.status && !["active", "unfilled", "fresh"].includes(zone.status);
      return <g key={zone.id} className={`ivl-zone is-${zone.feature} ${invalid ? "is-invalid" : ""}`}
                onClick={() => onPickOverlay(zone)}>
        <rect x={x(start) - step / 2} width={Math.max(W - RIGHT - x(start) + step / 2, 2)}
              y={y(zone.upper!)} height={Math.max(y(zone.lower!) - y(zone.upper!), 1)} />
        <text x={x(start) + 4} y={y(zone.upper!) - 3}>
          {zone.label || zone.feature}{invalid ? ` · ${zone.status}` : ""}
        </text>
      </g>;
    })}

    {lines.map((line) => {
      const path = (line.points ?? []).map((point) => {
        const index = nearest(point.t);
        return index === undefined ? null : `${x(index)},${y(point.v)}`;
      }).filter(Boolean).join(" ");
      if (!path) return null;
      return <g key={line.id} className={`ivl-line is-${line.feature}`}
                onClick={() => onPickOverlay(line)}>
        <polyline points={path} />
      </g>;
    })}

    {series.map((candle, index) => {
      const isForming = forming && candle.t === forming.t;
      const up = candle.c >= candle.o;
      const cx = x(index);
      return <g key={candle.t} className={`${up ? "up" : "down"}${isForming ? " is-forming" : ""}`}>
        <line x1={cx} x2={cx} y1={y(candle.h)} y2={y(candle.l)} />
        <rect x={cx - Math.max(step * 0.32, 0.7)} width={Math.max(step * 0.64, 1.4)}
              y={y(Math.max(candle.o, candle.c))}
              height={Math.max(Math.abs(y(candle.o) - y(candle.c)), 0.8)} />
      </g>;
    })}

    {markers.map((marker) => {
      const index = nearest(marker.confirmed_at || marker.occurred_at);
      if (index === undefined) return null;
      const price = marker.price ?? 0;
      return <g key={marker.id} className={`ivl-fmarker is-${marker.feature}`}
                onClick={() => onPickOverlay(marker)}>
        <circle cx={x(index)} cy={y(price)} r={3} />
        <text x={x(index) + 5} y={y(price) - 4}>
          {marker.label || marker.kind_label || marker.event_type || marker.feature}
        </text>
      </g>;
    })}

    {position?.entry ? level(position.entry, "entry", position.side === "long" ? "LONG" : "SHORT") : null}
    {position?.stop ? level(position.stop, "stop", "SL") : null}
    {position?.target ? level(position.target, "target", "TP") : null}

    {showDecisionMarkers ? events.map((event) => {
      const index = nearest(event.timestamp);
      if (index === undefined) return null;
      const accepted = event.decision === "accepted";
      const isFocus = focus === event.timestamp;
      return <g key={event.id}
                className={`ivl-decision ${accepted ? "accepted" : "rejected"}${isFocus ? " is-focus" : ""}`}
                onClick={() => onPick(event)}>
        <line x1={x(index)} x2={x(index)} y1={PAD} y2={H - PAD - BOTTOM} />
        <text x={x(index) + 3} y={PAD + 10}>{accepted ? "ACCEPTED" : `✗ ${event.blocker ?? "REJECTED"}`}</text>
      </g>;
    }) : null}
  </svg>;
}

export default function InstanceVisualLab() {
  const [instances, setInstances] = useState<InstanceRow[]>([]);
  const [selected, setSelected] = useState("");
  const [state, setState] = useState<LabState | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [candles, setCandles] = useState<Candles | null>(null);
  const [features, setFeatures] = useState<Features | null>(null);
  const [featureError, setFeatureError] = useState<string | null>(null);
  const [dataError, setDataError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forming, setForming] = useState<Candle | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [picked, setPicked] = useState<TimelineEvent | Overlay | null>(null);
  const [disabled, setDisabled] = useState<Set<string>>(new Set());
  const [showDecisionMarkers, setShowDecisionMarkers] = useState(true);
  // "" means the instance's own decision timeframe, whatever that is.
  const [frame, setFrame] = useState("");
  const [view, setView] = useState(120);
  const [fit, setFit] = useState(false);
  const formingRef = useRef<Candle | null>(null);

  useEffect(() => {
    void apiGet<{ instances: InstanceRow[] }>("/research/instance-visual/instances")
      .then((row) => {
        setInstances(row.instances);
        setSelected((current) => current || row.instances[0]?.instance_id || "");
      })
      .catch((exc) => setError(exc instanceof Error ? exc.message : String(exc)));
  }, []);

  const current = instances.find((row) => row.instance_id === selected);
  const instanceFrame = current?.timeframe || "";
  // "" means "whatever the instance decides on", resolved here once so the
  // chart, the footer and the websocket subscription cannot disagree.
  const shownFrame = frame || instanceFrame;

  /** State + timeline: the decision evidence. Polled often, small payloads. */
  const loadState = useCallback(async (instanceId: string) => {
    if (!instanceId) return;
    const query = `instance_id=${encodeURIComponent(instanceId)}`;
    try {
      const [next, events] = await Promise.all([
        apiGet<LabState>(`/research/instance-visual/state?${query}`),
        apiGet<Timeline>(`/research/instance-visual/timeline?${query}`),
      ]);
      setState(next); setTimeline(events); setError(null);
    } catch (exc) {
      setState(null); setTimeline(null);
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  /** Overlays: the strategy's own feature state. Polled less often. */
  const loadFeatures = useCallback(async (instanceId: string) => {
    if (!instanceId) return;
    try {
      setFeatures(await apiGet<Features>(
        `/research/instance-visual/features?instance_id=${encodeURIComponent(instanceId)}`));
      setFeatureError(null);
    } catch (exc) {
      setFeatures(null);
      setFeatureError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  /** Closed candles: the heaviest payload, so the least frequent. */
  const loadCandles = useCallback(async (instanceId: string, timeframe: string) => {
    if (!instanceId) return;
    const query = `instance_id=${encodeURIComponent(instanceId)}`
      + (timeframe ? `&timeframe=${encodeURIComponent(timeframe)}` : "");
    try {
      setCandles(await apiGet<Candles>(`/research/instance-visual/candles?${query}`));
      setDataError(null);
    } catch (exc) {
      // The chart frame stays up either way; only the series is dropped, so a
      // stale one is never left on screen pretending to be current.
      setCandles(null);
      setDataError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => {
    setFocus(null); setPicked(null); setForming(null); setFrame("");
    void loadState(selected); void loadFeatures(selected);
    // Two cadences rather than one: re-fetching the feature state every few
    // seconds to watch a blocker change would be most of the traffic for none
    // of the information.
    const s = setInterval(() => void loadState(selected), 4000);
    const f = setInterval(() => void loadFeatures(selected), 15000);
    return () => { clearInterval(s); clearInterval(f); };
  }, [selected, loadState, loadFeatures]);

  /** Candles are the heaviest payload, so the least frequent -- and they are
   *  their own effect because changing the displayed frame must refetch them
   *  without resetting the selection or restarting the other pollers. */
  useEffect(() => {
    formingRef.current = null; setForming(null);
    void loadCandles(selected, frame);
    const c = setInterval(() => void loadCandles(selected, frame),
                          cadence(frame || instanceFrame));
    return () => clearInterval(c);
  }, [selected, frame, instanceFrame, loadCandles]);

  /**
   * The forming candle, straight from the venue the instance trades.
   *
   * Display only. It is never handed to a marker, never compared against a
   * gate, and never allowed to imply a decision: the strategies evaluate
   * closed candles, so an intrabar price that looks like a breakout is not one
   * until the backend says the candle closed and the engine judged it.
   */
  useEffect(() => {
    if (!current?.symbol || !shownFrame) return;
    if (current.market_data_mode === "replay") return;
    let socket: WebSocket | null = null;
    let closedByUs = false, attempts = 0;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const flush = setInterval(() => {
      if (formingRef.current) setForming(formingRef.current);
    }, 1000);

    const connect = () => {
      try {
        socket = new WebSocket(
          `wss://fstream.binance.com/ws/${current.symbol.toLowerCase()}@kline_${shownFrame}`);
      } catch { schedule(); return; }
      socket.onopen = () => { attempts = 0; };
      socket.onclose = () => { if (!closedByUs) schedule(); };
      socket.onmessage = (frame) => {
        try {
          const k = JSON.parse(frame.data)?.k;
          if (!k || k.x) return;            // k.x true means CLOSED: the backend owns those
          formingRef.current = { t: new Date(k.t).toISOString(),
                                 o: +k.o, h: +k.h, l: +k.l, c: +k.c, v: +k.v };
        } catch { /* malformed frame */ }
      };
    };
    const schedule = () => {
      if (closedByUs || retry) return;
      const delay = Math.min(1000 * 2 ** Math.min(attempts, 5), 30000);
      attempts += 1;
      retry = setTimeout(() => { retry = null; connect(); }, delay);
    };
    connect();
    return () => {
      closedByUs = true;
      if (retry) clearTimeout(retry);
      clearInterval(flush);
      formingRef.current = null;
      try { socket?.close(); } catch { /* noop */ }
    };
  }, [current?.symbol, shownFrame, current?.market_data_mode]);

  const groups = useMemo(() => {
    const declared = features?.declared_features ?? state?.strategy.overlays ?? [];
    return Array.from(new Set(declared.map((f) => TOGGLE_GROUP[f]).filter(Boolean)));
  }, [features, state]);
  const enabled = useMemo(
    () => new Set(groups.filter((g) => !disabled.has(g))), [groups, disabled]);

  const toggle = (group: string) => setDisabled((current_) => {
    const next = new Set(current_);
    next.has(group) ? next.delete(group) : next.add(group);
    return next;
  });

  return <div className="page ivl">
    <header className="ivl-head">
      <div>
        <h1>Instance Visual Lab</h1>
        <p className="lede">
          Observability over the running instance. Every overlay, gate and blocker is read
          from the strategy runtime&rsquo;s own state — nothing here computes a trading
          feature, and nothing here can place an order.
        </p>
      </div>
      <select aria-label="Trading instance" value={selected}
              onChange={(event) => setSelected(event.target.value)}>
        {instances.map((row) => (
          <option key={row.instance_id} value={row.instance_id}>
            {row.name} · {row.symbol} · {row.timeframe}
            {row.has_visual_adapter ? "" : " (no visual adapter)"}
          </option>
        ))}
      </select>
    </header>

    {current ? <div className="ivl-identity">
      <span>Instance<b>{current.name}</b></span>
      <span>Strategy<b>{current.strategy_label || current.strategy_id}</b>
        <small>{current.strategy_id} · {current.strategy_version || "unversioned"}</small></span>
      <span>Market<b>{current.symbol} {current.timeframe}</b><small>{current.htf || "no HTF"}</small></span>
      <span>Venue<b>{current.venue}</b></span>
      <span>Mode<b>{current.operating_mode || "—"}</b></span>
      <span>Runtime<b>{(current.runtime_state || "—").toUpperCase()}</b></span>
      <span>Position<b>{current.position_open ? "OPEN" : "flat"}</b></span>
    </div> : null}

    {error ? <div className="ivl-alert"><b>This instance cannot be visualised</b><span>{error}</span></div> : null}

    {state ? <>
      <FeedPanel feed={state.feed} forming={forming} />

      <div className={`ivl-state ${BLOCKED_STATES.has(state.decision_state) ? "is-blocked" : ""}`}>
        <b>{state.decision_state.replace(/_/g, " ")}</b>
        {state.blocker ? <span className="ivl-code">{state.blocker}</span> : null}
        {state.blocker_explanation ? <span>{state.blocker_explanation}</span> : null}
        {state.required_next ? <em>Required next: {state.required_next}</em> : null}
        <small>as of {stamp(state.blocker_at)}</small>
      </div>

      <div className="ivl-pipeline">
        {state.pipeline.map((stage) => (
          <span key={stage} className={stage === state.current_stage ? "is-current" : ""}>
            {STAGE_LABEL[stage] ?? stage}
          </span>
        ))}
      </div>

      <div className="ivl-chartwrap">
        <div className="ivl-chartbar">
          <span className="ivl-chartsym">
            <b>{current?.symbol || "—"}</b>
            <em>{shownFrame || "—"}</em>
            {shownFrame && shownFrame !== instanceFrame
              ? <i className="ivl-offframe">context frame · this instance decides on {instanceFrame}</i>
              : null}
            {/* The verdict comes from the backend, which asks the one freshness
                authority. There is deliberately no branch here that can render
                FRESH without the server having said so. */}
            {candles?.freshness
              ? <i className={`ivl-fresh is-${candles.freshness.status.toLowerCase()}`}>
                  {candles.freshness.status} · {age(candles.freshness.age_seconds)}
                </i>
              : <i className="ivl-fresh is-unknown">AGE UNVERIFIED</i>}
          </span>

          <span className="ivl-frames">
            {TIMEFRAMES.map((tf) => {
              const held = candles?.strategy_timeframes?.includes(tf);
              return <button key={tf}
                             className={`${shownFrame === tf ? "active" : ""}${held ? " is-held" : ""}`}
                             title={held ? "the running strategy holds this frame"
                                         : "drawn from real provider history"}
                             onClick={() => setFrame(tf === instanceFrame ? "" : tf)}>{tf}</button>;
            })}
          </span>

          <span className="ivl-view">
            View
            <select aria-label="Bars in view" value={view}
                    onChange={(e) => { setView(Number(e.target.value)); setFit(false); }}>
              {VIEWS.map((n) => <option key={n} value={n}>{n} bars</option>)}
            </select>
            <button className={fit ? "active" : ""} onClick={() => setFit(true)}>Fit</button>
            <button className={fit ? "" : "active"} onClick={() => setFit(false)}>Latest</button>
          </span>

          <span className="ivl-layers">
            {groups.map((group) => (
              <button key={group} className={enabled.has(group) ? "active" : ""}
                      onClick={() => toggle(group)}>{group}</button>
            ))}
            <button className={showDecisionMarkers ? "active" : ""}
                    onClick={() => setShowDecisionMarkers((v) => !v)}>Decisions</button>
          </span>
        </div>

        {/* Notes sit above the chart, never instead of it. An operator whose
            feed has dropped needs to see the frame, the scale and the reason
            together -- a page that replaces the chart with an error box hides
            exactly the context that makes the error readable. */}
        {dataError ? <div className="ivl-chartnote is-bad">
          <b>DATA STALE · FAILING CLOSED</b>
          <span>{dataError}</span>
          <small>The Visual Lab will not substitute sample or synthetic candles.</small>
        </div> : null}
        {candles?.freshness && candles.freshness.status !== "FRESH"
          ? <div className="ivl-chartnote is-bad">
              <b>DATA STALE · {candles.freshness.blocker || candles.freshness.status}</b>
              <span>
                The newest closed {candles.timeframe} candle is {age(candles.freshness.age_seconds)} old;
                this timeframe allows {age(candles.freshness.allowed_age_seconds)}. Drawn from {candles.source}.
              </span>
              <small>These candles are real and are shown as they are. The age is the
                platform&rsquo;s own verdict, not this page&rsquo;s.</small>
            </div> : null}
        {candles?.strategy_series_behind
          ? <div className="ivl-chartnote is-warn">
              <b>The running strategy is behind this chart</b>
              <span>Its own series is stale, so the chart is drawn from the venue instead.
                The instance is seeing less than you are.</span>
            </div> : null}
        {featureError ? <div className="ivl-chartnote is-warn">
          <b>Strategy overlays unavailable</b><span>{featureError}</span>
          <small>Candles and decision markers are still drawn. Overlays come from
            runtime evidence only, never invented.</small>
        </div> : null}
        {candles && candles.aligned_with_overlays === false && !featureError
          ? <div className="ivl-chartnote is-warn">
              <b>Overlays may sit a candle off</b>
              <span>These candles are provider history, not the running strategy&rsquo;s own
                series, so an overlay read from the strategy can land on a neighbouring bar.</span>
            </div> : null}

        <Chart candles={candles?.candles ?? []} forming={forming}
               overlays={features?.overlays ?? []}
               events={timeline?.events ?? []} position={state.position}
               enabled={enabled} showDecisionMarkers={showDecisionMarkers}
               focus={focus} view={view} fit={fit} unavailable={dataError}
               onPick={(event) => { setFocus(event.timestamp); setPicked(event); }}
               onPickOverlay={(overlay) => setPicked(overlay)} />

        <div className="ivl-chartfoot">
          <span>Last closed candle
            <b>{stamp(candles?.candles[candles.candles.length - 1]?.t
                      ?? state.last_closed_candle)}</b>
            <small>{candles ? `${candles.candles.length} closed ${candles.timeframe} candles loaded`
                            : "0 closed candles loaded"}</small></span>
          <span>Forming candle · display only
            <b>{forming ? num(forming.c) : "Not available"}</b>
            <small>excluded from every decision</small></span>
          <span>Candle source<b>{candles?.source || "—"}</b>
            <small>{!candles ? "no series loaded"
              : candles.aligned_with_overlays
                ? "on the venue candle grid \u2014 overlays align by timestamp"
                : "cache, only as current as the last /data/sync"}</small></span>
          <span>Candle age<b>{age(candles?.freshness?.age_seconds)}</b>
            <small>{candles?.freshness
              ? `allowed ${age(candles.freshness.allowed_age_seconds)} for ${candles.timeframe}`
              : "no verdict"}</small></span>
          <span>Runtime overlays<b>{features ? features.overlays.length : "—"}</b>
            <small>{features?.withheld_features?.length
              ? `withheld (not declared): ${features.withheld_features.join(", ")}`
              : "declared features only"}</small></span>
          <span className="ivl-foot-right">PAPER · NO LIVE EXECUTION</span>
        </div>
      </div>

      {picked ? <div className="ivl-evidence">
        <button className="ivl-close" onClick={() => setPicked(null)}>close</button>
        {"decision" in picked ? <>
          <b>{String((picked as TimelineEvent).decision).toUpperCase()} · {(picked as TimelineEvent).final_state}</b>
          <span>{stamp((picked as TimelineEvent).timestamp)} · candle {(picked as TimelineEvent).candle_identity}</span>
          <span>{(picked as TimelineEvent).symbol} {(picked as TimelineEvent).timeframe} · {(picked as TimelineEvent).side || "—"} · regime {(picked as TimelineEvent).regime || "—"} · HTF {(picked as TimelineEvent).htf_bias || "—"}</span>
          {(picked as TimelineEvent).blocker
            ? <span className="ivl-code">{(picked as TimelineEvent).blocker} — {(picked as TimelineEvent).blocker_explanation}</span> : null}
          <span>{(picked as TimelineEvent).reason}</span>
          <span>passed: {(picked as TimelineEvent).passed_rules?.join(", ") || "—"}</span>
          <span>failed: {(picked as TimelineEvent).failed_rules?.join(", ") || "—"}</span>
          <span>stage {(picked as TimelineEvent).gate_stage || "—"} · decision #{(picked as TimelineEvent).id}</span>
        </> : <>
          <b>{(picked as Overlay).label || (picked as Overlay).feature}</b>
          <span>{(picked as Overlay).feature} · {(picked as Overlay).id}</span>
          {(picked as Overlay).lower !== undefined
            ? <span>{num((picked as Overlay).lower)} — {num((picked as Overlay).upper)}</span> : null}
          {(picked as Overlay).price !== undefined ? <span>price {num((picked as Overlay).price)}</span> : null}
          {(picked as Overlay).status ? <span>status {(picked as Overlay).status}</span> : null}
          {(picked as Overlay).created_at ? <span>created {stamp((picked as Overlay).created_at)}</span> : null}
          {(picked as Overlay).confirmed_at ? <span>confirmed {stamp((picked as Overlay).confirmed_at)}</span> : null}
          <span className="ivl-prov">from {(picked as Overlay).provenance?.module}
            {" · "}{(picked as Overlay).provenance?.field}</span>
          {(picked as Overlay).provenance?.note
            ? <span className="ivl-prov">{(picked as Overlay).provenance.note}</span> : null}
        </>}
      </div> : null}

      <div className="ivl-grid">
        <div className="ivl-panel">
          <h2>How this strategy can place a trade</h2>
          {(() => {
            const stages = state.gates.reduce<Record<string, Gate[]>>((acc, gate) => {
              (acc[gate.stage] ||= []).push(gate); return acc;
            }, {});
            return Object.entries(stages).map(([stage, rows]) => (
              <section key={stage}>
                <h3>{STAGE_LABEL[stage] ?? stage}</h3>
                <ul className="ivl-gates">
                  {rows.map((gate) => (
                    <li key={gate.id} className={`ivl-gate is-${gate.state.toLowerCase()}`}>
                      <span className="ivl-mark">{MARK[gate.state]}</span>
                      <span>
                        <b>{gate.label}</b>
                        {gate.detail ? <em>{gate.detail}</em> : null}
                        {gate.state === "FAIL" && gate.blocker
                          ? <strong className="ivl-blocker">{gate.blocker} — {gate.explanation}</strong> : null}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ));
          })()}
        </div>

        <div className="ivl-panel">
          <h2>What this strategy reads</h2>
          <ul className="ivl-features">
            {state.strategy.overlays.map((feature) => (
              <li key={feature}>{feature.replace(/_/g, " ")}</li>
            ))}
          </ul>
          <dl>
            <dt>Entry trigger</dt><dd>{state.strategy.entry_trigger}</dd>
            <dt>Invalidation</dt><dd>{state.strategy.invalidation}</dd>
            <dt>Stop</dt><dd>{state.strategy.stop_model}</dd>
            <dt>Target</dt><dd>{state.strategy.target_model}</dd>
            <dt>Risk</dt><dd>{state.strategy.risk_requirements}</dd>
          </dl>
          {state.position ? <>
            <h3>Open position</h3>
            <dl>
              <dt>Side / size</dt><dd>{state.position.side} {state.position.size}</dd>
              <dt>Entry (actual fill)</dt><dd>{num(state.position.entry)}</dd>
              <dt>Stop</dt><dd>{num(state.position.stop)}</dd>
              <dt>Target</dt><dd>{num(state.position.target)}</dd>
              <dt>Current R</dt><dd>{state.position.current_r ?? "—"}</dd>
              <dt>Risk amount</dt><dd>{num(state.position.risk_amount)}</dd>
              <dt>Unrealised</dt><dd>{num(state.position.unrealized_pnl)}</dd>
              <dt>Opened</dt><dd>{stamp(state.position.opened_at)}</dd>
            </dl>
          </> : null}
          {state.strategy.notes ? <p className="ivl-note">{state.strategy.notes}</p> : null}
          <small>Declared in services/strategy_visual_registry.py and checked against{" "}
            {state.strategy.module} by the test suite. Overlays come from
            services/strategy_visual_features.py, which reads the running strategy.</small>
        </div>
      </div>

      <div className="ivl-panel">
        <h2>Candle decision timeline</h2>
        <div className="ivl-focus">
          <button className={!focus ? "active" : ""}
                  onClick={() => { setFocus(null); setPicked(null); }}>Live</button>
          <button disabled={!timeline?.focus.last_rejected}
                  onClick={() => { const e = timeline?.focus.last_rejected; if (e) { setFocus(e.timestamp); setPicked(e); } }}>
            Last rejected</button>
          <button disabled={!timeline?.focus.last_accepted}
                  onClick={() => { const e = timeline?.focus.last_accepted; if (e) { setFocus(e.timestamp); setPicked(e); } }}>
            Last accepted</button>
          <button disabled={!timeline?.focus.last_trade}
                  onClick={() => { const e = timeline?.focus.last_trade; if (e) { setFocus(e.timestamp); setPicked(e); } }}>
            Last trade</button>
          <span className="ivl-note-inline">Inspection only — selecting an event never pauses the bot.</span>
        </div>
        <div className="ivl-table-wrap">
          <table className="ivl-table">
            <thead><tr>
              <th>When</th><th>Candle</th><th>Side</th><th>Decision</th>
              <th>Stage</th><th>Blocker</th><th>Final</th>
            </tr></thead>
            <tbody>
              {(timeline?.events ?? []).map((event) => (
                <tr key={event.id}
                    className={`${event.decision === "accepted" ? "is-accepted" : "is-rejected"}`
                               + (focus === event.timestamp ? " is-focus" : "")}
                    onClick={() => { setFocus(event.timestamp); setPicked(event); }}>
                  <td>{stamp(event.timestamp)}</td>
                  <td>{event.candle_identity || "—"}</td>
                  <td>{event.side || "—"}</td>
                  <td>{event.decision}</td>
                  <td>{event.gate_stage || "—"}</td>
                  <td>{event.blocker || "—"}</td>
                  <td>{event.final_state}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!(timeline?.events ?? []).length
            ? <div className="ivl-empty">{timeline?.coverage ?? "No recorded decisions."}</div> : null}
        </div>
        <small>{timeline?.coverage}</small>
      </div>

      <div className="ivl-foot">
        <b>OBSERVABILITY ONLY · NO ORDER PATH</b>
        <span>This page cannot place an order, change an operating mode or alter a parameter.</span>
      </div>
    </> : null}
  </div>;
}
