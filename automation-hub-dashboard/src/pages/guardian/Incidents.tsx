import { useState } from "react";
import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import {
  ago, clock, CONFIDENCE_TONE, type Incident, type IncidentDetail, INCIDENT_TONE, SEVERITY_TONE,
} from "./common";

const FILTERS = [{ id: "active", label: "Open" }, { id: "CLOSED", label: "Closed" }, { id: "", label: "All" }];

/** Symptom → affected → upstream → root cause → evidence → confidence →
 *  recommended action (PRD §11), then the reconstructed timeline (§10). */
function IncidentView({ id, onClose }: { id: number; onClose: () => void }) {
  const live = useLive<IncidentDetail>(`/guardian/incidents/${id}`, 10000);
  const inc = live.data;
  return (
    <Card title={inc ? `Incident #${inc.id}` : "Incident"} subtitle={inc?.title}
      right={<button type="button" className="btn btn-soft btn-sm" onClick={onClose}>Close</button>}>
      {live.error && <p className="neg">Could not load the incident: {live.error}</p>}
      {inc && (
        <div data-testid="guardian-incident">
          <div className="gd-trace-head">
            <Badge text={inc.state} tone={INCIDENT_TONE[inc.state] ?? "default"} />
            <Badge text={inc.severity} tone={SEVERITY_TONE[inc.severity] ?? "default"} />
            <span className="mono dim">{inc.kind.replace(/_/g, " ")}</span>
          </div>
          <ol className="gd-chain" data-testid="guardian-diagnosis">
            <li><span>Symptom</span><b>{inc.diagnosis.symptom}</b></li>
            <li><span>Affected</span><b className="mono">{inc.affected.join(", ") || Object.keys(inc.signals).join(", ") || "—"}</b></li>
            <li><span>Upstream / root component</span><b className="mono">{inc.root_component}</b></li>
            <li><span>Root-cause candidate</span><b>{inc.diagnosis.root_cause}</b></li>
            <li><span>Supporting evidence</span>
              {inc.diagnosis.evidence.length ? <ul>{inc.diagnosis.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul> : <b>—</b>}</li>
            <li><span>Confidence</span><b><Badge text={inc.diagnosis.confidence} tone={CONFIDENCE_TONE[inc.diagnosis.confidence] ?? "default"} /> {inc.diagnosis.why_this_confidence}</b></li>
            <li><span>Recommended action</span><b>{inc.diagnosis.recommended_action} <span className="dim">(advice only — Guardian takes no action)</span></b></li>
          </ol>
          <ul className="gd-kv gd-incident-times">
            <li><span>Started</span><b className="mono">{clock(inc.started_at)}</b></li>
            <li><span>Detected</span><b className="mono">{clock(inc.detected_at)}</b></li>
            <li><span>Recovered</span><b className="mono">{clock(inc.recovered_at)}</b></li>
            <li><span>Verified</span><b className="mono">{clock(inc.verified_at)}</b></li>
            <li><span>Closed</span><b className="mono">{clock(inc.closed_at)}</b></li>
            <li><span>Updates</span><b className="mono">{inc.updates}</b></li>
          </ul>
          {inc.related.length > 0 && (
            <p className="dim">Related: {inc.related.map((r) => `#${r.incident_id} (${r.confidence}: ${r.why})`).join("; ")}</p>
          )}
          <h4 className="gd-sub">Timeline</h4>
          <div className="tablewrap">
            <table className="data-table gd-trace" data-testid="guardian-timeline">
              <thead><tr><th>Time</th><th>Source</th><th>What</th><th>State</th><th>Detail</th></tr></thead>
              <tbody>
                {inc.timeline.map((t, i) => (
                  <tr key={`${t.at}-${i}`} className={t.source.startsWith("incident:") ? "gd-trace-own" : ""}>
                    <td className="mono dim">{clock(t.at)}</td>
                    <td className="mono">{t.source}</td>
                    <td>{t.what.replace(/_/g, " ")}</td>
                    <td className="mono">{t.state ?? ""}</td>
                    <td className="gd-reason dim">{t.detail ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Card>
  );
}

/** One incident per outage, however many components it touched. Incidents
 *  are never erased: a closed one stays as history. */
export default function Incidents() {
  const [filter, setFilter] = useState("active");
  const [openId, setOpenId] = useState<number | null>(null);
  const list = useLive<{ incidents: Incident[]; counts: Record<string, number> }>(
    `/guardian/incidents${filter ? `?state=${filter}` : ""}`, 8000);
  const counts = list.data?.counts ?? {};
  return (
    <>
      <Card title="Incidents" subtitle="Grouped by root cause; a closed incident is kept, never erased"
        right={
          <div className="gd-window" role="group" aria-label="Filter">
            {FILTERS.map((f) => (
              <button key={f.id} type="button" className={`btn btn-sm ${f.id === filter ? "btn-primary" : "btn-soft"}`}
                onClick={() => setFilter(f.id)}>{f.label}</button>
            ))}
          </div>
        }>
        <p className="dim">Open {counts.OPEN ?? 0} · recovering {counts.RECOVERED ?? 0} · closed {counts.CLOSED ?? 0}</p>
        {list.error && <p className="neg">Incidents are unavailable: {list.error}</p>}
        {list.data?.incidents.length ? (
          <ul className="gd-list gd-incidents" data-testid="guardian-incidents">
            {list.data.incidents.map((i) => (
              <li key={i.id}>
                <Badge text={i.state} tone={INCIDENT_TONE[i.state] ?? "default"} />
                <span className="mono dim">#{i.id} · {ago(i.started_at)}</span>
                <span>
                  <b>{i.title}</b> — {i.affected.length || Object.keys(i.signals).length} affected ·{" "}
                  <Badge text={i.diagnosis.confidence} tone={CONFIDENCE_TONE[i.diagnosis.confidence] ?? "default"} /> {i.diagnosis.root_cause}
                  {i.updates ? ` · ${i.updates} updates` : ""}{" "}
                  <button type="button" className="btn btn-soft btn-sm" onClick={() => setOpenId(i.id)}>Details</button>
                </span>
              </li>
            ))}
          </ul>
        ) : <p className="dim">{filter === "active" ? "No open incidents." : "No incidents recorded."}</p>}
      </Card>
      {openId != null && <IncidentView id={openId} onClose={() => setOpenId(null)} />}
    </>
  );
}
