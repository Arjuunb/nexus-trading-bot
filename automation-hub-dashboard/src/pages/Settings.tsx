import { useEffect, useState, type ChangeEvent, type ReactNode } from "react";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, Field, PageHeader } from "../components/common/ui";
import { useApp } from "../app-context";
import { API_BASE, apiPost, apiPostJson, useLive, type BotSettings, type EngineStatus } from "../lib/api";
import SettingsNav, { SETTINGS_SECTIONS } from "../components/settings/SettingsNav";
import GeneralSettings from "../components/settings/GeneralSettings";
import TradingDefaultsSettings from "../components/settings/TradingDefaultsSettings";
import MarketDataSettings from "../components/settings/MarketDataSettings";
import PaperSettings from "../components/settings/PaperSettings";
import LiveTradingSettings from "../components/settings/LiveTradingSettings";
import RiskSettings from "../components/settings/RiskSettings";
import NotificationSettings from "../components/settings/NotificationSettings";
import SystemSettings from "../components/settings/SystemSettings";
import SecuritySettings from "../components/settings/SecuritySettings";
import AuditLogPanel from "../components/settings/AuditLogPanel";
import ExchangeKeysPanel from "../components/settings/ExchangeKeysPanel";
import BackupsPanel from "../components/settings/BackupsPanel";
import ApiKeysPanel from "../components/settings/ApiKeysPanel";
import AdvancedSettings from "../components/settings/AdvancedSettings";

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const SESSIONS: Record<string, [number, number]> = { London: [7, 16], "New York": [12, 21], Asia: [0, 9], "24h": [0, 24] };

