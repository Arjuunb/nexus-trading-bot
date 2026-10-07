import { useEffect, useState } from "react";
import Card from "../../components/common/Card";
import Icon from "../../components/common/Icon";
import { Badge } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import { usePref } from "../../lib/prefs";
import { dash, type DecisionRow, humanize, ORIGINS, SOURCE_LABEL, when, whenFull } from "../../lib/journal";
import CandleArchive from "../Decisions";

/** Journal > Decisions: material decisions only -- a signal, and what became
 *  of it. Candles where nothing happened are not stored here; the per-candle
 *  archive keeps those and is one click away. */

const TYPE_TONE: Record<string, "green" | "red" | "amber" | "blue" | "default"> = {
  TRADE_OPENED: "green", SIGNAL_GENERATED: "blue", WAITING_CONFIRMATION: "blue",
  RISK_BLOCKED: "red", QUALITY_BLOCKED: "amber", HTF_BLOCKED: "amber", CONTEXT_BLOCKED: "amber",
  NEWS_BLACKOUT: "amber", STALE_DATA: "red", FEED_UNAVAILABLE: "red", SIGNALS_ONLY: "blue",
  APPROVAL_REQUIRED: "blue", SESSION_BLOCKED: "amber", ORDER_REJECTED: "red",
  EXECUTION_FAILED: "red", EXECUTION_UNCERTAIN: "amber", DUPLICATE_PREVENTED: "default",
  SETUP_REJECTED: "amber",
};

