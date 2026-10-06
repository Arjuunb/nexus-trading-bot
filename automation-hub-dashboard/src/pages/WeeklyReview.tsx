import { useState } from "react";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader } from "../components/common/ui";
import { ModeChips, useJournalFilters, useJournalMeta } from "../components/journal/JournalFilters";
import { apiPost, useLive } from "../lib/api";
import { MODE_LABELS, filterQuery, fmtMoney, fmtPct, fmtR, label, tone } from "../lib/journal";

/** Weekly strategy review — observations drawn only from the structured
 *  journal. It reports; it never rewrites strategy logic or risk. */

type Observation = { category: string; strategy: string | null; text: string; sample: number; confidence: string };
type Weekly = {
  week: string; persisted: boolean; observations: Observation[]; guardrails: string[];
  metrics: { total_trades: number; win_rate: number | null; net_pnl: number | null; avg_r: number | null;
    max_drawdown: number | null; profit_factor: number | "INF" | null };
  comparison: { strategy: string; trades: number; win_rate: number | null; net_pnl: number | null; avg_realised_r: number | null }[];
  scope: { modes_applied: string[]; mixed_modes: boolean; mode_warning: string | null };
};
type Saved = { reviews: { id: string; week_key: string; created_at: string; generated_by: string;
  report: { observations: Observation[] }; scope: { modes?: string[] } }[] };

function currentWeek(): string {
  // journal weeks are London ISO weeks (Monday 00:00 London time), like every time on screen
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/London", year: "numeric", month: "numeric", day: "numeric" })
    .formatToParts(new Date());
  const part = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const date = new Date(Date.UTC(part("year"), part("month") - 1, part("day")));
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

const confTone = (c: string) => (c === "SUPPORTED" ? "green" : c === "MODERATE" ? "blue" : "amber");

export default function WeeklyReview() {
  const [filters, setFilters] = useJournalFilters();
  const meta = useJournalMeta();
  const [week, setWeek] = useState(currentWeek());
  const [saving, setSaving] = useState(false);
  const qs = filterQuery({ ...filters, date_from: "", date_to: "" }, { week });
  const review = useLive<Weekly>(`/journal/v2/weekly-review?${qs}`, 30000);
  const saved = useLive<Saved>("/journal/v2/weekly-reviews?limit=12", 30000);
  const w = review.data;

  const save = async () => {
    setSaving(true);
    try { await apiPost(`/journal/v2/weekly-review?${qs}`); await saved.refetch(); }
    finally { setSaving(false); }
  };

  const grouped = (w?.observations ?? []).reduce<Record<string, Observation[]>>((acc, o) => {
    (acc[o.category] ??= []).push(o);
    return acc;
  }, {});

  return (
    <>
      <PageHeader title="Weekly Strategy Review" subtitle="What the journal says about each strategy this week — observations, never automatic changes."
        actions={<button className="btn btn-soft btn-sm" type="button" disabled={saving || !w} onClick={save}>
          <Icon name="check" size={13} /> {saving ? "Saving…" : "Save this review"}</button>} />
      <Card className="tj-filter-card">
        <div className="tj-filter-row">
          <label className="tj-field"><span>Week</span>
            <input aria-label="ISO week" type="week" value={week} onChange={(e) => e.target.value && setWeek(e.target.value)} />
          </label>
        </div>
        <ModeChips filters={filters} setFilters={setFilters} meta={meta.data} applied={w?.scope.modes_applied} />
      </Card>
      {review.error && !w && <div className="card tj-alert"><Icon name="warning" size={14} /> {review.error}</div>}
      {w && (
        <>
          <div className="stat-row">
            {[["Trades", String(w.metrics.total_trades)], ["Win rate", fmtPct(w.metrics.win_rate)],
              ["Net P&L", fmtMoney(w.metrics.net_pnl)], ["Average R", fmtR(w.metrics.avg_r)],
              ["Max drawdown", fmtMoney(w.metrics.max_drawdown)]].map(([k, v]) => (
              <div key={k} className="stat-card"><span className="stat-label">{k}</span><span className="stat-value">{v}</span></div>
            ))}
          </div>
          <Card title={`Observations · ${w.week}`} subtitle={w.scope.modes_applied.map((m) => MODE_LABELS[m] ?? m).join(" + ")}>
            {Object.entries(grouped).map(([category, items]) => (
              <div key={category} className="tj-obs-group">
                <div className="tj-subhead">{label(category)}</div>
                <ul className="tj-obs">
                  {items.map((o, i) => (
                    <li key={i}><span>{o.text}</span>
                      <Badge text={o.confidence === "LOW_SAMPLE" ? `low sample (${o.sample})` : `${label(o.confidence)} (${o.sample})`} tone={confTone(o.confidence) as any} /></li>
                  ))}
                </ul>
              </div>
            ))}
            <ul className="tj-guardrails dim">{w.guardrails.map((g, i) => <li key={i}>{g}</li>)}</ul>
          </Card>
          <Card title="Strategies this week">
            <table className="data-table"><thead><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Avg R</th></tr></thead>
              <tbody>{w.comparison.map((r) => (
                <tr key={r.strategy}><td><b>{r.strategy}</b></td><td>{r.trades}</td><td>{fmtPct(r.win_rate)}</td>
                  <td className={tone(r.net_pnl)}>{fmtMoney(r.net_pnl)}</td><td>{fmtR(r.avg_realised_r)}</td></tr>))}
                {!w.comparison.length && <tr><td colSpan={5} className="dim">No completed trades this week.</td></tr>}</tbody></table>
          </Card>
        </>
      )}
      <Card title="Saved reviews" subtitle="history is kept; saving again adds a new version">
        <ul className="tj-list">
          {(saved.data?.reviews ?? []).map((r) => (
            <li key={r.id}><b>{r.week_key}</b> <span className="dim">{new Date(r.created_at).toLocaleString()} · {r.report.observations.length} observations · {(r.scope.modes ?? []).map((m) => MODE_LABELS[m] ?? m).join(" + ")}</span>
              <button className="btn btn-ghost btn-sm" type="button" onClick={() => setWeek(r.week_key)}>Open</button></li>
          ))}
          {!(saved.data?.reviews ?? []).length && <li className="dim">No saved reviews yet.</li>}
        </ul>
      </Card>
    </>
  );
}
