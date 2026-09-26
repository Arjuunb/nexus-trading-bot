import { Fragment, useEffect, useMemo, useState } from "react";
import { usePref } from "../lib/prefs";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import DecisionJournalPanel from "../components/journal/DecisionJournalPanel";
import { useLive, hhmmss, API_BASE } from "../lib/api";
import { useApp } from "../app-context";
import { copyText } from "../lib/clipboard";

/** Bot Trade Journal — a dedicated, searchable record of every decision the
 *  bot made. Each row is one journaled trade; expand it for the full
 *  9-section decision journal. Evolution memory (staged learning) sits below. */

type JournalTrade = {
  trade_id: string; created_at: string; closed_at: string | null;
  mode: string; execution_mode: string; market_data_mode: string | null;
  market_data_source: string | null; exchange: string | null;
  instance_id: string | null; instance_name: string | null;
  strategy_id: string | null; strategy_name: string; strategy_version: string | null;
  position_id: string | null; symbol: string; side: string; strategy: string; timeframe: string;
  entry: number; exit: number | null; pnl: number | null;
  planned_rr: number | null; actual_rr: number | null;
  result: string | null; grade: string | null; status: string;
};
type EvoSetup = {
  setup_key: string; strategy: string; regime: string; side: string;
  trades: number; wins: number; net_r: number; stage: string; note: string;
};

