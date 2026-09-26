import { useEffect, useMemo, useRef, useState } from "react";
import AreaLine from "../components/chart/AreaLine";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, Field, PageHeader, StatCard } from "../components/common/ui";
import { apiDelete, apiGet, apiPatchJson, apiPost, apiPostJson, useLive } from "../lib/api";
import { useApp } from "../app-context";
import NewsGuardPanel from "../components/instances/NewsGuardPanel";
import PublicRecordPanel from "../components/instances/PublicRecordPanel";
import QualityGatePanel from "../components/instances/QualityGatePanel";

type Metric = Record<string, any>;
type MarketData = { market_data_mode?: string; market_data_status?: string; last_market_data_timestamp?: string; last_processed_candle_timestamp?: string; market_data_age_seconds?: number | null; warmup_bars?: number; duplicate_candles?: number; missing_candles?: number; out_of_order_candles?: number; reconnect_attempt?: number; data_source?: string; freshness_thresholds_seconds?: { healthy_under: number; disconnected_over: number } };
type StatusAxes = {
  runtime_status?: "RUNNING" | "STARTING" | "PAUSED" | "STOPPED" | "BLOCKED" | "ERROR";
  market_status?: "LIVE" | "CONNECTING" | "SYNCHRONIZING" | "STALE" | "DISCONNECTED" | "RECONNECTING" | "FAILED" | "WAITING_FOR_DATA";
  market_status_reason?: string;
  strategy_status?: "READY" | "WARMING_UP" | "WAITING_FOR_DATA" | "WAITING_FOR_HTF" | "WAITING_FOR_SETUP" | "BLOCKED" | "ERROR";
  strategy_status_reason?: string;
  execution_status?: "FORWARD_PAPER" | "SIGNALS_ONLY" | "DISABLED";
  execution_status_reason?: string;
  current_blocker?: string | null;
  strategy_lifecycle?: string;
  feed?: Feed;
  subscription?: Subscription;
  worker?: WorkerState;
  configuration_revision?: ConfigRevision;
};
type Feed = { exchange?: string; market_type?: string; symbol?: string; execution_timeframe?: string; htf_primary_timeframe?: string | null; htf_secondary_timeframe?: string | null; last_trade_price?: number | null; bid?: number | null; ask?: number | null; mark_price?: number | null; last_closed_candle_timestamp?: string | null; last_processed_candle_timestamp?: string | null; last_websocket_message_timestamp?: string | null; last_quote_timestamp?: string | null; data_age_seconds?: number | null; quote_age_seconds?: number | null; data_source?: string | null; warmup_bars?: number | null; warmup_required?: number | null; duplicate_candles?: number | null; missing_candles?: number | null; out_of_order_candles?: number | null };
type Subscription = { consumer_id?: string | null; channel?: string | null; state?: string | null; transport_state?: string | null; transport_channels?: Record<string, string> | null; reliable?: boolean | null; health_reason?: string | null; failing_dependency?: string | null; reconnect_attempts?: number | null; pending_candle_ids?: string[] | null };
type WorkerState = { alive?: boolean; lifecycle_state?: string; worker_id?: string | null; last_heartbeat?: string | null; persisted_heartbeat?: string | null; last_transition?: string | null; uptime_seconds?: number | null; engine_reconnect_attempt?: number | null; engine_max_reconnect_attempts?: number | null; engine_reconnect_next_at?: string | null };
type ConfigRevision = { configured?: number; running?: number | null; stale?: boolean };
type RebootState = { id: string; status: "running" | "completed" | "degraded" | "failed"; phase: string; message: string; started_at: string; updated_at: string; completed_at?: string | null; error?: string; details?: Metric };
type Instance = { id: string; symbol: string; strategy_key: string; strategy_label: string; strategy_version: string; timeframe: string; risk_per_trade_pct: number; capital_allocation: number; exchange?: string; effective_exchange?: string; instrument_type?: string; max_open_positions?: number; sizing_mode?: string; fixed_position_size?: number; fixed_quantity?: number; profit_reinvestment?: boolean; maximum_risk_amount?: number | null; minimum_equity?: number | null; starting_equity?: number; current_realized_equity?: number; entry_mode?: string; fill_model?: string; execution_mode?: string; market_data_mode?: string; mode: string; state: string; ui_status?: "RUNNING_UNARMED" | "RUNNING_ARMED" | "BLOCKED" | "ERROR"; created_at: string; started_at?: string | null; stopped_at?: string | null; last_error?: string; last_blocker?: string; metrics: Metric; performance?: Metric; execution?: Metric; risk?: Metric; engine?: Metric | null; mtf_policy?: { label?: string } | null; worker_counts?: { signals: number; accepted: number; rejections: number }; strategy_identity?: { configured_id?: string; configured_label?: string; worker_label?: string | null; matches?: boolean }; market_data?: MarketData; current_position?: Metric | null; strategy_health?: Metric | null; last_decision?: Metric | null; reboot?: RebootState | null } & StatusAxes;
type InstancesResponse = { instances: Instance[]; max_active_slots: number; active_slots: number; total_instances?: number; max_global_risk_pct: number; max_global_risk_amount: number; current_global_risk_amount: number; paper_account_capital?: number; total_allocated_capital?: number; total_current_equity?: number; available_paper_capital?: number; today_pnl?: number; today_trades?: number; total_open_positions?: number; global_risk_status?: string; market_data_status?: string; global_status?: string; instance_counts?: Record<string, number> };
type Options = { symbols: string[]; timeframes: string[]; strategies: { key: string; label: string; versions: string[]; status?: string; required_data?: string[]; supported_markets?: string[]; supported_timeframes?: string[] }[]; strategy_registry?: { strategy_id: string; display_name: string; status: string; lifecycle_reason?: string; version?: string }[]; execution_defaults: { position_sizing_mode?: string; entry_mode?: string; fill_model?: string; exchange?: string; instrument_type?: string; leverage?: number | null; max_open_positions?: number; symbol?: string; timeframe?: string; strategy?: string; capital?: number; risk_per_trade_pct?: number }; exchanges?: { key: string; label: string }[]; sizing_modes?: { key: string; label: string; implemented: boolean }[]; fill_models?: { key: string; label: string; recommended?: boolean }[]; market_data_mode: string };

