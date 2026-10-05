import { useState } from "react";
import Icon from "../common/Icon";
import { usePref } from "../../lib/prefs";
import { useLive } from "../../lib/api";
import { EMPTY_FILTERS, MODE_LABELS, activeFilterCount, label, type JournalFilters as Filters } from "../../lib/journal";

export type JournalMeta = {
  modes: { mode: string; trades: number }[];
  default_modes: string[];
  facets: {
    strategy_name: string[]; lab_id: string[]; symbol: string[]; timeframe: string[];
    entry_session: string[]; exit_reason: string[]; trading_mode: string[];
    instances: { instance_id: string; instance_name: string | null }[]; leverage: number[];
  };
  sessions: { key: string; label: string }[];
  results: string[]; exit_reasons: string[];
  columns: { key: string; label: string; default: boolean }[];
};

/** Shared, persisted journal filters. Every tab (trades, analytics, weekly
 *  review) reads the same filter state, so the cards, table and charts always
 *  describe the same trades. */
export function useJournalFilters(): [Filters, (f: Filters) => void] {
  const [stored, setStored] = usePref<Filters>("journal.v2.filters", EMPTY_FILTERS);
  return [{ ...EMPTY_FILTERS, ...(stored ?? {}) }, setStored];
}

export function useJournalMeta() {
  return useLive<JournalMeta>("/journal/v2/meta", 15000);
}

