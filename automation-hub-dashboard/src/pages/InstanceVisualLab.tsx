import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet } from "../lib/api";

/**
 * One page for every Trading Instance strategy.
 *
 * Everything shown here is derived from runtime evidence the backend already
 * publishes: the engine's own blocker, the strategy's gate sequence and the
 * persisted decision rows. This file computes no indicator and evaluates no
 * condition -- a Visual Lab that re-derived them would eventually disagree
 * with the strategy, and an operator would have no way to tell which was
 * wrong. Adding a strategy means adding a visual adapter on the backend, not
 * another page here.
 */

type GateState = "PASS" | "FAIL" | "WAITING" | "NOT_APPLICABLE";

interface Gate {
  id: string; stage: string; label: string; detail: string;
  state: GateState; blocker: string; explanation: string;
}
interface InstanceRow {
  instance_id: string; name: string; strategy_id: string; strategy_label?: string;
  strategy_version?: string; symbol: string; timeframe: string; htf?: string | null;
  venue: string; operating_mode?: string; runtime_state?: string;
  market_data_state?: string; market_data_mode?: string;
  last_closed_candle?: string | null; position_open: boolean;
  has_visual_adapter: boolean;
}
interface StrategyVisual {
  strategy_id: string; module: string; features: string[]; overlays: string[];
  entry_trigger: string; invalidation: string; stop_model: string;
  target_model: string; risk_requirements: string; notes: string;
}
interface LabState {
  instance: InstanceRow; strategy: StrategyVisual; decision_state: string;
  blocker: string | null; blocker_explanation: string; blocker_at?: string | null;
  required_next: string | null; gates: Gate[]; pipeline: string[];
  current_stage: string; position: Record<string, any> | null;
  last_closed_candle?: string | null; data_source?: string | null;
  mtf_evidence?: Record<string, any> | null;
}
interface TimelineEvent {
  id: number; timestamp: string; candle_identity: string; symbol: string;
  timeframe: string; strategy: string; side: string; regime: string;
  htf_bias: string; decision: string; final_state: string; gate_stage: string;
  blocker: string | null; blocker_explanation: string; reason: string;
  passed_rules: string[]; failed_rules: string[];
  components: Record<string, any>; executed: boolean;
}
interface Timeline {
  events: TimelineEvent[]; coverage: string;
  focus: { last_accepted: TimelineEvent | null; last_rejected: TimelineEvent | null;
           last_trade: TimelineEvent | null };
}
interface Candles {
  candles: { t: string; o: number; h: number; l: number; c: number }[];
  source: string; symbol: string; timeframe: string;
}

const STAGE_LABEL: Record<string, string> = {
  MARKET_DATA: "Market data", FEATURES: "Features", CONTEXT: "Context",
  SETUP: "Setup", CONFIRMATION: "Confirmation", STRATEGY_ACCEPT: "Strategy accept",
  RISK_CHECK: "Risk check", ORDER_INTENT: "Order intent", PAPER_BROKER: "Paper broker",
  FILL: "Fill", POSITION: "Position", EXIT: "Exit",
};

/** States that mean nothing can trade right now, for badge colour only. */
const BLOCKED_STATES = new Set([
  "DATA_BLOCKED", "WAITING_FOR_DATA", "WAITING_FOR_HTF", "RISK_BLOCKED",
  "SIGNAL_REJECTED", "PAUSED", "STOPPED", "SIGNALS_ONLY",
]);

const stamp = (value?: string | null) =>
  value ? value.replace("T", " ").replace("+00:00", " UTC").slice(0, 22) : "—";

const MARK: Record<GateState, string> = {
  PASS: "✓", FAIL: "✗", WAITING: "…", NOT_APPLICABLE: "–",
};

