import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import Card from "../components/common/Card";
import { Badge, EmptyState, Field, PageHeader } from "../components/common/ui";
import NativeSMCChartOverlay, {
  type NativeCandle, type NativeEvent, type NativePivot, type NativeProposal, type NativeSMCChartState,
  type ChartPriceViewport, type ChartTimeViewport, type NativeSMCOverlayFilters, type NativeSnapshot, type NativeZone,
} from "../components/chart/NativeSMCChartOverlay";
import { apiGet, apiPostJson, useLive } from "../lib/api";

type ReviewClassification = "CORRECT" | "INCORRECT" | "AMBIGUOUS";
interface Setup { id: string; direction: "bullish" | "bearish"; phase: string; next_required_event: string; transitions: { id: string; timestamp: string; to_phase: string; reason: string; object_id?: string | null }[] }
interface LadderCondition { key: string; label: string; status: "PASS" | "MISSING" | "NOT_REQUIRED" | "INVALIDATED" | "EXPIRED"; detail: string; object_id?: string | null; bars_since?: number | null }
interface LadderTrace { direction: "bullish" | "bearish"; state: string; conditions: LadderCondition[]; missing_conditions: string[]; invalidation_reason?: string | null; next_required_event: string; supporting_object_ids: string[]; event_ages: Record<string, number | null>; setup_id?: string | null }
interface LadderCandidate { strategy_id: string; version: string; research_status: string; execution_allowed: false; selected_direction?: "bullish" | "bearish" | null; state: string; conflict: boolean; next_required_event: string; direction_traces: LadderTrace[]; selected_trace?: LadderTrace | null }
interface StrategyLadder { research_id: string; ladder_id: string; version: string; research_only: true; execution_allowed: false; definitions_frozen: true; candidates: LadderCandidate[] }
interface NativeState extends NativeSMCChartState { pivots: NativePivot[]; events: NativeEvent[]; fair_value_gaps: NativeZone[]; order_blocks: NativeZone[]; proposals: NativeProposal[]; setups: Setup[]; strategy_ladder?: StrategyLadder }
interface ReviewSampleItem { object_id: string; category: string; timestamp: string; setup_id?: string | null }
interface ReviewSampleResponse { sample: ReviewSampleItem[] }
interface Review { id: string; object_id: string; component: string; classification: ReviewClassification; reason?: string | null; notes?: string | null; selected_candle_timestamp?: string | null }
interface ReviewsResponse { reviews: Review[] }
interface PineReference { reference_id: string; status: string; language: string; sha256: string; execution_allowed: false; notice: string; content: string }
interface DataProvenance { mode: string; venue: string; market: string; observed_at: string; closed_candles_loaded: number; closed_candles_visible: number; last_closed_candle: string; forming_candle_excluded: boolean; execution_allowed: false }
interface LiveHistoryPage { candles: NativeCandle[]; has_more_history: boolean; oldest: string | null; newest: string | null; execution_allowed: false }

type ReferenceInput = { label: string; value: string };
type ReferenceInputGroup = { title: string; inputs: ReferenceInput[] };
const LIVE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
// Binance USD-M is the feed every engine here actually trades: the labs,
// the instances and shadow research all read the one shared hub. It belongs
// first and by default, because a chart that shows a different venue than
// the engine traded is a chart you cannot read a rejection off. The
// alternates stay for Pine-parity work and for the case where Binance
// blocks the host IP; the server already marks them VIEW_ONLY_ALTERNATE and
// refuses to let them authorize an entry.
type ChartFeed = "binance_usdm" | "checkpoint" | "mexc_perpetual" | "kraken_spot";
// Naming the venue matters more than saying "live": MEXC and Kraken print
// different wicks from Binance at the same moment, so a generic label left
// the one fact a reader needs off the screen.
const FEED_LABEL: Record<ChartFeed, string> = {
  binance_usdm: "Binance USD-M live",
  checkpoint: "Verified March 2025",
  mexc_perpetual: "MEXC perpetual · view only",
  kraken_spot: "Kraken spot · view only",
};
const CHART_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"];
const DEFAULT_VISIBLE_BARS: Record<string, number> = {
  "1m": 180, "3m": 150, "5m": 120, "15m": 110, "30m": 96,
  "1h": 72, "4h": 60, "1d": 60, "1w": 52,
};

function defaultVisibleBars(timeframe: string) {
  const base = DEFAULT_VISIBLE_BARS[timeframe] ?? 120;
  if (typeof window === "undefined") return base;
  if (window.innerWidth >= 1_900) return Math.round(base * 1.15);
  if (window.innerWidth < 1_024) return Math.max(70, Math.round(base * 0.85));
  return base;
}
const REFERENCE_INPUT_GROUPS: ReferenceInputGroup[] = [
  { title: "PRO Strategy & Automation", inputs: [
    { label: "ATR Length for SL", value: "14" }, { label: "Stop Loss ATR Multiplier", value: "1.5" }, { label: "Risk/Reward Ratio (TP)", value: "2.5" },
    { label: "Killzone Session", value: "0700–1100, 1300–1600" }, { label: "Killzone Timezone", value: "Europe/London" }, { label: "Liquidity Sweep Memory", value: "10 bars" },
    { label: "CHoCH Memory", value: "8 bars" }, { label: "FVG Entry Window", value: "5 bars" }, { label: "Confirmed bars only", value: "Enabled" }, { label: "Structure Break ATR Filter", value: "0.3" },
  ] },
  { title: "Price Action Filters", inputs: [{ label: "Require Rejection Candle Entry", value: "Enabled" }, { label: "Wick-to-Body Ratio", value: "2.0" }] },
  { title: "Internal & Swing Structure", inputs: [
    { label: "Show Internal Structure", value: "Enabled" }, { label: "Internal Pivot Length", value: "5" }, { label: "Internal Bull/Bear Structure", value: "All" }, { label: "Internal Label Size", value: "Tiny" }, { label: "Internal Confluence Filter", value: "Disabled" },
    { label: "Show Swing Structure", value: "Enabled" }, { label: "Swing Structure", value: "All" }, { label: "Swing Label Size", value: "Small" }, { label: "Show Swing Points", value: "Disabled" }, { label: "Swing Pivot Length", value: "50" }, { label: "Strong/Weak High/Low", value: "Enabled" },
  ] },
  { title: "Order Blocks & Fair Value Gaps", inputs: [
    { label: "Internal Order Blocks", value: "Enabled · 5" }, { label: "Swing Order Blocks", value: "Disabled · 5" }, { label: "Order Block Filter", value: "ATR" }, { label: "OB Mitigation", value: "High/Low" },
    { label: "Equal High/Low", value: "Enabled · 3 bars · threshold 0.1" }, { label: "Fair Value Gaps", value: "Enabled" }, { label: "FVG Auto Threshold", value: "Enabled" }, { label: "FVG Volume Surge Filter", value: "Enabled" }, { label: "FVG Timeframe", value: "Chart timeframe" }, { label: "Extend FVG", value: "1 bar" },
  ] },
  { title: "Premium/Discount & Dashboard", inputs: [
    { label: "Premium / Discount Zones", value: "Enabled" }, { label: "Dashboard", value: "Enabled · Top Right" }, { label: "HTF Bias Filter", value: "240 minutes (4h)" }, { label: "POI Proximity ATR Buffer", value: "0.8" },
    { label: "Zone Lookback", value: "80 bars" }, { label: "Zone / Label Right Offset", value: "2 bars" },
  ] },
  { title: "Reference Visual Colours", inputs: [
    { label: "Bullish / Bearish Structure", value: "#089981 / #F23645" }, { label: "Internal Bullish / Bearish OB", value: "#3179F5 / #F77C80 · 80% transparent" },
    { label: "Swing Bullish / Bearish OB", value: "#1848CC / #B22833 · 80% transparent" }, { label: "Bullish / Bearish FVG", value: "#00FF68 / #FF0008 · 70% transparent" },
    { label: "Premium / Equilibrium / Discount", value: "#F23645 / #878B94 / #089981" }, { label: "Colour Candles", value: "Disabled" },
  ] },
];

