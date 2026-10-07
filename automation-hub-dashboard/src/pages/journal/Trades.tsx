import { useEffect, useMemo, useState } from "react";
import Card from "../../components/common/Card";
import Icon from "../../components/common/Icon";
import { Badge, StatCard } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import { usePref } from "../../lib/prefs";
import {
  compliance, coverage, dash, duration, type Kpis, money, num, ORIGINS, originTone, outcomeTone, pct,
  price,
  profitFactor, type RecordRow, rMult, SOURCE_LABEL, statusTone, when,
} from "../../lib/journal";
import TradeDetail from "./TradeDetail";

/** Journal > Trades: canonical trade records, one per execution lifecycle.
 *  Forward-paper records are the default; legacy, simulation, backtest and
 *  research records appear only when chosen, labelled, and never mixed into
 *  the forward-paper figures. */

type Facets = Record<string, string[]> & { counts?: Record<string, number> };
type Filters = {
  origin: string; source: string; instance_id: string; strategy: string; symbol: string;
  timeframe: string; side: string; session: string; mode: string; outcome: string;
  reviewed: string; date_from: string; date_to: string;
};
const EMPTY: Filters = {
  origin: "FORWARD_PAPER", source: "", instance_id: "", strategy: "", symbol: "", timeframe: "",
  side: "", session: "", mode: "", outcome: "", reviewed: "", date_from: "", date_to: "",
};
/** Which facet list validates each filter. A saved value that no longer
 *  exists (a deleted instance, a renamed strategy) is dropped instead of
 *  silently narrowing the list to nothing. */
const FACET_OF: Partial<Record<keyof Filters, string>> = {
  source: "record_source", instance_id: "instance_id", strategy: "strategy_name", symbol: "symbol",
  timeframe: "timeframe", side: "side", session: "trading_session", mode: "operating_mode",
};