export default function JournalDecisions({ focusId }: { focusId?: string }) {
  // A numeric focus id is a candle-cycle deep link from before this page existed.
  const cycleLink = !!focusId && /^\d+$/.test(focusId);
  const [view, setView] = usePref<"material" | "candles">("journal.decisions.view", "material");
  const shown = cycleLink ? "candles" : view;
  const [type, setType] = useState("");
  const [traded, setTraded] = useState("");
  const [origin, setOrigin] = usePref<string>("journal.decisions.origin", "FORWARD_PAPER");
  const [open, setOpen] = useState<string | null>(!cycleLink && focusId ? focusId : null);
  useEffect(() => { if (focusId && !/^\d+$/.test(focusId)) setOpen(focusId); }, [focusId]);

  const qs = new URLSearchParams({ limit: "200", origin });
  if (type) qs.set("decision_type", type);
  if (traded) qs.set("traded", traded);
  const data = useLive<{ decisions: DecisionRow[]; total: number; by_type: Record<string, number> }>(
    shown === "material" ? `/journal/decision-records?${qs.toString()}` : null, 10000);

  return (
    <>
      <div className="chips jr-origins" role="group" aria-label="Decision view">
        <button type="button" className={`chip-btn ${shown === "material" ? "active" : ""}`}
          onClick={() => setView("material")}>Material decisions</button>
        <button type="button" className={`chip-btn ${shown === "candles" ? "active" : ""}`}
          onClick={() => setView("candles")}>Candle archive</button>
      </div>
      {shown === "candles" ? <CandleArchive focusId={focusId} /> : open ? (
        <DecisionDetail id={open} onBack={() => setOpen(null)} />
      ) : (
        <Card title="Decisions" subtitle={`${data.data?.total ?? 0} material decision(s)${
          (data.data?.decisions.length ?? 0) < (data.data?.total ?? 0) ? ` · showing the newest ${data.data?.decisions.length}` : ""
        } · ${ORIGINS.find(([id]) => id === origin)?.[1] ?? origin}`}>
          <div className="chips jr-origins" role="group" aria-label="Decision origin">
            {ORIGINS.map(([id, label]) => (
              <button key={id} type="button" className={`chip-btn ${origin === id ? "active" : ""}`}
                onClick={() => setOrigin(id)}>{label}</button>
            ))}
          </div>
          <div className="chips jr-origins" role="group" aria-label="Decision type">
            <button type="button" className={`chip-btn ${type === "" ? "active" : ""}`} onClick={() => setType("")}>All</button>
            {Object.entries(data.data?.by_type ?? {}).sort((a, b) => b[1] - a[1]).map(([t, n]) => (
              <button key={t} type="button" className={`chip-btn ${type === t ? "active" : ""}`} onClick={() => setType(t)}>
                {humanize(t)} · {n}
              </button>
            ))}
            <span className="dim" style={{ padding: "0 4px" }}>·</span>
            {[["", "Any"], ["no", "No trade"], ["yes", "Became a trade"]].map(([v, label]) => (
              <button key={v} type="button" className={`chip-btn ${traded === v ? "active" : ""}`} onClick={() => setTraded(v)}>{label}</button>
            ))}
          </div>
          <div className="tablewrap">
            <table className="data-table jr-table">
              <thead>
                <tr>
                  <th>Time</th><th>Source</th><th>Strategy</th><th>Symbol</th><th>TF</th><th>Signal</th>
                  <th>Decision</th><th>Blocker</th><th>Passed</th><th>Missing</th><th>Market data</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(data.data?.decisions ?? []).map((d) => (
                  <tr key={d.decision_record_id} className="jr-row" onClick={() => setOpen(d.decision_record_id)}>
                    <td className="mono dim">
                      <button type="button" className="jr-open" aria-label={`Open ${d.symbol} decision`}
                        onClick={(e) => { e.stopPropagation(); setOpen(d.decision_record_id); }}>
                        {when(d.decided_at)}
                      </button>
                    </td>
                    <td>{SOURCE_LABEL[d.record_source] ?? d.record_source}</td>
                    <td>{d.strategy_name ?? dash}</td>
                    <td><b>{d.symbol ?? dash}</b></td>
                    <td>{d.timeframe ?? dash}</td>
                    <td>{d.signal ?? dash}</td>
                    <td><Badge text={humanize(d.decision_type)} tone={TYPE_TONE[d.decision_type] ?? "default"} /></td>
                    <td className="jr-clip" title={d.blocker ?? ""}>{d.blocker ?? dash}</td>
                    <td className="mono">{d.conditions_passed ? d.conditions_passed.length : dash}</td>
                    <td className="mono">{d.conditions_missing ? d.conditions_missing.length : dash}</td>
                    <td>{d.market_data_state ?? dash}</td>
                    <td>{d.journal_record_id ? <Badge text="TRADE" tone="green" /> : <span className="dim">{d.status ?? dash}</span>}</td>
                  </tr>
                ))}
                {(data.data?.decisions ?? []).length === 0 && (
                  <tr><td colSpan={12} className="dim ta-center" style={{ padding: 18 }}>
                    {data.error && !data.data ? "Backend not reachable." :
                      "No material decisions recorded yet. A decision is recorded when a strategy produces a signal, a gate blocks it, or data is unavailable for a run of candles."}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

function DecisionDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const d = useLive<DecisionRow>(`/journal/decision-records/${encodeURIComponent(id)}`, 15000);
  const row = d.data;
  const items = (v: unknown[] | null) => (v ?? []).map((x) => (typeof x === "string" ? x : JSON.stringify(x)));
  return (
    <Card title="Decision" subtitle={row ? `${row.symbol ?? ""} ${row.timeframe ?? ""} · ${whenFull(row.decided_at)}` : undefined}>
      <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}><Icon name="chevron" size={12} className="rot-90" /> Back to decisions</button>
      {!row ? <p className="dim">{d.error ? `Could not load this decision: ${d.error}` : "Loading…"}</p> : (
        <div className="jr-sections">
          <section className="card jr-section">
            <header className="jr-section-head"><h3>{row.journal_record_id ? "It became a trade" : "Why no order was placed"}</h3></header>
            <p className="jr-why">
              <Badge text={humanize(row.decision_type)} tone={TYPE_TONE[row.decision_type] ?? "default"} />{" "}
              {row.reason || row.blocker || "The source recorded no reason."}
            </p>
            <dl className="cal-kv jr-kv">
              {([["Source", SOURCE_LABEL[row.record_source] ?? row.record_source], ["Strategy", row.strategy_name],
                ["Signal", row.signal], ["Status", row.status], ["Blocker", row.blocker],
                ["Market data", row.market_data_state], ["Candle", whenFull(row.candle_time)],
                ["Instance", row.instance_id?.slice(0, 8)], ["Agent", row.agent_id]] as [string, string | null | undefined][])
                .map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v || dash}</dd></div>)}
            </dl>
          </section>
          <section className="card jr-section">
            <header className="jr-section-head"><h3>Conditions</h3></header>
            <div className="jr-conditions">
              <div className="jr-cond"><span className="dim">Passed</span>
                <div className="jr-cond-list">{items(row.conditions_passed).map((c, i) => <Badge key={i} text={c} tone="green" />)}
                  {!row.conditions_passed?.length && <span className="dim">{dash}</span>}</div></div>
              <div className="jr-cond"><span className="dim">Missing / failed</span>
                <div className="jr-cond-list">{items(row.conditions_missing).map((c, i) => <Badge key={i} text={c} tone="red" />)}
                  {!row.conditions_missing?.length && <span className="dim">{dash}</span>}</div></div>
            </div>
          </section>
          <section className="card jr-section">
            <header className="jr-section-head"><h3>Evidence</h3><span className="dim">As recorded at decision time</span></header>
            {row.evidence ? <pre className="jr-json">{JSON.stringify(row.evidence, null, 2)}</pre> : <p className="dim">None recorded.</p>}
          </section>
          {row.journal_record_id && (
            <p><a className="btn btn-soft btn-sm" href={`#/trade/${row.journal_record_id}`}>Open the trade record</a></p>
          )}
        </div>
      )}
    </Card>
  );
}