function GateList({ gates }: { gates: Gate[] }) {
  const stages = gates.reduce<Record<string, Gate[]>>((acc, gate) => {
    (acc[gate.stage] ||= []).push(gate);
    return acc;
  }, {});
  return <>{Object.entries(stages).map(([stage, rows]) => (
    <section key={stage}>
      <h3>{STAGE_LABEL[stage] ?? stage}</h3>
      <ul className="ivl-gates">
        {rows.map((gate) => (
          <li key={gate.id} className={`ivl-gate is-${gate.state.toLowerCase()}`}>
            <span className="ivl-mark">{MARK[gate.state]}</span>
            <span>
              <b>{gate.label}</b>
              {gate.detail ? <em>{gate.detail}</em> : null}
              {gate.state === "FAIL" && gate.blocker ? (
                <strong className="ivl-blocker">{gate.blocker} — {gate.explanation}</strong>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </section>
  ))}</>;
}

/** A plain candle chart with the decision markers the timeline supplies. */
function Chart({ candles, markers, focus }: {
  candles: Candles["candles"];
  markers: { t: string; accepted: boolean; label: string }[];
  focus?: string | null;
}) {
  if (!candles.length) return <div className="ivl-empty">No real candles to draw.</div>;
  const width = 1200, height = 320, pad = 8;
  const highs = candles.map((c) => c.h), lows = candles.map((c) => c.l);
  const top = Math.max(...highs), bottom = Math.min(...lows);
  const span = top - bottom || 1;
  const step = (width - pad * 2) / candles.length;
  const y = (price: number) => pad + (top - price) / span * (height - pad * 2);
  const x = (index: number) => pad + index * step + step / 2;
  const at = new Map(candles.map((c, i) => [c.t.slice(0, 16), i]));

  return (
    <svg className="ivl-chart" viewBox={`0 0 ${width} ${height}`} role="img"
         aria-label="Closed candles used by this instance">
      {candles.map((candle, index) => {
        const up = candle.c >= candle.o;
        const cx = x(index);
        return <g key={candle.t} className={up ? "up" : "down"}>
          <line x1={cx} x2={cx} y1={y(candle.h)} y2={y(candle.l)} />
          <rect x={cx - Math.max(step * 0.3, 0.6)} width={Math.max(step * 0.6, 1.2)}
                y={y(Math.max(candle.o, candle.c))}
                height={Math.max(Math.abs(y(candle.o) - y(candle.c)), 0.8)} />
        </g>;
      })}
      {markers.map((marker) => {
        const index = at.get(marker.t.slice(0, 16));
        if (index === undefined) return null;
        const cx = x(index);
        const isFocus = focus && marker.t.slice(0, 16) === focus.slice(0, 16);
        return <g key={`${marker.t}-${marker.label}`}
                  className={`ivl-marker ${marker.accepted ? "accepted" : "rejected"}${isFocus ? " is-focus" : ""}`}>
          <line x1={cx} x2={cx} y1={pad} y2={height - pad} />
          <circle cx={cx} cy={pad + 6} r={isFocus ? 6 : 4} />
        </g>;
      })}
    </svg>
  );
}

export default function InstanceVisualLab() {
  const [instances, setInstances] = useState<InstanceRow[]>([]);
  const [selected, setSelected] = useState("");
  const [state, setState] = useState<LabState | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [candles, setCandles] = useState<Candles | null>(null);
  const [dataError, setDataError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    void apiGet<{ instances: InstanceRow[] }>("/research/instance-visual/instances")
      .then((row) => {
        setInstances(row.instances);
        setSelected((current) => current || row.instances[0]?.instance_id || "");
      })
      .catch((exc) => setError(exc instanceof Error ? exc.message : String(exc)));
  }, []);

  const load = useCallback(async (instanceId: string) => {
    if (!instanceId) return;
    setLoading(true); setError(null); setDataError(null);
    const query = `instance_id=${encodeURIComponent(instanceId)}`;
    try {
      const [next, events] = await Promise.all([
        apiGet<LabState>(`/research/instance-visual/state?${query}`),
        apiGet<Timeline>(`/research/instance-visual/timeline?${query}`),
      ]);
      setState(next); setTimeline(events);
    } catch (exc) {
      setState(null); setTimeline(null);
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
    // Candles fail closed and separately: a refused feed must not blank the
    // decision panels, which are exactly what explains the refusal.
    try {
      setCandles(await apiGet<Candles>(`/research/instance-visual/candles?${query}`));
    } catch (exc) {
      setCandles(null);
      setDataError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  // Switching instances re-fetches in place; it never reloads the application.
  useEffect(() => { void load(selected); }, [selected, load]);

  const markers = useMemo(() => (timeline?.events ?? []).map((event) => ({
    t: event.timestamp, accepted: event.decision === "accepted",
    label: event.blocker ?? event.final_state,
  })), [timeline]);

  const focused = useMemo(
    () => (timeline?.events ?? []).find((e) => e.timestamp === focus) ?? null,
    [timeline, focus]);

  const current = instances.find((row) => row.instance_id === selected);

  return <div className="page ivl">
    <header className="ivl-head">
      <div>
        <h1>Instance Visual Lab</h1>
        <p className="lede">
          Observability over the running instance. Every state, gate and blocker below is
          read from the strategy runtime&rsquo;s own decision evidence — nothing here
          re-derives a strategy&rsquo;s conditions, and nothing here can place an order.
        </p>
      </div>
      <select aria-label="Trading instance" value={selected}
              onChange={(event) => { setFocus(null); setSelected(event.target.value); }}>
        {instances.map((row) => (
          <option key={row.instance_id} value={row.instance_id}>
            {row.name} · {row.symbol} · {row.timeframe}
            {row.has_visual_adapter ? "" : " (no visual adapter)"}
          </option>
        ))}
      </select>
    </header>

    {current ? <div className="ivl-identity">
      <span>Instance<b>{current.name}</b></span>
      <span>Strategy<b>{current.strategy_label || current.strategy_id}</b>
        <small>{current.strategy_id} · {current.strategy_version || "unversioned"}</small></span>
      <span>Market<b>{current.symbol} {current.timeframe}</b><small>{current.htf || "no HTF"}</small></span>
      <span>Venue<b>{current.venue}</b></span>
      <span>Mode<b>{current.operating_mode || "—"}</b></span>
      <span>Runtime<b>{(current.runtime_state || "—").toUpperCase()}</b></span>
      <span>Feed<b>{current.market_data_state || "—"}</b>
        <small>{current.market_data_mode || ""}</small></span>
      <span>Last closed candle<b>{stamp(current.last_closed_candle)}</b></span>
      <span>Position<b>{current.position_open ? "OPEN" : "flat"}</b></span>
    </div> : null}

    {error ? <div className="ivl-alert">
      <b>This instance cannot be visualised</b><span>{error}</span>
    </div> : null}

    {state ? <>
      <div className={`ivl-state ${BLOCKED_STATES.has(state.decision_state) ? "is-blocked" : ""}`}>
        <b>{state.decision_state.replace(/_/g, " ")}</b>
        {state.blocker ? <span className="ivl-code">{state.blocker}</span> : null}
        {state.blocker_explanation ? <span>{state.blocker_explanation}</span> : null}
        {state.required_next ? <em>Required next: {state.required_next}</em> : null}
        <small>as of {stamp(state.blocker_at)}</small>
      </div>

      <div className="ivl-pipeline">
        {state.pipeline.map((stage) => (
          <span key={stage} className={stage === state.current_stage ? "is-current" : ""}>
            {STAGE_LABEL[stage] ?? stage}
          </span>
        ))}
      </div>

      {dataError ? <div className="ivl-alert">
        <b>Market data unavailable — failing closed</b>
        <span>{dataError}</span>
        <small>The Visual Lab will not substitute sample or synthetic candles.</small>
      </div> : <Chart candles={candles?.candles ?? []} markers={markers} focus={focus} />}
      {candles ? <div className="ivl-provenance">
        {candles.candles.length} closed {candles.timeframe} candles · source {candles.source}
      </div> : null}

      <div className="ivl-grid">
        <div className="ivl-panel">
          <h2>How this strategy can place a trade</h2>
          <GateList gates={state.gates} />
        </div>

        <div className="ivl-panel">
          <h2>What this strategy reads</h2>
          <ul className="ivl-features">
            {state.strategy.overlays.map((feature) => (
              <li key={feature}>{feature.replace(/_/g, " ")}</li>
            ))}
          </ul>
          <dl>
            <dt>Entry trigger</dt><dd>{state.strategy.entry_trigger}</dd>
            <dt>Invalidation</dt><dd>{state.strategy.invalidation}</dd>
            <dt>Stop</dt><dd>{state.strategy.stop_model}</dd>
            <dt>Target</dt><dd>{state.strategy.target_model}</dd>
            <dt>Risk</dt><dd>{state.strategy.risk_requirements}</dd>
          </dl>
          {state.strategy.notes ? <p className="ivl-note">{state.strategy.notes}</p> : null}
          <small>Declared in services/strategy_visual_registry.py and checked against{" "}
            {state.strategy.module} by the test suite. Features this strategy does not
            consume are not drawn.</small>
        </div>
      </div>

      <div className="ivl-panel">
        <h2>Candle decision timeline</h2>
        <div className="ivl-focus">
          <button className={!focus ? "active" : ""} onClick={() => setFocus(null)}>Current</button>
          <button disabled={!timeline?.focus.last_rejected}
                  onClick={() => setFocus(timeline?.focus.last_rejected?.timestamp ?? null)}>
            Last rejected setup</button>
          <button disabled={!timeline?.focus.last_accepted}
                  onClick={() => setFocus(timeline?.focus.last_accepted?.timestamp ?? null)}>
            Last accepted setup</button>
          <button disabled={!timeline?.focus.last_trade}
                  onClick={() => setFocus(timeline?.focus.last_trade?.timestamp ?? null)}>
            Last trade</button>
        </div>
        {focused ? <div className="ivl-evidence">
          <b>{focused.decision.toUpperCase()} · {focused.final_state}</b>
          <span>{stamp(focused.timestamp)} · candle {focused.candle_identity}</span>
          {focused.blocker ? <span className="ivl-code">{focused.blocker} — {focused.blocker_explanation}</span> : null}
          <span>{focused.reason}</span>
          <span>passed: {focused.passed_rules.join(", ") || "—"}</span>
          <span>failed: {focused.failed_rules.join(", ") || "—"}</span>
        </div> : null}
        <div className="ivl-table-wrap">
          <table className="ivl-table">
            <thead><tr>
              <th>When</th><th>Candle</th><th>Side</th><th>Decision</th>
              <th>Stage</th><th>Blocker</th><th>Final</th>
            </tr></thead>
            <tbody>
              {(timeline?.events ?? []).map((event) => (
                <tr key={event.id}
                    className={`${event.decision === "accepted" ? "is-accepted" : "is-rejected"}`
                               + (focus === event.timestamp ? " is-focus" : "")}
                    onClick={() => setFocus(event.timestamp)}>
                  <td>{stamp(event.timestamp)}</td>
                  <td>{event.candle_identity || "—"}</td>
                  <td>{event.side || "—"}</td>
                  <td>{event.decision}</td>
                  <td>{event.gate_stage || "—"}</td>
                  <td>{event.blocker || "—"}</td>
                  <td>{event.final_state}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!(timeline?.events ?? []).length
            ? <div className="ivl-empty">{timeline?.coverage ?? "No recorded decisions."}</div>
            : null}
        </div>
        <small>{timeline?.coverage}</small>
      </div>

      <div className="ivl-foot">
        <b>OBSERVABILITY ONLY · NO ORDER PATH</b>
        <span>This page cannot place an order, change an operating mode or alter a parameter.</span>
      </div>
    </> : (loading ? <div className="ivl-empty">Reading the instance&rsquo;s decision evidence…</div> : null)}
  </div>;
}
