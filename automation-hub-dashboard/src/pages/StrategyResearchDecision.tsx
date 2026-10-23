import { useState } from "react";
import Card from "../components/common/Card";
import { PageHeader } from "../components/common/ui";
import { useLive } from "../lib/api";

type Mode = "edge-score" | "selection";
type Identity = Record<string, string | null>;
type Candidate = { cohort: Identity; eligible: boolean; qualification_status: string;
  metrics?: Record<string, string | number | null>; blocking_reasons?: string[];
  evidence?: { cache_run_id?: string; episode_ids?: string[]; validation_run_ids?: Record<string, string> } };
type Result = { status: string; cache_status: string; identity?: Identity; context?: Identity;
  score?: string | null; band?: string | null; qualification_status?: string;
  policy?: { policy_version?: string; fingerprint?: string }; blocking_reasons?: string[];
  components?: { name: string; weight: string; status: string; normalized_score: string | null;
    weighted_points: string | null; formula: string; reason: string }[];
  candidates?: Candidate[]; recommendation?: Candidate | null };
const base = "/api/v2/strategy-intelligence";
const exact = ["instance_id", "simulation_session_id", "lab_id", "strategy_id", "strategy_version",
  "config_fingerprint", "execution_mode", "source_kind"];
const contextKeys = ["symbol", "session", "trend_regime", "volatility_regime", "entry_timeframe"];
const display = (value: unknown) => value == null || value === "" ? "Unavailable" : String(value);
const matches = (value: Identity | undefined, request: Identity, keys: string[]) =>
  !!value && keys.every(key => (value[key] ?? null) === (request[key] ?? null));
const metrics = [["completed_episode_count", "Episodes"], ["expectancy_net_r", "Net expectancy R"],
  ["net_profit_factor", "Profit factor"], ["max_drawdown_r", "Drawdown R"]];

function Results({ mode, request }: { mode: Mode; request: Identity }) {
  const params = new URLSearchParams();
  Object.entries(request).forEach(([key, value]) => { if (value != null) params.set(key, value); });
  const live = useLive<Result>(`${base}/${mode}?${params}`, 30000);
  const data = !live.loading && !live.error ? live.data : null;
  const scopeKeys = ["instance_id", "simulation_session_id", "lab_id"];
  const scoped = (candidate: Candidate) => matches(candidate.cohort, request, scopeKeys)
    && candidate.cohort.execution_mode === "paper" && candidate.cohort.source_kind === "forward_paper"
    && candidate.cohort.symbol === request.symbol;
  const bound = data && (mode === "edge-score" ? matches(data.identity, request, [...exact, "symbol"])
    : matches(data.context, request, contextKeys) && (data.candidates ?? []).every(scoped)
      && (!data.recommendation || scoped(data.recommendation)));
  const current = bound ? data : null;
  const ready = current?.cache_status === "READY";
  return <>
    {live.loading && <p role="status">Loading research evidence…</p>}
    {live.error && <p role="alert">Evidence unavailable: {live.error}</p>}
    {!bound && !live.loading && !live.error && <p role="alert">Evidence does not match this request.</p>}
    <Card title={mode === "edge-score" ? "Versioned EdgeScore" : "Advisory context selection"}>
      <p role="status">{ready ? current.status : mode === "edge-score" ? "UNSCORED" : "UNAVAILABLE"}</p>
      <p>Policy: {display(current?.policy?.policy_version)}</p>
      {mode === "edge-score" ? <>
        <p>Score: <strong>{ready && current.status === "SCORED" ? display(current.score) : "Unavailable"}</strong> / 100</p>
        <p>Qualification: {display(current?.qualification_status)}</p>
        <p>Uncalibrated research score. It is not a probability of profit and does not qualify or promote a strategy.</p>
        {ready && !!current.components?.length && <div className="tablewrap"><table className="data-table" aria-label="EdgeScore components" style={{ minWidth: 700 }}><thead><tr><th>Component</th><th>Weight</th><th>Status</th><th>Normalized</th><th>Points</th><th>Explanation</th></tr></thead><tbody>{current.components.map(item => <tr key={item.name}><td>{item.name}</td><td>{item.weight}</td><td>{item.status}</td><td>{display(item.normalized_score)}</td><td>{display(item.weighted_points)}</td><td>{item.formula}<br />{item.reason}</td></tr>)}</tbody></table></div>}
      </> : <>
        <p>Ranking uses observed context net expectancy, then lower drawdown and larger sample. A tie abstains.</p>
        <p style={{ overflowWrap: "anywhere" }}>Recommendation: {ready && current.status === "RECOMMENDED" && current.recommendation?.eligible
          ? `${current.recommendation.cohort.strategy_id} · ${current.recommendation.cohort.config_fingerprint}` : "None"}</p>
        {ready && !!current.candidates?.length && <div className="tablewrap"><table className="data-table" aria-label="Context selection evidence" style={{ minWidth: 800 }}><thead><tr><th>Frozen strategy</th><th>Qualification</th>{metrics.map(([key, label]) => <th key={key}>{label}</th>)}<th>Eligibility</th><th>Evidence</th></tr></thead><tbody>{current.candidates.map(candidate => <tr key={JSON.stringify(candidate.cohort)}><td style={{ overflowWrap: "anywhere" }}>{candidate.cohort.strategy_id} · {candidate.cohort.strategy_version}<br />{candidate.cohort.config_fingerprint}</td><td>{candidate.qualification_status}</td>{metrics.map(([key]) => <td key={key}>{display(candidate.metrics?.[key])}</td>)}<td>{candidate.eligible ? "Eligible for advisory ranking" : candidate.blocking_reasons?.join(", ") || "Insufficient evidence"}</td><td><details><summary>Source references</summary><p style={{ overflowWrap: "anywhere" }}>Cache: {display(candidate.evidence?.cache_run_id)}<br />Episodes: {candidate.evidence?.episode_ids?.join(", ") || "Unavailable"}<br />Validation: {Object.values(candidate.evidence?.validation_run_ids ?? {}).join(", ") || "Unavailable"}</p></details></td></tr>)}</tbody></table></div>}
      </>}
      {current?.blocking_reasons?.map(reason => <p key={reason} style={{ overflowWrap: "anywhere" }}>{reason}</p>)}
      <p>Research is advisory. Execution and strategy settings remain unchanged.</p>
    </Card>
  </>;
}