export default function SettingsPage() {
  const readSection = () => {
    const raw = window.location.hash.split("?", 2)[1] ?? "";
    const candidate = new URLSearchParams(raw).get("section") ?? "general";
    return SETTINGS_SECTIONS.some(([id]) => id === candidate) ? candidate : "general";
  };
  const [active, setActive] = useState(readSection);
  const [search, setSearch] = useState("");
  useEffect(() => { const onHash = () => setActive(readSection()); window.addEventListener("hashchange", onHash); return () => window.removeEventListener("hashchange", onHash); }, []);
  const select = (id: string) => {
    const path = window.location.hash.replace(/^#\/?/, "").split("?", 1)[0] || "settings";
    window.location.hash = `/${path}?section=${id}`;
  };
  const labels = new Map<string, string>(SETTINGS_SECTIONS as readonly (readonly [string, string])[]);
  const visible = search.trim() ? SETTINGS_SECTIONS.filter(([, label]) => label.toLowerCase().includes(search.trim().toLowerCase())) : SETTINGS_SECTIONS.filter(([id]) => id === active);
  const render = (id: string) => id === "general" ? <GeneralSettings />
    : id === "trading" ? <TradingDefaultsSettings />
    : id === "market-data" ? <MarketDataSettings />
    : id === "paper" ? <PaperSettings />
    : id === "live" ? <LiveTradingSettings />
    : id === "risk" ? <RiskSettings />
    : id === "notifications" ? <NotificationSettings />
    : id === "system" ? <SystemSettings />
    : id === "security" ? <><SecuritySettings /><ExchangeKeysPanel /><ApiKeysPanel /><BackupsPanel /><AuditLogPanel /></>
    : <AdvancedSettings><LegacyEngineSettings /></AdvancedSettings>;
  return <>
    <PageHeader title="Settings Centre" subtitle="User preferences, platform defaults, safety limits and truthful runtime status" />
    <div className="settings-search"><input aria-label="Search settings" placeholder="Search settings…" value={search} onChange={(e) => setSearch(e.target.value)} /></div>
    <div className="settings-layout"><SettingsNav active={active} onSelect={select} /><div className="settings-content">{visible.length ? visible.map(([id]) => <div key={id} aria-label={labels.get(id)}>{render(id)}</div>) : <div className="card dim">No settings section matches that search.</div>}</div></div>
  </>;
}

// Real legacy configuration. Risk/position params are editable, applied live,
// and persisted on the backend. It is intentionally isolated under Advanced.
export function LegacyEngineSettings() {
  const app = useApp();
  const { data, error, refetch } = useLive<BotSettings>("/settings", 8000);
  const engine = useLive<EngineStatus>("/engine/status", 5000);
  const [f, setF] = useState<Record<string, string>>({});
  const [days, setDays] = useState<boolean[]>([]);
  const [symbols, setSymbols] = useState("");
  const [pairMode, setPairMode] = useState<"auto" | "manual">("auto");
  const [manualPair, setManualPair] = useState("");
  const [pairLoaded, setPairLoaded] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (data && days.length === 0) {
      const m = data.editable.trading_days_mask;
      setDays(Array.from({ length: 7 }, (_, i) => !!((m >> i) & 1)));
    }
  }, [data, days]);
  useEffect(() => { if (engine.data && symbols === "") setSymbols(engine.data.symbols.join(", ")); }, [engine.data, symbols]);
  useEffect(() => {
    if (engine.data && !pairLoaded) {
      setPairMode(engine.data.symbol_selection_mode === "manual" ? "manual" : "auto");
      setManualPair(engine.data.manual_symbol ?? engine.data.symbols[0] ?? "BTCUSDT");
      setPairLoaded(true);
    }
  }, [engine.data, pairLoaded]);

  const applySymbols = async () => {
    const list = symbols.split(",").map((s) => s.trim()).filter(Boolean);
    try { await apiPostJson("/market/symbols", { symbols: list }); app.toast(`Watchlist applied: ${list.join(", ")}`, "success"); }
    catch { app.toast("Apply failed", "error"); }
  };
  const applyPairSelection = async () => {
    const symbol = manualPair.trim().toUpperCase();
    if (pairMode === "manual" && !symbol) { app.toast("Enter one pair, for example BTCUSDT", "error"); return; }
    try {
      await apiPostJson("/engine/symbol-selection", { mode: pairMode, manual_symbol: symbol || undefined });
      app.toast(pairMode === "manual" ? `Manual mode: only ${symbol} will trade` : "Automatic watchlist mode restored", "success");
      engine.refetch();
    } catch (error) { app.toast(error instanceof Error ? error.message : "Could not apply pair selection", "error"); }
  };
  const preset = (name: string) => { const [s, e] = SESSIONS[name]; setF((p) => ({ ...p, sstart: String(s), send: String(e) })); };

  useEffect(() => {
    if (data && Object.keys(f).length === 0) {
      setF({
        risk: (data.editable.risk_per_trade_pct * 100).toString(),
        exposure: (data.editable.exposure_limit_pct * 100).toString(),
        drawdown: (data.editable.max_drawdown_pct * 100).toString(),
        maxpos: String(data.editable.max_open_positions),
        dedup: String(data.editable.dedup_window_s),
        daily: (data.editable.max_daily_loss_pct * 100).toString(),
        sstart: String(data.editable.session_start),
        send: String(data.editable.session_end),
        weekly: (data.editable.max_weekly_loss_pct * 100).toString(),
        maxday: String(data.editable.max_trades_per_day),
        consec: String(data.editable.max_consecutive_losses),
        cooldown: String(data.editable.cooldown_after_loss_min),
        entrymode: data.editable.entry_mode ?? "limit",
        reporthour: String(data.editable.daily_report_hour ?? 8),
        sizingmode: data.editable.position_sizing_mode ?? "auto",
        fixedsize: String(data.editable.fixed_position_size ?? 0),
      });
    }
  }, [data, f]);

  const set = (k: string) => (e: ChangeEvent<HTMLInputElement>) => setF((p) => ({ ...p, [k]: e.target.value }));

  const save = async () => {
    setSaving(true);
    try {
      await apiPostJson("/settings", {
        risk_per_trade_pct: Number(f.risk) / 100,
        exposure_limit_pct: Number(f.exposure) / 100,
        max_drawdown_pct: Number(f.drawdown) / 100,
        max_open_positions: Math.round(Number(f.maxpos)),
        dedup_window_s: Math.round(Number(f.dedup)),
        max_daily_loss_pct: Number(f.daily) / 100,
        session_start: Math.round(Number(f.sstart)),
        session_end: Math.round(Number(f.send)),
        max_weekly_loss_pct: Number(f.weekly) / 100,
        max_trades_per_day: Math.round(Number(f.maxday)),
        max_consecutive_losses: Math.round(Number(f.consec)),
        cooldown_after_loss_min: Math.round(Number(f.cooldown)),
        trading_days_mask: days.reduce((acc, on, i) => (on ? acc | (1 << i) : acc), 0),
        entry_mode: f.entrymode === "market" ? "market" : "limit",
        daily_report_hour: Math.round(Number(f.reporthour)),
        position_sizing_mode: f.sizingmode === "fixed" ? "fixed" : "auto",
        fixed_position_size: Number(f.fixedsize),
      });
      app.toast("Settings saved & applied (persisted on backend)", "success");
      refetch();
    } catch {
      app.toast("Save failed — backend unreachable or invalid value", "error");
    } finally {
      setSaving(false);
    }
  };

  const ro = data?.readonly;

  return (
    <>
      {error && !data && (
        <div className="card" style={{ borderColor: "#ef4444" }}>
          <Icon name="warning" size={15} className="neg" /> Backend not reachable — settings unavailable.
        </div>
      )}

      <details className="card" style={{ marginTop: 14 }}>
        <summary style={{ cursor: "pointer", fontWeight: 700 }}>Legacy Autonomous Engine <span className="dim">— stopped by default; not used by Trading Instances</span></summary>
        <p className="dim" style={{ marginTop: 10 }}>These retained controls exist for backward-compatible diagnostics only. They do not override an active Trading Instance’s pair, strategy, timeframe, risk, sizing, entry mode, or market-data mode.</p>
        <button className="btn btn-soft" disabled={saving || !data} onClick={save}><Icon name="check" size={14} /> {saving ? "Saving…" : "Save legacy settings"}</button>
      <div className="grid-2-eq">
        <Card title="Legacy Risk Management" subtitle="legacy worker only · persisted">
          <div className="form-grid-2">
            <Field label="Risk per trade (%)"><input value={f.risk ?? ""} onChange={set("risk")} inputMode="decimal" /></Field>
            <Field label="Max exposure (% equity)"><input value={f.exposure ?? ""} onChange={set("exposure")} inputMode="decimal" /></Field>
            <Field label="Max drawdown halt (%)"><input value={f.drawdown ?? ""} onChange={set("drawdown")} inputMode="decimal" /></Field>
          </div>
          <p className="dim" style={{ marginTop: 8 }}>
            The drawdown breaker auto-halts new entries when realized drawdown is breached. Exits are never blocked.
          </p>
        </Card>

        <Card title="Legacy Position & Execution" subtitle="legacy worker only · persisted">
          <div className="form-grid-2">
            <Field label="Max open positions"><input value={f.maxpos ?? ""} onChange={set("maxpos")} inputMode="numeric" /></Field>
            <Field label="Duplicate window (s)" hint="reject repeat alert_id within this window"><input value={f.dedup ?? ""} onChange={set("dedup")} inputMode="numeric" /></Field>
            <Field label="Entry mode" hint="limit = maker entries (measured better); market = immediate">
              <select value={f.entrymode ?? "limit"} onChange={(e) => setF((prev) => ({ ...prev, entrymode: e.target.value }))}>
                <option value="limit">limit (maker)</option>
                <option value="market">market (taker)</option>
              </select>
            </Field>
            <Field label="Position sizing" hint="auto = risk-based; manual = your fixed base-asset quantity">
              <select value={f.sizingmode ?? "auto"} onChange={(e) => setF((prev) => ({ ...prev, sizingmode: e.target.value }))}>
                <option value="auto">automatic (risk-based)</option>
                <option value="fixed">manual fixed quantity</option>
              </select>
            </Field>
            <Field label="Manual quantity / lots" hint="native base units, e.g. 0.01 BTC; used only in manual mode">
              <input value={f.fixedsize ?? ""} onChange={set("fixedsize")} inputMode="decimal"
                disabled={(f.sizingmode ?? "auto") !== "fixed"} />
            </Field>
            <Field label="Daily report hour (UTC)" hint="-1 disables the Telegram morning report">
              <input value={f.reporthour ?? ""} onChange={set("reporthour")} inputMode="numeric" />
            </Field>
          </div>
          <p className="dim" style={{ marginTop: 8 }}>Manual quantity still obeys per-trade exposure, total portfolio exposure, loss limits, position limits, and entry validation. It does not enable live trading.</p>
        </Card>
      </div>

      <div className="grid-2-eq">
        <Card title="Legacy Daily Loss Limit" subtitle="legacy worker only · auto-resets each UTC day">
          <div className="form-grid-2">
            <Field label="Max daily loss (%)" hint="0 = disabled; halts new entries for the day"><input value={f.daily ?? ""} onChange={set("daily")} inputMode="decimal" /></Field>
          </div>
          <p className="dim" style={{ marginTop: 8 }}>When today's realized loss exceeds this, new entries are blocked until the next UTC day. Open positions still exit.</p>
        </Card>

        <Card title="Legacy Trading Session (UTC)" subtitle="legacy worker only · entries only inside the window">
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginBottom: 10, flexWrap: "wrap" }}>
            {Object.keys(SESSIONS).map((s) => <button key={s} className="chip-btn" onClick={() => preset(s)}>{s}</button>)}
          </div>
          <div className="form-grid-2">
            <Field label="Session start (hour)"><input value={f.sstart ?? ""} onChange={set("sstart")} inputMode="numeric" /></Field>
            <Field label="Session end (hour)"><input value={f.send ?? ""} onChange={set("send")} inputMode="numeric" /></Field>
          </div>
          <p className="dim" style={{ marginTop: 8 }}>Presets are UTC; 0 to 24 = all day. Entries outside the window are skipped; exits are never blocked.</p>
        </Card>
      </div>

      <div className="grid-2-eq">
        <Card title="Legacy Pair Selection" subtitle="not used by Trading Instances">
          <div className="form-grid-2">
            <Field label="Trading pair mode">
              <select value={pairMode} onChange={(e) => setPairMode(e.target.value === "manual" ? "manual" : "auto")}>
                <option value="manual">manual — one selected pair</option>
                <option value="auto">automatic — watchlist pairs</option>
              </select>
            </Field>
            <Field label="Manual trading pair" hint="the only pair scanned and traded in manual mode">
              <input value={manualPair} onChange={(e) => setManualPair(e.target.value.toUpperCase())}
                disabled={pairMode !== "manual"} placeholder="BTCUSDT" />
            </Field>
          </div>
          <button className="btn btn-primary" style={{ marginTop: 8 }} onClick={applyPairSelection}><Icon name="check" size={14} /> Apply pair mode</button>
          <p className="dim" style={{ marginTop: 8 }}>Changing pair mode safely restarts the paper engine. Open paper positions remain managed; no live orders are possible.</p>
        </Card>

        <Card title="Legacy Automatic Watchlist" subtitle="not used by Trading Instances">
          <Field label="Traded symbols (comma-separated)"><input value={symbols} onChange={(e) => setSymbols(e.target.value.toUpperCase())} /></Field>
          <button className="btn btn-soft" style={{ marginTop: 8 }} onClick={applySymbols}><Icon name="check" size={14} /> Apply watchlist</button>
          <p className="dim" style={{ marginTop: 8 }}>Sets the auto-mode watchlist. In manual mode it is saved but not traded until you switch back to automatic.</p>
        </Card>

        <Card title="Legacy Engine Timeframe" subtitle="not used by Trading Instances">
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, flexWrap: "wrap" }}>
            {(["1m", "5m", "15m", "1h", "4h", "1d"] as const).map((tf) => (
              <button key={tf} className={`chip-btn ${engine.data?.timeframe === tf ? "active" : ""}`}
                onClick={async () => {
                  try {
                    await apiPost(`/engine/timeframe?timeframe=${tf}`);
                    app.toast(`Timeframe set to ${tf} — engine restarted`, "success");
                    engine.refetch();
                  } catch { app.toast("Change needs the webhook secret", "error"); }
                }}>{tf}</button>
            ))}
          </div>
          <p className="dim" style={{ marginTop: 8 }}>
            Candle interval the bot trades on. <b>4h</b> is the walk-forward-validated config;
            lower timeframes (1m–1h) give much faster signals/trades for testing and count as
            their own experiment. Bars appear after the first candle of the new timeframe closes.
          </p>
        </Card>

        <Card title="Legacy Allowed Trading Days (UTC)" subtitle="legacy worker only">
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, flexWrap: "wrap" }}>
            {DAY_NAMES.map((d, i) => (
              <button key={d} className={`chip-btn ${days[i] ? "active" : ""}`} onClick={() => setDays((p) => p.map((v, j) => (j === i ? !v : v)))}>{d}</button>
            ))}
          </div>
          <p className="dim" style={{ marginTop: 8 }}>Click to toggle. Disabled days block new entries (exits still run). Saved with the risk settings.</p>
        </Card>
      </div>

      <div className="grid-2-eq">
        <Card title="Legacy Loss Limits & Circuit Breakers" subtitle="legacy worker only · 0 = disabled">
          <div className="form-grid-2">
            <Field label="Weekly loss limit (%)" hint="resets each ISO week"><input value={f.weekly ?? ""} onChange={set("weekly")} inputMode="decimal" /></Field>
            <Field label="Max trades / day"><input value={f.maxday ?? ""} onChange={set("maxday")} inputMode="numeric" /></Field>
            <Field label="Stop after N consecutive losses" hint="auto-halts until Resume"><input value={f.consec ?? ""} onChange={set("consec")} inputMode="numeric" /></Field>
            <Field label="Cooldown after loss (min)"><input value={f.cooldown ?? ""} onChange={set("cooldown")} inputMode="numeric" /></Field>
          </div>
          <p className="dim" style={{ marginTop: 8 }}>These block NEW entries only; open positions always exit. Consecutive-loss halt requires a manual Resume.</p>
        </Card>

        <Card title="Account Protection — Progression" subtitle="paper instances remain simulation-only">
          <div className="risk-list">
            <Ro k="1. Backtest" v="any strategy (historical, isolated)" />
            <Ro k="2. Simulation" v="real historical data · labelled SIMULATION" />
            <Ro k="3. Paper trading" v="live engine · paper only" badge={<Badge text="ACTIVE" tone="blue" />} />
            <Ro k="4. Live trading" v="requires a live broker (not connected)" badge={<Badge text="LOCKED" tone="red" />} />
          </div>
          <p className="dim" style={{ marginTop: 8 }}>A new strategy can never trade live directly. Live execution is disabled until a broker is wired.</p>
        </Card>
      </div>

      </details>

      <div className="grid-2-eq">
        <Card title="Audit & Logs" subtitle="export the full trail">
          <p className="dim">Every settings change, strategy edit, deploy and engine event is recorded to the decision log.</p>
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginTop: 10 }}>
            <a className="btn btn-soft" href={`${API_BASE}/ledger/logs/export?fmt=csv`} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> Logs CSV</a>
            <a className="btn btn-soft" href={`${API_BASE}/paper/trades/export?fmt=csv`} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> Trades CSV</a>
          </div>
        </Card>
      </div>

      <div className="grid-2-eq">
        <Card title="Legacy Autonomous Engine" subtitle="read-only · stopped by default · not used by Trading Instances">
          {ro ? (
            <div className="risk-list">
              <Ro k="Mode" v={`${ro.mode} (simulation)`} badge={<Badge text="PAPER" tone="blue" />} />
              <Ro k="Strategy" v={`${ro.strategy} (${ro.strategy_key})`} />
              <Ro k="Timeframe" v={ro.timeframe} />
              <Ro k="Symbols" v={ro.symbols.join(", ")} />
              <Ro k="Starting balance" v={`$${ro.starting_cash.toLocaleString()}`} />
            </div>
          ) : <div className="dim">Loading…</div>}
          <p className="dim" style={{ marginTop: 8 }}>
            Set via env: HUB_AUTO_STRATEGY, HUB_AUTO_SYMBOLS, HUB_AUTO_TIMEFRAME (restart to change).
          </p>
        </Card>

        <Card title="Data & Connections" subtitle="read-only">
          {ro ? (
            <div className="risk-list">
              <Ro k="Market data" v={ro.data_source} />
              {ro.poll_seconds != null && <Ro k="Poll interval" v={`${ro.poll_seconds}s`} />}
              <Ro k="Broker" v={ro.broker_connected ? "connected" : "not connected"} badge={<Badge text={ro.broker_connected ? "LIVE" : "NONE"} tone={ro.broker_connected ? "green" : "default"} />} />
              <Ro k="Webhook secret" v={ro.webhook_secret_set ? "configured" : "not set"} />
              <Ro k="Telegram alerts" v={ro.telegram_configured ? "configured" : "not configured"} />
            </div>
          ) : <div className="dim">Loading…</div>}
          <p className="dim" style={{ marginTop: 8 }}>
            Live data: HUB_USE_LIVE_DATA=1 (ccxt). Notifications: TELEGRAM_BOT_TOKEN.
          </p>
        </Card>
      </div>
    </>
  );
}

