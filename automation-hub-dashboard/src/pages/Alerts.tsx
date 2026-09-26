import Icon from "../components/common/Icon";
import { Badge, PageHeader } from "../components/common/ui";
import { useLive, type AlertRow } from "../lib/api";

/** Local time, with the date when it is not today: an outage from yesterday
 *  must not look like one from an hour ago. */
function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const time = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return at.toDateString() === new Date().toDateString()
    ? time
    : `${at.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

const sevTone = (s: string) => ({ info: "blue", warning: "amber", critical: "red" }[s] as any) ?? "default";
const catTone = (c: string) => ({ risk: "purple", trade: "green", system: "blue", controls: "amber" }[c] as any) ?? "default";

export default function AlertsPage() {
  const { data, error } = useLive<AlertRow[]>("/ledger/alerts?limit=100", 3000);
  const items = data ?? [];

  return (
    <>
      <PageHeader title="Alerts" subtitle={`${items.length} alerts · live from the engine`} />

      {error && !data && (
        <div className="card" style={{ borderColor: "#ef4444" }}>
          <Icon name="warning" size={15} className="neg" /> Backend not reachable. Start the API to stream live alerts.
        </div>
      )}

      <div className="alert-stack">
        {items.map((a) => (
          <div className="card alert-card" key={a.id}>
            <span className={`alert-icon ${sevTone(a.severity) === "red" ? "neg" : sevTone(a.severity) === "amber" ? "amber" : "blue"}`}>
              <Icon name={a.severity === "critical" ? "warning" : a.severity === "warning" ? "warning" : "info"} size={16} />
            </span>
            <div className="alert-body">
              <div className="alert-titlerow">
                <b>{a.title}</b>
                <Badge text={a.severity} tone={sevTone(a.severity)} />
                <Badge text={a.category} tone={catTone(a.category)} />
              </div>
              <span className="dim">{a.detail}</span>
            </div>
            <span className="alert-time" title={a.ts ?? undefined}>{when(a.ts)}</span>
          </div>
        ))}
        {items.length === 0 && <div className="empty-state">No alerts yet.</div>}
      </div>
    </>
  );
}
