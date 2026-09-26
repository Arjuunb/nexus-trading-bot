import { useState } from "react";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader } from "../components/common/ui";
import { useApp } from "../app-context";
import { apiPost, apiPostJson, useLive, type PaperTradeRow, type SystemStatus, type AlertChannels, type AlertEvent, type EconProtection } from "../lib/api";
import { getProgress } from "../lib/progress";

const STAGES = ["Backtest", "Simulation", "Paper Trading", "Live Trading"];

export default function SafetyCenterPage() {
  const app = useApp();
  const sys = useLive<SystemStatus>("/system/status", 3000);
  const trades = useLive<PaperTradeRow[]>("/paper/trades", 4000);
  const prog = getProgress();

  const done = [
    prog.backtest,
    prog.simulation,
    (trades.data?.length ?? 0) > 0,
    !!sys.data?.broker_connected,
  ];

  const killAll = async () => {
    if (!window.confirm("KILL SWITCH: immediately halt all trading and stop the engine?")) return;
    try {
      await apiPost("/controls/stop-all");
      await apiPost("/engine/stop");
      app.toast("All trading halted", "error");
      sys.refetch();
    } catch {
      app.toast("Backend not reachable", "error");
    }
  };

  return (
    <>
      <PageHeader
        title="Safety Center"
        subtitle="Live-trading gate, progression flow, data separation and emergency controls"
        actions={<button className="btn btn-soft btn-sm" onClick={() => app.go("Live Trading")}><Icon name="chart" size={13} /> Live Trading</button>}
      />

      <LiveReadinessPanel />

      <Card title="Required Progression" subtitle="strategies can never skip a step">
        <div className="flow-row" style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
          {STAGES.map((name, i) => (
            <span key={name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="ui-badge" style={{ background: done[i] ? "#22c55e22" : "#5b647822", color: done[i] ? "#22c55e" : "#8a93a6", display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Icon name={done[i] ? "check" : "lock"} size={13} /> {name}
              </span>
              {i < STAGES.length - 1 && <Icon name="chevron" size={14} className="dim" />}
            </span>
          ))}
        </div>
        <p className="dim" style={{ marginTop: 10 }}>
          Live stays locked until every earlier stage has real, recorded results and a broker is connected.
        </p>
      </Card>

      <Card title="Data Separation" subtitle="datasets are never mixed or cross-shown">
        <div className="risk-list">
          <div className="risk-item"><b>Backtest</b> <span className="dim">— historical, on the Backtesting page.</span></div>
          <div className="risk-item"><b>Simulation</b> <span className="dim">— forward sim, on the Simulation page.</span></div>
          <div className="risk-item"><b>Paper</b> <span className="dim">— real engine, simulated money, on the Paper Trading page.</span></div>
          <div className="risk-item"><b>Live</b> <span className="dim">— locked; no live data exists until a broker is connected.</span></div>
        </div>
        <p className="dim" style={{ marginTop: 8 }}>Simulated and paper performance are always labelled as such — never shown as live results.</p>
      </Card>

      <Card title="Emergency Controls">
        <div className="estop-box">
          <div><b className="neg">Kill Switch</b><span className="dim">Immediately halt all trading and stop the engine (paper).</span></div>
          <button className="btn btn-danger" onClick={killAll}><Icon name="close" size={14} /> Stop Everything</button>
        </div>
      </Card>

      <AlertsPanel />
      <EconProtectionPanel />
    </>
  );
}

type Requirement = { key: string; label: string; passed: boolean; detail: string };
type LiveReadiness = {
  live_allowed: boolean; hard_locked: boolean; locked_reason: string;
  default_mode: string; passed: number; total: number; requirements: Requirement[];
};

function LiveReadinessPanel() {
  const app = useApp();
  const r = useLive<LiveReadiness>("/safety/live-readiness", 5000);
  const [testing, setTesting] = useState(false);
  const d = r.data;

  const testEstop = async () => {
    if (!window.confirm("Test the kill switch? This halts trading momentarily, then restores your current state.")) return;
    setTesting(true);
    try {
      const res = await apiPost<{ verified: boolean; state_after: string }>("/safety/test-emergency-stop");
      if (res.verified) app.toast(`Kill switch verified — restored to ${res.state_after}`, "success");
      else app.toast("Kill switch did NOT halt trading — investigate", "error");
      r.refetch();
    } catch {
      app.toast("Test needs the webhook secret", "error");
    } finally {
      setTesting(false);
    }
  };

  const locked = !d || d.hard_locked || !d.live_allowed;
  return (
    <Card
      title="Live Trading Readiness"
      subtitle="live is locked by default — it unlocks only when every requirement below is met on real state"
      right={d && <Badge text={locked ? "LOCKED" : "READY"} tone={locked ? "red" : "green"} />}
    >
      {!d ? <div className="dim">Loading readiness…</div> : (
        <>
          <div className="card" style={{ marginBottom: 10, borderColor: locked ? "#ef4444" : "#22c55e",
            background: locked ? "rgba(239,68,68,0.08)" : "rgba(34,197,94,0.08)", display: "flex", alignItems: "center", gap: 10 }}>
            <Icon name={locked ? "lock" : "check"} size={16} className={locked ? "neg" : "pos"} />
            <span>
              <b className={locked ? "neg" : "pos"}>{locked ? "Live trading is LOCKED." : "Live trading requirements met."}</b>{" "}
              <span className="dim">{d.locked_reason}</span>
            </span>
          </div>

          <div className="risk-list">
            {d.requirements.map((req) => (
              <div key={req.key} className="risk-item">
                <span style={{ fontSize: 13 }}>
                  {req.label} <span className="dim">· {req.detail}</span>
                </span>
                <Badge text={req.passed ? "Passed" : "Required"} tone={req.passed ? "green" : "red"} />
              </div>
            ))}
          </div>

          <div className="row-actions" style={{ justifyContent: "space-between", alignItems: "center", marginTop: 10, gap: 8, flexWrap: "wrap" }}>
            <span className="dim" style={{ fontSize: 12 }}>{d.passed} / {d.total} requirements passed · default mode <b>{d.default_mode}</b></span>
            <button className="btn btn-soft" onClick={testEstop} disabled={testing}>
              <Icon name="shield" size={13} /> {testing ? "Testing…" : "Test Emergency Stop"}
            </button>
          </div>
        </>
      )}
    </Card>
  );
}

function EconProtectionPanel() {
  const app = useApp();
  const live = useLive<EconProtection>("/econ/protection", 15000);
  const e = live.data;
  const [syncing, setSyncing] = useState(false);
  const syncNow = async () => {
    setSyncing(true);
    try {
      const r = await apiPost<{ last_error: string | null; events: number }>("/econ/feed/sync");
      if (r.last_error) app.toast(`Calendar fetch failed: ${r.last_error}`, "error");
      else app.toast(`Calendar updated: ${r.events} high-impact releases this week`, "success");
      void live.refetch();
    } catch { app.toast("Fetching needs the webhook secret", "error"); }
    finally { setSyncing(false); }
  };
  const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "never");
  const tone = e?.mode === "normal" ? "green" : e?.mode === "caution" ? "amber" : "red";
  return (
    <Card title="Economic Event Protection" subtitle="halt / reduce size / widen stops around CPI · FOMC · NFP · rate decisions"
      right={e && <Badge text={e.mode} tone={tone as any} />}>
      {!e ? <div className="dim">—</div> : (
        <>
          <div className="risk-list">
            <div className="risk-item"><span className="dim">Next high-impact event</span> <b>{e.next_event ? `${e.next_event.name} · ${e.minutes_to_event! < 0 ? `released ${Math.round(-e.minutes_to_event!)}m ago` : e.minutes_to_event! >= 60 ? `in ${(e.minutes_to_event! / 60).toFixed(1)}h` : `in ${e.minutes_to_event}m`}` : "none scheduled"}</b></div>
            <div className="risk-item"><span className="dim">Risk multiplier</span> <b>{Math.round(e.risk_multiplier * 100)}%</b></div>
            <div className="risk-item"><span className="dim">Stop multiplier</span> <b>{e.stop_multiplier}×</b></div>
            <div className="risk-item"><span className="dim">New entries</span> <Badge text={e.halt_new_entries ? "halted" : "allowed"} tone={e.halt_new_entries ? "red" : "green"} /></div>
          </div>
          {e.actions.length > 0 && (
            <div className="card" style={{ marginTop: 8, borderColor: "var(--gold)", background: "rgba(234,181,79,0.08)" }}>
              <b className="amber"><Icon name="shield" size={13} /> Protective actions</b>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18, lineHeight: 1.5 }}>{e.actions.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </div>
          )}
          {e.feed ? (
            <div className="risk-list" style={{ marginTop: 8 }}>
              <div className="risk-item"><span className="dim">Calendar feed</span>
                <b>{e.feed.enabled ? `${e.feed.source} · ${e.feed.countries.join(", ")} · ${e.feed.events} high-impact this week${typeof e.feed.rows_seen === "number" ? ` (of ${e.feed.rows_seen} releases in the export)` : ""}` : "off"}</b></div>
              <div className="risk-item"><span className="dim">Last fetched</span>
                <b>{when(e.feed.last_success)}{e.feed.last_error ? <span className="neg"> · last try failed: {e.feed.last_error}</span> : null}</b></div>
              <div className="row-actions" style={{ justifyContent: "flex-end" }}>
                <button className="btn btn-ghost btn-sm" onClick={syncNow} disabled={syncing || !e.feed.enabled}>
                  <Icon name="refresh" size={12} /> {syncing ? "Fetching…" : "Fetch now"}
                </button>
              </div>
            </div>
          ) : null}
          {!e.connected && <p className="dim" style={{ fontSize: 11, marginTop: 8 }}><Icon name="info" size={12} /> {e.note}</p>}
        </>
      )}
    </Card>
  );
}

function AlertsPanel() {
  const app = useApp();
  const ch = useLive<{ channels: AlertChannels }>("/alerts/channels", 8000);
  const live = useLive<{ alerts: AlertEvent[] }>("/alerts/check", 10000);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const c = ch.data?.channels;

  const save = async () => {
    try { const r = await apiPostJson<any>("/alerts/channels", keys); if (r?.error || r?.detail) app.toast(r.error || r.detail, "error"); else { app.toast("Alert channels saved", "success"); setKeys({}); ch.refetch(); } }
    catch { app.toast("Saving needs the webhook secret", "error"); }
  };
  const test = async () => {
    try { await apiPost("/alerts/test"); app.toast("Test alert sent to connected channels", "success"); }
    catch { app.toast("Test needs the webhook secret", "error"); }
  };
  const sev = (s: string) => (s === "critical" ? "red" : s === "warning" ? "amber" : "blue");
  const chip = (name: string, st?: { connected: boolean; note: string }) => (
    <div className="risk-item"><span className="dim">{name}</span>
      <Badge text={st?.connected ? "Connected" : "Not connected"} tone={st?.connected ? "green" : "default"} /></div>
  );

  return (
    <Card title="Alerts" subtitle="trade / risk / sentiment alerts to Telegram, Discord, Email"
      right={<div className="row-actions" style={{ gap: 6 }}><button className="btn btn-soft" onClick={test}><Icon name="bell" size={13} /> Test</button></div>}>
      <div className="grid-2-eq">
        <div>
          <div className="card-subtitle" style={{ marginBottom: 6 }}>Channels</div>
          <div className="risk-list">
            {chip("Telegram", c?.telegram)}
            {chip("Discord", c?.discord)}
            {chip("Email", c?.email)}
          </div>
          <div className="form-grid-2" style={{ marginTop: 8 }}>
            <label className="field"><span className="field-label">Discord webhook</span>
              <input type="password" placeholder={c?.discord?.connected ? "•••• connected" : "webhook URL"} value={keys.discord_webhook ?? ""} onChange={(e) => setKeys((k) => ({ ...k, discord_webhook: e.target.value }))} /></label>
            <label className="field"><span className="field-label">Alert email</span>
              <input placeholder="you@example.com" value={keys.email_to ?? ""} onChange={(e) => setKeys((k) => ({ ...k, email_to: e.target.value }))} /></label>
            <label className="field"><span className="field-label">SMTP host</span>
              <input placeholder="smtp.gmail.com" value={keys.smtp_host ?? ""} onChange={(e) => setKeys((k) => ({ ...k, smtp_host: e.target.value }))} /></label>
            <label className="field"><span className="field-label">SMTP user / pass</span>
              <input type="password" placeholder="user:pass via two fields" value={keys.smtp_user ?? ""} onChange={(e) => setKeys((k) => ({ ...k, smtp_user: e.target.value }))} /></label>
          </div>
          <button className="btn btn-primary" style={{ marginTop: 8 }} onClick={save}><Icon name="check" size={13} /> Save Channels</button>
          <p className="dim" style={{ fontSize: 11, marginTop: 6 }}>Missing channels show “Not connected” — never faked. Telegram uses the env token.</p>
        </div>
        <div>
          <div className="card-subtitle" style={{ marginBottom: 6 }}>Live alerts now</div>
          {(live.data?.alerts?.length ?? 0) === 0 ? <div className="dim" style={{ fontSize: 13 }}>No alert conditions firing right now.</div> : (
            <div className="alert-stack">
              {live.data!.alerts.map((a, i) => (
                <div key={i} className="risk-item" style={{ alignItems: "flex-start", flexDirection: "column", gap: 2 }}>
                  <span className="row-actions" style={{ gap: 6 }}><Badge text={a.severity} tone={sev(a.severity) as any} /> <b>{a.title}</b></span>
                  <span className="dim" style={{ fontSize: 12 }}>{a.detail}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