const defaultFilters: NativeSMCOverlayFilters = { pivots: true, internal: true, swing: true, structure: true, liquidity: true, fvg: true, orderBlocks: true, mitigated: true, labels: true };
const shortId = (id?: string | null) => id ? `${id.slice(0, 10)}…` : "—";
const at = (value?: string | null) => value ? value.replace("T", " ").replace("+00:00", " UTC").slice(0, 23) : "—";
const bias = (value?: number) => value === 1 ? "Bullish" : value === -1 ? "Bearish" : "Neutral";
const category = (value: string) => value.split("_").join(" ").toUpperCase();
const toUtc = (value: string) => value ? new Date(value).toISOString() : "";

function mergeCandles(...groups: NativeCandle[][]): NativeCandle[] {
  const byTimestamp = new Map<string, NativeCandle>();
  for (const group of groups) for (const candle of group) byTimestamp.set(candle.timestamp, candle);
  return [...byTimestamp.values()].sort((left, right) => left.timestamp.localeCompare(right.timestamp));
}

function Toggle({ label, enabled, onClick }: { label: string; enabled: boolean; onClick: () => void }) {
  return <button type="button" className={`btn ${enabled ? "btn-primary" : "btn-soft"}`} style={{ padding: "6px 9px", fontSize: 11 }} onClick={onClick} aria-pressed={enabled}>{label}</button>;
}

function VerdictPanel({ snapshot, selectedObjectId, data }: { snapshot?: NativeSnapshot | null; selectedObjectId?: string; data?: NativeState }) {
  const fvg = snapshot?.active_fvg_ids.map((id) => data?.fair_value_gaps.find((row) => row.id === id)).find(Boolean);
  const ob = snapshot?.active_ob_ids.map((id) => data?.order_blocks.find((row) => row.id === id)).find(Boolean);
  const setup = snapshot?.active_setup_id ? data?.setups.find((row) => row.id === snapshot.active_setup_id) : undefined;
  const verdict = snapshot?.setup_phase ?? "WATCHLIST";
  const reason = snapshot ? [snapshot.session === "inactive" ? "Outside active session" : null, !fvg && !ob ? "No active POI" : null, snapshot.latest_sweep_id ? "Liquidity sweep observed" : "Awaiting liquidity"].filter(Boolean).join(" · ") : "Select a closed candle";
  const rows: [string, string][] = [
    ["HTF", bias(snapshot?.htf_bias)], ["Swing", bias(snapshot?.swing_bias)], ["Internal", bias(snapshot?.internal_bias)],
    ["Location", snapshot?.dealing_range.area?.toUpperCase() ?? "—"], ["POI", fvg ? `FVG ${shortId(fvg.id)}` : ob ? `OB ${shortId(ob.id)}` : "None"],
    ["Liquidity", snapshot?.latest_sweep_id ? `Sweep ${shortId(snapshot.latest_sweep_id)}` : "None recorded"],
    ["Structure", snapshot?.event_ids.length ? `${snapshot.event_ids.length} native event(s)` : "No event"],
    ["Session", snapshot?.session ?? "—"], ["Rejection", snapshot?.price_action.bullish_rejection ? "Bullish" : snapshot?.price_action.bearish_rejection ? "Bearish" : "None"],
    ["Setup", setup?.phase ?? snapshot?.setup_phase ?? "IDLE"], ["Action", snapshot?.setup_phase === "ENTRY_READY" ? "RESEARCH ONLY" : "WAIT"],
  ];
  return <Card title="Native SMC verdict" subtitle="factual snapshot · no probabilities">
    <div className="instance-risk-notice amber"><b>FINAL VERDICT · {verdict}</b><br /><span className="dim">Reason: {reason}</span><br /><span className="dim">Next: {snapshot?.next_required_event ?? "Select a closed candle"}</span></div>
    <div className="risk-list terminal" style={{ marginTop: 10 }}>{rows.map(([label, value]) => <div className="risk-item" key={label}><span>{label}</span><b>{value}</b></div>)}</div>
    <div className="dim" style={{ marginTop: 10, fontSize: 11 }}>Selected object: <code>{shortId(selectedObjectId)}</code></div>
  </Card>;
}