const noValue = (value: unknown) => value === undefined || value === null || value === "";
const money = (value: unknown) => noValue(value) ? "—" : new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(value));
const signedMoney = (value: unknown) => noValue(value) ? "—" : `${Number(value) > 0 ? "+" : ""}${money(value)}`;
const pct = (value: unknown, digits = 2) => noValue(value) ? "—" : `${Number(value).toFixed(digits)}%`;
const number = (value: unknown, digits = 2) => noValue(value) ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
const timestamp = (value: unknown) => noValue(value) ? "—" : new Date(String(value)).toLocaleString();
const duration = (seconds: unknown) => {
  if (noValue(seconds)) return "—";
  const n = Math.max(0, Number(seconds));
  if (n < 60) return `${Math.floor(n)}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m`;
  return `${Math.floor(n / 3600)}h ${Math.floor((n % 3600) / 60)}m`;
};
const titleCase = (value?: string) => value ? value.replace(/_/g, " ").replace(/\b\w/g, (c: string) => c.toUpperCase()) : "Not available";
const tone = (state?: string) => state === "running" || state === "ready" || state === "healthy" || state === "completed" ? "green" : state === "paused" || state === "warning" || state === "stale" || state === "warming_up" || state === "starting" || state === "bootstrapping" || state === "warming" || state === "syncing" || state === "recovering" || state === "data_stale" || state === "rebooting" ? "amber" : state === "error" || state === "degraded" || state === "failed" || state === "critical" || state === "disconnected" ? "red" : "default";
const statusTone = (state?: string) => state === "RUNNING_ARMED" ? "green" : state === "ERROR" ? "red" : "amber";
// Four independent axes. One badge could never say WHICH of "the worker is
// alive", "the feed is fresh", "the strategy has warmed up" and "entries are
// armed" had failed, so each gets its own colour from its own value.
const axisTone = (value?: string) => {
  switch (value) {
    case "RUNNING": case "LIVE": case "READY": case "FORWARD_PAPER": return "green";
    case "ERROR": case "FAILED": case "DISCONNECTED": case "BLOCKED": return "red";
    case "STOPPED": case "DISABLED": return "default";
    default: return "amber";
  }
};
const price = (value: unknown, digits = 2) => noValue(value) ? "—" : Number(value).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: 8 });
const StatusAxis = ({ label, value, reason }: { label: string; value?: string; reason?: string }) => (
  <div className="instance-axis" title={reason ?? ""}>
    <span className="dim" style={{ fontSize: 10, letterSpacing: .4 }}>{label}</span>
    <Badge text={value ?? "UNKNOWN"} tone={axisTone(value) as any} />
  </div>
);
const recordedError = (instance: Instance) => instance.last_error || String(instance.engine?.last_error || instance.engine?.stop_reason || "");
const recoveryGuidance = (message: string) => {
  const text = message.toLowerCase();
  if (text.includes("cursor") || text.includes("recoverable") || text.includes("backfill") || text.includes("previously processed")) return "The saved market cursor is outside the venue's recoverable window. If this old instance has no open position, delete it and create a fresh instance with the current built-in strategy version.";
  if (text.includes("migration") || text.includes("schema") || text.includes("instance_market_state")) return "Apply the current trading_instances_schema.sql migration in Supabase, then restart the app container.";
  if (text.includes("slot") || text.includes("capacity") || text.includes("allocation")) return "Stop or delete an unused instance, or reduce its allocation before starting another worker.";
  if (text.includes("websocket") || text.includes("market data") || text.includes("disconnect") || text.includes("timeout")) return "Use Restart once. If recovery fails again, verify the configured venue and inspect the app logs before recreating the instance.";
  return "Use Restart once and review the app logs. If this is an obsolete instance with no open position, delete it and create a fresh one.";
};

function Detail({ label, value, negative = false }: { label: string; value: React.ReactNode; negative?: boolean }) {
  return <div style={{ minWidth: 0, marginBottom: 8 }}><div className="dim" style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: ".06em" }}>{label}</div><div className={negative ? "neg" : ""} style={{ overflowWrap: "anywhere" }}>{value}</div></div>;
}

const rebootPhases = [
  "blocking_entries", "stopping_worker", "disconnecting_market_data",
  "flushing_runtime_state", "clearing_transient_state", "reloading_configuration",
  "reconciling_execution_state", "connecting_market_data", "loading_warmup",
  "rebuilding_indicators", "running_health_checks", "running",
];

function RebootProgress({ reboot }: { reboot?: RebootState | null }) {
  if (!reboot) return null;
  const activeIndex = rebootPhases.indexOf(reboot.phase);
  const failed = reboot.status === "degraded" || reboot.status === "failed";
  return <div className={`instance-reboot ${failed ? "is-degraded" : reboot.status === "completed" ? "is-complete" : ""}`} role={failed ? "alert" : "status"} aria-live="polite">
    <div className="instance-reboot-head"><div><b>{failed ? "Full Bot Reboot — manual repair required" : reboot.status === "completed" ? "Full Bot Reboot complete" : "Full Bot Reboot in progress"}</b><div className="dim">{reboot.message}</div></div><Badge text={titleCase(reboot.status)} tone={tone(reboot.status) as any} /></div>
    <div className="instance-reboot-steps" aria-label="Full Bot Reboot progress">
      {rebootPhases.map((phase, index) => <span key={phase} className={index < activeIndex || reboot.status === "completed" ? "done" : index === activeIndex ? "active" : "pending"} title={titleCase(phase)} />)}
    </div>
    <div className="instance-reboot-meta"><span>{titleCase(reboot.phase)}</span><span>Started {timestamp(reboot.started_at)}</span></div>
    {reboot.error && <div className="neg" style={{ marginTop: 6 }}>{reboot.error}</div>}
  </div>;
}

//: The states TradingInstanceManager.delete will accept. A running worker --
//: including a paused one, whose worker is deliberately retained -- is
//: refused there, so it is refused here too.
const DELETABLE_STATES = ["created", "stopped", "error", "degraded"];

function InstanceActions({ instance, action, remove, actionBusy, locked = false, deleting = false, compact = false }: { instance: Instance; action: (instance: Instance, name: string) => Promise<void>; remove: (instance: Instance) => Promise<void>; actionBusy?: string | null; locked?: boolean; deleting?: boolean; compact?: boolean }) {
  const cls = compact ? "btn btn-soft btn-sm" : "btn btn-soft btn-sm";
  const rebooting = instance.reboot?.status === "running";
  const working = ["starting", "bootstrapping", "warming", "syncing", "ready", "data_stale", "recovering", "rebooting"].includes(instance.state);
  const busyName = actionBusy?.startsWith(`${instance.id}:`) ? actionBusy.slice(instance.id.length + 1) : "";
  // Actions are serialized so two rapid controls cannot race durable worker
  // state. Disable every row while one lifecycle request is in flight.
  const rowBusy = locked || deleting || Boolean(actionBusy) || rebooting;
  const progressLabels: Record<string, string> = { start: "Starting…", pause: "Pausing…", resume: "Resuming…", stop: "Stopping…", restart: "Starting reboot…" };
  // Why Delete is unavailable, in the backend's own words. These mirror the
  // refusals in TradingInstanceManager.delete rather than paraphrasing them,
  // so the button and the 409 it would have produced say the same thing.
  const deleteBlocker = DELETABLE_STATES.includes(instance.state) ? null
    : instance.state === "paused"
      ? "Stop this instance before deleting it. Pause only closes the entry gate — the market worker stays alive to keep its candle cursor."
      : rebooting
        ? "A Full Bot Reboot is in progress. It must finish before this instance can be deleted."
        : `Stop this instance before deleting it (it is ${instance.state}).`;
  const label = (name: string, idle: string) => busyName === name ? progressLabels[name] : idle;
  return <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, flexWrap: "wrap" }}>
    {(instance.state === "running" || instance.state === "ready") && <button className={`${cls} btn-warn`} disabled={rowBusy} onClick={() => void action(instance, "pause")}>{label("pause", "Pause")}</button>}
    {(instance.state === "running" || instance.state === "ready" || working) && <button className={`${cls} btn-danger`} disabled={rowBusy} onClick={() => void action(instance, "stop")}>{label("stop", "Stop")}</button>}
    {instance.state === "paused" && <><button className={`${cls} btn-primary`} disabled={rowBusy} onClick={() => void action(instance, "resume")}>{label("resume", "Resume")}</button><button className={`${cls} btn-danger`} disabled={rowBusy} onClick={() => void action(instance, "stop")}>{label("stop", "Stop")}</button></>}
    {!working && instance.state !== "running" && instance.state !== "ready" && instance.state !== "paused" && <button className={`${cls} btn-primary`} disabled={rowBusy} onClick={() => void action(instance, "start")}>{label("start", "Start")}</button>}
    <button className={cls} disabled={rowBusy} onClick={() => void action(instance, "restart")}>{rebooting ? titleCase(instance.reboot?.phase) : label("restart", "Full Bot Reboot")}</button>
    {/* Delete is always shown, and says why when it cannot be used.
        It used to render only for a deletable state, so a running or paused
        instance simply had no Delete button and nothing anywhere explained
        the absence -- indistinguishable from the feature being broken. The
        rule itself is unchanged: the backend refuses a delete while a worker
        is alive, and a paused worker is deliberately still alive, so this
        button stays disabled in exactly the states it was hidden in. */}
    <button className={`${cls} btn-danger`} title={deleteBlocker ?? "Permanently delete this stopped Trading Instance"}
            disabled={rowBusy || Boolean(deleteBlocker)}
            onClick={() => void remove(instance)}>{deleting ? "Deleting…" : "Delete"}</button>
    {deleteBlocker ? <small className="dim" style={{ flexBasis: "100%", fontSize: 10 }}>{deleteBlocker}</small> : null}
  </div>;
}

