import { useMemo, useState } from "react";
import Card from "../../components/common/Card";
import Icon from "../../components/common/Icon";
import { Badge } from "../../components/common/ui";
import { apiPost, apiPostJson, useLive } from "../../lib/api";
import { usePref } from "../../lib/prefs";
import { useApp } from "../../app-context";
import { dash, type Finding, money, num, pct, rMult, when, whenFull } from "../../lib/journal";
import EvidenceModal from "./Evidence";

/** Journal > Weekly Reviews. Figures are computed by the application; the
 *  agent's statements are sorted into facts, observations, hypotheses and
 *  recommendations, each linked to the records it rests on. Proposals only
 *  ever wait for a person. */

type Summary = {
  trades: number; wins: number; losses: number; breakevens: number; win_rate: number | null;
  net_pnl: number | null; total_r: number | null; profit_factor: number | null;
  profit_factor_note: string | null; average_r: number | null; max_drawdown: number | null;
  max_drawdown_r: number | null; rule_compliance: number | null; reviewed: number;
  sample_warning: string | null; journal_record_ids: string[];
};
type Report = {
  overall: Summary; long: Summary; short: Summary; by_symbol: Record<string, Summary>;
  by_timeframe: Record<string, Summary>; by_session: Record<string, Summary>;
  by_setup: Record<string, Summary>; by_exit_reason: Record<string, Summary>;
};
type Week = { period_start: string; period_end: string; in_progress: boolean; stats: Report };
type Scope = { agent_id: string; strategy_id: string; label: string };
type Proposal = {
  proposal_id: string; review_id: string; agent_id: string; affected_strategy: string; title: string;
  evidence: { journal_record_ids: string[]; trades: number; total_r: number | null; profit_factor: number | null };
  expected_benefit: string; risk: string; sample_size: number; status: string; created_at: string;
  decided_by: string | null; decided_at: string | null;
};
type ReviewSummary = { review_id: string; agent_id: string; strategy_id: string; period_start: string;
  period_end: string; review_version: number; generated_at: string; journal_record_ids: string[];
  revision?: number; superseded_by?: string | null; revision_reason?: string | null };
type ReviewFull = ReviewSummary & {
  stats: Report; comparison: Record<string, { this: number | null; previous: number | null }> | null;
  findings: { facts: Finding[]; observations: Finding[]; hypotheses: Finding[]; recommendations: Finding[] };
  validation: { records: number; missing_realized_r: string[]; minimal_records: string[]; missing_exit_reason: string[] };
  proposals: Proposal[];
};

const METRICS: [string, (s: Summary) => string][] = [
  ["Trades", (s) => String(s.trades)],
  ["Wins / losses", (s) => (s.trades ? `${s.wins} / ${s.losses}${s.breakevens ? ` / ${s.breakevens} BE` : ""}` : dash)],
  ["Win rate", (s) => pct(s.win_rate)],
  ["Net P&L", (s) => money(s.net_pnl)],
  ["Total R", (s) => rMult(s.total_r)],
  ["Profit factor", (s) => (s.profit_factor != null ? num(s.profit_factor) : s.profit_factor_note ? "No losses" : dash)],
  ["Average R", (s) => rMult(s.average_r)],
  ["Max drawdown", (s) => (s.max_drawdown != null ? `$${num(s.max_drawdown)} · ${rMult(s.max_drawdown_r == null ? null : -s.max_drawdown_r)}` : dash)],
  ["Rule compliance", (s) => (s.rule_compliance != null ? pct(s.rule_compliance, 0) : s.trades ? "Not reviewed" : dash)],
];

function extremes(groups: Record<string, Summary>): { best?: [string, Summary]; worst?: [string, Summary] } {
  const rows = Object.entries(groups).filter(([, s]) => s.trades > 0 && s.total_r != null);
  if (!rows.length) return {};
  rows.sort((a, b) => (b[1].total_r as number) - (a[1].total_r as number));
  return { best: rows[0], worst: rows.length > 1 ? rows[rows.length - 1] : undefined };
}

