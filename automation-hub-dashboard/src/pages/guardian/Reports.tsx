import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import { clock, type RecoveryView, type ReportsView } from "./common";

/** Reports (PRD §28-31) and controlled recovery (§39-40). Reports say how
 *  much of the period Guardian actually observed; recovery lists every
 *  decision, taken or not, and nothing is automatic unless the owner enabled
 *  it. */
export default function Reports() {
  const reports = useLive<ReportsView>("/guardian/reports", 60000);
  const recovery = useLive<RecoveryView>("/guardian/recovery", 30000);
  const r = reports.data;
  const rec = recovery.data;
  return (
    <>
      <Card title="Reports" subtitle="Issued once per finished day and week, kept as issued">
        {!r ? <p className="dim">{reports.error ? `Unavailable: ${reports.error}` : "Loading…"}</p>
          : r.reports.length ? (
            <ul className="gd-reports" data-testid="guardian-reports">
              {r.reports.map((rep) => (
                <li key={rep.seq}>
                  <div className="gd-trace-head">
                    <Badge text={rep.kind.toUpperCase()} tone={rep.kind === "weekly" ? "purple" : "blue"} />
                    <span className="mono dim">{rep.period_start.slice(0, 10)} → {rep.period_end.slice(0, 10)}</span>
                    <span className="dim">Guardian observed {(rep.body.guardian_coverage * 100).toFixed(1)}% of it</span>
                  </div>
                  <pre className="gd-report-text">{rep.text}</pre>
                </li>
              ))}
            </ul>
          ) : <p className="dim" data-testid="guardian-reports-empty">No reports yet. The first daily report is issued after the first full day Guardian observes.</p>}
      </Card>

      <div className="grid-2-eq">
        <Card title="Recovery policies" subtitle="Operational actions only — never a trading one">
          {!rec ? <p className="dim">Loading…</p> : !rec.configured ? <p className="dim">Recovery is not configured.</p> : (
            <>
              <ul className="gd-kv" data-testid="guardian-recovery-policies">
                {Object.entries(rec.actions ?? {}).map(([name, a]) => (
                  <li key={name}>
                    <span>{name.replace(/_/g, " ").toLowerCase()} — {a.effect}</span>
                    <Badge text={a.enabled ? (a.auto ? "AUTOMATIC" : "ENABLED BY OWNER") : "OFF"} tone={a.enabled ? "green" : "default"} />
                  </li>
                ))}
              </ul>
              <p className="dim gd-cluster">At most {rec.max_per_hour} per hour, {Math.round((rec.cooldown_s ?? 0) / 60)} min cooldown per target.
                Enable a policy with HUB_GUARDIAN_RECOVERY on the server. Never: {(rec.never ?? []).join(", ")}.</p>
            </>
          )}
        </Card>
        <Card title="Notifications" subtitle="Serious incidents (opened and closed) and reports — once each">
          {r?.notifications.length ? (
            <ul className="gd-list">
              {r.notifications.map((a) => (
                <li key={a.action_id}>
                  <span className="mono dim">{clock(a.at)}</span>
                  <Badge text={a.result} tone={a.result === "SENT" ? "green" : "amber"} />
                  <span>{a.reason}</span>
                </li>
              ))}
            </ul>
          ) : <p className="dim">None sent yet.</p>}
        </Card>
      </div>

      <Card title="Recovery decisions" subtitle="Every one recorded — including those not taken, and why">
        {rec?.history.length ? (
          <ul className="gd-list" data-testid="guardian-recovery-history">
            {rec.history.map((a) => (
              <li key={a.action_id}>
                <span className="mono dim">{clock(a.at)}</span>
                <Badge text={a.result.replace(/_/g, " ")} tone={a.result === "SUCCESS" ? "green" : a.result.startsWith("NOT_TAKEN") ? "blue" : "amber"} />
                <span><b>{a.action.replace(/_/g, " ").toLowerCase()}</b> — {a.reason}</span>
              </li>
            ))}
          </ul>
        ) : <p className="dim">No recovery decisions yet.</p>}
      </Card>
    </>
  );
}
