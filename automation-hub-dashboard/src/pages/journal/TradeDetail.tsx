import { useState, type ReactNode } from "react";
import Card from "../../components/common/Card";
import Icon from "../../components/common/Icon";
import { Badge } from "../../components/common/ui";
import DecisionJournalPanel from "../../components/journal/DecisionJournalPanel";
import { apiPostJson, useLive } from "../../lib/api";
import { copyText } from "../../lib/clipboard";
import { useApp } from "../../app-context";
import {
  dash, duration, type FullRecord, humanize, latency, money, num, originTone, outcomeTone, pct, price,
  rMult, SOURCE_LABEL, statusTone, whenFull,
} from "../../lib/journal";

/** One canonical trade record. The sections above "Agent review" are facts
 *  recorded from the execution layer; the review and notes below them are
 *  interpretation stored separately, and cannot change a fact. */
export default function TradeDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const { toast, viewInstance } = useApp();
  const rec = useLive<FullRecord>(`/journal/records/${encodeURIComponent(id)}`, 15000);
  const r = rec.data;

  if (!r) {
    return (
      <Card title="Trade record">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}><Icon name="chevron" size={12} className="rot-90" /> Back to trades</button>
        <p className="dim" style={{ marginTop: 12 }}>
          {rec.error ? `This trade record could not be loaded: ${rec.error}` : "Loading…"}
        </p>
      </Card>
    );
  }

  const setup = (r.setup ?? {}) as Record<string, unknown>;
  const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => (typeof x === "string" ? x : JSON.stringify(x))) : []);
  const passed = list(setup.conditions_passed ?? setup.checklist_passed);
  const failed = list(setup.conditions_failed ?? setup.missing_conditions);
  const required = list(setup.conditions_required);
  const legacy = r.record_origin === "LEGACY_MIGRATION" || r.record_source === "LEGACY_ENGINE";

  return (
    <>
      <div className="jr-detail-head">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}>
          <Icon name="chevron" size={12} className="rot-90" /> Back to trades
        </button>
        <div className="jr-detail-title">
          <h2>{r.symbol ?? dash} <span className="dim">{r.timeframe ?? ""}</span></h2>
          {r.side && <Badge text={r.side.toUpperCase()} tone={r.side === "long" ? "green" : "red"} />}
          <Badge text={r.status} tone={statusTone(r.status)} />
          {r.outcome && <Badge text={r.outcome} tone={outcomeTone(r.outcome)} />}
          <Badge text={r.record_origin.replace(/_/g, " ")} tone={originTone(r.record_origin)} />
          <Badge text={r.verification} tone={r.verification === "VERIFIED" ? "green" : "amber"} />
          <Badge text={`${r.data_completeness} DATA`} tone={r.data_completeness === "FULL" ? "default" : "amber"} />
        </div>
        <button type="button" className="chip-btn" title="Copy a shareable link to this record"
          onClick={() => { void copyText(`${location.origin}${location.pathname}#/trade/${r.journal_record_id}`, toast); }}>
          <Icon name="external" size={11} /> Copy link
        </button>
      </div>

      <div className="jr-sections">
        <Section title="Overview">
          <KV items={[
            ["Record", r.journal_record_id], ["Trade id", r.trade_id], ["Execution key", r.execution_key],
            ["Source", SOURCE_LABEL[r.record_source] ?? r.record_source], ["Origin", humanize(r.record_origin)],
            ["Instance", r.instance_id ? (
              <button type="button" className="btn btn-ghost btn-sm jr-link" onClick={() => viewInstance(r.instance_id as string)}>
                {r.instance_id.slice(0, 8)}</button>) : null],
            ["Lab", r.lab_id], ["Agent", r.agent_id],
            ["Strategy", r.strategy_name], ["Version", r.strategy_version], ["Exchange", r.exchange],
            ["Market", r.market_type], ["Mode", r.operating_mode],
          ]} />
          {r.missing && r.missing.length > 0 && (
            <p className="dim jr-note">Not recorded by the source, so left empty rather than guessed: {r.missing.map(humanize).join(", ")}.</p>
          )}
        </Section>

        <Section title="Setup" note="As the strategy saw it at decision time">
          <KV items={[
            ["Setup", r.setup_type], ["Regime", r.market_regime], ["HTF bias", r.htf_bias],
            ["Session", r.trading_session?.replace(/_/g, " ")], ["Structure", setup.market_structure as string],
            ["Reason", setup.strategy_reason as string],
          ]} />
          {(required.length > 0 || passed.length > 0 || failed.length > 0) && (
            <div className="jr-conditions">
              {required.length > 0 && <Conditions title="Required" items={required} tone="default" />}
              {passed.length > 0 && <Conditions title="Passed" items={passed} tone="green" />}
              {failed.length > 0 && <Conditions title="Failed / missing" items={failed} tone="red" />}
            </div>
          )}
          {!r.setup && <p className="dim">The source recorded no setup snapshot for this trade.</p>}
        </Section>

        <Section title="Strategy evidence" note="Frozen at decision time; never recomputed from later prices">
          {r.evidence ? <Json value={r.evidence} /> : <p className="dim">No strategy evidence was recorded.</p>}
        </Section>

        <Section title="Trade plan">
          <KV items={[
            ["Signal price", price(r.signal_price)], ["Planned entry", price(r.planned_entry)],
            ["Stop-loss", price(r.planned_stop_loss)], ["Take-profit", price(r.planned_take_profit)],
            ["Planned R:R", r.planned_rr != null ? num(r.planned_rr) : dash],
            ["Risk", r.risk_percent != null ? pct(r.risk_percent, 2) : dash],
            ["Risk amount", r.risk_amount != null ? `$${num(r.risk_amount)}` : dash],
            ["Quantity", num(r.quantity, 8)], ["Leverage", r.leverage != null ? `${num(r.leverage)}×` : dash],
            ["Equity before", r.equity_before != null ? `$${num(r.equity_before)}` : dash],
            ["Available before", r.available_balance_before != null ? `$${num(r.available_balance_before)}` : dash],
          ]} />
        </Section>

        <Section title="Execution" note="Planned entry and actual fill are recorded separately">
          <KV items={[
            ["Requested entry", price(r.requested_entry)], ["Actual fill", price(r.actual_entry)],
            ["Requested qty", num(r.requested_quantity, 8)], ["Filled qty", num(r.filled_quantity, 8)],
            ["Bid", price(r.bid)], ["Ask", price(r.ask)], ["Spread", price(r.spread)],
            ["Slippage", price(r.slippage)], ["Order type", r.order_type], ["Fill model", r.fill_model],
            ["Order id", r.order_id], ["Position id", r.position_id], ["Status", r.execution_status],
            ["Decision latency", latency(r.decision_latency_ms)], ["Execution latency", latency(r.execution_latency_ms)],
          ]} />
        </Section>

        <Section title="Risk">
          {r.risk_check ? <Json value={r.risk_check} /> : <p className="dim">No risk-check receipt was recorded.</p>}
        </Section>

        <Section title="Result">
          {r.status === "CLOSED" ? (
            <KV items={[
              ["Exit", price(r.actual_exit)], ["Exit reason", r.exit_reason],
              ["Gross P&L", money(r.gross_pnl)], ["Fees", money(r.fees != null ? -Math.abs(r.fees) : null)],
              ["Funding", r.funding != null ? money(r.funding) : dash], ["Net P&L", money(r.net_pnl)],
              ["Realized R", rMult(r.realized_r)], ["Achieved R:R", rMult(r.achieved_rr)],
              ["MAE", rMult(r.mae_r)], ["MFE", rMult(r.mfe_r)],
              ["Max trade drawdown", r.max_trade_drawdown != null ? `$${num(r.max_trade_drawdown)}` : dash],
              ["Duration", duration(r.trade_duration_s)], ["Outcome", r.outcome],
            ]} />
          ) : (
            <p className="dim">{r.status === "OPEN" ? "The position is still open." :
              r.status === "PENDING" ? "The order is waiting for its fill." :
                `No result: ${humanize(r.status)}${r.exit_reason ? ` — ${r.exit_reason}` : ""}.`}</p>
          )}
        </Section>

        <Section title="Timeline">
          <ol className="jr-timeline">
            {r.timeline.map((s) => (
              <li key={s.stage} className={`jr-stage jr-stage-${s.status.toLowerCase()}`}>
                <span className="jr-dot" aria-hidden="true" />
                <span className="jr-stage-name">{humanize(s.stage)}</span>
                <span className="mono dim">{s.at ? whenFull(s.at) : humanize(s.status)}</span>
              </li>
            ))}
          </ol>
        </Section>

        <Section title="Agent review" note="Interpretation — stored apart from the facts above">
          {r.reviews.length === 0 ? <p className="dim">Not reviewed yet. Reviews run after a trade is finalized.</p> :
            r.reviews.map((rv) => (
              <div key={rv.trade_review_id} className="jr-review">
                <div className="jr-review-head">
                  <b>{rv.agent_id}</b><span className="dim">v{rv.review_version} · {whenFull(rv.reviewed_at)}</span>
                </div>
                <KV items={[["Setup quality", rv.setup_quality], ["Execution quality", rv.execution_quality],
                  ["Risk compliance", rv.risk_compliance], ["Strategy compliance", rv.strategy_compliance]]} />
                <ReviewList title="Rule violations" items={(rv.rule_violations ?? []).map((v) => `${v.rule}: ${v.detail}`)} tone="red" />
                <ReviewList title="Mistakes" items={rv.mistakes ?? []} tone="red" />
                <ReviewList title="Positive behaviours" items={rv.positive_behaviours ?? []} tone="green" />
                <ReviewList title="Observations" items={rv.observations ?? []} tone="default" />
                <ReviewList title="Recommendations" items={rv.recommendations ?? []} tone="default" />
              </div>
            ))}
        </Section>

        <Notes record={r} onAdded={() => void rec.refetch()} />

        {r.corrections.length > 0 && (
          <Section title="Corrections log" note="Every change to a finalized record, with its reason">
            <Json value={r.corrections} />
          </Section>
        )}
      </div>

      {legacy && r.trade_id && (
        <Card title="Legacy decision journal" subtitle="The old journal's entry for this trade, shown as it was recorded">
          <DecisionJournalPanel tradeId={r.trade_id} />
        </Card>
      )}
    </>
  );
}

