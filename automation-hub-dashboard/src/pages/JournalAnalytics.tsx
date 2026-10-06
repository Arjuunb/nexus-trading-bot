import type { ReactNode } from "react";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader } from "../components/common/ui";
import AreaLine from "../components/chart/AreaLine";
import BarChart from "../components/chart/BarChart";
import JournalFiltersBar, { ModeChips, useJournalFilters, useJournalMeta } from "../components/journal/JournalFilters";
import { useLive } from "../lib/api";
import {
  MODE_LABELS, dash, filterQuery, fmtDuration, fmtLev, fmtMoney, fmtPct, fmtPF, fmtR, fmtRR, isNum, label, tone,
  type GroupRow, type Scope, type Summary,
} from "../lib/journal";

/** Journal analytics — strategy comparison and every breakdown, computed by the
 *  backend over the same filtered journal the Trades tab shows. */

type CompareRow = { strategy: string; family: string | null; modes: string[]; trades: number; wins: number;
  losses: number; break_even: number; win_rate: number | null; net_pnl: number | null;
  avg_planned_rr: number | null; avg_realised_r: number | null; expectancy: number | null;
  expectancy_r: number | null; profit_factor: number | "INF" | null; max_drawdown: number | null;
  avg_leverage: number | null; avg_risk_pct: number | null; total_fees: number | null;
  best_session: string | null; best_symbol: string | null; sample_warning: string | null; verdict: string };
type Side = { trades: number; wins: number; losses: number; win_rate: number | null; net_pnl: number | null;
  avg_r: number | null; profit_factor: number | "INF" | null };
type SideSplit = { LONG: Side; SHORT: Side; stronger_side: string | null };
type RRBlock = { trades: number; avg_planned_rr: number | null; avg_realised_r: number | null;
  avg_realised_r_winners: number | null; difference_r: number | null; winner_capture_pct: number | null;
  pct_full_target: number | null; pct_stopped_before_target: number | null; pct_other_exit: number | null };
type TrendBlock = { status: string; detail: string; recent?: { avg_r: number | null; win_rate: number | null };
  prior?: { avg_r: number | null; win_rate: number | null } };

type Analytics = Scope & {
  summary: Summary;
  comparison: CompareRow[];
  strategies: GroupRow[];
  instances: GroupRow[];
  sessions: { sessions: GroupRow[]; by_strategy: Record<string, GroupRow[]>;
    preferred_window: { inside: GroupRow; outside: GroupRow } | null };
  hours: { hour: number; label: string; trades: number; net_pnl: number | null; win_rate: number | null; avg_r: number | null }[];
  weekdays: GroupRow[];
  symbols: { overall: GroupRow[]; by_strategy: Record<string, GroupRow[]> };
  directions: { overall: SideSplit; by_strategy: Record<string, SideSplit> };
  leverage: { leverage: string; trades: number; win_rate: number | null; net_pnl: number | null; avg_r: number | null;
    avg_risk_pct: number | null; avg_return_on_margin_pct: number | null; avg_drawdown_r: number | null;
    avg_drawdown_amount: number | null; max_drawdown: number | null }[];
  rr: { overall: RRBlock; by_strategy: Record<string, RRBlock>; realised_r_distribution: Record<string, number> };
  excursions: { coverage: { tracked: number; closed: number; note: string }; avg_mfe_r: number | null;
    avg_mae_r: number | null; avg_mfe_amount: number | null; avg_mae_amount: number | null;
    avg_winner_mae_r: number | null; avg_loser_mfe_r: number | null; losers_that_reached_1r: number;
    winner_capture_ratio: number | null };
  trend: { weekly: { week: string; trades: number; net_pnl: number | null; win_rate: number | null; avg_r: number | null;
    cumulative_pnl: number | null }[]; overall: TrendBlock; by_strategy: Record<string, TrendBlock> };
  equity_curve: { at: string; trade_ref: string; cumulative_pnl: number | null; cumulative_r: number | null }[];
};

