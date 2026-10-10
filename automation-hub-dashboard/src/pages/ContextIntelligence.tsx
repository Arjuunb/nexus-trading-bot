import { useState } from "react";
import Card from "../components/common/Card";
import { PageHeader } from "../components/common/ui";
import { useLive } from "../lib/api";

type Status = string | { status?: string; label?: string; reasons?: string[]; warnings?: string[] };
type Cohort = {
  strategy_id: string; strategy_version: string | null; config_fingerprint: string | null;
  instance_id: string; lab_id: string | null; simulation_session_id: string | null;
  execution_mode: string; source_kind: string; symbol?: string | null;
};
type ContextGroup = {
  group_key: string;
  group: Record<string, string | null>;
  classifier?: { classifier_id?: string; classifier_version?: string; parameter_hash?: string };
  metrics: { completed_episode_count?: number; win_rate_pct?: string | null; net_profit_factor?: string | null; net_pnl?: string | null };
  evidence_quality?: Status;
  context_quality?: Status;
  cost_coverage?: { status?: string; fees?: Status; funding?: Status; slippage?: Status; verification_blockers?: string[] };
  sample_confidence?: { label?: string; warnings?: string[]; win_rate_interval?: { lower_pct?: string | null; upper_pct?: string | null; method?: string } };
  profitability_verified?: boolean;
  profitability?: { status?: string; verified?: boolean; observed_direction?: string; blockers?: string[] };
};
type ContextSnapshot = {
  snapshot_id?: string; episode_id?: string; trade_id?: string;
  symbol?: string; entry_timeframe?: string; higher_timeframe?: string | null;
  session?: string; trend_regime?: string; volatility_regime?: string; structure_regime?: string;
  signal_timestamp?: string | null; entry_timestamp?: string | null;
  last_closed_candle_timestamp?: string | null;
  context_quality?: Status; evidence_quality?: Status;
  classifier_id?: string; classifier_version?: string; parameter_hash?: string;
};
type Envelope = {
  contract_version: string; calculation_timestamp: string | null;
  cache_status?: string; evidence_quality?: Status;
  error_reason?: string | null; supported_groupings?: string[][];
  cohorts?: Cohort[]; groups?: ContextGroup[]; contexts?: ContextSnapshot[];
};

const base = "/api/v2/strategy-intelligence";
const status = (value?: Status) => typeof value === "string" ? value : value?.status ?? value?.label ?? "UNKNOWN";
const text = (value?: string | number | null) => value == null || value === "" ? "Unavailable" : String(value);
// Preserve financial Decimal text. The presentation layer never recomputes P&L.
const decimal = (value?: string | null, suffix = "") => value == null ? "Unavailable" : `${value}${suffix}`;
const groupings = [
  ["symbol,session,trend_regime", "Asset × Session × Trend"],
  ["symbol", "Asset"], ["session", "Session"], ["trend_regime", "Trend regime"],
  ["volatility_regime", "Volatility"], ["symbol,session", "Asset × Session"],
  ["symbol,trend_regime", "Asset × Trend"], ["entry_timeframe,direction", "Timeframe × Direction"],
  ["direction", "Direction"], ["entry_timeframe", "Timeframe"], ["symbol,volatility_regime", "Asset × Volatility"],
];

function verificationReady(group: ContextGroup) {
  const covered = (value?: Status) => ["COMPLETE", "NOT_APPLICABLE"].includes(status(value));
  return (group.profitability_verified ?? group.profitability?.verified) === true
    && group.profitability?.verified !== false && status(group.evidence_quality) === "COMPLETE"
    && group.cost_coverage?.status === "COMPLETE" && covered(group.cost_coverage.fees)
    && covered(group.cost_coverage.funding) && covered(group.cost_coverage.slippage);
}

function cohortKey(cohort: Cohort) {
  return JSON.stringify([cohort.strategy_id, cohort.strategy_version, cohort.config_fingerprint,
    cohort.instance_id, cohort.lab_id, cohort.simulation_session_id, cohort.execution_mode, cohort.source_kind, cohort.symbol ?? null]);
}
function query(cohort: Cohort) {
  const params = new URLSearchParams();
  for (const key of ["strategy_id", "strategy_version", "config_fingerprint", "instance_id", "lab_id", "simulation_session_id", "execution_mode", "source_kind", "symbol"] as const) {
    const value = cohort[key];
    if (value != null) params.set(key, value);
  }
  return params;
}

