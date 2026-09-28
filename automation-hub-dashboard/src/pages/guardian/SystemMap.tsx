import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { ago, type GComponent, type GuardianStatus, HEALTH_TONE } from "./common";

// Left to right, the way data flows: the exchange, each consumer's feed, the
// consumers, and what they write to.
const COLUMNS: { title: string; kinds: string[] }[] = [
  { title: "Market data", kinds: ["upstream"] },
  { title: "Feeds", kinds: ["feed"] },
  { title: "Consumers", kinds: ["instance", "lab"] },
  { title: "Records", kinds: ["database", "journal"] },
  { title: "Observer", kinds: ["guardian"] },
];

function Node({ c, onOpen }: { c: GComponent; onOpen: (id: string) => void }) {
  return (
    <button type="button" className={`gd-node gd-${c.effective.toLowerCase()}`} onClick={() => onOpen(c.id)}
      aria-label={`${c.label}: ${c.effective}. Open its activity`} data-testid={`guardian-node-${c.id}`}>
      <span className="gd-node-head">
        <span className="gd-node-label">{c.label}</span>
        <Badge text={c.effective} tone={HEALTH_TONE[c.effective]} />
      </span>
      <span className="gd-node-detail">{c.detail}</span>
      {c.blocked_by.length > 0 && <span className="gd-node-blocked">Blocked by {c.blocked_by.join(", ")}</span>}
      {c.depends_on.length > 0 && <span className="gd-node-deps dim">Depends on {c.depends_on.join(", ")}</span>}
      <span className="gd-node-seen dim">Observed {ago(c.observed_at)}</span>
    </button>
  );
}

/** The dependency map: every component Guardian observes, with the state it
 *  reports itself and the state it is in once its dependencies are counted. */
export default function SystemMap({ status }: { status: GuardianStatus }) {
  const open = (id: string) => { window.location.hash = `/guardian?tab=activity&component=${encodeURIComponent(id)}`; };
  return (
    <Card title="Live system map" subtitle="Click a component for its activity. A component whose dependency failed reads BLOCKED and names it.">
      <div className="gd-map">
        {COLUMNS.map((col) => {
          const nodes = status.components.filter((c) => col.kinds.includes(c.kind));
          return (
            <div key={col.title} className="gd-col">
              <h4 className="gd-col-title">{col.title}</h4>
              {nodes.length ? nodes.map((c) => <Node key={c.id} c={c} onOpen={open} />)
                : <p className="dim gd-empty">Nothing running here.</p>}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
