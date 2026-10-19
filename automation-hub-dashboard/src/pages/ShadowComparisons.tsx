import { useState } from "react";
import Card from "../components/common/Card";
import { apiPostJson, useLive } from "../lib/api";

type Source = { source_id: string; coverage_class: string; limitations?: string[] };
type Metrics = { matched_opportunities?: number; all_eligible_agreement?: number|null; all_eligible_denominator?: number; signal_relevant_agreement?: number|null; signal_relevant_denominator?: number; both_no_signal?: number; active_only_signals?: number; shadow_only_signals?: number; opposite_signals?: number; excluded_opportunities?: number; missing_decision_rate?: number|null };
type Row = { candle_close_timestamp: string; active_status: string; shadow_status: string; eligible: boolean; exclusion_reasons: string[] };
type Health = { source:Source; start:string; end:string; status:string; comparison_readiness?:string; expected_opportunities?:number|null; recorded_opportunities?:number; verified_opportunities?:number; configuration_completeness?:number|null; market_context_completeness?:number|null; active_decision_coverage?:number|null; field_matrix?:Record<string,string>; blocking_reasons?:Record<string,number>; last_verified_observation?:string|null; reconciliation_backlog?:number|null; reconciliation_status?:string; optional_fields?:string[] };
type ActiveRow = { decision_id:string;status:string;candle_close_timestamp?:string;config_fingerprint?:string|null;configuration_reference?:string|null;persistence_status?:string;limitations?:string[] };
type Artifact = { comparison_id: string; registration_id: string; active_source_id: string; start?: string; end?: string; eligibility?: string; evidence_completeness?: string; metrics?: Metrics; timeline?: Row[]; coverage?: Record<string,unknown>; reliability?: Record<string,unknown>; breakdowns?: Record<string,Record<string,Metrics>>; warnings?: string[]; calculation_version?: string; input_watermark?: string; created_at?: string; inputs?: unknown };
const base="/api/v2/strategy-intelligence/shadow";
const unknown=(x:unknown)=>x===null||x===undefined?"UNKNOWN":String(x);
const rate=(x:number|null|undefined)=>x===null||x===undefined?"UNAVAILABLE":`${(x*100).toFixed(1)}%`;

