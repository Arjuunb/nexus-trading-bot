import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import Icon from "../common/Icon";
import { useApp } from "../../app-context";
import { apiPatchJson, apiPost, useLive } from "../../lib/api";

type StrategyOption = { key: string; label: string; versions: string[] };
type Instance = {
  id: string; symbol: string; strategy_key: string; strategy_label: string; strategy_version: string;
  timeframe: string; state: string; mode: "trading" | "research"; last_error?: string;
  ui_status?: "RUNNING_UNARMED" | "RUNNING_ARMED" | "BLOCKED" | "ERROR";
  risk_per_trade_pct: number; capital_allocation: number; max_open_positions?: number;
  sizing_mode?: string; entry_mode?: string; fill_model?: string;
  current_position?: unknown;
  engine?: { last_heartbeat?: string; lifecycle_state?: string; last_closed_candle?: string } | null;
  market_data?: { market_data_status?: string; data_source?: string; market_data_mode?: string; last_market_data_timestamp?: string };
};
type InstanceSnapshot = {
  instances: Instance[]; active_slots: number; max_active_slots: number;
  total_current_equity?: number; paper_account_capital?: number; available_paper_capital?: number;
  current_global_risk_amount?: number; max_global_risk_amount?: number; total_open_positions?: number;
  global_risk_status?: string; global_risk_message?: string; market_data_status?: string;
};
type Options = {
  strategies: StrategyOption[]; timeframes: string[];
  execution_defaults?: { max_open_positions?: number; max_quick_risk_pct?: number };
};
type Menu = "mode" | "instance" | "strategy" | "timeframe" | "risk" | "allocation" | "max" | "engine" | "controls" | "more" | null;
type Command = { label: string; hint?: string; run: () => void | Promise<void> };
const ACTIVE_INSTANCE_STATES = new Set(["starting", "bootstrapping", "warming", "syncing", "ready", "running", "data_stale", "recovering", "paused"]);