export default function TradingInstancesPage({ instanceId }: { instanceId?: string }) {
  const app = useApp();
  const live = useLive<InstancesResponse>("/instances", 5000);
  const options = useLive<Options>("/instances/options", 30000);
  const [selected, setSelected] = useState<string | null>(instanceId ?? null);
  const [view, setView] = useState<"cards" | "table">("cards");
  const [range, setRange] = useState<"today" | "7d" | "30d" | "all">("all");
  const [filters, setFilters] = useState({ status: "", pair: "", strategy: "", timeframe: "", version: "", query: "", sort: "newest" });
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const operationLock = useRef(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [form, setForm] = useState({ symbol: "", strategy: "", strategy_version: "", timeframe: "", exchange: "", risk: "0.5", capital: "1000", max_open_positions: "3", sizing_mode: "fixed_starting_equity_percent", fixed_quantity: "", profit_reinvestment: false, maximum_risk_amount: "", minimum_equity: "", entry_mode: "limit", fill_model: "" });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ strategy: "", strategy_version: "", timeframe: "", exchange: "inherit", risk: "", capital: "", max_open_positions: "", sizing_mode: "fixed_starting_equity_percent", fixed_quantity: "", profit_reinvestment: false, maximum_risk_amount: "", minimum_equity: "", entry_mode: "limit", fill_model: "RealisticFill" });

  useEffect(() => {
    const firstStrategy = options.data?.strategies.find((row) => row.key === options.data?.execution_defaults.strategy) ?? options.data?.strategies[0];
    setForm((current) => ({ ...current,
      symbol: current.symbol || options.data?.execution_defaults.symbol || options.data?.symbols[0] || "",
      strategy: current.strategy || firstStrategy?.key || "",
      strategy_version: current.strategy_version || firstStrategy?.versions[0] || "",
      timeframe: current.timeframe || options.data?.execution_defaults.timeframe || options.data?.timeframes[0] || "",
      exchange: current.exchange || options.data?.execution_defaults.exchange || "inherit",
      risk: current.risk === "0.5" ? String((options.data?.execution_defaults.risk_per_trade_pct ?? 0.005) * 100) : current.risk,
      capital: current.capital === "1000" ? String(options.data?.execution_defaults.capital ?? 1000) : current.capital,
      max_open_positions: current.max_open_positions === "3" ? String(options.data?.execution_defaults.max_open_positions ?? 3) : current.max_open_positions,
      sizing_mode: current.sizing_mode || options.data?.execution_defaults.position_sizing_mode || "fixed_starting_equity_percent",
      fill_model: current.fill_model || options.data?.execution_defaults.fill_model || "RealisticFill",
    }));
  }, [options.data]);

  const rows = live.data?.instances ?? [];
  const filtered = useMemo(() => {
    const query = filters.query.trim().toLowerCase();
    const output = rows.filter((row) => (!filters.status || row.state === filters.status)
      && (!filters.pair || row.symbol === filters.pair)
      && (!filters.strategy || row.strategy_key === filters.strategy)
      && (!filters.timeframe || row.timeframe === filters.timeframe)
      && (!filters.version || row.strategy_version === filters.version)
      && (!query || [row.symbol, row.strategy_label, row.strategy_version, row.timeframe, row.state].join(" ").toLowerCase().includes(query)));
    const val = (row: Instance, key: string): number | string => {
      if (key === "newest" || key === "oldest") return Date.parse(row.created_at || "") || 0;
      if (key === "risk") return row.risk_per_trade_pct || 0;
      if (key === "status") return row.state;
      return Number((row.performance ?? row.metrics)?.[key] ?? 0);
    };
    return output.sort((a, b) => {
      const left = val(a, filters.sort), right = val(b, filters.sort);
      if (typeof left === "string" || typeof right === "string") return String(left).localeCompare(String(right));
      const asc = filters.sort === "oldest" || filters.sort === "max_drawdown_pct" || filters.sort === "risk";
      return asc ? Number(left) - Number(right) : Number(right) - Number(left);
    });
  }, [rows, filters]);
  const activeStates = new Set(["starting", "bootstrapping", "warming", "syncing", "ready", "running", "data_stale", "recovering", "rebooting"]);
  const active = filtered.filter((row) => activeStates.has(row.state));
  const inactive = (state: string) => filtered.filter((row) => row.state === state);
  const current = rows.find((row) => row.id === (instanceId ?? selected)) ?? rows[0];
  const formStrategy = options.data?.strategies.find((row) => row.key === form.strategy);
  const editStrategy = options.data?.strategies.find((row) => row.key === editForm.strategy);
  const capacityKnown = live.data?.available_paper_capital !== undefined && live.data?.available_paper_capital !== null;
  const availableAllocation = capacityKnown ? Number(live.data?.available_paper_capital) : 0;
  const requestedAllocation = Number(form.capital);
  const invalidAllocation = !Number.isFinite(requestedAllocation) || requestedAllocation <= 0;
  const allocationUnavailable = capacityKnown && requestedAllocation > availableAllocation + 1e-9;
  const formRisk = Number(form.risk);
  const invalidRisk = !Number.isFinite(formRisk) || formRisk <= 0 || formRisk > 5;
  const invalidFixedQuantity = form.sizing_mode === "fixed_quantity" && (!Number.isFinite(Number(form.fixed_quantity)) || Number(form.fixed_quantity) <= 0);
  const controlsUnavailable = Boolean(live.error);
  const createDisabled = busy || controlsUnavailable || Boolean(options.error) || !capacityKnown || !form.symbol || !form.strategy || !form.strategy_version || !form.timeframe || invalidAllocation || allocationUnavailable || invalidRisk || invalidFixedQuantity;

  const create = async () => {
    if (createDisabled) return;
    setCreateError(null);
    setBusy(true);
    try {
      const created = await apiPostJson<{ instance: Instance }>("/instances", { symbol: form.symbol, strategy: form.strategy, strategy_version: form.strategy_version, timeframe: form.timeframe, exchange: form.exchange, instrument_type: "spot", risk_per_trade_pct: Number(form.risk) / 100, capital_allocation: Number(form.capital), max_open_positions: Number(form.max_open_positions), mode: "trading", sizing_mode: form.sizing_mode, fixed_quantity: Number(form.fixed_quantity || 0), profit_reinvestment: form.profit_reinvestment, maximum_risk_amount: form.maximum_risk_amount ? Number(form.maximum_risk_amount) : null, minimum_equity: form.minimum_equity ? Number(form.minimum_equity) : null, entry_mode: form.entry_mode, fill_model: form.fill_model });
      // Cards are rendered only from the authoritative GET /instances poll.
      // Never append the POST payload optimistically: creation may fail while
      // Supabase validates the durable per-instance market cursor.
      setSelected(created.instance.id);
      const refreshed = await live.refetch();
      if (!refreshed) {
        const message = "Instance was created, but the authoritative list could not be refreshed. Controls remain disabled until the backend reconnects.";
        setCreateError(message);
        app.toast(message, "error");
        return;
      }
      app.toast("Trading instance created — start it when ready", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not create instance";
      setCreateError(message);
      app.toast(message, "error");
    }
    finally { setBusy(false); }
  };
  const action = async (instance: Instance, name: string) => {
    if (operationLock.current) return;
    const label = `${instance.symbol} · ${instance.strategy_label} ${instance.strategy_version} · ${instance.timeframe}`;
    if (name === "restart" && !window.confirm(`Full Bot Reboot?\n\n${label}\n\nThis rebuilds the worker, market-data connection, caches and strategy runtime from saved configuration.\n\nPaper balance, open positions, pending orders, journal, history, strategy and settings are preserved. New entries stay blocked until backend health checks pass.`)) return;
    if (name === "stop" && !window.confirm(`Stop this Trading Instance?\n\n${label}\n\nIts configuration and market cursor will remain saved.`)) return;
    const key = `${instance.id}:${name}`;
    operationLock.current = true;
    setActionBusy(key);
    try {
      await apiPost(`/instances/${instance.id}/${name}`);
      setActionErrors((current) => { const next = { ...current }; delete next[instance.id]; return next; });
      const refreshed = await live.refetch();
      if (!refreshed) {
        const message = `Instance ${name} completed, but its authoritative state could not be refreshed.`;
        setActionErrors((current) => ({ ...current, [instance.id]: message }));
        app.toast(message, "error");
        return;
      }
      app.toast(name === "restart" ? "Full Bot Reboot started — live backend stages are shown on the instance" : `Instance ${name} completed`, "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : `Could not ${name} instance`;
      setActionErrors((current) => ({ ...current, [instance.id]: message }));
      await live.refetch();
      app.toast(message, "error");
    } finally {
      operationLock.current = false;
      setActionBusy(null);
    }
  };
  const remove = async (instance: Instance) => {
    if (operationLock.current) return;
    const label = `${instance.symbol} · ${instance.strategy_label} ${instance.strategy_version} · ${instance.timeframe}`;
    if (!window.confirm(`Permanently delete this stopped Trading Instance?\n\n${label}\n\nIts configuration and runtime cursor will be removed. Closed trade records remain in the ledger. An instance with an open position cannot be deleted.`)) return;
    operationLock.current = true;
    setDeletingId(instance.id);
    setActionBusy(`${instance.id}:delete`);
    try {
      await apiDelete<{ deleted_instance_id: string }>(`/instances/${instance.id}`);
      setActionErrors((current) => { const next = { ...current }; delete next[instance.id]; return next; });
      if ((instanceId ?? selected) === instance.id) setSelected(null);
      const refreshed = await live.refetch();
      if (!refreshed) {
        const message = "Trading instance was deleted, but the authoritative list could not be refreshed.";
        setActionErrors((current) => ({ ...current, [instance.id]: message }));
        app.toast(message, "error");
        return;
      }
      app.toast("Trading instance deleted", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not delete Trading Instance";
      // A delete refused because the instance still holds an open paper
      // position is not a dead end. Offer the one explicit action that
      // resolves it, so realising that P&L stays a deliberate choice rather
      // than a side effect of pressing Delete.
      if (message.includes("open paper position")) {
        let blocking: { open_positions: { symbol: string; side: string; size: number; unrealized_pnl: number | null; mark_available: boolean; mark_reason?: string | null }[]; resolution: { ready?: boolean; blocked_reason: string | null } | null } | null = null;
        try {
          blocking = await apiGet(`/instances/${instance.id}/open-positions`);
        } catch { /* the plain refusal below is still accurate */ }
        if (blocking) {
          const rows = blocking.open_positions ?? [];
          const summary = rows.map((row) => `${row.side} ${row.size} ${row.symbol}${row.mark_available ? ` (${signedMoney(row.unrealized_pnl)} unrealised)` : ` — ${row.mark_reason ?? "cannot be priced"}`}`).join("\n");
          if (!blocking.resolution?.ready) {
            const detail = `${blocking.resolution?.blocked_reason ?? message}\n\n${summary}`;
            setActionErrors((current) => ({ ...current, [instance.id]: detail }));
            app.toast("Open positions cannot be priced yet", "error");
            return;
          }
          if (window.confirm(`This instance still holds ${rows.length} open paper position(s):\n\n${summary}\n\nClose them at the current mark and realise the P&L into the session that owns them? The instance can then be deleted.`)) {
            try {
              const outcome = await apiPostJson<{ closed: unknown[]; remaining: { symbol: string; reason: string }[] }>(`/instances/${instance.id}/close-open-positions`, { confirm: true });
              await live.refetch();
              // A close that only partly succeeded must not read as success:
              // swallowing this left the operator staring at the original
              // refusal with no sign the close had even been attempted.
              if (outcome.remaining?.length) {
                const detail = `Closed ${outcome.closed?.length ?? 0}; ${outcome.remaining.length} still open:\n${outcome.remaining.map((row) => `${row.symbol} — ${row.reason}`).join("\n")}`;
                setActionErrors((current) => ({ ...current, [instance.id]: detail }));
                app.toast("Some positions could not be closed", "error");
                return;
              }
              // Clear the refusal the operator just resolved. Leaving it
              // pinned under the row redisplays the message they acted on,
              // which is the defect this whole branch exists to fix.
              setActionErrors((current) => { const next = { ...current }; delete next[instance.id]; return next; });
              app.toast("Open paper positions closed; delete again to remove the instance", "success");
              return;
            } catch (closeError) {
              const detail = closeError instanceof Error ? closeError.message : "Could not close the open paper positions";
              setActionErrors((current) => ({ ...current, [instance.id]: detail }));
              app.toast(detail, "error");
              return;
            }
          }
        }
      }
      setActionErrors((current) => ({ ...current, [instance.id]: message }));
      app.toast(message, "error");
    } finally {
      operationLock.current = false;
      setDeletingId(null);
      setActionBusy(null);
    }
  };
  const errorFor = (instance: Instance) => actionErrors[instance.id] || recordedError(instance);
  const beginEdit = (instance: Instance) => {
    setEditError(null);
    setEditingId(instance.id);
    setEditForm({
      strategy: instance.strategy_key,
      strategy_version: instance.strategy_version,
      timeframe: instance.timeframe,
      exchange: instance.exchange ?? "inherit",
      risk: String(instance.risk_per_trade_pct * 100),
      capital: String(instance.capital_allocation),
      max_open_positions: String(instance.max_open_positions ?? 3),
      sizing_mode: instance.sizing_mode ?? "fixed_starting_equity_percent",
      fixed_quantity: String(instance.fixed_quantity ?? instance.fixed_position_size ?? ""),
      profit_reinvestment: Boolean(instance.profit_reinvestment),
      maximum_risk_amount: instance.maximum_risk_amount == null ? "" : String(instance.maximum_risk_amount),
      minimum_equity: instance.minimum_equity == null ? "" : String(instance.minimum_equity),
      entry_mode: instance.entry_mode ?? "limit",
      fill_model: instance.fill_model ?? "PerfectFill",
    });
  };
  const saveEdit = async () => {
    if (!current || editingId !== current.id || editBusy) return;
    setEditBusy(true);
    setEditError(null);
    try {
      await apiPatchJson<{ instance: Instance }>(`/instances/${current.id}`, {
        strategy: editForm.strategy,
        strategy_version: editForm.strategy_version,
        timeframe: editForm.timeframe,
        exchange: editForm.exchange,
        instrument_type: "spot",
        risk_per_trade_pct: Number(editForm.risk) / 100,
        capital_allocation: Number(editForm.capital),
        max_open_positions: Number(editForm.max_open_positions),
        sizing_mode: editForm.sizing_mode,
        fixed_quantity: Number(editForm.fixed_quantity || 0),
        profit_reinvestment: editForm.profit_reinvestment,
        ...(editForm.maximum_risk_amount ? { maximum_risk_amount: Number(editForm.maximum_risk_amount) } : {}),
        ...(editForm.minimum_equity ? { minimum_equity: Number(editForm.minimum_equity) } : {}),
        entry_mode: editForm.entry_mode,
        fill_model: editForm.fill_model,
      });
      live.refetch();
      setEditingId(null);
      app.toast("Trading Instance configuration saved", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not save instance configuration";
      setEditError(message);
      app.toast(message, "error");
    } finally { setEditBusy(false); }
  };

  const marketLegend = current?.market_data?.freshness_thresholds_seconds;
  const curve = ((current?.performance ?? current?.metrics)?.equity_curve ?? []).filter((point: any) => {
    if (range === "all" || !point.t) return true;
    const age = Date.now() - Date.parse(point.t);
    return age <= (range === "today" ? 86400000 : range === "7d" ? 604800000 : 2592000000);
  });

  return <>
    <PageHeader title="Trading Instances" subtitle="independent paper workers · live closed candles, simulated orders, no real funds" />
    {live.error && <div className="instance-risk-notice red" role="alert" style={{ marginBottom: 12 }}><b>Trading Instances unavailable</b><br />{live.error}<br /><span className="dim">Creation and lifecycle controls remain disabled until the authoritative backend state reconnects.</span></div>}
    {options.error && <div className="instance-risk-notice red" role="alert" style={{ marginBottom: 12 }}><b>Instance options unavailable</b><br />{options.error}</div>}
    <div className="stat-row instance-overview">
      <StatCard label="Active slots" value={`${live.data?.active_slots ?? "—"} / ${live.data?.max_active_slots ?? "—"}`} sub="running paper workers" />
      <StatCard label="Total instances" value={String(live.data?.total_instances ?? rows.length)} sub={`running ${live.data?.instance_counts?.running ?? 0} · paused ${live.data?.instance_counts?.paused ?? 0} · stopped ${live.data?.instance_counts?.stopped ?? 0} · error ${live.data?.instance_counts?.error ?? 0}`} />
      <StatCard label="Allocated / available" value={`${money(live.data?.total_allocated_capital)} / ${money(live.data?.available_paper_capital)}`} sub={`current equity ${money(live.data?.total_current_equity)}`} />
      <StatCard label="Global open risk" value={`${money(live.data?.current_global_risk_amount)} / ${money(live.data?.max_global_risk_amount)}`} sub={titleCase(live.data?.global_risk_status)} />
      <StatCard label="Today" value={signedMoney(live.data?.today_pnl)} sub={`${live.data?.today_trades ?? "—"} opened or closed trades`} />
      <StatCard label="Platform" value={titleCase(live.data?.global_status)} sub={`market data ${titleCase(live.data?.market_data_status)}`} />
    </div>

    <div className="instances-control-grid">
      <Card className="instance-control-card" title="Create Trading Instance" subtitle="1 pair · 2 strategy · 3 version · 4 timeframe · 5 capital · 6 risk · 7 sizing · 8 entry · 9 review · 10 start">
        <div className="form-grid-2">
          <Field label="Pair"><select value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })}>{(options.data?.symbols ?? []).map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="Strategy"><select value={form.strategy} onChange={(e) => { const strategy = options.data?.strategies.find((row) => row.key === e.target.value); const supported = strategy?.supported_timeframes ?? options.data?.timeframes ?? []; setForm({ ...form, strategy: e.target.value, strategy_version: strategy?.versions[0] ?? "", timeframe: supported.includes(form.timeframe) ? form.timeframe : supported[0] ?? "" }); }}>{(options.data?.strategies ?? []).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
          <Field label="Strategy version"><select value={form.strategy_version} onChange={(e) => setForm({ ...form, strategy_version: e.target.value })}>{(formStrategy?.versions ?? []).map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="Timeframe"><select value={form.timeframe} onChange={(e) => setForm({ ...form, timeframe: e.target.value })}>{(formStrategy?.supported_timeframes ?? options.data?.timeframes ?? []).map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="Venue / instrument" hint="live candles and executable lot filters"><select value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })}>{(options.data?.exchanges ?? []).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
          <Field label="Capital allocation" hint={capacityKnown ? `${money(availableAllocation)} available` : "Loading available capital…"}><input value={form.capital} max={capacityKnown ? availableAllocation : undefined} min="0.01" type="number" step="0.01" onChange={(e) => setForm({ ...form, capital: e.target.value })} inputMode="decimal" /></Field>
          <Field label="Risk per trade (%)"><input value={form.risk} onChange={(e) => setForm({ ...form, risk: e.target.value })} inputMode="decimal" /></Field>
          <Field label="Maximum open positions"><input type="number" min="1" max="50" step="1" value={form.max_open_positions} onChange={(e) => setForm({ ...form, max_open_positions: e.target.value })} /></Field>
          <Field label="Sizing mode"><select value={form.sizing_mode} onChange={(e) => setForm({ ...form, sizing_mode: e.target.value })}>{(options.data?.sizing_modes ?? []).filter((value) => value.implemented).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
          {form.sizing_mode === "fixed_quantity" && <Field label="Fixed quantity" hint="base-asset units on every trade"><input value={form.fixed_quantity} onChange={(e) => setForm({ ...form, fixed_quantity: e.target.value })} inputMode="decimal" /></Field>}
          {form.sizing_mode === "dynamic_current_equity_percent" && <Field label="Profit reinvestment" hint="losses always reduce risk"><select value={form.profit_reinvestment ? "on" : "off"} onChange={(e) => setForm({ ...form, profit_reinvestment: e.target.value === "on" })}><option value="off">Off — freeze upside risk</option><option value="on">On — compound realized profit</option></select></Field>}
          <Field label="Maximum risk amount" hint="optional hard cap per trade"><input value={form.maximum_risk_amount} placeholder="No additional cap" onChange={(e) => setForm({ ...form, maximum_risk_amount: e.target.value })} inputMode="decimal" /></Field>
          <Field label="Minimum equity" hint="optional risk-halt floor"><input value={form.minimum_equity} placeholder="No equity floor" onChange={(e) => setForm({ ...form, minimum_equity: e.target.value })} inputMode="decimal" /></Field>
          <Field label="Entry mode"><select value={form.entry_mode} onChange={(e) => setForm({ ...form, entry_mode: e.target.value })}><option value="limit">Limit</option><option value="market">Market</option></select></Field>
          <Field label="Fill model" hint="realistic is recommended for forward validation"><select value={form.fill_model} onChange={(e) => setForm({ ...form, fill_model: e.target.value })}>{(options.data?.fill_models ?? []).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
          <Field label="Execution / data"><input readOnly value="Simulated fills / live closed candles" /></Field>
        </div>
        <p className="dim" style={{ margin: "10px 0 0", fontSize: 12 }}>Realistic execution models spread, slippage, latency and fees. Perfect Fill is retained only for controlled ideal-fill comparisons. Historical replay cannot be created as a paper-trading instance.</p>
        {invalidAllocation && <div className="instance-risk-notice amber" role="status" style={{ marginTop: 10 }}>Capital allocation must be a valid value greater than zero.</div>}
        {allocationUnavailable && <div className="instance-risk-notice amber" role="status" style={{ marginTop: 10 }}>{availableAllocation <= 0 ? "No paper-account allocation is available. Stop does not release allocation: delete an unused stopped/error instance, or increase the paper-account capital in platform settings." : `Allocation must not exceed the available ${money(availableAllocation)}.`}</div>}
        {invalidRisk && <div className="instance-risk-notice amber" role="status" style={{ marginTop: 10 }}>Risk per trade must be greater than 0% and no more than 5%.</div>}
        {invalidFixedQuantity && <div className="instance-risk-notice amber" role="status" style={{ marginTop: 10 }}>Fixed quantity must be greater than zero.</div>}
        {createError && <div className="instance-risk-notice red" role="alert" style={{ marginTop: 10 }}>{createError}</div>}
        <button className="btn btn-primary" disabled={createDisabled} aria-busy={busy} style={{ marginTop: 10 }} onClick={() => void create()}><Icon name="plus" size={14} /> {busy ? "Creating…" : "Create instance"}</button>
      </Card>
      <Card className="instance-filter-card" title="Filters & display" subtitle="instance-scoped records only">
        <div className="form-grid-2">
          <Field label="Search"><input placeholder="Pair, strategy, version…" value={filters.query} onChange={(e) => setFilters({ ...filters, query: e.target.value })} /></Field>
          <Field label="Sort"><select value={filters.sort} onChange={(e) => setFilters({ ...filters, sort: e.target.value })}>{[["newest", "Newest"], ["oldest", "Oldest"], ["net_pnl", "Net P&L"], ["profit_factor", "Profit Factor"], ["win_rate", "Win Rate"], ["expectancy", "Expectancy"], ["max_drawdown_pct", "Max Drawdown"], ["risk", "Risk"], ["status", "Status"]].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
          <Field label="Status"><select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })}><option value="">All</option>{["created", "starting", "bootstrapping", "warming", "syncing", "ready", "running", "data_stale", "recovering", "rebooting", "degraded", "paused", "stopped", "error"].map((value) => <option key={value} value={value}>{titleCase(value)}</option>)}</select></Field>
          <Field label="Pair"><select value={filters.pair} onChange={(e) => setFilters({ ...filters, pair: e.target.value })}><option value="">All</option>{[...new Set(rows.map((row) => row.symbol))].map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="Strategy"><select value={filters.strategy} onChange={(e) => setFilters({ ...filters, strategy: e.target.value })}><option value="">All</option>{[...new Map(rows.map((row) => [row.strategy_key, row.strategy_label])).entries()].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
          <Field label="Timeframe"><select value={filters.timeframe} onChange={(e) => setFilters({ ...filters, timeframe: e.target.value })}><option value="">All</option>{[...new Set(rows.map((row) => row.timeframe))].map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="Strategy version"><select value={filters.version} onChange={(e) => setFilters({ ...filters, version: e.target.value })}><option value="">All</option>{[...new Set(rows.map((row) => row.strategy_version))].map((value) => <option key={value}>{value}</option>)}</select></Field>
          <Field label="View"><div className="row-actions" style={{ justifyContent: "flex-start", gap: 6 }}><button className={`btn btn-sm ${view === "cards" ? "btn-primary" : "btn-soft"}`} onClick={() => setView("cards")}>Cards</button><button className={`btn btn-sm ${view === "table" ? "btn-primary" : "btn-soft"}`} onClick={() => setView("table")}>Table</button></div></Field>
        </div>
      </Card>
    <Card className="instance-health-card" title="Market Data Health" subtitle="actual latest closed candles; source connectivity alone is never healthy">
      <div className="dim" style={{ marginBottom: 10, fontSize: 12 }}>Healthy &lt; {marketLegend ? `${duration(marketLegend.healthy_under)}` : "1.5× timeframe"} · Stale 1.5×–3× · Disconnected &gt; {marketLegend ? duration(marketLegend.disconnected_over) : "3× timeframe"} · Error = no usable source</div>
      <div className="tablewrap"><table className="data-table"><thead><tr><th>Pair</th><th>TF</th><th>Source</th><th>Last closed</th><th>Processed</th><th>Age</th><th>Status</th><th>Warm-up</th></tr></thead><tbody>
        {active.map((row) => <tr key={row.id}><td><b>{row.symbol}</b></td><td>{row.timeframe}</td><td>{row.market_data?.data_source ?? "Not available"}</td><td>{timestamp(row.market_data?.last_market_data_timestamp)}</td><td>{timestamp(row.market_data?.last_processed_candle_timestamp)}</td><td>{duration(row.market_data?.market_data_age_seconds)}</td><td><Badge text={titleCase(row.market_data?.market_data_status)} tone={tone(row.market_data?.market_data_status) as any} /></td><td>{row.market_data?.warmup_bars ?? "—"} / {row.engine?.warmup_required ?? "—"}</td></tr>)}
        {!active.length && <tr><td colSpan={8} className="dim ta-center">No running trading instances match the current filters.</td></tr>}
      </tbody></table></div>
    </Card>
    </div>

    <Card className="instance-active-card" title="Active Trading Instances" subtitle="live runtime is separate from historical performance" right={<Badge text={`${active.filter((row) => row.state === "running").length} running · ${active.length} active`} tone={active.length ? "green" : "default"} />}>
      {view === "table" ? <div className="tablewrap"><table className="data-table"><thead><tr><th>Pair</th><th>Strategy</th><th>Version</th><th>TF</th><th>State</th><th>Capital</th><th>Risk</th><th>P&L</th><th>Trades</th><th>WR</th><th>PF</th><th>Market data</th><th>Position</th><th>Health</th><th></th></tr></thead><tbody>
        {filtered.map((row) => <tr key={row.id}><td><button className="btn btn-link" onClick={() => setSelected(row.id)}>{row.symbol}</button></td><td>{row.strategy_label}</td><td>{row.strategy_version}</td><td>{row.timeframe}</td><td><Badge text={row.ui_status ?? "BLOCKED"} tone={statusTone(row.ui_status) as any} />{(row.ui_status === "ERROR" || actionErrors[row.id]) && <div className="neg" title={errorFor(row) || "No error reason recorded"} style={{ marginTop: 5, maxWidth: 260, whiteSpace: "normal", overflowWrap: "anywhere", fontSize: 11 }}>{errorFor(row) || "No error reason recorded"}</div>}</td><td>{money(row.capital_allocation)}</td><td>{pct(row.risk_per_trade_pct * 100)}</td><td className={Number(row.performance?.net_pnl ?? row.metrics?.realized_pnl) < 0 ? "neg" : "pos"}>{signedMoney(row.performance?.net_pnl ?? row.metrics?.realized_pnl)}</td><td>{row.performance?.trades ?? row.metrics?.trades ?? "—"}</td><td>{pct(row.performance?.win_rate ?? row.metrics?.win_rate, 1)}</td><td>{number(row.performance?.profit_factor ?? row.metrics?.profit_factor)}</td><td>{row.market_status ?? titleCase(row.market_data?.market_data_status)}</td><td>{row.current_position ? `${row.current_position.side} ${row.current_position.symbol}` : "—"}</td><td className={row.runtime_status === "ERROR" ? "neg" : ""} title={row.current_blocker ?? ""}>{`${row.runtime_status ?? "—"} · ${row.strategy_status ?? "—"}`}</td><td><InstanceActions instance={row} action={action} remove={remove} actionBusy={actionBusy} locked={controlsUnavailable} deleting={deletingId === row.id} compact /></td></tr>)}
        {!filtered.length && <tr><td colSpan={15} className="dim ta-center">No instances match the current filters.</td></tr>}
      </tbody></table></div> : <div style={{ display: "grid", gap: 12 }}>
        {active.map((row) => <article key={row.id} className="instance-worker-row">
          <section className="instance-worker-column instance-identity"><div className="dim" style={{ fontSize: 10 }}>{row.execution_status ?? "FORWARD_PAPER"} · BINANCE USD-M</div><h3>{row.symbol}</h3><div>{row.strategy_label}</div><div className="dim">{row.strategy_key} · {row.strategy_version} · {row.id.slice(0, 8).toUpperCase()}</div><div className="instance-axes"><StatusAxis label="RUNTIME" value={row.runtime_status} /><StatusAxis label="MARKET" value={row.market_status} reason={row.market_status_reason} /><StatusAxis label="STRATEGY" value={row.strategy_status} reason={row.strategy_status_reason} /><StatusAxis label="EXECUTION" value={row.execution_status} reason={row.execution_status_reason} /></div>{row.current_blocker && <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>Blocker: {row.current_blocker}</div>}{row.strategy_lifecycle && row.strategy_lifecycle !== "PRODUCTION" && <div className="instance-risk-notice amber" style={{ marginTop: 6 }}>{row.strategy_label} is {row.strategy_lifecycle}: it keeps running here, but it can no longer be chosen for a new instance.</div>}{row.configuration_revision?.stale && <div className="instance-risk-notice amber" style={{ marginTop: 6 }}>This worker is still running configuration revision {row.configuration_revision.running}; the saved settings are revision {row.configuration_revision.configured}. Restart the instance to apply them.</div>}{row.runtime_status === "BLOCKED" && row.last_error?.startsWith("RECONCILIATION_FAILED") && <div className="instance-risk-notice red" style={{ marginTop: 6 }}><b>Blocked by reconciliation.</b><br /><span className="dim">{row.last_error}</span></div>}{row.strategy_identity?.matches === false && <div className="instance-risk-notice red">Configured {row.strategy_identity.configured_label} differs from worker {row.strategy_identity.worker_label ?? "unknown"}.</div>}{(row.state === "error" || row.state === "degraded" || actionErrors[row.id]) && <div className="instance-risk-notice red" style={{ marginTop: 8 }}><b>{errorFor(row) || "No error reason recorded"}</b><br /><span className="dim">Recommended: {recoveryGuidance(errorFor(row))}</span></div>}<RebootProgress reboot={row.reboot} /><div className="instance-worker-actions"><InstanceActions instance={row} action={action} remove={remove} actionBusy={actionBusy} locked={controlsUnavailable} deleting={deletingId === row.id} /></div><button className="btn btn-link" onClick={() => setSelected(row.id)}>Open details</button></section>
          <section className="instance-worker-column"><Detail label="Multi-timeframe policy" value={row.mtf_policy?.label ?? `Entry ${row.timeframe} · native HTF loading`} /><Detail label="Venue / instrument" value={`${titleCase(row.effective_exchange ?? row.exchange)} / ${titleCase(row.instrument_type)}`} /><Detail label="Allocation / realized equity" value={`${money(row.capital_allocation)} / ${money(row.execution?.current_realized_equity)}`} /><Detail label="Risk % / next max risk" value={`${pct(row.risk_per_trade_pct * 100)} / ${money(row.execution?.next_trade_risk_amount)}`} /><Detail label="Next quantity" value={row.execution?.next_trade_quantity ?? "Calculated from the next valid stop"} /><Detail label="Sizing / entry" value={`${titleCase(row.sizing_mode ?? row.execution?.position_sizing_mode)} / ${titleCase(row.entry_mode ?? row.execution?.entry_mode)}`} /><Detail label="Risk basis / reinvest" value={`${money(row.execution?.risk_basis)} / ${row.profit_reinvestment ? "On" : "Off"}`} /></section>
          <section className="instance-worker-column"><Detail label="Venue / market / symbol" value={`${titleCase(row.feed?.exchange ?? row.effective_exchange)} · ${titleCase(row.feed?.market_type)} · ${row.feed?.symbol ?? row.symbol}`} /><Detail label="Execution TF / HTF" value={`${row.feed?.execution_timeframe ?? row.timeframe} · ${row.feed?.htf_primary_timeframe ?? "—"} / ${row.feed?.htf_secondary_timeframe ?? "—"}`} /><Detail label="Last / bid / ask / mark" value={`${price(row.feed?.last_trade_price)} · ${price(row.feed?.bid)} / ${price(row.feed?.ask)} · ${price(row.feed?.mark_price)}`} /><Detail label="Last closed candle / age" value={`${timestamp(row.feed?.last_closed_candle_timestamp)} · ${duration(row.feed?.data_age_seconds)}`} /><Detail label="Last WebSocket message" value={timestamp(row.feed?.last_websocket_message_timestamp)} /><Detail label="Subscription" value={`${row.subscription?.channel ?? "—"} · ${row.subscription?.state ?? "—"} · ${row.subscription?.transport_state ?? "—"}`} /><Detail label="Reconnect attempts" value={`feed ${row.subscription?.reconnect_attempts ?? 0} · worker ${row.worker?.engine_reconnect_attempt ?? 0}/${row.worker?.engine_max_reconnect_attempts ?? "—"}`} /><Detail label="Warm-up / dup / missing / order" value={`${row.feed?.warmup_bars ?? "—"}/${row.feed?.warmup_required ?? "—"} · ${row.feed?.duplicate_candles ?? "—"} · ${row.feed?.missing_candles ?? "—"} · ${row.feed?.out_of_order_candles ?? "—"}`} /><Detail label="Source" value={row.feed?.data_source ?? "Not available"} /></section>
          <section className="instance-worker-column"><div className="dim" style={{ fontSize: 10, marginBottom: 8 }}>HISTORICAL PERFORMANCE — NOT RUNTIME HEALTH</div><Detail label="Net P&L / return" value={`${signedMoney(row.performance?.net_pnl)} / ${pct(row.performance?.return_pct, 3)}`} /><Detail label="Trades / win rate" value={`${row.performance?.trades ?? "—"} / ${pct(row.performance?.win_rate, 1)}`} /><Detail label="Profit factor / average R" value={`${number(row.performance?.profit_factor)} / ${number(row.performance?.average_rr, 3)}R`} /><Detail label="Expectancy / max DD" value={`${money(row.performance?.expectancy)} / ${pct(row.performance?.max_drawdown_pct)}`} /><Detail label="Sharpe (per-trade R)" value={number(row.performance?.sharpe_ratio)} /><Detail label="Historical strategy health" value={row.strategy_health?.status ?? "Not available"} /></section>
          <section className="instance-worker-column"><Detail label="Worker alive / heartbeat" value={`${row.worker?.alive ? "Yes" : "No"} · ${titleCase(row.worker?.lifecycle_state)} · ${timestamp(row.worker?.last_heartbeat)}`} /><Detail label="Execution owner" value={row.worker?.worker_id ?? "No worker holds this instance"} /><Detail label="Config revision" value={`saved ${row.configuration_revision?.configured ?? "—"} · running ${row.configuration_revision?.running ?? "—"}${row.configuration_revision?.stale ? " · NOT APPLIED" : ""}`} /><Detail label="Signals / accepted / rejected" value={`${row.worker_counts?.signals ?? 0} / ${row.worker_counts?.accepted ?? 0} / ${row.worker_counts?.rejections ?? 0}`} /><Detail label="Rejections learning / risk / corr / dedup" value={`${row.engine?.rejection_counts?.learning ?? 0} / ${row.engine?.rejection_counts?.risk ?? row.engine?.rejection_counts?.risk_guard ?? 0} / ${row.engine?.rejection_counts?.correlation ?? 0} / ${row.engine?.rejection_counts?.dedup ?? 0}`} /><Detail label="Last decision" value={row.last_decision ? `${row.last_decision.final_state ?? "QUALIFIED"} · ${row.last_decision.blocker || row.last_decision.reason}` : row.last_blocker ?? "No decision"} /><Detail label="Open positions / orders" value={`${row.engine?.open_positions ?? "—"} / ${row.execution?.pending_orders ?? "—"}`} /><Detail label="Open risk / Unrealized" value={`${money(row.risk?.open_risk_amount)} / ${signedMoney(row.execution?.unrealized_pnl)}`} /></section>
        </article>)}
        {!active.length && <div className="dim ta-center" style={{ padding: 14 }}>No running instances match the current filters.</div>}
      </div>}
    </Card>

    {current && <div className="grid-2-eq">
      <Card title={`${current.symbol} performance`} subtitle="instance-only closed paper trades; never blended with another strategy or pair">
        <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginBottom: 8 }}>{(["today", "7d", "30d", "all"] as const).map((value) => <button key={value} className={`btn btn-sm ${range === value ? "btn-primary" : "btn-soft"}`} onClick={() => setRange(value)}>{value === "all" ? "All" : value.toUpperCase()}</button>)}</div>
        {curve.length > 1 ? <div className="chart-md"><AreaLine labels={curve.map((point: any) => point.t ? new Date(point.t).toLocaleDateString() : "Start")} series={[{ name: "Equity", data: curve.map((point: any) => Number(point.equity)), color: "#eab54f" }]} valueFormatter={(value) => money(value)} /></div> : <div className="dim ta-center" style={{ padding: 48 }}>Insufficient data</div>}
      </Card>
      <Card title={`${current.symbol} status`} subtitle="actual worker, decision, risk, and position state">
        <RebootProgress reboot={current.reboot} />
        {errorFor(current) && <div className="instance-risk-notice amber"><b>{titleCase(current.state)}</b><br />{errorFor(current)}<br /><span className="dim">Recommended: {recoveryGuidance(errorFor(current))}</span></div>}
        <div className="form-grid-2"><Detail label="Last decision" value={current.last_decision ? `${current.last_decision.final_state ?? "QUALIFIED"} · ${current.last_decision.side ?? "No side"}` : "Not available"} /><Detail label="Gate / blocker" value={current.last_decision?.blocker || `${current.last_decision?.gate_stage ?? "—"} · ${current.last_decision?.reason ?? "Not available"}`} /><Detail label="Failed rules" value={(current.last_decision?.failed_rules ?? []).join(" · ") || "None"} /><Detail label="Strategy health" value={current.strategy_health?.status ?? "Not available"} /><Detail label="Health sample" value={current.strategy_health?.sample_size ?? "Not available"} /><Detail label="Starting / realized equity" value={`${money(current.execution?.starting_equity)} / ${money(current.execution?.current_realized_equity)}`} /><Detail label="Unrealized / mark-to-market" value={`${signedMoney(current.execution?.unrealized_pnl)} / ${money(current.execution?.mark_to_market_equity)}`} /><Detail label="Gross P&L / fees / net" value={`${signedMoney(current.execution?.gross_realized_pnl)} / ${money(current.execution?.fees_paid)} / ${signedMoney(current.execution?.realized_pnl)}`} /><Detail label="Available capital" value={money(current.execution?.available_capital)} /><Detail label="Sizing / basis / next risk" value={`${titleCase(current.sizing_mode)} / ${money(current.execution?.risk_basis)} / ${money(current.execution?.next_trade_risk_amount)}`} /><Detail label="Risk cap / equity floor" value={`${money(current.maximum_risk_amount)} / ${money(current.minimum_equity)}`} /></div>
        {current.current_position ? <details style={{ marginTop: 8 }}><summary style={{ cursor: "pointer" }}>Open position · {current.current_position.side} {current.current_position.symbol}</summary><div className="form-grid-2" style={{ marginTop: 10 }}><Detail label="Entry / current" value={`${number(current.current_position.entry, 8)} / ${number(current.current_position.mark, 8)}`} /><Detail label="Stop / target" value={`${number(current.current_position.stop, 8)} / ${number(current.current_position.target, 8)}`} /><Detail label="Quantity / risk" value={`${number(current.current_position.size, 8)} / ${money(current.current_position.risk_amount)}`} /><Detail label="Current R / P&L" value={`${number(current.current_position.current_r, 3)}R / ${signedMoney(current.current_position.unrealized_pnl)}`} /><Detail label="Duration" value={duration(current.current_position.duration_seconds)} /></div></details> : <p className="dim" style={{ marginTop: 12 }}>No open position for this instance.</p>}
        <NewsGuardPanel instanceId={current.id} />
        <QualityGatePanel instanceId={current.id} />
        <PublicRecordPanel instanceId={current.id} />
        {editingId !== current.id ? <button className="btn btn-soft" style={{ marginTop: 10 }} onClick={() => beginEdit(current)}><Icon name="settings" size={14} /> Edit complete configuration</button> : <details open style={{ marginTop: 12 }}>
          <summary style={{ cursor: "pointer", fontWeight: 700 }}>Instance configuration</summary>
          <p className="dim" style={{ margin: "8px 0" }}>Changes are saved to this instance only. Execution changes safely rebuild a running worker. Allocation and fill model cannot change after the first trade, and configuration changes are rejected while a position is open.</p>
          <div className="form-grid-2">
            <Field label="Strategy"><select value={editForm.strategy} onChange={(e) => { const strategy = options.data?.strategies.find((row) => row.key === e.target.value); setEditForm({ ...editForm, strategy: e.target.value, strategy_version: strategy?.versions[0] ?? "" }); }}>{(options.data?.strategies ?? []).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
            <Field label="Strategy version"><select value={editForm.strategy_version} onChange={(e) => setEditForm({ ...editForm, strategy_version: e.target.value })}>{(editStrategy?.versions ?? []).map((value) => <option key={value}>{value}</option>)}</select></Field>
            <Field label="Timeframe"><select value={editForm.timeframe} onChange={(e) => setEditForm({ ...editForm, timeframe: e.target.value })}>{(options.data?.timeframes ?? []).map((value) => <option key={value}>{value}</option>)}</select></Field>
            <Field label="Venue / instrument" hint="immutable after the first trade"><select value={editForm.exchange} onChange={(e) => setEditForm({ ...editForm, exchange: e.target.value })}>{(options.data?.exchanges ?? []).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
            <Field label="Capital allocation"><input value={editForm.capital} onChange={(e) => setEditForm({ ...editForm, capital: e.target.value })} inputMode="decimal" /></Field>
            <Field label="Risk per trade (%)"><input value={editForm.risk} onChange={(e) => setEditForm({ ...editForm, risk: e.target.value })} inputMode="decimal" /></Field>
            <Field label="Maximum open positions"><input type="number" min="1" max="50" step="1" value={editForm.max_open_positions} onChange={(e) => setEditForm({ ...editForm, max_open_positions: e.target.value })} /></Field>
            <Field label="Sizing mode"><select value={editForm.sizing_mode} onChange={(e) => setEditForm({ ...editForm, sizing_mode: e.target.value })}>{(options.data?.sizing_modes ?? []).filter((value) => value.implemented).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
            {editForm.sizing_mode === "fixed_quantity" && <Field label="Fixed quantity" hint="base-asset units on every trade"><input value={editForm.fixed_quantity} onChange={(e) => setEditForm({ ...editForm, fixed_quantity: e.target.value })} inputMode="decimal" /></Field>}
            {editForm.sizing_mode === "dynamic_current_equity_percent" && <Field label="Profit reinvestment" hint="losses always reduce risk"><select value={editForm.profit_reinvestment ? "on" : "off"} onChange={(e) => setEditForm({ ...editForm, profit_reinvestment: e.target.value === "on" })}><option value="off">Off — freeze upside risk</option><option value="on">On — compound realized profit</option></select></Field>}
            <Field label="Maximum risk amount" hint="blank keeps the existing cap"><input value={editForm.maximum_risk_amount} placeholder="No additional cap" onChange={(e) => setEditForm({ ...editForm, maximum_risk_amount: e.target.value })} inputMode="decimal" /></Field>
            <Field label="Minimum equity" hint="blank keeps the existing floor"><input value={editForm.minimum_equity} placeholder="No equity floor" onChange={(e) => setEditForm({ ...editForm, minimum_equity: e.target.value })} inputMode="decimal" /></Field>
            <Field label="Entry mode"><select value={editForm.entry_mode} onChange={(e) => setEditForm({ ...editForm, entry_mode: e.target.value })}><option value="limit">Limit</option><option value="market">Market</option></select></Field>
            <Field label="Fill model"><select value={editForm.fill_model} onChange={(e) => setEditForm({ ...editForm, fill_model: e.target.value })}>{(options.data?.fill_models ?? []).map((value) => <option key={value.key} value={value.key}>{value.label}</option>)}</select></Field>
            <Field label="Execution / data"><input readOnly value="Simulated fills / live closed candles" /></Field>
          </div>
          {editError && <div className="instance-risk-notice red" role="alert" style={{ marginTop: 10 }}>{editError}</div>}
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 8, marginTop: 10 }}><button className="btn btn-primary" disabled={editBusy} onClick={() => void saveEdit()}>{editBusy ? "Saving…" : "Save configuration"}</button><button className="btn btn-soft" disabled={editBusy} onClick={() => { setEditingId(null); setEditError(null); }}>Cancel</button></div>
        </details>}
      </Card>
    </div>}

    {(["created", "paused", "stopped", "degraded", "error"] as const).map((state) => <details key={state} style={{ marginTop: 12 }}><summary className="card" style={{ cursor: "pointer", padding: 12 }}>{titleCase(state)} · {inactive(state).length}</summary>{inactive(state).map((row) => <Card key={row.id} title={`${row.symbol} · ${row.strategy_label}`} subtitle={`${row.strategy_version} · ${row.timeframe}`}><RebootProgress reboot={row.reboot} /><div className="grid-2-eq"><div>{errorFor(row) ? <div className="instance-risk-notice amber"><b>{errorFor(row)}</b><br /><span className="dim">Recommended: {recoveryGuidance(errorFor(row))}</span></div> : <p className="dim">No error reason recorded.</p>}</div><InstanceActions instance={row} action={action} remove={remove} actionBusy={actionBusy} locked={controlsUnavailable} deleting={deletingId === row.id} /></div></Card>)}</details>)}
  </>;
}