export default function JournalWeekly() {
  const { toast } = useApp();
  const list = useLive<{ scopes: Scope[]; reviews: ReviewSummary[]; pending_proposals: Proposal[];
    scheduler: { running: boolean; last_result: { written: string[] } | null; week_start_dow: number;
      runs: { agent_id: string; period_start: string; status: string; error: string | null }[] } | null }>(
    "/journal/weekly", 20000);
  const [scopeKey, setScopeKey] = usePref<string>("journal.weekly.scope", "");
  const scopes = list.data?.scopes ?? [];
  const scope = scopes.find((s) => `${s.agent_id}|${s.strategy_id}` === scopeKey) ?? scopes[0];
  const overview = useLive<{ this_week: Week; previous_week: Week; trend: Week[] }>(
    scope ? `/journal/weekly/overview?agent_id=${encodeURIComponent(scope.agent_id)}&strategy_id=${encodeURIComponent(scope.strategy_id)}` : null,
    30000);
  const [openReview, setOpenReview] = useState<string | null>(null);
  const review = useLive<ReviewFull>(openReview ? `/journal/weekly/${openReview}` : null, 60000);
  const [evidence, setEvidence] = useState<{ title: string; ids: string[] } | null>(null);

  const reviews = useMemo(() => (list.data?.reviews ?? []).filter((r) =>
    !scope || (r.agent_id === scope.agent_id && r.strategy_id === scope.strategy_id)), [list.data, scope]);

  const decide = async (p: Proposal, approve: boolean) => {
    const note = window.prompt(approve
      ? "Approve this proposal? Approval records your decision only — nothing in the strategy changes. Optional note:"
      : "Reject this proposal? Optional note:", "");
    if (note === null) return;
    try {
      await apiPostJson(`/journal/proposals/${p.proposal_id}/decision`, { approve, note });
      toast(approve ? "Proposal approved — recorded, not applied" : "Proposal rejected", "success");
      void list.refetch();
    } catch (e) {
      toast(`Could not record the decision: ${(e as Error).message}`, "error");
    }
  };
  const runNow = async () => {
    try {
      const out = await apiPost<{ written: string[] }>("/journal/weekly/run");
      toast(out.written.length ? `${out.written.length} weekly review(s) written` : "Every completed week is already reviewed", "success");
      void list.refetch();
    } catch (e) {
      toast(`Could not run the reviews: ${(e as Error).message}`, "error");
    }
  };

  if (list.data && scopes.length === 0) {
    return (
      <Card title="Weekly reviews">
        <p className="dim">No forward-paper trades have completed yet, so there is nothing to review. Each agent reviews its own
          records once a week — the SMC agent its SMC trades, the PA agent its Price Action trades, and one reviewer per
          Trading Instance — and a review is only written for a week that had trades.</p>
      </Card>
    );
  }

  const tw = overview.data?.this_week.stats;
  const pw = overview.data?.previous_week.stats;
  const setup = tw ? extremes(tw.by_setup) : {};
  const session = tw ? extremes(tw.by_session) : {};
  const tf = tw ? extremes(tw.by_timeframe) : {};

  return (
    <>
      <Card title="Weekly reviews" subtitle="Each agent reviews only its own records"
        right={<button type="button" className="btn btn-soft btn-sm" onClick={() => void runNow()}><Icon name="refresh" size={12} /> Run now</button>}>
        <div className="jr-filters">
          <label className="field">
            <span className="field-label">Agent · strategy</span>
            <select aria-label="Review scope" value={scope ? `${scope.agent_id}|${scope.strategy_id}` : ""}
              onChange={(e) => setScopeKey(e.target.value)}>
              {scopes.map((s) => <option key={`${s.agent_id}|${s.strategy_id}`} value={`${s.agent_id}|${s.strategy_id}`}>{s.label}</option>)}
            </select>
          </label>
        </div>
        {tw && pw && overview.data ? (
          <div className="grid-2-eq jr-weekly-grid">
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Metric</th><th>This week <span className="dim">(so far)</span></th><th>Previous week</th></tr></thead>
                <tbody>
                  {METRICS.map(([label, fmt]) => (
                    <tr key={label}><td>{label}</td><td className="mono">{fmt(tw.overall)}</td><td className="mono">{fmt(pw.overall)}</td></tr>
                  ))}
                </tbody>
              </table>
              {tw.overall.sample_warning && <p className="dim jr-note">{tw.overall.trades} trade(s) this week — descriptive, not evidence.</p>}
            </div>
            <div>
              <h4 className="jr-sub">4-week trend</h4>
              <ul className="jr-trend">
                {overview.data.trend.map((w) => (
                  <li key={w.period_start}>
                    <span className="dim">{when(w.period_start).split(",")[0]}{w.in_progress ? " · now" : ""}</span>
                    <span className="mono">{w.stats.overall.trades} trades</span>
                    <span className={`mono ${(w.stats.overall.total_r ?? 0) > 0 ? "pos" : (w.stats.overall.total_r ?? 0) < 0 ? "neg" : ""}`}>{rMult(w.stats.overall.total_r)}</span>
                  </li>
                ))}
              </ul>
              <h4 className="jr-sub">This week</h4>
              <dl className="cal-kv jr-kv">
                <Pair label="Best setup" value={setup.best} onOpen={setEvidence} />
                <Pair label="Weakest setup" value={setup.worst} onOpen={setEvidence} />
                <Pair label="Best session" value={session.best} onOpen={setEvidence} />
                <Pair label="Worst session" value={session.worst} onOpen={setEvidence} />
                <Pair label="Best timeframe" value={tf.best} onOpen={setEvidence} />
                <Pair label="Worst timeframe" value={tf.worst} onOpen={setEvidence} />
                <div><dt>Long</dt><dd>{tw.long.trades ? `${tw.long.trades} · ${rMult(tw.long.total_r)}` : dash}</dd></div>
                <div><dt>Short</dt><dd>{tw.short.trades ? `${tw.short.trades} · ${rMult(tw.short.total_r)}` : dash}</dd></div>
              </dl>
            </div>
          </div>
        ) : <p className="dim">{overview.error ? `Could not load: ${overview.error}` : "Loading…"}</p>}
      </Card>

      {(list.data?.pending_proposals ?? []).length > 0 && (
        <Card title="Pending improvement proposals" subtitle="Nothing changes unless you approve — and approving records a decision, it applies nothing">
          {(list.data?.pending_proposals ?? []).map((p) => (
            <div key={p.proposal_id} className="jr-proposal">
              <div>
                <b>{p.title}</b>
                <p className="dim">Evidence: {p.evidence.trades} forward-paper trades, {rMult(p.evidence.total_r)}, PF {p.evidence.profit_factor != null ? num(p.evidence.profit_factor) : dash}.
                  Expected benefit: {p.expected_benefit}. Risk: {p.risk}.</p>
              </div>
              <div className="jr-proposal-actions">
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEvidence({ title: p.title, ids: p.evidence.journal_record_ids })}>
                  {p.sample_size} records</button>
                <button type="button" className="btn btn-soft btn-sm" onClick={() => void decide(p, true)}>Approve</button>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => void decide(p, false)}>Reject</button>
              </div>
            </div>
          ))}
        </Card>
      )}

      <Card title="Saved reviews" subtitle={`${reviews.length} review(s) for this scope · written for each completed week, revised if a late trade lands in it`}>
        {reviews.length === 0 ? <p className="dim">No completed week with trades yet for this scope.</p> : (
          <ul className="jr-review-weeks">
            {reviews.map((r) => (
              <li key={r.review_id}>
                <button type="button" className={`jr-week-btn ${openReview === r.review_id ? "active" : ""}`}
                  onClick={() => setOpenReview(openReview === r.review_id ? null : r.review_id)}>
                  <span>{when(r.period_start).split(",")[0]} → {when(r.period_end).split(",")[0]}</span>
                  <span className="dim">{r.journal_record_ids.length} trade(s) · v{r.review_version}
                    {(r.revision ?? 1) > 1 ? ` · revision ${r.revision}` : ""}</span>
                </button>
                {openReview === r.review_id && review.data && (
                  <ReviewBody review={review.data} onOpen={setEvidence} />
                )}
              </li>
            ))}
          </ul>
        )}
        {list.data?.scheduler && (
          <p className="dim jr-note">Scheduler {list.data.scheduler.running ? "running" : "not running"} · weeks start on day {list.data.scheduler.week_start_dow} (0 = Monday, UTC).</p>
        )}
      </Card>

      <EvidenceModal title={evidence?.title ?? ""} ids={evidence?.ids ?? null} onClose={() => setEvidence(null)} />
    </>
  );
}