type InstancePlatform = {
  max_active_slots: number; max_global_risk_pct: number; max_global_daily_loss_pct: number;
  paper_account_capital: number; total_allocated_capital: number; available_paper_capital: number;
};

export function InstancePlatformCard() {
  const app = useApp();
  const platform = useLive<InstancePlatform>("/instances", 5000);
  const [form, setForm] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (platform.data && Object.keys(form).length === 0) setForm({
      slots: String(platform.data.max_active_slots),
      risk: String(platform.data.max_global_risk_pct * 100),
      daily: String(platform.data.max_global_daily_loss_pct * 100),
      capital: String(platform.data.paper_account_capital),
    });
  }, [platform.data, form]);
  const save = async () => {
    setSaving(true);
    try {
      await apiPostJson("/instances/platform", {
        max_active_slots: Number(form.slots), max_global_risk_pct: Number(form.risk) / 100,
        max_global_daily_loss_pct: Number(form.daily) / 100, paper_account_capital: Number(form.capital),
      });
      platform.refetch(); app.toast("Global Paper Trading limits saved", "success");
    } catch (error) { app.toast(error instanceof Error ? error.message : "Could not save global limits", "error"); }
    finally { setSaving(false); }
  };
  return <Card title="Trading Instance Global Risk Boundary" subtitle="applies above all Paper Trading instances; individual pair and strategy controls are in each instance">
    <div className="form-grid-2">
      <Field label="Paper account capital ($)"><input value={form.capital ?? ""} onChange={(e) => setForm({ ...form, capital: e.target.value })} inputMode="decimal" /></Field>
      <Field label="Maximum active instances"><select value={form.slots ?? "1"} onChange={(e) => setForm({ ...form, slots: e.target.value })}><option value="1">1</option><option value="2">2 (recommended)</option><option value="3">3</option></select></Field>
      <Field label="Global open-risk limit (%)"><input value={form.risk ?? ""} onChange={(e) => setForm({ ...form, risk: e.target.value })} inputMode="decimal" /></Field>
      <Field label="Global daily-loss limit (%)"><input value={form.daily ?? ""} onChange={(e) => setForm({ ...form, daily: e.target.value })} inputMode="decimal" /></Field>
    </div>
    <div className="risk-list" style={{ marginTop: 10 }}>
      <Ro k="Allocated / available" v={`$${(platform.data?.total_allocated_capital ?? 0).toLocaleString()} / $${(platform.data?.available_paper_capital ?? 0).toLocaleString()}`} />
      <Ro k="Active configuration" v="Trading Instances are authoritative" badge={<Badge text="INSTANCE-OWNED" tone="green" />} />
    </div>
    <button className="btn btn-primary" style={{ marginTop: 10 }} disabled={saving || !platform.data} onClick={() => void save()}><Icon name="check" size={14} /> {saving ? "Saving…" : "Save global limits"}</button>
  </Card>;
}