function GroupExplanation({ group }: { group: ContextGroup }) {
  const reasons = [
    ...(typeof group.evidence_quality === "object" ? group.evidence_quality.reasons ?? [] : []),
    ...(group.cost_coverage?.verification_blockers ?? []),
    ...(group.profitability?.blockers ?? []),
    ...(group.sample_confidence?.warnings ?? []),
  ];
  return <details style={{ maxWidth: 340, whiteSpace: "normal" }}>
    <summary>Explain status</summary>
    <p>Context quality: {status(group.context_quality)}</p>
    <p>Fees: {status(group.cost_coverage?.fees)}</p>
    <p>Funding: {status(group.cost_coverage?.funding)}</p>
    <p>Slippage: {status(group.cost_coverage?.slippage)}</p>
    <p>Observed result: {text(group.profitability?.observed_direction)}.</p>
    {group.sample_confidence?.win_rate_interval && <p>{text(group.sample_confidence.win_rate_interval.method)} win-rate interval: {decimal(group.sample_confidence.win_rate_interval.lower_pct, "%")} to {decimal(group.sample_confidence.win_rate_interval.upper_pct, "%")}.</p>}
    {[...new Set(reasons)].map((reason) => <p key={reason}>{reason}</p>)}
    <p>Classifier: {text(group.classifier?.classifier_id)} {text(group.classifier?.classifier_version)}</p>
    <p style={{ overflowWrap: "anywhere" }}>Parameter hash: {text(group.classifier?.parameter_hash)}</p>
  </details>;
}

function SelectedCohort({ cohort, grouping }: { cohort: Cohort; grouping: string }) {
  const params = query(cohort);
  const context = useLive<Envelope>(`${base}/context?${params.toString()}`, 30000);
  params.set("group_by", grouping);
  const performance = useLive<Envelope>(`${base}/performance?${params.toString()}`, 30000);
  const data = performance.data;
  const unsupported = data && data.contract_version !== "strategy_intelligence.v2";
  const groups = unsupported ? [] : data?.groups ?? [];
  const snapshots = context.data?.contract_version === "strategy_intelligence.v2" ? context.data.contexts ?? [] : [];

  return <>
    <Card title="Evidence identity">
      <div style={{ overflowWrap: "anywhere" }}>
        <p>Strategy: {cohort.strategy_id} · Version: {text(cohort.strategy_version)}</p>
        <p>Configuration fingerprint: {text(cohort.config_fingerprint)}</p>
        <p>Instance: {cohort.instance_id} · Lab: {text(cohort.lab_id)} · Session: {text(cohort.simulation_session_id)}</p>
        <p>Execution: {cohort.execution_mode} · Source: {cohort.source_kind}</p>
        <p>Contract: {text(data?.contract_version)} · Calculated: {text(data?.calculation_timestamp)}</p>
        <p>Evidence assessment: {status(data?.evidence_quality)}</p>
      </div>
    </Card>
    <Card title="Performance by Context" subtitle="One completed episode is one trade; partial exits remain within the episode.">
      {performance.loading && !data && <p role="status">Loading intelligence evidence…</p>}
      {performance.error && <p role="alert">Intelligence unavailable: {performance.error}. <button type="button" className="btn btn-soft btn-sm" onClick={() => void performance.refetch()}>Retry performance</button></p>}
      {!performance.error && data?.cache_status === "ERROR" && <p role="alert">{data.error_reason ?? "Intelligence cache unavailable"}. <button type="button" className="btn btn-soft btn-sm" onClick={() => void performance.refetch()}>Retry performance</button></p>}
      {data?.cache_status === "CONFLICTED" && <p role="alert">Evidence cache identity conflict. Results require reconciliation; profitability verification is unavailable.</p>}
      {unsupported && <p role="alert">Unsupported intelligence contract. Results are unavailable.</p>}
      {data?.cache_status === "PENDING" && <p role="status">Intelligence calculation pending.</p>}
      {data?.cache_status === "STALE" && <p role="status">Cached intelligence is stale. Profitability verification is unavailable.</p>}
      {data?.cache_status === "RECONCILIATION_REQUIRED" && <p role="status">Evidence reconciliation is required. Profitability verification is unavailable.</p>}
      <p className="dim">Sample labels describe evidence volume. Each subgroup has its own evidence, cost coverage and sample assessment.</p>
      {!groups.length && !performance.loading && <p>No completed episode groups available.</p>}
      {!!groups.length && <div className="tablewrap"><table aria-label="Performance by context"><thead><tr>
        <th>Market context</th><th>Completed episodes</th><th>Win rate</th><th>Net profit factor</th><th>Booked net P&amp;L</th><th>Evidence quality</th><th>Cost coverage</th><th>Sample confidence</th><th>Profitability</th><th>Explanation</th>
      </tr></thead><tbody>{groups.map((group) => <tr key={group.group_key}>
        <td>{Object.entries(group.group).map(([key, value]) => <div key={key}><span className="dim">{key.replace(/_/g, " ")}: </span><span>{text(value)}</span></div>)}</td>
        <td>{text(group.metrics.completed_episode_count)}</td><td>{decimal(group.metrics.win_rate_pct, "%")}</td>
        <td>{decimal(group.metrics.net_profit_factor)}</td><td>{decimal(group.metrics.net_pnl)}</td>
        <td>{status(group.evidence_quality)}</td><td>{text(group.cost_coverage?.status)}</td><td>{text(group.sample_confidence?.label)}</td>
        <td>{!performance.error && data?.cache_status === "READY" && verificationReady(group) ? "VERIFIED" : "UNVERIFIED"}</td>
        <td><GroupExplanation group={group} /></td>
      </tr>)}</tbody></table></div>}
    </Card>
    <Card title="Market Context" subtitle="Immutable classifications use evidence available at signal time.">
      {context.error && <p role="alert">Context unavailable: {context.error}. <button type="button" className="btn btn-soft btn-sm" onClick={() => void context.refetch()}>Retry context</button></p>}
      {!context.error && context.data?.cache_status === "ERROR" && <p role="alert">{context.data.error_reason ?? "Context cache unavailable"}. <button type="button" className="btn btn-soft btn-sm" onClick={() => void context.refetch()}>Retry context</button></p>}
      {context.data?.cache_status === "CONFLICTED" && <p role="alert">Evidence cache identity conflict. Context results require reconciliation.</p>}
      {context.loading && !context.data && <p role="status">Loading context snapshots…</p>}
      {!snapshots.length && !context.loading && <p>No market context snapshots available.</p>}
      {!!snapshots.length && <div className="tablewrap"><table aria-label="Market context snapshots"><thead><tr>
        <th>Episode / trade</th><th>Asset / timeframe</th><th>Signal / entry time</th><th>Last closed candle</th><th>Session</th><th>Trend regime</th><th>Volatility</th><th>Structure</th><th>Context quality</th><th>Evidence quality</th><th>Classifier</th>
      </tr></thead><tbody>{snapshots.map((snapshot, index) => <tr key={snapshot.snapshot_id ?? `${snapshot.episode_id}:${snapshot.classifier_version}:${index}`}>
        <td>{text(snapshot.episode_id)}<br />{text(snapshot.trade_id)}</td><td>{text(snapshot.symbol)}<br />{text(snapshot.entry_timeframe)} / {text(snapshot.higher_timeframe)}</td>
        <td>{text(snapshot.signal_timestamp)}<br />{text(snapshot.entry_timestamp)}</td><td>{text(snapshot.last_closed_candle_timestamp)}</td>
        <td>{text(snapshot.session)}</td><td>{text(snapshot.trend_regime)}</td><td>{text(snapshot.volatility_regime)}</td><td>{text(snapshot.structure_regime)}</td>
        <td>{status(snapshot.context_quality)}</td><td>{status(snapshot.evidence_quality)}</td><td>{text(snapshot.classifier_id)}<br />{text(snapshot.classifier_version)}</td>
      </tr>)}</tbody></table></div>}
    </Card>
  </>;
}

