import { useEffect, useMemo, useState } from "react";
import { apiPostJson, useLive } from "../lib/api";

/**
 * What the SMC agent decided, and why, on every closed candle.
 *
 * The agent approves and places its own paper orders with nobody in the loop.
 * That is the point of it, and it is also why this page exists: an autonomous
 * trader you cannot read back is one you have to take on trust.
 *
 * The four outcomes are deliberately not collapsed into "traded / did not
 * trade", because they fail in different directions and only one of them
 * means something is broken:
 *
 *   TAKEN      the agent opened a trade
 *   REJECTED   the strategy offered one and an agent gate refused it
 *   NOT_READY  the strategy itself offered nothing
 *   MISSED     the strategy offered one and the agent FAILED to act
 *
 * An agent rejecting everything and an agent that is not running both show an
 * empty trade list; they are not the same thing, and this page has to say
 * which one you are looking at. MISSED is the row to care about.
 *
 * Read-only. Nothing here places, approves, cancels or configures anything.
 */

type Outcome = "TAKEN" | "REJECTED" | "NOT_READY" | "MISSED";

interface Decision {
  id: string; at: string; candle_time: string; symbol: string; timeframe: string;
  smc_state: string; outcome: Outcome; reason_code: string; reason: string;
  proposal_id?: string; trade_id?: string;
  gates?: Record<string, unknown> | null;
  plan?: Record<string, unknown> | null;
}
interface Trade {
  id: string; decision_id: string; symbol: string; timeframe: string;
  side?: string; entry_price?: number; stop_price?: number; target_price?: number;
  size?: number; requested_size?: number; size_capped?: number;
  opened_at?: string; closed_at?: string | null;
  exit_price?: number | null; realised_r?: number | null;
}
interface AgentStatus {
  attached?: boolean; is_approver?: boolean; gates_orders_in_mode?: string;
  minimum_reward_to_risk?: number | null;
  last_result?: Record<string, unknown> | null;
}
interface AgentResponse {
  agent?: AgentStatus;
  feed?: { state?: string; reliable?: boolean;
    transport_diagnostics?: { channels?: Record<string, string> | null } | null };
  execution_state?: string; blockers?: string[];
  decisions?: Decision[]; decision_counts?: Record<string, number>;
  trades?: Trade[]; open_trades?: number;
  session_id?: string; symbol?: string; timeframe?: string;
}


interface TradePolicy { enabled: boolean; breakeven_at_r: number | null;
  breakeven_offset_r: number; trail_after_r: number | null;
  trail_lookback: number; trail_buffer_r: number }
interface ContextPolicyShape { enabled: boolean; daily_loss_cap_r: number | null;
  max_consecutive_losses: number | null; allowed_hours_utc: number[][];
  min_candle_range_bps: number | null; volatility_lookback: number }
interface MemoryPolicyShape { enabled: boolean; min_sample: number;
  veto_at_or_below_expectancy_r: number; by_hour: boolean }
interface PolicyResponse { trade_management: TradePolicy;
  context: ContextPolicyShape; memory: MemoryPolicyShape; note?: string }

const OUTCOMES: Outcome[] = ["TAKEN", "REJECTED", "NOT_READY", "MISSED"];

const BLURB: Record<Outcome, string> = {
  TAKEN: "the agent opened a trade",
  REJECTED: "an agent gate refused a setup the strategy offered",
  NOT_READY: "the strategy offered nothing to judge",
  MISSED: "the strategy offered a setup and the agent could not act",
};

function when(value?: string) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function num(value: unknown, digits = 2) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(digits) : "—";
}


/**
 * The three optional rule-sets.
 *
 * Each one changes how much and how often the agent trades, so the switch
 * sits next to the decisions it will change rather than on a settings page
 * away from the evidence. The wording is deliberate: these are hypotheses to
 * backtest, and nothing here should read as an improvement.
 */
