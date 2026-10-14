import { useState } from "react";
import Card from "../components/common/Card";
import { PageHeader, StatCard } from "../components/common/ui";
import { apiPostJson, useLive } from "../lib/api";
import "./ShadowStrategyLab.css";
import ShadowComparisons from "./ShadowComparisons";

type Candidate = { candidate_id: string; strategy_id: string; strategy_version?: string; config_fingerprint?: string; symbol?: string; timeframe?: string };
type Registration = Candidate & { registration_id: string; status: string; readiness?: string; venue?: string; market_type?: string; observation_start?: string; feed_freshness?: string; blocking_reasons?: string[]; heartbeat?: number };
type Requirements = { valid: boolean; lookback_spec?: Record<string, unknown>; reviewed_source_hash?: string; errors?: string[] };
type Decision = { decision_id: string; candle_close_timestamp: string; observed_at: string; status: string; rejection_reasons: string[]; data_freshness: string; evaluation_latency_ms: number; candle_hash: string; evidence_completeness: string; market_context?: { session?: string; trend_regime?: string; volatility_regime?: string } };
type Coverage = { scheduled_opportunities: number; eligible_opportunities: number; signal_count: number; data_block_count: number; observation_gaps: number; evidence_completeness: string };
type Comparison = { active_state: string; matched_opportunities: number; agreement_rate: number | null; warning: string };
type Audit = { event_id: string; action: string; timestamp: string; payload: Record<string, unknown> };
const base = "/api/v2/strategy-intelligence";
const unknown = (value: unknown) => value === null || value === undefined || value === "" ? "UNKNOWN" : String(value);