function CandleInspector({ candle, snapshot, data }: { candle?: NativeCandle; snapshot?: NativeSnapshot | null; data?: NativeState }) {
  const activeFvgs = snapshot?.active_fvg_ids.map((id) => data?.fair_value_gaps.find((row) => row.id === id)).filter(Boolean) ?? [];
  const activeObs = snapshot?.active_ob_ids.map((id) => data?.order_blocks.find((row) => row.id === id)).filter(Boolean) ?? [];
  const proposal = snapshot?.proposal_ids.map((id) => data?.proposals.find((row) => row.id === id)).find(Boolean);
  if (!candle || !snapshot) return <Card title="Selected closed candle" subtitle="click a candle or choose a review item"><EmptyState text="No historical snapshot selected." /></Card>;
  return <Card title="Selected closed candle" subtitle={at(candle.timestamp)}>
    <div className="risk-list terminal">
      <div className="risk-item"><span>OHLCV</span><b>O {candle.open} · H {candle.high} · L {candle.low} · C {candle.close} · V {candle.volume}</b></div>
      <div className="risk-item"><span>Bias</span><b>{bias(snapshot.htf_bias)} HTF · {bias(snapshot.swing_bias)} swing · {bias(snapshot.internal_bias)} internal</b></div>
      <div className="risk-item"><span>Location</span><b>{snapshot.dealing_range.area.toUpperCase()} · EQ {snapshot.dealing_range.equilibrium}</b></div>
      <div className="risk-item"><span>Structure</span><b>{snapshot.event_ids.length ? snapshot.event_ids.map(shortId).join(" ") : "None"}</b></div>
      <div className="risk-item"><span>Liquidity</span><b>{shortId(snapshot.latest_sweep_id)}</b></div>
      <div className="risk-item"><span>FVG / OB</span><b>{activeFvgs.map((row: any) => shortId(row.id)).join(" ") || "—"} / {activeObs.map((row: any) => shortId(row.id)).join(" ") || "—"}</b></div>
      <div className="risk-item"><span>Rejection</span><b>{snapshot.price_action.bullish_rejection ? "Bullish" : snapshot.price_action.bearish_rejection ? "Bearish" : "None"}</b></div>
      <div className="risk-item"><span>Setup / next</span><b>{snapshot.setup_phase ?? "IDLE"} · {snapshot.next_required_event}</b></div>
      <div className="risk-item"><span>Proposal</span><b>{proposal ? `${shortId(proposal.id)} · E ${proposal.entry} · SL ${proposal.stop} · TP ${proposal.target}` : "None"}</b></div>
    </div>
  </Card>;
}

function PineReferencePanel({ reference, error }: { reference: PineReference | null; error: string | null }) {
  if (error) return <div className="instance-risk-notice red">{error}</div>;
  if (!reference) return <EmptyState text="Loading the immutable Pine reference…" />;
  return <Card title="Pine reference source" subtitle={`${reference.reference_id} · ${reference.status} · SHA-256 ${reference.sha256.slice(0, 12)}…`}>
    <div className="instance-risk-notice amber"><b>Reference only — not executed.</b> {reference.notice}</div>
    <pre aria-label="Read-only Pine reference source" style={{ margin: "12px 0 0", maxHeight: "calc(100vh - 300px)", minHeight: 560, overflow: "auto", padding: 16, borderRadius: 8, background: "#0a0c10", border: "1px solid var(--border)", color: "#d8deea", fontSize: 12, lineHeight: 1.55, whiteSpace: "pre" }}>{reference.content}</pre>
  </Card>;
}

function StrategyLadderTrace({ candidate, onObjectSelect }: { candidate?: LadderCandidate; onObjectSelect: (id: string) => void }) {
  if (!candidate) return <Card title="SMC strategy ladder" subtitle="frozen native-object research definitions"><EmptyState text="Loading the read-only candidate trace…" /></Card>;
  const trace = candidate.selected_trace;
  return <Card title="SMC strategy ladder" subtitle={`${candidate.strategy_id} · ${candidate.research_status} · execution disabled`}>
    <div className="instance-risk-notice amber"><b>{candidate.state}</b><br /><span className="dim">{candidate.next_required_event}</span></div>
    {trace ? <><div className="smc-ladder-meta"><span>Direction <b>{trace.direction}</b></span><span>Setup <code>{shortId(trace.setup_id)}</code></span></div><div className="smc-ladder-conditions">{trace.conditions.map((condition) => <button type="button" key={`${condition.key}-${condition.object_id ?? "none"}`} className={`smc-ladder-condition ${condition.status.toLowerCase()}`} onClick={() => condition.object_id && onObjectSelect(condition.object_id)} disabled={!condition.object_id}><span>{condition.label}</span><b>{condition.status.replace("_", " ")}</b><small>{condition.detail}{condition.bars_since !== undefined && condition.bars_since !== null ? ` · ${condition.bars_since} bars` : ""}</small></button>)}</div>{trace.invalidation_reason ? <div className="instance-risk-notice red">{trace.invalidation_reason}</div> : null}</> : <EmptyState text="No directional trace is available for this native closed-bar state." />}
  </Card>;
}