function Select({ name, value, options, onChange, all = "All" }: {
  name: string; value: string; options: { value: string; label: string }[];
  onChange: (v: string) => void; all?: string;
}) {
  return (
    <label className="tj-field">
      <span>{name}</span>
      <select aria-label={name} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{all}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

function Range({ name, min, max, onMin, onMax, step = "any" }: {
  name: string; min: string; max: string; onMin: (v: string) => void; onMax: (v: string) => void; step?: string;
}) {
  return (
    <label className="tj-field tj-range">
      <span>{name}</span>
      <div>
        <input aria-label={`${name} minimum`} type="number" step={step} placeholder="min" value={min}
          onChange={(e) => onMin(e.target.value)} />
        <input aria-label={`${name} maximum`} type="number" step={step} placeholder="max" value={max}
          onChange={(e) => onMax(e.target.value)} />
      </div>
    </label>
  );
}

/** Trading-mode selector. No selection = the server picks ONE mode; several
 *  modes (or All Modes) must be chosen explicitly, and the UI flags it. */
export function ModeChips({ filters, setFilters, meta, applied }: {
  filters: Filters; setFilters: (f: Filters) => void; meta: JournalMeta | null; applied?: string[];
}) {
  const modes = meta?.modes ?? [];
  const selected = filters.modes.length ? filters.modes : applied ?? meta?.default_modes ?? [];
  const toggle = (mode: string) => {
    if (mode === "ALL") return setFilters({ ...filters, modes: filters.modes.includes("ALL") ? [] : ["ALL"] });
    const base = filters.modes.filter((m) => m !== "ALL");
    const next = base.includes(mode) ? base.filter((m) => m !== mode) : [...base, mode];
    setFilters({ ...filters, modes: next });
  };
  return (
    <div className="tj-modes" role="group" aria-label="Trading mode">
      {modes.filter((m) => m.trades > 0 || selected.includes(m.mode)).map((m) => (
        <button key={m.mode} type="button" aria-pressed={selected.includes(m.mode) || selected.includes("ALL")}
          className={`chip-btn ${selected.includes(m.mode) || selected.includes("ALL") ? "active" : ""}`}
          onClick={() => toggle(m.mode)}>
          {MODE_LABELS[m.mode] ?? m.mode} <span className="dim">{m.trades}</span>
        </button>
      ))}
      <button type="button" aria-pressed={filters.modes.includes("ALL")}
        className={`chip-btn ${filters.modes.includes("ALL") ? "active" : ""}`} onClick={() => toggle("ALL")}>
        All Modes
      </button>
      {!filters.modes.length && <span className="dim tj-hint">single mode chosen by the server — modes are never mixed unless you select them</span>}
    </div>
  );
}

export default function JournalFiltersBar({ filters, setFilters, meta }: {
  filters: Filters; setFilters: (f: Filters) => void; meta: JournalMeta | null;
}) {
  const [more, setMore] = useState(false);
  const set = (key: keyof Filters) => (value: string) => setFilters({ ...filters, [key]: value });
  const facets = meta?.facets;
  const opts = (values?: string[]) => (values ?? []).map((v) => ({ value: v, label: v }));
  const count = activeFilterCount(filters);
  return (
    <div className="tj-filters">
      <div className="tj-filter-row">
        <label className="tj-field">
          <span>From</span>
          <input aria-label="Date from" type="date" value={filters.date_from} onChange={(e) => set("date_from")(e.target.value)} />
        </label>
        <label className="tj-field">
          <span>To</span>
          <input aria-label="Date to" type="date" value={filters.date_to} onChange={(e) => set("date_to")(e.target.value)} />
        </label>
        <Select name="Strategy" value={filters.strategy} onChange={set("strategy")} options={opts(facets?.strategy_name)} />
        <Select name="Instance" value={filters.instance_id} onChange={set("instance_id")}
          options={(facets?.instances ?? []).map((i) => ({ value: i.instance_id, label: i.instance_name ?? i.instance_id.slice(0, 8) }))} />
        <Select name="Lab" value={filters.lab} onChange={set("lab")}
          options={(facets?.lab_id ?? []).map((v) => ({ value: v, label: label(v) }))} />
        <Select name="Symbol" value={filters.symbol} onChange={set("symbol")} options={opts(facets?.symbol)} />
        <Select name="Side" value={filters.direction} onChange={set("direction")}
          options={[{ value: "LONG", label: "Long" }, { value: "SHORT", label: "Short" }]} />
        <Select name="Result" value={filters.result} onChange={set("result")}
          options={[{ value: "WINS", label: "Wins" }, { value: "LOSSES", label: "Losses" },
            { value: "BREAK_EVEN", label: "Break even" }, { value: "OPEN", label: "Open" },
            { value: "OPERATIONAL", label: "Operational events" },
            ...(meta?.results ?? []).map((r) => ({ value: r, label: label(r) }))]} />
        <button type="button" className="btn btn-soft btn-sm tj-more" aria-expanded={more} onClick={() => setMore(!more)}>
          <Icon name="settings" size={13} /> {more ? "Fewer filters" : "More filters"}
        </button>
        {count > 0 && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilters({ ...EMPTY_FILTERS, modes: filters.modes })}>
            Clear {count}
          </button>
        )}
      </div>
      {more && (
        <div className="tj-filter-row">
          <Select name="Session" value={filters.session} onChange={set("session")}
            options={(meta?.sessions ?? []).map((s) => ({ value: s.key, label: s.label }))} />
          <Select name="Timeframe" value={filters.timeframe} onChange={set("timeframe")} options={opts(facets?.timeframe)} />
          <Select name="Exit reason" value={filters.exit_reason} onChange={set("exit_reason")}
            options={(meta?.exit_reasons ?? []).map((v) => ({ value: v, label: label(v) }))} />
          <Select name="Rule check" value={filters.rule_violation} onChange={set("rule_violation")}
            options={[{ value: "true", label: "Rule violation" }, { value: "false", label: "Rules followed" }]} />
          <Range name="Leverage" min={filters.leverage_min} max={filters.leverage_max}
            onMin={set("leverage_min")} onMax={set("leverage_max")} />
          <Range name="Planned RR" min={filters.rr_min} max={filters.rr_max} onMin={set("rr_min")} onMax={set("rr_max")} />
          <Range name="Net P&L" min={filters.pnl_min} max={filters.pnl_max} onMin={set("pnl_min")} onMax={set("pnl_max")} />
        </div>
      )}
    </div>
  );
}