function RulePanel() {
  const live = useLive<PolicyResponse>("/research/smc/agent/policy", 30_000);
  const [draft, setDraft] = useState<PolicyResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => { if (live.data && !draft) setDraft(live.data); }, [live.data, draft]);
  if (!draft) return <p className="pa-note">Reading the agent&rsquo;s rule-sets&hellip;</p>;

  const set = (section: keyof PolicyResponse, key: string, value: unknown) =>
    setDraft({ ...draft, [section]: { ...(draft[section] as object), [key]: value } });

  const save = async () => {
    setBusy(true); setNote("");
    try {
      await apiPostJson("/research/smc/agent/policy", {
        trade_management: draft.trade_management,
        context: draft.context, memory: draft.memory });
      setNote("Saved and applied to the running agent.");
    } catch (error) {
      setNote(`Not saved: ${error instanceof Error ? error.message : String(error)}`);
    } finally { setBusy(false); }
  };

  const numberOrNull = (raw: string) => raw.trim() === "" ? null : Number(raw);

  return <section>
    <h2>Rules</h2>
    <p className="pa-note">
      Each of these changes how much and how often the agent trades. They are
      <b> hypotheses to backtest and forward-test</b>, not improvements &mdash;
      and every one of them can only make the agent trade <i>less</i>, never
      take a setup the strategy did not offer.
    </p>

    <div className="pa-rules">
      <label><input type="checkbox" checked={draft.trade_management.enabled}
        onChange={(e) => set("trade_management", "enabled", e.target.checked)} />
        <b>Manage open trades</b>
        <small>Move the stop to breakeven at 1R, then trail behind structure.
          Never widens a stop and never moves a target, so the runner keeps
          the reward-to-risk it was taken at.</small></label>
      <label>Breakeven at (R)<input value={draft.trade_management.breakeven_at_r ?? ""}
        onChange={(e) => set("trade_management", "breakeven_at_r", numberOrNull(e.target.value))} /></label>
      <label>Trail after (R, blank = off)<input value={draft.trade_management.trail_after_r ?? ""}
        onChange={(e) => set("trade_management", "trail_after_r", numberOrNull(e.target.value))} /></label>

      <label><input type="checkbox" checked={draft.context.enabled}
        onChange={(e) => set("context", "enabled", e.target.checked)} />
        <b>Context vetoes</b>
        <small>Stand aside on a daily loss cap, a losing streak, outside
          session hours, or when the range has gone flat.</small></label>
      <label>Daily loss cap (R, blank = off)<input value={draft.context.daily_loss_cap_r ?? ""}
        onChange={(e) => set("context", "daily_loss_cap_r", numberOrNull(e.target.value))} /></label>
      <label>Consecutive losses (blank = off)<input value={draft.context.max_consecutive_losses ?? ""}
        onChange={(e) => set("context", "max_consecutive_losses", numberOrNull(e.target.value))} /></label>

      <label><input type="checkbox" checked={draft.memory.enabled}
        onChange={(e) => set("memory", "enabled", e.target.checked)} />
        <b>Journal memory</b>
        <small>Decline a setup family its own history has lost on. Says
          nothing below the sample floor &mdash; a small sample always shows a
          pattern whether or not one exists.</small></label>
      <label>Minimum sample<input value={draft.memory.min_sample}
        onChange={(e) => set("memory", "min_sample", Number(e.target.value))} /></label>
    </div>

    <button type="button" className="pa-export" disabled={busy} onClick={() => void save()}>
      {busy ? "Saving\u2026" : "Save and apply"}</button>
    {note ? <p className="pa-note">{note}</p> : null}
  </section>;
}