function SMCTradingToolbar({ symbol, timeframe, chartFeed, live, lastPrice, reviewProgress, showObjects, showJump, autoFollowLatest, candidateId, candidates, onSymbolChange, onTimeframeChange, onFeedChange, onCandidateChange, onToggleObjects, onToggleJump, onFit, onLatest, onCompare, onAutoScale, onOpenSettings, onFullScreen }: {
  symbol: string; timeframe: string; live: boolean; lastPrice?: number; onSymbolChange: (symbol: string) => void;
  chartFeed: ChartFeed; reviewProgress: string; showObjects: boolean; showJump: boolean; autoFollowLatest: boolean;
  candidateId: string; candidates: LadderCandidate[]; onCandidateChange: (candidate: string) => void;
  onTimeframeChange: (timeframe: string) => void; onFeedChange: (feed: ChartFeed) => void;
  onToggleObjects: () => void; onToggleJump: () => void; onFit: () => void; onLatest: () => void; onCompare: () => void; onAutoScale: () => void; onOpenSettings: () => void; onFullScreen: () => void;
}) {
  return <div className="smc-terminal-toolbar" aria-label="SMC chart toolbar">
    <div className="smc-terminal-market"><span className={`pulse-dot ${live ? "green" : "gold"}`} /><select aria-label="Chart market" value={symbol} onChange={(event) => onSymbolChange(event.target.value)}>{LIVE_SYMBOLS.map((row) => <option key={row}>{row}</option>)}</select></div>
    <div className="smc-timeframe-group" role="group" aria-label="Chart timeframe">{CHART_TIMEFRAMES.map((row) => <button key={row} type="button" className={row === timeframe ? "active" : ""} aria-pressed={row === timeframe} onClick={() => onTimeframeChange(row)}>{row}</button>)}</div>
    <select className="smc-toolbar-select" aria-label="Chart data source" value={chartFeed} onChange={(event) => onFeedChange(event.target.value as typeof chartFeed)}><option value="binance_usdm">Binance USD-M · live paper feed</option><option value="checkpoint">Verified March checkpoint</option><option value="mexc_perpetual">MEXC perpetual</option><option value="kraken_spot">Kraken spot</option></select>
    <select className="smc-toolbar-select" aria-label="Frozen SMC research candidate" value={candidateId} onChange={(event) => onCandidateChange(event.target.value)}>{candidates.map((candidate) => <option key={candidate.strategy_id} value={candidate.strategy_id}>{candidate.strategy_id.replace("SMC_", "").replace(/_/g, " ")} · {candidate.state}</option>)}</select>
    <span className="smc-toolbar-mode">Candles</span><span className="smc-research-chip">SMC NATIVE V1 · RESEARCH</span>
    <div className="smc-terminal-actions"><span className="smc-live-quote">{lastPrice ? lastPrice.toLocaleString(undefined, { maximumFractionDigits: 4 }) : "Closed bars"}</span><button className={`btn ${showJump ? "btn-primary" : "btn-soft"}`} type="button" onClick={onToggleJump}>⌕ Time</button><button className={`btn ${showObjects ? "btn-primary" : "btn-soft"}`} type="button" onClick={onToggleObjects}>Objects</button><button className="btn btn-soft" type="button" onClick={onFit}>Fit visible structure</button><button className="btn btn-soft" type="button" onClick={onAutoScale}>Auto scale</button><button className={`btn ${autoFollowLatest ? "btn-soft" : "btn-primary"}`} type="button" onClick={onLatest}>{autoFollowLatest ? "Latest" : "Go to latest"}</button><button className="btn btn-soft" type="button" onClick={onCompare}>Compare</button><button className="btn btn-soft" type="button" onClick={onOpenSettings}>Settings</button><button className="btn btn-primary" type="button" onClick={onFullScreen}>Full screen</button><span className="smc-toolbar-progress">{reviewProgress}</span></div>
  </div>;
}

function SMCWatchlist({ symbol, activePrice, chartFeed, collapsed, onToggle, onSelect }: {
  symbol: string; activePrice?: number; chartFeed: ChartFeed; collapsed: boolean; onToggle: () => void; onSelect: (symbol: string) => void;
}) {
  return <aside className={`smc-watchlist ${collapsed ? "is-collapsed" : ""}`} aria-label="Research market watchlist">
    <div className="smc-watchlist-head"><div><b>{collapsed ? "WL" : "WATCHLIST"}</b><span>{collapsed ? "" : "native chart markets"}</span></div><button className="btn btn-soft" type="button" onClick={onToggle} aria-expanded={!collapsed}>{collapsed ? "›" : "‹"}</button></div>
    {!collapsed ? <><div className="smc-watchlist-columns"><span>Symbol</span><span>Last</span><span>State</span></div><div className="smc-watchlist-rows">{LIVE_SYMBOLS.map((row) => {
      const selected = row === symbol;
      const unavailable = chartFeed === "checkpoint" && row !== "BTCUSDT";
      return <button type="button" className={selected ? "active" : ""} key={row} onClick={() => onSelect(row)}><span><i className={row.slice(0, 3).toLowerCase()}>{row.slice(0, 3)}</i>{row}</span><b>{selected && activePrice ? activePrice.toLocaleString(undefined, { maximumFractionDigits: 4 }) : "—"}</b><em>{unavailable ? "Unavailable" : selected ? "Loaded" : "Load"}</em></button>;
    })}</div><p>Only the selected market displays a returned price. The frozen checkpoint contains BTCUSDT only; other rows move to an explicit live venue instead of inventing history.</p></> : null}
  </aside>;
}

function IndicatorAndChartSettings({ lightChart, setLightChart, visibleBars, setVisibleBars, rightOffsetBars, setRightOffsetBars, liveRefreshMs, setLiveRefreshMs, setFitSignal }: {
  lightChart: boolean; setLightChart: (value: boolean) => void; visibleBars: number; setVisibleBars: (value: number) => void;
  rightOffsetBars: number; setRightOffsetBars: (value: number) => void; liveRefreshMs: number; setLiveRefreshMs: (value: number) => void; setFitSignal: Dispatch<SetStateAction<number>>;
}) {
  return <div className="smc-settings-layout">
    <Card title="Chart settings" subtitle="active display preferences — they never alter native SMC research state">
      <div className="form-grid smc-settings-grid">
        <Field label="Visible candles"><select value={visibleBars} onChange={(event) => setVisibleBars(Number(event.target.value))}><option value={100}>100 · close focus</option><option value={120}>120 · default intraday</option><option value={150}>150 · wider intraday</option><option value={240}>240 · extended view</option><option value={400}>400 · broad context</option></select></Field>
        <Field label="Right-edge space"><select value={rightOffsetBars} onChange={(event) => setRightOffsetBars(Number(event.target.value))}><option value={0}>None · last candle at edge</option><option value={6}>6 bars · compact</option><option value={12}>12 bars · standard</option><option value={24}>24 bars · extended</option><option value={48}>48 bars · planning room</option><option value={96}>96 bars · latest near centre</option><option value={160}>160 bars · wide future space</option></select></Field>
        <Field label="Live refresh cadence"><select value={liveRefreshMs} onChange={(event) => setLiveRefreshMs(Number(event.target.value))}><option value={2500}>Exchange cadence · about 2.5 seconds</option><option value={5000}>Every 5 seconds</option><option value={10000}>Every 10 seconds</option></select></Field>
        <Field label="Chart theme"><select value={lightChart ? "light" : "dark"} onChange={(event) => setLightChart(event.target.value === "light")}><option value="dark">Nexus dark</option><option value="light">Light</option></select></Field>
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}><button className="btn btn-primary" type="button" onClick={() => setFitSignal((value) => value + 1)}>Reset chart view</button><span className="dim" style={{ alignSelf: "center", fontSize: 12 }}>Wheel zooms · drag pans · hovering never moves the chart.</span></div>
      <div className="instance-risk-notice amber" style={{ marginTop: 12 }}><b>Premium / discount range.</b> The chart now selects a recent range automatically from the active timeframe (rather than using one fixed all-history range). This changes the display only; native SMC snapshot calculations remain unchanged.</div>
      <div className="instance-risk-notice amber" style={{ marginTop: 12 }}><b>Live display boundary.</b> A forming candle may move visually, but it is not supplied to the native SMC engine. Structure, zones, snapshots, and execution authority remain closed-bar-only.</div>
    </Card>
    <Card title="Pine indicator inputs" subtitle="immutable SMC PRO v2 reference defaults · shown for TradingView comparison">
      <div className="instance-risk-notice amber"><b>Read-only reference.</b> These are the original Pine defaults. Change them in TradingView when comparing the reference script; this Lab does not reconfigure the Pine source or native SMC model.</div>
      <div className="smc-reference-input-groups">{REFERENCE_INPUT_GROUPS.map((group) => <details key={group.title} open><summary>{group.title}<span>{group.inputs.length} inputs</span></summary><div className="smc-reference-inputs">{group.inputs.map((input) => <label key={input.label}><span>{input.label}</span><input value={input.value} readOnly aria-label={`${group.title}: ${input.label}`} /></label>)}</div></details>)}</div>
    </Card>
  </div>;
}

