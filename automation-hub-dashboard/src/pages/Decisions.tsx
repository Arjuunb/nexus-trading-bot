import { Fragment, useEffect, useMemo, useState } from "react";
import { usePref } from "../lib/prefs";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { useLive, API_BASE } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { useApp } from "../app-context";

/** Decisions — the Explainable Trading feed. One complete Decision Report per
 *  analysis cycle (including WAIT candles): narrated market analysis, the
 *  rule-by-rule checklist, a five-category confidence score, the decision and
 *  a recommendation. The bot never trades or skips silently. */

interface CycleRow {
  id: number; ts: string; symbol: string; timeframe: string;
  price: number | null; decision: string; score: number | null;
}
interface Rule { name: string; status: string; explanation: string }
interface Report {
  ts: string; symbol: string; timeframe: string; price: number | null;
  decision: string; side: string | null; score: number;
  market_analysis: Record<string, unknown> & {
    available?: boolean; bias?: string;
    trend?: Record<string, unknown>; structure?: Record<string, unknown>;
    volume?: { label?: string }; volatility?: { label?: string };
    liquidity?: { sweep?: string }; last_candle?: string;
  };
  checklist: Rule[];
  scores: Record<string, number | string | boolean | null> & { total?: number; label?: string };
  reasons: string[]; recommendation: string;
}
interface V2EngineHealth {
  mode: "shadow";
  execution_enabled: false;
  total_decisions: number;
  sampled_decisions: number;
  engine_status_counts: Record<string, Record<string, number>>;
}

const DECISIONS = ["all", "BUY", "SELL", "WAIT", "SKIP"] as const;
const decTone = (d: string) =>
  d === "BUY" ? "green" : d === "SELL" ? "red" : d === "SKIP" ? "amber" : "default";
const ruleTone = (s: string) => (s === "PASS" ? "green" : s === "FAIL" ? "red" : "default");
const CATS: [string, string][] = [
  ["trend", "Trend"], ["structure", "Structure"], ["supply_demand", "Supply/Demand"],
  ["volume", "Volume"], ["risk", "Risk"],
];

function ScoreBars({ scores }: { scores: Report["scores"] }) {
  return (
    <div style={{ display: "grid", gap: 6, maxWidth: 380 }}>
      {CATS.map(([k, label]) => {
        const v = Number(scores[k] ?? 0);
        return (
          <div key={k} style={{ display: "grid", gridTemplateColumns: "110px 1fr 42px", gap: 8, alignItems: "center" }}>
            <span className="dim" style={{ fontSize: 12 }}>{label}</span>
            <div style={{ height: 6, borderRadius: 3, background: "#1b1b1f", overflow: "hidden" }}>
              <div style={{ width: `${(v / 20) * 100}%`, height: 6, borderRadius: 3,
                background: v >= 14 ? "var(--green)" : v >= 8 ? "var(--gold)" : "var(--red)" }} />
            </div>
            <span className="mono dim" style={{ fontSize: 12, textAlign: "right" }}>{v}/20</span>
          </div>
        );
      })}
    </div>
  );
}