export default function ShadowStrategyLab() {
  const candidates = useLive<{ rows: Candidate[] }>(`${base}/research-candidates`, 30000);
  const registrations = useLive<{ rows: Registration[] }>(`${base}/shadow/registrations`, 5000);
  const [candidateId, setCandidateId] = useState("");
  const [registrationId, setRegistrationId] = useState("");
  const [authorize, setAuthorize] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const requirements = useLive<Requirements>(candidateId ? `${base}/shadow/requirements/${encodeURIComponent(candidateId)}` : "", 30000);
  const selectedPath = registrationId ? `${base}/shadow/registrations/${encodeURIComponent(registrationId)}` : "";
  const status = useLive<Registration>(selectedPath, 5000);
  const decisions = useLive<{ rows: Decision[] }>(selectedPath ? `${selectedPath}/decisions?limit=50` : "", 5000);
  const coverage = useLive<Coverage>(selectedPath ? `${selectedPath}/coverage` : "", 5000);
  const comparison = useLive<Comparison>(selectedPath ? `${selectedPath}/comparison` : "", 10000);
  const audit = useLive<{ rows: Audit[] }>(selectedPath ? `${selectedPath}/audit` : "", 10000);

  async function refresh() {
    await Promise.all([registrations.refetch(), status.refetch(), decisions.refetch(), coverage.refetch(), comparison.refetch(), audit.refetch()]);
  }
  async function register() {
    setBusy(true); setError(null);
    try {
      const result = await apiPostJson<Registration>(`${base}/shadow/registrations`, {
        candidate_id: candidateId, idempotency_key: key, authorize, authorization_reason: reason,
        lookback_spec: requirements.data?.lookback_spec, reviewed_source_hash: requirements.data?.reviewed_source_hash,
      });
      setRegistrationId(result.registration_id); setKey(crypto.randomUUID()); await registrations.refetch();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  async function action(actionName: string) {
    setBusy(true); setError(null);
    try { await apiPostJson(`${selectedPath}/${actionName.toLowerCase()}`, { reason }); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  const messages = [error, candidates.error, registrations.error, requirements.error, status.error, decisions.error, coverage.error, comparison.error, audit.error].filter(Boolean);
  const candidate = candidates.data?.rows?.find(c => c.candidate_id === candidateId);
  const reg = status.data?.registration_id === registrationId ? status.data : null;
  const allowed: Record<string, string[]> = { VALIDATE: ["DRAFT", "READY", "BLOCKED"], START: ["READY"], PAUSE: ["READY", "OBSERVING", "BLOCKED"], RESUME: ["PAUSED"], STOP: ["DRAFT", "READY", "OBSERVING", "PAUSED", "BLOCKED", "FAILED"] };
  return <div className="shadow-lab">
    <PageHeader title="Shadow Strategy Lab" subtitle="Finalized public market data → read-only decisions. No orders, simulated fills or trading profitability." />
    {messages.map((message, i) => <p role="alert" key={i}>{message}</p>)}
    <Card title="Candidate Registration">
      <label>Frozen research candidate<select aria-label="Shadow research candidate" value={candidateId} onChange={e => { setCandidateId(e.target.value); setAuthorize(false); setKey(crypto.randomUUID()); }}><option value="">Select a candidate</option>{(candidates.data?.rows || []).map(c => <option key={c.candidate_id} value={c.candidate_id}>{c.strategy_id} · {c.strategy_version} · {c.symbol} · {c.timeframe}</option>)}</select></label>
      <p className="break-all">Configuration fingerprint: {unknown(candidate?.config_fingerprint)}</p>
      <p>Read-only adapter / lookback: {candidateId ? requirements.data?.valid ? "VERIFIED SPECIFICATION — human review required" : "BLOCKED / UNKNOWN" : "UNKNOWN"}</p>
      {requirements.data?.errors?.map(message => <p key={message}>{message}</p>)}
      {requirements.data?.lookback_spec && <details><summary>Review lookback and source identity</summary><pre className="overflow-auto">{JSON.stringify(requirements.data, null, 2)}</pre></details>}
      <label>Audit reason<input aria-label="Shadow authorization reason" value={reason} onChange={e => setReason(e.target.value)} maxLength={1000} /></label>
      <label><input type="checkbox" checked={authorize} onChange={e => setAuthorize(e.target.checked)} />I authorize this frozen configuration and reviewed lookback for shadow observation only.</label>
      <button disabled={busy || !candidateId || !requirements.data?.valid || !authorize || !reason.trim()} onClick={() => void register()}>Register shadow candidate</button>
    </Card>
    <Card title="Active Observations">
      <label>Shadow registration<select aria-label="Shadow registration" value={registrationId} onChange={e => setRegistrationId(e.target.value)}><option value="">Select an observation</option>{(registrations.data?.rows || []).map(r => <option key={r.registration_id} value={r.registration_id}>{r.strategy_id} · {r.symbol} · {r.status}</option>)}</select></label>
      {!registrations.loading && !registrations.data?.rows?.length && <p>No shadow observations registered.</p>}
      {reg && <><p>{reg.strategy_id} {reg.strategy_version} · {reg.venue} / {reg.market_type} · {reg.symbol} {reg.timeframe}</p><p className="break-all">Frozen fingerprint: {reg.config_fingerprint}</p><p aria-live="polite">Observation status: {reg.status}</p><p>Last evaluated state — Lookback readiness: {unknown(reg.readiness)} · Feed freshness: {unknown(reg.feed_freshness)}</p><p>Observation start: {unknown(reg.observation_start)}</p>{reg.blocking_reasons?.map(r => <p key={r}>{r}</p>)}<div className="flex flex-wrap gap-2">{Object.keys(allowed).map(name => <button key={name} disabled={busy || !reason.trim() || !allowed[name].includes(reg.status)} onClick={() => void action(name)}>{name}</button>)}<button disabled={busy} onClick={() => void refresh()}>Refresh observations</button></div></>}
    </Card>
    {registrationId && <>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4"><StatCard label="Eligible decisions" value={unknown(coverage.data?.eligible_opportunities)} /><StatCard label="Signals (not trades)" value={unknown(coverage.data?.signal_count)} /><StatCard label="Data-blocked opportunities" value={unknown(coverage.data?.data_block_count)} /><StatCard label="Observation gap events" value={unknown(coverage.data?.observation_gaps)} /></div>
      <Card title="Data Quality and Reliability"><p>Evidence completeness: {unknown(coverage.data?.evidence_completeness)}</p><p>Market regime and volatility may be UNKNOWN when per-candle publication evidence is unavailable. Missing periods are never backfilled as forward observations.</p></Card>
      <Card title="Shadow Decision Timeline"><div className="overflow-auto"><table aria-label="Shadow decision timeline"><thead><tr><th>Candle close (UTC)</th><th>Observed at</th><th>Decision</th><th>Freshness</th><th>Latency ms</th><th>Evidence</th><th>Reasons</th></tr></thead><tbody>{(decisions.data?.rows || []).map(d => <tr key={d.decision_id}><td>{d.candle_close_timestamp}</td><td>{d.observed_at}</td><td>{d.status}</td><td>{d.data_freshness}</td><td>{unknown(d.evaluation_latency_ms)}</td><td>{d.evidence_completeness}</td><td>{d.rejection_reasons.join(", ")}<details><summary>Decision evidence</summary><pre className="overflow-auto">{JSON.stringify(d, null, 2)}</pre></details></td></tr>)}</tbody></table></div>{!decisions.data?.rows?.length && <p>No committed shadow decisions.</p>}</Card>
      <Card title="Shadow vs Active Comparison"><p>Active evidence: {unknown(comparison.data?.active_state)}</p><p>Matched opportunities: {unknown(comparison.data?.matched_opportunities)}</p><p>Signal agreement: {comparison.data?.agreement_rate == null ? "UNAVAILABLE" : `${(comparison.data.agreement_rate * 100).toFixed(1)}%`}</p><p>{comparison.data?.warning || "Signal agreement is not trading performance. Missing active evidence is not NO_SIGNAL."}</p></Card>
      <ShadowComparisons key={registrationId} registrationId={registrationId}/>
      <Card title="Observation Audit History">{(audit.data?.rows || []).map(event => <details key={event.event_id}><summary>{event.timestamp} · {event.action}</summary><pre className="overflow-auto">{JSON.stringify(event.payload, null, 2)}</pre></details>)}</Card>
    </>}
  </div>;
}
