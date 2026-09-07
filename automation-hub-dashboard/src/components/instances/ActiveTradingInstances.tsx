import Card from "../common/Card";
import { Badge, StatCard } from "../common/ui";
import { apiPost, useLive } from "../../lib/api";
import { useApp } from "../../app-context";

type Metrics = {
  trades: number; win_rate: number; profit_factor: number; realized_pnl: number;
  strategy_health?: { status?: string; warnings?: { detail: string }[] };
};
type Position = {
  side: string; entry: number; stop?: number | null; target?: number | null;
  unrealized_pnl?: number | null;
};
type Instance = {
  id: string; symbol: string; strategy_label: string; strategy_version: string;
  timeframe: string; risk_per_trade_pct: number; state: string; mode: string;
  ui_status?: "RUNNING_UNARMED" | "RUNNING_ARMED" | "BLOCKED" | "ERROR";
  last_blocker?: string;
  last_error?: string; current_position?: Position | null; metrics: Metrics;
  engine?: { last_heartbeat?: string | null; lifecycle_state?: string } | null;
  mtf_policy?: { label?: string } | null;
};
type InstanceSnapshot = {
  instances: Instance[]; active_slots: number; max_active_slots: number;
  current_global_risk_amount: number; max_global_risk_amount: number;
  total_open_positions: number; today_pnl: number; global_risk_status: string;
  global_risk_message: string; paper_account_capital: number;
  total_allocated_capital: number; available_paper_capital: number; total_current_equity: number;
};

const money = (value?: number | null) => `${(value ?? 0) >= 0 ? "+" : "-"}$${Math.abs(value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
const price = (value?: number | null) => value == null ? "—" : `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

function displayState(instance: Instance): string {
  return instance.ui_status ?? "BLOCKED";
}

function health(instance: Instance): string {
  if (instance.ui_status === "ERROR") return "Error";
  if (instance.ui_status === "BLOCKED") return "Blocked";
  if (instance.ui_status === "RUNNING_UNARMED") return "Unarmed";
  if ((instance.metrics?.trades ?? 0) < 8) return "Insufficient data";
  const status = instance.metrics?.strategy_health?.status;
  return status === "Degrading" || status === "Unhealthy" ? "Unhealthy" : "Healthy";
}

function InstanceCard({ instance, onPause }: { instance: Instance; onPause: (id: string) => void }) {
  const app = useApp();
  const pos = instance.current_position;
  const status = displayState(instance);
  const h = health(instance);
  const drift = h === "Healthy" ? "Low" : h === "Degraded" ? "Raised" : "—";
  const pnl = instance.metrics?.realized_pnl ?? 0;
  const error = instance.last_error || instance.engine?.lifecycle_state === "error" ? instance.last_error || "Unknown internal error" : "";

  return (
    <article className="instance-summary-card" role="button" tabIndex={0}
      onClick={() => app.viewInstance(instance.id)}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") app.viewInstance(instance.id); }}>
      <div className="instance-summary-head">
        <div><b>{instance.symbol}</b><span>{instance.strategy_label} · {instance.strategy_version}</span></div>
        <Badge text={status} tone={status === "ERROR" ? "red" : status === "RUNNING_ARMED" ? "green" : "amber"} />
      </div>
      <div className="instance-summary-meta">{instance.mtf_policy?.label ?? `Entry ${instance.timeframe} · native HTF loading`} · Risk {(instance.risk_per_trade_pct * 100).toFixed(2)}% · {instance.mode}</div>
      <div className="instance-summary-stats">
        <div><span>P&amp;L</span><b className={pnl >= 0 ? "pos" : "neg"}>{money(pnl)}</b></div>
        <div><span>Win rate</span><b>{instance.metrics?.win_rate ?? 0}%</b></div>
        <div><span>Profit factor</span><b>{instance.metrics?.profit_factor ?? 0}</b></div>
        <div><span>Trades</span><b>{instance.metrics?.trades ?? 0}</b></div>
      </div>
      {pos ? (
        <div className="instance-position">
          <b className={pos.side === "long" ? "pos" : "neg"}>{pos.side?.toUpperCase()} {instance.symbol}</b>
          <span>Entry {price(pos.entry)} · P&amp;L <b className={(pos.unrealized_pnl ?? 0) >= 0 ? "pos" : "neg"}>{pos.unrealized_pnl == null ? "Awaiting mark" : money(pos.unrealized_pnl)}</b></span>
          <span>TP {price(pos.target)} · SL {price(pos.stop)}</span>
        </div>
      ) : <div className="instance-wait">{error || instance.last_blocker || "Waiting for setup"}</div>}
      <div className="instance-summary-foot">
        <span>Health <b className={h === "Healthy" ? "pos" : h === "Unhealthy" || h === "Error" ? "neg" : "amber"}>{h}</b> · PF {instance.metrics?.profit_factor ?? 0} · Drift {drift}</span>
        <span>{instance.engine?.last_heartbeat ? `Heartbeat ${new Date(instance.engine.last_heartbeat).toLocaleTimeString()}` : "No heartbeat yet"}</span>
        <div><button className="btn btn-soft btn-sm" onClick={(event) => { event.stopPropagation(); app.viewInstance(instance.id); }}>View Instance</button>{instance.state === "running" && <button className="btn btn-warn btn-sm" onClick={(event) => { event.stopPropagation(); onPause(instance.id); }}>Pause</button>}</div>
      </div>
    </article>
  );
}

