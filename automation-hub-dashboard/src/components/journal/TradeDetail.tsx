import { useCallback, useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import Icon from "../common/Icon";
import { Badge } from "../common/ui";
import DecisionJournalPanel from "./DecisionJournalPanel";
import { apiGet, apiPost, apiPostJson } from "../../lib/api";
import {
  MODE_LABELS, RESULT_TONE, dash, fmtDuration, fmtLev, fmtMoney, fmtNum, fmtPct, fmtPF, fmtPrice, fmtR,
  fmtRR, fmtTime, isNum, label, tone, type JournalTrade, type GroupRow,
} from "../../lib/journal";

type Condition = { name: string; detail?: string | null; status?: string; required?: boolean };
type Snapshot = {
  captured_at: string; decision: string | null; decision_reason: string | null;
  conditions_passed: Condition[] | null; conditions_failed: Condition[] | null; conditions_missing: Condition[] | null;
  confidence: number | null; setup_score: number | null; market_bias: string | null; htf_bias: string | null;
  risk_decision: string | null; feed_health: string | null; candle_freshness: string | null;
  htf_freshness: string | null; strategy_state: string | null; execution_state: string | null;
  strategy_family: string | null; setup: Record<string, any> | null; market_context: Record<string, any> | null;
  risk: Record<string, any> | null; raw: Record<string, any> | null; source: string;
};
type Review = { id: string; reviewer: string; review_version: string; created_at: string; setup_quality: string | null;
  execution_quality: string | null; risk_management: string | null; outcome: string | null; grade: string | null;
  summary: string | null; mistakes: string[]; went_well: string[]; went_wrong: string[]; improvement: string | null;
  rule_violations: string[] };
export type TradeDetailPayload = {
  trade: JournalTrade & { provenance: Record<string, any> | string | null };
  display: Record<string, string | null>;
  facts: { q: string; a: string | number | null }[];
  snapshot: Snapshot | null;
  executions: { execution_id: string; kind: string; side: string; quantity: number | null; requested_price: number | null;
    price: number | null; fee: number | null; slippage: number | null; slippage_cost: number | null;
    realized_gross_pnl: number | null; liquidity: string | null; executed_at: string | null }[];
  fees: { items: { fee_type: string; amount: number; rate: number | null; basis: number | null; source_ref: string }[];
    totals: Record<string, number> };
  modifications: { field: string; old_value: number | null; new_value: number | null; reason: string; actor: string;
    detail: string | null; modified_at: string }[];
  timeline: { ts: string; kind: string; detail: string | null; actor: string | null }[];
  reviews: Review[];
  notes: { id: string; note: string; author: string; created_at: string }[];
  corrections: { seq: number; field: string; previous_value: unknown; new_value: unknown; reason: string; actor: string;
    corrected_at: string }[];
  links: { link_type: string; ref: string; created_at: string }[];
  missing_fields: { field: string; label: string }[];
  strategy_context: { mode: string; strategy: string | null; metrics: Record<string, any>; sessions: GroupRow[];
    symbols: GroupRow[]; trend: { status: string; detail: string } };
  legacy_decision_journal: { trade_id: string } | null;
};

const SECTIONS = ["Trade Summary", "Execution", "Risk", "Strategy Setup", "Decision Snapshot", "Market Context",
  "Exit & Result", "Fees", "Performance", "Timeline", "Agent Review", "Raw Audit Data"] as const;
type Section = (typeof SECTIONS)[number];

function KV({ k, v, cls }: { k: string; v: ReactNode; cls?: string }) {
  const empty = v === null || v === undefined || v === "" || v === dash;
  return (
    <div className="tj-kv">
      <span className="dim">{k}</span>
      <b className={empty ? "dim" : cls}>{empty ? dash : v}</b>
    </div>
  );
}

function Grid({ children }: { children: ReactNode }) {
  return <div className="tj-kv-grid">{children}</div>;
}

function statusTone(s?: string | null) {
  const v = (s ?? "").toUpperCase();
  if (["PASSED", "PASS", "RESPECTED", "STRONG"].includes(v)) return "green";
  if (["FAILED", "FAIL", "VIOLATED", "POOR", "NO_STOP"].includes(v)) return "red";
  if (["WEAK", "ACCEPTABLE", "MISSING", "NEUTRAL", "UNKNOWN"].includes(v)) return "amber";
  return "default";
}

function Conditions({ title, rows, kind }: { title: string; rows: Condition[] | null | undefined; kind: "ok" | "bad" | "na" }) {
  if (!rows || !rows.length) return null;
  return (
    <div className="tj-cond-block">
      <div className="tj-subhead">{title}</div>
      <ul className="tj-cond">
        {rows.map((c, i) => (
          <li key={i} className={kind}>
            <span className="tj-cond-mark">{kind === "ok" ? "✓" : kind === "bad" ? "✗" : "○"}</span>
            <span>{c.name}{c.required ? <span className="dim"> · required</span> : null}{c.detail ? <span className="dim"> — {c.detail}</span> : null}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="tj-json">{JSON.stringify(value, null, 2)}</pre>;
}

export default function TradeDetail({ tradeRef, onClose }: { tradeRef: string; onClose: () => void }) {
  const [data, setData] = useState<TradeDetailPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [section, setSection] = useState<Section>("Trade Summary");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiGet<TradeDetailPayload>(`/journal/v2/trades/${encodeURIComponent(tradeRef)}`)
      .then((d) => { setData(d); setError(null); })
      .catch((e: Error) => setError(e.message));
  }, [tradeRef]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const t = data?.trade;
  const snap = data?.snapshot;

  const runReview = async () => {
    setBusy(true);
    try { await apiPost(`/journal/v2/trades/${encodeURIComponent(tradeRef)}/reviews`); load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const addNote = async () => {
    if (!note.trim()) return;
    setBusy(true);
    try { await apiPostJson(`/journal/v2/trades/${encodeURIComponent(tradeRef)}/notes`, { note }); setNote(""); load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  const body = (() => {
    if (error && !data) return <div className="tj-empty"><Icon name="warning" size={14} /> {error}</div>;
    if (!data || !t) return <div className="tj-empty dim">Loading trade…</div>;
    switch (section) {
      case "Trade Summary":
        return (
          <>
            <div className="tj-facts">
              {data.facts.map((f) => (
                <div key={f.q} className="tj-fact"><span className="dim">{f.q}</span><b>{f.a === null || f.a === "" ? dash : String(f.a)}</b></div>
              ))}
            </div>
            {data.missing_fields.length > 0 && (
              <div className="tj-missing">
                <Icon name="info" size={13} /> Not recorded for this trade (left empty, never estimated):{" "}
                {data.missing_fields.map((m) => m.label).join(", ")}.
              </div>
            )}
          </>
        );
      case "Execution":
        return (
          <>
            <Grid>
              <KV k="Trade ID" v={t.trade_ref} /><KV k="Order ID" v={t.order_id} />
              <KV k="Execution ID" v={t.execution_id} /><KV k="Position ID" v={t.position_id} />
              <KV k="Exchange / venue" v={t.exchange} /><KV k="Market type" v={label(t.market_type)} />
              <KV k="Signal" v={fmtTime(t.signal_at)} /><KV k="Order created" v={fmtTime(t.order_created_at)} />
              <KV k="Filled" v={fmtTime(t.entry_filled_at)} /><KV k="Requested entry" v={fmtPrice(t.requested_entry_price)} />
              <KV k="Actual fill" v={fmtPrice(t.entry_price)} />
              <KV k="Slippage" v={isNum(t.entry_slippage) ? `${fmtPrice(t.entry_slippage)} (${fmtMoney(t.entry_slippage_cost, false)})` : null} />
              <KV k="Quantity" v={fmtNum(t.quantity, 8)} />
              <KV k="Position value" v={fmtMoney(t.notional_value, false)} />
              <KV k="Leverage" v={isNum(t.leverage) ? `${fmtLev(t.leverage)}${t.leverage_source ? ` · ${label(t.leverage_source)}` : ""}` : null} />
              <KV k="Margin used" v={fmtMoney(t.margin_used, false)} />
              <KV k="Balance before" v={fmtMoney(t.account_balance_before, false)} />
              <KV k="Equity before" v={fmtMoney(t.account_equity_before, false)} />
              <KV k="Available margin before" v={fmtMoney(t.available_margin_before, false)} />
            </Grid>
            <div className="tj-subhead">Fills</div>
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Time</th><th>Kind</th><th>Side</th><th>Qty</th><th>Requested</th><th>Price</th><th>Slippage</th><th>Gross P&amp;L</th><th>Fee</th></tr></thead>
                <tbody>
                  {data.executions.map((e) => (
                    <tr key={e.execution_id}>
                      <td className="mono dim">{fmtTime(e.executed_at)}</td><td>{label(e.kind)}</td><td>{e.side}</td>
                      <td>{fmtNum(e.quantity, 8)}</td><td>{fmtPrice(e.requested_price)}</td><td>{fmtPrice(e.price)}</td>
                      <td>{fmtPrice(e.slippage)}</td><td className={tone(e.realized_gross_pnl)}>{fmtMoney(e.realized_gross_pnl)}</td>
                      <td>{fmtMoney(e.fee, false)}</td>
                    </tr>
                  ))}
                  {data.executions.length === 0 && <tr><td colSpan={9} className="dim">No fills recorded (order did not execute).</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        );
      case "Risk":
        return (
          <>
            <Grid>
              <KV k="Stop loss (original)" v={fmtPrice(t.initial_stop)} /><KV k="Take profit (original)" v={fmtPrice(t.initial_target)} />
              <KV k="Stop loss (current)" v={fmtPrice(t.current_stop)} /><KV k="Take profit (current)" v={fmtPrice(t.current_target)} />
              <KV k="Entry → stop" v={fmtPrice(t.stop_distance)} /><KV k="Entry → target" v={fmtPrice(t.target_distance)} />
              <KV k="Risk amount" v={fmtMoney(t.risk_amount, false)} /><KV k="Risk %" v={fmtPct(t.risk_pct, 2)} />
              <KV k="Planned reward" v={fmtMoney(t.planned_reward, false)} /><KV k="Planned RR" v={fmtRR(t.planned_rr)} />
              <KV k="Realised R" v={fmtR(t.realised_r)} cls={tone(t.realised_r)} />
              <KV k="Max allowed risk" v={isNum(t.max_allowed_risk_pct) ? fmtPct(t.max_allowed_risk_pct, 2) : null} />
              <KV k="Risk rule" v={t.risk_rule_status ? <Badge text={t.risk_rule_status} tone={statusTone(t.risk_rule_status) as any} /> : null} />
            </Grid>
            <div className="tj-subhead">Changes after entry (original values above are preserved)</div>
            {data.modifications.length ? (
              <ul className="tj-list">
                {data.modifications.map((m, i) => (
                  <li key={i}><span className="mono dim">{fmtTime(m.modified_at)}</span> <b>{label(m.field)}</b>{" "}
                    {fmtPrice(m.old_value)} → {fmtPrice(m.new_value)} <Badge text={label(m.reason)} /> <span className="dim">by {m.actor}{m.detail ? ` · ${m.detail}` : ""}</span></li>
                ))}
              </ul>
            ) : <div className="dim tj-pad">No stop, target or size changes after entry.</div>}
            {snap?.risk?.gates?.length ? (
              <>
                <div className="tj-subhead">Risk gates at entry</div>
                <ul className="tj-cond">
                  {(snap.risk.gates as { rule: string; status: string; detail: string }[]).map((g, i) => (
                    <li key={i} className={g.status === "PASSED" ? "ok" : "bad"}>
                      <span className="tj-cond-mark">{g.status === "PASSED" ? "✓" : "✗"}</span>
                      <span>{label(g.rule)}<span className="dim"> — {g.detail}</span></span>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </>
        );
      case "Strategy Setup": {
        const setup = snap?.setup ?? null;
        if (!setup) return <div className="dim tj-pad">The strategy setup was not captured for this trade (it is not reconstructed from later market data).</div>;
        const entries = Object.entries(setup).filter(([k]) => k !== "family" && k !== "conditions");
        return (
          <>
            <div className="tj-subhead">{label(setup.family)} setup — as the strategy evaluated it at entry</div>
            <div className="tj-setup">
              {entries.map(([k, v]) => {
                const row = (v ?? {}) as { status?: string; detail?: string };
                return (
                  <div key={k} className="tj-setup-row">
                    <span>{label(k)}</span>
                    <Badge text={label(row.status ?? "unknown")} tone={statusTone(row.status) as any} />
                    <span className="dim">{row.detail ?? ""}</span>
                  </div>
                );
              })}
              {Array.isArray(setup.conditions) && <Conditions title="Conditions" rows={setup.conditions} kind="ok" />}
            </div>
          </>
        );
      }
      case "Decision Snapshot":
        if (!snap) return <div className="dim tj-pad">No decision snapshot was captured for this trade. It is never reconstructed afterwards.</div>;
        return (
          <>
            <div className="tj-decision">
              <Badge text={snap.decision ?? "UNKNOWN"} tone={snap.decision?.includes("LONG") ? "green" : "red"} />
              <span>{snap.decision_reason ?? dash}</span>
            </div>
            <Grid>
              <KV k="Captured" v={fmtTime(snap.captured_at)} /><KV k="Source" v={label(snap.source)} />
              <KV k="Confidence" v={fmtNum(snap.confidence, 3)} /><KV k="Setup score" v={fmtNum(snap.setup_score, 1)} />
              <KV k="Market bias" v={snap.market_bias} /><KV k="HTF bias" v={snap.htf_bias} />
              <KV k="Risk decision" v={snap.risk_decision} /><KV k="Feed health" v={snap.feed_health} />
              <KV k="Candle freshness" v={snap.candle_freshness} /><KV k="HTF freshness" v={snap.htf_freshness} />
              <KV k="Strategy state" v={snap.strategy_state} /><KV k="Execution state" v={snap.execution_state} />
            </Grid>
            <Conditions title="Conditions passed" rows={snap.conditions_passed} kind="ok" />
            <Conditions title="Conditions failed" rows={snap.conditions_failed} kind="bad" />
            <Conditions title="Not evaluated / missing" rows={snap.conditions_missing} kind="na" />
            <div className="dim tj-pad"><Icon name="lock" size={12} /> Frozen at entry — this snapshot cannot be edited.</div>
          </>
        );
      case "Market Context":
        return (
          <>
            <Grid>
              <KV k="Symbol" v={`${t.symbol} (${t.base_asset ?? dash}/${t.quote_asset ?? dash})`} />
              <KV k="Timeframe" v={t.timeframe} /><KV k="HTF timeframe" v={t.htf_timeframe} />
              <KV k="HTF bias" v={t.htf_bias} /><KV k="Market regime" v={t.market_regime} />
              <KV k="Session" v={data.display.session} /><KV k="Day" v={t.entry_weekday} />
              <KV k="Entry (London)" v={data.display.entry} /><KV k="Outside preferred session" v={t.in_preferred_session === null ? null : t.in_preferred_session ? "No" : "Yes"} />
            </Grid>
            {snap?.market_context ? <JsonBlock value={snap.market_context} /> : <div className="dim tj-pad">No market snapshot captured.</div>}
          </>
        );
      case "Exit & Result":
        return (
          <Grid>
            <KV k="Result" v={t.result ? <Badge text={label(t.result)} tone={RESULT_TONE[t.result] ?? "default"} /> : t.status} />
            <KV k="Exit reason" v={label(t.exit_reason)} /><KV k="Reason source" v={label(t.exit_reason_source)} />
            <KV k="Exit time" v={fmtTime(t.exit_at)} /><KV k="Exit price (avg)" v={fmtPrice(t.exit_price)} />
            <KV k="Quantity closed" v={fmtNum(t.closed_quantity, 8)} /><KV k="Partial exits" v={String(t.partial_exit_count ?? 0)} />
            <KV k="Gross P&L" v={fmtMoney(t.gross_pnl)} cls={tone(t.gross_pnl)} />
            <KV k="Fees" v={fmtMoney(isNum(t.fees_total) ? -t.fees_total : null)} />
            <KV k="Funding" v={isNum(t.funding_total) ? fmtMoney(-t.funding_total) : "not modelled"} />
            <KV k="Net P&L" v={fmtMoney(t.net_pnl)} cls={tone(t.net_pnl)} />
            <KV k="P&L % of account" v={fmtPct(t.pnl_pct, 2)} /><KV k="Return on margin" v={fmtPct(t.return_on_margin_pct, 2)} />
            <KV k="Gross R" v={fmtR(t.gross_r)} /><KV k="Realised R" v={fmtR(t.realised_r)} cls={tone(t.realised_r)} />
            <KV k="Duration" v={fmtDuration(t.duration_s)} />
            <KV k="MFE" v={isNum(t.mfe_r) ? `${fmtR(t.mfe_r)} · ${fmtMoney(t.mfe_amount)} @ ${fmtPrice(t.mfe_price)}` : null} />
            <KV k="MAE" v={isNum(t.mae_r) ? `${fmtR(t.mae_r)} · ${fmtMoney(t.mae_amount)} @ ${fmtPrice(t.mae_price)}` : null} />
            <KV k="Excursion source" v={label(t.excursion_source)} />
            <KV k="Rule check" v={t.rule_violation === null ? null : t.rule_violation ? <Badge text={`${t.rule_violation_count} violation(s)`} tone="red" /> : <Badge text="Rules followed" tone="green" />} />
            {t.result_reason && <KV k="Operational reason" v={t.result_reason} />}
          </Grid>
        );
      case "Fees":
        return (
          <>
            <Grid>
              {Object.entries(data.fees.totals).map(([k, v]) => <KV key={k} k={label(k)} v={fmtMoney(v, false)} />)}
              <KV k="Total commission" v={fmtMoney(t.fees_total, false)} />
              <KV k="Funding" v={isNum(t.funding_total) ? fmtMoney(t.funding_total, false) : "not modelled by this engine"} />
              <KV k="Slippage cost" v={fmtMoney(t.slippage_cost_total, false)} />
            </Grid>
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Type</th><th>Amount</th><th>Rate</th><th>Basis</th><th>Execution</th></tr></thead>
                <tbody>
                  {data.fees.items.map((f, i) => (
                    <tr key={i}><td>{label(f.fee_type)}</td><td>{fmtMoney(f.amount, false)}</td>
                      <td>{isNum(f.rate) ? `${(f.rate * 100).toFixed(3)}%` : dash}</td><td>{fmtMoney(f.basis, false)}</td>
                      <td className="mono dim">{f.source_ref.slice(0, 22)}</td></tr>
                  ))}
                  {data.fees.items.length === 0 && <tr><td colSpan={5} className="dim">No fees recorded.</td></tr>}
                </tbody>
              </table>
            </div>
          </>
        );
      case "Performance": {
        const ctx = data.strategy_context;
        const m = ctx.metrics;
        return (
          <>
            <div className="tj-subhead">{ctx.strategy} · {MODE_LABELS[ctx.mode] ?? ctx.mode} — overall</div>
            <Grid>
              <KV k="Trades" v={String(m.total_trades)} /><KV k="Win rate" v={fmtPct(m.win_rate)} />
              <KV k="Net P&L" v={fmtMoney(m.net_pnl)} cls={tone(m.net_pnl)} /><KV k="Profit factor" v={fmtPF(m.profit_factor)} />
              <KV k="Average R" v={fmtR(m.avg_r)} cls={tone(m.avg_r)} /><KV k="Max drawdown" v={fmtMoney(m.max_drawdown)} />
              <KV k="Trend" v={<Badge text={label(ctx.trend.status)} tone={ctx.trend.status === "IMPROVING" ? "green" : ctx.trend.status === "DETERIORATING" ? "red" : "default"} />} />
              {m.sample_warning && <KV k="Sample" v={<Badge text="early sample" tone="amber" />} />}
            </Grid>
            <div className="tj-two">
              <div>
                <div className="tj-subhead">By session</div>
                <table className="data-table"><tbody>
                  {ctx.sessions.map((r) => <tr key={r.key}><td>{r.label}</td><td>{r.total_trades}</td><td>{fmtPct(r.win_rate)}</td><td className={tone(r.net_pnl)}>{fmtMoney(r.net_pnl)}</td></tr>)}
                </tbody></table>
              </div>
              <div>
                <div className="tj-subhead">By symbol</div>
                <table className="data-table"><tbody>
                  {ctx.symbols.map((r) => <tr key={r.key}><td>{r.label}</td><td>{r.total_trades}</td><td>{fmtPct(r.win_rate)}</td><td className={tone(r.net_pnl)}>{fmtMoney(r.net_pnl)}</td></tr>)}
                </tbody></table>
              </div>
            </div>
          </>
        );
      }
      case "Timeline":
        return (
          <ol className="tj-timeline">
            {data.timeline.map((e, i) => (
              <li key={i}>
                <span className="mono dim">{fmtTime(e.ts)}</span>
                <b>{label(e.kind)}</b>
                <span className="dim">{e.detail}</span>
                {e.actor && <span className="tj-actor">{e.actor}</span>}
              </li>
            ))}
          </ol>
        );
      case "Agent Review":
        return (
          <>
            <div className="row-actions" style={{ justifyContent: "flex-start" }}>
              <button className="btn btn-soft btn-sm" disabled={busy || !t.finalised_at || t.is_operational} onClick={runReview} type="button">
                <Icon name="robot" size={13} /> Run review agent
              </button>
              <span className="dim" style={{ fontSize: 11 }}>Reviews are stored separately; they cannot change trade data.</span>
            </div>
            {data.reviews.length === 0 && <div className="dim tj-pad">No review yet.</div>}
            {[...data.reviews].reverse().map((r) => (
              <div key={r.id} className="tj-review">
                <div className="tj-review-head">
                  <b>{r.reviewer}</b> <span className="dim">v{r.review_version} · {fmtTime(r.created_at)}</span>
                  {r.grade && <Badge text={`Grade ${r.grade}`} tone={r.grade <= "B" ? "green" : r.grade === "C" ? "amber" : "red"} />}
                </div>
                {r.summary && <p>{r.summary}</p>}
                <Grid>
                  <KV k="Setup quality" v={r.setup_quality} /><KV k="Execution quality" v={r.execution_quality} />
                  <KV k="Risk management" v={r.risk_management} /><KV k="Outcome" v={r.outcome} />
                </Grid>
                {[["What went well", r.went_well], ["What went wrong", r.went_wrong], ["Mistakes", r.mistakes],
                  ["Rule violations", r.rule_violations]].map(([title, items]) =>
                  (items as string[]).length ? (
                    <div key={title as string}><div className="tj-subhead">{title as string}</div>
                      <ul className="tj-list">{(items as string[]).map((x, i) => <li key={i}>{x}</li>)}</ul></div>
                  ) : null)}
                {r.improvement && <KV k="Improvement" v={r.improvement} />}
              </div>
            ))}
            <div className="tj-subhead">Notes (commentary only)</div>
            <ul className="tj-list">{data.notes.map((n) => <li key={n.id}><span className="mono dim">{fmtTime(n.created_at)}</span> {n.note} <span className="dim">— {n.author}</span></li>)}</ul>
            <div className="tj-note">
              <input placeholder="Add a note (e.g. entered early, FOMO)…" value={note} onChange={(e) => setNote(e.target.value)} />
              <button className="btn btn-soft btn-sm" type="button" disabled={busy || !note.trim()} onClick={addNote}>Add note</button>
            </div>
          </>
        );
      case "Raw Audit Data":
        return (
          <>
            <Grid>
              <KV k="Canonical ID" v={t.trade_id} /><KV k="Source system" v={label(t.source_system)} />
              <KV k="Trade source" v={label(t.trade_source)} /><KV k="Mode" v={MODE_LABELS[t.trading_mode] ?? t.trading_mode} />
              <KV k="Data completeness" v={label(t.data_completeness)} /><KV k="Finalised" v={fmtTime(t.finalised_at)} />
            </Grid>
            <div className="tj-subhead">Source links</div>
            <ul className="tj-list mono">{data.links.map((l, i) => <li key={i}>{l.link_type} · {l.ref}</li>)}</ul>
            <div className="tj-subhead">Correction audit trail</div>
            {data.corrections.length ? (
              <table className="data-table"><thead><tr><th>#</th><th>Field</th><th>Previous</th><th>New</th><th>Reason</th><th>Actor</th><th>When</th></tr></thead>
                <tbody>{data.corrections.map((c, i) => (
                  <tr key={i}><td>{c.seq}</td><td>{c.field}</td><td className="mono">{JSON.stringify(c.previous_value)}</td>
                    <td className="mono">{JSON.stringify(c.new_value)}</td><td>{c.reason}</td><td>{c.actor}</td><td className="mono dim">{fmtTime(c.corrected_at)}</td></tr>))}
                </tbody></table>
            ) : <div className="dim tj-pad">No corrections — the recorded facts are unchanged since entry/close.</div>}
            <div className="tj-subhead">Provenance</div>
            <JsonBlock value={t.provenance} />
            {data.legacy_decision_journal && (
              <>
                <div className="tj-subhead">Legacy decision journal</div>
                <DecisionJournalPanel tradeId={data.legacy_decision_journal.trade_id} />
              </>
            )}
          </>
        );
    }
  })();

  return createPortal(
    <div className="tj-drawer-overlay" onClick={onClose}>
      <aside className="tj-drawer" role="dialog" aria-modal="true" aria-label={`Trade ${tradeRef}`} onClick={(e) => e.stopPropagation()}>
        <header className="tj-drawer-head">
          <div>
            <div className="tj-drawer-title">
              {t ? <><b>{t.trade_ref}</b> · {t.symbol} <Badge text={t.direction} tone={t.direction === "LONG" ? "green" : "red"} />
                {t.result ? <Badge text={label(t.result)} tone={RESULT_TONE[t.result] ?? "default"} /> : <Badge text={label(t.status)} tone="blue" />}
                <Badge text={MODE_LABELS[t.trading_mode] ?? t.trading_mode} tone={t.trading_mode === "LIVE" ? "red" : t.trading_mode === "BACKTEST" ? "purple" : "blue"} /></> : tradeRef}
            </div>
            {t && <div className="dim tj-drawer-sub">{t.strategy_name}{t.strategy_version ? ` v${t.strategy_version}` : ""} · {t.instance ?? dash} · {t.timeframe ?? dash}
              {isNum(t.net_pnl) && <> · <span className={tone(t.net_pnl)}>{fmtMoney(t.net_pnl)}</span> ({fmtR(t.realised_r)})</>}</div>}
          </div>
          <div className="row-actions">
            <button className="chip-btn" type="button" title="Copy a shareable link to this trade"
              onClick={() => navigator.clipboard?.writeText(`${location.origin}${location.pathname}#/trade/${t?.trade_ref ?? tradeRef}`)}>
              <Icon name="external" size={11} /> Copy link
            </button>
            <button className="icon-btn" onClick={onClose} aria-label="Close trade detail" type="button"><Icon name="close" size={18} /></button>
          </div>
        </header>
        <nav className="tj-sections" aria-label="Trade sections">
          {SECTIONS.map((s) => (
            <button key={s} type="button" className={`tj-section-btn ${section === s ? "active" : ""}`}
              aria-pressed={section === s} onClick={() => setSection(s)}>{s}</button>
          ))}
        </nav>
        {error && data && <div className="tj-missing"><Icon name="warning" size={13} /> {error}</div>}
        <div className="tj-drawer-body">{body}</div>
      </aside>
    </div>,
    document.body,
  );
}
