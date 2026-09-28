import { useMemo, useState } from "react";
import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { apiGet, useLive } from "../../lib/api";
import { clock, type GEvent, type GuardianStatus, SEVERITY_TONE } from "./common";

const SEVERITIES = ["INFO", "WATCH", "WARNING", "HIGH", "CRITICAL"];
const CATEGORIES = ["market_data", "strategy", "risk", "execution", "position", "journal", "infrastructure", "guardian"];

function hashParam(name: string): string {
  const query = window.location.hash.split("?")[1] ?? "";
  return new URLSearchParams(query).get(name) ?? "";
}

/** Everything Guardian recorded, newest first. Events are immutable evidence:
 *  this page can filter them but nothing can edit them. */
export default function Activity({ status }: { status: GuardianStatus }) {
  const [component, setComponent] = useState(hashParam("component"));
  const [severity, setSeverity] = useState("");
  const [category, setCategory] = useState("");
  const [older, setOlder] = useState<GEvent[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const query = useMemo(() => {
    const p = new URLSearchParams({ limit: "200" });
    if (component) p.set("component", component);
    if (severity) p.set("min_severity", severity);
    if (category) p.set("category", category);
    return p.toString();
  }, [component, severity, category]);
  const live = useLive<{ events: GEvent[]; next_before: number | null }>(`/guardian/events?${query}`, 5000);
  const rows = [...(live.data?.events ?? []), ...older];
  const cursor = older.length ? nextBefore : live.data?.next_before ?? null;
  const reset = (fn: () => void) => { fn(); setOlder([]); setNextBefore(null); };
  const loadOlder = async () => {
    if (cursor == null) return;
    const page = await apiGet<{ events: GEvent[]; next_before: number | null }>(`/guardian/events?${query}&before=${cursor}`);
    setOlder((prev) => [...prev, ...page.events]);
    setNextBefore(page.next_before);
  };

  return (
    <Card title="Activity" subtitle="Every event Guardian recorded, as it was observed">
      <div className="jr-filters gd-filters">
        <label className="field">
          <span className="field-label">Component</span>
          <select aria-label="Component" value={component} onChange={(e) => reset(() => setComponent(e.target.value))}>
            <option value="">All</option>
            {status.components.map((c) => <option key={c.id} value={c.id}>{c.label} ({c.id})</option>)}
            {component && !status.components.some((c) => c.id === component) && <option value={component}>{component}</option>}
          </select>
        </label>
        <label className="field">
          <span className="field-label">At least</span>
          <select aria-label="Minimum severity" value={severity} onChange={(e) => reset(() => setSeverity(e.target.value))}>
            <option value="">Any severity</option>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="field">
          <span className="field-label">Category</span>
          <select aria-label="Category" value={category} onChange={(e) => reset(() => setCategory(e.target.value))}>
            <option value="">All</option>
            {CATEGORIES.map((c) => <option key={c} value={c}>{c.replace(/_/g, " ")}</option>)}
          </select>
        </label>
      </div>
      {live.error && <p className="neg">Could not load events: {live.error}</p>}
      {rows.length === 0 ? <p className="dim">No events match.</p> : (
        <div className="tablewrap">
          <table className="data-table gd-events" data-testid="guardian-events">
            <thead><tr><th>Time</th><th>Severity</th><th>Event</th><th>Component</th><th>Market</th><th>State</th><th>Reason</th></tr></thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.event_id}>
                  <td className="mono dim">{clock(e.timestamp)}</td>
                  <td><Badge text={e.severity} tone={SEVERITY_TONE[e.severity]} /></td>
                  <td>{e.event_type.replace(/_/g, " ")}</td>
                  <td className="mono">{e.source_component}</td>
                  <td className="mono">{[e.symbol, e.timeframe].filter(Boolean).join(" ") || "—"}</td>
                  <td className="mono">{e.state_before || e.state_after ? `${e.state_before ?? "—"} → ${e.state_after ?? "—"}` : "—"}</td>
                  <td className="gd-reason">{e.reason ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cursor != null && <button type="button" className="btn btn-soft btn-sm" onClick={() => void loadOlder()}>Load older</button>}
    </Card>
  );
}