export default function SMCAgentPage() {
  const [filter, setFilter] = useState<"" | Outcome>("");
  const query = filter ? `?outcome=${filter}&limit=200` : "?limit=200";
  const feedState = useLive<AgentResponse>(`/research/smc/agent${query}`, 5_000);
  const data = feedState.data;

  const agent = data?.agent ?? {};
  const counts = data?.decision_counts ?? {};
  const decisions = data?.decisions ?? [];
  const trades = data?.trades ?? [];
  const channels = data?.feed?.transport_diagnostics?.channels ?? null;

  // A trade per decision, so a row can show what its decision became.
  const tradeByDecision = useMemo(() => {
    const map = new Map<string, Trade>();
    for (const row of trades) if (row.decision_id) map.set(row.decision_id, row);
    return map;
  }, [trades]);

  const approving = agent.is_approver === true;
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);

  return <div className="page smc-agent">
    <header>
      <h1>SMC Agent</h1>
      <p className="lede">
        Every closed candle the agent looked at, what it decided and why.
        It trades without anyone approving, so this is the record that holds
        it to account. Nothing on this page can place or change an order.
      </p>
    </header>

    {/* Is it actually running? This is the question the page is opened with. */}
    <section>
      <h2>Status</h2>
      <div className="pa-grid">
        <div><small>Approver</small><b>{
          agent.attached === false ? "NO AGENT ATTACHED"
            : approving ? "AGENT · no human approval"
            : "STOOD DOWN"
        }</b></div>
        <div><small>Gates orders in mode</small>
          <b>{agent.gates_orders_in_mode ?? "—"}</b></div>
        <div><small>Minimum reward-to-risk</small>
          <b>{agent.minimum_reward_to_risk != null
            ? `1:${num(agent.minimum_reward_to_risk, 1)}` : "—"}</b></div>
        <div><small>Execution state</small>
          <b>{data?.execution_state ?? "—"}</b></div>
        <div><small>Feed</small><b>{data?.feed?.state ?? "—"}
          {data?.feed?.reliable === false ? " · not reliable" : ""}</b></div>
        <div><small>Open agent trades</small><b>{data?.open_trades ?? 0}</b></div>
      </div>

      {!approving && agent.attached !== false ? <p className="pa-note">
        The agent is attached but is not the approver, so it is judging
        nothing. It gates orders in <b>{agent.gates_orders_in_mode ?? "manual_approval"}</b>
        {" "}— the &ldquo;Agent decides&rdquo; paper mode. In Automatic paper the
        lab places the strategy&rsquo;s entry before the agent can see it, so the
        agent stands down rather than claim an order it did not gate.
      </p> : null}

      {channels ? <p className="pa-note">
        Sockets — {Object.entries(channels)
          .map(([name, state]) => `${name}: ${state}`).join(" · ")}
      </p> : null}

      {(data?.blockers ?? []).length ? <ul className="pa-blockers">
        {(data?.blockers ?? []).map((row) => <li key={row}>{row}</li>)}
      </ul> : null}
    </section>

    <RulePanel />

    {/* The four outcomes, never collapsed into traded / did not trade. */}
    <section>
      <h2>Decisions</h2>
      <div className="pa-grid">
        {OUTCOMES.map((name) => <div key={name}>
          <small>{name}</small>
          <b>{counts[name] ?? 0}</b>
          <small>{BLURB[name]}</small>
        </div>)}
      </div>

      <div className="pa-filters">
        <button type="button" className={filter === "" ? "is-on" : ""}
                onClick={() => setFilter("")}>All</button>
        {OUTCOMES.map((name) => <button key={name} type="button"
          className={filter === name ? "is-on" : ""}
          onClick={() => setFilter(name)}>{name}</button>)}
      </div>

      {feedState.error ? <p className="pa-note">
        Could not reach the agent endpoint. Nothing is inferred from that —
        this page shows only what it read.
      </p> : null}

      {!decisions.length ? <p className="pa-note">
        {total === 0 && !filter
          ? "No decision has been recorded yet. The agent writes a row for every closed candle it looks at, so an empty list means it has not run — check the feed and the operating mode above."
          : "No decision matches this filter."}
      </p> : <table className="pa-table">
        <thead><tr>
          <th>Candle</th><th>Outcome</th><th>SMC state</th>
          <th>Reward-to-risk</th><th>Why</th><th>Became</th>
        </tr></thead>
        <tbody>
          {decisions.map((row) => {
            const trade = row.trade_id ? tradeByDecision.get(row.id) : undefined;
            return <tr key={row.id}>
              <td>{when(row.candle_time || row.at)}</td>
              <td><span className={`pa-outcome is-${row.outcome.toLowerCase()}`}>
                {row.outcome}</span></td>
              <td>{row.smc_state || "—"}</td>
              <td>{num((row.gates ?? {})["reward_to_risk"])}</td>
              <td><b>{row.reason_code}</b><br /><small>{row.reason}</small></td>
              <td>{trade
                ? `${trade.side ?? ""} ${num(trade.size, 4)} @ ${num(trade.entry_price)}`
                  + (trade.closed_at ? ` · ${num(trade.realised_r)}R` : " · open")
                : "—"}</td>
            </tr>;
          })}
        </tbody>
      </table>}
    </section>

    {/* Size is shown as requested vs executed: a capped trade is not a
        rejected one, and the journal records both so the difference stays
        visible rather than being quietly rounded away. */}
    <section>
      <h2>Agent trades</h2>
      {!trades.length ? <p className="pa-note">
        The agent has opened no trade yet.
      </p> : <table className="pa-table">
        <thead><tr>
          <th>Opened</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th>
          <th>Size (requested → placed)</th><th>Closed</th><th>R</th>
        </tr></thead>
        <tbody>
          {trades.map((row) => <tr key={row.id}>
            <td>{when(row.opened_at)}</td>
            <td>{row.side ?? "—"}</td>
            <td>{num(row.entry_price)}</td>
            <td>{num(row.stop_price)}</td>
            <td>{num(row.target_price)}</td>
            <td>{num(row.requested_size, 4)} → {num(row.size, 4)}
              {row.size_capped ? <b> · capped</b> : null}</td>
            <td>{row.closed_at ? when(row.closed_at) : "open"}</td>
            <td>{row.realised_r != null ? `${num(row.realised_r)}R` : "—"}</td>
          </tr>)}
        </tbody>
      </table>}
    </section>
  </div>;
}
