import { useState } from "react";
import SectionTabs from "../components/common/SectionTabs";
import Card from "../components/common/Card";
import ContextIntelligence from "./ContextIntelligence";
import ShadowStrategyLab from "./ShadowStrategyLab";
import ForwardQualification from "./ForwardQualification";
import IntegrityOperations from "./IntegrityOperations";
import LocalReadiness from "./LocalReadiness";
import StrategyMonitoring from "./StrategyMonitoring";
import StrategyResearchDecision from "./StrategyResearchDecision";
import { PageHeader, StatCard } from "../components/common/ui";
import { apiPostJson, useLive } from "../lib/api";

type Cohort = {
  strategy_id: string; strategy_version: string | null; config_fingerprint: string | null;
  instance_id: string; simulation_session_id: string | null; execution_mode: string; source_kind: string;
  symbol?: string | null;
};
type Envelope = { cache_status?: string; rows?: OverviewRow[]; summary?: Summary; cells?: Cell[]; comparison_status?: string; comparison_reasons?: string[]; error_reason?: string };
type OverviewRow = { cohort: Cohort; completed_episode_count?: number; win_rate_pct?: string | null; profit_factor?: string | null; expectancy_r?: string | null; net_pnl?: string | null; metrics?: { completed_episode_count?: number; net_pnl?: string | null }; evidence_quality?: string | { status?: string }; sample_confidence?: { classification?: string; label?: string }; cost_coverage?: { status?: string }; profitability_verified?: boolean; cache_status?: string };
type Summary = { total_strategies?: number; strategies_under_observation?: number; qualified_strategies?: number; total_completed_episodes?: number; qualification_status?: string; evidence_quality?: string };
type Cell = { x?: string | null; y?: string | null; metric?: string | null; completed_episodes?: number; evidence_quality?: string | { status?: string }; sample_confidence?: { classification?: string; label?: string }; cost_coverage?: { status?: string }; profitability_verified?: boolean };

const base = "/api/v2/strategy-intelligence";
const tabs = [
  ["overview", "Overview"], ["explorer", "Strategy Explorer"], ["edge-map", "Edge Map"],
  ["comparison", "Strategy Comparison"], ["regimes", "Market Regimes"], ["sessions", "Session Analysis"],
  ["edge-score", "EdgeScore"], ["selection", "Strategy Selection"],
  ["evidence", "Trade Evidence"], ["qualification", "Qualification"], ["oos", "Out-of-Sample"],
  ["walk-forward", "Walk-Forward"], ["robustness", "Stress & Monte Carlo"],
  ["validation-history", "Validation History"], ["champion-challenger", "Champion / Challenger"], ["edge-monitoring", "Edge Monitoring"], ["findings", "Research Findings"],
  ["research-agent", "Research Agent"], ["shadow-lab", "Shadow Strategy Lab"],
  ["forward-qualification", "Forward Qualification"],
  ["operations", "Operational Reliability"], ["paper-reconciliation", "Paper Reconciliation"],
  ["local-readiness", "Local Readiness"],
] as const;
const text = (value?: string | number | null) => value == null || value === "" ? "Unavailable" : String(value);
const quality = (value?: string | { status?: string }) => typeof value === "string" ? value : value?.status ?? "UNKNOWN";
const cohortParams = (cohort: Cohort) => {
  const params = new URLSearchParams();
  for (const key of ["instance_id", "strategy_id", "strategy_version", "config_fingerprint", "simulation_session_id", "execution_mode", "source_kind", "symbol"] as const) {
    const value = cohort[key];
    if (value != null) params.set(key, value);
  }
  return params;
};