const trendTone = (s: string) => (s === "IMPROVING" ? "green" : s === "DETERIORATING" ? "red" : s === "STABLE" ? "blue" : "default");

function GroupTable({ rows, first = "Group", empty }: { rows: GroupRow[]; first?: string; empty: string }) {
  return (
    <div className="tablewrap">
      <table className="data-table">
        <thead><tr><th>{first}</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win rate</th><th>Net P&amp;L</th>
          <th>Avg R</th><th>Profit factor</th><th>Avg duration</th></tr></thead>
        <tbody>
          {rows.filter((r) => r.total_trades).map((r) => (
            <tr key={r.key}>
              <td><b>{r.label}</b>{r.sample_warning ? <span className="dim" title="fewer than 30 trades"> · early</span> : null}</td>
              <td>{r.total_trades}</td><td>{r.wins}</td><td>{r.losses}</td><td>{fmtPct(r.win_rate)}</td>
              <td className={tone(r.net_pnl)}>{fmtMoney(r.net_pnl)}</td><td className={tone(r.avg_r)}>{fmtR(r.avg_r)}</td>
              <td>{fmtPF(r.profit_factor)}</td><td>{fmtDuration(r.avg_duration_s)}</td>
            </tr>
          ))}
          {!rows.some((r) => r.total_trades) && <tr><td colSpan={9} className="dim">{empty}</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return <Card title={title} subtitle={subtitle}>{children}</Card>;
}

export default function JournalAnalytics() {
  const [filters, setFilters] = useJournalFilters();
  const meta = useJournalMeta();
  const res = useLive<Analytics>(`/journal/v2/analytics?${filterQuery(filters)}`, 15000);
  const a = res.data;
  const strategies = a ? Object.keys(a.symbols.by_strategy) : [];
  // trades with no recorded P&L have no point on the curve (never a flat $0 step)
  const pnlCurve = a ? a.equity_curve.filter((p) => isNum(p.cumulative_pnl)) : [];

  return (
    <>
      <PageHeader title="Journal Analytics" subtitle="Which strategy is making money, where, when and how — from the structured journal." />
      <Card className="tj-filter-card">
        <ModeChips filters={filters} setFilters={setFilters} meta={meta.data} applied={a?.modes_applied} />
        {a?.mixed_modes && <div className="tj-mixed"><Icon name="warning" size={13} /> {a.mode_warning}</div>}
        <JournalFiltersBar filters={filters} setFilters={setFilters} meta={meta.data} />
      </Card>
      {res.error && !a && <div className="card tj-alert"><Icon name="warning" size={14} /> {res.error}</div>}
      {!a ? <div className="dim tj-pad">Loading analytics…</div> : (
        <>
          <Section title="Strategy comparison" subtitle={`${a.modes_applied.map((m) => MODE_LABELS[m] ?? m).join(" + ")} · ranked by net P&L`}>
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Strategy</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win rate</th><th>Net P&amp;L</th>
                  <th>Avg planned RR</th><th>Avg realised R</th><th>Profit factor</th><th>Max DD</th><th>Avg leverage</th>
                  <th>Best session</th><th>Best symbol</th><th>Verdict</th></tr></thead>
                <tbody>
                  {a.comparison.map((r) => (
                    <tr key={r.strategy}>
                      <td><b>{r.strategy}</b><div className="dim" style={{ fontSize: 11 }}>{r.family ?? ""}</div></td>
                      <td>{r.trades}</td><td>{r.wins}</td><td>{r.losses}</td><td>{fmtPct(r.win_rate)}</td>
                      <td className={tone(r.net_pnl)}><b>{fmtMoney(r.net_pnl)}</b></td>
                      <td>{fmtRR(r.avg_planned_rr)}</td><td className={tone(r.avg_realised_r)}>{fmtR(r.avg_realised_r)}</td>
                      <td>{fmtPF(r.profit_factor)}</td><td className={tone(r.max_drawdown)}>{fmtMoney(r.max_drawdown)}</td>
                      <td>{fmtLev(r.avg_leverage)}</td><td>{r.best_session ?? dash}</td><td>{r.best_symbol ?? dash}</td>
                      <td><Badge text={label(r.verdict)} tone={r.verdict.startsWith("PROFITABLE") ? "green" : r.verdict.startsWith("LOSING") ? "red" : "default"} /></td>
                    </tr>
                  ))}
                  {a.comparison.length === 0 && <tr><td colSpan={14} className="dim">No closed trades for these filters.</td></tr>}
                </tbody>
              </table>
            </div>
          </Section>

          <div className="grid-2-eq tj-grid">
            <Section title="Equity curve" subtitle="cumulative net P&L by exit time">
              <div style={{ height: 220 }}>
                {pnlCurve.length ? (
                  <AreaLine labels={pnlCurve.map((p) => p.trade_ref)}
                    series={[{ name: "Net P&L", data: pnlCurve.map((p) => p.cumulative_pnl as number), color: "#eab54f" }]}
                    valueFormatter={(v) => fmtMoney(v)} />
                ) : <div className="dim tj-pad">{a.equity_curve.length ? "No P&L was recorded for these trades." : "No closed trades."}</div>}
              </div>
            </Section>
            <Section title="Is it improving?" subtitle="recent trades vs the ones before, by average R">
              <div className="tj-trend">
                <div className="tj-trend-row"><b>All strategies</b><Badge text={label(a.trend.overall.status)} tone={trendTone(a.trend.overall.status) as any} /><span className="dim">{a.trend.overall.detail}</span></div>
                {Object.entries(a.trend.by_strategy).map(([name, t]) => (
                  <div key={name} className="tj-trend-row"><b>{name}</b><Badge text={label(t.status)} tone={trendTone(t.status) as any} />
                    <span className="dim">{t.recent ? `${fmtR(t.recent.avg_r)} now vs ${fmtR(t.prior?.avg_r)} before` : t.detail}</span></div>
                ))}
              </div>
              <table className="data-table"><thead><tr><th>Week</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Avg R</th></tr></thead>
                <tbody>{a.trend.weekly.slice(-8).map((w) => (
                  <tr key={w.week}><td>{w.week}</td><td>{w.trades}</td><td>{fmtPct(w.win_rate)}</td><td className={tone(w.net_pnl)}>{fmtMoney(w.net_pnl)}</td><td>{fmtR(w.avg_r)}</td></tr>))}</tbody></table>
            </Section>
          </div>

          <Section title="Strategy performance" subtitle="full statistics per strategy">
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Strategy</th><th>Trades</th><th>W / L / BE</th><th>Win rate</th><th>Net P&amp;L</th><th>Gross profit</th>
                  <th>Gross loss</th><th>PF</th><th>Avg win</th><th>Avg loss</th><th>Avg R</th><th>Expectancy</th><th>Best</th><th>Worst</th>
                  <th>Max DD</th><th>Avg duration</th><th>Fees</th><th>Avg lev.</th><th>Avg size</th></tr></thead>
                <tbody>
                  {a.strategies.filter((r) => r.total_trades).map((r) => (
                    <tr key={r.key}>
                      <td><b>{r.label}</b></td><td>{r.total_trades}</td><td>{r.wins} / {r.losses} / {r.break_even}</td>
                      <td>{fmtPct(r.win_rate)}</td><td className={tone(r.net_pnl)}>{fmtMoney(r.net_pnl)}</td>
                      <td>{fmtMoney(r.gross_profit, false)}</td><td>{fmtMoney(r.gross_loss, false)}</td><td>{fmtPF(r.profit_factor)}</td>
                      <td>{fmtMoney(r.avg_win)}</td><td>{fmtMoney(r.avg_loss)}</td><td className={tone(r.avg_r)}>{fmtR(r.avg_r)}</td>
                      <td className={tone(r.expectancy)}>{fmtMoney(r.expectancy)}</td>
                      <td>{r.best_trade ? `${fmtMoney(r.best_trade.net_pnl)}` : dash}</td>
                      <td>{r.worst_trade ? `${fmtMoney(r.worst_trade.net_pnl)}` : dash}</td>
                      <td>{fmtMoney(r.max_drawdown)}</td><td>{fmtDuration(r.avg_duration_s)}</td><td>{fmtMoney(r.total_fees, false)}</td>
                      <td>{fmtLev(r.avg_leverage)}</td><td>{fmtMoney(r.avg_position_size, false)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title="Per trading instance / lab"><GroupTable rows={a.instances} first="Instance / lab" empty="No instance trades." /></Section>

          <div className="grid-2-eq tj-grid">
            <Section title="Session performance" subtitle="classified in London / New York / Tokyo local time">
              <GroupTable rows={a.sessions.sessions} first="Session" empty="No closed trades." />
              {a.sessions.preferred_window && (
                <div className="dim tj-pad">Inside configured window: {a.sessions.preferred_window.inside.total_trades} trades, {fmtMoney(a.sessions.preferred_window.inside.net_pnl)} ·
                  outside: {a.sessions.preferred_window.outside.total_trades} trades, {fmtMoney(a.sessions.preferred_window.outside.net_pnl)}</div>
              )}
            </Section>
            <Section title="Net P&L by hour of day" subtitle="entry hour, London time">
              <div style={{ height: 240 }}>
                <BarChart labels={a.hours.map((h) => `${String(h.hour).padStart(2, "0")}:00`)} data={a.hours.map((h) => h.net_pnl ?? 0)} diverging />
              </div>
              <div className="tj-hours">
                {a.hours.filter((h) => h.trades).map((h) => (
                  <span key={h.hour} className={tone(h.net_pnl)}>{h.label}: {fmtMoney(h.net_pnl)} ({h.trades})</span>
                ))}
              </div>
            </Section>
          </div>

          {Object.keys(a.sessions.by_strategy).length > 0 && (
            <Section title="Sessions per strategy">
              <div className="tj-split">
                {Object.entries(a.sessions.by_strategy).map(([name, rows]) => (
                  <div key={name}><div className="tj-subhead">{name}</div><GroupTable rows={rows} first="Session" empty="—" /></div>
                ))}
              </div>
            </Section>
          )}

          <Section title="Symbol performance per strategy">
            <div className="tj-split">
              {strategies.map((name) => (
                <div key={name}><div className="tj-subhead">{name}</div><GroupTable rows={a.symbols.by_strategy[name]} first="Symbol" empty="—" /></div>
              ))}
              {!strategies.length && <div className="dim">No closed trades.</div>}
            </div>
          </Section>

          <Section title="Long vs short" subtitle="does a strategy work better in one direction?">
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Strategy</th><th>Long trades</th><th>Long win rate</th><th>Long net</th><th>Long avg R</th>
                  <th>Short trades</th><th>Short win rate</th><th>Short net</th><th>Short avg R</th><th>Stronger side</th></tr></thead>
                <tbody>
                  {[["All", a.directions.overall] as const, ...Object.entries(a.directions.by_strategy)].map(([name, d]) => (
                    <tr key={name}>
                      <td><b>{name}</b></td>
                      <td>{d.LONG.trades}</td><td>{fmtPct(d.LONG.win_rate)}</td><td className={tone(d.LONG.net_pnl)}>{fmtMoney(d.LONG.net_pnl)}</td><td>{fmtR(d.LONG.avg_r)}</td>
                      <td>{d.SHORT.trades}</td><td>{fmtPct(d.SHORT.win_rate)}</td><td className={tone(d.SHORT.net_pnl)}>{fmtMoney(d.SHORT.net_pnl)}</td><td>{fmtR(d.SHORT.avg_r)}</td>
                      <td>{d.stronger_side ? <Badge text={d.stronger_side} tone={d.stronger_side === "LONG" ? "green" : "red"} /> : <span className="dim">no clear edge</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <div className="grid-2-eq tj-grid">
            <Section title="Leverage analysis" subtitle="compare on R and return on margin, not raw P&L">
              <table className="data-table">
                <thead><tr><th>Leverage</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Avg R</th><th>Avg risk</th><th>Avg RoM</th><th>Avg drawdown</th></tr></thead>
                <tbody>{a.leverage.map((r) => (
                  <tr key={r.leverage}><td><b>{r.leverage}</b></td><td>{r.trades}</td><td>{fmtPct(r.win_rate)}</td>
                    <td className={tone(r.net_pnl)}>{fmtMoney(r.net_pnl)}</td><td>{fmtR(r.avg_r)}</td><td>{fmtPct(r.avg_risk_pct, 2)}</td>
                    <td>{fmtPct(r.avg_return_on_margin_pct, 2)}</td><td>{fmtR(r.avg_drawdown_r)}</td></tr>))}
                  {!a.leverage.length && <tr><td colSpan={8} className="dim">No closed trades.</td></tr>}</tbody>
              </table>
            </Section>
            <Section title="Planned RR vs realised RR" subtitle="is execution eroding valid setups?">
              <div className="tj-kv-grid">
                {[["Avg planned RR", fmtRR(a.rr.overall.avg_planned_rr)], ["Avg realised R", fmtR(a.rr.overall.avg_realised_r)],
                  ["Difference", fmtR(a.rr.overall.difference_r)], ["Winners realised", fmtR(a.rr.overall.avg_realised_r_winners)],
                  ["Winner capture", fmtPct(a.rr.overall.winner_capture_pct)], ["Reached full target", fmtPct(a.rr.overall.pct_full_target)],
                  ["Stopped before target", fmtPct(a.rr.overall.pct_stopped_before_target)], ["Other exits", fmtPct(a.rr.overall.pct_other_exit)]]
                  .map(([k, v]) => <div key={k} className="tj-kv"><span className="dim">{k}</span><b>{v}</b></div>)}
              </div>
              <table className="data-table"><thead><tr><th>Strategy</th><th>Planned</th><th>Realised</th><th>Full target</th><th>Stopped early</th></tr></thead>
                <tbody>{Object.entries(a.rr.by_strategy).map(([name, b]) => (
                  <tr key={name}><td>{name}</td><td>{fmtRR(b.avg_planned_rr)}</td><td>{fmtR(b.avg_realised_r)}</td>
                    <td>{fmtPct(b.pct_full_target)}</td><td>{fmtPct(b.pct_stopped_before_target)}</td></tr>))}</tbody></table>
            </Section>
          </div>

          <Section title="MAE / MFE" subtitle={a.excursions.coverage.note}>
            <div className="tj-kv-grid">
              {[["Tracked trades", `${a.excursions.coverage.tracked} of ${a.excursions.coverage.closed}`],
                ["Avg MFE", `${fmtR(a.excursions.avg_mfe_r)} · ${fmtMoney(a.excursions.avg_mfe_amount)}`],
                ["Avg MAE", `${fmtR(a.excursions.avg_mae_r)} · ${fmtMoney(a.excursions.avg_mae_amount)}`],
                ["Winners' avg MAE", fmtR(a.excursions.avg_winner_mae_r)], ["Losers' avg MFE", fmtR(a.excursions.avg_loser_mfe_r)],
                ["Losers that reached +1R", String(a.excursions.losers_that_reached_1r)],
                ["Winner capture ratio", a.excursions.winner_capture_ratio == null ? dash : `${(a.excursions.winner_capture_ratio * 100).toFixed(0)}% of MFE`]]
                .map(([k, v]) => <div key={k} className="tj-kv"><span className="dim">{k}</span><b>{v}</b></div>)}
            </div>
          </Section>
        </>
      )}
    </>
  );
}
