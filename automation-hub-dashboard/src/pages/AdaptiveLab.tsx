import { useMemo, useRef, useState } from "react";
import { useApp } from "../app-context";
import { apiPostJson, useLive } from "../lib/api";
import {
  Chart, TOGGLE_GROUP,
  type Candle, type Gate, type Overlay, type Position, type TimelineEvent,
} from "./InstanceVisualLab";

/**
 * Adaptive MTF Trend Pullback Lab -- one private paper bot, laid out like the
 * SMC Strategy Lab.
 *
 * The bot is the unmodified strategy on the same engine, fills and safety
 * checks as a Trading Instance, but with its own paper account and database
 * (services/adaptive_lab.py). The chart is the Instance Visual Lab's own
 * component, fed by the same payload functions, so there is still exactly one
 * chart implementation and this page computes no trading feature.
 */

interface BotStatus {
  id: string; symbol: string; state: string; ui_status?: string;
  market_status?: string; market_status_reason?: string;
  strategy_status_reason?: string; current_blocker?: string | null;
  last_decision?: Record<string, any> | null;
  metrics?: Record<string, any>; current_realized_equity?: number;
  starting_equity?: number;
}
interface LabStatus {
  strategy: { key: string; label: string; version: string; timeframe: string };
  symbol: string; mode: string; risk_pct: number; max_risk_pct: number;
  modes: { id: string; label: string }[];
  bots: { symbol: string; id: string; mode: string }[];
  supported_symbols?: string[];
  bot: BotStatus | null; bot_id?: string;
}
interface Paper {
  positions: Record<string, any>[]; trades: Record<string, any>[];
  orders: Record<string, Record<string, any>>; logs: Record<string, any>[];
}
interface LabState {
  gates: Gate[]; required_next: string | null; blocker: string | null;
  blocker_explanation: string; decision_state: string; position: Position | null;
}

const SYMBOLS = ["XRPUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "BNBUSDT", "DOGEUSDT", "AVAXUSDT"];
const TABS = ["positions", "orders", "trades", "decisions", "log"] as const;
type Tab = typeof TABS[number];
const MARK: Record<string, string> = { PASS: "✓", FAIL: "✗", WAITING: "…", NOT_APPLICABLE: "–" };
const ALL_LAYERS = new Set(Object.values(TOGGLE_GROUP));
const money = (value: unknown) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—";
};
const stamp = (value?: string | null) =>
  value ? value.replace("T", " ").replace("+00:00", " UTC").slice(0, 19) : "—";