function Overview() {
  const discovery = useLive<{ cohorts?: Cohort[] }>(`${base}/cohorts`, 30000);
  const instanceId = discovery.data?.cohorts?.[0]?.instance_id;
  const overview = useLive<Envelope>(instanceId ? `${base}/overview?instance_id=${encodeURIComponent(instanceId)}` : "", 30000);
  const summary = overview.data?.summary;
  const rows = overview.data?.rows ?? [];
  return <>
    <PageHeader title="Strategy Intelligence" subtitle="Evidence based research over exact strategy cohorts." />
    {!instanceId && !discovery.loading && <Card title="No observed cohorts"><p>No completed strategy evidence is available for this account.</p></Card>}
    {overview.error && <Card title="Analytics unavailable"><p role="alert">{overview.error}</p></Card>}
    {instanceId && <>
      <div className="stat-row">
        <StatCard label="Total strategies" value={text(summary?.total_strategies)} />
        <StatCard label="Under observation" value={text(summary?.strategies_under_observation)} />
        <StatCard label="Qualified" value={summary?.qualification_status === "NOT_AVAILABLE" ? "Not available" : text(summary?.qualified_strategies)} />
        <StatCard label="Completed episodes" value={text(summary?.total_completed_episodes)} />
        <StatCard label="Evidence quality" value={text(summary?.evidence_quality)} />
      </div>
      <Card title="Strategy performance" subtitle="Qualification is never inferred from profitability alone.">
        {!rows.length && <p>No cached strategy results are available.</p>}
        {!!rows.length && <div className="tablewrap"><table aria-label="Strategy performance"><thead><tr><th>Strategy</th><th>Version</th><th>Asset</th><th>Episodes</th><th>Win rate</th><th>Expectancy R</th><th>Net P&amp;L</th><th>Evidence</th><th>Sample</th><th>Cost</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.cohort.strategy_id}:${row.cohort.config_fingerprint}:${row.cohort.symbol}`}><td>{row.cohort.strategy_id}</td><td>{text(row.cohort.strategy_version)}</td><td>{text(row.cohort.symbol)}</td><td>{text(row.completed_episode_count)}</td><td>{text(row.win_rate_pct)}{row.win_rate_pct == null ? "" : "%"}</td><td>{text(row.expectancy_r)}</td><td>{text(row.net_pnl)}</td><td>{quality(row.evidence_quality)}</td><td>{text(row.sample_confidence?.label ?? row.sample_confidence?.classification)}</td><td>{text(row.cost_coverage?.status)}</td></tr>)}</tbody></table></div>}
      </Card>
    </>}
  </>;
}

function EdgeMap() {
  const discovery = useLive<{ cohorts?: Cohort[] }>(`${base}/cohorts`, 30000);
  const cohorts = discovery.data?.cohorts ?? [];
  const [selected, setSelected] = useState("");
  const [metric, setMetric] = useState("expectancy_r");
  const cohort = cohorts.find((item) => JSON.stringify(item) === selected) ?? cohorts[0];
  const params = cohortParams(cohort ?? { instance_id: "", strategy_id: "", strategy_version: null, config_fingerprint: null, simulation_session_id: null, execution_mode: "", source_kind: "" });
  params.set("x_dimension", "session"); params.set("y_dimension", "symbol"); params.set("metric", metric);
  const map = useLive<Envelope>(cohort ? `${base}/edge-map?${params}` : "", 30000);
  const cells = map.data?.cells ?? [];
  return <>
    <PageHeader title="Strategy Edge Map" subtitle="Context cells are colored by evidence quality and sample size, not profit alone." />
    <Card title="Map controls"><div className="form-grid-2"><label className="field"><span className="field-label">Cohort</span><select aria-label="Edge map cohort" value={selected} onChange={(event) => setSelected(event.target.value)}><option value="">{cohort ? `${cohort.strategy_id} · ${text(cohort.symbol)}` : "No observed cohort"}</option>{cohorts.map((item) => <option key={JSON.stringify(item)} value={JSON.stringify(item)}>{item.strategy_id} · {text(item.symbol)} · {item.execution_mode}</option>)}</select></label><label className="field"><span className="field-label">Metric</span><select aria-label="Edge map metric" value={metric} onChange={(event) => setMetric(event.target.value)}><option value="expectancy_r">Expectancy R</option><option value="profit_factor">Profit factor</option><option value="win_rate">Win rate</option><option value="net_pnl">Net P&amp;L</option><option value="completed_episodes">Completed episodes</option></select></label></div></Card>
    <Card title="Session × asset"><p className="dim">Green means the result is verified by complete evidence and costs. Amber or grey states require review.</p>{map.error && <p role="alert">Edge map unavailable: {map.error}</p>}{!cells.length && !map.loading && <p>No cached context cells are available.</p>}{!!cells.length && <div className="tablewrap"><table aria-label="Strategy edge map"><thead><tr><th>Asset</th><th>Session</th><th>Metric</th><th>Episodes</th><th>Evidence</th><th>Sample</th><th>Cost</th></tr></thead><tbody>{cells.map((cell, index) => <tr key={`${cell.x}:${cell.y}:${index}`}><td>{text(cell.y)}</td><td>{text(cell.x)}</td><td>{text(cell.metric)}</td><td>{text(cell.completed_episodes)}</td><td>{quality(cell.evidence_quality)}</td><td>{text(cell.sample_confidence?.label ?? cell.sample_confidence?.classification)}</td><td>{text(cell.cost_coverage?.status)}</td></tr>)}</tbody></table></div>}</Card>
  </>;
}

function Comparison() {
  const discovery = useLive<{ cohorts?: Cohort[] }>(`${base}/cohorts`, 30000);
  const cohorts = discovery.data?.cohorts ?? [];
  const [selected, setSelected] = useState<string[]>([]);
  const instances = cohorts.filter((item, index, all) => all.findIndex((candidate) => candidate.strategy_id === item.strategy_id) === index);
  const cohort = cohorts[0];
  const params = cohort ? cohortParams(cohort) : new URLSearchParams();
  if (selected.length >= 2) params.set("strategy_ids", selected.join(","));
  const comparison = useLive<Envelope>(selected.length >= 2 ? `${base}/comparison?${params}` : "", 30000);
  return <><PageHeader title="Strategy Comparison" subtitle="Only compatible execution, asset, session and cost cohorts can be compared." /><Card title="Strategies"><div className="form-grid-2">{instances.map((item) => <label className="pa-check" key={item.strategy_id}><input type="checkbox" checked={selected.includes(item.strategy_id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, item.strategy_id].slice(-4) : current.filter((id) => id !== item.strategy_id))} /> {item.strategy_id}</label>)}</div>{selected.length < 2 && <p className="dim">Select two to four strategies actually present in the evidence store.</p>}</Card>{selected.length >= 2 && <Card title={comparison.data?.comparison_status === "NOT_DIRECTLY_COMPARABLE" ? "Not directly comparable" : "Comparison"}>{comparison.data?.comparison_reasons?.map((reason) => <p key={reason}>{reason}</p>)}{(comparison.data?.rows ?? []).map((row) => <div key={row.cohort.strategy_id} className="card" style={{ marginBottom: 8 }}><b>{row.cohort.strategy_id}</b><p>Evidence: {quality(row.evidence_quality)} · Sample: {text(row.sample_confidence?.label ?? row.sample_confidence?.classification)} · Cost: {text(row.cost_coverage?.status)}</p><p>Net P&amp;L: {text(row.metrics?.net_pnl)} · Episodes: {text(row.metrics?.completed_episode_count)}</p></div>)}</Card>}</>;
}

function EvidenceExplorer() {
  const discovery = useLive<{ cohorts?: Cohort[] }>(`${base}/cohorts`, 30000);
  const cohort = discovery.data?.cohorts?.[0];
  const params = cohortParams(cohort ?? { instance_id: "", strategy_id: "", strategy_version: null, config_fingerprint: null, simulation_session_id: null, execution_mode: "", source_kind: "" });
  params.set("limit", "50");
  const evidence = useLive<{ rows?: Array<{ episode_id?: string; trade_id?: string; symbol?: string; opened_at?: string; closed_at?: string; net_pnl?: string | null; fees?: string | null; funding_coverage?: string; context?: { session?: string; trend_regime?: string; volatility_regime?: string }; evidence_quality?: string | { status?: string } }>; total?: number }>(cohort ? `${base}/episodes?${params}` : "", 30000);
  const rows = evidence.data?.rows ?? [];
  return <><PageHeader title="Trade Evidence" subtitle="Read-only completed position episodes. Partial exits remain within one episode." /><Card title="Supporting episodes">{!cohort && !discovery.loading && <p>No observed cohorts are available.</p>}{evidence.error && <p role="alert">Evidence unavailable: {evidence.error}</p>}{cohort && !rows.length && !evidence.loading && <p>No completed episodes are available for this cohort.</p>}{!!rows.length && <div className="tablewrap"><table aria-label="Supporting trade episodes"><thead><tr><th>Episode</th><th>Asset</th><th>Entry</th><th>Exit</th><th>Net P&amp;L</th><th>Fees</th><th>Funding</th><th>Session</th><th>Trend</th><th>Volatility</th><th>Evidence</th></tr></thead><tbody>{rows.map((row) => <tr key={row.episode_id}><td>{text(row.episode_id)}<br />{text(row.trade_id)}</td><td>{text(row.symbol)}</td><td>{text(row.opened_at)}</td><td>{text(row.closed_at)}</td><td>{text(row.net_pnl)}</td><td>{text(row.fees)}</td><td>{text(row.funding_coverage)}</td><td>{text(row.context?.session)}</td><td>{text(row.context?.trend_regime)}</td><td>{text(row.context?.volatility_regime)}</td><td>{quality(row.evidence_quality)}</td></tr>)}</tbody></table></div>}</Card></>;
}

function QualificationArea({ view }: { view: "qualification" | "oos" | "walk-forward" | "robustness" | "validation-history" }) {
  const discovery = useLive<{ cohorts?: Cohort[] }>(`${base}/cohorts`, 30000);
  const cohort = discovery.data?.cohorts?.[0];
  const params = cohort ? cohortParams(cohort) : new URLSearchParams();
  const path = view === "walk-forward" ? "walk-forward" : view === "validation-history" ? "validation-history" : view;
  const result = useLive<Envelope & { status?: string; qualification_status?: string; blocking_reasons?: string[]; passed_requirements?: string[]; failed_requirements?: string[]; unknown_requirements?: string[]; policy?: { policy_version?: string }; evidence_quality?: string; checks?: Array<{ name: string; status: string; reason: string }>; result?: any; validation_history?: any[]; rows?: any[] }>(
    view === "validation-history" ? `${base}/${path}?instance_id=${encodeURIComponent(cohort?.instance_id ?? "")}` : (cohort ? `${base}/${path}?${params}` : ""), 30000);
  const payload = result.data;
  const title = view === "qualification" ? "Qualification Overview" : view === "oos" ? "Out-of-Sample Validation" : view === "walk-forward" ? "Walk-Forward Validation" : view === "robustness" ? "Robustness & Stress Testing" : "Validation History";
  const checks = payload?.checks ?? [];
  const rows = payload?.rows ?? payload?.validation_history ?? [];
  const [jobType, setJobType] = useState("OUT_OF_SAMPLE_VALIDATION");
  const [job, setJob] = useState<any>(null);
  const [jobError, setJobError] = useState<string | null>(null);
  const jobStatus = useLive<any>(job?.job_id ? `${base}/research-jobs/${encodeURIComponent(job.job_id)}` : "", 2000);
  const displayedJob = jobStatus.data ?? job;
  const queueResearch = async () => {
    if (!cohort) return;
    setJobError(null);
    try {
      const response = await apiPostJson<any>(`${base}/research-jobs`, {
        idempotency_key: `dashboard-${Date.now()}`,
        job_type: jobType,
        strategy_id: cohort.strategy_id,
        strategy_version: cohort.strategy_version,
        config_fingerprint: cohort.config_fingerprint,
        frozen_config: {}, symbol: cohort.symbol ?? "", timeframe: "5m",
        instance_id: cohort.instance_id, simulation_session_id: cohort.simulation_session_id,
      });
      setJob(response);
    } catch (error) { setJobError(error instanceof Error ? error.message : "Research job request failed"); }
  };
  const runResearch = async () => {
    if (!job?.job_id) return;
    try { setJob(await apiPostJson<any>(`${base}/research-jobs/${encodeURIComponent(job.job_id)}/run`, {})); }
    catch (error) { setJobError(error instanceof Error ? error.message : "Research execution failed"); }
  };
  const cancelResearch = async () => {
    if (!job?.job_id) return;
    try { setJob(await apiPostJson<any>(`${base}/research-jobs/${encodeURIComponent(job.job_id)}/cancel`, {})); }
    catch (error) { setJobError(error instanceof Error ? error.message : "Research cancellation failed"); }
  };
  return <><PageHeader title={title} subtitle="Advisory, evidence-backed research only. No result changes execution or promotes a strategy." />
    {result.error && <Card title="Validation unavailable"><p role="alert">{result.error}</p></Card>}
    {!cohort && view !== "validation-history" && !discovery.loading && <Card title="No exact cohort"><p>Qualification requires an observed strategy, version, configuration fingerprint and execution mode.</p></Card>}
    {payload && view === "qualification" && <>
      <Card title="Research execution"><p className="dim">Runs use the existing real-data backtester in an isolated research namespace. Missing real data fails closed.</p><div className="form-grid-2"><label className="field"><span className="field-label">Method</span><select aria-label="Research method" value={jobType} onChange={(event) => setJobType(event.target.value)}><option value="OUT_OF_SAMPLE_VALIDATION">Out-of-sample</option><option value="ANCHORED_WALK_FORWARD">Anchored walk-forward</option><option value="ROLLING_WALK_FORWARD">Rolling walk-forward</option><option value="ROBUSTNESS_EVALUATION">Robustness</option><option value="COST_STRESS">Cost stress</option></select></label><div><button className="button" type="button" disabled={!cohort} onClick={queueResearch}>Queue research job</button>{job?.job_id && <button className="button" type="button" onClick={runResearch} style={{ marginLeft: 8 }}>Start worker</button>}{job?.job_id && !["COMPLETED", "FAILED", "CANCELLED"].includes(displayedJob?.status) && <button className="button" type="button" onClick={cancelResearch} style={{ marginLeft: 8 }}>Cancel</button>}</div></div>{displayedJob && <p role="status">Job {displayedJob.job_id}: {displayedJob.status} ({displayedJob.progress ?? 0}%)</p>}{jobError && <p role="alert">{jobError}</p>}</Card>
      <div className="stat-row"><StatCard label="Status" value={text(payload.qualification_status ?? payload.status)} /><StatCard label="Evidence" value={text((payload as any).evidence_quality)} /><StatCard label="Passed" value={String(payload.passed_requirements?.length ?? 0)} /><StatCard label="Failed" value={String(payload.failed_requirements?.length ?? 0)} /><StatCard label="Unknown" value={String(payload.unknown_requirements?.length ?? 0)} /></div>
      <Card title="Qualification checks"><p>Policy: {text((payload as any).policy?.policy_version)} · Missing mandatory evidence is not a pass.</p><div className="tablewrap"><table aria-label="Qualification checks"><thead><tr><th>Requirement</th><th>Status</th><th>Reason</th></tr></thead><tbody>{checks.map((check) => <tr key={check.name}><td>{check.name}</td><td>{check.status}</td><td>{check.reason}</td></tr>)}</tbody></table></div><h3>Blocking reasons</h3>{(payload.blocking_reasons ?? []).map((reason) => <p className="dim" key={reason}>{reason}</p>)}</Card>
    </>}
    {payload && view !== "qualification" && <Card title={view === "validation-history" ? "Immutable validation runs" : "Validation result"}><p>Status: {text(payload.status)} · Results remain separate from financial evidence and are never treated as realized trading P&amp;L.</p>{!rows.length && <p>No completed validation run is available for this exact cohort.</p>}{!!rows.length && <div className="tablewrap"><table aria-label="Validation results"><thead><tr><th>Method</th><th>Status</th><th>Dataset</th><th>Policy</th><th>Created</th><th>Holdout access</th></tr></thead><tbody>{rows.map((row: any, index: number) => <tr key={row.run_id ?? index}><td>{text(row.method)}</td><td>{text(row.status)}</td><td>{text(row.dataset_identity)}</td><td>{text(row.policy_version)}</td><td>{text(row.created_at)}</td><td>{text(row.holdout_access_count)}</td></tr>)}</tbody></table></div>}</Card>}
  </>;
}

function ChampionChallenger() {
  const candidates = useLive<{ rows?: Array<{ candidate_id: string; strategy_id: string; strategy_version?: string | null; config_fingerprint?: string | null; symbol?: string | null; evidence_quality?: string }> }>(`${base}/research-candidates`, 10000);
  const history = useLive<{ rows?: any[] }>(`${base}/comparisons`, 10000);
  const [champion, setChampion] = useState("");
  const [challenger, setChallenger] = useState("");
  const [challengers, setChallengers] = useState<string[]>([]);
  const [comparison, setComparison] = useState<any>(null);
  const [experiment, setExperiment] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const rows = candidates.data?.rows ?? [];
  const submit = async () => {
    if (!champion || !challenger || champion === challenger) return;
    setError(null);
    try {
      setComparison(await apiPostJson<any>(`${base}/comparisons`, { idempotency_key: `dashboard-${Date.now()}`, champion_candidate_id: champion, challenger_candidate_id: challenger, random_seed: 1 }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Comparison failed"); }
  };
  const submitMatched = async () => {
    const selected = challengers.length ? challengers : challenger ? [challenger] : [];
    if (!champion || !selected.length) return;
    setError(null);
    try {
      const created = await apiPostJson<any>(`${base}/matched-experiments`, { idempotency_key: `dashboard-matched-${Date.now()}`, champion_candidate_id: champion, challenger_candidate_ids: selected, method: "OUT_OF_SAMPLE_VALIDATION" });
      setExperiment(await apiPostJson<any>(`${base}/matched-experiments/${encodeURIComponent(created.experiment_id)}/submit`, {}));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Matched experiment failed"); }
  };
  return <><PageHeader title="Champion / Challenger" subtitle="Advisory research comparisons only. No comparison changes execution." />
    <Card title="Research candidates"><p className="dim">Candidates remain separate by strategy version, configuration fingerprint, dataset and execution mode.</p><div className="form-grid-2"><label className="field"><span className="field-label">Research champion</span><select aria-label="Research champion" value={champion} onChange={(event) => setChampion(event.target.value)}><option value="">No research champion selected</option>{rows.map((item) => <option key={item.candidate_id} value={item.candidate_id}>{item.strategy_id} · {item.symbol ?? "portfolio"} · {item.config_fingerprint?.slice(0, 10) ?? "unknown"}</option>)}</select></label><label className="field"><span className="field-label">Challenger</span><select aria-label="Research challenger" value={challenger} onChange={(event) => setChallenger(event.target.value)}><option value="">Select challenger</option>{rows.map((item) => <option key={item.candidate_id} value={item.candidate_id}>{item.strategy_id} · {item.symbol ?? "portfolio"} · {item.config_fingerprint?.slice(0, 10) ?? "unknown"}</option>)}</select></label></div><button className="button" type="button" disabled={!champion || !challenger || champion === challenger} onClick={submit}>Run advisory comparison</button>{error && <p role="alert">{error}</p>}</Card>
    <Card title="Matched research experiment"><p className="dim">Freeze one common dataset and methodology, then submit one bounded job per challenger through the existing research worker.</p><label className="field"><span className="field-label">Challengers (multi-select)</span><select aria-label="Matched challengers" multiple value={challengers} onChange={(event) => setChallengers(Array.from(event.target.selectedOptions, (option) => option.value))}>{rows.map((item) => <option key={item.candidate_id} value={item.candidate_id}>{item.strategy_id} · {item.symbol ?? "portfolio"}</option>)}</select></label><button className="button" type="button" disabled={!champion || (!challengers.length && !challenger)} onClick={submitMatched}>Submit matched experiment</button>{experiment && <><p role="status">Experiment {experiment.experiment_id}: {experiment.status}</p><h3>Evidence integrity</h3><div className="stat-row"><StatCard label="Dataset" value={text(experiment.scope?.dataset_identity ?? "UNKNOWN")} /><StatCard label="Period" value={text(experiment.scope?.period_start && experiment.scope?.period_end ? "MATCHED" : "UNKNOWN")} /><StatCard label="Warm-up" value={text(experiment.report?.coverage?.status ?? "UNKNOWN")} /><StatCard label="Holdout" value={text(experiment.holdout_status ?? "UNKNOWN")} /><StatCard label="Eligibility" value={text(experiment.recommendation ?? "UNKNOWN")} /></div><p className="dim">Actual coverage and HTF status remain UNKNOWN until committed validation reports provide those references.</p></>}</Card>
    {comparison && <Card title="Comparison recommendation"><p role="status">{comparison.recommendation ?? "UNKNOWN"}</p><p className="dim">Only compatible, committed evidence is eligible. Unknown costs or incomplete runs remain blocking reasons.</p>{(comparison.blocking_reasons ?? []).map((reason: string) => <p className="dim" key={reason}>{reason}</p>)}</Card>}
    <Card title="Immutable comparison history">{!(history.data?.rows ?? []).length && <p>No comparison has been completed for this tenant.</p>}{(history.data?.rows ?? []).map((item: any) => <p key={item.comparison_id}>{item.recommendation} · {item.created_at}</p>)}</Card>
  </>;
}

function ResearchAgent() {
  const candidates = useLive<{ rows?: Array<{ candidate_id: string; strategy_id: string; strategy_version?: string | null; config_fingerprint?: string | null; frozen_config?: Record<string, unknown> }> }>(`${base}/research-candidates`, 10000);
  const policy = useLive<{ policy?: { policy_version?: string; allowed_parameters?: string[]; max_hypotheses_per_campaign?: number }; capabilities?: string[] }>(`${base}/research-agent/policy`, 30000);
  const [candidateId, setCandidateId] = useState("");
  const [minScore, setMinScore] = useState("60");
  const [datasetScope, setDatasetScope] = useState("{}");
  const [costs, setCosts] = useState('{"funding":"observed","version":"cost.v1"}');
  const [hypothesis, setHypothesis] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const rows = candidates.data?.rows ?? [];
  const selected = rows.find((item) => item.candidate_id === candidateId) ?? rows[0];
  const generate = async () => {
    if (!selected) return;
    setError(null);
    try {
      const scope = JSON.parse(datasetScope);
      const cost = JSON.parse(costs);
      const baseline = selected.frozen_config ?? {};
      const proposed = { ...baseline, min_score: Number(minScore) };
      setHypothesis(await apiPostJson<any>(`${base}/research-agent/hypotheses`, {
        candidate_id: selected.candidate_id, proposed_config: proposed, dataset_scope: scope,
        cost_assumptions: cost, lookback_verified: Boolean(scope.lookback_verified), lookback_spec: scope.lookback_spec,
        question: "Does this allowlisted configuration variation improve robustness under the recorded conditions?",
      }));
      setResult(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Hypothesis request failed"); }
  };
  const action = async (path: string, body: Record<string, unknown> = {}) => {
    if (!hypothesis?.hypothesis_id) return;
    setError(null);
    try { setResult(await apiPostJson<any>(`${base}/research-agent/hypotheses/${encodeURIComponent(hypothesis.hypothesis_id)}${path}`, body)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Research agent action failed"); }
  };
  return <><PageHeader title="Research Agent" subtitle="Controlled, advisory hypothesis research. Active strategies and financial state are never modified." />
    <Card title="Forward evidence review"><p>Committed forward qualification reports describe observation coverage and behaviour. They remain separate from historical experiments and trade profitability.</p><a className="btn btn-soft" href="#/strategy-intelligence?tab=forward-qualification">Open forward qualification reports</a></Card>
    <Card title="Policy and evidence"><p>Policy: {text(policy.data?.policy?.policy_version)} · Allowed parameters: {text(policy.data?.policy?.allowed_parameters?.join(", "))}</p><p className="dim">All numerical results must come from committed validation evidence. Missing lookback, funding or dataset identity blocks approval.</p>{policy.error && <p role="alert">Agent policy unavailable: {policy.error}</p>}</Card>
    <Card title="Propose a hypothesis"><div className="form-grid-2"><label className="field"><span className="field-label">Evidence candidate</span><select aria-label="Research evidence candidate" value={candidateId || selected?.candidate_id || ""} onChange={(event) => setCandidateId(event.target.value)}><option value="">Select candidate</option>{rows.map((item) => <option key={item.candidate_id} value={item.candidate_id}>{item.strategy_id} · {item.strategy_version ?? "unknown"} · {item.config_fingerprint?.slice(0, 10) ?? "unknown"}</option>)}</select></label><label className="field"><span className="field-label">Proposed min score</span><input aria-label="Proposed min score" value={minScore} onChange={(event) => setMinScore(event.target.value)} inputMode="numeric" /></label></div><label className="field"><span className="field-label">Dataset scope JSON</span><textarea aria-label="Dataset scope" value={datasetScope} onChange={(event) => setDatasetScope(event.target.value)} rows={2} /></label><label className="field"><span className="field-label">Cost assumptions JSON</span><textarea aria-label="Cost assumptions" value={costs} onChange={(event) => setCosts(event.target.value)} rows={2} /></label><button className="button" type="button" disabled={!selected} onClick={generate}>Generate proposal</button>{error && <p role="alert">{error}</p>}</Card>
    {hypothesis && <Card title="Frozen research plan"><p role="status">{hypothesis.hypothesis_id} · {hypothesis.status}</p><p>{hypothesis.question}</p><p>Baseline: {text(hypothesis.baseline_fingerprint)} · Proposed: {text(hypothesis.proposed_fingerprint)}</p><p className="dim">Approval is human and plan-specific. Any material change invalidates approval.</p><div><button className="button" type="button" onClick={() => action("/validate")}>Validate plan</button><button className="button" type="button" onClick={() => action("/approve", { decision: "APPROVED", max_jobs: 1 })} style={{ marginLeft: 8 }}>Approve research</button><button className="button" type="button" onClick={() => action("/submit")} style={{ marginLeft: 8 }}>Start approved research</button></div>{result && <p role="status">{result.status ?? result.outcome ?? result.execution ?? "Action completed"}</p>}</Card>}
  </>;
}

export default function StrategyIntelligenceHub({ tab }: { tab?: string }) {
  const active = tabs.some(([id]) => id === tab) ? (tab ?? "overview") : "overview";
  if (active === "shadow-lab") return <><SectionTabs tabs={tabs.map(([id, label]) => ({ id, label }))} active={active} /><ShadowStrategyLab /></>;
  if (active === "forward-qualification") return <><SectionTabs tabs={tabs.map(([id, label]) => ({ id, label }))} active={active} /><ForwardQualification /></>;
  if (active === 'operations' || active === 'paper-reconciliation') return <><SectionTabs tabs={tabs.map(([id,label])=>({id,label}))} active={active}/><IntegrityOperations key={active} mode={active==='operations'?'operations':'reconciliation'}/></>;
  if (active === 'local-readiness') return <><SectionTabs tabs={tabs.map(([id,label])=>({id,label}))} active={active}/><LocalReadiness/></>;
  if (active === 'edge-monitoring' || active === 'findings') return <><SectionTabs tabs={tabs.map(([id,label])=>({id,label}))} active={active}/><StrategyMonitoring key={active} mode={active==='edge-monitoring'?'decay':'weekly'}/></>;
  if (active === 'edge-score' || active === 'selection') return <><SectionTabs tabs={tabs.map(([id,label])=>({id,label}))} active={active}/><StrategyResearchDecision key={active} mode={active}/></>;
  return <><SectionTabs tabs={tabs.map(([id, label]) => ({ id, label }))} active={active} />{active === "overview" ? <Overview /> : active === "edge-map" ? <EdgeMap /> : active === "comparison" ? <Comparison /> : active === "explorer" ? <ContextIntelligence title="Strategy Explorer" /> : active === "regimes" ? <ContextIntelligence title="Market Regimes" defaultGrouping="trend_regime" /> : active === "sessions" ? <ContextIntelligence title="Session Analysis" /> : active === "evidence" ? <EvidenceExplorer /> : active === "qualification" ? <QualificationArea view="qualification" /> : active === "oos" ? <QualificationArea view="oos" /> : active === "walk-forward" ? <QualificationArea view="walk-forward" /> : active === "robustness" ? <QualificationArea view="robustness" /> : active === "validation-history" ? <QualificationArea view="validation-history" /> : active === "champion-challenger" ? <ChampionChallenger /> : active === "research-agent" ? <ResearchAgent /> : <ContextIntelligence />}</>;
}
