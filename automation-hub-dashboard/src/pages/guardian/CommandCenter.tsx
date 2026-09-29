import Card from "../../components/common/Card";
import { Badge, StatCard } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import {
  ago, clock, CONFIDENCE_TONE, type GEvent, type GuardianStatus, HEALTH_TONE, INCIDENT_TONE, KIND_LABEL,
  SEVERITY_TONE, verdict,
} from "./common";

const GROUP_ORDER = ["instance", "lab", "feed", "upstream", "database", "journal", "guardian"];

/** "Is my trading platform behaving correctly?" -- answered from Guardian's
 *  observed component states, then the evidence behind the answer. */
export default function CommandCenter({ status }: { status: GuardianStatus }) {
  const notable = useLive<{ events: GEvent[] }>("/guardian/events?min_severity=WARNING&limit=8", 10000);
  const { summary, self, events_24h: day } = status;
  const bus = self.bus;
  return (
    <>
      <div className={`gd-hero gd-${summary.state.toLowerCase()}`} data-testid="guardian-headline">
        <div>
          <span className="gd-hero-label">SYSTEM STATUS</span>
          <div className="gd-hero-state"><span className="gd-dot" aria-hidden /> {summary.state}</div>
          <p className="gd-hero-verdict">{verdict(status.components)}</p>
        </div>
        <div className="gd-hero-meta dim">
          <span>Observed {ago(status.generated_at)}</span>
          <span>Guardian heartbeat {self.heartbeat_age_s == null ? "—" : `${self.heartbeat_age_s}s ago`}</span>
        </div>
      </div>

      <Card title="Platform" subtitle="Each group's worst component. BLOCKED means waiting on a failed dependency, not broken itself.">
        <div className="gd-groups">
          {GROUP_ORDER.map((kind) => {
            const g = summary.groups[kind];
            if (!g) return null;
            return (
              <div key={kind} className="gd-group" data-testid={`guardian-group-${kind}`}>
                <span className="gd-group-name">{KIND_LABEL[kind] ?? kind}</span>
                <span className="mono">{g.healthy}/{g.total}</span>
                <Badge text={g.state} tone={HEALTH_TONE[g.state]} />
              </div>
            );
          })}
        </div>
      </Card>

      <div className="grid-2-eq">
        <Card title="Open incidents" subtitle="One per outage, grouped by root cause">
          {status.incidents?.active.length ? (
            <ul className="gd-list" data-testid="guardian-open-incidents">
              {status.incidents.active.map((i) => (
                <li key={i.id}>
                  <Badge text={i.state} tone={INCIDENT_TONE[i.state] ?? "default"} />
                  <span className="mono dim">#{i.id} · {ago(i.started_at)}</span>
                  <span><b>{i.title}</b> · <Badge text={i.diagnosis.confidence} tone={CONFIDENCE_TONE[i.diagnosis.confidence] ?? "default"} /></span>
                </li>
              ))}
            </ul>
          ) : <p className="dim">No open incidents.</p>}
          <a className="gd-more" href="#/guardian?tab=incidents">All incidents</a>
        </Card>
        <Card title="Anomalies" subtitle="Deviations from a stream's own normal — not failures">
          {status.anomalies?.length ? (
            <ul className="gd-list" data-testid="guardian-anomalies">
              {status.anomalies.map((a) => (
                <li key={a.key}>
                  <Badge text="WATCH" tone="blue" />
                  <span className="mono dim">{a.scope}</span>
                  <span><b>{a.detector.replace(/_/g, " ")}</b> — {a.detail} <span className="dim">(normal: {a.baseline})</span></span>
                </li>
              ))}
            </ul>
          ) : <p className="dim">Nothing outside its normal range, or not enough history yet to judge.</p>}
        </Card>
      </div>

      <div className="stat-row">
        <StatCard label="Events (24h)" value={String(day.total)} sub="everything Guardian recorded" />
        <StatCard label="Warning or worse (24h)" value={String(day.warning_or_worse)}
          tone={day.warning_or_worse ? "amber" : "default"} />
        <StatCard label="Integrity findings" value={status.integrity ? String(status.integrity.findings) : "—"}
          sub={status.integrity ? `paper open risk ${status.integrity.paper_open_risk}` : "not checked yet"}
          tone={status.integrity?.findings ? "red" : "default"} />
        <StatCard label="Research hypotheses"
          value={status.research ? String(Object.values(status.research.hypotheses).reduce((a, b) => a + b, 0)) : "—"}
          sub={status.research?.hypotheses.RECOMMENDED ? `${status.research.hypotheses.RECOMMENDED} awaiting the owner` : "ideas under test, never changes"} />
        <StatCard label="Events dropped" value={String(bus.dropped)} sub="queue full; counted, never hidden"
          tone={bus.dropped ? "red" : "default"} />
        <StatCard label="Ingest delay" value={`${bus.last_delay_ms} ms`} sub={`worst ${bus.max_delay_ms} ms`} />
        <StatCard label="Backlog" value={String(bus.backlog)} sub={`of ${bus.capacity}`} />
      </div>

      <div className="grid-2-eq">
        <Card title="Recent warnings" subtitle="WARNING, HIGH and CRITICAL events, newest first">
          {notable.data?.events.length ? (
            <ul className="gd-list">
              {notable.data.events.map((e) => (
                <li key={e.event_id}>
                  <Badge text={e.severity} tone={SEVERITY_TONE[e.severity]} />
                  <span className="mono dim">{clock(e.timestamp)}</span>
                  <span><b>{e.event_type.replace(/_/g, " ")}</b> · {e.source_component}{e.reason ? ` — ${e.reason}` : ""}</span>
                </li>
              ))}
            </ul>
          ) : <p className="dim">No warnings recorded.</p>}
        </Card>
        <Card title="Guardian itself" subtitle="Guardian judges its own health; a stopped Guardian reads FAILED">
          <ul className="gd-kv">
            <li><span>Running</span><b>{self.running ? "yes" : "no"}</b></li>
            <li><span>Cycles</span><b className="mono">{self.cycles} (every {self.interval_s}s, last {self.last_cycle_ms ?? "—"} ms)</b></li>
            <li><span>Events stored</span><b className="mono">{bus.stored} of {bus.published} published</b></li>
            <li><span>Rejected as invalid</span><b className="mono">{bus.rejected}</b></li>
            <li><span>Evidence writes</span><b>{bus.last_store_error ? `failing: ${bus.last_store_error}` : "ok"}</b></li>
            <li><span>Collectors failing</span><b>{Object.keys(self.collectors_failing).length ? Object.keys(self.collectors_failing).join(", ") : "none"}</b></li>
          </ul>
          <p className="gd-boundary">
            {status.boundary.may_change.length ? (
              <><Badge text="OWNER-ENABLED RECOVERY" tone="amber" /> Guardian may {status.boundary.may_change.join(", ")}, because the owner enabled it.</>
            ) : <><Badge text="READ-ONLY" tone="blue" /> Guardian observes only.</>}{" "}
            It never changes {status.boundary.never_changes.join(", ")}.
          </p>
        </Card>
      </div>
    </>
  );
}
