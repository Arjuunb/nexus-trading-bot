import { useApp } from "../../app-context";
import { type LabBotStatus, useLive } from "../../lib/api";

const number = (value: unknown, digits = 2) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—";
};

const percent = (value: unknown) => {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : "—";
};

const shortTime = (value: unknown) => {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
};

const reference = (row?: Record<string, any> | null) =>
  row?.order_id ?? row?.correlation_id ?? row?.proposal_id ?? row?.id ?? "—";

function PerformanceScope({ title, data }: { title: string; data: Record<string, any> }) {
  return (
    <div className="lab-perf-scope">
      <b>{title}</b>
      {data?.available === false ? (
        <span className="lab-unavailable">Unavailable · {data.reason}</span>
      ) : (
        <span>
          {data?.closed_trades ?? 0} closed · Win {percent(data?.win_rate)} · PF {number(data?.profit_factor)} ·
          Avg R {number(data?.average_realized_rr)} · DD {number(data?.maximum_drawdown)}
        </span>
      )}
    </div>
  );
}

/** Name the saved operating mode exactly.
 *
 * This badge used to read ISOLATED_FORWARD_PAPER for any mode that was not
 * signals_only, so a session in manual_approval — which places no order until
 * a person approves each proposal — was indistinguishable from an armed one.
 * Only operating_mode "automatic" arms execution, so the three modes must read
 * as three different things.
 */
function modeBadge(status: LabBotStatus | null): string {
  if (!status?.session_id || !status.mode) return "LOADING SESSION";
  switch (status.mode) {
    case "signals_only": return "SIGNALS_ONLY";
    case "manual_approval": return "MANUAL_APPROVAL";
    case "automatic": return "ISOLATED_FORWARD_PAPER";
    default: return String(status.mode).toUpperCase();
  }
}

function LabCard({ title, page, status, error }: {
  title: string; page: string; status: LabBotStatus | null; error: string | null;
}) {
  const app = useApp();
  const feedState = String(status?.feed?.state ?? "DISCONNECTED");
  const ready = status?.execution_state === "RUNNING_ARMED";
  const account = status?.account ?? {};
  const decision = status?.latest_closed_candle_decision;
  const latestOrder = status?.latest_order;
  const latestFill = status?.latest_fill;
  const strategyLabel = status?.strategy?.model_id ?? status?.strategy?.id ?? "Not loaded";

  return (
    <section className="card lab-bot-card" data-testid={`dashboard-${page.toLowerCase().replace(/\s+/g, "-")}`}>
      <header className="lab-bot-head">
        <div>
          <span className="lab-scope">{status?.scope_label ?? `${title} · isolated paper ledger`}</span>
          <h3>{title}</h3>
          <p>{strategyLabel} · {status?.strategy?.version ?? "version unavailable"}</p>
        </div>
        <div className="lab-badges">
          <span className="lab-badge paper">{modeBadge(status)}</span>
          <span className={`lab-badge ${status?.feed?.reliable ? "ok" : "bad"}`}>{feedState}</span>
          <span className={`lab-badge ${ready ? "ok" : "bad"}`}>{status?.execution_state ?? "BLOCKED"}</span>
        </div>
      </header>

      {error && <div className="lab-bot-error">Status API unavailable: {error}</div>}

      <div className="lab-bot-grid">
        <span><small>Market</small><b>{status?.symbol ?? "—"} · {status?.timeframe ?? "—"}</b></span>
        <span><small>Saved mode</small><b>{status?.mode ?? "—"}</b></span>
        <span><small>Decision state</small><b>{status?.decision_state ?? "DISCONNECTED"}</b></span>
        <span><small>Balance / equity</small><b>{number(account.balance)} / {number(account.equity)} USDT</b></span>
        <span><small>Positions / orders</small><b>{status?.open_positions ?? 0} / {status?.pending_orders ?? 0}</b></span>
        <span><small>Realized / unrealized</small><b>{number(account.realized_pnl)} / {number(account.unrealized_pnl)} USDT</b></span>
      </div>

      <div className="lab-evidence">
        <div><small>Latest closed-candle decision</small><b>{decision?.state ?? "No recorded decision"}</b>
          <span>{decision?.candle_time ? shortTime(decision.candle_time) : "—"} · {decision?.reason ?? "Awaiting first confirmed candle"}</span>
          {!!decision?.missing_conditions?.length && <span>Missing: {decision.missing_conditions.join(" · ")}</span>}
        </div>
        <div><small>Latest signal</small><b>{reference(status?.latest_signal)}</b>
          <span>{status?.latest_signal ? "Correlated to persisted candle evaluation" : "No eligible signal recorded"}</span>
        </div>
        <div><small>Latest order / fill</small><b>{reference(latestOrder)} / {reference(latestFill)}</b>
          <span>{latestOrder?.status ?? "No order"} · {latestFill ? shortTime(latestFill.created_at ?? latestFill.timestamp) : "No fill"}</span>
        </div>
        <div><small>Feed evidence</small><b>{status?.feed?.failing_dependency ?? (status ? "All required dependencies healthy" : "Loading feed status")}</b>
          <span>{status?.feed?.health_reason ?? "No heartbeat"} · Last event {shortTime(status?.feed?.last_successful_event?.at)}</span>
          <span>Heartbeat {shortTime(status?.last_heartbeat)} · Retry {number(status?.feed?.retry_state?.attempt, 0)}</span>
        </div>
      </div>

      <div className={`lab-blockers ${status?.blockers?.length ? "has-blockers" : "clear"}`}>
        <b>{!status ? "Loading execution status" : status.blockers?.length ? "Execution blockers" : "Execution gate clear"}</b>
        <span>{status?.blockers?.length ? status.blockers.join(" · ") : "May execute only on a new confirmed closed candle that produces an eligible signal."}</span>
      </div>

      <div className="lab-performance">
        <PerformanceScope title="Backtest" data={status?.performance?.backtest ?? { available: false, reason: "Loading" }} />
        <PerformanceScope title="Forward Validation" data={status?.performance?.forward_validation ?? { available: false, reason: "Loading" }} />
        <PerformanceScope title="Live Paper" data={status?.performance?.live_paper ?? { available: false, reason: "Loading" }} />
      </div>

      <footer className="lab-bot-foot">
        <span>Scope: <b>{status?.account_scope ?? "isolated lab account"}</b> · never the global Trading Instance ledger</span>
        <button className="btn secondary" onClick={() => app.go(page)}>Open Lab</button>
      </footer>
    </section>
  );
}

export default function LabBotCards() {
  const priceAction = useLive<LabBotStatus>("/research/price-action/bot-status", 4000);
  const smc = useLive<LabBotStatus>("/research/smc/bot-status", 4000);
  return (
    <div className="lab-bot-cards" aria-label="Independent paper lab systems">
      <LabCard title="Price Action Bot" page="Price Action Lab" status={priceAction.data} error={priceAction.error} />
      <LabCard title="SMC Bot" page="SMC Strategy Lab" status={smc.data} error={smc.error} />
    </div>
  );
}
