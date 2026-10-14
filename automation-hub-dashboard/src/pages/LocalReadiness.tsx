import {useState} from 'react';
import Card from '../components/common/Card';
import {PageHeader} from '../components/common/ui';
import {apiGet,useLive} from '../lib/api';

type Assessment={assessment_id:string;environment_id:string;software_revision:string;status:string;worker_backend:string;
  created_at:string;gates:Record<string,{status:string;reason?:string;evidence_reference?:string}>};
const base='/api/v2/strategy-intelligence/local-readiness';
export default function LocalReadiness(){
  const history=useLive<{rows:Assessment[]}>(base+'?limit=50',15000);
  const [selected,setSelected]=useState<Assessment|null>(null);const [error,setError]=useState<string|null>(null);
  const inspect=async(id:string)=>{try{const next=await apiGet<Assessment>(base+'/'+encodeURIComponent(id));
    if(next.assessment_id!==id||!next.gates||typeof next.gates!=='object')throw new Error('Incomplete local readiness evidence');
    setSelected(next);setError(null);}catch(reason){setError(reason instanceof Error?reason.message:'Readiness evidence unavailable');}};
  return <><PageHeader title="Local Readiness" subtitle="Immutable local validation evidence. Staging and production require separately authorized validation."/>
    <Card title="Local assessment history">{history.error&&<p role="alert">{history.error}</p>}{error&&<p role="alert">{error}</p>}
      {!history.loading&&!history.data?.rows?.length&&<p>UNKNOWN — no owned local readiness assessment is available.</p>}
      {(history.data?.rows??[]).map(row=><p key={row.assessment_id}><button className="btn btn-soft" onClick={()=>inspect(row.assessment_id)}>{row.environment_id}</button> · {row.status} · {row.created_at}</p>)}
      <p className="dim">These records authorize no trading activity or deployment.</p></Card>
    {selected&&<Card title="Readiness evidence"><p role="status">{selected.status}</p><p>Environment: {selected.environment_id} · Backend: {selected.worker_backend}</p>
      <p>Software revision: {selected.software_revision}</p><div className="tablewrap"><table aria-label="Local readiness gates"><thead><tr><th>Gate</th><th>Status</th><th>Evidence / blocker</th></tr></thead><tbody>{Object.entries(selected.gates).map(([name,gate])=><tr key={name}><td>{name}</td><td>{gate.status}</td><td>{gate.evidence_reference??gate.reason??'UNKNOWN'}</td></tr>)}</tbody></table></div>
      <pre style={{maxHeight:350,overflow:'auto'}}>{JSON.stringify(selected,null,2)}</pre></Card>}
  </>;
}