function ReportDetail({ id }: { id: number }) {
  const { data } = useLive<{ report: Report }>(`/engine/cycles/${id}`, 0);
  const r = data?.report;
  if (!r) return <p className="dim" style={{ padding: 14 }}>Loading report…</p>;
  const ma = r.market_analysis || {};
  const trend = (ma.trend ?? {}) as Record<string, unknown>;
  const structure = (ma.structure ?? {}) as Record<string, unknown>;
  return (
    <div style={{ padding: "12px 14px", display: "grid", gap: 14 }}>
      {/* decision + reasons */}
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <Badge text={r.decision} tone={decTone(r.decision) as never} />
          <b>Confidence {r.score}/100</b>
          <span className="dim">({String(r.scores?.label ?? "")})</span>
          {r.scores?.engine_score != null && (
            <span className="dim mono" style={{ fontSize: 12 }}>brain gate {String(r.scores.engine_score)}/100</span>
          )}
        </div>
        <ul style={{ margin: "8px 0 0 18px" }}>
          {r.reasons.map((x, i) => <li key={i} style={{ fontSize: 13 }}>{x}</li>)}
        </ul>
        <p style={{ marginTop: 8, fontSize: 13 }}>
          <b>Recommendation:</b> <span className="dim">{r.recommendation}</span>
        </p>
      </div>

      {/* score breakdown */}
      <div>
        <p className="dim" style={{ margin: "0 0 6px", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>Confidence breakdown</p>
        <ScoreBars scores={r.scores} />
      </div>

      {/* checklist */}
      <div className="tablewrap">
        <p className="dim" style={{ margin: "0 0 6px", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>Strategy checklist</p>
        <table className="data-table">
          <tbody>
            {r.checklist.map((c) => (
              <tr key={c.name}>
                <td style={{ width: 70 }}><Badge text={c.status} tone={ruleTone(c.status) as never} /></td>
                <td style={{ whiteSpace: "normal" }}><b style={{ fontSize: 12.5 }}>{c.name}</b></td>
                <td className="dim" style={{ whiteSpace: "normal", fontSize: 12.5 }}>{c.explanation}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* market analysis */}
      {ma.available ? (
        <div style={{ display: "flex", gap: 18, flexWrap: "wrap", fontSize: 12.5 }}>
          <span><span className="dim">Bias </span><b>{String(ma.bias)}</b></span>
          <span><span className="dim">EMA8/33 </span><b>{String(trend.ema8_vs_ema33)}</b></span>
          <span><span className="dim">Swings </span><b>{String(trend.swing_highs)} · {String(trend.swing_lows)}</b></span>
          <span><span className="dim">Structure </span><b>{String(structure.state)}</b> <span className="dim">BOS {String(structure.break_of_structure)}</span></span>
          <span><span className="dim">Volume </span><b>{String(ma.volume?.label)}</b></span>
          <span><span className="dim">Volatility </span><b>{String(ma.volatility?.label)}</b></span>
          <span><span className="dim">Liquidity </span><b>{String(ma.liquidity?.sweep)}</b></span>
          <span><span className="dim">Last candle </span><b>{String(ma.last_candle)}</b></span>
        </div>
      ) : (
        <p className="dim" style={{ fontSize: 12.5 }}>Market analysis unavailable this cycle (insufficient history).</p>
      )}
    </div>
  );
}

export default function DecisionsPage({ focusId }: { focusId?: string } = {}) {
  const { toast } = useApp();
  const [decision, setDecision] = usePref<(typeof DECISIONS)[number]>("decisions.filter", "all");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<number | null>(focusId ? Number(focusId) : null);

  // deep link (#/decision/<id>): expand the linked cycle on arrival
  useEffect(() => { if (focusId) setOpen(Number(focusId)); }, [focusId]);

  const qs = new URLSearchParams({ limit: "150" });
  if (decision !== "all") qs.set("decision", decision);
  const cycles = useLive<{ cycles: CycleRow[]; total: number }>(`/engine/cycles?${qs}`, 5000);
  const v2Health = useLive<V2EngineHealth>("/api/v2/health/engines", 10000);
  const offline = cycles.error && !cycles.data;

  const rows = useMemo(() => {
    const all = cycles.data?.cycles ?? [];
    const q = query.trim().toUpperCase();
    return q ? all.filter((c) => c.symbol.includes(q)) : all;
  }, [cycles.data, query]);

  const stats = useMemo(() => {
    const all = cycles.data?.cycles ?? [];
    const by = (d: string) => all.filter((c) => c.decision === d).length;
    return { total: cycles.data?.total ?? 0, buys: by("BUY") + by("SELL"), waits: by("WAIT"), skips: by("SKIP") };
  }, [cycles.data]);

  return (
    <>
      <PageHeader
        title="Decision Archive"
        subtitle="Every analysis cycle explained — market read, checklist, confidence score and the decision. The bot never trades or skips silently."
      />
      {focusId && (
        <div className="card" style={{ borderColor: "var(--gold)", background: "rgba(234,181,79,0.06)", marginBottom: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <Icon name="external" size={13} className="amber" />
            <b>Linked decision · cycle #{focusId}</b>
            <button className="chip-btn" style={{ marginLeft: "auto" }}
              onClick={() => { void copyText(window.location.href, toast); }}>Copy link</button>
          </div>
          <ReportDetail id={Number(focusId)} />
        </div>
      )}
      {offline && (
        <div className="card" style={{ borderColor: "#ef4444", display: "flex", alignItems: "center", gap: 10 }}>
          <Icon name="warning" size={16} className="neg" />
          <span className="dim">Backend not reachable (<span className="mono">{API_BASE}</span>). Reports fill in as the engine processes candles.</span>
        </div>
      )}

      <Card title="Core Engine V2 Shadow Diagnostics" subtitle="Evidence is recorded alongside paper cycles for comparison only; V2 cannot execute trades.">
        {typeof v2Health.data?.total_decisions === "number" ? (
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <Badge text="SHADOW ONLY" tone="amber" />
            <span className="mono">{v2Health.data.total_decisions} observations</span>
            <span className="dim">{Object.keys(v2Health.data.engine_status_counts ?? {}).length} evidence engines observed</span>
            <span className="dim">Execution: disabled</span>
          </div>
        ) : (
          <span className="dim">No V2 shadow observations yet. Set <code>HUB_CORE_V2_MODE=shadow</code> to observe closed paper-engine bars.</span>
        )}
      </Card>

      <Card title="Audit & Compliance Pack" subtitle="config + decision archive + trade record + alerts, SHA-256 integrity-stamped">
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span className="dim" style={{ fontSize: 12.5, flex: 1, minWidth: 220 }}>
            A tamper-evident export of the bot's real state. The pack carries a SHA-256 hash of its
            contents — recompute it to prove nothing was altered after export. Paper-trading record.
          </span>
          <a className="btn btn-soft" href={`${API_BASE}/audit/export?fmt=html`} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> Printable report (PDF)</a>
          <a className="btn btn-primary" href={`${API_BASE}/audit/export?fmt=json`} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> Download JSON</a>
        </div>
      </Card>

      <div className="stat-row">
        <StatCard label="Cycles recorded" value={String(stats.total)} sub="every candle, incl. WAIT" />
        <StatCard label="Trades (in view)" value={String(stats.buys)} tone="green" />
        <StatCard label="Waits (in view)" value={String(stats.waits)} />
        <StatCard label="Skips (in view)" value={String(stats.skips)} />
      </div>

      <Card title="Decision Reports" subtitle="newest first · refreshes every 5s">
        <div className="toolbar">
          <div className="search">
            <Icon name="info" size={15} className="search-icon" />
            <input placeholder="Filter symbol…" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="chips">
            {DECISIONS.map((d) => (
              <button key={d} className={`chip-btn ${decision === d ? "active" : ""}`} type="button"
                      onClick={() => setDecision(d)}>{d}</button>
            ))}
          </div>
        </div>
        <div className="tablewrap">
          <table className="data-table">
            <thead>
              <tr><th></th><th>Time</th><th>Symbol</th><th>TF</th><th>Price</th><th>Decision</th><th>Score</th></tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const isOpen = open === c.id;
                return (
                  <Fragment key={c.id}>
                    <tr>
                      <td>
                        <button className="btn btn-ghost btn-sm" aria-expanded={isOpen}
                                onClick={() => setOpen(isOpen ? null : c.id)}>
                          <Icon name="chevron" size={12} className={isOpen ? "rot-180" : undefined} /> View
                        </button>
                      </td>
                      <td className="dim mono">{c.ts?.slice(5, 16).replace("T", " ")}</td>
                      <td><b>{c.symbol}</b></td>
                      <td className="dim">{c.timeframe}</td>
                      <td className="mono">{c.price ?? "—"}</td>
                      <td><Badge text={c.decision} tone={decTone(c.decision) as never} /></td>
                      <td className="mono">{c.score ?? "—"}/100</td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={7} style={{ background: "var(--surface-2, #121214)", padding: 0 }}>
                          <div style={{ display: "flex", justifyContent: "flex-end", padding: "6px 10px 0" }}>
                            <button className="chip-btn" title="Copy a shareable link to this decision"
                              onClick={() => { void copyText(`${location.origin}${location.pathname}#/decision/${c.id}`, toast); }}>
                              <Icon name="external" size={11} /> Copy link
                            </button>
                          </div>
                          <ReportDetail id={c.id} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {rows.length === 0 && (
                <tr><td colSpan={7} className="dim ta-center" style={{ padding: 18 }}>
                  No cycle reports yet — they appear as the engine processes candles.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}
