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
  incidents?: { counts: Record<string, number>; active: Incident[] };
  anomalies?: Anomaly[];
  integrity?: { at: string; findings: number; worst: Severity | null; paper_open_risk: number;
    paper_positions: number; live_positions: number; live_routing_locked: boolean | null } | null;
  boundary: { mode: string; may_change: string[]; never_changes: string[] };
}

export interface GEvent {
  seq: number; event_id: string; timestamp: string; received_at: string;
  source_service: string; source_component: string; instance_id: string | null; lab_id: string | null;
  strategy_id: string | null; symbol: string | null; timeframe: string | null;
  event_type: string; category: string; severity: Severity; decision?: string | null;
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

// ---- Phase 2: strategy telemetry (routers/guardian.py /guardian/strategies)

export interface TraceCondition {
  id: string | null; label: string | null; stage: string; kind: "market_data" | "strategy" | "gate";
  state: string; code: string | null; detail: string | null;
}

export interface Trace {
  final: string; direction: string | null; blocker_code: string | null; reason: string | null;
  candle_time?: string | null; stage_reached?: string; data?: string;
  conditions: TraceCondition[]; blocking: { id: string | null; code: string | null; detail: string | null }[];
  quality?: { score: number | null; min_score: number | null; hard_blocks: string[]; passed: string[]; weak: string[] } | null;
}

export interface StrategyPerformance {
  record_source: string; record_origin: string; strategy_id: string | null; instance_id: string | null;
  lab_id: string | null; trades: number; wins: number; losses: number; net_pnl: number | null;
  expectancy: number | null; average_r: number | null; profit_factor: number | null;
  max_drawdown_r: number | null; r_measured: number; last_closed_at: string | null;
}

export interface StrategyCard {
  scope: string; lab_id: string | null; instance_id: string | null; strategy_id: string | null;
  strategy_version: string | null; symbol: string | null; timeframe: string | null;
  evaluations: number; setups: number; entries: number; refused: number; no_setup: number;
  decisions: Record<string, number>; top_rejection_reasons: { decision: string; code: string; count: number }[];
  almost_trades: number; last_evaluation_at: string | null; performance: StrategyPerformance[];
}

export interface StrategiesView {
  days: number; since_day: string; strategies: StrategyCard[]; almost_trades_total: number;
  telemetry: Record<string, { ok: boolean; read?: number; written?: number; error?: string; at: string }>;
  performance: StrategyPerformance[]; performance_error: string | null;
  research: { built: boolean; note: string };
}

export interface AlmostTrade {
  identity: string; first_event_id: string; last_event_id: string; first_seen: string; last_seen: string;
  sightings: number; source_component: string; strategy_id: string | null; strategy_version: string | null;
  symbol: string | null; timeframe: string | null; direction: string | null; kind: string;
  classification: string; passed: number; evaluated: number;
  prevented_by: { condition: string | null; code: string | null; detail: string | null };
  conditions: TraceCondition[] | null; note: string;
}

export const CONDITION_TONE: Record<string, Tone> = {
  PASS: "green", FAIL: "red", HELD: "amber", BYPASSED: "amber", NOT_REACHED: "default",
  NOT_APPLICABLE: "default", NOT_REQUIRED: "default", UNATTRIBUTED: "purple",
};

export const FINAL_TONE: Record<string, Tone> = {
  ENTERED: "green", ORDER_PENDING: "blue", APPROVAL_REQUIRED: "blue", SIGNAL_ONLY: "blue",
  SIGNAL: "blue", SETUP_PENDING: "blue", REJECTED: "amber", MISSED: "amber", NO_SETUP: "default",
  ERROR: "red", EXECUTION_UNCERTAIN: "red",
};

// ---- Phase 3: incidents and anomalies

export interface Diagnosis {
  kind: string; symptom: string; root_cause: string; confidence: string;
  why_this_confidence: string; evidence: string[]; recommended_action: string;
}

export interface Incident {
  id: number; key: string; title: string; kind: string; state: "OPEN" | "RECOVERED" | "CLOSED";
  severity: Severity; root_component: string; diagnosis: Diagnosis; affected: string[];
  signals: Record<string, { event_type: string; timestamp: string; reason: string | null }>;
  related: { incident_id: number; title: string; confidence: string; why: string }[];
  started_at: string; detected_at: string; recovered_at: string | null; verified_at: string | null;
  closed_at: string | null; updates: number; last_update_at: string;
}

export interface IncidentDetail extends Incident {
  log: { seq: number; at: string; entry: string; detail: string }[];
  timeline: { at: string; source: string; what: string; severity: string; state: string | null;
    detail: string | null; event_id: string | null }[];
}

export interface Anomaly {
  key: string; detector: string; scope: string; value: number; unit: string; baseline: string;
  detail: string; note: string; since: string;
}

export const INCIDENT_TONE: Record<string, Tone> = { OPEN: "red", RECOVERED: "amber", CLOSED: "green" };

export const CONFIDENCE_TONE: Record<string, Tone> = {
  CONFIRMED: "green", "HIGH CONFIDENCE": "blue", PROBABLE: "amber", POSSIBLE: "purple", UNKNOWN: "default",
};

// ---- Phase 4: integrity and exposure

export interface ExposureTotals {
  positions: number; notional: number; risk: number; risk_unknown: number;
  by_symbol: { symbol: string; side: string; positions: number; notional: number; risk: number;
    risk_unknown: number; accounts: string[] }[];
  by_cluster: Record<string, { long: number; short: number; net: number; positions: number }>;
}

export interface IntegrityReport {
  at: string; journal_checked: boolean; errors: Record<string, string>;
  sources: Record<string, Record<string, unknown>>;
  findings: { rule: string; source: string; item: string; detail: string; severity: Severity; meaning: string }[];
  exposure: { paper: ExposureTotals; live: ExposureTotals & { routing_locked: boolean | null }; note: string;
    positions: Record<string, unknown>[] };
}