export default function ContextIntelligence() {
  const discovery = useLive<Envelope>(`${base}/cohorts`, 30000);
  const [selected, setSelected] = useState("");
  const [grouping, setGrouping] = useState(groupings[0][0]);
  const cohorts = discovery.data?.contract_version === "strategy_intelligence.v2" ? (discovery.data.cohorts ?? []).filter((cohort) => !!cohort.instance_id) : [];
  const cohort = cohorts.find((item) => cohortKey(item) === selected);
  const availableGroupings = discovery.data?.supported_groupings
    ? groupings.filter(([value]) => discovery.data!.supported_groupings!.some((dimensions) => dimensions.join(",") === value)) : groupings;
  return <>
    <PageHeader title="Context Intelligence" subtitle="Inspect strategy_intelligence.v2 evidence by exact cohort and entry-time market context." />
    <Card title="Select evidence">
      {discovery.error && <p role="alert">Cohort discovery unavailable: {discovery.error}. <button type="button" className="btn btn-soft btn-sm" onClick={() => void discovery.refetch()}>Retry cohort discovery</button></p>}
      {!discovery.error && discovery.data?.cache_status === "ERROR" && <p role="alert">{discovery.data.error_reason ?? "Cohort cache unavailable"}. <button type="button" className="btn btn-soft btn-sm" onClick={() => void discovery.refetch()}>Retry cohort discovery</button></p>}
      {discovery.data?.cache_status === "CONFLICTED" && <p role="alert">Evidence cache identity conflict. Cohort results require reconciliation.</p>}
      {discovery.data && discovery.data.contract_version !== "strategy_intelligence.v2" && <p role="alert">Unsupported intelligence contract. Cohorts are unavailable.</p>}
      <div className="form-grid-2">
        <label className="field"><span className="field-label">Evidence cohort</span><select value={selected} onChange={(event) => setSelected(event.target.value)} aria-label="Evidence cohort" style={{ minWidth: 0, width: "100%" }}>
          <option value="">Select an observed cohort</option>
          {cohorts.map((item) => <option key={cohortKey(item)} value={cohortKey(item)}>{item.strategy_id} {text(item.strategy_version)} · {item.instance_id} · {item.execution_mode} · {item.config_fingerprint?.slice(0, 12) ?? "unknown configuration"} · {text(item.simulation_session_id)}</option>)}
        </select></label>
        <label className="field"><span className="field-label">Performance grouping</span><select value={grouping} onChange={(event) => setGrouping(event.target.value)} aria-label="Performance grouping">
          {availableGroupings.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      </div>
      {!cohorts.length && !discovery.loading && !discovery.error && <p>No observed strategy cohorts available.</p>}
      {!cohort && <p className="dim">Select one exact strategy version, configuration and execution cohort to inspect its evidence.</p>}
    </Card>
    {cohort && <SelectedCohort key={`${selected}:${grouping}`} cohort={cohort} grouping={grouping} />}
  </>;
}