export default function StrategyResearchDecision({ mode }: { mode: Mode }) {
  const discovery = useLive<{ cohorts?: Identity[] }>(`${base}/cohorts`, 30000);
  const [selected, setSelected] = useState("");
  const [symbol, setSymbol] = useState("");
  const [context, setContext] = useState({ session: "LONDON", trend_regime: "BULL", volatility_regime: "NORMAL", entry_timeframe: "5m" });
  const cohorts = (discovery.data?.cohorts ?? []).filter(c => c.execution_mode === "paper" && c.source_kind === "forward_paper" && c.strategy_version && c.config_fingerprint);
  const cohort = cohorts.find(c => JSON.stringify(c) === selected);
  const request: Identity = { symbol: symbol.trim().toUpperCase() };
  if (cohort) (mode === "edge-score" ? exact : ["instance_id", "simulation_session_id", "lab_id"]).forEach(key => { request[key] = cohort[key] ?? null; });
  if (mode === "selection") Object.assign(request, context);
  return <>
    <PageHeader title={mode === "edge-score" ? "Strategy EdgeScore" : "Strategy Selection"} subtitle="Inspect frozen evidence before making a research decision." />
    <Card title="Research scope"><div className="form-grid-2">
      <label className="field"><span className="field-label">{mode === "selection" ? "Paper account and session" : "Frozen paper strategy"}</span><select aria-label="Research cohort" value={selected} onChange={event => setSelected(event.target.value)}><option value="">Select an observed cohort</option>{cohorts.map(c => <option key={JSON.stringify(c)} value={JSON.stringify(c)}>{c.strategy_id} · {c.instance_id} · {c.simulation_session_id} · {c.config_fingerprint?.slice(0, 10)}</option>)}</select></label>
      <label className="field"><span className="field-label">Exact asset</span><input aria-label="Research asset" value={symbol} onChange={event => setSymbol(event.target.value)} /></label>
      {mode === "selection" && ([
        ["session", ["ASIA", "LONDON", "NEW_YORK", "LONDON_NEW_YORK_OVERLAP", "OFF_SESSION"]],
        ["trend_regime", ["BULL", "BEAR", "STRONG_BULL", "STRONG_BEAR", "RANGE"]],
        ["volatility_regime", ["LOW", "NORMAL", "HIGH", "EXTREME"]],
        ["entry_timeframe", ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]],
      ] as [keyof typeof context, string[]][]).map(([key, values]) => <label className="field" key={key}><span className="field-label">{key.replace(/_/g, " ")}</span><select aria-label={key} value={context[key]} onChange={event => setContext({ ...context, [key]: event.target.value })}>{values.map(value => <option key={value}>{value}</option>)}</select></label>)}
    </div>{discovery.error && <p role="alert">{discovery.error}</p>}
      <p>{mode === "selection" ? "Selection reviews all qualified frozen strategies in this account, asset and context. The server selects the current classifier." : "All nine components require complete bound evidence. Missing components remain unknown."}</p>
    </Card>
    {cohort && request.symbol ? <Results key={`${mode}:${JSON.stringify(request)}`} mode={mode} request={request} /> : <p>Select an observed cohort and exact asset to inspect evidence.</p>}
  </>;
}
