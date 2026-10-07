import { useEffect, useMemo, useState } from "react";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import JournalFiltersBar, { ModeChips, useJournalFilters, useJournalMeta } from "../components/journal/JournalFilters";
import TradeDetail from "../components/journal/TradeDetail";
import { API_BASE, apiDownload, apiPost, useLive } from "../lib/api";
import { usePref } from "../lib/prefs";
import { useApp } from "../app-context";
import {
  MODE_LABELS, RESULT_TONE, dash, filterQuery, fmtDuration, fmtLev, fmtMoney, fmtNum, fmtPct, fmtPF,
  fmtPrice, fmtR, fmtRR, label, tone, type JournalTrade, type Scope, type Summary,
} from "../lib/journal";

/** Trade Journal — the single, structured record of every executed trade
 *  across Trading Instances, the Price Action and SMC labs, the legacy engine
 *  and backtests (kept separate by trading mode). Every figure on this page is
 *  computed by the backend from the canonical journal. */

const PAGE = 100;

function cell(t: JournalTrade, key: string) {
  switch (key) {
    case "entry_filled_at": return <span className="mono dim">{t.entry_at_display ?? dash}</span>;
    case "trade_ref": return <span className="mono">{t.trade_ref}</span>;
    case "strategy_name": return <><b>{t.strategy_name ?? dash}</b>{t.strategy_version ? <span className="dim"> v{t.strategy_version}</span> : null}</>;
    case "instance": return <span>{t.instance ?? dash}</span>;
    case "symbol": return <b>{t.symbol}</b>;
    case "direction": return <Badge text={t.direction} tone={t.direction === "LONG" ? "green" : "red"} />;
    case "timeframe": return t.timeframe ?? dash;
    case "entry_session": return t.session_label ?? dash;
    case "entry_price": return fmtPrice(t.entry_price);
    case "exit_price": return fmtPrice(t.exit_price);
    case "quantity": return fmtNum(t.quantity, 6);
    case "leverage": return fmtLev(t.leverage);
    case "risk_pct": return fmtPct(t.risk_pct, 2);
    case "planned_rr": return fmtRR(t.planned_rr);
    case "realised_r": return <span className={tone(t.realised_r)}>{fmtR(t.realised_r)}</span>;
    case "net_pnl": return <span className={tone(t.net_pnl)}>{fmtMoney(t.net_pnl)}</span>;
    case "result": return t.result ? <Badge text={label(t.result)} tone={RESULT_TONE[t.result] ?? "default"} />
      : <Badge text={label(t.status)} tone="blue" />;
    case "duration_s": return t.duration_display ?? fmtDuration(t.duration_s);
    case "trading_mode": return <Badge text={MODE_LABELS[t.trading_mode] ?? t.trading_mode} tone={t.trading_mode === "BACKTEST" ? "purple" : "blue"} />;
    case "exit_reason": return label(t.exit_reason) || dash;
    case "notional_value": return fmtMoney(t.notional_value, false);
    case "margin_used": return fmtMoney(t.margin_used, false);
    case "risk_amount": return fmtMoney(t.risk_amount, false);
    case "initial_stop": return fmtPrice(t.initial_stop);
    case "initial_target": return fmtPrice(t.initial_target);
    case "gross_pnl": return <span className={tone(t.gross_pnl)}>{fmtMoney(t.gross_pnl)}</span>;
    case "fees_total": return fmtMoney(t.fees_total, false);
    case "mfe_r": return fmtR(t.mfe_r);
    case "mae_r": return fmtR(t.mae_r);
    case "rule_violation": return t.rule_violation === null ? dash : t.rule_violation ? <Badge text="Violation" tone="red" /> : <Badge text="OK" tone="green" />;
    case "status": return label(t.status);
    case "exchange": return t.exchange ?? dash;
    default: return dash;
  }
}

function best(ref: Summary["best_strategy"]) {
  if (!ref) return { value: dash, sub: "no closed trades" };
  return { value: ref.label, sub: `${fmtMoney(ref.net_pnl)} · ${ref.trades} trades · ${fmtR(ref.avg_r)}` };
}