function Section({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="card jr-section">
      <header className="jr-section-head"><h3>{title}</h3>{note && <span className="dim">{note}</span>}</header>
      {children}
    </section>
  );
}

function KV({ items }: { items: [string, ReactNode | string | null | undefined][] }) {
  return (
    <dl className="cal-kv jr-kv">
      {items.map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{value === null || value === undefined || value === "" ? dash : value}</dd></div>
      ))}
    </dl>
  );
}

function Conditions({ title, items, tone }: { title: string; items: string[]; tone: "green" | "red" | "default" }) {
  return (
    <div className="jr-cond">
      <span className="dim">{title}</span>
      <div className="jr-cond-list">{items.map((c, i) => <Badge key={`${c}-${i}`} text={c} tone={tone} />)}</div>
    </div>
  );
}

function ReviewList({ title, items, tone }: { title: string; items: string[]; tone: "green" | "red" | "default" }) {
  if (!items.length) return null;
  return (
    <div className="jr-review-list">
      <span className={`jr-review-label ${tone === "green" ? "pos" : tone === "red" ? "neg" : "dim"}`}>{title}</span>
      <ul>{items.map((t, i) => <li key={i}>{t}</li>)}</ul>
    </div>
  );
}

function Json({ value }: { value: unknown }) {
  return <pre className="jr-json">{JSON.stringify(value, null, 2)}</pre>;
}

function Notes({ record, onAdded }: { record: FullRecord; onAdded: () => void }) {
  const { toast } = useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await apiPostJson(`/journal/records/${encodeURIComponent(record.journal_record_id)}/notes`, { text });
      setText("");
      onAdded();
      toast?.("Note added");
    } catch (e) {
      toast?.(`Could not save the note: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title="Notes" note="Your notes sit beside the record and never edit it">
      {record.notes.length === 0 ? <p className="dim">No notes yet.</p> : (
        <ul className="jr-notes">
          {record.notes.map((n) => (
            <li key={n.note_id}><span className="dim">{whenFull(n.created_at)} · {n.author}</span><p>{n.text}</p></li>
          ))}
        </ul>
      )}
      <div className="jr-note-form">
        <textarea value={text} maxLength={4000} rows={3} placeholder="What did you see in this trade?"
          aria-label="New note" onChange={(e) => setText(e.target.value)} />
        <button type="button" className="btn btn-soft btn-sm" disabled={busy || !text.trim()} onClick={() => void save()}>
          Add note
        </button>
      </div>
    </Section>
  );
}
