import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../app-context";
import { apiPostJson, useLive } from "../lib/api";
import NativeSMCChartOverlay, {
  type NativeCandle, type NativeSMCChartState, type NativeSMCOverlayFilters,
  type SMCFillOverlay, type SMCTradePlanOverlay,
} from "../components/chart/NativeSMCChartOverlay";

/**
 * Adaptive MTF Trend Pullback Lab -- laid out like the SMC Strategy Lab.
 *
 * It shows one of two sources, chosen at the top of the sidebar:
 * - the lab's own private paper bot (services/adaptive_lab.py), which this
 *   page configures; or
 * - a Trading Instance running the same strategy, mirrored VIEW ONLY: its own
 *   feed, orders, trades and journal. The page never writes to an instance;
 *   it is started, stopped and configured from Trading Instances.
 *
 * The chart is the SMC lab's own component, fed with the source's own Binance
 * hub feed: closed candles, the forming candle (display only) and
 * bid/ask/mark. This page computes no trading feature.
 */

interface Gate { id: string; label: string; detail: string; state: string; explanation: string }
interface BotStatus {
  id: string; symbol: string; state: string; market_status?: string;
  strategy_status_reason?: string; last_decision?: Record<string, any> | null;
  metrics?: Record<string, any>; current_realized_equity?: number;
}
interface Source { id: string; kind: "lab" | "instance"; symbol: string; timeframe: string; running: boolean; label: string }
interface View {
  source: string; kind: "lab" | "instance"; bot: BotStatus | null; bot_id: string | null;
  symbol: string; timeframe: string; running: boolean; armed: boolean; state_label: string;
  risk_pct: number; capital_allocation: number; controlled_from: string;
}
interface LabStatus {
  strategy: { key: string; label: string; version: string; timeframe: string };
  symbol: string; mode: string; risk_pct: number; max_risk_pct: number;
  modes: { id: string; label: string }[];
  supported_symbols?: string[];
  bot: BotStatus | null; bot_id?: string;
  sources?: Source[]; view?: View;
}
function isLabStatus(value: unknown): value is LabStatus {
  const v = value as Partial<LabStatus> | null;
  return !!v && typeof v.strategy === "object" && !!v.strategy && typeof v.mode === "string";
}
interface Paper {
  positions: Record<string, any>[]; trades: Record<string, any>[];
  orders: Record<string, Record<string, any>>; logs: Record<string, any>[];
}
interface LiveChart {
  candles: NativeCandle[]; forming_candle: NativeCandle | null;
  live_display: NonNullable<NativeSMCChartState["live_display"]> & { connection_state?: string; health_reason?: string };
  data_provenance: { last_closed_candle: string | null; closed_candles_loaded: number };
  trade_plan: SMCTradePlanOverlay | null; fills: SMCFillOverlay[];
}
interface JournalEntry {
  id: number | string; candle_time: string; engine_decision: string | null; price: number | null;
  strategy_state: string | null; strategy_decision: string | null; direction: string | null;
  reason: string | null; quality: number | null; rr: number | null;
  entry: number | null; stop: number | null; target: number | null; engine_reasons: string[];
  evidence?: "strategy_journal" | "engine_report";
}

const SYMBOLS = ["XRPUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "BNBUSDT", "DOGEUSDT", "AVAXUSDT"];
const TABS = ["positions", "orders", "trades", "journal", "log"] as const;
type Tab = typeof TABS[number];
const MARK: Record<string, string> = { PASS: "✓", FAIL: "✗", WAITING: "…", NOT_APPLICABLE: "–" };
const SOURCE_KEY = "adaptive-lab-source";
/** The SMC chart's own layers are SMC objects; this strategy publishes none. */
const NO_SMC_LAYERS: NativeSMCOverlayFilters = {
  pivots: false, internal: false, swing: false, structure: false, liquidity: false,
  fvg: false, orderBlocks: false, mitigated: false, labels: false,
};
const money = (value: unknown) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—";
};
const price = (value: unknown) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 6 }) : "—";
};
const stamp = (value?: string | null) =>
  value ? value.replace("T", " ").replace("+00:00", " UTC").slice(0, 19) : "—";