export default function TradeJournal({ focusId }: { focusId?: string } = {}) {
  const { go } = useApp();
  const [filters, setFilters] = useJournalFilters();
  const meta = useJournalMeta();
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(focusId ?? null);
  const [showColumns, setShowColumns] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const defaults = useMemo(() => (meta.data?.columns ?? []).filter((c) => c.default).map((c) => c.key), [meta.data]);
  const [columns, setColumns] = usePref<string[]>("journal.v2.columns", []);
  const visible = columns.length ? columns : defaults;

  useEffect(() => { if (focusId) setOpen(focusId); }, [focusId]);
  useEffect(() => { setOffset(0); }, [JSON.stringify(filters)]);

  const query = filterQuery(filters);
  const trades = useLive<{ trades: JournalTrade[]; total: number } & Scope>(
    `/journal/v2/trades?${filterQuery(filters, { limit: String(PAGE), offset: String(offset) })}`, 5000);
  const summary = useLive<Summary>(`/journal/v2/summary?${query}`, 5000);
  const s = summary.data;
  const offline = trades.error && !trades.data;
  const scope = trades.data;
  const catalogue = meta.data?.columns ?? [];

  const toggleColumn = (key: string) => {
    const next = visible.includes(key) ? visible.filter((k) => k !== key) : [...visible, key];
    setColumns(catalogue.map((c) => c.key).filter((k) => next.includes(k)));
  };

  const sync = async () => {
    setSyncing(true);
    try { await apiPost("/journal/v2/sync"); await trades.refetch(); await summary.refetch(); }
    finally { setSyncing(false); }
  };

  const bs = best(s?.best_strategy ?? null), ws = best(s?.worst_strategy ?? null);
  const bsess = best(s?.best_session ?? null), bsym = best(s?.best_symbol ?? null);

  return (
    <>
      <PageHeader
        title="Trade Journal"
        subtitle="Every executed trade from instances, labs and bots — recorded automatically, structured, and audit-grade."
        actions={<>
          <button className="btn btn-soft btn-sm" type="button" onClick={() => go("Decision Archive")}><Icon name="history" size={13} /> Decisions</button>
          <button className="btn btn-soft btn-sm" type="button" disabled={syncing} onClick={sync}><Icon name="refresh" size={13} /> {syncing ? "Syncing…" : "Sync now"}</button>
          <button className="btn btn-soft btn-sm" type="button" onClick={() => apiDownload(`/journal/v2/trades.csv?${query}`, "trade-journal.csv")}><Icon name="external" size={13} /> Export CSV</button>
        </>}
      />

      {offline && (
        <div className="card tj-alert">
          <Icon name="warning" size={16} className="neg" />
          <span><b>Backend not reachable.</b> Start it with <span className="mono">cd automation-hub &amp;&amp; uvicorn app:app</span> (expected at <span className="mono">{API_BASE}</span>).</span>
        </div>
      )}

      <Card className="tj-filter-card">
        <ModeChips filters={filters} setFilters={setFilters} meta={meta.data} applied={scope?.modes_applied} />
        {scope?.mixed_modes && (
          <div className="tj-mixed"><Icon name="warning" size={13} /> {scope.mode_warning} Showing: {scope.modes_applied.map((m) => MODE_LABELS[m] ?? m).join(" + ")}.</div>
        )}
        <JournalFiltersBar filters={filters} setFilters={setFilters} meta={meta.data} />
      </Card>

      <div className="stat-row tj-cards">
        <StatCard label="Net P&L" value={fmtMoney(s?.net_pnl)} tone={(s?.net_pnl ?? 0) > 0 ? "green" : (s?.net_pnl ?? 0) < 0 ? "red" : "default"} sub={s ? `fees ${fmtMoney(s.total_fees, false)}` : undefined} />
        <StatCard label="Total Trades" value={String(s?.total_trades ?? 0)} sub={s ? `${s.open_trades} open · ${s.operational_events} operational` : undefined} />
        <StatCard label="Win Rate" value={fmtPct(s?.win_rate)} tone={(s?.win_rate ?? 0) >= 50 ? "green" : s?.win_rate == null ? "default" : "red"} sub={s?.sample_warning ? "early sample (< 30 trades)" : undefined} />
        <StatCard label="Profit Factor" value={fmtPF(s?.profit_factor)} tone={typeof s?.profit_factor === "number" && s.profit_factor >= 1 ? "green" : "default"} />
        <StatCard label="Average R" value={fmtR(s?.avg_r)} tone={(s?.avg_r ?? 0) > 0 ? "green" : (s?.avg_r ?? 0) < 0 ? "red" : "default"} />
        <StatCard label="Max Drawdown" value={fmtMoney(s?.max_drawdown)} tone={(s?.max_drawdown ?? 0) < 0 ? "red" : "default"} />
        <StatCard label="Best Strategy" value={bs.value} sub={bs.sub} />
        <StatCard label="Worst Strategy" value={ws.value} sub={ws.sub} />
        <StatCard label="Best Session" value={bsess.value} sub={bsess.sub} />
        <StatCard label="Best Symbol" value={bsym.value} sub={bsym.sub} />
      </div>

      <Card title="Trades" subtitle={scope ? `${scope.total} trade(s) · ${scope.modes_applied.map((m) => MODE_LABELS[m] ?? m).join(" + ")}` : undefined}>
        <div className="tj-table-tools">
          <div className="tj-colpicker">
            <button className="btn btn-ghost btn-sm" type="button" aria-expanded={showColumns} onClick={() => setShowColumns(!showColumns)}>
              <Icon name="grid" size={13} /> Columns
            </button>
            {showColumns && (
              <div className="tj-colmenu" role="menu">
                {catalogue.map((c) => (
                  <label key={c.key}><input type="checkbox" checked={visible.includes(c.key)} onChange={() => toggleColumn(c.key)} /> {c.label}</label>
                ))}
                <button className="btn btn-ghost btn-sm" type="button" onClick={() => setColumns([])}>Reset to default</button>
              </div>
            )}
          </div>
          <span className="dim">Click a trade to open its full record.</span>
        </div>
        <div className="tablewrap">
          <table className="data-table tj-table">
            <thead>
              <tr>{visible.map((k) => <th key={k}>{catalogue.find((c) => c.key === k)?.label ?? k}</th>)}</tr>
            </thead>
            <tbody>
              {(scope?.trades ?? []).map((t) => (
                <tr key={t.trade_id} className={`tj-row ${t.is_operational ? "tj-operational" : ""}`}
                  onClick={() => setOpen(t.trade_ref)}>
                  {visible.map((k, i) => (
                    <td key={k}>{i === 0 ? (
                      <button type="button" className="tj-open" aria-label={`Open trade ${t.trade_ref}`}
                        onClick={(e) => { e.stopPropagation(); setOpen(t.trade_ref); }}>{cell(t, k)}</button>
                    ) : cell(t, k)}</td>
                  ))}
                </tr>
              ))}
              {scope && scope.trades.length === 0 && (
                <tr><td colSpan={visible.length || 1} className="dim ta-center" style={{ padding: 18 }}>
                  {scope.total === 0 ? "No trades for these filters and trading mode yet — bot trades are journaled automatically as they execute." : "No trades on this page."}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        {scope && scope.total > PAGE && (
          <div className="row-actions tj-pager">
            <button className="btn btn-ghost btn-sm" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
            <span className="dim">{offset + 1}–{Math.min(offset + PAGE, scope.total)} of {scope.total}</span>
            <button className="btn btn-ghost btn-sm" type="button" disabled={offset + PAGE >= scope.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
          </div>
        )}
      </Card>

      {open && <TradeDetail tradeRef={open} onClose={() => setOpen(null)} />}
    </>
  );
}
