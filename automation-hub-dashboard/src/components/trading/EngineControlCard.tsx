import { useState } from "react";
import Card from "../common/Card";
import Icon from "../common/Icon";
import { Badge } from "../common/ui";
import { apiPost, hhmmss, type EngineStatus, type LogRow, uptime } from "../../lib/api";

type Props = {
  engine: EngineStatus | null;
  logs: LogRow[];
  onRefresh: () => void;
  toast: (message: string, tone: "success" | "error" | "info") => void;
};

const stateTone = (state?: string) => {
  if (state === "running" || state === "ready") return "green";
  if (["starting", "bootstrapping", "warming", "syncing", "recovering"].includes(state ?? "")) return "blue";
  if (state === "data_stale") return "amber";
  if (state === "paused") return "amber";
  return "red";
};

const titleCase = (value?: string) => value ? value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) : "Stopped";

export default function EngineControlCard({ engine, logs, onRefresh, toast }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [showLogs, setShowLogs] = useState(false);
  const state = engine?.state ?? "stopped";
  const working = ["starting", "bootstrapping", "warming", "syncing", "recovering"].includes(state);
  const canStart = state === "stopped" || state === "error";
  const canPause = engine?.running && state === "running" && engine.trading_state === "Active";
  const canResume = state === "paused" || engine?.trading_state !== "Active";
  const canRestart = !working;
  const canStop = Boolean(engine?.running) || working;

  const control = async (path: string, label: string, confirm?: string) => {
    if (confirm && !window.confirm(confirm)) return;
    setBusy(path);
    try {
      await apiPost(path);
      toast(label, "success");
      onRefresh();
    } catch (error) {
      toast(error instanceof Error ? error.message : "Engine control request failed", "error");
    } finally {
      setBusy(null);
    }
  };

  const detail = (label: string, value: string | number | null | undefined) => (
    <div className="risk-item" key={label}><span className="dim">{label}</span><b>{value ?? "—"}</b></div>
  );
  const lastTrade = engine?.last_trade;
  const reconnectNote = state === "recovering"
    ? `Reconnecting… Attempt ${engine?.reconnect_attempt ?? 0} of ${engine?.max_reconnect_attempts ?? 5}`
    : null;

  return (
    <Card title="Paper Trading Engine" subtitle="Server-authoritative lifecycle · paper mode only">
      <div className="row-actions engine-control-head" style={{ justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div>
          <Badge text={titleCase(state)} tone={stateTone(state) as any} />
          <span className="dim" style={{ marginLeft: 8, fontSize: 12 }}>{reconnectNote ?? engine?.reason ?? "Ready to scan the market."}</span>
        </div>
        <div className="row-actions" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <button className="btn btn-primary btn-sm" disabled={!canStart || busy !== null} onClick={() => void control("/engine/start", "Engine start requested") }><Icon name="play" size={13} /> Start Engine</button>
          <button className="btn btn-warn btn-sm" disabled={!canPause || busy !== null} onClick={() => void control("/engine/pause", "Engine paused — new entries are blocked") }><Icon name="pause" size={13} /> Pause</button>
          <button className="btn btn-primary btn-sm" disabled={!canResume || busy !== null} onClick={() => void control("/engine/resume", "Engine resumed") }><Icon name="play" size={13} /> Resume</button>
          <button className="btn btn-soft btn-sm" disabled={!canRestart || busy !== null} onClick={() => void control("/engine/restart", "Engine restart requested", "Restart the paper engine? Open paper positions remain managed.") }><Icon name="refresh" size={13} /> Restart</button>
          <button className="btn btn-danger btn-sm" disabled={!canStop || busy !== null} onClick={() => void control("/engine/stop", "Engine stopped", "Stop the paper engine? It will stop scanning for new entries.") }><Icon name="close" size={13} /> Stop</button>
        </div>
      </div>

      {(engine?.reason || engine?.recommended_action) && (
        <div className="banner" style={{ marginBottom: 12 }}>
          <Icon name={state === "error" ? "warning" : "info"} size={14} />
          <span><b>{engine?.reason || "Engine status"}.</b> {engine?.recommended_action}</span>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: "8px 18px" }}>
        {detail("Last heartbeat", hhmmss(engine?.last_heartbeat))}
        {detail("Engine uptime", uptime(engine?.uptime_s ?? undefined))}
        {detail("Current symbol", engine?.current_symbol ?? "Waiting for market data")}
        {detail("Configured symbols", engine?.symbols?.join(", "))}
        {detail("Pair mode", engine?.symbol_selection_mode === "manual" ? `Manual · ${engine?.manual_symbol ?? "—"}` : `Automatic · ${(engine?.auto_symbols ?? engine?.symbols ?? []).length} pairs`)}
        {detail("Timeframe", engine?.timeframe)}
        {detail("Active strategy", engine?.strategy)}
        {detail("Position sizing", engine?.position_sizing_mode === "fixed" ? `Manual · ${engine?.fixed_position_size ?? "—"} units` : "Automatic · risk-based")}
        {detail("Exchange", engine?.connected_exchange)}
        {detail("Market feed", engine?.feed_status ?? engine?.websocket_status)}
        {detail("Market session (UTC)", engine?.market_session)}
        {detail("Last market data", hhmmss(engine?.last_activity ?? engine?.last_bar_ts))}
        {detail("Processed candles", engine?.bars)}
        {detail("Last trade", lastTrade ? `${lastTrade.action ?? "fill"} ${lastTrade.symbol ?? ""}` : "No fill yet")}
        {detail("Last trade time", hhmmss(lastTrade?.timestamp))}
      </div>

      {engine?.last_error && <p className="dim" style={{ margin: "10px 0 0" }}><b>Last error:</b> {engine.last_error}</p>}
      <div style={{ marginTop: 12 }}>
        <button className="btn btn-ghost btn-sm" onClick={() => setShowLogs((open) => !open)}>
          <Icon name="history" size={13} /> {showLogs ? "Hide Logs" : "View Logs"}
        </button>
      </div>
      {showLogs && (
        <div className="tablewrap" style={{ marginTop: 8, maxHeight: 260 }}>
          <table className="data-table"><thead><tr><th>Time</th><th>Level</th><th>Event</th></tr></thead>
            <tbody>{logs.filter((row) => row.stage === "engine" || row.stage === "controls").slice(0, 20).map((row) => (
              <tr key={row.id}><td className="dim mono">{hhmmss(row.ts)}</td><td>{row.level}</td><td>{row.message}</td></tr>
            ))}{!logs.length && <tr><td colSpan={3} className="dim ta-center">No engine events recorded.</td></tr>}</tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