const savedSource = () => { try { return window.localStorage.getItem(SOURCE_KEY) ?? ""; } catch { return ""; } };
const rememberSource = (id: string) => { try { window.localStorage.setItem(SOURCE_KEY, id); } catch { /* per-viewer convenience only */ } };

export default function AdaptiveLab() {
  const { toast } = useApp();
  const [picked, setPicked] = useState<string>(savedSource);
  const [sources, setSources] = useState<Source[]>([]);
  // Until the viewer picks, show the Trading Instance when one runs this
  // strategy (what it does is what the lab is for), else the lab's own bot.
  const effective = sources.some((row) => row.id === picked) ? picked
    : sources.find((row) => row.kind === "instance")?.id ?? "lab";
  const q = `source=${encodeURIComponent(effective)}`;
  const status = useLive<LabStatus>(`/research/adaptive-lab/status?${q}`, 5_000);
  useEffect(() => {
    const next = status.data?.sources;
    if (next) setSources((previous) => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
  }, [status.data?.sources]);

  // A status reply without the strategy block (an older hub, an error body)
  // is treated as not loaded rather than dereferenced into a crash.
  const lab = isLabStatus(status.data) ? status.data : null;
  const labUnreadable = Boolean(status.data) && !lab;
  const view = lab?.view ?? null;
  const isInstance = view?.kind === "instance";
  const bot = view ? view.bot : lab?.bot ?? null;
  const hasBot = Boolean(bot);
  const timeframe = view?.timeframe ?? lab?.strategy.timeframe ?? "5m";
  // Off means no worker and therefore no feed: nothing to poll, and nothing
  // to call an error. The chart only ever draws the running source's own feed.
  const isOff = isInstance ? !view?.running : lab?.mode === "off";
  const paper = useLive<Paper>(hasBot ? `/research/adaptive-lab/paper?${q}` : null, 5_000);
  const state = useLive<{ gates: Gate[]; required_next: string | null }>(
    hasBot ? `/research/adaptive-lab/state?${q}` : null, 5_000);
  const live = useLive<LiveChart>(hasBot && !isOff ? `/research/adaptive-lab/live-chart?window=400&${q}` : null, 2_500);
  const journal = useLive<{ entries: JournalEntry[]; state_counts: Record<string, number> }>(
    hasBot ? `/research/adaptive-lab/journal?limit=300&${q}` : null, 10_000);
  const [tab, setTab] = useState<Tab>("positions");
  const [busy, setBusy] = useState(false);
  const [riskDraft, setRiskDraft] = useState<string | null>(null);
  const [visibleBars, setVisibleBars] = useState(96);
  const [fitSignal, setFitSignal] = useState(0);
  const [latestSignal, setLatestSignal] = useState(0);
  const saving = useRef(false);

  const risk = riskDraft ?? String(lab?.risk_pct ?? "");
  const modeLabel = (id?: string) => lab?.modes.find((row) => row.id === id)?.label ?? id ?? "—";
  const orders = useMemo(() => Object.entries(paper.data?.orders ?? {})
    .flatMap(([kind, rows]) => Object.entries(rows ?? {}).map(([id, row]) => ({ id, kind, ...(row as object) }))),
  [paper.data?.orders]);
  const feed = live.data?.live_display;
  const reliable = Boolean(feed?.reliable) && !live.error;
  const armed = view ? view.armed : lab?.mode === "automatic";
  const health = isOff ? (isInstance ? "INSTANCE STOPPED" : "BOT OFF") : live.error ? "ERROR" : feed?.connection_state ?? "CONNECTING";
  const chartState = useMemo<NativeSMCChartState | null>(() => live.data?.candles.length ? {
    research_id: "adaptive-mtf-lab", execution_allowed: false, candles: live.data.candles,
    pivots: [], events: [], fair_value_gaps: [], order_blocks: [], proposals: [],
    snapshot: null, selected_snapshot: null, snapshot_ledger: [],
    forming_candle: live.data.forming_candle, live_display: live.data.live_display,
  } : null, [live.data]);
  const lastClosed = live.data?.candles[live.data.candles.length - 1];
  const forming = live.data?.forming_candle;

  const pickSource = (id: string) => { setPicked(id); rememberSource(id); setTab("positions"); };

  /** One change to the LAB bot, saved at once; the server's answer is what the page shows. */
  const save = async (change: { symbol?: string; mode?: string; risk_pct?: number }, what: string) => {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    try {
      const next = await apiPostJson<LabStatus>("/research/adaptive-lab/configuration", change);
      setRiskDraft(null);
      await Promise.all([status.refetch(), paper.refetch(), state.refetch(), live.refetch()]);
      toast(`Saved: ${what}. The bot is ${modeLabel(next.mode).toLowerCase()} on ${next.symbol}.`, "success");
    } catch (error) {
      setRiskDraft(null);
      toast(`Not saved: ${error instanceof Error ? error.message : "the server refused the change"}`, "error");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };
  const saveRisk = () => {
    const value = Number(risk);
    if (riskDraft === null || !Number.isFinite(value) || value === lab?.risk_pct) { setRiskDraft(null); return; }
    void save({ risk_pct: value }, `risk ${value}% per trade`);
  };

  const decision = bot?.last_decision ?? null;
  const waiting = state.data?.required_next || decision?.reason || bot?.strategy_status_reason || "—";
  const metrics = bot?.metrics ?? {};
  const bottom = (() => {
    if (!bot) return <div className="pa-empty">Choose a mode to start the bot.</div>;
    if (tab === "positions") return paper.data?.positions.length
      ? <table className="pa-table"><thead><tr><th>Side</th><th>Size</th><th>Entry</th><th>Stop</th><th>Target</th><th>Opened</th></tr></thead>
          <tbody>{paper.data.positions.map((row) => <tr key={row.id}><td>{row.side}</td><td>{row.size}</td><td>{price(row.entry)}</td><td>{price(row.stop)}</td><td>{price(row.target)}</td><td>{stamp(row.opened_at)}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No open paper positions.</div>;
    if (tab === "orders") return orders.length
      ? <table className="pa-table"><thead><tr><th>Kind</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>Id</th></tr></thead>
          <tbody>{orders.map((row: any) => <tr key={row.id}><td>{row.kind.replace(/_/g, " ")}</td><td>{row.side ?? row.direction ?? "—"}</td><td>{price(row.entry ?? row.limit_price)}</td><td>{price(row.stop ?? row.stop_loss)}</td><td>{price(row.target ?? row.take_profit)}</td><td>{row.id.slice(0, 10)}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No working paper orders.</div>;
    if (tab === "trades") return paper.data?.trades.length
      ? <table className="pa-table"><thead><tr><th>Opened</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Status</th></tr></thead>
          <tbody>{paper.data.trades.map((row) => <tr key={row.id}><td>{stamp(row.opened_at)}</td><td>{row.side}</td><td>{price(row.entry)}</td><td>{price(row.exit)}</td><td>{money(row.realized_pnl ?? row.pnl)}</td><td>{row.status ?? "—"}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No paper trades yet.</div>;
    if (tab === "journal") return journal.data?.entries.length
      ? <div className="pa-table-wrap"><div className="adaptive-journal-head"><b>Decision journal · every closed candle · append-only</b>{Object.entries(journal.data.state_counts).map(([key, count]) => <span key={key}>{key.replace(/_/g, " ")} {count}</span>)}</div>
          <table className="pa-table" data-testid="adaptive-journal"><thead><tr><th>Candle</th><th>Close</th><th>Decision</th><th>Strategy state</th><th>Side</th><th>Why</th><th>Quality</th><th>R:R</th></tr></thead>
          <tbody>{journal.data.entries.map((row) => <tr key={row.id} className={row.engine_decision === "BUY" || row.engine_decision === "SELL" ? "is-signal" : ""}><td>{stamp(row.candle_time)}</td><td>{price(row.price)}</td><td>{row.strategy_decision ?? row.engine_decision ?? "—"}</td><td>{row.evidence === "engine_report" ? <em className="adaptive-evidence" title="Before the lab journal: the instance's own report for this candle, which holds no strategy state">engine report</em> : (row.strategy_state ?? "—").replace(/_/g, " ")}</td><td>{row.direction ?? "—"}</td><td>{row.reason ?? row.engine_reasons[0] ?? "—"}</td><td>{row.quality ? row.quality.toFixed(0) : "—"}</td><td>{row.rr ? row.rr.toFixed(2) : "—"}</td></tr>)}</tbody></table></div>
      : <div className="pa-empty">No closed candle judged yet. The journal fills one row per closed {timeframe} candle.</div>;
    return paper.data?.logs.length
      ? <table className="pa-table"><tbody>{paper.data.logs.map((row, i) => <tr key={row.id ?? i}><td>{stamp(row.ts)}</td><td>{row.level}</td><td>{row.message}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No log entries yet.</div>;
  })();

  const offPanel = isInstance
    ? <div className="pa-loading adaptive-off" data-testid="adaptive-off">
        <b>The Trading Instance is stopped</b>
        <span>A stopped instance has no live feed, so there are no live candles, forming candle or bid/ask to draw. Start it from Trading Instances; this lab only mirrors it. Its journal and history stay below.</span></div>
    : <div className="pa-loading adaptive-off" data-testid="adaptive-off">
        <b>The bot is off</b>
        <span>An off bot has no live feed, so there are no live candles, forming candle or bid/ask to draw. Its journal and history stay below.</span>
        <span className="adaptive-off-actions">
          <button type="button" disabled={busy} onClick={() => void save({ mode: "automatic" }, "mode Automatic paper")}>Turn on · Automatic paper</button>
          <button type="button" disabled={busy} onClick={() => void save({ mode: "signals_only" }, "mode Signals only")}>Turn on · Signals only</button>
        </span></div>;

  return <div className="pa-lab adaptive-lab">
    <header className="pa-titlebar">
      <div><span className="pa-kicker">{isInstance ? "TRADING INSTANCE · VIEW ONLY" : "ISOLATED FORWARD-PAPER"}</span><h1>Adaptive MTF Lab</h1>
        <p>{lab?.strategy.label ?? "Adaptive MTF Trend Pullback"} {lab?.strategy.version ?? ""} · live Binance USD-M data · {isInstance ? "mirroring a Trading Instance" : "its own paper account"} · no exchange routing</p></div>
      <div className="pa-safety"><b>{labUnreadable ? "UNAVAILABLE" : !lab ? "LOADING" : isInstance ? `MIRROR · ${view?.state_label.toUpperCase()}` : modeLabel(lab.mode).toUpperCase()}</b><span>LIVE ROUTING DISABLED</span></div>
    </header>
    {labUnreadable && <p className="pa-note" role="status">This hub did not return the lab&rsquo;s status, so its settings and bot cannot be shown here.</p>}
    <div className={`pa-health-scope ${reliable ? "is-healthy" : isOff ? "is-off" : "is-stale"}`}>
      <b>{isInstance ? "TRADING INSTANCE (MIRROR)" : "ADAPTIVE MTF BOT"}</b><span>Candles / quote / mark: {health}</span>
      <span>Decision readiness: {reliable ? "CLOSED-BAR ELIGIBLE" : isOff ? "NOT RUNNING" : "PAUSED · FAIL CLOSED"}</span>
      <span>Paper execution: {reliable && armed ? "ELIGIBLE" : "BLOCKED"}</span>
    </div>

    <div className="pa-workspace">
      <aside className="pa-sidebar" aria-label="Adaptive MTF Lab controls">
        {sources.length > 1 ? <section><h2>Show</h2>
          <label>Source<select aria-label="Adaptive lab source" value={effective} onChange={(event) => pickSource(event.target.value)}>{sources.map((row) => <option key={row.id} value={row.id}>{row.label}</option>)}</select></label>
          <small>{isInstance ? "What the Trading Instance does, shown here: its chart, orders, trades and journal." : "The lab's own private paper bot."}</small>
        </section> : null}
        {isInstance ? <section><h2>Trading Instance</h2>
          <p className="pa-saved-config" data-testid="adaptive-mirror">{view ? <><b>{lab?.strategy.label} · view only</b><span>{view.symbol} {view.timeframe} · {view.state_label} · risk {view.risk_pct}% · allocation {money(view.capital_allocation)} USDT</span></> : "Loading…"}</p>
          <small>Start, stop and settings live in Trading Instances. This lab never changes the instance; it shows what it does.</small>
        </section> : <>
          <section><h2>Bot</h2>
            <p className="pa-saved-config" data-testid="adaptive-saved-configuration">{lab ? <><b>{lab.strategy.label}</b><span>{lab.symbol} {lab.strategy.timeframe} · {modeLabel(lab.mode)} · risk {lab.risk_pct}%</span></> : "Loading…"}</p>
            <label>Mode<select aria-label="Adaptive lab mode" disabled={busy || !lab} value={lab?.mode ?? ""} onChange={(event) => void save({ mode: event.target.value }, `mode ${modeLabel(event.target.value)}`)}>{(lab?.modes ?? []).map((row) => <option key={row.id} value={row.id}>{row.label}</option>)}</select></label>
            <label>Risk per trade %<input aria-label="Adaptive lab risk per trade" disabled={busy || !lab} value={risk} inputMode="decimal" onChange={(event) => setRiskDraft(event.target.value)} onBlur={saveRisk} onKeyDown={(event) => { if (event.key === "Enter") saveRisk(); }} /></label>
            <small>Changes save instantly. Signals only: the bot judges every candle but places no order.</small>
          </section>
          <section><h2>Market</h2>
            <label>Symbol<select aria-label="Adaptive lab symbol" disabled={busy || !lab} value={lab?.symbol ?? ""} onChange={(event) => void save({ symbol: event.target.value }, `symbol ${event.target.value}`)}>{(lab?.supported_symbols?.length ? lab.supported_symbols : SYMBOLS).map((row) => <option key={row}>{row}</option>)}</select></label>
            <small>Each symbol keeps its own bot and paper account.</small>
          </section>
        </>}
        <section><h2>Account</h2>
          <div className="pa-account">
            <span>Balance<b>{money(metrics.balance ?? bot?.current_realized_equity)} USDT</b></span>
            <span>Realized P&amp;L<b className={Number(metrics.realized_pnl ?? 0) >= 0 ? "positive" : "negative"}>{money(metrics.realized_pnl ?? 0)}</b></span>
            <span>Trades<b>{metrics.trades ?? paper.data?.trades.length ?? 0}</b></span>
            <span>Win rate<b>{metrics.trades ? `${Number(metrics.win_rate ?? 0).toFixed(0)}%` : "—"}</b></span>
          </div>
        </section>
      </aside>

      <main className="pa-main">
        <div className="pa-toolbar"><div className="pa-symbol"><i className={reliable ? "live" : "stale"} />{view?.symbol ?? lab?.symbol ?? "—"}<span>{isInstance ? "TRADING INSTANCE" : "ADAPTIVE BOT"} · PERPETUAL · {timeframe}</span></div>
          <label className="pa-view-bars">View<select aria-label="Visible adaptive chart candles" value={visibleBars} onChange={(event) => setVisibleBars(Number(event.target.value))}>{[48, 96, 160, 240].map((row) => <option key={row} value={row}>{row} bars</option>)}</select></label>
          <button type="button" onClick={() => setFitSignal((value) => value + 1)}>Fit</button>
          <button type="button" onClick={() => setLatestSignal((value) => value + 1)}>Latest</button>
          <span className={`pa-feed-badge ${reliable ? "is-live" : isOff ? "is-off" : "is-stale"}`}>{health}</span></div>
        <div className="pa-chart-shell" aria-label="Adaptive MTF chart workspace">
          <div className="pa-chart-head"><div><b>{view?.symbol ?? lab?.symbol ?? "—"} · {timeframe}</b><span>1h regime · 15m pullback · 5m confirmation</span><span>Binance USDⓈ-M Futures · {isInstance ? "instance" : "bot"} {(view?.bot_id ?? lab?.bot_id)?.slice(0, 8) ?? "—"}</span></div>
            <div><span>{decision?.decision ?? "—"}</span><b>{decision?.state === "ORDER_PENDING" || paper.data?.positions.length ? "IN PLAY" : "WAIT"}</b></div></div>
          <div className="pa-metric-scope" data-testid="adaptive-waiting"><b>Waiting for</b><span>{waiting}</span>
            {state.data?.gates?.length ? <span className="adaptive-gates">{state.data.gates.map((gate) => <em key={gate.id} title={gate.explanation || gate.detail} className={`gate-${gate.state.toLowerCase()}`}>{MARK[gate.state] ?? "·"} {gate.label}</em>)}</span> : null}</div>
          {live.error && !isOff ? <div className="pa-error"><b>Live feed unavailable</b><span>{live.error}</span><button type="button" onClick={() => void live.refetch()}>Retry</button></div> : null}
          {!bot ? <div className="pa-loading">No bot yet — choose a mode to start one.</div>
            : isOff ? offPanel
            : !chartState ? <div className="pa-loading">Loading the {isInstance ? "instance" : "bot"}&rsquo;s Binance candles, quote and mark…</div>
            : <NativeSMCChartOverlay state={chartState} timeframe={timeframe} rightOffsetBars={8}
                initialVisibleBars={visibleBars} filters={NO_SMC_LAYERS} onCandleSelect={() => undefined}
                fitContentSignal={fitSignal} latestSignal={latestSignal}
                modelLabel="adaptive MTF trend pullback" liveDataStale={!reliable}
                tradePlan={live.data?.trade_plan ?? undefined} fillMarkers={live.data?.fills ?? []}
                height="clamp(480px, 56vh, 660px)" />}
          <div className={`pa-stream-truth ${reliable ? "is-healthy" : isOff ? "is-off" : "is-stale"}`}><b>{health}</b><span>{isOff ? `The ${isInstance ? "instance" : "bot"} is not running; it subscribes to no market data until it is started` : feed?.health_reason ?? "Waiting for the reconciled Binance candles, quote and mark"}</span><span>Entries {reliable && armed ? "ELIGIBLE ON CLOSED BARS" : isOff ? "OFF" : "PAUSED"}</span></div>
          <div className="pa-market-readout">
            <span>Last completed candle<b>{lastClosed ? `${stamp(lastClosed.timestamp)} · C ${price(lastClosed.close)}` : "—"}</b><small>{live.data?.data_provenance.closed_candles_loaded ?? 0} closed candles loaded</small></span>
            <span>Forming candle · display only<b>{forming ? `${stamp(forming.timestamp)} · O ${price(forming.open)} H ${price(forming.high)} L ${price(forming.low)} C ${price(forming.close)}` : "Not available"}</b><small>Excluded from decisions: {forming ? "YES" : "N/A"}</small></span>
            <span>Live bid / ask<b>{price(feed?.bid)} / {price(feed?.ask)}</b><small>Binance public websocket</small></span>
            <span>Mark price<b>{price(feed?.mark)}</b><small>paper fills use the post-decision quote</small></span>
          </div>
          <div className="pa-chart-foot"><span><i className={reliable ? "live" : "stale"} />Binance · {health}</span><span>Updated {stamp(feed?.observed_at)}</span><span>Closed candles used: {live.data?.candles.length ?? 0}</span><span>Forming candle excluded from strategy: {forming ? "YES" : "N/A"}</span><b>PAPER · NO LIVE EXECUTION PATH</b></div>
        </div>
        <div className="pa-bottom">
          <nav>{TABS.map((row) => <button type="button" key={row} className={tab === row ? "active" : ""} onClick={() => setTab(row)}>{row}<em>{row === "positions" ? paper.data?.positions.length ?? 0 : row === "orders" ? orders.length : row === "trades" ? paper.data?.trades.length ?? 0 : row === "journal" ? journal.data?.entries.length ?? 0 : ""}</em></button>)}</nav>
          <div className={`pa-bottom-body ${tab === "journal" ? "is-governance" : ""}`}>{bottom}</div>
        </div>
      </main>
    </div>
  </div>;
}
