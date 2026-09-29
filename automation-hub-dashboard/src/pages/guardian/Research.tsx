import { useState } from "react";
import Card from "../../components/common/Card";
import { Badge, StatCard } from "../../components/common/ui";
import { apiPostJson, useLive } from "../../lib/api";
import { ago, type AnalystView, type Hypothesis, HYPOTHESIS_TONE, type ResearchView } from "./common";

const STAGE_TONE = { PASS: "green", FAIL: "red", PENDING: "default" } as const;
const OPEN = new Set(["UNPROVEN", "TESTING", "RECOMMENDED"]);
const r2 = (v: number | null | undefined) => (v == null ? "—" : v.toFixed(2));

/** The owner's controls (PRD §38). Each changes this hypothesis's standing in
 *  Guardian's research store only -- never a strategy. There is no
 *  "optimize production" control, by design. */
function OwnerControls({ h, onDone }: { h: Hypothesis; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!OPEN.has(h.status)) return null;
  const act = async (action: string) => {
    setBusy(true); setError(null);
    try { await apiPostJson(`/guardian/research/${h.id}/action`, { action, note }); setNote(""); onDone(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const canApprove = h.stage === "OWNER_APPROVAL" && h.status === "RECOMMENDED";
  return (
    <div className="gd-owner" data-testid="guardian-owner-controls">
      <input className="gd-input" placeholder="Note (optional)" value={note} maxLength={2000}
        onChange={(e) => setNote(e.target.value)} aria-label="Owner note" />
      <div className="gd-owner-buttons">
        <button type="button" className="btn btn-soft btn-sm" disabled={busy} onClick={() => act("REVIEW")}>Mark reviewed</button>
        {h.stage === "HISTORICAL_BACKTEST" && (
          <button type="button" className="btn btn-soft btn-sm" disabled={busy} onClick={() => act("SEND_TO_BACKTEST")}>Send to backtest</button>)}
        {h.stage === "FORWARD_PAPER" && (
          <button type="button" className="btn btn-soft btn-sm" disabled={busy} onClick={() => act("SEND_TO_FORWARD_PAPER")}>Keep in forward paper</button>)}
        <button type="button" className="btn btn-primary btn-sm" disabled={busy || !canApprove}
          title={canApprove ? "Approve for a person to develop. Production is unchanged." : "Only after every evidence stage passes"}
          onClick={() => act("APPROVE_FOR_DEVELOPMENT")}>Approve for development</button>
        <button type="button" className="btn btn-danger btn-sm" disabled={busy} onClick={() => act("REJECT")}>Reject</button>
      </div>
      {error && <p className="neg">{error}</p>}
    </div>
  );
}

function HypothesisCard({ h, stages, onDone }: { h: Hypothesis; stages: string[]; onDone: () => void }) {
  return (
    <li className="gd-hypothesis" data-testid="guardian-hypothesis">
      <div className="gd-trace-head">
        <Badge text={h.status.replace(/_/g, " ")} tone={HYPOTHESIS_TONE[h.status] ?? "default"} />
        <span className="mono dim">#{h.id} · {h.strategy_id} {h.strategy_version} · {h.record_source}</span>
        <span className="dim">{ago(h.created_at)}</span>
      </div>
      <p><b>{h.hypothesis}</b></p>
      <p className="dim">{h.observation} Found on the first {h.sample} trades only (to {h.discovery_cutoff.slice(0, 16).replace("T", " ")}).</p>
      <ol className="gd-pipeline" aria-label="Research pipeline">
        {stages.map((s) => {
          const r = h.stages[s] ?? { state: "PENDING" };
          const here = s === h.stage && OPEN.has(h.status);
          return (
            <li key={s} className={here ? "gd-stage-here" : ""} title={r.why ?? (here ? "current stage" : "")}>
              <Badge text={r.state === "PENDING" && here ? "WAITING" : r.state} tone={here && r.state === "PENDING" ? "blue" : STAGE_TONE[r.state]} />
              <span>{s.replace(/_/g, " ").toLowerCase()}</span>
            </li>
          );
        })}
      </ol>
      <OwnerControls h={h} onDone={onDone} />
    </li>
  );
}

function Analyst() {
  const live = useLive<AnalystView>("/guardian/research/analyst", 60000);
  const v = live.data;
  if (!v) return <p className="dim">{live.error ? `Unavailable: ${live.error}` : "Loading…"}</p>;
  if (v.error) return <p className="neg">The journal could not be read: {v.error}</p>;
  if (!v.strategies.length) return <p className="dim">No finished forward-paper trades in the journal yet.</p>;
  return (
    <div className="tablewrap">
      <table className="data-table" data-testid="guardian-analyst">
        <thead><tr><th>Strategy</th><th>Version</th><th>Trades</th><th>Win rate</th><th>Avg R</th><th>Total R</th><th>Winners avg R</th><th>Losers avg R</th><th>Cohorts compared</th></tr></thead>
        <tbody>
          {v.strategies.map((s) => (
            <tr key={`${s.record_source}-${s.strategy_id}-${s.strategy_version}`}>
              <td className="mono">{s.strategy_id ?? "—"} <span className="dim">({s.record_source})</span></td>
              <td className="mono">{s.strategy_version ?? "—"}</td>
              <td className="mono">{s.overall.trades}</td>
              <td className="mono">{s.overall.win_rate == null ? "—" : `${(s.overall.win_rate * 100).toFixed(1)}%`}</td>
              <td className="mono">{r2(s.overall.average_r)}</td><td className="mono">{r2(s.overall.total_r)}</td>
              <td className="mono">{r2(s.winners.average_r)}</td><td className="mono">{r2(s.losers.average_r)}</td>
              <td className="mono">{s.cohorts.filter((c) => c.versus_rest).length}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Research intelligence (PRD §12-16): observations become hypotheses that
 *  must pass every stage in order. Nothing here changes a strategy. */
export default function Research() {
  const live = useLive<ResearchView>("/guardian/research", 15000);
  const v = live.data;
  if (live.error && !v) return <p className="neg">Research is unavailable: {live.error}</p>;
  if (!v) return <p className="dim">Loading…</p>;
  const open = v.hypotheses.filter((h) => OPEN.has(h.status));
  const closed = v.hypotheses.filter((h) => !OPEN.has(h.status));
  return (
    <>
      <p className="gd-warning" data-testid="guardian-research-boundary">
        <Badge text="RESEARCH ONLY" tone="amber" /> An observed correlation is not a proven improvement. Guardian
        recommends; approval means “approved for a person to develop”. Production strategies are unchanged until a
        person implements, tests and deploys the change.
      </p>
      <div className="stat-row">
        <StatCard label="Open hypotheses" value={String(open.length)} sub="under test" />
        <StatCard label="Recommended" value={String(v.hypotheses.filter((h) => h.status === "RECOMMENDED").length)}
          tone="amber" sub="awaiting the owner" />
        <StatCard label="Rejected, kept" value={String(v.hypotheses.filter((h) => h.status.startsWith("REJECTED")).length)}
          sub="never rediscovered" />
        <StatCard label="Last research run" value={v.last_run ? ago(new Date(v.last_run.at * 1000).toISOString()) : "not yet"}
          sub={v.last_run ? `${v.last_run.trades} forward-paper trades read` : "runs hourly"} />
      </div>
      <Card title="Hypotheses" subtitle="Every stage runs in order; none is skipped. A failed stage ends the idea.">
        <p className="dim gd-cluster">{v.method}</p>
        {open.length ? (
          <ul className="gd-hypotheses">{open.map((h) => <HypothesisCard key={h.id} h={h} stages={v.stages} onDone={live.refetch} />)}</ul>
        ) : <p className="dim" data-testid="guardian-research-empty">No open hypotheses. One appears when a cohort of at least 20 finished trades loses significantly more than the rest of the same strategy version.</p>}
        {closed.length > 0 && (
          <>
            <h4 className="gd-sub">Decided — kept as history</h4>
            <ul className="gd-hypotheses">{closed.map((h) => <HypothesisCard key={h.id} h={h} stages={v.stages} onDone={live.refetch} />)}</ul>
          </>
        )}
      </Card>
      <Card title="Strategy analyst" subtitle="Per strategy version — versions are never combined">
        <Analyst />
      </Card>
    </>
  );
}
