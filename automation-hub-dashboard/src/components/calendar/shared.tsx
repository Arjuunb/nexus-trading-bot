import { Activity, Blocks, Brain, RefreshCw, SquareTerminal, type LucideIcon } from "lucide-react";
import { decimalSign, formatMoney } from "../../lib/money";
import type { ByCurrency, DayState, Money } from "./types";

const dash = <span className="dim">—</span>;

// One icon per source, the same icons the sidebar uses for those pages, so a
// trade's origin reads the same everywhere.
export const SOURCE_ICON: Record<string, LucideIcon> = {
  trading_instance: Blocks,
  pa_lab: Activity,
  smc_lab: Brain,
  adaptive_lab: RefreshCw,
  paper_trading: SquareTerminal,
};

/** Which ledger each collector reads, for the data-source status notes. */
export const COLLECTOR_LABEL: Record<string, string> = {
  ledger: "Main trade ledger (Trading Instances and Paper Trading)",
  adaptive_lab: "Adaptive MTF Lab ledger",
  pa_lab: "Price Action Lab paper account",
  smc_lab: "SMC Lab paper account",
};

export const STATE_LABEL: Record<DayState, string> = {
  none: "No trades", profit: "Profit", loss: "Loss", breakeven: "Break-even", mixed: "Mixed currencies",
};

/** Text glyph for a state, so it never depends on colour alone. */
export const STATE_GLYPH: Record<DayState, string> = {
  none: "", profit: "▲", loss: "▼", breakeven: "=", mixed: "◆",
};

export function stateOf(net: string): "profit" | "loss" | "breakeven" {
  const s = decimalSign(net);
  return s > 0 ? "profit" : s < 0 ? "loss" : "breakeven";
}

export function SourceChip({ source, label }: { source: string; label: string }) {
  const Icon = SOURCE_ICON[source] ?? Activity;
  return (
    <span className={`cal-source cal-source-${source}`}>
      <Icon size={13} strokeWidth={2} aria-hidden />
      {label}
    </span>
  );
}

export function StateTag({ state }: { state: DayState }) {
  if (state === "none") return <span className="cal-state cal-state-none">No trades</span>;
  return (
    <span className={`cal-state cal-state-${state}`}>
      <span aria-hidden>{STATE_GLYPH[state]}</span> {STATE_LABEL[state]}
    </span>
  );
}

/** A signed amount with its state class; the sign carries the meaning. */
export function Amount({ value, currency, strong }: { value: string; currency: string; strong?: boolean }) {
  const state = stateOf(value);
  const Tag = strong ? "b" : "span";
  return <Tag className={`cal-amt cal-${state}`}>{formatMoney(value, currency)}</Tag>;
}

/** Net per currency, one line each (never added across currencies). */
export function NetList({ byCurrency, strong }: { byCurrency: ByCurrency; strong?: boolean }) {
  const entries = Object.entries(byCurrency);
  if (!entries.length) return <span className="dim">No trades</span>;
  return (
    <span className="cal-netlist">
      {entries.map(([currency, m]) => <Amount key={currency} value={m.net} currency={currency} strong={strong} />)}
    </span>
  );
}

const isZero = (v: string) => decimalSign(v) === 0;

/** Funding is stored positive when paid; show it as its effect on P&L. */
export function fundingText(funding: string, currency: string): string {
  if (isZero(funding)) return formatMoney("0", currency);
  return funding.startsWith("-")
    ? `${formatMoney(funding.slice(1), currency)} received`
    : `-${formatMoney(funding, currency, { signed: false })} paid`;
}

/** Gross loss is stored as a positive magnitude and shown as a loss. */
export const grossLossText = (magnitude: string, currency: string) =>
  isZero(magnitude) ? formatMoney("0", currency) : `-${formatMoney(magnitude, currency, { signed: false })}`;

export const winRate = (m: Money) => (m.win_rate == null ? "No closed trades" : `${Number(m.win_rate).toFixed(1)}%`);

export function duration(seconds: number | null): string {
  if (seconds == null) return "Not recorded";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** "2026-09-24T02:15:00+01:00" -> "02:15". The backend already converted the
 *  instant to the calendar timezone, so the browser's zone is never applied. */
export const clock = (iso: string | null) => (iso ? iso.slice(11, 16) : "Not recorded");

/** "2026-09-24T..." -> "24 Sep 02:15" (same zone rule as clock). */
export function stamp(iso: string | null): string {
  if (!iso) return "Not recorded";
  const [y, mo, d] = iso.slice(0, 10).split("-").map(Number);
  const month = new Date(Date.UTC(y, mo - 1, d)).toLocaleString("en-GB", { month: "short", timeZone: "UTC" });
  return `${d} ${month} ${iso.slice(11, 16)}`;
}

/** "2026-09-24" -> "Thursday 24 September 2026" (a calendar date, no zone). */
export function longDate(isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

export const shortId = (id: string | null | undefined) => (id ? (id.length > 10 ? `${id.slice(0, 8)}…` : id) : "");

export const NOT_RECORDED = <span className="cal-missing">Not recorded</span>;

/** Profit factor as a ratio; "No losses" when it is undefined but there was profit. */
export function profitFactorText(m: Money): string {
  if (m.profit_factor != null) return Number(m.profit_factor).toFixed(2);
  return decimalSign(m.gross_profit) > 0 ? "No losses" : "—";
}

/**
 * Per-trade figures of a period. The averages and extremes use each closed
 * trade's total net over all its exits; profit factor is the period's gross
 * profit over its gross loss (the two figures shown beside it).
 */
export function TradeStats({ m, currency, month }: { m: Money; currency: string; month?: boolean }) {
  const money = (v: string | null, signed = true) => (v == null ? dash : formatMoney(v, currency, { signed }));
  const loss = (v: string | null) => (v == null ? dash : `-${formatMoney(v, currency, { signed: false })}`);
  return (
    <dl className="cal-kv cal-stats">
      <div><dt>Profit factor</dt><dd>{profitFactorText(m)}</dd></div>
      <div><dt>Expectancy / trade</dt><dd>{money(m.expectancy)}</dd></div>
      <div><dt>Average win</dt><dd>{money(m.avg_win)}</dd></div>
      <div><dt>Average loss</dt><dd>{loss(m.avg_loss)}</dd></div>
      <div><dt>Largest win</dt><dd>{money(m.largest_win)}</dd></div>
      <div><dt>Largest loss</dt><dd>{loss(m.largest_loss)}</dd></div>
      {month ? (
        <>
          <div><dt>Winning / losing days</dt><dd>{m.winning_days ?? 0} / {m.losing_days ?? 0}{m.breakeven_days ? <span className="dim"> · {m.breakeven_days} flat</span> : null}</dd></div>
          <div><dt>Longest streak (days)</dt><dd>{m.longest_winning_streak ?? 0} up · {m.longest_losing_streak ?? 0} down</dd></div>
        </>
      ) : null}
    </dl>
  );
}
