import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import LabNewsGuard from "../components/research/LabNewsGuard";
import NativeSMCChartOverlay, {
  type ChartPriceViewport,
  type ChartTimeViewport,
  type NativeCandle,
  type NativeEvent,
  type NativePivot,
  type NativeProposal,
  type NativeSMCChartState,
  type NativeSMCOverlayFilters,
  type NativeZone,
} from "../components/chart/NativeSMCChartOverlay";
import { useApp } from "../app-context";
import { apiDownload, apiGet, apiPostJson, useLive } from "../lib/api";
import ResearchComparisonPanel from "../components/research/ResearchComparisonPanel";

type ChartFeed = "checkpoint" | "binance_usdm";
type ChartPreset = "clean" | "structure" | "zones" | "strategy" | "trades" | "debug";
type BottomTab = "positions" | "orders" | "trades" | "setups" | "rejected" | "journal" | "analysis" | "session" | "connection";

interface Setup {
  id: string;
  direction: "bullish" | "bearish";
  phase: string;
  next_required_event: string;
}

interface LadderCondition {
  key: string;
  label: string;
  status: "PASS" | "MISSING" | "NOT_REQUIRED" | "CONFLICT";
  detail: string;
  object_id?: string | null;
}

interface LadderTrace {
  direction: "bullish" | "bearish";
  state: string;
  conditions: LadderCondition[];
  missing_conditions: string[];
  next_required_event: string;
  supporting_object_ids: string[];
  setup_id?: string | null;
}

interface LadderCandidate {
  strategy_id: string;
  version: string;
  state: string;
  next_required_event: string;
  selected_trace?: LadderTrace | null;
}

interface StrategyLadder {
  candidates: LadderCandidate[];
}

interface SMCSourceModel {
  id: string;
  label: string;
  status: "ACTIVE" | "PARKED";
  narrative: string;
  ordered_rules: string[];
}

interface SMCSourceModelsResponse {
  models: SMCSourceModel[];
}

interface SMCSourceStrategyEvaluation {
  strategy_id: string;
  version: string;
  state: string;
  next_required_event: string;
  selected_candidate_id?: string | null;
  model: SMCSourceModel;
  native_object_ids: string[];
  missing_conditions: string[];
  ordered_condition_results: LadderCondition[];
  proposal_id?: string | null;
  setup_id?: string | null;
  trade_plan?: {
    entry: number;
    stop: number;
    target_1: number;
    target_1_r: number;
    target_2: number;
    target_2_r: number;
    risk_percent: number;
  } | null;
}

interface DataProvenance {
  mode: string;
  venue: string;
  market: string;
  observed_at: string;
  closed_candles_loaded: number;
  closed_candles_visible: number;
  last_closed_candle: string;
  forming_candle_excluded: boolean;
}

interface NativeState extends NativeSMCChartState {
  pivots: NativePivot[];
  events: NativeEvent[];
  fair_value_gaps: NativeZone[];
  order_blocks: NativeZone[];
  proposals: NativeProposal[];
  setups: Setup[];
  strategy_ladder?: StrategyLadder;
  source_strategy?: SMCSourceStrategyEvaluation;
  data_provenance?: DataProvenance;
  mtf_policy?: { label: string; available_entry_timeframes: string[] };
}

interface SMCPaperState {
  session: {
    id: string;
    mode: string;
    symbol: string;
    timeframe: string;
    operating_mode: string;
    model_id: string;
    risk_pct: number;
    started_at?: string;
    status?: string;
  };
  account: {
    balance: number;
    equity: number;
    available_margin: number;
    used_margin: number;
    open_risk: number;
    unrealized_pnl: number;
    leverage: number;
  };
  positions: Record<string, unknown>[];
  orders: Record<string, unknown>[];
  trades: Record<string, unknown>[];
  candidates: { proposal_id: string; status: string; reason: string; created_at: string }[];
  activity: Record<string, unknown>[];
}

interface SMCJournalRow {
  journal_id: string;
  session_id: string;
  symbol: string;
  timeframe: string;
  model_id: string;
  direction?: string | null;
  status: string;
  signal_timestamp?: string | null;
  created_at: string;
  proposal_id: string;
  net_pnl: number;
  data_quality: string;
  rule_compliance: string;
  native_object_ids: string[];
  ordered_conditions: LadderCondition[];
  missing_conditions: string[];
  trade_plan?: SMCSourceStrategyEvaluation["trade_plan"];
  notes: { id: string; note: string; created_at: string }[];
}

interface SMCJournalResponse {
  journal: SMCJournalRow[];
}

interface SMCSessionsResponse {
  sessions: Record<string, unknown>[];
}

interface LiveHistoryPage {
  candles: NativeCandle[];
  has_more_history: boolean;
}

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"];
const TABS: BottomTab[] = ["positions", "orders", "trades", "setups", "rejected", "journal", "analysis", "session", "connection"];

const PRESETS: Record<ChartPreset, NativeSMCOverlayFilters> = {
  clean: { pivots: false, internal: false, swing: false, structure: false, liquidity: false, fvg: false, orderBlocks: false, mitigated: false, labels: false },
  structure: { pivots: true, internal: true, swing: true, structure: true, liquidity: true, fvg: false, orderBlocks: false, mitigated: false, labels: true },
  zones: { pivots: false, internal: false, swing: false, structure: false, liquidity: true, fvg: true, orderBlocks: true, mitigated: false, labels: true },
  strategy: { pivots: true, internal: false, swing: true, structure: true, liquidity: true, fvg: true, orderBlocks: true, mitigated: false, labels: true },
  trades: { pivots: false, internal: false, swing: false, structure: false, liquidity: false, fvg: false, orderBlocks: false, mitigated: false, labels: true },
  debug: { pivots: true, internal: true, swing: true, structure: true, liquidity: true, fvg: true, orderBlocks: true, mitigated: true, labels: true },
};

