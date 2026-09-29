import { useMemo, useState } from "react";
import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import {
  type AlmostTrade, ago, clock, CONDITION_TONE, FINAL_TONE, type GEvent, type StrategiesView,
  type StrategyCard, type Trace,
} from "./common";

const WINDOWS = [1, 7, 30];
const n = (v: number | null | undefined, digits = 2) => (v == null ? "—" : v.toFixed(digits));

function market(c: { symbol: string | null; timeframe: string | null }): string {
  return [c.symbol, c.timeframe].filter(Boolean).join(" ") || "—";
}

/** One evaluation, condition by condition (PRD §8): what passed, what
 *  stopped it, and what was never reached. */
function TraceView({ eventId, onClose }: { eventId: string; onClose: () => void }) {
  const live = useLive<GEvent>(`/guardian/events/${encodeURIComponent(eventId)}`, 60000);
  const e = live.data;
  const trace = (e?.evidence ?? null) as Trace | null;
  return (
    <Card title="Decision trace" subtitle={e ? `${e.source_component} · ${market(e)} · ${clock(trace?.candle_time ?? e.timestamp)}` : "Loading…"}
      right={<button type="button" className="btn btn-soft btn-sm" onClick={onClose}>Close</button>}>
      {live.error && <p className="neg">Could not load the trace: {live.error}</p>}
      {trace && (
        <div data-testid="guardian-trace">
          <div className="gd-trace-head">
            <Badge text={trace.final} tone={FINAL_TONE[trace.final] ?? "default"} />
            {trace.direction && <span className="mono">{trace.direction}</span>}
            {trace.blocker_code && <span className="mono dim">{trace.blocker_code}</span>}
            {trace.data === "replay" && <Badge text="REPLAY DATA" tone="purple" />}
          </div>
          {trace.reason && <p className="gd-reason">{trace.reason}</p>}
          <table className="data-table gd-trace">
            <thead><tr><th>Condition</th><th>Stage</th><th>Result</th><th>Detail</th></tr></thead>
            <tbody>
              {trace.conditions.map((c, i) => (
                <tr key={`${c.id}-${i}`} className={c.kind === "strategy" ? "gd-trace-own" : ""}>
                  <td>{c.label ?? c.id}</td>
                  <td className="mono dim">{c.stage}</td>
                  <td><Badge text={c.state.replace(/_/g, " ")} tone={CONDITION_TONE[c.state] ?? "default"} />
                    {c.code && <span className="mono dim"> {c.code}</span>}</td>
                  <td className="gd-reason dim">{c.detail ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {trace.quality && (
            <p className="dim gd-quality">
              Decision Brain score {trace.quality.score ?? "—"} (minimum {trace.quality.min_score ?? "—"})
              {trace.quality.hard_blocks.length ? ` · hard blocks: ${trace.quality.hard_blocks.join("; ")}` : ""}
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

function StrategyTile({ card, selected, onSelect }: { card: StrategyCard; selected: boolean; onSelect: () => void }) {
  return (
    <button type="button" className={`gd-strategy${selected ? " gd-strategy-on" : ""}`} onClick={onSelect}
      data-testid={`guardian-strategy-${card.scope}`}>
      <div className="gd-strategy-head">
        <b>{card.strategy_id ?? "unnamed strategy"}</b>
        <span className="mono dim">{market(card)}</span>
      </div>
      <span className="mono dim">{card.scope}{card.strategy_version ? ` · ${card.strategy_version}` : ""}</span>
      <div className="gd-strategy-counts">
        <span><b className="mono">{card.evaluations}</b> evaluations</span>
        <span><b className="mono">{card.setups}</b> setups</span>
        <span><b className="mono">{card.entries}</b> entries</span>
        <span><b className="mono">{card.refused}</b> refused</span>
        <span><b className="mono">{card.almost_trades}</b> almost-trades</span>
      </div>
      <span className="dim">Last evaluation {ago(card.last_evaluation_at)}</span>
      {card.top_rejection_reasons.length > 0 && (
        <ul className="gd-reasons">
          {card.top_rejection_reasons.map((r) => (
            <li key={`${r.decision}-${r.code}`}><span className="mono">{r.code}</span>
              <span className="dim">{r.decision === "NO_SETUP" ? "no setup" : "refused"}</span><b className="mono">{r.count}</b></li>
          ))}
        </ul>
      )}
      {card.performance.length ? card.performance.map((p) => (
        <div key={`${p.record_source}-${p.record_origin}`} className="gd-perf">
          <span className="dim">{p.record_origin.replace(/_/g, " ").toLowerCase()} · closed trades</span>
          <span className="mono">{p.trades} ({p.wins}W / {p.losses}L) · net {n(p.net_pnl)} · avg R {n(p.average_r)}
            · PF {n(p.profit_factor)} · max DD {n(p.max_drawdown_r)}R</span>
        </div>
      )) : <span className="dim">No closed trades in the journal for this scope.</span>}
    </button>
  );
}

/** What each strategy attempted and why it did or did not trade. Observation
 *  only: nothing on this page changes a strategy, a rule or a risk limit. */
export default function Strategies() {
  const [days, setDays] = useState(7);
  const [scope, setScope] = useState<string>("");
  const [traceId, setTraceId] = useState<string | null>(null);
  const view = useLive<StrategiesView>(`/guardian/strategies?days=${days}`, 15000);
  const almost = useLive<{ almost_trades: AlmostTrade[] }>(
    `/guardian/almost-trades?limit=50${scope ? `&component=${encodeURIComponent(scope)}` : ""}`, 15000);
  const evalQuery = useMemo(() => {
    const p = new URLSearchParams({ category: "strategy", limit: "50" });
    if (scope) p.set("component", scope);
    return p.toString();
  }, [scope]);
  const evaluations = useLive<{ events: GEvent[] }>(`/guardian/events?${evalQuery}`, 10000);
  const data = view.data;
  const failing = Object.entries(data?.telemetry ?? {}).filter(([, r]) => !r.ok);

  return (
    <>
      <Card title="Strategies" subtitle="Every evaluation, why it did or did not trade, and near-valid setups — observation only"
        right={
          <div className="gd-window" role="group" aria-label="Window">
            {WINDOWS.map((d) => (
              <button key={d} type="button" className={`btn btn-sm ${d === days ? "btn-primary" : "btn-soft"}`}
                onClick={() => setDays(d)}>{d === 1 ? "Today" : `${d} days`}</button>
            ))}
          </div>
        }>
        {view.error && !data && <p className="neg">Strategy telemetry is unavailable: {view.error}</p>}
        {failing.map(([lab, r]) => <p key={lab} className="neg">Cannot read the {lab} lab's decisions: {r.error}</p>)}
        {data?.performance_error && <p className="neg">Closed-trade results unavailable: {data.performance_error}</p>}
        {!data ? (!view.error && <p className="dim">Loading…</p>) : data.strategies.length === 0 ? (
          <p className="dim">No strategy evaluations recorded in this window. Instances publish a trace for every closed candle while they run on live data; the SMC and Price Action labs are read from their own decision records.</p>
        ) : (
          <div className="gd-strategies" data-testid="guardian-strategies">
            {data.strategies.map((card) => (
              <StrategyTile key={`${card.scope}-${card.strategy_id}-${card.symbol}-${card.timeframe}`} card={card}
                selected={scope === card.scope} onSelect={() => setScope(scope === card.scope ? "" : card.scope)} />
            ))}
          </div>
        )}
        {data && <p className="dim gd-research">{data.research.note}</p>}
      </Card>

      {traceId && <TraceView eventId={traceId} onClose={() => setTraceId(null)} />}

      <div className="grid-2-eq">
        <Card title="Almost-trades" subtitle="Near-valid setups: every condition but one passed, or a complete setup one gate refused">
          <p className="gd-warning" data-testid="guardian-almost-note">
            <Badge text="NOT A RULE VERDICT" tone="amber" /> A missed-opportunity candidate does not mean the rule was wrong. It is for research only; production rules are never changed from it.
          </p>
          {almost.data?.almost_trades.length ? (
            <ul className="gd-list gd-almost" data-testid="guardian-almost-trades">
              {almost.data.almost_trades.map((a) => (
                <li key={a.identity}>
                  <Badge text={`${a.passed}/${a.evaluated}`} tone="amber" />
                  <span className="mono dim">{clock(a.last_seen)}</span>
                  <span>
                    <b>{a.strategy_id ?? a.source_component}</b> {market(a)} {a.direction ?? ""} — prevented by{" "}
                    <b>{a.prevented_by.condition ?? a.prevented_by.code}</b>
                    {a.prevented_by.code && a.prevented_by.condition !== a.prevented_by.code ? ` (${a.prevented_by.code})` : ""}
                    {a.sightings > 1 ? ` · seen on ${a.sightings} candles` : ""}{" "}
                    <button type="button" className="btn btn-soft btn-sm" onClick={() => setTraceId(a.first_event_id)}>Trace</button>
                  </span>
                </li>
              ))}
            </ul>
          ) : <p className="dim">No almost-trades recorded{scope ? " for this strategy" : ""}.</p>}
        </Card>

        <Card title="Recent evaluations" subtitle={scope ? `Decision traces for ${scope}` : "Decision traces, newest first — select a strategy to filter"}>
          {evaluations.data?.events.length ? (
            <ul className="gd-list" data-testid="guardian-evaluations">
              {evaluations.data.events.map((e) => (
                <li key={e.event_id}>
                  <Badge text={e.decision ?? e.event_type} tone={FINAL_TONE[e.decision ?? ""] ?? "default"} />
                  <span className="mono dim">{clock(e.timestamp)}</span>
                  <span>{e.source_component} · {market(e)}{e.reason ? ` — ${e.reason}` : ""}{" "}
                    <button type="button" className="btn btn-soft btn-sm" onClick={() => setTraceId(e.event_id)}>Trace</button></span>
                </li>
              ))}
            </ul>
          ) : <p className="dim">No evaluations recorded yet.</p>}
        </Card>
      </div>
    </>
  );
}