export function WorkspaceCard() {
  const app = useApp();
  const [busy, setBusy] = useState(false);
  const reset = async () => {
    if (!window.confirm("Reset saved dashboard preferences (filters, chart timeframes, layout state)? Trades, journal and memory are NOT touched.")) return;
    setBusy(true);
    const { resetDashboardPrefs } = await import("../lib/prefs");
    await resetDashboardPrefs();
    setBusy(false);
    app.toast("Dashboard preferences reset — defaults restored.", "success");
  };
  return (
    <Card title="Workspace" subtitle="per-user preferences · saved to your account on every change">
      <p className="dim" style={{ fontSize: 12.5, marginBottom: 10 }}>
        Filters, chart timeframes and view preferences are saved to your account as you change
        them, and restored on every login. Nothing resets unless you ask it to.
      </p>
      <button className="btn btn-warn" disabled={busy} onClick={() => void reset()}>
        <Icon name="refresh" size={13} /> {busy ? "…" : "Reset dashboard preferences"}
      </button>
    </Card>
  );
}

export function AccountCard() {
  const app = useApp();
  const auth = useLive<{ authenticated: boolean; user: string | null; signup_open: boolean }>("/auth/status", 30000);
  const [pw, setPw] = useState({ current: "", next: "", confirm: "" });
  const [busy, setBusy] = useState(false);

  const logout = async () => {
    try { await fetch(`${API_BASE}/auth/logout`, { method: "POST" }); } catch { /* ignore */ }
    window.location.href = "/login";
  };
  const changePw = async () => {
    if (pw.next.length < 8) { app.toast("New password must be 8+ characters", "error"); return; }
    if (pw.next !== pw.confirm) { app.toast("Passwords do not match", "error"); return; }
    setBusy(true);
    try {
      const res = await fetch(`${API_BASE}/auth/change-password`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current: pw.current, new: pw.next }),
      });
      const body = await res.json();
      if (!res.ok || body.error) app.toast(body.error ?? "Change failed", "error");
      else { app.toast("Password changed ✅", "success"); setPw({ current: "", next: "", confirm: "" }); }
    } catch { app.toast("Change failed — backend unreachable?", "error"); }
    finally { setBusy(false); }
  };

  return (
    <Card title="TradeLogX Nexus Account" subtitle="who is signed in · change password · sign out"
      right={<button className="btn btn-danger" onClick={logout}><Icon name="close" size={13} /> Log out</button>}>
      <div className="risk-list" style={{ marginBottom: 10 }}>
        <div className="risk-item"><span className="dim">Signed in as</span>
          <b>{auth.data?.user ?? (auth.data?.authenticated === false ? "not signed in" : "…")}</b></div>
        <div className="risk-item"><span className="dim">Sessions</span>
          <span className="dim" style={{ fontSize: 12 }}>signed cookies · valid 7 days · survive restarts</span></div>
      </div>
      <div className="form-grid-2">
        <Field label="Current password"><input type="password" value={pw.current}
          onChange={(e) => setPw((s) => ({ ...s, current: e.target.value }))} /></Field>
        <Field label="New password (8+)"><input type="password" value={pw.next}
          onChange={(e) => setPw((s) => ({ ...s, next: e.target.value }))} /></Field>
        <Field label="Confirm new password"><input type="password" value={pw.confirm}
          onChange={(e) => setPw((s) => ({ ...s, confirm: e.target.value }))} /></Field>
      </div>
      <div className="row-actions" style={{ justifyContent: "flex-start", marginTop: 8 }}>
        <button className="btn btn-soft" disabled={busy} onClick={changePw}>
          {busy ? "Changing…" : "Change password"}</button>
      </div>
    </Card>
  );
}

function Ro({ k, v, badge }: { k: string; v: string; badge?: ReactNode }) {
  return (
    <div className="risk-item"><div className="risk-head">
      <span className="dim">{k}</span>
      <b style={{ display: "flex", alignItems: "center", gap: 6 }}>{v}{badge}</b>
    </div></div>
  );
}