export default function AdaptiveLab() {
  const { toast } = useApp();
  const status = useLive<LabStatus>("/research/adaptive-lab/status", 5_000);
  const hasBot = Boolean(status.data?.bot);
  const paper = useLive<Paper>(hasBot ? "/research/adaptive-lab/paper" : null, 5_000);
  const state = useLive<LabState>(hasBot ? "/research/adaptive-lab/state" : null, 5_000);
  const candles = useLive<{ candles: Candle[]; source: string }>(
    hasBot ? "/research/adaptive-lab/candles?limit=300" : null, 10_000);
  const features = useLive<{ overlays: Overlay[] }>(hasBot ? "/research/adaptive-lab/features" : null, 10_000);
  const timeline = useLive<{ events: TimelineEvent[] }>(hasBot ? "/research/adaptive-lab/timeline" : null, 10_000);
  const [tab, setTab] = useState<Tab>("positions");
  const [busy, setBusy] = useState(false);
  const [riskDraft, setRiskDraft] = useState<string | null>(null);
  const saving = useRef(false);

  const lab = status.data;
  const bot = lab?.bot ?? null;
  const risk = riskDraft ?? String(lab?.risk_pct ?? "");
  const modeLabel = (id?: string) => lab?.modes.find((row) => row.id === id)?.label ?? id ?? "—";
  const orders = useMemo(() => Object.entries(paper.data?.orders ?? {})
    .flatMap(([kind, rows]) => Object.entries(rows ?? {}).map(([id, row]) => ({ id, kind, ...(row as object) }))),
  [paper.data?.orders]);

  /** One change, saved at once; the server's answer is what the page shows. */
  const save = async (change: { symbol?: string; mode?: string; risk_pct?: number }, what: string) => {
    if (saving.current) return;
    saving.current = true;
    setBusy(true);
    try {
      const next = await apiPostJson<LabStatus>("/research/adaptive-lab/configuration", change);
      setRiskDraft(null);
      await Promise.all([status.refetch(), paper.refetch(), state.refetch()]);
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
          <tbody>{paper.data.positions.map((row) => <tr key={row.id}><td>{row.side}</td><td>{row.size}</td><td>{row.entry}</td><td>{row.stop ?? "—"}</td><td>{row.target ?? "—"}</td><td>{stamp(row.opened_at)}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No open paper positions.</div>;
    if (tab === "orders") return orders.length
      ? <table className="pa-table"><thead><tr><th>Kind</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>Id</th></tr></thead>
          <tbody>{orders.map((row: any) => <tr key={row.id}><td>{row.kind.replace(/_/g, " ")}</td><td>{row.side ?? row.direction ?? "—"}</td><td>{row.entry ?? row.limit_price ?? "—"}</td><td>{row.stop ?? row.stop_loss ?? "—"}</td><td>{row.target ?? row.take_profit ?? "—"}</td><td>{row.id.slice(0, 10)}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No working paper orders.</div>;
    if (tab === "trades") return paper.data?.trades.length
      ? <table className="pa-table"><thead><tr><th>Opened</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Result</th></tr></thead>
          <tbody>{paper.data.trades.map((row) => <tr key={row.id}><td>{stamp(row.opened_at)}</td><td>{row.side}</td><td>{row.entry}</td><td>{row.exit ?? "—"}</td><td>{money(row.realized_pnl ?? row.pnl)}</td><td>{row.status ?? "—"}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No paper trades yet.</div>;
    if (tab === "decisions") return timeline.data?.events.length
      ? <table className="pa-table"><thead><tr><th>Candle</th><th>Side</th><th>Decision</th><th>Reason</th></tr></thead>
          <tbody>{timeline.data.events.map((row) => <tr key={row.id}><td>{stamp(row.timestamp)}</td><td>{row.side}</td><td>{row.decision}</td><td>{row.blocker_explanation || row.reason}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No signals yet. Candles with no setup are summed up by &ldquo;Waiting for&rdquo; above the chart.</div>;
    return paper.data?.logs.length
      ? <table className="pa-table"><tbody>{paper.data.logs.map((row, i) => <tr key={row.id ?? i}><td>{stamp(row.ts)}</td><td>{row.level}</td><td>{row.message}</td></tr>)}</tbody></table>
      : <div className="pa-empty">No log entries yet.</div>;
  })();

  return <div className="pa-lab adaptive-lab">
    <header className="pa-titlebar">
      <div><span className="pa-kicker">ISOLATED FORWARD-PAPER</span><h1>Adaptive MTF Lab</h1>
        <p>{lab?.strategy.label ?? "Adaptive MTF Trend Pullback"} {lab?.strategy.version ?? ""} · its own paper account · no exchange routing</p></div>
      <div className="pa-safety"><b>{lab ? modeLabel(lab.mode).toUpperCase() : "LOADING"}</b><span>LIVE ROUTING DISABLED</span></div>
    </header>

    <div className="pa-workspace">
      <aside className="pa-sidebar" aria-label="Adaptive MTF Lab controls">
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
        <div className="pa-chart-shell" aria-label="Adaptive MTF chart workspace">
          <div className="pa-chart-head"><div><b>{lab?.symbol ?? "—"} · 5m</b><span>1h regime · 15m pullback · 5m confirmation</span><span>{bot?.market_status ?? "—"}</span></div>
            <div><span>{decision?.decision ?? state.data?.decision_state ?? "—"}</span><b>{decision?.state === "ORDER_PENDING" || state.data?.position ? "IN PLAY" : "WAIT"}</b></div></div>
          <div className="pa-metric-scope" data-testid="adaptive-waiting"><b>Waiting for</b><span>{waiting}</span>
            {state.data?.gates?.length ? <span className="adaptive-gates">{state.data.gates.map((gate) => <em key={gate.id} title={gate.explanation || gate.detail} className={`gate-${gate.state.toLowerCase()}`}>{MARK[gate.state] ?? "·"} {gate.label}</em>)}</span> : null}</div>
          {!bot ? <div className="pa-loading">No bot yet — choose a mode to start one.</div>
            : <Chart candles={candles.data?.candles ?? []} forming={null}
                     overlays={features.data?.overlays ?? []} events={timeline.data?.events ?? []}
                     position={state.data?.position ?? null} enabled={ALL_LAYERS}
                     showDecisionMarkers focus={null} view={120} fit={false}
                     unavailable={candles.error} onPick={() => undefined} onPickOverlay={() => undefined} />}
          <div className="pa-chart-foot"><span><i className={bot?.market_status === "LIVE" ? "live" : "stale"} />{candles.data?.source ?? "Waiting for closed candles"}</span><b>PAPER · NO LIVE EXECUTION PATH</b></div>
        </div>
        <div className="pa-bottom">
          <nav>{TABS.map((row) => <button type="button" key={row} className={tab === row ? "active" : ""} onClick={() => setTab(row)}>{row}<em>{row === "positions" ? paper.data?.positions.length ?? 0 : row === "orders" ? orders.length : row === "trades" ? paper.data?.trades.length ?? 0 : row === "decisions" ? timeline.data?.events.length ?? 0 : ""}</em></button>)}</nav>
          <div className="pa-bottom-body">{bottom}</div>
        </div>
      </main>
    </div>
  </div>;
}
