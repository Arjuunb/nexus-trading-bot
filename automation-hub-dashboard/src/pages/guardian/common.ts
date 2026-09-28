// Guardian's shapes (automation-hub/routers/guardian.py) and how they read.

export type HealthState = "HEALTHY" | "DEGRADED" | "BLOCKED" | "FAILED" | "UNKNOWN";
export type Severity = "INFO" | "WATCH" | "WARNING" | "HIGH" | "CRITICAL";
export type Tone = "green" | "red" | "amber" | "blue" | "purple" | "gold" | "default";

export interface GComponent {
  id: string; label: string; kind: string; raw: HealthState; effective: HealthState;
  detail: string; depends_on: string[]; blocked_by: string[]; observed_at: string | null;
  facts: Record<string, unknown>;
}

export interface GroupSummary { state: HealthState; total: number; healthy: number }

export interface BusStats {
  running: boolean; capacity: number; backlog: number; published: number; stored: number;
  dropped: number; rejected: number; store_failures: number; last_store_error: string | null;
  last_stored_at: string | null; last_delay_ms: number; max_delay_ms: number;
}

export interface GuardianStatus {
  generated_at: string;
  summary: { state: HealthState; groups: Record<string, GroupSummary | null>; counts: Record<HealthState, number> };
  components: GComponent[];
  self: {
    running: boolean; heartbeat_age_s: number | null; interval_s: number; cycles: number;
    last_cycle_ms: number | null; bus: BusStats; collectors_failing: Record<string, string>;
  };
  events_24h: { total: number; warning_or_worse: number };
  boundary: { mode: string; may_change: string[]; never_changes: string[] };
}

export interface GEvent {
  seq: number; event_id: string; timestamp: string; received_at: string;
  source_service: string; source_component: string; instance_id: string | null; lab_id: string | null;
  strategy_id: string | null; symbol: string | null; timeframe: string | null;
  event_type: string; category: string; severity: Severity;
  state_before: string | null; state_after: string | null; reason: string | null;
  evidence: unknown; metadata: unknown;
}

export const HEALTH_TONE: Record<HealthState, Tone> = {
  HEALTHY: "green", DEGRADED: "amber", BLOCKED: "blue", FAILED: "red", UNKNOWN: "default",
};

export const SEVERITY_TONE: Record<Severity, Tone> = {
  INFO: "default", WATCH: "blue", WARNING: "amber", HIGH: "red", CRITICAL: "red",
};

export const KIND_LABEL: Record<string, string> = {
  upstream: "Market data source", feed: "Market feeds", instance: "Trading instances", lab: "Labs",
  database: "Ledger database", journal: "Trade journal", guardian: "Guardian",
};

/** One sentence that answers "is my platform behaving correctly?" from the
 *  components alone -- it never says more than their states do. */
export function verdict(components: GComponent[]): string {
  const by = (s: HealthState) => components.filter((c) => c.effective === s);
  const names = (list: GComponent[]) => list.slice(0, 3).map((c) => c.label).join(", ") + (list.length > 3 ? ` and ${list.length - 3} more` : "");
  const failed = by("FAILED"), blocked = by("BLOCKED"), degraded = by("DEGRADED"), unknown = by("UNKNOWN");
  if (!components.length) return "Guardian has not observed the platform yet.";
  if (failed.length) return `${failed.length} component${failed.length > 1 ? "s have" : " has"} failed: ${names(failed)}.`;
  if (blocked.length) return `${blocked.length} component${blocked.length > 1 ? "s are" : " is"} waiting on a failed dependency: ${names(blocked)}. They are not broken themselves.`;
  if (degraded.length) return `Working, with ${degraded.length} degraded: ${names(degraded)}.`;
  if (unknown.length) return `Working where Guardian can see; it cannot yet judge ${names(unknown)}.`;
  return "Everything Guardian can see is working.";
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(s)) return "—";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