export default function JournalTrades({ focusId }: { focusId?: string }) {
  const [saved, setSaved] = usePref<Filters>("journal.records.filters", EMPTY);
  const [open, setOpen] = useState<string | null>(focusId ?? null);
  useEffect(() => { if (focusId) setOpen(focusId); }, [focusId]);
  const facets = useLive<Facets>("/journal/records/facets", 30000);
  const recorder = useLive<{ running: boolean; last_reconcile: { at: string; errors: string[] } | null }>(
    "/journal/recorder", 15000);

  const filters = useMemo(() => {
    const f = { ...EMPTY, ...saved };
    if (!facets.data) return f;
    for (const [key, facet] of Object.entries(FACET_OF) as [keyof Filters, string][]) {
      const values = facets.data[facet] ?? [];
      if (f[key] && !values.includes(f[key])) f[key] = "";
    }
    return f;
  }, [saved, facets.data]);
  const set = (patch: Partial<Filters>) => setSaved({ ...filters, ...patch });

  const qs = new URLSearchParams({ limit: "200" });
  // Dates are London days (the server reads a bare date that way).
  for (const [k, v] of Object.entries(filters)) if (v) qs.set(k, k.startsWith("date_") ? v.slice(0, 10) : v);
  const data = useLive<{ records: RecordRow[]; total: number; kpis: Kpis; origin: string }>(
    `/journal/records?${qs.toString()}`, 8000);

  if (open) return <TradeDetail id={open} onBack={() => setOpen(null)} />;

  const k = data.data?.kpis;
  const insufficient = (value: string) => (k && k.trades === 0 ? "Insufficient data" : value);
  const counts = facets.data?.counts ?? {};
  const rows = data.data?.records ?? [];
  const legacyWaiting = filters.origin === "FORWARD_PAPER" && (counts.LEGACY_MIGRATION ?? 0) > 0;
  const activeFilters = Object.entries(filters).filter(([key, v]) => v && key !== "origin").length;

  return (
    <>
      <div className="stat-row six">
        <StatCard label="Completed trades" value={k ? String(k.trades) : dash}
          sub={k ? `${k.wins}W · ${k.losses}L · ${k.breakevens}BE` : undefined} />
        <StatCard label="Net P&L" value={insufficient(money(k?.net_pnl))}
          tone={(k?.net_pnl ?? 0) > 0 ? "green" : (k?.net_pnl ?? 0) < 0 ? "red" : "default"}
          sub={coverage(k?.pnl_known, k?.trades, "P&L")} />
        <StatCard label="Total R" value={insufficient(rMult(k?.total_r))}
          tone={(k?.total_r ?? 0) > 0 ? "green" : (k?.total_r ?? 0) < 0 ? "red" : "default"}
          sub={coverage(k?.r_known, k?.trades, "R")} />
        <StatCard label="Profit factor" value={insufficient(profitFactor(k?.profit_factor, k?.profit_factor_note))}
          sub={k?.profit_factor == null && k?.profit_factor_note !== "no losing trades"
            ? k?.profit_factor_note ?? undefined : undefined} />
        <StatCard label="Win rate" value={insufficient(pct(k?.win_rate))}
          sub={k?.sample_warning ? `${k.trades} trades — not yet evidence` : undefined} />
        <StatCard label="Rule compliance"
          value={k && k.trades ? compliance(k.rule_compliance, k.trades, k.reviewed) : "Insufficient data"}
          sub={k?.reviewed ? `${k.reviewed} reviewed` : undefined} />
      </div>

      <Card title="Trade records"
        subtitle={`${data.data?.total ?? 0} record(s)${rows.length < (data.data?.total ?? 0)
          ? ` · showing the newest ${rows.length}` : ""} · ${ORIGINS.find(([id]) => id === filters.origin)?.[1] ?? filters.origin}`}
        right={recorder.data?.last_reconcile ? (
          <span className="dim jr-recorder" title="The recorder rebuilds records from execution facts">
            <Icon name={recorder.data.last_reconcile.errors?.length ? "warning" : "check"} size={12} />
            reconciled {when(recorder.data.last_reconcile.at)}
          </span>) : undefined}>
        <div className="chips jr-origins" role="group" aria-label="Record origin">
          {ORIGINS.map(([id, label]) => (
            <button key={id} type="button" className={`chip-btn ${filters.origin === id ? "active" : ""}`}
              onClick={() => set({ origin: id })}>
              {label}{id !== "all" && counts[id] ? ` · ${counts[id]}` : ""}
            </button>
          ))}
        </div>
        {legacyWaiting && (
          <p className="dim jr-note">
            {counts.LEGACY_MIGRATION} legacy record(s) from the old journal are kept separately and never
            counted as forward-paper performance.{" "}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => set({ origin: "LEGACY_MIGRATION" })}>
              Show legacy
            </button>
          </p>
        )}

        <div className="jr-filters">
          <Select label="Source" value={filters.source} onChange={(v) => set({ source: v })}
            options={(facets.data?.record_source ?? []).map((s) => [s, SOURCE_LABEL[s] ?? s])} />
          <Select label="Instance" value={filters.instance_id} onChange={(v) => set({ instance_id: v })}
            options={(facets.data?.instance_id ?? []).map((s) => [s, s.slice(0, 8)])} />
          <Select label="Strategy" value={filters.strategy} onChange={(v) => set({ strategy: v })}
            options={(facets.data?.strategy_name ?? []).map((s) => [s, s])} />
          <Select label="Symbol" value={filters.symbol} onChange={(v) => set({ symbol: v })}
            options={(facets.data?.symbol ?? []).map((s) => [s, s])} />
          <Select label="TF" value={filters.timeframe} onChange={(v) => set({ timeframe: v })}
            options={(facets.data?.timeframe ?? []).map((s) => [s, s])} />
          <Select label="Side" value={filters.side} onChange={(v) => set({ side: v })}
            options={[["long", "Long"], ["short", "Short"]]} />
          <Select label="Session" value={filters.session} onChange={(v) => set({ session: v })}
            options={(facets.data?.trading_session ?? []).map((s) => [s, s.replace(/_/g, " ")])} />
          <Select label="Mode" value={filters.mode} onChange={(v) => set({ mode: v })}
            options={[["paper", "Paper"], ["live", "Live"]]} />
          <Select label="Result" value={filters.outcome} onChange={(v) => set({ outcome: v })}
            options={[["WIN", "Win"], ["LOSS", "Loss"], ["BREAKEVEN", "Breakeven"]]} />
          <Select label="Reviewed" value={filters.reviewed} onChange={(v) => set({ reviewed: v })}
            options={[["yes", "Reviewed"], ["no", "Unreviewed"]]} />
          <label className="field">
            <span className="field-label">From</span>
            <input type="date" value={filters.date_from.slice(0, 10)}
              onChange={(e) => set({ date_from: e.target.value })} />
          </label>
          <label className="field">
            <span className="field-label">To</span>
            <input type="date" value={filters.date_to.slice(0, 10)}
              onChange={(e) => set({ date_to: e.target.value })} />
          </label>
          {activeFilters > 0 && (
            <button type="button" className="btn btn-ghost btn-sm jr-clear"
              onClick={() => setSaved({ ...EMPTY, origin: filters.origin })}>Clear {activeFilters}</button>
          )}
        </div>

        <div className="tablewrap">
          <table className="data-table jr-table">
            <thead>
              <tr>
                <th>Date / time</th><th>Source</th><th>Strategy</th><th>Symbol</th><th>TF</th><th>Side</th>
                <th>Entry</th><th>SL</th><th>TP</th><th>Exit</th><th>Risk</th><th>Planned RR</th>
                <th>Actual R</th><th>Net P&amp;L</th><th>Result</th><th>Duration</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.journal_record_id} className="jr-row" onClick={() => setOpen(r.journal_record_id)}>
                  <td className="mono dim">
                    <button type="button" className="jr-open" aria-label={`Open ${r.symbol} ${r.side} trade record`}
                      onClick={(e) => { e.stopPropagation(); setOpen(r.journal_record_id); }}>
                      {when(r.position_opened_at ?? r.signal_detected_at)}
                    </button>
                  </td>
                  <td>
                    <div className="jr-src">{SOURCE_LABEL[r.record_source] ?? r.record_source}</div>
                    {r.record_origin !== "FORWARD_PAPER" && (
                      <Badge text={r.record_origin.replace(/_/g, " ")} tone={originTone(r.record_origin)} />)}
                  </td>
                  <td>{r.strategy_name ?? dash}{r.strategy_version ? <span className="dim"> · {r.strategy_version}</span> : null}</td>
                  <td><b>{r.symbol ?? dash}</b></td>
                  <td>{r.timeframe ?? dash}</td>
                  <td>{r.side ? <Badge text={r.side.toUpperCase()} tone={r.side === "long" ? "green" : "red"} /> : dash}</td>
                  <td className="mono">{price(r.actual_entry ?? r.planned_entry)}</td>
                  <td className="mono">{price(r.planned_stop_loss)}</td>
                  <td className="mono">{price(r.planned_take_profit)}</td>
                  <td className="mono">{price(r.actual_exit)}</td>
                  <td className="mono">{r.risk_amount != null ? `$${num(r.risk_amount)}` : dash}</td>
                  <td className="mono">{r.planned_rr != null ? `${num(r.planned_rr)}` : dash}</td>
                  <td className={`mono ${(r.realized_r ?? 0) > 0 ? "pos" : (r.realized_r ?? 0) < 0 ? "neg" : ""}`}>{rMult(r.realized_r)}</td>
                  <td className={`mono ${(r.net_pnl ?? 0) > 0 ? "pos" : (r.net_pnl ?? 0) < 0 ? "neg" : ""}`}>{money(r.net_pnl)}</td>
                  <td>{r.outcome ? <Badge text={r.outcome} tone={outcomeTone(r.outcome)} /> : dash}</td>
                  <td className="mono">{duration(r.trade_duration_s)}</td>
                  <td>
                    <Badge text={r.status} tone={statusTone(r.status)} />
                    {r.reviewed && <span className="dim jr-reviewed" title="An agent review exists"> ✓</span>}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={17} className="dim ta-center" style={{ padding: 18 }}>
                  {data.error && !data.data ? "Backend not reachable." :
                    activeFilters ? "No trade records match these filters." :
                      "No trade records of this origin yet. Records appear as Trading Instances, the PA lab and the SMC lab execute paper trades."}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][];
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        <option value="">All</option>
        {options.map(([v, text]) => <option key={v} value={v}>{text}</option>)}
      </select>
    </label>
  );
}
