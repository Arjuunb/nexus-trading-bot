import { useState } from "react";
import Card from "../components/common/Card";
import { PageHeader } from "../components/common/ui";
import { apiPostJson, useLive } from "../lib/api";

type Cohort = {
  instance_id: string; strategy_id: string; strategy_version: string | null;
  config_fingerprint: string | null; simulation_session_id: string | null;
  execution_mode: string; source_kind: string; lab_id?: string | null;
};
type Metrics = Record<string, string | number | null>;
type Window = { window_size: number; status: string; metrics?: Metrics | null; reasons?: string[] };
type Review = {
  review_id: string; identity: Record<string, unknown>; status: string;
  period?: { start: string; end: string }; current?: { metrics?: Metrics }; previous?: { metrics?: Metrics };
  comparison?: { status: string; expectancy_change_r?: string | null };
  observations?: Record<string, { status?: string; mean?: string | null; sample_size?: number }>;
  groups?: Record<string, { value: string; metrics?: Metrics; pattern_sample_sufficient?: boolean }[]>;
  findings?: { kind: string; episode_ids?: string[] }[];
  proposals?: { proposal_id: string; status: string; question: string; episode_ids?: string[] }[];
  missed_opportunities?: { status: string }; violations?: { status: string };
  review_episode_ids?: string[]; review_trade_ids?: string[]; calculated_at?: string;
};
type Envelope = {
  identity?: Record<string, unknown>; status?: string; cache_status?: string;
  windows?: Window[]; suggested_lifecycle_state?: string; action_status?: string;
  baseline?: Metrics; blocking_reasons?: string[]; error_reason?: string;
  policy?: { version?: string; fingerprint?: string }; rows?: Review[];
};
const base = "/api/v2/strategy-intelligence";
const display = (value: unknown) => value == null || value === "" ? "Unavailable" : String(value);
const exactKeys = ["instance_id", "strategy_id", "strategy_version", "config_fingerprint", "simulation_session_id", "lab_id", "execution_mode", "source_kind"] as const;
const metricColumns = [["completed_episode_count", "Episodes"], ["expectancy_net_r", "Net expectancy R"],
  ["net_profit_factor", "Profit factor"], ["max_drawdown_r", "Drawdown R"], ["net_pnl", "Net P&L"], ["net_r", "Total R"]] as const;
const wraps = { overflowWrap: "anywhere" as const };

function sameCohort(identity: Record<string, unknown> | undefined, request: Record<string, unknown>) {
  return !!identity && [...exactKeys, "symbol"].every(key => (identity[key] ?? null) === (request[key] ?? null));
}

