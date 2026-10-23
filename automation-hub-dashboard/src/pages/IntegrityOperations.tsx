import {useState} from 'react';
import Card from '../components/common/Card';
import {PageHeader,StatCard} from '../components/common/ui';
import {apiGet,apiPostJson,useLive} from '../lib/api';

const base='/api/v2/strategy-intelligence';
const show=(v:unknown)=>v===null||v===undefined||v===''?'UNKNOWN':String(v);
const percent=(v:unknown)=>typeof v==='number'&&Number.isFinite(v)?`${(100*v).toFixed(1)}%`:'UNKNOWN';
type Source={source_id:string;kind?:string;scope?:Record<string,unknown>;monitoring_started_at?:string};
type PaperReport={report_id:string;status:string;performance_eligible:boolean;performance_type:string;commitment_status:string;
  metrics:Record<string,unknown>;cost_coverage:Record<string,string>;checks:Array<{name:string;status:string;reason:string}>;
  warning_codes:string[];scope?:Record<string,unknown>;evidence_hash?:string;original_snapshot?:unknown};

export default function IntegrityOperations({mode}:{mode:'operations'|'reconciliation'}){
  const prefix=base+(mode==='operations'?'/operational-reliability':'/paper-reconciliation');
  const sources=useLive<{rows:Source[]}>(prefix+'/sources',15000);
  const history=useLive<{rows:PaperReport[]}>(mode==='reconciliation'?prefix+'/reports?limit=50':'',15000);
  const resilience=useLive<any>(mode==='operations'?base+'/operational-resilience/health':'',15000);
  const [source,setSource]=useState('');
  const [start,setStart]=useState(()=>new Date(Date.now()-86400000).toISOString());
  const [end,setEnd]=useState(()=>new Date().toISOString());
  const [data,setData]=useState<any>(null);const [report,setReport]=useState<PaperReport|null>(null);
  const [availability,setAvailability]=useState<any>(null);const [error,setError]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);const [key,setKey]=useState(()=>crypto.randomUUID());
  const [evidence,setEvidence]=useState<unknown>(null);
  const [reviewReason,setReviewReason]=useState('');const [reviewed,setReviewed]=useState<any>(null);
  const available=sources.data?.rows?.some(r=>r.source_id===source);
  const query=()=>new URLSearchParams({start,end}).toString();
  const reset=()=>{setData(null);setReport(null);setAvailability(null);setEvidence(null);setReviewed(null);setReviewReason('');setKey(crypto.randomUUID());};
  const act=async(kind:'inspect'|'preview'|'commit')=>{
    if(!available||busy)return;setBusy(true);setError(null);setEvidence(null);
    try{
      if(mode==='operations')setData(await apiGet(prefix+'/sources/'+encodeURIComponent(source)+'?'+query()));
      else if(kind==='inspect')setAvailability(await apiGet(prefix+'/sources/'+encodeURIComponent(source)+'/availability'));
      else{
        const next=await apiPostJson<PaperReport>(prefix+(kind==='commit'?'/reports':'/preview'),{source_id:source,start,end,idempotency_key:key});
        if(!next.report_id||!next.checks||!next.metrics||(kind==='commit'&&next.commitment_status!=='COMMITTED'))throw new Error('Incomplete reconciliation response');
        setReport(next);if(kind==='commit')setKey(crypto.randomUUID());
      }
    }catch(reason){setError(reason instanceof Error?reason.message:'Evidence unavailable');}
    finally{setBusy(false);}
  };
  const inspectEvidence=async(id?:string)=>{
    setBusy(true);setError(null);
    try{
      if(mode==='operations')setEvidence(await apiGet(prefix+'/sources/'+encodeURIComponent(source)+'/events?'+query()+'&limit=100'));
      else if(id){const detail=await apiGet<PaperReport>(prefix+'/reports/'+encodeURIComponent(id));setReport(detail);setEvidence(detail.original_snapshot);setReviewed(null);setReviewReason('');}
    }catch(reason){setError(reason instanceof Error?reason.message:'Original evidence unavailable');}
    finally{setBusy(false);}
  };
  const review=async()=>{
    if(!report||report.commitment_status!=='COMMITTED'||reviewReason.trim().length<3||busy)return;
    setBusy(true);setError(null);
    try{setReviewed(await apiPostJson(prefix+'/reports/'+encodeURIComponent(report.report_id)+'/review',{
      decision:'REQUEST_MORE_EVIDENCE',reason:reviewReason.trim(),idempotency_key:crypto.randomUUID()}));}
    catch(reason){setError(reason instanceof Error?reason.message:'Review unavailable');}finally{setBusy(false);}
  };
  return <><PageHeader title={mode==='operations'?'Operational Reliability':'Paper Evidence Reconciliation'} subtitle={mode==='operations'?'Original worker, feed, delivery, evaluation and commitment evidence. Missing monitoring remains unknown.':'Read-only original account evidence. Unknown costs block verified paper performance.'}/>
    <Card title="Select owned evidence">
      {sources.error&&<p role="alert">{sources.error}</p>}{!sources.loading&&!sources.error&&!sources.data?.rows?.length&&<p>No owned evidence source is available.</p>}
      <div className="form-grid-2"><label className="field"><span className="field-label">Evidence source</span><select aria-label="Integrity evidence source" value={source} onChange={event=>{setSource(event.target.value);reset();}}><option value="">Select source</option>{(sources.data?.rows??[]).map(row=><option key={row.source_id} value={row.source_id}>{row.source_id}</option>)}</select></label>
        <label className="field"><span className="field-label">Start (UTC)</span><input aria-label="Integrity period start" value={start} onChange={event=>{setStart(event.target.value);reset();}}/></label>
        <label className="field"><span className="field-label">End (UTC)</span><input aria-label="Integrity period end" value={end} onChange={event=>{setEnd(event.target.value);reset();}}/></label></div>
      <button className="btn btn-soft" disabled={!available||busy} onClick={()=>act('inspect')}>{mode==='operations'?'Inspect reliability':'Inspect paper evidence availability'}</button>
      {mode==='reconciliation'&&<><button className="btn btn-soft" style={{marginLeft:8}} disabled={!available||busy} onClick={()=>act('preview')}>Preview reconciliation</button><button className="btn btn-soft" style={{marginLeft:8}} disabled={!available||busy} onClick={()=>act('commit')}>Create immutable reconciliation report</button></>}
      {error&&<p role="alert">{error}</p>}<p className="dim">Research and reconciliation cannot start paper trading, change strategy settings or place orders.</p>
    </Card>
    {mode==='operations'&&<Card title="Operational Resilience">{resilience.error&&<p role="alert">{resilience.error}</p>}
      <p>Durable spool: {show(resilience.data?.spool?.status)} · pending: {show(resilience.data?.spool?.pending)}</p>
      <p>Corrupt / conflicting / overflow: {show(resilience.data?.spool?.corrupt)} / {show(resilience.data?.spool?.conflicting)} / {show(resilience.data?.spool?.overflow_count)}</p>
      <p>Owned retention bytes / limit: {show(resilience.data?.raw_retention?.owner_payload_bytes)} / {show(resilience.data?.raw_retention?.owner_byte_limit)}</p>
      <p>Retention reclamation: {show(resilience.data?.raw_retention?.raw_reclamation)}</p>
      <p>Live-feed soak: {show(resilience.data?.feed_soak?.status)} — {show(resilience.data?.feed_soak?.reason)}</p>
      <p className="dim">Durable buffered observations cannot prove uptime during an unobserved interval.</p></Card>}
    {mode==='operations'&&data&&<Card title="Monitoring and availability"><p role="status">Monitoring: {show(data.monitoring_status)}</p><p>{show(data.availability_basis)}</p>
      <div className="stat-row"><StatCard label="Worker probe availability" value={percent(data.worker_availability)}/><StatCard label="Feed probe availability" value={percent(data.feed_availability)}/><StatCard label="Missing probe opportunities" value={show(data.missing_probe_opportunities)}/></div>
      <p>Worker/feed denominator: {show(data.worker_denominator)} / {show(data.feed_denominator)}</p><p>Evidence commitment: {percent(data.commit_success_rate)} · failures: {show(data.evidence_commitment_failures)}</p>
      <p>Finalized live receipts: {show(data.finalized_candle_receipts)} · Historical repairs: {show(data.historical_repair_events)}</p><p>Feed disconnects: {show(data.feed_disconnects)} · Worker restarts: {show(data.worker_restarts)}</p><p>Stale candles / HTF: {show(data.stale_candle_events)} / {show(data.stale_htf_events)}</p>
      {(data.warning_codes??[]).map((code:string)=><p key={code}>{code}</p>)}<button className="btn btn-soft" disabled={busy} onClick={()=>inspectEvidence()}>Inspect operational evidence</button><p className="dim">Probes are separate from continuous uptime certification, fresh candles and successful strategy decisions.</p>
    </Card>}
    {mode==='reconciliation'&&availability&&<Card title="Financial Evidence Integrity">
      {availability.native_capture&&<><p>Original / captured fills: {show(availability.native_capture.original_fill_count)} / {show(availability.native_capture.captured_fill_count)}</p><p>Transaction capture: {show(availability.native_capture.transaction_capture)}</p></>}
      <div className="tablewrap"><table aria-label="Paper source evidence availability"><thead><tr><th>Field</th><th>Status</th><th>Reason</th></tr></thead><tbody>{Object.entries(availability.fields??{}).map(([field,entry]:[string,any])=><tr key={field}><td>{field}</td><td>{show(entry.status)}</td><td>{show(entry.reason)}</td></tr>)}</tbody></table></div>
      <button className="btn btn-soft" disabled={busy} onClick={async()=>{setBusy(true);try{setEvidence(await apiGet(prefix+'/sources/'+encodeURIComponent(source)+'/lineage?'+query()+'&limit=100'));}catch(reason){setError(reason instanceof Error?reason.message:'Original lineage unavailable');}finally{setBusy(false);}}}>Inspect native configuration and episode lineage</button>
      </Card>}
    {mode==='reconciliation'&&report&&<><Card title="Reconciliation result"><p role="status">{report.status} · {report.commitment_status}</p><p>{report.performance_type}</p><p>Performance eligibility: {report.performance_eligible?'VERIFIED PAPER EVIDENCE':'BLOCKED'}</p>
      <p>Account / strategy / version: {show(report.scope?.account_id)} / {show(report.scope?.strategy_id)} / {show(report.scope?.strategy_version)}</p><p>Configuration: {show(report.scope?.config_fingerprint)}</p>
      {Object.entries(report.cost_coverage).map(([name,status])=><p key={name}>{name}: {status}</p>)}<div className="tablewrap"><table aria-label="Paper reconciliation checks"><thead><tr><th>Check</th><th>Status</th><th>Reason</th></tr></thead><tbody>{report.checks.map(check=><tr key={check.name}><td>{check.name}</td><td>{check.status}</td><td>{check.reason}</td></tr>)}</tbody></table></div>
      {report.warning_codes.map(code=><p className="dim" key={code}>{code}</p>)}{report.commitment_status==='COMMITTED'&&<><button className="btn btn-soft" disabled={busy} onClick={()=>inspectEvidence(report.report_id)}>Inspect original financial evidence</button>
        <label className="field"><span className="field-label">Human review reason</span><input aria-label="Paper evidence review reason" value={reviewReason} maxLength={1000} onChange={e=>setReviewReason(e.target.value)}/></label>
        <button className="btn btn-soft" disabled={busy||reviewReason.trim().length<3} onClick={review}>Record request for more evidence</button>
        {reviewed&&<p>Human review: {show(reviewed.decision)} · {show(reviewed.review_id)} · No execution authorization</p>}</>}
    </Card><Card title="Reconciled paper metrics">{!report.performance_eligible?<p>UNKNOWN — mandatory original financial, cost or operational evidence is incomplete.</p>:<div className="tablewrap"><table aria-label="Reconciled paper metrics"><tbody>{Object.entries(report.metrics).map(([name,value])=><tr key={name}><th>{name}</th><td>{typeof value==='object'&&value!==null?JSON.stringify(value):show(value)}</td></tr>)}</tbody></table></div>}<p className="dim">Paper returns remain separate from shadow behaviour and live returns. No trading authorization is granted.</p></Card></>}
    {evidence!==null&&<Card title="Original immutable evidence"><pre style={{maxHeight:400,overflow:'auto'}}>{JSON.stringify(evidence,null,2)}</pre></Card>}
    {mode==='reconciliation'&&<Card title="Immutable reconciliation history">{history.error&&<p role="alert">{history.error}</p>}{(history.data?.rows??[]).map(row=><p key={row.report_id}><button className="btn btn-soft" disabled={busy} onClick={()=>inspectEvidence(row.report_id)}>{row.report_id}</button> · {row.status}</p>)}</Card>}
  </>;
}