const operatingModeLabel = (mode?: string) =>
  mode === "manual_approval" ? "Agent decides" : mode === "automatic" ? "Automatic paper"
    : mode === "signals_only" ? "Signals only" : mode || "—";
const pretty = (value: string) => value.replace(/^SMC_[A-Z0-9]+_?/, "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
const cell = (value: unknown) => value === null || value === undefined || value === "" ? "—" : typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: 6 }) : String(value);
const money = (value?: number) => Number(value ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const stamp = (value?: string | null) => value ? value.replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC").slice(0, 23) : "—";
const age = (value?: number | null) => value == null ? "—" : value < 1 ? "<1s" : `${Math.round(value)}s`;

function mergeCandles(...groups: NativeCandle[][]): NativeCandle[] {
  const rows = new Map<string, NativeCandle>();
  for (const group of groups) for (const candle of group) rows.set(candle.timestamp, candle);
  return [...rows.values()].sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
}

function DataTable({ rows, empty }: { rows: Record<string, unknown>[]; empty: string }) {
  const columns = useMemo(() => {
    const keys = new Set<string>();
    for (const row of rows.slice(0, 12)) for (const key of Object.keys(row)) if (!["payload", "metadata", "fills", "notes", "ordered_conditions", "native_object_ids"].includes(key)) keys.add(key);
    return [...keys].slice(0, 10);
  }, [rows]);
  if (!rows.length) return <div className="pa-empty">{empty}</div>;
  return <div className="pa-table-wrap"><table className="pa-table"><thead><tr>{columns.map((key) => <th key={key}>{pretty(key)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={String(row.id ?? row.order_id ?? row.proposal_id ?? index)}>{columns.map((key) => <td key={key}>{cell(row[key])}</td>)}</tr>)}</tbody></table></div>;
}

function StrategyAnalysis({ evaluation, candidate, onObjectSelect }: {
  evaluation?: SMCSourceStrategyEvaluation | null;
  candidate?: LadderCandidate;
  onObjectSelect: (id: string) => void;
}) {
  if (!evaluation) return <div className="pa-empty">Waiting for SMC strategy evidence.</div>;
  const conditions = evaluation.ordered_condition_results;
  return <div className="pa-governance">
    <div className="pa-learning-warning"><b>{evaluation.model.label}</b><span>{evaluation.state} · {evaluation.next_required_event}</span><span>PAPER ONLY · LIVE EXECUTION DISABLED</span></div>
    <div className="pa-journal-stats"><span>Model<b>{evaluation.model.id}</b></span><span>State<b>{evaluation.state}</b></span><span>Candidate<b>{evaluation.selected_candidate_id ?? "Watching"}</b></span><span>Risk<b>{evaluation.trade_plan ? `${evaluation.trade_plan.risk_percent}%` : "None"}</b></span><span>Missing<b>{evaluation.missing_conditions.length}</b></span><span>Execution<b>DISABLED</b></span></div>
    <div className="pa-journal-grid">
      <div className="pa-table-wrap"><table className="pa-table"><thead><tr><th>Condition</th><th>Status</th><th>Evidence</th></tr></thead><tbody>{conditions.map((condition) => <tr key={condition.key} className={condition.object_id ? "is-selected" : ""} onClick={() => condition.object_id && onObjectSelect(condition.object_id)}><td>{condition.label}</td><td>{condition.status}</td><td>{condition.detail}</td></tr>)}</tbody></table>{!conditions.length ? <div className="pa-empty">No ordered strategy conditions were returned.</div> : null}</div>
      <div className="pa-journal-detail"><h3>Current SMC decision</h3><dl><dt>Version</dt><dd>{evaluation.version}</dd><dt>Proposal</dt><dd>{evaluation.proposal_id ?? "—"}</dd><dt>Setup</dt><dd>{evaluation.setup_id ?? candidate?.selected_trace?.setup_id ?? "—"}</dd><dt>Entry</dt><dd>{evaluation.trade_plan?.entry ?? "—"}</dd><dt>Stop</dt><dd>{evaluation.trade_plan?.stop ?? "—"}</dd><dt>Target 1</dt><dd>{evaluation.trade_plan ? `${evaluation.trade_plan.target_1} · ${evaluation.trade_plan.target_1_r.toFixed(2)}R` : "—"}</dd><dt>Target 2</dt><dd>{evaluation.trade_plan ? `${evaluation.trade_plan.target_2} · ${evaluation.trade_plan.target_2_r.toFixed(2)}R` : "—"}</dd><dt>Missing</dt><dd>{evaluation.missing_conditions.join(", ") || "None"}</dd></dl></div>
    </div>
  </div>;
}

function JournalPanel({ rows, selectedId, onSelect }: { rows: SMCJournalRow[]; selectedId: string; onSelect: (row: SMCJournalRow) => void }) {
  const selected = rows.find((row) => row.journal_id === selectedId) ?? rows[0];
  return <div className="pa-governance">
    <div className="pa-governance-bar"><b>Immutable SMC decision journal</b><button className="pa-export" type="button" onClick={() => void apiDownload("/research/smc/journal/export?format=csv", "smc-journal.csv")}>Export CSV</button></div>
    <div className="pa-journal-grid"><div className="pa-table-wrap"><table className="pa-table"><thead><tr><th>Signal</th><th>Model</th><th>Direction</th><th>Status</th><th>Rules</th><th>Net P&amp;L</th></tr></thead><tbody>{rows.map((row) => <tr key={row.journal_id} className={selected?.journal_id === row.journal_id ? "is-selected" : ""} onClick={() => onSelect(row)}><td>{stamp(row.signal_timestamp ?? row.created_at)}</td><td>{pretty(row.model_id)}</td><td>{row.direction ?? "—"}</td><td>{row.status}</td><td>{row.rule_compliance}</td><td>{row.net_pnl.toFixed(2)}</td></tr>)}</tbody></table>{!rows.length ? <div className="pa-empty">No SMC journal records in this session.</div> : null}</div>
      <div className="pa-journal-detail">{selected ? <><h3>{pretty(selected.model_id)} · {selected.status}</h3><dl><dt>Journal ID</dt><dd>{selected.journal_id}</dd><dt>Session</dt><dd>{selected.session_id}</dd><dt>Proposal</dt><dd>{selected.proposal_id}</dd><dt>Data quality</dt><dd>{selected.data_quality}</dd><dt>Rules</dt><dd>{selected.rule_compliance}</dd><dt>Native objects</dt><dd>{selected.native_object_ids.join(", ") || "—"}</dd><dt>Missing</dt><dd>{selected.missing_conditions.join(", ") || "None"}</dd><dt>Plan</dt><dd>{selected.trade_plan ? `Entry ${selected.trade_plan.entry} · Stop ${selected.trade_plan.stop} · T1 ${selected.trade_plan.target_1} · T2 ${selected.trade_plan.target_2}` : "No executable plan"}</dd></dl><h4>Ordered evidence</h4><ol>{selected.ordered_conditions.map((condition) => <li key={condition.key}><b>{condition.status} · {condition.label}</b>{condition.detail}</li>)}</ol></> : <div className="pa-empty">Select a journal record.</div>}</div>
    </div>
  </div>;
}

export default function SMCStrategyLabPage() {
  const { toast } = useApp();
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [timeframe, setTimeframe] = useState("5m");
  const [chartFeed, setChartFeed] = useState<ChartFeed>("binance_usdm");
  const [selectedModelId, setSelectedModelId] = useState("SMC_M1_SWEEP_REVERSAL");
  const [selectedCandidateId, setSelectedCandidateId] = useState("SMC_S1_PIVOT_REVERSAL");
  const [controlsOpen, setControlsOpen] = useState(false);
  const [chartPreset, setChartPreset] = useState<ChartPreset>("strategy");
  const [filters, setFilters] = useState(PRESETS.strategy);
  const [tab, setTab] = useState<BottomTab>("positions");
  const [visibleBars, setVisibleBars] = useState(72);
  const [fitSignal, setFitSignal] = useState(0);
  const [latestSignal, setLatestSignal] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [selectedCandle, setSelectedCandle] = useState("");
  const [selectedJournalId, setSelectedJournalId] = useState("");
  const [operatingMode, setOperatingMode] = useState("");
  const [riskPct, setRiskPct] = useState("0.5");
  const [leverage, setLeverage] = useState("1");
  const [resetPhrase, setResetPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [marketBusy, setMarketBusy] = useState(false);
  const marketSwitchInFlight = useRef(false);
  const strategySaveInFlight = useRef(false);
  const [order, setOrder] = useState({ side: "buy", type: "market", quantity: "0.001", price: "", stop_loss: "", target_1: "", target_2: "" });
  const [priceViewport, setPriceViewport] = useState<ChartPriceViewport>({ auto: true, scale: 1, offset: 0 });
  const [timeViewport, setTimeViewport] = useState<ChartTimeViewport | null>(null);
  const [autoFollowLatest, setAutoFollowLatest] = useState(true);
  const [olderCandles, setOlderCandles] = useState<NativeCandle[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [historyPrepend, setHistoryPrepend] = useState({ version: 0, count: 0 });
  const historyRequestRef = useRef(false);

  const sourceModels = useLive<SMCSourceModelsResponse>("/research/smc/strategy-models", 600_000);
  const identity = useLive<{ session: SMCPaperState["session"] }>("/research/smc/session", 5_000);
  const paper = useLive<SMCPaperState>("/research/smc/paper", 5_000);
  const savedSession = paper.data?.session.id ? paper.data.session : identity.data?.session;
  const sessionLoaded = Boolean(savedSession?.id && savedSession.operating_mode);
  const journal = useLive<SMCJournalResponse>(`/research/smc/journal?symbol=${symbol}&timeframe=${timeframe}`, 5_000);
  const sessions = useLive<SMCSessionsResponse>("/research/smc/sessions", 15_000);
  const focusedAt = chartFeed === "checkpoint" ? selectedCandle : "";
  const chartPath = chartFeed === "checkpoint"
    ? `/research/smc/chart?symbol=${symbol}&timeframe=${timeframe}&window=800&model_id=${encodeURIComponent(selectedModelId)}${focusedAt ? `&at=${encodeURIComponent(focusedAt)}` : ""}`
    : `/research/smc/live-chart?symbol=${symbol}&timeframe=${timeframe}&venue=binance_usdm&window=800&visible=800&model_id=${encodeURIComponent(selectedModelId)}`;
  const chartIdentityReady = chartFeed === "checkpoint" || (savedSession?.symbol === symbol && savedSession?.timeframe === timeframe);
  const chartState = useLive<NativeState>(sessionLoaded && !marketBusy && chartIdentityReady ? chartPath : null, chartFeed === "checkpoint" ? 5_000 : 2_500);
  const sourceStrategy = useLive<SMCSourceStrategyEvaluation>(chartFeed === "checkpoint" ? `/research/smc/strategy-v1/evaluate?symbol=${symbol}&timeframe=${timeframe}&model_id=${encodeURIComponent(selectedModelId)}${focusedAt ? `&at=${encodeURIComponent(focusedAt)}` : ""}` : null, 5_000);
  const data = chartState.data;
  const chartData = useMemo(() => data ? { ...data, candles: mergeCandles(olderCandles, data.candles) } : null, [data, olderCandles]);
  const candidates = data?.strategy_ladder?.candidates ?? [];
  const selectedCandidate = candidates.find((candidate) => candidate.strategy_id === selectedCandidateId) ?? candidates[0];
  const evaluation = chartFeed === "checkpoint" ? sourceStrategy.data : data?.source_strategy;
  const highlightedObjectIds = useMemo(() => selectedCandidate?.selected_trace?.supporting_object_ids ?? evaluation?.native_object_ids ?? [], [evaluation?.native_object_ids, selectedCandidate]);
  const fillMarkers = useMemo(() => (paper.data?.activity ?? []).flatMap((row) => {
    if (row.kind !== "paper_fill" || !row.payload || typeof row.payload !== "object") return [];
    const payload = row.payload as Record<string, unknown>;
    const timestamp = typeof payload.candle_time === "string" ? payload.candle_time : null;
    const price = Number(payload.price);
    return timestamp && Number.isFinite(price) ? [{ timestamp, price, side: String(payload.side ?? ""), realized_pnl: Number(payload.realized_pnl ?? 0) }] : [];
  }), [paper.data?.activity]);

  useEffect(() => {
    if (!savedSession?.id) return;
    setOperatingMode(savedSession.operating_mode);
    setRiskPct(String(savedSession.risk_pct));
    setSymbol(savedSession.symbol);
    setTimeframe(savedSession.timeframe);
    setSelectedModelId(savedSession.model_id);
    if (paper.data) setLeverage(String(paper.data.account.leverage));
  }, [paper.data?.account.leverage, savedSession?.id, savedSession?.symbol, savedSession?.timeframe, savedSession?.operating_mode, savedSession?.risk_pct, savedSession?.model_id]);

  useEffect(() => {
    historyRequestRef.current = false;
    setOlderCandles([]);
    setHasMoreHistory(true);
    setHistoryLoading(false);
    setTimeViewport(null);
    setHistoryPrepend((current) => ({ version: current.version + 1, count: 0 }));
  }, [symbol, timeframe, chartFeed]);

  const feedReliable = chartFeed === "checkpoint"
    ? Boolean(data?.candles.length)
    : Boolean(data?.live_display?.reliable && !data.live_display.new_entries_paused && !chartState.error);
  const healthState = chartFeed === "checkpoint" ? "FROZEN_REVIEW" : chartState.error ? "ERROR" : data?.live_display?.connection_state ?? "CONNECTING";
  const lastClosed = data?.candles[data.candles.length - 1];
  const rejected = paper.data?.candidates.filter((row) => ["DATA_PAUSED", "REJECTED", "EXPIRED", "CONFLICT"].includes(row.status)) ?? [];

  const applyPreset = (preset: ChartPreset) => {
    setChartPreset(preset);
    setFilters(PRESETS[preset]);
  };
  const toggleLayer = (key: keyof NativeSMCOverlayFilters) => {
    setChartPreset("debug");
    setFilters((current) => ({ ...current, [key]: !current[key] }));
  };
  const switchMarket = async (nextSymbol: string, nextTimeframe = timeframe) => {
    // Never while a strategy save is in flight: this request resends the saved
    // mode/model/risk, and the saved copy it holds would undo that save.
    if (!savedSession || marketSwitchInFlight.current || strategySaveInFlight.current || (nextSymbol === symbol && nextTimeframe === timeframe)) return;
    marketSwitchInFlight.current = true;
    setMarketBusy(true);
    try {
      // Changing a chart must not silently apply draft risk/model/mode edits.
      const updated = await apiPostJson<SMCPaperState>("/research/smc/sessions/current/configuration", {
        symbol: nextSymbol, timeframe: nextTimeframe, mode: "LIVE_PAPER",
        operating_mode: savedSession.operating_mode, model_id: savedSession.model_id,
        risk_pct: savedSession.risk_pct,
      });
      setSymbol(updated.session.symbol);
      setTimeframe(updated.session.timeframe);
      setChartFeed("binance_usdm");
      setSelectedId(""); setSelectedCandle("");
      setLatestSignal((value) => value + 1);
      await Promise.all([paper.refetch(), identity.refetch()]);
      toast(`SMC session changed to ${updated.session.symbol} ${updated.session.timeframe}`, "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "SMC market change failed", "error");
    } finally {
      marketSwitchInFlight.current = false;
      setMarketBusy(false);
    }
  };
  const focusCandidate = () => {
    const objectId = selectedCandidate?.selected_trace?.supporting_object_ids[0] ?? evaluation?.native_object_ids[0];
    if (objectId) setSelectedId(objectId);
    setChartPreset("strategy");
    setFilters(PRESETS.strategy);
    setFitSignal((value) => value + 1);
  };
  const runAction = async (task: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try {
      await task();
      await paper.refetch();
      toast(success, "success");
    } catch (error) {
      toast(error instanceof Error ? error.message : "SMC paper action failed", "error");
    } finally {
      setBusy(false);
    }
  };
  const modelLabel = (id?: string) => sourceModels.data?.models.find((model) => model.id === id)?.label ?? id ?? "—";
  /** Save one strategy change the moment it is made.
   *
   * These controls used to be drafts that only an "Apply" press sent, so a
   * strategy picked without Apply was never saved and the page quietly went
   * back to the saved one. Every field the operator did NOT change is sent
   * as the server holds it: the endpoint fills a missing field with its
   * default, so a partial request would silently reset the mode or risk.
   * A refusal puts every control back to what is actually saved.
   */
  const saveStrategy = async (change: { model_id?: string; operating_mode?: string; risk_pct?: number }, what: string) => {
    if (!savedSession || strategySaveInFlight.current || marketSwitchInFlight.current) return;
    strategySaveInFlight.current = true;
    setBusy(true);
    try {
      const updated = await apiPostJson<SMCPaperState>("/research/smc/sessions/current/configuration", {
        symbol: savedSession.symbol, timeframe: savedSession.timeframe,
        operating_mode: savedSession.operating_mode, model_id: savedSession.model_id,
        risk_pct: savedSession.risk_pct, ...change,
      });
      setSelectedModelId(updated.session.model_id);
      setOperatingMode(updated.session.operating_mode);
      setRiskPct(String(updated.session.risk_pct));
      await Promise.all([paper.refetch(), identity.refetch()]);
      toast(`Saved: ${what}. The bot now trades ${modelLabel(updated.session.model_id)} on ${updated.session.symbol} ${updated.session.timeframe}.`, "success");
    } catch (error) {
      setSelectedModelId(savedSession.model_id);
      setOperatingMode(savedSession.operating_mode);
      setRiskPct(String(savedSession.risk_pct));
      toast(`Not saved: ${error instanceof Error ? error.message : "the server refused the change"}`, "error");
    } finally {
      strategySaveInFlight.current = false;
      setBusy(false);
    }
  };
  const saveRisk = () => {
    const value = Number(riskPct);
    if (!savedSession || !Number.isFinite(value) || value === savedSession.risk_pct) return;
    void saveStrategy({ risk_pct: value }, `risk ${value}% per trade`);
  };
  const applyLeverage = (value: string) => {
    setLeverage(value);
    return runAction(() => apiPostJson("/research/smc/paper/leverage", { leverage: Number(value) }), `SMC paper leverage set to ${value}x`);
  };
  const submitOrder = () => runAction(() => apiPostJson("/research/smc/paper/orders", {
    symbol,
    side: order.side,
    type: order.type,
    quantity: Number(order.quantity),
    limit_price: order.type === "limit" && order.price ? Number(order.price) : null,
    trigger_price: order.type === "stop" && order.price ? Number(order.price) : null,
    stop_loss: order.stop_loss ? Number(order.stop_loss) : null,
    target_1: order.target_1 ? Number(order.target_1) : null,
    target_2: order.target_2 ? Number(order.target_2) : null,
  }, { "Idempotency-Key": `smc-manual-${Date.now()}` }), "SMC paper order accepted");
  const resetPaper = () => runAction(() => apiPostJson("/research/smc/paper/reset", { confirmation: resetPhrase }), "SMC paper account reset");

  const requestOlderHistory = useCallback(async () => {
    if (chartFeed === "checkpoint" || historyRequestRef.current || historyLoading || !hasMoreHistory || !chartData?.candles.length) return;
    historyRequestRef.current = true;
    setHistoryLoading(true);
    try {
      const page = await apiGet<LiveHistoryPage>(`/research/smc/live-history?symbol=${symbol}&timeframe=${timeframe}&venue=binance_usdm&before=${encodeURIComponent(chartData.candles[0].timestamp)}&limit=400`);
      setOlderCandles((current) => mergeCandles(page.candles, current));
      setHasMoreHistory(page.has_more_history && page.candles.length > 0);
      if (page.candles.length) setHistoryPrepend((current) => ({ version: current.version + 1, count: page.candles.length }));
    } catch (error) {
      toast(error instanceof Error ? error.message : "Unable to load older SMC candles", "error");
    } finally {
      historyRequestRef.current = false;
      setHistoryLoading(false);
    }
  }, [chartData?.candles, chartFeed, hasMoreHistory, historyLoading, symbol, timeframe, toast]);

  const onViewportChange = useCallback((range: ChartTimeViewport) => {
    setTimeViewport(range);
    setAutoFollowLatest(range.end >= 99.2);
  }, []);
  const onGoLatest = useCallback(() => {
    setAutoFollowLatest(true);
    setLatestSignal((value) => value + 1);
  }, []);
  const onPriceAxisDrag = useCallback((deltaY: number, verticalPan: boolean) => {
    setPriceViewport((current) => {
      const active = { ...current, auto: false };
      return verticalPan
        ? { ...active, offset: Math.max(-2, Math.min(2, active.offset + deltaY * 0.0025)) }
        : { ...active, scale: Math.max(0.2, Math.min(4, active.scale * Math.exp(deltaY * 0.008))) };
    });
  }, []);

  const bottomContent = tab === "positions" ? <DataTable rows={paper.data?.positions ?? []} empty="No open SMC paper positions." />
    : tab === "orders" ? <><div className="pa-order-ticket"><select aria-label="SMC paper order side" value={order.side} onChange={(event) => setOrder((current) => ({ ...current, side: event.target.value }))}><option value="buy">Buy / Long</option><option value="sell">Sell / Short</option></select><select aria-label="SMC paper order type" value={order.type} onChange={(event) => setOrder((current) => ({ ...current, type: event.target.value }))}><option value="market">Market</option><option value="limit">Limit</option><option value="stop">Stop</option></select><input aria-label="SMC paper order quantity" value={order.quantity} onChange={(event) => setOrder((current) => ({ ...current, quantity: event.target.value }))} placeholder="Quantity" />{order.type !== "market" ? <input aria-label="SMC paper order price" value={order.price} onChange={(event) => setOrder((current) => ({ ...current, price: event.target.value }))} placeholder="Trigger / limit" /> : null}<input aria-label="SMC stop loss" value={order.stop_loss} onChange={(event) => setOrder((current) => ({ ...current, stop_loss: event.target.value }))} placeholder="Stop loss" /><input aria-label="SMC target one" value={order.target_1} onChange={(event) => setOrder((current) => ({ ...current, target_1: event.target.value }))} placeholder="Target 1" /><input aria-label="SMC target two" value={order.target_2} onChange={(event) => setOrder((current) => ({ ...current, target_2: event.target.value }))} placeholder="Target 2" /><button type="button" disabled={busy} onClick={() => void submitOrder()}>Place paper order</button><span>PAPER · {paper.data?.orders.length ?? 0} orders</span></div><DataTable rows={paper.data?.orders ?? []} empty="No pending SMC paper orders." /></>
    : tab === "trades" ? <DataTable rows={paper.data?.trades ?? []} empty="No SMC paper fills in this session." />
    : tab === "setups" ? <DataTable rows={paper.data?.candidates ?? []} empty="No qualified SMC setups in this session." />
    : tab === "rejected" ? <DataTable rows={rejected} empty="No rejected, expired, conflicted or data-paused SMC setups." />
    : tab === "journal" ? <JournalPanel rows={journal.data?.journal ?? []} selectedId={selectedJournalId} onSelect={(row) => { setSelectedJournalId(row.journal_id); if (row.signal_timestamp) setSelectedCandle(row.signal_timestamp); if (row.native_object_ids[0]) setSelectedId(row.native_object_ids[0]); }} />
    : tab === "analysis" ? <StrategyAnalysis evaluation={evaluation} candidate={selectedCandidate} onObjectSelect={setSelectedId} />
    : tab === "session" ? <><div className="pa-session"><span>Session ID<b>{savedSession?.id ?? "—"}</b></span><span>Started<b>{stamp(paper.data?.session.started_at)}</b></span><span>Status<b>{paper.data?.session.status?.toUpperCase() ?? "ACTIVE"}</b></span><span>Operating mode<b>{sessionLoaded ? pretty(savedSession?.operating_mode ?? "") : "Loading"}</b></span><span>Model<b>{paper.data?.session.model_id ?? "—"}</b></span><span>Risk<b>{paper.data?.session.risk_pct ?? 0}%</b></span></div><div className="pa-order-ticket"><input aria-label="SMC reset confirmation" value={resetPhrase} onChange={(event) => setResetPhrase(event.target.value)} placeholder="Type RESET SMC PAPER" /><button className="btn-danger" type="button" disabled={busy || resetPhrase !== "RESET SMC PAPER"} onClick={() => void resetPaper()}>Reset SMC paper</button><span>Previous session evidence is preserved.</span></div><DataTable rows={sessions.data?.sessions ?? []} empty="No archived SMC paper sessions." /></>
    : <div className="pa-session"><span>Exchange<b>{chartFeed === "checkpoint" ? "Verified frozen checkpoint" : "Binance USDⓈ-M Futures"}</b></span><span>Overall health<b>{healthState}</b></span><span>Transport<b>{data?.live_display?.connection_state ?? (chartFeed === "checkpoint" ? "ISOLATED" : "CONNECTING")}</b></span><span>Bid / ask stream<b>{age(data?.live_display?.quote_age_seconds)}</b></span><span>Mark stream<b>{age(data?.live_display?.mark_age_seconds)}</b></span><span>Failing dependency<b>{data?.live_display?.failing_dependency ?? (chartFeed === "checkpoint" ? "None · isolated" : "—")}</b></span><span>Last successful event<b>{data?.live_display?.last_successful_event ? `${data.live_display.last_successful_event.kind} · ${data.live_display.last_successful_event.at}` : "—"}</b></span><span>Retry state<b>{data?.live_display?.retry_state?.automatic_retry ? `automatic · attempt ${data.live_display.retry_state.attempt ?? 0}` : "—"}</b></span><span>Reconciliation<b>{data?.live_display?.health_reason ?? (chartFeed === "checkpoint" ? "Frozen evidence only" : "—")}</b></span><span>New entries<b>{feedReliable && chartFeed !== "checkpoint" ? "CLOSED BARS ONLY" : "PAUSED · FAIL CLOSED"}</b></span><span>Real execution<b>DISABLED</b></span></div>;

  return <div className="pa-lab smc-strategy-lab">
    <header className="pa-titlebar">
      <div><span className="pa-kicker">ISOLATED FORWARD-PAPER</span><h1>SMC Strategy Lab</h1><p>Live Binance USD-M data · simulated orders · no exchange routing</p></div>
      <button type="button" className="pa-controls-toggle" onClick={() => setControlsOpen((open) => !open)} aria-expanded={controlsOpen}>Controls</button>
      <div className="pa-safety"><b>{!sessionLoaded ? "LOADING SESSION" : savedSession?.operating_mode === "signals_only" ? "SIGNALS_ONLY" : "ISOLATED_FORWARD_PAPER"}</b><span>LIVE ROUTING DISABLED</span></div>
    </header>
    <div className={`pa-health-scope ${feedReliable ? "is-healthy" : "is-stale"}`}>
      <b>SMC STRATEGY SESSION</b><span>Candles / quote / mark: {healthState}</span>
      <span>Decision readiness: {feedReliable && chartFeed !== "checkpoint" ? "CLOSED-BAR ELIGIBLE" : "PAUSED · FAIL CLOSED"}</span>
      <span>Paper execution: {feedReliable && chartFeed !== "checkpoint" ? "ELIGIBLE UNDER SAVED MODE" : "BLOCKED"}</span>
      <small>This page and footer report only the isolated SMC account.</small>
    </div>

    <div className="pa-workspace">
      <aside className={`pa-sidebar ${controlsOpen ? "is-open" : ""}`} aria-label="SMC Strategy controls">
        {/* Minimal on purpose: what the bot trades, the three things that
            change it, and nothing that looks like a strategy but is not. */}
        <section><h2>Bot</h2>
          <p className="pa-saved-config" data-testid="smc-saved-configuration">{savedSession ? <><b>{modelLabel(savedSession.model_id)}</b><span>{savedSession.symbol} {savedSession.timeframe} · {operatingModeLabel(savedSession.operating_mode)} · risk {savedSession.risk_pct}%</span></> : "Loading…"}</p>
          <label>Strategy<select aria-label="SMC entry model" disabled={busy || marketBusy || !sessionLoaded} value={selectedModelId} onChange={(event) => { const next = event.target.value; setSelectedModelId(next); void saveStrategy({ model_id: next }, `strategy ${modelLabel(next)}`); }}>{(sourceModels.data?.models ?? []).map((model) => <option key={model.id} value={model.id} disabled={model.status === "PARKED"}>{model.label}{model.status === "PARKED" ? " · not built yet" : ""}</option>)}</select></label>
          <label>Mode<select aria-label="Paper operating mode" disabled={busy || marketBusy || !sessionLoaded} value={operatingMode} onChange={(event) => { const next = event.target.value; setOperatingMode(next); void saveStrategy({ operating_mode: next }, `mode ${operatingModeLabel(next)}`); }}>{!sessionLoaded && <option value="">Loading…</option>}<option value="signals_only">Signals only</option><option value="manual_approval">Agent decides</option><option value="automatic">Automatic paper</option></select></label>
          <label>Risk per trade %<input aria-label="SMC risk per trade" value={riskPct} disabled={busy || marketBusy || !sessionLoaded} onChange={(event) => setRiskPct(event.target.value)} onBlur={saveRisk} onKeyDown={(event) => { if (event.key === "Enter") saveRisk(); }} inputMode="decimal" /></label>
          <small>Changes save instantly.</small>
        </section>
        <LabNewsGuard lab="smc" />
        <section><h2>SMC session market</h2><label>Symbol<select aria-label="SMC session symbol" disabled={marketBusy || busy || !sessionLoaded} value={symbol} onChange={(event) => switchMarket(event.target.value)}>{SYMBOLS.map((row) => <option key={row}>{row}</option>)}</select></label><div className="pa-segment"><button type="button" className={chartFeed === "binance_usdm" ? "active" : ""} onClick={() => { if (savedSession) { setSymbol(savedSession.symbol); setTimeframe(savedSession.timeframe); } setChartFeed("binance_usdm"); }}>Live paper</button><button type="button" className={chartFeed === "checkpoint" ? "active" : ""} onClick={() => { setChartFeed("checkpoint"); setSymbol("BTCUSDT"); setTimeframe("5m"); }}>Frozen review</button></div><small className="pa-context-note"><button type="button" className="link-button" onClick={() => { window.location.hash = "/smc-visual-lab"; }}>SMC Visual Lab</button></small></section>
        <section><h2>Account</h2><div className="pa-account"><span>Balance<b>{money(paper.data?.account.balance)} USDT</b></span><span>Equity<b>{money(paper.data?.account.equity)} USDT</b></span><span>Open P&amp;L<b className={(paper.data?.account.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}>{money(paper.data?.account.unrealized_pnl)}</b></span><span>Free margin<b>{money(paper.data?.account.available_margin)}</b></span></div><label>Leverage<select value={leverage} onChange={(event) => void applyLeverage(event.target.value)}>{[1, 2, 3, 5, 10].map((value) => <option key={value} value={value}>{value}×</option>)}</select></label></section>
        <section><details className="pa-details"><summary>Chart display</summary><label className="pa-layer-mode">Preset<select aria-label="SMC chart layer preset" value={chartPreset} onChange={(event) => applyPreset(event.target.value as ChartPreset)}>{(Object.keys(PRESETS) as ChartPreset[]).map((preset) => <option key={preset} value={preset}>{pretty(preset)}</option>)}</select></label>{([[
          "pivots", "Swings"], ["structure", "Events"], ["liquidity", "Liquidity"], ["fvg", "Fair value gaps"], ["orderBlocks", "Order blocks"], ["mitigated", "Invalidated · lifecycle"], ["labels", "Labels"]] as [keyof NativeSMCOverlayFilters, string][]).map(([key, label]) => <label className="pa-check" key={key}><input type="checkbox" checked={filters[key]} onChange={() => toggleLayer(key)} /><span>{label}</span></label>)}<label className="pa-layer-mode">Highlight a setup · chart only<select aria-label="Selected SMC setup" value={selectedCandidate?.strategy_id ?? selectedCandidateId} onChange={(event) => setSelectedCandidateId(event.target.value)}>{candidates.map((candidate) => <option key={candidate.strategy_id} value={candidate.strategy_id}>{pretty(candidate.strategy_id)} · {candidate.state}</option>)}</select></label><button type="button" className="pa-focus-setup" disabled={!selectedCandidate && !evaluation} onClick={focusCandidate}>Show on chart</button><div className="pa-legend"><span><i className="confirmed" />Confirmed</span><span><i className="provisional" />Forming · display only</span><span><i className="invalid" />Invalidated</span></div></details></section>
      </aside>

      <main className="pa-main">
        <div className="pa-toolbar"><div className="pa-symbol"><i className={feedReliable ? "live" : "stale"} />{symbol}<span>SMC SESSION · PERPETUAL</span></div><div className="pa-timeframes" role="group" aria-label="Bot trading timeframe" title="These buttons change the timeframe the bot TRADES, and save it">{(data?.mtf_policy?.available_entry_timeframes ?? ["1m", "5m", "15m", "1h", "4h"]).map((row) => <button type="button" key={row} disabled={marketBusy || busy || !sessionLoaded} className={row === timeframe ? "active" : ""} onClick={() => switchMarket(symbol, row)}>{row}</button>)}</div><label className="pa-view-bars">View<select aria-label="Visible SMC chart candles" value={visibleBars} onChange={(event) => { setVisibleBars(Number(event.target.value)); setTimeViewport(null); setFitSignal((value) => value + 1); }}>{[48, 72, 120, 240].map((value) => <option key={value} value={value}>{value} bars</option>)}</select></label><button type="button" onClick={() => { setTimeViewport(null); setPriceViewport({ auto: true, scale: 1, offset: 0 }); setFitSignal((value) => value + 1); }}>Fit</button><button type="button" onClick={onGoLatest}>Latest</button><button type="button" className="pa-clean-view" onClick={() => applyPreset("clean")}>Clean view</button><span className={`pa-mode-chip ${feedReliable ? "" : "is-stale"}`}>{healthState}</span></div>

        <div className="pa-chart-shell" aria-label="Native SMC chart workspace">
          <div className="pa-chart-head"><div><b>{symbol} · {timeframe}</b><span>{data?.mtf_policy?.label ?? "Native MTF context loading"}</span><span>{data?.data_provenance?.venue ?? "Binance USDⓈ-M Futures"} · session {savedSession?.id?.slice(0, 8) ?? "loading"}</span></div><div><span>{evaluation ? `${evaluation.model.label} · ${evaluation.state} · ${evaluation.next_required_event}` : "SMC strategy evidence loading"}</span><span>{data?.snapshot?.swing_bias === 1 ? "BULLISH" : data?.snapshot?.swing_bias === -1 ? "BEARISH" : "NEUTRAL"}</span><b>{evaluation?.state === "ENTRY_READY" ? "READY" : "WAIT"}</b></div></div>
          <div className="pa-metric-scope"><b>Selected SMC model shown above</b><span>Candidate {evaluation?.selected_candidate_id ?? selectedCandidate?.strategy_id ?? "watching"}</span><span>{evaluation?.missing_conditions.length ?? 0} missing conditions</span><span>Version {evaluation?.version ?? "—"} · execution PAPER ONLY</span></div>
          {chartState.error ? <div className="pa-error"><b>Market data unavailable</b><span>{chartState.error}</span><button type="button" onClick={() => void chartState.refetch()}>Retry</button></div> : null}
          {!chartData ? <div className="pa-loading">Loading and reconciling Binance market streams…</div> : <NativeSMCChartOverlay state={chartData} timeframe={timeframe} rightOffsetBars={8} initialVisibleBars={visibleBars} filters={filters} selectedObjectId={selectedId || undefined} highlightedObjectIds={highlightedObjectIds} onCandleSelect={setSelectedCandle} fitContentSignal={fitSignal} latestSignal={latestSignal} centerTimestamp={selectedCandle || undefined} priceViewport={priceViewport} viewport={timeViewport} onViewportChange={onViewportChange} onHistoryNearStart={requestOlderHistory} historyLoading={historyLoading} hasMoreHistory={hasMoreHistory} historicalMode={!autoFollowLatest} onGoLive={onGoLatest} prependedHistory={historyPrepend} onPriceAxisDrag={onPriceAxisDrag} onResetPriceScale={() => setPriceViewport({ auto: true, scale: 1, offset: 0 })} liveDataStale={!feedReliable} tradePlan={evaluation?.trade_plan ?? null} fillMarkers={fillMarkers} modelLabel="native SMC strategy" height="clamp(520px, 58vh, 680px)" />}
          <div className={`pa-stream-truth ${feedReliable ? "is-healthy" : "is-stale"}`}><b>{healthState}</b><span>{chartFeed === "checkpoint" ? "Frozen checkpoint is isolated from live paper execution" : data?.live_display?.health_reason ?? "Waiting for reconciled Binance candles, quote and mark"}</span><span>Transport {data?.live_display?.connection_state ?? (chartFeed === "checkpoint" ? "ISOLATED" : "CONNECTING")} · entries {feedReliable && chartFeed !== "checkpoint" ? "ELIGIBLE ON CLOSED BARS" : "PAUSED"}</span></div>
          <div className="pa-market-readout"><span>Last completed candle<b>{lastClosed ? `${stamp(lastClosed.timestamp)} · C ${lastClosed.close.toLocaleString()}` : "—"}</b><small>{data?.data_provenance?.closed_candles_loaded ?? data?.candles.length ?? 0} closed candles loaded</small></span><span>Forming candle · display only<b>{data?.forming_candle ? `${stamp(data.forming_candle.timestamp)} · O ${data.forming_candle.open.toLocaleString()} H ${data.forming_candle.high.toLocaleString()} L ${data.forming_candle.low.toLocaleString()} C ${data.forming_candle.close.toLocaleString()}` : "Not available"}</b><small>Excluded from SMC decisions: {data?.forming_candle ? "YES" : "N/A"}</small></span><span>Live bid / ask<b>{data?.live_display?.bid?.toLocaleString() ?? "—"} / {data?.live_display?.ask?.toLocaleString() ?? "—"}</b><small>Age {age(data?.live_display?.quote_age_seconds)}</small></span><span>Mark price<b>{data?.live_display?.mark?.toLocaleString() ?? "—"}</b><small>Age {age(data?.live_display?.mark_age_seconds)} · deviation {data?.live_display?.candle_quote_deviation_bps?.toFixed(2) ?? "—"} bps</small></span></div>
          <div className="pa-chart-foot"><span><i className={feedReliable ? "live" : "stale"} />{chartFeed === "checkpoint" ? "Verified frozen checkpoint" : `Binance · ${healthState}`}</span><span>Updated {stamp(data?.live_display?.observed_at ?? data?.data_provenance?.observed_at)}</span><span>Quote source {data?.live_display?.quote_source ?? "—"}</span><span>Closed candles used: {String(data?.data_provenance?.closed_candles_visible ?? data?.candles.length ?? 0)}</span><span>Forming candle excluded from strategy: {data?.forming_candle ? "YES" : "N/A"}</span><b>PAPER · NO LIVE EXECUTION PATH</b></div>
        </div>

        <div className="pa-bottom">
          <nav>{TABS.map((row) => <button type="button" key={row} className={tab === row ? "active" : ""} onClick={() => setTab(row)}>{row}<em>{row === "positions" ? paper.data?.positions.length ?? 0 : row === "orders" ? paper.data?.orders.length ?? 0 : row === "trades" ? paper.data?.trades.length ?? 0 : row === "setups" ? paper.data?.candidates.length ?? 0 : row === "rejected" ? rejected.length : row === "journal" ? journal.data?.journal.length ?? 0 : ""}</em></button>)}</nav>
          <div className={`pa-bottom-body ${tab === "journal" || tab === "analysis" ? "is-governance" : ""}`}>{bottomContent}</div>
        </div>
        <ResearchComparisonPanel engine="SMC" />
      </main>
    </div>
  </div>;
}