export default function ActiveTradingInstances() {
  const app = useApp();
  const instances = useLive<InstanceSnapshot>("/instances", 2000);
  const snapshot = instances.data;
  // Running, paused, bootstrap/recovery and error states all deserve
  // visibility. Stopped historical instances live on the dedicated page.
  const active = (snapshot?.instances ?? []).filter((instance) => instance.state !== "stopped")
    .sort((a, b) => Number(b.id === app.selectedInstanceId) - Number(a.id === app.selectedInstanceId));
  const pause = async (id: string) => {
    try { await apiPost(`/instances/${id}/pause`); instances.refetch(); app.toast("Trading instance paused", "success"); }
    catch (error) { app.toast(error instanceof Error ? error.message : "Could not pause instance", "error"); }
  };
  const riskPct = snapshot?.max_global_risk_amount ? snapshot.current_global_risk_amount / snapshot.max_global_risk_amount * 100 : 0;
  const riskTone = snapshot?.global_risk_status === "healthy" ? "green" : snapshot?.global_risk_status === "warning" ? "amber" : "red";
  const eligible = (snapshot?.instances ?? []).filter((instance) => instance.mode === "trading" && (instance.metrics?.trades ?? 0) >= 8);
  const best = eligible.length ? eligible.reduce((winner, candidate) => (candidate.metrics.profit_factor > winner.metrics.profit_factor ? candidate : winner)) : null;

  return <section className="active-instances-section" aria-label="Active Trading Instances">
    <div className="active-instances-title"><div><h2>Active Trading Instances</h2><p>Authoritative paper engine state · monitor here, manage in the instance detail</p></div><Badge text={`${snapshot?.active_slots ?? 0} / ${snapshot?.max_active_slots ?? 1} active`} tone="blue" /></div>
    <div className="instance-summary-row">
      <StatCard label="Paper capacity" value={`$${(snapshot?.paper_account_capital ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`} sub={`allocated $${(snapshot?.total_allocated_capital ?? 0).toLocaleString()} · available $${(snapshot?.available_paper_capital ?? 0).toLocaleString()}`} />
      <StatCard label="Today's P&L" value={money(snapshot?.today_pnl)} tone={(snapshot?.today_pnl ?? 0) >= 0 ? "green" : "red"} sub="instances only" />
      <StatCard label="Open positions" value={String(snapshot?.total_open_positions ?? 0)} sub={`${snapshot?.active_slots ?? 0} / ${snapshot?.max_active_slots ?? 1} trading slots`} />
      <StatCard label="Open risk" value={`${riskPct.toFixed(1)}%`} tone={riskTone} sub={`${money(snapshot?.current_global_risk_amount)} of ${money(snapshot?.max_global_risk_amount)}`} />
      <StatCard label="Global risk" value={snapshot?.global_risk_status === "healthy" ? "Healthy" : snapshot?.global_risk_status === "warning" ? "Warning" : "Paused"} tone={riskTone} sub={snapshot?.global_risk_message ?? "Checking limits"} />
    </div>
    {snapshot?.global_risk_status && snapshot.global_risk_status !== "healthy" && <div className={`instance-risk-notice ${riskTone}`}>{snapshot.global_risk_message}</div>}
    <div className="instance-summary-grid">
      {active.map((instance) => <InstanceCard key={instance.id} instance={instance} onPause={pause} />)}
      {!active.length && <Card title="No Active Trading Instances" subtitle="Your Paper Trading Engine is currently idle."><div className="instance-empty"><p>Create one isolated pair + strategy instance when you are ready to run paper trading.</p><button className="btn btn-primary" onClick={() => app.go("Trading Instances")}>Create Trading Instance</button></div></Card>}
    </div>
    {best && <button className="best-instance-insight" onClick={() => app.viewInstance(best.id)}><span>Best measured instance</span><b>{best.symbol} · {best.strategy_label} {best.strategy_version}</b><span>PF {best.metrics.profit_factor} · Expectancy {money(best.metrics.realized_pnl / Math.max(best.metrics.trades, 1))} · {best.metrics.trades} trades</span></button>}
  </section>;
}