const money = (value?: number) => `$${Number(value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const shortMoney = (value?: number) => Number(value ?? 0) >= 1000 ? `$${(Number(value) / 1000).toFixed(Number(value) % 1000 ? 1 : 0)}K` : money(value);
const percent = (value?: number) => `${(Number(value ?? 0) * 100).toFixed(2)}%`;

function Chip({ children, menu, open, setOpen, className = "" }: {
  children: ReactNode; menu: Exclude<Menu, null>; open: Menu; setOpen: (menu: Menu) => void; className?: string;
}) {
  const active = open === menu;
  return <button type="button" className={`hdr-chip ${className} ${active ? "open" : ""}`}
    aria-haspopup="menu" aria-expanded={active} onClick={() => setOpen(active ? null : menu)}>
    {children}<Icon name="chevron" size={9} className="dim hdr-caret" />
  </button>;
}

function Popover({ children, wide = false, label }: { children: ReactNode; wide?: boolean; label: string }) {
  return <div className={`hdr-pop ${wide ? "hdr-pop-wide" : ""}`} role="menu" aria-label={label}>{children}</div>;
}

/** Dense, server-authoritative quick controls for the selected Trading Instance. */
export default function HeaderControls() {
  const app = useApp();
  const live = useLive<InstanceSnapshot>("/instances", 2500);
  const options = useLive<Options>("/instances/options", 30000);
  const root = useRef<HTMLDivElement | null>(null);
  const commandInput = useRef<HTMLInputElement | null>(null);
  const operationInFlight = useRef(false);
  const [open, setOpen] = useState<Menu>(null);
  const [palette, setPalette] = useState(false);
  const [query, setQuery] = useState("");
  const [customRisk, setCustomRisk] = useState("0.50");
  const [allocation, setAllocation] = useState("");
  const [maxPositions, setMaxPositions] = useState("");
  const [busy, setBusy] = useState(false);
  const rows = live.data?.instances ?? [];
  const selected = rows.find((row) => row.id === app.selectedInstanceId)
    ?? rows.find((row) => ACTIVE_INSTANCE_STATES.has(row.state)) ?? rows[0];

  useEffect(() => {
    if (selected && selected.id !== app.selectedInstanceId) app.selectInstance(selected.id);
  }, [selected?.id, app.selectedInstanceId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (selected) {
      setCustomRisk((selected.risk_per_trade_pct * 100).toFixed(2));
      setAllocation(String(selected.capital_allocation));
      setMaxPositions(String(selected.max_open_positions ?? 3));
    }
  }, [selected?.id, selected?.risk_per_trade_pct, selected?.capital_allocation, selected?.max_open_positions]);

  useEffect(() => {
    const outside = (event: MouseEvent) => { if (root.current && !root.current.contains(event.target as Node)) setOpen(null); };
    const keyboard = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); setPalette(true); setOpen(null);
      } else if (event.key === "Escape") { setOpen(null); setPalette(false); }
    };
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", keyboard);
    return () => { document.removeEventListener("mousedown", outside); document.removeEventListener("keydown", keyboard); };
  }, []);

  useEffect(() => { if (palette) window.setTimeout(() => commandInput.current?.focus(), 0); }, [palette]);

  const update = async (body: Record<string, unknown>, message: string) => {
    if (!selected || operationInFlight.current) return false;
    operationInFlight.current = true;
    setBusy(true);
    try {
      await apiPatchJson(`/instances/${selected.id}`, body);
      const refreshed = await live.refetch();
      setOpen(null);
      app.toast(refreshed ? message : `${message}; live status refresh is reconnecting`, refreshed ? "success" : "info");
      return true;
    } catch (error) {
      app.toast(error instanceof Error ? error.message : "Update failed", "error");
      return false;
    } finally {
      operationInFlight.current = false;
      setBusy(false);
    }
  };
  const changeExecution = async (body: Record<string, unknown>, message: string) => {
    if (!selected) return;
    const active = ["running", "ready", "paused", "recovering", "data_stale", "starting", "bootstrapping", "warming", "syncing"].includes(selected.state);
    if (active && !window.confirm(`${message} requires a safe engine restart. Continue?`)) return;
    await update(body, `${message} applied`);
  };
  const lifecycle = async (action: "start" | "pause" | "resume" | "restart" | "stop") => {
    if (!selected || operationInFlight.current) return;
    operationInFlight.current = true;
    setBusy(true);
    try {
      await apiPost(`/instances/${selected.id}/${action}`);
      const refreshed = await live.refetch();
      setOpen(null);
      app.toast(refreshed ? `Instance ${action} successful` : `Instance ${action} accepted; live status refresh is reconnecting`, refreshed ? "success" : "info");
    } catch (error) {
      app.toast(error instanceof Error ? error.message : `${action} failed`, "error");
    } finally {
      operationInFlight.current = false;
      setBusy(false);
    }
  };

  const market = selected?.market_data?.market_data_status ?? "not available";
  const riskBlocked = Boolean(live.data?.global_risk_status && live.data.global_risk_status !== "healthy");
  const dataDegraded = ["error", "disconnected", "stale"].includes(market);
  const engineState = !selected ? "BLOCKED" : riskBlocked || dataDegraded ? "BLOCKED" : selected.ui_status ?? "BLOCKED";
  const stateDot = engineState === "RUNNING_ARMED" ? "online" : engineState === "ERROR" ? "offline" : "warn";
  const modeLabel = selected?.mode === "research" ? "RESEARCH" : "PAPER";
  const independentLabPage = /#\/(price-action-lab|smc-strategy-lab)(?:\?|$)/.test(window.location.hash);
  const strategyVersion = selected ? `${selected.strategy_label} ${selected.strategy_version}` : "Strategy";
  const maxQuickRisk = (options.data?.execution_defaults?.max_quick_risk_pct ?? 0.01) * 100;

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [];
    rows.forEach((row) => list.push({ label: `Switch ${row.symbol}`, hint: `${row.strategy_label} · ${row.timeframe} · ${row.state}`, run: () => app.selectInstance(row.id) }));
    (options.data?.strategies ?? []).forEach((strategy) => strategy.versions.forEach((version) => list.push({ label: `Use ${strategy.label}`, hint: version, run: () => changeExecution({ strategy: strategy.key, strategy_version: version }, `Strategy change to ${strategy.label} ${version}`) })));
    (options.data?.timeframes ?? []).forEach((timeframe) => list.push({ label: `Set timeframe ${timeframe}`, run: () => changeExecution({ timeframe }, `Timeframe change to ${timeframe}`) }));
    [0.0025, 0.005, 0.0075, 0.01].forEach((risk) => list.push({
      label: `Set risk ${(risk * 100).toFixed(2)}%`,
      run: async () => { await update({ risk_per_trade_pct: risk }, "Risk updated"); },
    }));
    list.push(
      { label: "Pause instance", run: () => lifecycle("pause") },
      { label: "Resume instance", run: () => lifecycle("resume") },
      { label: "Restart instance", run: () => lifecycle("restart") },
      { label: "Open Risk Manager", run: () => app.go("Risk Manager") },
      { label: "Open Logs", run: () => app.go("Logs") },
      { label: "Open Trading Instances", run: () => app.go("Trading Instances") },
      { label: "Open Strategy Studio", run: () => app.go("Strategy Studio") },
    );
    return list;
  }, [rows, options.data?.strategies, options.data?.timeframes, selected?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const filteredCommands = commands.filter((command) => `${command.label} ${command.hint ?? ""}`.toLowerCase().includes(query.toLowerCase())).slice(0, 12);
  const runCommand = async (command: Command) => { setPalette(false); setQuery(""); await command.run(); };

  return <>
    <div className="hdr-controls" ref={root}
      aria-label={independentLabPage ? "Global Trading Instance controls; independent lab status is shown in the page" : "Quick trading controls"}
      aria-busy={busy}>
      <div className="hdr-seg">
        <Chip menu="mode" open={open} setOpen={setOpen}><span className="dot online" /><b>{modeLabel}</b></Chip>
        {open === "mode" && <Popover label="Trading mode">
          <button className="hdr-item active" onClick={() => { setOpen(null); app.go("Dashboard"); }}><span className="dot online" /><b>Paper</b><span className="hdr-tag">current</span></button>
          <button className="hdr-item" onClick={() => { setOpen(null); app.go("Replay"); }}><span className="dot warn" /><b>Research / Replay</b></button>
          <button className="hdr-item" onClick={() => { setOpen(null); app.go("Live Trading"); app.toast("Live activation remains protected by Safety Center confirmation", "info"); }}><span className="dot offline" /><b>Live</b><span className="hdr-tag">gated</span></button>
        </Popover>}
      </div>

      <div className="hdr-seg">
        <Chip menu="instance" open={open} setOpen={setOpen}><b className="mono">{selected?.symbol ?? "No instance"}</b></Chip>
        {open === "instance" && <Popover wide label="Select dashboard instance">
          <p className="hdr-pop-title">Dashboard context</p>
          {rows.map((row) => <button key={row.id} className={`hdr-item ${row.id === selected?.id ? "active" : ""}`} onClick={() => { app.selectInstance(row.id); setOpen(null); }}>
            <span className={`dot ${row.state === "running" ? "online" : row.state === "paused" ? "warn" : "offline"}`} />
            <span className="hdr-instance-copy"><b>{row.symbol}</b><small>{row.strategy_label} {row.strategy_version} · {row.timeframe} · {row.state}</small></span>
          </button>)}
          {!rows.length && <p className="hdr-note">No Trading Instances exist yet.</p>}
        </Popover>}
      </div>

      <div className="hdr-seg quick-strategy">
        <Chip menu="strategy" open={open} setOpen={setOpen}><b>{strategyVersion}</b></Chip>
        {open === "strategy" && <Popover wide label="Instance strategy">
          <p className="hdr-pop-title">Strategy · change safely restarts engine</p>
          {(options.data?.strategies ?? []).flatMap((strategy) => strategy.versions.map((version) => {
            const active = strategy.key === selected?.strategy_key && version === selected?.strategy_version;
            return <button key={`${strategy.key}-${version}`} disabled={busy} className={`hdr-item ${active ? "active" : ""}`}
              onClick={() => active ? setOpen(null) : void changeExecution({ strategy: strategy.key, strategy_version: version }, `Strategy change to ${strategy.label} ${version}`)}>
              <b>{strategy.label} {version}</b>{active && <span className="hdr-tag">active</span>}
            </button>;
          }))}
        </Popover>}
      </div>

      <div className="hdr-seg">
        <Chip menu="timeframe" open={open} setOpen={setOpen}><b className="mono">{selected?.timeframe ?? "—"}</b></Chip>
        {open === "timeframe" && <Popover label="Execution timeframe">
          <p className="hdr-pop-title">Execution timeframe · restart + warm-up</p>
          <div className="tf-grid">{(options.data?.timeframes ?? []).map((timeframe) => <button key={timeframe} disabled={busy} className={`tf-btn ${timeframe === selected?.timeframe ? "active" : ""}`}
            onClick={() => timeframe === selected?.timeframe ? setOpen(null) : void changeExecution({ timeframe }, `Timeframe change to ${timeframe}`)}>{timeframe}</button>)}</div>
        </Popover>}
      </div>

      <div className="hdr-seg quick-risk">
        <Chip menu="risk" open={open} setOpen={setOpen}><span className="dim">Risk</span><b>{percent(selected?.risk_per_trade_pct)}</b></Chip>
        {open === "risk" && <Popover label="Risk per trade">
          <p className="hdr-pop-title">Risk per trade</p>
          <div className="hdr-choice-row">{[0.0025, 0.005, 0.0075, 0.01].map((risk) => <button key={risk} className={`tf-btn ${risk === selected?.risk_per_trade_pct ? "active" : ""}`} onClick={() => void update({ risk_per_trade_pct: risk }, "Risk updated")}>{percent(risk)}</button>)}</div>
          <form className="hdr-inline hdr-quick-form" onSubmit={(event) => { event.preventDefault(); const value = Number(customRisk); if (value > 0 && value <= maxQuickRisk) void update({ risk_per_trade_pct: value / 100 }, "Custom risk updated"); else app.toast(`Risk must be above 0 and at most ${maxQuickRisk.toFixed(2)}%`, "error"); }}>
            <input aria-label="Custom risk percent" type="number" min="0.01" max={maxQuickRisk} step="0.01" value={customRisk} onChange={(event) => setCustomRisk(event.target.value)} /><span>%</span><button className="btn btn-sm btn-soft" type="submit">Apply</button>
          </form>
          <div className="hdr-pop-sep" /><div className="hdr-kv"><span>Max quick risk</span><b>{maxQuickRisk.toFixed(2)}%</b><span>Risk Manager</span><b className={riskBlocked ? "neg" : "pos"}>{live.data?.global_risk_status ?? "checking"}</b></div>
        </Popover>}
      </div>

      <div className="hdr-seg quick-secondary quick-allocation">
        <Chip menu="allocation" open={open} setOpen={setOpen}><span className="dim alloc-label">Alloc</span><b>{shortMoney(selected?.capital_allocation)}</b></Chip>
        {open === "allocation" && <Popover label="Capital allocation">
          <p className="hdr-pop-title">Paper allocation</p><div className="hdr-kv"><span>Paper capital</span><b>{money(live.data?.paper_account_capital)}</b><span>Allocated</span><b>{money(selected?.capital_allocation)}</b><span>Account available</span><b>{money(live.data?.available_paper_capital)}</b></div>
          <form className="hdr-inline hdr-quick-form" onSubmit={(event) => { event.preventDefault(); void changeExecution({ capital_allocation: Number(allocation) }, "Allocation change"); }}><input aria-label="Capital allocation" type="number" min="1" step="100" value={allocation} onChange={(event) => setAllocation(event.target.value)} /><button className="btn btn-sm btn-soft" type="submit">Apply</button></form>
        </Popover>}
      </div>

      <div className="hdr-seg quick-secondary quick-max">
        <Chip menu="max" open={open} setOpen={setOpen}><span className="dim">Max</span><b>{selected?.max_open_positions ?? 3}</b></Chip>
        {open === "max" && <Popover label="Maximum positions">
          <p className="hdr-pop-title">Position constraint</p><div className="hdr-kv"><span>Maximum positions</span><b>{selected?.max_open_positions ?? 3}</b><span>Currently open</span><b>{selected?.current_position ? 1 : 0}</b><span>Global Risk Manager</span><b className={riskBlocked ? "neg" : "pos"}>{live.data?.global_risk_status ?? "checking"}</b></div>
          <form className="hdr-inline hdr-quick-form" onSubmit={(event) => { event.preventDefault(); void update({ max_open_positions: Number(maxPositions) }, "Position cap updated"); }}><input aria-label="Maximum positions" type="number" min="1" max="50" step="1" value={maxPositions} onChange={(event) => setMaxPositions(event.target.value)} /><button className="btn btn-sm btn-soft" type="submit">Apply</button></form>
        </Popover>}
      </div>

      <div className="hdr-seg quick-engine">
        <Chip menu="engine" open={open} setOpen={setOpen}><span className={`dot ${stateDot}`} /><b className="state-label">{independentLabPage ? `Global ${engineState}` : engineState}</b></Chip>
        {open === "engine" && <Popover label="Engine lifecycle">
          <p className="hdr-pop-title">Actual worker lifecycle</p>
          <button className="hdr-item" disabled={busy || !selected || !["stopped", "error", "created"].includes(selected.state)} onClick={() => void lifecycle("start")}><b>Start</b></button>
          <button className="hdr-item" disabled={busy || selected?.state !== "running"} onClick={() => void lifecycle("pause")}><b>Pause</b></button>
          <button className="hdr-item" disabled={busy || selected?.state !== "paused"} onClick={() => void lifecycle("resume")}><b>Resume</b></button>
          <button className="hdr-item" disabled={busy || !selected} onClick={() => void lifecycle("restart")}><b>Restart</b></button>
          <button className="hdr-item danger" disabled={busy || !selected || selected.state === "stopped"} onClick={() => void lifecycle("stop")}><b>Stop</b></button>
          {busy && <p className="hdr-note">Applying and confirming server state…</p>}
          {selected?.last_error && <p className="hdr-note neg">{selected.last_error}</p>}
        </Popover>}
      </div>

      <div className="hdr-seg quick-more">
        <Chip menu="more" open={open} setOpen={setOpen}><b>•••</b></Chip>
        {open === "more" && <Popover wide label="More quick controls">
          <p className="hdr-pop-title">Responsive quick controls</p>
          <form className="hdr-overflow-form more-risk" onSubmit={(event) => { event.preventDefault(); const value = Number(customRisk); if (value > 0 && value <= maxQuickRisk) void update({ risk_per_trade_pct: value / 100 }, "Risk updated"); else app.toast(`Risk must be above 0 and at most ${maxQuickRisk.toFixed(2)}%`, "error"); }}><label>Risk %</label><input aria-label="Overflow risk percent" type="number" min="0.01" max={maxQuickRisk} step="0.01" value={customRisk} onChange={(event) => setCustomRisk(event.target.value)} /><button className="btn btn-sm btn-soft">Apply</button></form>
          <form className="hdr-overflow-form" onSubmit={(event) => { event.preventDefault(); void changeExecution({ capital_allocation: Number(allocation) }, "Allocation change"); }}><label>Allocation</label><input aria-label="Overflow allocation" type="number" min="1" step="100" value={allocation} onChange={(event) => setAllocation(event.target.value)} /><button className="btn btn-sm btn-soft">Apply</button></form>
          <form className="hdr-overflow-form" onSubmit={(event) => { event.preventDefault(); void update({ max_open_positions: Number(maxPositions) }, "Position cap updated"); }}><label>Max positions</label><input aria-label="Overflow maximum positions" type="number" min="1" max="50" step="1" value={maxPositions} onChange={(event) => setMaxPositions(event.target.value)} /><button className="btn btn-sm btn-soft">Apply</button></form>
        </Popover>}
      </div>

      <div className="hdr-seg quick-controls">
        <button type="button" className={`hdr-chip ${open === "controls" ? "open" : ""}`} aria-haspopup="menu" aria-expanded={open === "controls"} onClick={() => setOpen(open === "controls" ? null : "controls")}><Icon name="settings" size={12} /><b>Controls</b></button>
        {open === "controls" && <Popover wide label="Trading controls summary">
          <p className="hdr-sect">Execution</p><div className="hdr-kv"><span>Instance</span><b>{selected?.symbol ?? "—"}</b><span>Strategy</span><b>{strategyVersion}</b><span>Timeframe</span><b>{selected?.timeframe ?? "—"}</b></div>
          <p className="hdr-sect">Risk</p><div className="hdr-kv"><span>Per trade</span><b>{percent(selected?.risk_per_trade_pct)}</b><span>Max positions</span><b>{selected?.max_open_positions ?? 3}</b><span>Open risk</span><b>{money(live.data?.current_global_risk_amount)} / {money(live.data?.max_global_risk_amount)}</b></div>
          <p className="hdr-sect">Market data</p><div className="hdr-kv"><span>Status</span><b>{market}</b><span>Source</span><b>{selected?.market_data?.data_source ?? "—"}</b><span>Mode</span><b>{selected?.market_data?.market_data_mode ?? "—"}</b><span>Last candle</span><b>{selected?.market_data?.last_market_data_timestamp ? new Date(selected.market_data.last_market_data_timestamp).toLocaleTimeString() : "—"}</b></div>
          <p className="hdr-sect">Engine & safety</p><div className="hdr-kv"><span>State</span><b>{engineState}</b><span>Heartbeat</span><b>{selected?.engine?.last_heartbeat ? new Date(selected.engine.last_heartbeat).toLocaleTimeString() : "—"}</b><span>Risk Manager</span><b>{live.data?.global_risk_status ?? "checking"}</b><span>Safety message</span><b>{live.data?.global_risk_message ?? "Checking server policy"}</b></div>
          <button className="link-row" onClick={() => { setOpen(null); app.go("Settings"); }}>Open full settings →</button>
        </Popover>}
      </div>

      <button type="button" className="hdr-chip command-trigger" aria-label="Open command palette" onClick={() => { setPalette(true); setOpen(null); }}><b>⌘K</b></button>
    </div>

    {palette && createPortal(<div className="command-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPalette(false); }}>
      <div className="command-palette" role="dialog" aria-modal="true" aria-label="Trading command palette">
        <div className="command-search"><span>⌕</span><input ref={commandInput} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && filteredCommands[0]) { event.preventDefault(); void runCommand(filteredCommands[0]); } }} placeholder="Switch instance, strategy, timeframe, risk…" /></div>
        <div className="command-list">{filteredCommands.map((command) => <button key={`${command.label}-${command.hint ?? ""}`} onClick={() => void runCommand(command)}><b>{command.label}</b>{command.hint && <span>{command.hint}</span>}</button>)}{!filteredCommands.length && <p className="hdr-note">No matching command.</p>}</div>
        <div className="command-foot"><span>Enter to run</span><span>Esc to close</span></div>
      </div>
    </div>, document.body)}
  </>;
}
