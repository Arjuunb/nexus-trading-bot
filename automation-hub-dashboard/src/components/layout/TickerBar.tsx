import { useEffect, useState } from "react";
import { type LabBotStatus, uptime, useLive } from "../../lib/api";
import { useApp } from "../../app-context";
import NexusBotPet from "../nexus-pet/NexusBotPet";

const usd = (value?: number | null) => typeof value === "number" && Number.isFinite(value) ? `$${value.toLocaleString()}` : "—";

type Snapshot = {
  active_slots: number; max_active_slots: number; total_open_positions: number;
  current_global_risk_amount: number; max_global_risk_amount: number;
  market_data_status: string; instances: {
    id: string; symbol: string; strategy_label: string; timeframe: string; state: string;
    started_at?: string | null; engine?: { started_at?: string | null; uptime_s?: number | null; running?: boolean } | null;
  }[];
};

const ACTIVE_INSTANCE_STATES = new Set(["starting", "bootstrapping", "warming", "syncing", "ready", "running", "data_stale", "recovering", "paused"]);

export function footerConnectionState(error: string | null, loading: boolean): string {
  if (loading && !error) return "LOADING";
  if (!error) return "DATA_UNAVAILABLE";
  const status = error.match(/\bHTTP\s+(\d{3})\b/i)?.[1];
  if (status === "401") return "AUTH_REQUIRED";
  if (status === "403") return "ACCESS_DENIED";
  if (status === "429") return "RATE_LIMITED";
  if (status) return `API_ERROR · HTTP ${status}`;
  if (/failed to fetch|fetch failed|networkerror|network error|load failed|connection refused/i.test(error)) {
    return "BACKEND_UNREACHABLE";
  }
  return "API_ERROR";
}

/** Footer uses the same Trading Instance payload as the dashboard and detail UI. */
export default function TickerBar({ surface }: { surface: string }) {
  const app = useApp();
  const instanceState = useLive<Snapshot>("/instances", 4000);
  const { data } = instanceState;
  const pa = useLive<LabBotStatus>("/research/price-action/bot-status", 4000);
  const smc = useLive<LabBotStatus>("/research/smc/bot-status", 4000);
  const [, setClock] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setClock((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const rows = data?.instances ?? [];
  const active = rows.filter((row) => row.engine?.running || ACTIVE_INSTANCE_STATES.has(row.state));
  const runningCount = rows.filter((row) => row.state === "running").length;
  const instances = active.map((row) => `${row.symbol} · ${row.strategy_label} · ${row.timeframe}`).join(" | ") || "No active instance";
  const selected = active.find((row) => row.id === app.selectedInstanceId) ?? active[0];
  const activeSince = selected?.engine?.started_at ?? selected?.started_at;
  const parsedStart = activeSince ? Date.parse(activeSince) : Number.NaN;
  const activeSeconds = Number.isFinite(parsedStart)
    ? Math.max(0, (Date.now() - parsedStart) / 1000)
    : selected?.engine?.uptime_s ?? undefined;
  const labRequest = surface === "Price Action Lab" ? pa : surface === "SMC Strategy Lab" ? smc : null;
  const lab = labRequest?.data ?? null;
  const labItems: [string, string][] | null = lab ? [
    ["Surface", lab.lab === "PRICE_ACTION" ? "PRICE ACTION LAB" : "SMC STRATEGY LAB"],
    ["Mode", !lab.session_id || !lab.mode ? "LOADING SESSION" : lab.mode === "signals_only" ? "SIGNALS_ONLY" : "ISOLATED_FORWARD_PAPER"],
    ["Data", `Binance USD-M · ${labRequest?.error ? "STALE" : lab.feed?.state ?? "DISCONNECTED"}`],
    ["Market", `${lab.symbol ?? "—"} · ${lab.timeframe ?? "—"}`],
    ["Positions / orders", `${lab.open_positions ?? 0} / ${lab.pending_orders ?? 0}`],
    ["Account", `${Number(lab.account?.equity ?? 0).toLocaleString()} USDT`],
    ["State", labRequest?.error ? `DEGRADED · ${footerConnectionState(labRequest.error, false)}` : lab.execution_state ?? "BLOCKED"],
  ] : null;
  const researchItems: [string, string][] | null = surface === "SMC Visual Lab" ? [
    ["Surface", "SMC VISUAL RESEARCH"], ["Mode", "SIGNALS_ONLY"],
    ["Execution", "DISABLED"], ["Data", "Binance USD-M public market data"],
  ] : null;
  const instanceItems: [string, string][] = data ? [
    ...(instanceState.error ? [["Connection", `DEGRADED · ${footerConnectionState(instanceState.error, false)}`] as [string, string]] : []),
    ["Instance mode", "FORWARD_PAPER"], ["Instances", `${runningCount} / ${data.max_active_slots} running · ${data.active_slots} workers`],
    ["Global instance data", instanceState.error ? "STALE" : data.market_data_status], ["Open positions", String(data.total_open_positions)],
    // This bar sits outside the page error boundary: a missing amount must
    // read as unknown, not take the whole app down with it.
    ["Open risk", `${usd(data.current_global_risk_amount)} / ${usd(data.max_global_risk_amount)}`],
    ["Bot active time", activeSeconds === undefined ? "—" : uptime(activeSeconds)],
    ["Active", selected ? `${selected.symbol} · ${selected.strategy_label} · ${selected.timeframe}` : instances],
  ] : [["System", footerConnectionState(instanceState.error, instanceState.loading)]];
  const unavailableLabItems: [string, string][] | null = labRequest && !lab ? [
    ["Surface", surface === "Price Action Lab" ? "PRICE ACTION LAB" : "SMC STRATEGY LAB"],
    ["State", footerConnectionState(labRequest.error, labRequest.loading)],
  ] : null;
  const items = labItems ?? unavailableLabItems ?? researchItems ?? instanceItems;
  return <footer className="ticker"><div className="ticker-items">{items.map(([k, v]) => <span className="ticker-item" key={k}><b>{k}</b><span className="ticker-price">{v}</span></span>)}</div><div className="ticker-meta"><NexusBotPet /></div></footer>;
}
