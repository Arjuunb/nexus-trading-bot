import {useState} from 'react';
import Card from '../components/common/Card';
import {PageHeader,StatCard} from '../components/common/ui';
import {apiGet,apiPostJson,useLive} from '../lib/api';

type Source={source_id:string;source_kind:string;strategy_id?:string;strategy_version?:string;config_fingerprint?:string;symbol?:string;timeframe?:string;coverage_class?:string};
type Policy={policy_version:string;minimum_duration_seconds?:number;minimum_opportunities?:number;minimum_observation_coverage?:number};
type Check={name:string;status:string;purpose:string;reason:string};
type Report={report_id:string;window_id:string;source_id:string;source_kind:string;start:string;end:string;
  identity?:{strategy_id?:string;strategy_version?:string;config_fingerprint?:string};policy?:Policy;outcome:string;
  coverage?:Record<string,number|null>;reliability?:Record<string,number|null>;behaviour?:Record<string,any>;
  checks?:Check[];warning_codes?:string[];blocking_reasons?:string[];profitability?:string;matched_qualification?:string;evidence_completeness?:string;
  commitment_status?:string;window_exclusion_reasons?:Record<string,number>;created_at?:string;
  holdout_provenance?:{status?:string};paper_evidence?:{account_id?:string;cost_coverage?:Record<string,string>;evidence_references?:string[];blocking_reasons?:string[]};
  matched_comparison?:{eligibility?:string;metrics?:Record<string,number|null>;exclusion_reasons?:Record<string,number>}|null};
type Detail={report:Report;workflow_state:string;reviews?:Array<{decision:string;reviewer_id:string;reason:string;created_at:string}>};
const base='/api/v2/strategy-intelligence/forward-qualification';
const value=(x:unknown)=>x===null||x===undefined||x===''?'UNKNOWN':String(x);
const rate=(x:unknown)=>typeof x==='number'&&Number.isFinite(x)?`${(x*100).toFixed(1)}%`:'UNKNOWN';
const boundary=()=>Math.floor(Date.now()/300000)*300000;