export default function NativeSMCVisualPage() {
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("5m");
  const [selectedId, setSelectedId] = useState("");
  const [selectedCandle, setSelectedCandle] = useState("");
  const [jumpValue, setJumpValue] = useState("");
  const [filters, setFilters] = useState(defaultFilters);
  const [lightChart, setLightChart] = useState(false);
  const [workspace, setWorkspace] = useState<"chart" | "pine" | "settings">("chart");
  const [chartFeed, setChartFeed] = useState<ChartFeed>("binance_usdm");
  const [fullChart, setFullChart] = useState(false);
  const [fitSignal, setFitSignal] = useState(0);
  const [visibleBars, setVisibleBars] = useState(() => defaultVisibleBars("5m"));
  const [rightOffsetBars, setRightOffsetBars] = useState(12);
  const [liveRefreshMs, setLiveRefreshMs] = useState(2_500);
  const [watchlistCollapsed, setWatchlistCollapsed] = useState(() => localStorage.getItem("tradexa.smc.watchlistCollapsed") === "1");
  const [smcPanelCollapsed, setSmcPanelCollapsed] = useState(() => localStorage.getItem("tradexa.smc.smcPanelCollapsed") === "1");
  const [showObjects, setShowObjects] = useState(false);
  const [showJump, setShowJump] = useState(false);
  const [bottomTab, setBottomTab] = useState<"review" | "inspector" | "timeline" | "proposals">("review");
  const [selectedCandidateId, setSelectedCandidateId] = useState("SMC_S1_PIVOT_REVERSAL");
  const [latestSignal, setLatestSignal] = useState(0);
  const [autoFollowLatest, setAutoFollowLatest] = useState(true);
  const [priceViewport, setPriceViewport] = useState<ChartPriceViewport>({ auto: true, scale: 1, offset: 0 });
  const [timeViewport, setTimeViewport] = useState<ChartTimeViewport | null>(null);
  const [olderCandles, setOlderCandles] = useState<NativeCandle[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyPrepend, setHistoryPrepend] = useState({ version: 0, count: 0 });
  // The chart can emit several near-left-edge viewport updates for a single
  // drag. Keep one request in flight so those events cannot fetch duplicate
  // exchange pages before React has rendered `historyLoading`.
  const historyRequestRef = useRef(false);
  const [classification, setClassification] = useState<ReviewClassification>("CORRECT");
  const [reason, setReason] = useState("");
  const [expectedStructure, setExpectedStructure] = useState("");
  const [actualStructure, setActualStructure] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const sample = useLive<ReviewSampleResponse>(`/research/smc/review-sample?symbol=${symbol}&timeframe=${timeframe}`, 15_000);
  // A fresh visual session deliberately opens on the newest working bars.
  // A review only becomes a navigation target after the user selects it.
  const selectedSample = sample.data?.sample.find((row) => row.object_id === selectedId);
  const focusedAt = chartFeed === "checkpoint" ? (selectedCandle || selectedSample?.timestamp || "") : "";
  const chartPath = chartFeed === "checkpoint"
    ? `/research/smc/chart?symbol=${symbol}&timeframe=${timeframe}&window=800${focusedAt ? `&at=${encodeURIComponent(focusedAt)}` : ""}`
    // Keep a full recent history loaded behind the initial screen window.
    // `visibleBars` controls only the initial chart viewport; sending it to
    // the API used to discard all older candles, leaving nothing to drag back
    // into on a live chart.
    : `/research/smc/live-chart?symbol=${symbol}&timeframe=${timeframe}&venue=${chartFeed}&window=800&visible=800`;
  const state = useLive<NativeState & { data_provenance?: DataProvenance }>(chartPath, chartFeed === "checkpoint" ? 5_000 : liveRefreshMs);
  const reviews = useLive<ReviewsResponse>(`/research/smc/reviews?symbol=${symbol}&timeframe=${timeframe}`, 15_000);
  const pineReference = useLive<PineReference>("/research/smc/pine-reference", 600_000);
  const data = state.data;
  const chartData = useMemo(() => data ? { ...data, candles: mergeCandles(olderCandles, data.candles) } : null, [data, olderCandles]);
  const reviewItems = sample.data?.sample ?? [];
  const reviewIndex = Math.max(0, reviewItems.findIndex((row) => row.object_id === selectedId));
  const selectedObjectId = selectedId || undefined;
  const selectedSnapshot = data?.snapshot_ledger.find((row) => row.candle_open === focusedAt) ?? data?.selected_snapshot ?? data?.snapshot;
  const selectedRow = data?.candles.find((row) => row.timestamp === (selectedSnapshot?.candle_open ?? focusedAt));
  const ladderCandidates = data?.strategy_ladder?.candidates ?? [];
  const selectedCandidate = ladderCandidates.find((candidate) => candidate.strategy_id === selectedCandidateId) ?? ladderCandidates[0];
  const highlightedObjectIds = useMemo(() => selectedCandidate?.selected_trace?.supporting_object_ids ?? [], [selectedCandidate]);
  const selectedReview = reviewItems[reviewIndex];
  const selectReview = useCallback((index: number) => {
    const item = reviewItems[index];
    if (!item) return;
    setSelectedId(item.object_id); setSelectedCandle(item.timestamp); setJumpValue(item.timestamp.slice(0, 16));
  }, [reviewItems]);
  const onSelectCandle = useCallback((value: string) => { setSelectedCandle(value); setJumpValue(value.slice(0, 16)); }, []);
  const setFilter = (key: keyof NativeSMCOverlayFilters) => setFilters((current) => ({ ...current, [key]: !current[key] }));
  const jumpToTime = () => { if (!jumpValue) return; setSelectedCandle(toUtc(jumpValue)); };
  const stepCandle = (direction: -1 | 1) => {
    const index = data?.candles.findIndex((row) => row.timestamp === (selectedSnapshot?.candle_open ?? focusedAt)) ?? -1;
    const next = data?.candles[index + direction];
    if (next) onSelectCandle(next.timestamp);
  };
  const goToLive = useCallback(() => {
    setAutoFollowLatest(true);
    setLatestSignal((value) => value + 1);
  }, []);
  useEffect(() => {
    historyRequestRef.current = false;
    setOlderCandles([]); setHasMoreHistory(true); setHistoryLoading(false); setHistoryError(null);
    setHistoryPrepend((current) => ({ version: current.version + 1, count: 0 }));
  }, [symbol, timeframe, chartFeed]);
  const requestOlderHistory = useCallback(async () => {
    if (chartFeed === "checkpoint" || historyRequestRef.current || historyLoading || !hasMoreHistory || !chartData?.candles.length) return;
    const before = chartData.candles[0].timestamp;
    historyRequestRef.current = true;
    setHistoryLoading(true); setHistoryError(null);
    try {
      const page = await apiGet<LiveHistoryPage>(`/research/smc/live-history?symbol=${symbol}&timeframe=${timeframe}&venue=${chartFeed}&before=${encodeURIComponent(before)}&limit=400`);
      const merged = mergeCandles(page.candles, olderCandles);
      const count = Math.max(0, merged.length - olderCandles.length);
      setOlderCandles(merged);
      setHasMoreHistory(page.has_more_history && count > 0);
      if (count > 0) setHistoryPrepend((current) => ({ version: current.version + 1, count }));
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : "Unable to load older exchange candles.");
    } finally { historyRequestRef.current = false; setHistoryLoading(false); }
  }, [chartFeed, chartData?.candles, hasMoreHistory, historyLoading, olderCandles, symbol, timeframe]);
  const handleViewportChange = useCallback((range: ChartTimeViewport) => {
    setTimeViewport(range);
    setAutoFollowLatest(range.end >= 99.2);
  }, []);
  const submitReview = async () => {
    if (!selectedObjectId || saving || !selectedReview) return;
    setSaving(true); setReviewError(null);
    try {
      await apiPostJson(`/research/smc/reviews?symbol=${symbol}&timeframe=${timeframe}`, {
        object_id: selectedObjectId, component: category(selectedReview.category), classification,
        reason: reason || null, expected_structure: expectedStructure || null, actual_structure: actualStructure || null,
        notes: notes || null, screenshot_timestamp: selectedSnapshot?.candle_open ?? null,
        selected_candle_timestamp: selectedSnapshot?.candle_open ?? null,
        visible_range_start: data?.candles[0]?.timestamp ?? null, visible_range_end: data?.candles[data.candles.length - 1]?.timestamp ?? null,
      });
      setReason(""); setExpectedStructure(""); setActualStructure(""); setNotes(""); await reviews.refetch();
    } catch (error) { setReviewError(error instanceof Error ? error.message : "Unable to save review evidence."); }
    finally { setSaving(false); }
  };
  const switchDataset = (nextSymbol: string, nextTimeframe = timeframe) => {
    // The attached visual-review checkpoint is BTCUSDT 5m only. Other views
    // should immediately use the explicit live venue rather than appear empty.
    if (chartFeed === "checkpoint" && (nextSymbol !== "BTCUSDT" || nextTimeframe !== "5m")) setChartFeed("binance_usdm");
    setSymbol(nextSymbol); setTimeframe(nextTimeframe); setSelectedId(""); setSelectedCandle(""); setJumpValue(""); setVisibleBars(defaultVisibleBars(nextTimeframe)); setTimeViewport(null); setPriceViewport({ auto: true, scale: 1, offset: 0 }); setAutoFollowLatest(true); setLatestSignal((value) => value + 1);
  };

  useEffect(() => {
    const isTyping = (target: EventTarget | null) => target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement || (target instanceof HTMLElement && target.isContentEditable);
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTyping(event.target)) return;
      if (event.key === "Escape") { if (fullChart) setFullChart(false); else { setSelectedCandle(""); setSelectedId(""); } return; }
      if (event.key.toLowerCase() === "f") { event.preventDefault(); setFitSignal((value) => value + 1); setPriceViewport({ auto: true, scale: 1, offset: 0 }); return; }
      if (event.key.toLowerCase() === "l") { event.preventDefault(); goToLive(); return; }
      if (event.key === "ArrowLeft") { event.preventDefault(); event.shiftKey ? selectReview(Math.max(0, reviewIndex - 1)) : stepCandle(-1); }
      if (event.key === "ArrowRight") { event.preventDefault(); event.shiftKey ? selectReview(Math.min(reviewItems.length - 1, reviewIndex + 1)) : stepCandle(1); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [fullChart, reviewIndex, reviewItems.length, selectReview, goToLive]);

  useEffect(() => { localStorage.setItem("tradexa.smc.watchlistCollapsed", watchlistCollapsed ? "1" : "0"); }, [watchlistCollapsed]);
  useEffect(() => { localStorage.setItem("tradexa.smc.smcPanelCollapsed", smcPanelCollapsed ? "1" : "0"); }, [smcPanelCollapsed]);

  const adjustPriceViewport = useCallback((deltaY: number, verticalPan: boolean) => {
    setPriceViewport((current) => {
      const active = { ...current, auto: false };
      return verticalPan
        ? { ...active, offset: Math.max(-2, Math.min(2, active.offset + deltaY * 0.0025)) }
        : { ...active, scale: Math.max(0.2, Math.min(4, active.scale * Math.exp(deltaY * 0.008))) };
    });
  }, []);

  const chartSubtitle = data?.data_provenance
    ? `${data.data_provenance.venue} · ${data.data_provenance.market} · last closed ${at(data.data_provenance.last_closed_candle)}`
    : "native verified closed OHLCV";
  const liveDataStale = Boolean(chartFeed !== "checkpoint" && state.error);
  const chartPanel = data ? <section className="smc-chart-surface" aria-label="Native SMC chart workspace">
    <div className="smc-chart-heading"><div><b>Nexus SMC chart</b><span>{chartSubtitle} · {data.candles.length} closed candles{data.forming_candle ? " + visual forming candle" : ""}</span></div><Badge text="CLOSED-BAR SMC" tone="green" /></div>
    {data.data_provenance ? <div className={`smc-live-strip ${liveDataStale ? "is-stale" : ""}`}><span className={`pulse-dot ${liveDataStale ? "gold" : "green"}`} /><span><b>{liveDataStale ? "Live feed stale" : "Live exchange feed"}</b> · observed {at(data.live_display?.observed_at)} · {liveDataStale ? "last confirmed price frozen" : "forming candle is display-only"}</span><span className="dim">Native SMC uses closed candles only</span></div> : <div className="smc-live-strip"><span className="pulse-dot gold" /><span><b>Verified March 2025 checkpoint</b> · frozen human-review evidence</span><span className="dim">Execution disabled</span></div>}
    <NativeSMCChartOverlay state={chartData ?? data} timeframe={timeframe} rightOffsetBars={rightOffsetBars} initialVisibleBars={visibleBars} filters={filters} selectedObjectId={selectedObjectId} highlightedObjectIds={highlightedObjectIds} onCandleSelect={onSelectCandle} fitContentSignal={fitSignal} latestSignal={latestSignal} centerTimestamp={selectedId ? selectedSnapshot?.candle_open : undefined} priceViewport={priceViewport} viewport={timeViewport} onViewportChange={handleViewportChange} onHistoryNearStart={requestOlderHistory} historyLoading={historyLoading} hasMoreHistory={hasMoreHistory} historicalMode={!autoFollowLatest} onGoLive={goToLive} prependedHistory={historyPrepend} onPriceAxisDrag={adjustPriceViewport} onResetPriceScale={() => setPriceViewport({ auto: true, scale: 1, offset: 0 })} lightMode={lightChart} liveDataStale={liveDataStale} height={fullChart ? "calc(100vh - 250px)" : "min(68vh, 810px)"} />
    <div className="smc-chart-footer"><span><b>{autoFollowLatest ? "Live follow" : "Historical browse"}</b> · crosshair inspects closed candles without changing native SMC state{chartFeed !== "checkpoint" && !hasMoreHistory ? " · oldest page reached" : ""}{historyError ? ` · ${historyError}` : ""}</span><span>Drag chart to pan · wheel to zoom · hover for OHLCV · L / Latest returns to live · drag price scale to resize</span></div>
  </section> : null;

  const reviewTerminal = chartFeed === "checkpoint" ? <div className="smc-bottom-content">
    <div className="smc-review-navigation"><span><b>Frozen review</b> · {reviewItems.length ? `${reviewIndex + 1} / ${reviewItems.length}` : "No sample"}</span><button className="btn btn-soft" type="button" disabled={reviewIndex <= 0} onClick={() => selectReview(reviewIndex - 1)}>Previous</button><button className="btn btn-soft" type="button" onClick={() => stepCandle(-1)}>‹ Candle</button><button className="btn btn-soft" type="button" onClick={() => stepCandle(1)}>Candle ›</button><button className="btn btn-soft" type="button" disabled={reviewIndex >= reviewItems.length - 1} onClick={() => selectReview(reviewIndex + 1)}>Next</button></div>
    <div className="form-grid smc-review-fields"><Field label="Classification"><select value={classification} onChange={(event) => setClassification(event.target.value as ReviewClassification)}><option>CORRECT</option><option>INCORRECT</option><option>AMBIGUOUS</option></select></Field><Field label="Reason"><input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why?" /></Field><Field label="Expected"><input value={expectedStructure} onChange={(event) => setExpectedStructure(event.target.value)} placeholder="Expected structure" /></Field><Field label="Native result"><input value={actualStructure} onChange={(event) => setActualStructure(event.target.value)} placeholder="Native SMC result" /></Field><Field label="Notes"><input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Optional notes" /></Field></div>
    <button className="btn btn-primary" type="button" disabled={!selectedObjectId || saving} onClick={submitReview}>{saving ? "Saving…" : "Save review evidence"}</button>{reviewError ? <div className="instance-risk-notice red" role="alert">{reviewError}</div> : null}
  </div> : <div className="smc-bottom-content"><div className="instance-risk-notice amber"><b>Live view is observation only.</b> Formal parity classifications are recorded only against the frozen verified checkpoint.</div></div>;
  const terminalGrid = <div className={`smc-terminal-grid ${watchlistCollapsed ? "watchlist-collapsed" : ""} ${smcPanelCollapsed ? "panel-collapsed" : ""}`}>
    {!fullChart ? <SMCWatchlist symbol={symbol} activePrice={data?.live_display?.last_price} chartFeed={chartFeed} collapsed={watchlistCollapsed} onToggle={() => setWatchlistCollapsed((value) => !value)} onSelect={(value) => switchDataset(value)} /> : null}
    {chartPanel}
    <aside className={`smc-state-panel ${smcPanelCollapsed ? "is-collapsed" : ""}`} aria-label="SMC state panel"><div className="smc-state-panel-head"><b>{smcPanelCollapsed ? "SMC" : "Native SMC state"}</b><button className="btn btn-soft" type="button" onClick={() => setSmcPanelCollapsed((value) => !value)}>{smcPanelCollapsed ? "‹" : "›"}</button></div>{!smcPanelCollapsed ? <><StrategyLadderTrace candidate={selectedCandidate} onObjectSelect={setSelectedId} /><VerdictPanel snapshot={selectedSnapshot} selectedObjectId={selectedObjectId} data={data ?? undefined} /><CandleInspector candle={selectedRow} snapshot={selectedSnapshot} data={data ?? undefined} /></> : null}</aside>
  </div>;

  const chartWorkspace = state.error && !data ? <div className="instance-risk-notice red">{state.error}</div> : !data?.candles.length ? <EmptyState text={chartFeed === "checkpoint" ? "No verified closed-candle checkpoint is attached. Configure HUB_SMC_VISUAL_CHECKPOINT_PATH before reviewing native SMC." : "The selected live venue has not returned enough valid closed candles yet."} /> : <section className="smc-terminal-workspace">
    <SMCTradingToolbar symbol={symbol} timeframe={timeframe} chartFeed={chartFeed} live={Boolean(data.data_provenance)} lastPrice={data.live_display?.last_price} reviewProgress={chartFeed === "checkpoint" ? `${selectedId ? reviewIndex + 1 : 0}/${reviewItems.length || 0}` : "LIVE"} showObjects={showObjects} showJump={showJump} autoFollowLatest={autoFollowLatest} candidateId={selectedCandidate?.strategy_id ?? selectedCandidateId} candidates={ladderCandidates} onCandidateChange={setSelectedCandidateId} onSymbolChange={switchDataset} onTimeframeChange={(value) => switchDataset(symbol, value)} onFeedChange={(feed) => { setChartFeed(feed); setSelectedCandle(""); setTimeViewport(null); goToLive(); }} onToggleObjects={() => setShowObjects((value) => !value)} onToggleJump={() => setShowJump((value) => !value)} onFit={() => { setTimeViewport(null); setFitSignal((value) => value + 1); setPriceViewport({ auto: true, scale: 1, offset: 0 }); }} onLatest={goToLive} onCompare={() => setWorkspace("pine")} onAutoScale={() => setPriceViewport({ auto: true, scale: 1, offset: 0 })} onOpenSettings={() => setWorkspace("settings")} onFullScreen={() => setFullChart(true)} />
    {showJump ? <div className="smc-compact-control-row"><label>Jump to UTC<input type="datetime-local" value={jumpValue} onChange={(event) => setJumpValue(event.target.value)} /></label><button className="btn btn-primary" type="button" onClick={jumpToTime}>Jump</button>{chartFeed === "checkpoint" ? <label>Review item<select value={selectedObjectId ?? ""} onChange={(event) => selectReview(reviewItems.findIndex((row) => row.object_id === event.target.value))}><option value="">Select review item</option>{reviewItems.map((row, index) => <option key={row.object_id} value={row.object_id}>{index + 1} · {category(row.category)} · {at(row.timestamp)}</option>)}</select></label> : null}<label>Right space<select value={rightOffsetBars} onChange={(event) => setRightOffsetBars(Number(event.target.value))}><option value={6}>6 bars</option><option value={12}>12 bars</option><option value={24}>24 bars</option><option value={48}>48 bars</option><option value={96}>96 bars</option><option value={160}>160 bars</option></select></label></div> : null}
    {showObjects ? <div className="smc-overlay-controls"><span className="dim">Visual objects</span>{([ ["Pivots", "pivots"], ["Internal", "internal"], ["Swing", "swing"], ["Structure", "structure"], ["Liquidity", "liquidity"], ["FVG", "fvg"], ["Order blocks", "orderBlocks"], ["Mitigated", "mitigated"], ["Labels", "labels"] ] as [string, keyof NativeSMCOverlayFilters][]).map(([label, key]) => <Toggle key={key} label={label} enabled={filters[key]} onClick={() => setFilter(key)} />)}<Toggle label="Light" enabled={lightChart} onClick={() => setLightChart((value) => !value)} /></div> : null}
    {terminalGrid}
    <section className="smc-bottom-terminal"><div className="smc-bottom-tabs">{([ ["review", "Review"], ["inspector", "Object inspector"], ["timeline", "Timeline"], ["proposals", "Proposals"] ] as const).map(([key, label]) => <button key={key} type="button" className={bottomTab === key ? "active" : ""} onClick={() => setBottomTab(key)}>{label}</button>)}</div>{bottomTab === "review" ? reviewTerminal : bottomTab === "inspector" ? <CandleInspector candle={selectedRow} snapshot={selectedSnapshot} data={data} /> : bottomTab === "timeline" ? <div className="smc-bottom-content"><b>Native timeline</b><p>{selectedSnapshot ? `${at(selectedSnapshot.candle_open)} · ${selectedSnapshot.event_ids.length} native events · next ${selectedSnapshot.next_required_event}` : "Select a closed candle to inspect the native timeline."}</p></div> : <div className="smc-bottom-content"><b>Research proposals — not executable</b><p>{data.proposals.length ? data.proposals.map((proposal) => `${shortId(proposal.id)} · entry ${proposal.entry} · stop ${proposal.stop} · target ${proposal.target}`).join(" | ") : "No native research proposals at the selected snapshot."}</p></div>}</section>
    <footer className="smc-status-bar"><span>SMC_NATIVE_V1_RESEARCH</span><span>{symbol}</span><span>{timeframe}</span><span>{FEED_LABEL[chartFeed]}</span><span>Review {chartFeed === "checkpoint" ? `${reviewIndex + 1}/${reviewItems.length || 0}` : "—"}</span><span>EXECUTION DISABLED</span></footer>
  </section>;

  return <>
    <PageHeader title="Native SMC Visual Lab" subtitle="Native closed-candle SMC data · professional visual inspection workspace" actions={<><Badge text="SMC_NATIVE_V1_RESEARCH" tone="purple" /> <Badge text="EXECUTION DISABLED" tone="red" /></>} />
    <div className="smc-workspace-tabs"><button className={`btn ${workspace === "chart" ? "btn-primary" : "btn-soft"}`} type="button" onClick={() => setWorkspace("chart")}>Chart terminal</button><button className={`btn ${workspace === "pine" ? "btn-primary" : "btn-soft"}`} type="button" onClick={() => setWorkspace("pine")}>Pine reference</button><button className={`btn ${workspace === "settings" ? "btn-primary" : "btn-soft"}`} type="button" onClick={() => setWorkspace("settings")}>Indicator & chart settings</button><span className="dim">Browser interactions only — no visual control can calculate SMC or create an order.</span></div>
    {workspace === "chart" ? chartWorkspace : workspace === "pine" ? <PineReferencePanel reference={pineReference.data} error={pineReference.error} /> : <IndicatorAndChartSettings lightChart={lightChart} setLightChart={setLightChart} visibleBars={visibleBars} setVisibleBars={(value) => { setVisibleBars(value); setTimeViewport(null); setFitSignal((signal) => signal + 1); }} rightOffsetBars={rightOffsetBars} setRightOffsetBars={setRightOffsetBars} liveRefreshMs={liveRefreshMs} setLiveRefreshMs={setLiveRefreshMs} setFitSignal={setFitSignal} />}
    {fullChart && chartPanel ? <div className="smc-fullscreen" role="dialog" aria-modal="true" aria-label="Full screen native SMC chart"><div className="smc-fullscreen-header"><div><span className="eyebrow">SMC RESEARCH TERMINAL</span><b>{symbol} · {timeframe} · {FEED_LABEL[chartFeed]}</b></div><button className="btn btn-soft" type="button" onClick={() => setFullChart(false)}>Exit full screen · Esc</button></div>{terminalGrid}</div> : null}
  </>;
}