function Pair({ label, value, onOpen }: { label: string; value?: [string, Summary];
  onOpen: (e: { title: string; ids: string[] }) => void }) {
  return (
    <div><dt>{label}</dt><dd>{value ? (
      <button type="button" className="jr-linkbtn" onClick={() => onOpen({ title: `${label}: ${value[0]}`, ids: value[1].journal_record_ids })}>
        {value[0]} · {rMult(value[1].total_r)} ({value[1].trades})</button>) : dash}</dd></div>
  );
}

function ReviewBody({ review, onOpen }: { review: ReviewFull; onOpen: (e: { title: string; ids: string[] }) => void }) {
  const groups: [keyof ReviewFull["findings"], string, "default" | "blue" | "amber" | "green"][] = [
    ["facts", "Facts", "default"], ["observations", "Observations", "blue"],
    ["hypotheses", "Hypotheses — untested", "amber"], ["recommendations", "Recommendations", "green"],
  ];
  const v = review.validation;
  return (
    <div className="jr-review-body">
      <p className="dim">Generated {whenFull(review.generated_at)} from {v.records} record(s)
        {v.missing_realized_r.length ? ` · ${v.missing_realized_r.length} without known risk (left out of R figures)` : ""}.</p>
      {review.revision_reason && (
        <p className="jr-note"><Badge text={`Revision ${review.revision}`} tone="amber" /> {review.revision_reason}</p>
      )}
      {review.superseded_by && (
        <p className="jr-note"><Badge text="Superseded" tone="amber" /> A later revision of this week replaces this review.</p>
      )}
      {groups.map(([key, title, tone]) => (
        <div key={key} className="jr-findings">
          <Badge text={title} tone={tone} />
          {review.findings[key].length === 0 ? <p className="dim">None.</p> : (
            <ul>{review.findings[key].map((f, i) => (
              <li key={i}>{f.text}{" "}
                {f.journal_record_ids.length > 0 && (
                  <button type="button" className="jr-linkbtn" onClick={() => onOpen({ title: f.text, ids: f.journal_record_ids })}>
                    {f.journal_record_ids.length} record(s)</button>)}
              </li>))}
            </ul>
          )}
        </div>
      ))}
      {review.proposals.length > 0 && (
        <div className="jr-findings">
          <Badge text="Proposals" tone="amber" />
          <ul>{review.proposals.map((p) => <li key={p.proposal_id}>{p.title} — <b>{p.status.replace(/_/g, " ")}</b>
            {p.decided_by ? ` by ${p.decided_by}` : ""}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