function ReportResults({ request, mode }: { request: Record<string, string | null>; mode: "decay" | "weekly" }) {
  const params = new URLSearchParams();
  Object.entries(request).forEach(([key, value]) => { if (value != null) params.set(key, value); });
  const path = `${base}/${mode === "decay" ? "decay" : "weekly-research"}?${params}`;
  const live = useLive<Envelope>(path, 30000);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [dimension, setDimension] = useState("trend_regime");
  const data = !live.loading && !live.error ? live.data : null;
  const report = data?.rows?.find(row => sameCohort(row.identity, request)
    && (!request.week_start || row.period?.start.slice(0, 10) === request.week_start));
  const currentDecay = data?.cache_status === "READY" && sameCohort(data.identity, request) ? data : null;
  const generate = async () => {
    setBusy(true); setMessage(null); setWriteError(null);
    try {
      await apiPostJson(`${base}/weekly-research`, request);
      setMessage("Report saved. Findings and proposals require review.");
      await live.refetch();
    } catch (error) { setWriteError(error instanceof Error ? error.message : "Report unavailable"); }
    finally { setBusy(false); }
  };
  return <>
    {live.loading && <p role="status">Loading evidence…</p>}
    {live.error && <Card title="Evidence unavailable"><p role="alert">{live.error}</p><button className="btn btn-soft" onClick={() => live.refetch()}>Retry evidence</button></Card>}
    {mode === "decay" ? <Card title="Rolling edge windows" subtitle="Descriptive thresholds compare 20, 50 and 100 final closed episodes with the same frozen historical baseline.">
      <p role="status">{currentDecay?.status ?? "UNKNOWN"}</p>
      <p>Policy: {display(currentDecay?.policy?.version)}</p>
      {currentDecay?.suggested_lifecycle_state && <p>Suggested state: {currentDecay.suggested_lifecycle_state} · {display(currentDecay.action_status)}</p>}
      <p>Execution settings remain unchanged. A pause suggestion requires your review.</p>
      {!currentDecay && !live.loading && <p>Current evidence is unavailable. No decay conclusion can be made.</p>}
      {data?.blocking_reasons?.map(reason => <p key={reason}>{reason}</p>)}
      {data?.error_reason && <p role="alert">{data.error_reason}</p>}
      {!!currentDecay?.windows?.length && <div className="tablewrap"><table className="data-table" aria-label="Rolling edge windows" style={{ minWidth: 800 }}><thead><tr><th>Window</th><th>Status</th>{metricColumns.map(([key, label]) => <th key={key}>{label}</th>)}<th>Reasons</th></tr></thead><tbody>{currentDecay.windows.map(window => <tr key={window.window_size}><td>{window.window_size}</td><td>{window.status}</td>{metricColumns.map(([key]) => <td key={key}>{display(window.metrics?.[key])}</td>)}<td>{window.reasons?.join(", ") || "None recorded"}</td></tr>)}</tbody></table></div>}
      {currentDecay?.baseline && <p>Historical baseline: {display(currentDecay.baseline.completed_episode_count)} episodes · expectancy {display(currentDecay.baseline.expectancy_net_r)} R · PF {display(currentDecay.baseline.net_profit_factor)} · drawdown {display(currentDecay.baseline.max_drawdown_r)} R.</p>}
      <p className="dim">A warning can flag expectancy below half the historical baseline, PF below 1, or drawdown above the policy limit. Continued decay uses two disjoint 20-episode blocks. These thresholds are uncalibrated research rules.</p>
    </Card> : <>
      <Card title="Weekly reports" subtitle="Completed Monday-to-Monday UTC weeks, by actual final close time. Reports are immutable snapshots.">
        <p>The existing evidence refresh loop creates the latest completed week when complete financial evidence is available.</p>
        <button className="btn btn-soft" disabled={busy} onClick={generate}>{busy ? "Generating…" : "Generate weekly report"}</button>
        {message && <p role="status">{message}</p>}{writeError && <p role="alert">{writeError}</p>}
        {!report && !live.loading && <p>No saved report is available for this exact cohort and week.</p>}
        {report && <><p style={wraps}>{report.review_id}</p><p>{display(report.period?.start)} → {display(report.period?.end)} · {report.status}</p>
          <p className="dim">Saved evidence snapshot. Current source freshness has not been revalidated.</p>
          <div className="tablewrap"><table className="data-table" aria-label="Weekly performance comparison"><thead><tr><th>Period</th>{metricColumns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>{[["Reviewed week", report.current?.metrics], ["Previous week", report.previous?.metrics]].map(([label, metrics]) => <tr key={String(label)}><td>{String(label)}</td>{metricColumns.map(([key]) => <td key={key}>{display((metrics as Metrics | undefined)?.[key])}</td>)}</tr>)}</tbody></table></div>
          <p>Comparison: {display(report.comparison?.status)} · expectancy change {display(report.comparison?.expectancy_change_r)} R.</p>
          <p>Missed opportunities: {display(report.missed_opportunities?.status)} · Violations: {display(report.violations?.status)}.</p>
          <p>MAE: {display(report.observations?.mae_r?.status)} · MFE: {display(report.observations?.mfe_r?.status)} · Planned RR: {display(report.observations?.planned_rr?.status)}.</p>
          <label className="field"><span className="field-label">Weekly breakdown</span><select aria-label="Weekly breakdown" value={dimension} onChange={event => setDimension(event.target.value)}>{["trend_regime", "session", "direction", "entry_timeframe", "symbol", "volatility_regime", "setup", "exit_reason"].map(key => <option key={key}>{key}</option>)}</select></label>
          <div className="tablewrap"><table className="data-table" aria-label="Weekly evidence breakdown"><thead><tr><th>Group</th><th>Episodes</th><th>Expectancy R</th><th>Sample</th></tr></thead><tbody>{report.groups?.[dimension]?.map(group => <tr key={group.value}><td>{group.value}</td><td>{display(group.metrics?.completed_episode_count)}</td><td>{display(group.metrics?.expectancy_net_r)}</td><td>{group.pattern_sample_sufficient ? "Descriptive sample" : "Insufficient for pattern claims"}</td></tr>)}</tbody></table></div>
        </>}
      </Card>
      {report && <Card title="Findings and proposals"><p>Proposals are pending approval. Each candidate needs separate testing before any execution change.</p>
        {!report.findings?.length && <p>No recurring pattern met the review criteria.</p>}
        {report.findings?.map((finding, index) => <details key={`${finding.kind}:${index}`}><summary>{finding.kind}</summary><p style={wraps}>Episodes: {finding.episode_ids?.join(", ") || "Unavailable"}</p></details>)}
        {report.proposals?.map(proposal => <div key={proposal.proposal_id}><b>{proposal.status}</b><p>{proposal.question}</p><details><summary>Proposal evidence</summary><p style={wraps}>{proposal.episode_ids?.join(", ")}</p></details></div>)}
        <details><summary>Reviewed episode evidence</summary><p style={wraps}>{report.review_episode_ids?.join(", ") || "Unavailable"}</p></details>
        <details><summary>Original trade records</summary><p style={wraps}>{report.review_trade_ids?.join(", ") || "Unavailable"}</p></details>
      </Card>}
    </>}
  </>;
}

export default function StrategyMonitoring({ mode }: { mode: "decay" | "weekly" }) {
  const discovery = useLive<{ cohorts?: Cohort[] }>(`${base}/cohorts`, 30000);
  const [selected, setSelected] = useState("");
  const [symbol, setSymbol] = useState("");
  const [week, setWeek] = useState("");
  const cohorts = (discovery.data?.cohorts ?? []).filter(c => c.execution_mode === "paper" && c.source_kind === "forward_paper" && c.strategy_version && c.config_fingerprint);
  const cohort = cohorts.find(c => JSON.stringify(c) === selected);
  const request: Record<string, string | null> = {};
  if (cohort) exactKeys.forEach(key => { request[key] = cohort[key] ?? null; });
  request.symbol = symbol.trim().toUpperCase();
  if (mode === "weekly" && week) request.week_start = week;
  return <>
    <PageHeader title={mode === "decay" ? "Edge Monitoring" : "Weekly Research"} subtitle="Frozen strategies, actual paper evidence and proposals for human review." />
    <Card title="Exact evidence cohort"><div className="form-grid-2">
      <label className="field"><span className="field-label">Frozen paper cohort</span><select aria-label="Monitoring cohort" value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select an observed cohort</option>{cohorts.map(c => <option key={JSON.stringify(c)} value={JSON.stringify(c)}>{c.strategy_id} · {c.strategy_version} · {c.instance_id} · {c.simulation_session_id} · {c.config_fingerprint?.slice(0, 10)}</option>)}</select></label>
      <label className="field"><span className="field-label">Asset</span><input aria-label="Monitoring asset" placeholder="Exact observed symbol" value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
      {mode === "weekly" && <label className="field"><span className="field-label">Week starting Monday (UTC)</span><input aria-label="Review week" type="date" value={week} onChange={event => setWeek(event.target.value)} /><span className="dim">Leave blank for the latest completed week.</span></label>}
    </div>{discovery.error && <p role="alert">{discovery.error}</p>}
      {!cohorts.length && !discovery.loading && <p>No frozen forward-paper cohorts are available.</p>}
      <p>Analysis uses completed position episodes. Partial exits stay within their original episode.</p>
    </Card>
    {cohort && request.symbol ? <ReportResults key={`${mode}:${JSON.stringify(request)}`} request={request} mode={mode} /> : <p>Select a cohort and exact asset to inspect evidence.</p>}
  </>;
}