export default function ForwardQualification(){
  const sources=useLive<{rows:Source[]}>(base+'/sources',15000);
  const policies=useLive<{rows:Policy[]}>(base+'/policies',30000);
  const history=useLive<{rows:Report[];warning_codes?:string[]}>(base+'/reports?limit=50',10000);
  const operations=useLive<{production_ready:boolean;gates:Record<string,{status:string;reason:string}>}>(base+'/operations',30000);
  const [sourceId,setSourceId]=useState('');
  const [policyVersion,setPolicyVersion]=useState('');
  const [start,setStart]=useState(()=>new Date(boundary()-7*86400000).toISOString());
  const [end,setEnd]=useState(()=>new Date(boundary()).toISOString());
  const [activeId,setActiveId]=useState('');
  const [paperInstance,setPaperInstance]=useState('');
  const [paperSession,setPaperSession]=useState('');
  const [key,setKey]=useState(()=>crypto.randomUUID());
  const [preview,setPreview]=useState<{material:string;report:Report}|null>(null);
  const [committed,setCommitted]=useState<Report|null>(null);
  const [reportId,setReportId]=useState('');
  const [reviewed,setReviewed]=useState<Detail|null>(null);
  const [reviewDecision,setReviewDecision]=useState('ACKNOWLEDGED');
  const [reviewReason,setReviewReason]=useState('');
  const [reviewKey,setReviewKey]=useState(()=>crypto.randomUUID());
  const [showEvidence,setShowEvidence]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState<string|null>(null);
  const source=sources.data?.rows?.find(x=>x.source_id===sourceId);
  const chosenPolicy=policies.data?.rows?.find(x=>x.policy_version===(policyVersion||policies.data?.rows?.[0]?.policy_version));
  const material=JSON.stringify([sourceId,source?.source_kind,start,end,chosenPolicy?.policy_version,activeId,paperInstance,paperSession]);
  const currentPreview=preview?.material===material?preview.report:null;
  const details=useLive<Detail>(reportId?base+'/reports/'+encodeURIComponent(reportId):'',10000);
  const detail=reviewed?.report?.report_id===reportId?reviewed:details.data?.report?.report_id===reportId?details.data:null;
  const report=detail?.report??(committed?.report_id===reportId?committed:null);
  const evidence=useLive<{window_id:string;evidence_hash:string;inputs:{decisions:unknown[];active:unknown[]}}>(showEvidence&&report?base+'/windows/'+encodeURIComponent(report.window_id)+'?limit=100':'',15000);
  const validWindow=Number.isFinite(Date.parse(start))&&Number.isFinite(Date.parse(end))&&Date.parse(start)<Date.parse(end);
  const canAssess=!!source&&!!chosenPolicy&&validWindow&&!busy;
  const request=()=>({source_kind:source!.source_kind,source_id:sourceId,start,end,policy_version:chosenPolicy!.policy_version,
    idempotency_key:key,...(source?.source_kind==='FORWARD_SHADOW_DECISION'&&activeId?{active_source_id:activeId}:{}),
    ...(paperInstance?{paper_instance_id:paperInstance,...(paperSession?{paper_session_id:paperSession}:{})}:{})});
  const assess=async(commit:boolean)=>{
    if(!canAssess)return;
    setBusy(true);setError(null);
    try{
      const result=await apiPostJson<Report>(base+(commit?'/reports':'/eligibility'),request());
      if(!result.report_id||!result.window_id||!result.outcome||(commit&&result.commitment_status!=='COMMITTED'))throw new Error('Forward qualification response is incomplete');
      if(commit){setCommitted(result);setReportId(result.report_id);setReviewed(null);setShowEvidence(false);setKey(crypto.randomUUID());}
      else setPreview({material,report:result});
    }catch(reason){setError(reason instanceof Error?reason.message:'Forward evidence unavailable');}
    finally{setBusy(false);}
  };
  const review=async()=>{
    if(!report||reviewReason.trim().length<3||busy)return;
    setBusy(true);setError(null);
    try{
      await apiPostJson(base+'/reports/'+encodeURIComponent(report.report_id)+'/review',{
        idempotency_key:reviewKey,decision:reviewDecision,reason:reviewReason.trim()});
      setReviewed(await apiGet<Detail>(base+'/reports/'+encodeURIComponent(report.report_id)));
      setReviewKey(crypto.randomUUID());
    }catch(reason){setError(reason instanceof Error?reason.message:'Review unavailable');}
    finally{setBusy(false);}
  };
  return <><PageHeader title="Forward Qualification" subtitle="Advisory evidence readiness. Signals, agreement and coverage do not establish profitability."/>
    <Card title="Select forward evidence"><p className="dim">UTC half-open windows [start, end), at most seven days and 2,016 native candle opportunities. Each configuration remains separate.</p>
      {sources.error&&<p role="alert">{sources.error}</p>}{policies.error&&<p role="alert">{policies.error}</p>}
      {!sources.loading&&!sources.error&&!sources.data?.rows?.length&&<p>No committed forward evidence sources are available for this tenant.</p>}
      <div className="form-grid-2"><label className="field"><span className="field-label">Strategy evidence source</span><select aria-label="Forward evidence source" value={sourceId} onChange={event=>{setSourceId(event.target.value);setActiveId('');setKey(crypto.randomUUID());}}><option value="">Select an authorized source</option>{(sources.data?.rows??[]).map(x=><option key={x.source_kind+':'+x.source_id} value={x.source_id}>{x.strategy_id??x.source_id} · {x.strategy_version??'UNKNOWN'} · {x.source_kind}</option>)}</select></label>
      <label className="field"><span className="field-label">Qualification policy</span><select aria-label="Forward qualification policy" value={chosenPolicy?.policy_version??''} onChange={event=>{setPolicyVersion(event.target.value);setKey(crypto.randomUUID());}}>{(policies.data?.rows??[]).map(x=><option key={x.policy_version} value={x.policy_version}>{x.policy_version}</option>)}</select></label>
      <label className="field"><span className="field-label">Observation start (UTC)</span><input aria-label="Observation start (UTC)" value={start} onChange={event=>{setStart(event.target.value);setKey(crypto.randomUUID());}}/></label>
      <label className="field"><span className="field-label">Observation end (UTC)</span><input aria-label="Observation end (UTC)" value={end} onChange={event=>{setEnd(event.target.value);setKey(crypto.randomUUID());}}/></label>
      {source?.source_kind==='FORWARD_SHADOW_DECISION'&&<label className="field"><span className="field-label">Optional matched active source</span><select aria-label="Forward matched active source" value={activeId} onChange={event=>{setActiveId(event.target.value);setKey(crypto.randomUUID());}}><option value="">No active comparison requested</option>{(sources.data?.rows??[]).filter(x=>x.source_kind==='ACTIVE_DECISION_OBSERVATION').map(x=><option key={x.source_id} value={x.source_id}>{x.source_id} · {x.coverage_class??'UNKNOWN'}</option>)}</select></label>}
      <label className="field"><span className="field-label">Optional existing paper instance</span><input aria-label="Forward paper evidence instance" value={paperInstance} onChange={event=>{setPaperInstance(event.target.value);setKey(crypto.randomUUID());}}/></label>
      {paperInstance&&<label className="field"><span className="field-label">Paper evidence session</span><input aria-label="Forward paper evidence session" value={paperSession} onChange={event=>{setPaperSession(event.target.value);setKey(crypto.randomUUID());}}/></label>}</div>
      <p className="dim">Policy defaults: {value(chosenPolicy?.minimum_duration_seconds)} seconds, {value(chosenPolicy?.minimum_opportunities)} opportunities, {rate(chosenPolicy?.minimum_observation_coverage)} coverage. These are research thresholds, not scientific guarantees.</p>
      <button className="btn btn-soft" type="button" disabled={!canAssess} onClick={()=>assess(false)}>Check evidence eligibility</button>
      <button className="btn btn-soft" type="button" disabled={!canAssess} onClick={()=>assess(true)} style={{marginLeft:8}}>Generate immutable report</button>
      {busy&&<p role="status">Reading committed evidence…</p>}{error&&<p role="alert">{error}</p>}
      {currentPreview&&<><p role="status">Eligibility preview: {currentPreview.outcome}</p><p>{currentPreview.commitment_status}</p>{(currentPreview.blocking_reasons??[]).map((x,i)=><p className="dim" key={i}>{x}</p>)}</>}
    </Card>
    {report&&<><Card title="Recorded qualification report"><p role="status">Qualification outcome: {report.outcome}</p><p>Profitability: {value(report.profitability)}</p><p>Human review state: {detail?.workflow_state??'COMPLETED'}</p>
      <p>Strategy: {value(report.identity?.strategy_id)} · {value(report.identity?.strategy_version)}</p><p>Configuration: {value(report.identity?.config_fingerprint)}</p><p>Period: {report.start} → {report.end}</p><p>Report: {report.report_id}</p><p>Policy: {value(report.policy?.policy_version)}</p>
      <div className="stat-row"><StatCard label="Eligible candles" value={value(report.coverage?.eligible_decisions)}/><StatCard label="Expected candles" value={value(report.coverage?.expected_opportunities)}/><StatCard label="Decision coverage" value={rate(report.coverage?.decision_evaluation_coverage)}/><StatCard label="Missing-data rate" value={rate(report.reliability?.missing_data_rate)}/><StatCard label="Evaluation errors" value={rate(report.reliability?.evaluation_error_rate)}/><StatCard label="Signal count" value={value(report.behaviour?.signal_count)}/><StatCard label="Worker availability" value={rate(report.reliability?.worker_availability)}/></div>
      <p className="dim">Coverage uses the requested native candle clock. NO_SIGNAL is a decision; missing decisions are excluded. Worker uptime and provider availability require separate evidence.</p>
      <div className="tablewrap"><table aria-label="Forward qualification checks"><thead><tr><th>Gate</th><th>Status</th><th>Purpose</th><th>Reason</th></tr></thead><tbody>{(report.checks??[]).map(x=><tr key={x.name}><td>{x.name}</td><td>{x.status}</td><td>{x.purpose}</td><td>{x.reason}</td></tr>)}</tbody></table></div>
      {(report.warning_codes??[]).map(x=><p className="dim" key={x}>{x}</p>)}{Object.entries(report.window_exclusion_reasons??{}).map(([code,count])=><p key={code}>{code}: {count}</p>)}
    </Card><Card title="Signal behaviour and matched evidence"><p>Signal frequency: {rate(report.behaviour?.signal_frequency)} · Long: {value(report.behaviour?.long_count)} · Short: {value(report.behaviour?.short_count)}</p>
      <p>Matched eligibility: {value(report.matched_comparison?.eligibility)} · Behavioural qualification: {value(report.matched_qualification)} · Matched opportunities: {value(report.matched_comparison?.metrics?.matched_opportunities)}</p>
      <p>Overall agreement: {rate(report.matched_comparison?.metrics?.all_eligible_agreement)} · Signal-relevant agreement: {rate(report.matched_comparison?.metrics?.signal_relevant_agreement)}</p>
      {Object.entries(report.matched_comparison?.exclusion_reasons??{}).map(([code,count])=><p key={code}>{code}: {count}</p>)}
      <details><summary>Session, regime, volatility and rejection evidence</summary><pre style={{overflow:"auto",maxHeight:320}}>{JSON.stringify(report.behaviour?.breakdowns??{status:'UNKNOWN'},null,2)}</pre></details>
      <p className="dim">Frequency and agreement describe behaviour. They do not rank profitability or prove strategy correctness.</p></Card>
    <Card title="Read-only forward-paper evidence"><p>Account: {value(report.paper_evidence?.account_id)} · Holdout provenance: {value(report.holdout_provenance?.status)}</p>
      <div className="tablewrap"><table aria-label="Forward paper cost coverage"><thead><tr><th>Cost</th><th>Coverage</th></tr></thead><tbody>{Object.entries(report.paper_evidence?.cost_coverage??{}).map(([name,status])=><tr key={name}><td>{name}</td><td>{status}</td></tr>)}</tbody></table></div>
      {(report.paper_evidence?.blocking_reasons??[]).map(x=><p className="dim" key={x}>{x}</p>)}<p>Episode references: {report.paper_evidence?.evidence_references?.join(', ')||'UNKNOWN'}</p>
      <p className="dim">No paper account is started or modified. Missing fees, funding, slippage or spread block cost-adjusted claims.</p></Card>
    <Card title="Human evidence review"><p>Review acknowledges the recorded evidence. Execution authorization requires a separate process.</p>
      <label className="field"><span className="field-label">Review decision</span><select aria-label="Qualification review decision" value={reviewDecision} onChange={event=>{setReviewDecision(event.target.value);setReviewKey(crypto.randomUUID());}}><option value="ACKNOWLEDGED">Acknowledge evidence</option><option value="REQUEST_MORE_EVIDENCE">Request more evidence</option><option value="REJECTED">Reject readiness</option><option value="ARCHIVED">Archive report</option></select></label>
      <label className="field"><span className="field-label">Review reason</span><textarea style={{width:"100%",background:"var(--bg-2)",color:"var(--text)",border:"1px solid var(--card-border)",borderRadius:8,padding:10}} aria-label="Qualification review reason" value={reviewReason} onChange={event=>{setReviewReason(event.target.value);setReviewKey(crypto.randomUUID());}} maxLength={1000}/></label>
      <button className="btn btn-soft" type="button" disabled={busy||reviewReason.trim().length<3||detail?.workflow_state==='ARCHIVED'} onClick={review}>Record human review</button>
      {(detail?.reviews??[]).map((x,i)=><p key={i}>{x.reviewer_id} · {x.decision} · {x.reason}</p>)}
      <button className="btn btn-soft" type="button" style={{marginLeft:8}} onClick={()=>setShowEvidence(!showEvidence)}>Inspect frozen decision evidence</button>
      {showEvidence&&<><p>Window: {report.window_id} · Evidence hash: {value(evidence.data?.evidence_hash)}</p>{evidence.error&&<p role="alert">{evidence.error}</p>}<pre style={{overflow:"auto",maxHeight:320}}>{evidence.data?.window_id===report.window_id?JSON.stringify(evidence.data.inputs,null,2):'Loading original evidence…'}</pre></>}
    </Card></>}
    <Card title="Qualification history and coverage trends"><p className="dim">Each row is an immutable policy/window assessment. Overlapping windows are not independent samples, and configurations are not merged.</p>{history.error&&<p role="alert">{history.error}</p>}{(history.data?.warning_codes??[]).map(x=><p key={x}>{x}</p>)}
      <div className="tablewrap"><table aria-label="Forward qualification history"><thead><tr><th>Report</th><th>Configuration</th><th>Window</th><th>Coverage</th><th>Errors</th><th>Outcome</th><th>Policy</th></tr></thead><tbody>{(history.data?.rows??[]).map(x=><tr key={x.report_id}><td><button className="btn btn-soft" type="button" onClick={()=>{setReportId(x.report_id);setReviewed(null);setShowEvidence(false);}}>{x.report_id}</button></td><td>{value(x.identity?.config_fingerprint?.slice(0,12))}</td><td>{x.start} → {x.end}</td><td>{rate(x.coverage?.decision_evaluation_coverage)}</td><td>{rate(x.reliability?.evaluation_error_rate)}</td><td>{x.outcome}</td><td>{value(x.policy?.policy_version)}</td></tr>)}</tbody></table></div>
    </Card><Card title="Operational readiness"><p>Production readiness: {operations.data?.production_ready===false?'BLOCKED':'UNKNOWN'}</p>{operations.error&&<p role="alert">{operations.error}</p>}
      <div className="tablewrap"><table aria-label="Operational readiness gates"><thead><tr><th>Gate</th><th>Status</th><th>Reason</th></tr></thead><tbody>{Object.entries(operations.data?.gates??{}).map(([name,gate])=><tr key={name}><td>{name}</td><td>{gate.status}</td><td>{gate.reason}</td></tr>)}</tbody></table></div>
      <p className="dim">Local tests do not attest production configuration, credentials, continuous feed uptime or staging readiness.</p></Card>
  </>;
}