const money = (n: number | null | undefined) => `$${(n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
const stageTone = (s: string) =>
  s === "evidence" ? "green" : s === "building" ? "amber" : "blue";

const MODES = ["all", "paper", "live", "LEGACY / UNVERIFIED"] as const;
const RESULTS = ["all", "win", "loss"] as const;

export default function JournalPage({ focusId }: { focusId?: string } = {}) {
  const { go, viewInstance, toast } = useApp();
  const [mode, setMode] = usePref<(typeof MODES)[number]>("journal.mode", "all");
  const [result, setResult] = usePref<(typeof RESULTS)[number]>("journal.result", "all");
  const [query, setQuery] = useState("");
  const [instanceId, setInstanceId] = usePref<string>("journal.instance", "all");
  const [open, setOpen] = useState<string | null>(focusId ?? null);

  // deep link (#/trade/<id>): expand the linked trade on arrival
  useEffect(() => { if (focusId) setOpen(focusId); }, [focusId]);

  const qs = new URLSearchParams({ limit: "200" });
  if (mode !== "all") qs.set("mode", mode);
  if (result !== "all") qs.set("result", result);
  if (instanceId !== "all") qs.set("instance_id", instanceId);

  const trades = useLive<{ trades: JournalTrade[] }>(`/journal/trades?${qs.toString()}`, 4000);
  const instances = useLive<{ instances: Array<{ id: string; symbol: string; strategy_label: string; timeframe: string }> }>("/instances", 10000);
  const evolution = useLive<{ setups: EvoSetup[] }>("/journal/evolution", 8000);

  const offline = trades.error && !trades.data;
  const rows = useMemo(() => {
    const all = trades.data?.trades ?? [];
    const q = query.trim().toUpperCase();
    return q ? all.filter((t) => t.symbol.toUpperCase().includes(q) || t.strategy_name.toUpperCase().includes(q) || (t.instance_name ?? "").toUpperCase().includes(q)) : all;
  }, [trades.data, query]);

  const stats = useMemo(() => {
    const all = trades.data?.trades ?? [];
    const closed = all.filter((t) => t.result);
    const wins = closed.filter((t) => t.result === "win").length;
    const graded = all.filter((t) => t.grade);
    const gradeScore = { A: 4, B: 3, C: 2, D: 1, F: 0 } as Record<string, number>;
    const avg = graded.length
      ? graded.reduce((s, t) => s + (gradeScore[t.grade as string] ?? 0), 0) / graded.length
      : null;
    const letter = avg == null ? "—" : ["F", "D", "C", "B", "A"][Math.round(avg)] ?? "—";
    return { total: all.length, winRate: closed.length ? (wins / closed.length) * 100 : null, letter };
  }, [trades.data]);

  const evoStats = useMemo(() => {
    const s = evolution.data?.setups ?? [];
    return { total: s.length, evidence: s.filter((x) => x.stage === "evidence").length };
  }, [evolution.data]);

  return (
    <>
      <PageHeader
        title="Bot Trade Journal"
        subtitle="Every bot decision — explainable, reviewable, searchable. Real strategy data only."
        actions={<button className="btn btn-soft btn-sm" onClick={() => go("Decision Archive")}><Icon name="history" size={13} /> Decision Archive</button>}
      />

      {offline && (
        <div className="card" style={{ borderColor: "#ef4444", display: "flex", alignItems: "center", gap: 10 }}>
          <Icon name="warning" size={16} className="neg" />
          <span>
            <b>Backend not reachable.</b> Start it with{" "}
            <span className="mono">cd automation-hub &amp;&amp; uvicorn app:app</span>{" "}
            (expected at <span className="mono">{API_BASE}</span>). The journal fills as the engine opens and closes trades.
          </span>
        </div>
      )}

      <div className="stat-row">
        <StatCard label="Journaled Trades" value={String(stats.total)} sub="with full decision history" />
        <StatCard label="Win Rate" value={stats.winRate == null ? "—" : `${stats.winRate.toFixed(1)}%`} tone={(stats.winRate ?? 0) >= 50 ? "green" : "red"} />
        <StatCard label="Avg Grade" value={stats.letter} sub="post-trade review" />
        <StatCard label="Learned Setups" value={String(evoStats.total)} sub={`${evoStats.evidence} at evidence stage`} />
      </div>

      <Card title="Decision Journal" subtitle={`${rows.length} of ${stats.total} trades`}>
        <div className="toolbar">
          <div className="search">
            <Icon name="info" size={15} className="search-icon" />
            <input placeholder="Search symbol or strategy…" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="chips">
            <select aria-label="Instance filter" value={instanceId} onChange={(e) => setInstanceId(e.target.value)}>
              <option value="all">All instances</option>
              {(instances.data?.instances ?? []).map((instance) => (
                <option key={instance.id} value={instance.id}>{instance.symbol} · {instance.strategy_label} · {instance.timeframe}</option>
              ))}
            </select>
            {MODES.map((m) => (
              <button key={m} className={`chip-btn ${mode === m ? "active" : ""}`} onClick={() => setMode(m)} type="button">{m}</button>
            ))}
            <span className="dim" style={{ padding: "0 2px" }}>·</span>
            {RESULTS.map((r) => (
              <button key={r} className={`chip-btn ${result === r ? "active" : ""}`} onClick={() => setResult(r)} type="button">{r}</button>
            ))}
          </div>
        </div>
        <div className="tablewrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Instance</th><th>Strategy</th><th>Symbol</th><th>Timeframe</th>
                <th>Execution</th><th>Market Data</th><th>Exchange</th><th>Entry</th>
                <th>Exit</th><th>P&amp;L</th><th>Status</th><th>Opened</th><th>Closed</th><th>Journal</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const isOpen = open === t.trade_id;
                return (
                  <Fragment key={t.trade_id}>
                    <tr>
                      <td>{t.instance_id ? <button className="btn btn-ghost btn-sm" onClick={() => viewInstance(t.instance_id as string)}>{t.instance_name ?? t.instance_id.slice(0, 8)}</button> : <span className="dim">Legacy / unverified</span>}</td>
                      <td><b>{t.strategy_name}</b>{t.strategy_version ? <span className="dim"> · {t.strategy_version}</span> : null}</td>
                      <td><b>{t.symbol}</b></td>
                      <td>{t.timeframe || "—"}</td>
                      <td><Badge text={t.execution_mode.toUpperCase()} tone={t.execution_mode === "live" ? "red" : t.execution_mode === "paper" ? "blue" : "amber"} /></td>
                      <td><Badge text={t.market_data_mode ? `${t.market_data_mode.toUpperCase()} DATA` : "UNKNOWN"} tone="default" />{t.market_data_source ? <div className="dim" style={{ fontSize: 11 }}>{t.market_data_source}</div> : null}</td>
                      <td><Badge text={(t.exchange ?? "unknown").toUpperCase()} tone="default" /></td>
                      <td>{t.entry.toLocaleString()}</td>
                      <td>{t.exit != null ? t.exit.toLocaleString() : "—"}</td>
                      <td className={(t.pnl ?? 0) >= 0 ? "pos" : "neg"}>{t.pnl == null ? "—" : `${(t.pnl ?? 0) >= 0 ? "+" : ""}${money(t.pnl)}`}</td>
                      <td><Badge text={t.status} tone={t.status === "open" ? "blue" : t.result === "win" ? "green" : t.result === "loss" ? "red" : "default"} /></td>
                      <td className="dim mono">{hhmmss(t.created_at)}</td>
                      <td className="dim mono">{t.closed_at ? hhmmss(t.closed_at) : "—"}</td>
                      <td>
                        <button
                          className="btn btn-ghost btn-sm"
                          aria-expanded={isOpen}
                          onClick={() => setOpen(isOpen ? null : t.trade_id)}
                          type="button"
                        >
                          <Icon name="chevron" size={12} className={isOpen ? "rot-180" : undefined} />
                          {isOpen ? "Hide" : "View"}
                        </button>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={14} style={{ background: "var(--surface-2, #121214)", padding: 0 }}>
                          <div style={{ display: "flex", justifyContent: "flex-end", padding: "6px 10px 0" }}>
                            <button className="chip-btn" title="Copy a shareable link to this trade"
                              onClick={() => { void copyText(`${location.origin}${location.pathname}#/trade/${t.trade_id}`, toast); }}>
                              <Icon name="external" size={11} /> Copy link
                            </button>
                          </div>
                          <DecisionJournalPanel tradeId={t.trade_id} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {rows.length === 0 && (
                <tr><td colSpan={14} className="dim ta-center" style={{ padding: 18 }}>
                  {stats.total === 0 ? "No journaled trades yet — the journal fills as the engine opens and closes trades." : "No trades match the current filters."}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Evolution Memory" subtitle="what the bot has learned per setup — staged so no single trade changes strategy">
        <div className="tablewrap">
          <table className="data-table">
            <thead><tr><th>Setup</th><th>Regime</th><th>Side</th><th>Trades</th><th>Wins</th><th>Net R</th><th>Stage</th><th>Note</th></tr></thead>
            <tbody>
              {(evolution.data?.setups ?? []).map((s) => (
                <tr key={s.setup_key}>
                  <td><b>{s.strategy}</b></td>
                  <td className="dim">{s.regime}</td>
                  <td><Badge text={s.side} tone={s.side === "long" ? "green" : "red"} /></td>
                  <td>{s.trades}</td>
                  <td>{s.wins}</td>
                  <td className={s.net_r >= 0 ? "pos" : "neg"}>{s.net_r >= 0 ? "+" : ""}{s.net_r.toFixed(2)}R</td>
                  <td><Badge text={s.stage} tone={stageTone(s.stage) as any} /></td>
                  <td className="dim" style={{ fontSize: 12 }}>{s.note}</td>
                </tr>
              ))}
              {(evolution.data?.setups?.length ?? 0) === 0 && (
                <tr><td colSpan={8} className="dim ta-center" style={{ padding: 18 }}>
                  No learned setups yet. Insights under 30 trades are early signals; 50+ trades are needed for stronger changes.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}
