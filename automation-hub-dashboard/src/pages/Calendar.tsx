import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { CalendarDays, ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { PageHeader } from "../components/common/ui";
import DayDrawer from "../components/calendar/DayDrawer";
import {
  Amount, COLLECTOR_LABEL, NetList, STATE_GLYPH, STATE_LABEL, SourceChip, StateTag, fundingText, grossLossText, longDate,
  shortId, winRate,
} from "../components/calendar/shared";
import {
  EMPTY_FILTERS, type CalendarFilters, type DayResponse, type MonthDay, type MonthResponse, type OptionsResponse,
} from "../components/calendar/types";
import { apiGet, useLive } from "../lib/api";
import { formatMoney } from "../lib/money";
import { useApp } from "../app-context";

// The app-wide realized P&L calendar. Every number on this page comes from
// /calendar/* (automation-hub/services/pnl_calendar.py); this page formats
// and lays out, it never adds amounts together. docs/PNL_CALENDAR.md has the
// rules (sources, identity, currency, timezone, drawdown).

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const FILTER_KEYS: (keyof CalendarFilters)[] = ["source", "instance", "strategy", "symbol", "timeframe"];
const REFRESH_MS = 60_000;

const pad = (n: number) => String(n).padStart(2, "0");
const isoDate = (y: number, m: number, d: number) => `${y}-${pad(m)}-${pad(d)}`;
const daysIn = (y: number, m: number) => new Date(Date.UTC(y, m, 0)).getUTCDate();
/** Monday-first column of a date (0 = Monday). */
const column = (y: number, m: number, d: number) => (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;

function shiftDate(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + days));
  return isoDate(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
}

/** Today's date in the calendar timezone (not the browser's). */
function todayIn(tz: string): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" })
      .format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

function timeIn(iso: string, tz: string): string {
  try {
    return new Date(iso).toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

// ---- URL state: #/calendar?month=2026-09&date=2026-09-24&source=pa_lab ----
interface UrlState { month: string | null; date: string | null; filters: CalendarFilters }

function readUrl(): UrlState {
  const query = window.location.hash.split("?", 2)[1] ?? "";
  const q = new URLSearchParams(query);
  const month = q.get("month");
  const date = q.get("date");
  const filters = { ...EMPTY_FILTERS };
  for (const k of FILTER_KEYS) filters[k] = q.get(k) ?? "";
  return {
    month: month && /^\d{4}-\d{2}$/.test(month) ? month : null,
    date: date && /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : null,
    filters,
  };
}

function writeUrl(month: string, date: string | null, filters: CalendarFilters) {
  const q = new URLSearchParams({ month });
  if (date) q.set("date", date);
  for (const k of FILTER_KEYS) if (filters[k]) q.set(k, filters[k]);
  const next = `#/calendar?${q.toString()}`;
  if (window.location.hash !== next) {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${next}`);
  }
}

function filterQuery(filters: CalendarFilters): string {
  const q = new URLSearchParams();
  for (const k of FILTER_KEYS) if (filters[k]) q.set(k, filters[k]);
  const s = q.toString();
  return s ? `&${s}` : "";
}

// ---- pieces ----
function DayCell({ y, m, d, day, today, selected, focused, loading, onOpen, onKey }: {
  y: number; m: number; d: number; day: MonthDay | undefined; today: string; selected: boolean; focused: boolean;
  loading: boolean; onOpen: (date: string) => void; onKey: (e: KeyboardEvent<HTMLButtonElement>, date: string) => void;
}) {
  const date = isoDate(y, m, d);
  const state = day?.state ?? "none";
  const currencies = day ? Object.keys(day.by_currency) : [];
  const partial = day ? day.realizations - day.closed_trades : 0;
  const future = date > today;
  const label = [
    longDate(date),
    loading ? "loading" : STATE_LABEL[state],
    ...currencies.map((c) => formatMoney(day!.by_currency[c].net, c)),
    day && day.closed_trades ? `${day.closed_trades} closed trade${day.closed_trades === 1 ? "" : "s"}` : "",
    partial > 0 ? `${partial} partial exit${partial === 1 ? "" : "s"}` : "",
    date === today ? "today" : "",
  ].filter(Boolean).join(", ");
  return (
    <div role="gridcell" aria-selected={selected}>
      <button
        type="button"
        data-date={date}
        tabIndex={focused ? 0 : -1}
        className={`cal-day st-${state}${selected ? " is-selected" : ""}${date === today ? " is-today" : ""}${future ? " is-future" : ""}`}
        aria-label={label}
        onClick={() => onOpen(date)}
        onKeyDown={(e) => onKey(e, date)}
      >
        <span className="cal-day-top">
          <span className="cal-day-num">{d}</span>
          {state !== "none" && !loading ? <span className="cal-day-glyph" aria-hidden>{STATE_GLYPH[state]}</span> : null}
        </span>
        {loading ? <span className="cal-day-skel" aria-hidden /> : day && currencies.length ? (
          <span className="cal-day-body cal-fade" aria-hidden>
            {currencies.slice(0, 2).map((c) => (
              <span key={c} className={`cal-day-net cal-${day.by_currency[c].state}`}>{formatMoney(day.by_currency[c].net, c)}</span>
            ))}
            {currencies.length > 2 ? <span className="cal-day-more">+{currencies.length - 2} more</span> : null}
            <span className="cal-day-count">
              {day.closed_trades ? `${day.closed_trades} trade${day.closed_trades === 1 ? "" : "s"}` : ""}
              {partial > 0 ? `${day.closed_trades ? " · " : ""}${partial} partial` : ""}
            </span>
          </span>
        ) : (
          <span className="cal-day-empty" aria-hidden>{future ? "" : "No trades"}</span>
        )}
      </button>
    </div>
  );
}

function SummaryRow({ currency, data, multi }: { currency: string; data: MonthResponse; multi: boolean }) {
  const s = data.summary[currency];
  const bestDay = s.best_day; const worstDay = s.worst_day;
  const dayRef = (x: { date: string; net: string } | null | undefined) => x ? (
    <><Amount value={x.net} currency={currency} strong /><span className="cal-sum-sub">{longDate(x.date).replace(/ \d{4}$/, "")}</span></>
  ) : <span className="dim">None</span>;
  return (
    <div className="cal-sum cal-fade">
      {multi ? <div className="cal-sum-cur">{currency === "UNKNOWN" ? "Currency not recorded" : currency}</div> : null}
      <div className="cal-sum-grid">
        <div className="cal-sum-item cal-sum-net">
          <span className="cal-sum-label">Monthly net P&amp;L</span>
          <Amount value={s.net} currency={currency} strong />
          <span className="cal-sum-sub"><StateTag state={s.state} /></span>
        </div>
        <div className="cal-sum-item"><span className="cal-sum-label">Closed trades</span><b>{s.closed_trades}</b>
          <span className="cal-sum-sub">{s.trading_days ?? 0} trading day{s.trading_days === 1 ? "" : "s"}</span></div>
        <div className="cal-sum-item"><span className="cal-sum-label">Wins</span><b>{s.wins}</b></div>
        <div className="cal-sum-item"><span className="cal-sum-label">Losses</span><b>{s.losses}</b>
          {s.breakeven ? <span className="cal-sum-sub">{s.breakeven} break-even</span> : null}</div>
        <div className="cal-sum-item"><span className="cal-sum-label">Win rate</span><b>{winRate(s)}</b></div>
        <div className="cal-sum-item"><span className="cal-sum-label">Best day</span>{dayRef(bestDay)}</div>
        <div className="cal-sum-item"><span className="cal-sum-label">Worst day</span>{dayRef(worstDay)}</div>
        <div className="cal-sum-item"><span className="cal-sum-label">Max drawdown</span>
          <b>{formatMoney(s.max_drawdown, currency, { signed: false })}</b>
          <span className="cal-sum-sub">realized, peak to trough</span></div>
      </div>
      <div className="cal-sum-foot">
        <span>Gross profit {formatMoney(s.gross_profit, currency)}</span>
        <span>Gross loss {grossLossText(s.gross_loss, currency)}</span>
        <span>Fees {formatMoney(s.fees, currency, { signed: false })}</span>
        <span>Funding {fundingText(s.funding, currency)}</span>
      </div>
    </div>
  );
}

export default function CalendarPage() {
  const { go } = useApp();
  const initial = useMemo(readUrl, []);
  const options = useLive<OptionsResponse>("/calendar/options", 300_000);
  const tz = options.data?.timezone ?? "UTC";
  const today = todayIn(tz);

  const [ym, setYm] = useState<{ y: number; m: number } | null>(() => {
    if (!initial.month) return null;
    const [y, m] = initial.month.split("-").map(Number);
    return m >= 1 && m <= 12 ? { y, m } : null;
  });
  const [direction, setDirection] = useState<"next" | "prev" | "none">("none");
  const [filters, setFilters] = useState<CalendarFilters>(initial.filters);
  const [openDate, setOpenDate] = useState<string | null>(initial.date);
  const [focusDate, setFocusDate] = useState<string | null>(initial.date);
  const [refreshing, setRefreshing] = useState(false);
  const moveFocus = useRef(false);
  const grid = useRef<HTMLDivElement>(null);

  // Default to the current month in the calendar timezone once it is known.
  useEffect(() => {
    if (!ym && options.data) {
      const [y, m] = today.split("-").map(Number);
      setYm({ y, m });
    }
  }, [ym, options.data, today]);
  // If options cannot load, fall back to UTC's current month rather than a blank page.
  useEffect(() => {
    if (!ym && options.error) {
      const [y, m] = todayIn("UTC").split("-").map(Number);
      setYm({ y, m });
    }
  }, [ym, options.error]);

  const y = ym?.y ?? 0; const m = ym?.m ?? 0;
  const monthKey = ym ? `${y}-${pad(m)}` : "";
  const query = filterQuery(filters);
  const monthPath = ym ? `/calendar/month?year=${y}&month=${m}${query}` : null;
  const month = useLive<MonthResponse>(monthPath, REFRESH_MS);
  const dayPath = openDate ? `/calendar/day?date_=${openDate}${query}` : null;
  const day = useLive<DayResponse>(dayPath, REFRESH_MS);

  useEffect(() => { if (ym) writeUrl(monthKey, openDate, filters); }, [ym, monthKey, openDate, filters]);

  const data = month.data && month.data.year === y && month.data.month === m ? month.data : null;
  const byDate = useMemo(() => new Map((data?.days ?? []).map((d) => [d.date, d])), [data]);
  const loadingGrid = !data && !month.error;

  const changeMonth = useCallback((delta: number) => {
    if (!ym) return;
    const idx = ym.y * 12 + (ym.m - 1) + delta;
    setDirection(delta > 0 ? "next" : "prev");
    setYm({ y: Math.floor(idx / 12), m: (idx % 12) + 1 });
  }, [ym]);

  const goToday = () => {
    const [ty, tm] = today.split("-").map(Number);
    if (ym) setDirection(ty * 12 + tm > ym.y * 12 + ym.m ? "next" : ty * 12 + tm < ym.y * 12 + ym.m ? "prev" : "none");
    setYm({ y: ty, m: tm });
    setFocusDate(today);
  };

  // Roving focus: exactly one date button is in the tab order.
  const inMonth = (iso: string) => iso.startsWith(`${monthKey}-`);
  const tabDate = focusDate && inMonth(focusDate) ? focusDate
    : openDate && inMonth(openDate) ? openDate
      : inMonth(today) ? today : `${monthKey}-01`;

  useEffect(() => {
    if (!moveFocus.current || !focusDate) return;
    moveFocus.current = false;
    grid.current?.querySelector<HTMLButtonElement>(`button[data-date="${focusDate}"]`)?.focus();
  }, [focusDate, monthKey]);

  const onKey = (e: KeyboardEvent<HTMLButtonElement>, date: string) => {
    const col = column(...(date.split("-").map(Number) as [number, number, number]));
    const moves: Record<string, number> = {
      ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -col, End: 6 - col,
    };
    let next: string | null = null;
    if (e.key in moves) next = shiftDate(date, moves[e.key]);
    else if (e.key === "PageUp" || e.key === "PageDown") {
      const [dy, dm, dd] = date.split("-").map(Number);
      const idx = dy * 12 + (dm - 1) + (e.key === "PageDown" ? 1 : -1);
      const ny = Math.floor(idx / 12); const nm = (idx % 12) + 1;
      next = isoDate(ny, nm, Math.min(dd, daysIn(ny, nm)));
    }
    if (!next) return;
    e.preventDefault();
    const [ny, nm] = next.split("-").map(Number);
    if (ny !== y || nm !== m) { setDirection(ny * 12 + nm > y * 12 + m ? "next" : "prev"); setYm({ y: ny, m: nm }); }
    moveFocus.current = true;
    setFocusDate(next);
  };

  const openDay = (date: string) => { setFocusDate(date); setOpenDate(date); };
  const closeDay = () => setOpenDate(null);
  const returnFocus = () => {
    const target = focusDate;
    window.requestAnimationFrame(() => {
      if (target) grid.current?.querySelector<HTMLButtonElement>(`button[data-date="${target}"]`)?.focus();
    });
  };

  const setFilter = (key: keyof CalendarFilters, value: string) => setFilters((f) => ({ ...f, [key]: value }));
  // An instance belongs to one source; picking another source drops it.
  const chooseSource = (source: string) => setFilters((f) => {
    const keep = !source || !f.instance
      || (options.data?.instances ?? []).some((i) => i.id === f.instance && i.source === source);
    return { ...f, source, instance: keep ? f.instance : "" };
  });
  const activeFilters = FILTER_KEYS.filter((k) => filters[k]);

  const refresh = async () => {
    if (!monthPath) return;
    setRefreshing(true);
    try {
      await apiGet(`${monthPath}&fresh=true`);   // re-read every ledger now
      await Promise.all([month.refetch(), day.refetch(), options.refetch()]);
    } catch { /* the error surfaces through the live hooks */ }
    finally { setRefreshing(false); }
  };

  // Filters that name something the recorded trades do not contain.
  const unknownFilters = useMemo(() => {
    const o = options.data;
    if (!o) return [] as string[];
    const bad: string[] = [];
    if (filters.source && !o.sources.some((s) => s.key === filters.source)) bad.push(`source "${filters.source}"`);
    if (filters.instance && !o.instances.some((i) => i.id === filters.instance)) bad.push(`instance "${filters.instance}"`);
    if (filters.strategy && !o.strategies.includes(filters.strategy)) bad.push(`strategy "${filters.strategy}"`);
    if (filters.symbol && !o.symbols.includes(filters.symbol.toUpperCase())) bad.push(`symbol "${filters.symbol}"`);
    if (filters.timeframe && !o.timeframes.includes(filters.timeframe)) bad.push(`timeframe "${filters.timeframe}"`);
    return bad;
  }, [options.data, filters]);

  const diagnostics = data?.diagnostics ?? options.data?.diagnostics;
  const failed = Object.entries(diagnostics?.sources ?? {}).filter(([, s]) => !s.ok);
  const openPositions = Object.values(diagnostics?.sources ?? {}).reduce((a, s) => a + (s.open_positions ?? 0), 0);
  const stale = Boolean(data && month.error);
  const hasTrades = Boolean(data && data.currencies.length);
  const instances = (options.data?.instances ?? []).filter((i) => !filters.source || i.source === filters.source);

  // Weeks of the visible month, Monday first, with leading blanks.
  const cells = useMemo(() => {
    if (!ym) return [] as (number | null)[][];
    const lead = column(y, m, 1);
    const flat: (number | null)[] = [...Array(lead).fill(null), ...Array.from({ length: daysIn(y, m) }, (_, i) => i + 1)];
    while (flat.length % 7) flat.push(null);
    const weeks: (number | null)[][] = [];
    for (let i = 0; i < flat.length; i += 7) weeks.push(flat.slice(i, i + 7));
    return weeks;
  }, [ym, y, m]);

  const tradingDays = (data?.days ?? []).filter((d) => d.state !== "none");

  return (
    <div className="cal-page">
      <PageHeader
        title="Calendar"
        subtitle="Realized P&L from every trading source, day by day · closed trades only"
        actions={
          <>
            {diagnostics?.collected_at ? (
              <span className="cal-updated dim">Collected {timeIn(diagnostics.collected_at, tz)}</span>
            ) : null}
            <button type="button" className="btn btn-ghost" onClick={refresh} disabled={refreshing || !monthPath}
              aria-label="Refresh from every ledger">
              <RefreshCw size={14} className={refreshing ? "spin" : ""} aria-hidden /> Refresh
            </button>
          </>
        }
      />

      {/* month navigation + filters */}
      <section className="card cal-toolbar" aria-label="Calendar controls">
        <div className="cal-nav">
          <button type="button" className="icon-btn" onClick={() => changeMonth(-1)} aria-label="Previous month" disabled={!ym}>
            <ChevronLeft size={18} aria-hidden />
          </button>
          <h2 className="cal-month" id="cal-month-title" aria-live="polite">
            {ym ? `${MONTHS[m - 1]} ${y}` : "Loading…"}
          </h2>
          <button type="button" className="icon-btn" onClick={() => changeMonth(1)} aria-label="Next month" disabled={!ym}>
            <ChevronRight size={18} aria-hidden />
          </button>
          <button type="button" className="btn btn-soft btn-sm" onClick={goToday} disabled={!ym}>
            <CalendarDays size={14} aria-hidden /> Today
          </button>
          <span className="cal-tz dim">
            Times in <b>{tz}</b> · <button type="button" className="link-button" onClick={() => go("Settings")}>change</button>
          </span>
        </div>

        <div className="cal-sources" role="radiogroup" aria-label="Source">
          {[{ key: "", label: "All sources", trades: -1 }, ...(options.data?.sources ?? [])].map((s) => (
            <button
              key={s.key || "all"}
              type="button"
              role="radio"
              aria-checked={filters.source === s.key}
              className={`cal-chip${filters.source === s.key ? " is-on" : ""}`}
              onClick={() => chooseSource(s.key)}
              title={s.trades >= 0 ? `${s.trades} realizations recorded in total` : undefined}
            >
              {s.key ? <SourceChip source={s.key} label={s.label} /> : s.label}
              {s.trades >= 0 ? <span className="cal-chip-count" aria-label={`${s.trades} realizations recorded`}>{s.trades}</span> : null}
            </button>
          ))}
        </div>

        <div className="cal-filters">
          <label className="field">
            <span className="field-label">Instance</span>
            <select value={filters.instance} onChange={(e) => setFilter("instance", e.target.value)}>
              <option value="">All instances</option>
              {instances.map((i) => (
                <option key={i.id} value={i.id}>{i.name || "Not recorded"} · {shortId(i.id)}</option>
              ))}
              {filters.instance && !instances.some((i) => i.id === filters.instance)
                ? <option value={filters.instance}>{shortId(filters.instance)} (not found)</option> : null}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Strategy</span>
            <select value={filters.strategy} onChange={(e) => setFilter("strategy", e.target.value)}>
              <option value="">All strategies</option>
              {(options.data?.strategies ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
              {filters.strategy && !(options.data?.strategies ?? []).includes(filters.strategy)
                ? <option value={filters.strategy}>{filters.strategy} (not found)</option> : null}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Symbol</span>
            <select value={filters.symbol} onChange={(e) => setFilter("symbol", e.target.value)}>
              <option value="">All symbols</option>
              {(options.data?.symbols ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
              {filters.symbol && !(options.data?.symbols ?? []).includes(filters.symbol.toUpperCase())
                ? <option value={filters.symbol}>{filters.symbol} (not found)</option> : null}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Timeframe</span>
            <select value={filters.timeframe} onChange={(e) => setFilter("timeframe", e.target.value)}>
              <option value="">All timeframes</option>
              {(options.data?.timeframes ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
              {filters.timeframe && !(options.data?.timeframes ?? []).includes(filters.timeframe)
                ? <option value={filters.timeframe}>{filters.timeframe} (not found)</option> : null}
            </select>
          </label>
          {activeFilters.length ? (
            <button type="button" className="btn btn-ghost btn-sm cal-clear" onClick={() => setFilters(EMPTY_FILTERS)}>
              Clear filters ({activeFilters.length})
            </button>
          ) : null}
        </div>
      </section>

      {/* honest status: what is missing, stale or excluded */}
      {month.error && !data ? (
        <div className="cal-banner cal-banner-error" role="alert">
          <b>The calendar could not be loaded.</b> {month.error}
          <div className="dim">No P&amp;L is shown rather than figures that could be wrong. It retries automatically.</div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void month.refetch()}>Retry now</button>
        </div>
      ) : null}
      {stale ? (
        <div className="cal-banner cal-banner-warn" role="status">
          <b>Showing the last figures received.</b> The latest refresh failed ({month.error}); these were collected at{" "}
          {data ? timeIn(data.diagnostics.collected_at, tz) : ""} and may be out of date.
        </div>
      ) : null}
      {failed.length ? (
        <div className="cal-banner cal-banner-warn" role="status">
          <b>Some trades are missing.</b>{" "}
          {failed.map(([k, s]) => `${COLLECTOR_LABEL[k] ?? k} could not be read (${s.error ?? "unknown error"})`).join("; ")}.
          {" "}Every total on this page excludes those trades.
        </div>
      ) : null}
      {unknownFilters.length ? (
        <div className="cal-banner cal-banner-warn" role="status">
          <b>No recorded trades match the {unknownFilters.join(", ")}.</b>{" "}
          <button type="button" className="link-button" onClick={() => setFilters(EMPTY_FILTERS)}>Clear filters</button>
        </div>
      ) : null}
      {data?.conversion.needed ? <p className="cal-note cal-conversion">{data.conversion.note}</p> : null}

      {/* month summary */}
      <section className="card cal-summary" aria-label="Month summary">
        {loadingGrid ? (
          <div className="cal-sum-skeleton" aria-label="Loading month summary"><span /><span /><span /><span /></div>
        ) : data && hasTrades ? (
          data.currencies.map((c) => <SummaryRow key={`${monthKey}-${query}-${c}`} currency={c} data={data} multi={data.currencies.length > 1} />)
        ) : data ? (
          <div className="cal-empty">
            <b>No closed trades in {MONTHS[m - 1]} {y}</b>
            <span className="dim">
              {activeFilters.length ? "Nothing matches the current filters. " : "No source realized any P&L this month. "}
              Open positions are not counted until they close.
            </span>
          </div>
        ) : null}
        {openPositions > 0 ? (
          <p className="cal-unrealized">
            <span className="cal-tag">Unrealized</span> {openPositions} open position{openPositions === 1 ? " is" : "s are"} not
            included: the calendar counts realized P&amp;L only.{" "}
            <button type="button" className="link-button" onClick={() => go("Portfolio")}>See open positions</button>
          </p>
        ) : null}
      </section>

      {/* month grid */}
      <section className="card cal-grid-card" aria-labelledby="cal-month-title">
        <div className="cal-legend" aria-hidden>
          <span><i className="cal-profit">▲</i> Profit</span>
          <span><i className="cal-loss">▼</i> Loss</span>
          <span><i className="cal-breakeven">=</i> Break-even</span>
          <span><i className="cal-mixed">◆</i> Mixed currencies</span>
        </div>
        <div
          ref={grid}
          role="grid"
          aria-labelledby="cal-month-title"
          aria-busy={loadingGrid}
          className={`cal-grid cal-slide-${direction}`}
          key={monthKey}
        >
          <div role="row" className="cal-week cal-weekdays">
            {WEEKDAYS.map((w) => <div role="columnheader" key={w} className="cal-weekday"><abbr title={w}>{w}</abbr></div>)}
          </div>
          {cells.map((week, wi) => (
            <div role="row" className="cal-week" key={wi}>
              {week.map((d, di) => d == null ? <div role="gridcell" key={`b${di}`} className="cal-blank" aria-hidden /> : (
                <DayCell
                  key={d}
                  y={y} m={m} d={d}
                  day={byDate.get(isoDate(y, m, d))}
                  today={today}
                  selected={openDate === isoDate(y, m, d)}
                  focused={tabDate === isoDate(y, m, d)}
                  loading={loadingGrid}
                  onOpen={openDay}
                  onKey={onKey}
                />
              ))}
            </div>
          ))}
        </div>
        <p className="cal-help dim">
          Each day is the P&amp;L realized on that date in {tz}: a trade counts on the day it closed, and a partial exit on the
          day it was filled. Use the arrow keys to move between days, Page Up / Page Down to change month, Enter to open.
        </p>
      </section>

      {/* mobile agenda: the trading days as a list */}
      {data && tradingDays.length ? (
        <section className="card cal-agenda" aria-label="Trading days this month">
          <h3 className="card-title">Trading days</h3>
          <ul>
            {tradingDays.map((d) => (
              <li key={d.date}>
                <button type="button" onClick={() => openDay(d.date)}>
                  <span>{longDate(d.date).replace(/ \d{4}$/, "")}</span>
                  <span className="dim">{d.closed_trades} trade{d.closed_trades === 1 ? "" : "s"}</span>
                  <NetList byCurrency={d.by_currency} strong />
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {data && data.diagnostics.duplicates_dropped > 0 ? (
        <p className="cal-note">
          {data.diagnostics.duplicates_dropped} duplicate cop{data.diagnostics.duplicates_dropped === 1 ? "y" : "ies"} of
          already-counted fills were ignored (same fill id read from both a live lab session and its saved snapshot).
        </p>
      ) : null}

      {openDate ? (
        <DayDrawer
          date={openDate}
          data={day.data}
          error={day.error}
          loading={day.loading}
          onClose={closeDay}
          returnFocus={returnFocus}
        />
      ) : null}
    </div>
  );
}