export default function ShadowComparisons({registrationId}:{registrationId:string}) {
  const sources=useLive<{rows:Source[]}>(`${base}/active-sources`,30000);
  const history=useLive<{rows:Artifact[]}>(`${base}/comparisons?limit=50`,10000);
  const [activeId,setActiveId]=useState("");
  const [start,setStart]=useState(()=>new Date(Date.now()-3600000).toISOString());
  const [end,setEnd]=useState(()=>new Date().toISOString());
  const [key,setKey]=useState(()=>crypto.randomUUID());
  const [artifactId,setArtifactId]=useState("");
  const [committed,setCommitted]=useState<Artifact|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const [showActive,setShowActive]=useState(false);
  const query=`start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
  const health=useLive<Health>(activeId?`${base}/active-sources/${encodeURIComponent(activeId)}/health?${query}`:"",10000);
  const activeDecisions=useLive<{rows:ActiveRow[];total?:number}>(activeId&&showActive?`${base}/active-sources/${encodeURIComponent(activeId)}/decisions?${query}&limit=100`:"",10000);
  // A response from the preceding selection/window cannot become a green
  // health indicator for newly edited material fields.
  const activeHealth=health.data?.source?.source_id===activeId&&Date.parse(health.data.start)===Date.parse(start)&&Date.parse(health.data.end)===Date.parse(end)?health.data:null;
  const details=useLive<Artifact>(artifactId?`${base}/comparisons/${encodeURIComponent(artifactId)}`:"",10000);
  const source=sources.data?.rows?.find(s=>s.source_id===activeId);
  const artifact=details.data?.comparison_id===artifactId?details.data:committed?.comparison_id===artifactId?committed:null;
  const m=artifact?.metrics;
  function changed(){setKey(crypto.randomUUID());setArtifactId("");setCommitted(null);setShowActive(false);}
  async function create(){
    setBusy(true);setError(null);
    try {
      const result=await apiPostJson<Artifact>(`${base}/comparisons`,{registration_id:registrationId,active_source_id:activeId,start,end,idempotency_key:key});
      setCommitted(result);setArtifactId(result.comparison_id);await history.refetch();
    }catch(e){setError(String(e));}finally{setBusy(false);}
  }
  return <>
    <Card title="Matched Active vs Shadow Comparison">
      <p>Forward decisions only—not fills, trades or profitability. Comparisons retain immutable evidence snapshots.</p>
      {[error,sources.error,history.error,details.error].filter(Boolean).map((e,i)=><p role="alert" key={i}>{e}</p>)}
      <label>Active decision source<select aria-label="Active decision source" value={activeId} onChange={e=>{setActiveId(e.target.value);changed();}}><option value="">Select a persisted source</option>{(sources.data?.rows||[]).map(s=><option key={s.source_id} value={s.source_id}>{s.source_id} · {s.coverage_class}</option>)}</select></label>
      {!sources.loading&&!sources.data?.rows?.length&&<p>No accessible active decision sources. Missing records are not NO_SIGNAL.</p>}
      <p>Source evidence: {source?.coverage_class||"UNKNOWN"}</p>
      {source?.limitations?.map(r=><p key={r}>{r}</p>)}
      <label>Comparison start (UTC)<input aria-label="Comparison start (UTC)" value={start} onChange={e=>{setStart(e.target.value);changed();}} maxLength={50}/></label>
      <label>Comparison end (UTC)<input aria-label="Comparison end (UTC)" value={end} onChange={e=>{setEnd(e.target.value);changed();}} maxLength={50}/></label>
      {activeId&&<section aria-label="Active Evidence Health">
        <h3>Active Evidence Health</h3>
        {health.error&&<p role="alert">{health.error}</p>}
        <p aria-live="polite">Active evidence status: {activeHealth?.status||"UNKNOWN"}</p>
        <p>Active comparison readiness: {activeHealth?.comparison_readiness||"UNKNOWN"}</p>
        <p>Configuration completeness: {rate(activeHealth?.configuration_completeness)}</p>
        <p>Candle and HTF completeness: {rate(activeHealth?.market_context_completeness)}</p>
        <p>Decision coverage: {rate(activeHealth?.active_decision_coverage)} · recorded {unknown(activeHealth?.recorded_opportunities)} / expected {unknown(activeHealth?.expected_opportunities)}</p>
        <p>Last verified observation: {unknown(activeHealth?.last_verified_observation)}</p>
        <p>Receipt reconciliation: {activeHealth?.reconciliation_status||"UNKNOWN"} · backlog {unknown(activeHealth?.reconciliation_backlog)}</p>
        {Object.entries(activeHealth?.blocking_reasons||{}).map(([reason,count])=><p key={reason}>{reason}: {count}</p>)}
        <div className="overflow-auto"><table aria-label="Active evidence field verification"><thead><tr><th>Evidence field</th><th>Verification</th></tr></thead><tbody>{Object.entries(activeHealth?.field_matrix||{}).map(([field,status])=><tr key={field}><td>{field}</td><td>{status}</td></tr>)}</tbody></table></div>
        <p>Optional context: {(activeHealth?.optional_fields||[]).join(", ")||"UNKNOWN"}. Active readiness does not establish shadow comparability or profitability.</p>
        <button onClick={()=>setShowActive(!showActive)}>{showActive?"Hide active decision evidence":"View active decision evidence"}</button>
        {showActive&&<>
          {activeDecisions.error&&<p role="alert">{activeDecisions.error}</p>}
          <p>First 100 persisted decisions; total {unknown(activeDecisions.data?.total)} in this window.</p>
          {(activeDecisions.data?.rows||[]).map(r=><details key={r.decision_id}><summary>{r.candle_close_timestamp} · {r.status}</summary><p className="break-all">{r.configuration_reference||"Configuration UNKNOWN"}</p><pre className="overflow-auto">{JSON.stringify(r,null,2)}</pre></details>)}
        </>}
      </section>}
      <p>Half-open interval [start, end). Maximum seven days. Pause/offline gaps remain visible; no forward backfill.</p>
      <button disabled={busy||!activeId||!start||!end} onClick={()=>void create()}>Calculate matched comparison</button>
      {artifact?<>
        <p aria-live="polite">Comparison eligibility: {unknown(artifact.eligibility)}</p>
        <p>Evidence completeness: {unknown(artifact.evidence_completeness)}</p>
        <p>Active: {artifact.active_source_id} · Shadow: {artifact.registration_id}</p>
        <p>Period: {artifact.start} — {artifact.end}</p>
        <p>Matched opportunities: {unknown(m?.matched_opportunities)}</p>
        <p>All eligible agreement: {rate(m?.all_eligible_agreement)} · denominator {unknown(m?.all_eligible_denominator)}</p>
        <p>Signal-relevant agreement: {rate(m?.signal_relevant_agreement)} · denominator {unknown(m?.signal_relevant_denominator)}</p>
        <p>Shared no-signal decisions: {unknown(m?.both_no_signal)} · Active-only signals: {unknown(m?.active_only_signals)} · Shadow-only signals: {unknown(m?.shadow_only_signals)} · Opposite signals: {unknown(m?.opposite_signals)}</p>
        <p>Excluded opportunities: {unknown(m?.excluded_opportunities)} · Missing active decision rate: {rate(m?.missing_decision_rate)}</p>
        {artifact.warnings?.map((w,i)=><p key={i}>{w}</p>)}
      </>:<p>No committed matched comparison selected.</p>}
    </Card>
    {artifact&&<>
      <Card title="Observation Coverage and Reliability">
        <pre className="overflow-auto">{JSON.stringify({coverage:artifact.coverage,reliability:artifact.reliability},null,2)}</pre>
        <p>Market coverage counts recorded finalized snapshots, not raw-feed uptime. Exchange-to-system latency is UNKNOWN without source receipt timing.</p>
      </Card>
      <Card title="Matched Decision Timeline"><div className="overflow-auto"><table aria-label="Matched decision timeline"><thead><tr><th>Candle close (UTC)</th><th>Active</th><th>Shadow</th><th>Eligibility</th><th>Exclusions</th></tr></thead><tbody>{(artifact.timeline||[]).slice(0,100).map(r=><tr key={r.candle_close_timestamp}><td>{r.candle_close_timestamp}</td><td>{r.active_status}</td><td>{r.shadow_status}</td><td>{r.eligible?"ELIGIBLE":"EXCLUDED"}</td><td>{r.exclusion_reasons.join(", ")}</td></tr>)}</tbody></table></div><p>First 100 opportunities; the evidence artifact retains the complete bounded timeline.</p></Card>
      <Card title="Session and Regime Analysis">{Object.entries(artifact.breakdowns||{}).map(([dimension,groups])=><div key={dimension}><h3>{dimension}</h3><table><thead><tr><th>Observed group</th><th>Matched</th><th>Signal-relevant agreement</th></tr></thead><tbody>{Object.entries(groups).map(([name,metrics])=><tr key={name}><td>{name}</td><td>{unknown(metrics.matched_opportunities)}</td><td>{rate(metrics.signal_relevant_agreement)}</td></tr>)}</tbody></table></div>)}</Card>
      <Card title="Comparison Evidence"><details><summary>Comparison evidence details</summary><p className="break-all">Input watermark: {artifact.input_watermark}</p><p>Calculation: {artifact.calculation_version} · Committed artifact: {artifact.comparison_id}</p><pre className="overflow-auto">{JSON.stringify(artifact,null,2)}</pre></details></Card>
    </>}
    <Card title="Immutable Comparison History">{(history.data?.rows||[]).filter(r=>r.registration_id===registrationId).map(r=><p key={r.comparison_id}><button disabled={busy} onClick={()=>{setArtifactId(r.comparison_id);setError(null);}}>Open comparison {r.created_at||r.comparison_id}</button> · {r.active_source_id}</p>)}</Card>
  </>;
}
