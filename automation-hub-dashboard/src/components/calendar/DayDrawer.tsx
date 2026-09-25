import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { formatDecimal, formatMoney } from "../../lib/money";
import {
  Amount, NOT_RECORDED, NetList, SourceChip, StateTag, clock, duration, fundingText, grossLossText, longDate, shortId,
  stamp, winRate,
} from "./shared";
import type { Breakdown, ByCurrency, DayResponse, Trade } from "./types";

/** Who produced a trade: the instance, the lab session, or the engine. */
export function origin(t: Trade): { name: string; id: string } {
  if (t.source === "pa_lab" || t.source === "smc_lab") {
    const session = t.account.includes(":") ? t.account.split(":").slice(1).join(":") : "";
    return { name: `${t.source_label} paper session`, id: session };
  }
  if (t.instance_id) return { name: t.instance_name || "Not recorded", id: t.instance_id };
  return { name: "No instance (signal engine)", id: "" };
}

const sideText = (side: Trade["side"]) => (side === "long" ? "Long" : side === "short" ? "Short" : null);
const outcomeText = { win: "Win", loss: "Loss", breakeven: "Break-even" } as const;
const val = (v: ReactNode | null | undefined) => (v == null || v === "" ? NOT_RECORDED : v);

function SummaryBlock({ currency, summary }: { currency: string; summary: ByCurrency }) {
  const m = summary[currency];
  return (
    <div className="cal-daysum">
      <div className="cal-daysum-head">
        <span className="cal-cur">{currency === "UNKNOWN" ? "Currency not recorded" : currency}</span>
        <Amount value={m.net} currency={currency} strong />
      </div>
      <dl className="cal-kv">
        <div><dt>Closed trades</dt><dd>{m.closed_trades}</dd></div>
        <div><dt>Wins</dt><dd>{m.wins}</dd></div>
        <div><dt>Losses</dt><dd>{m.losses}</dd></div>
        <div><dt>Break-even</dt><dd>{m.breakeven}</dd></div>
        <div><dt>Win rate</dt><dd>{winRate(m)}</dd></div>
        <div><dt>Max drawdown</dt><dd>{formatMoney(m.max_drawdown, currency, { signed: false })}</dd></div>
        <div><dt>Gross profit</dt><dd>{formatMoney(m.gross_profit, currency)}</dd></div>
        <div><dt>Gross loss</dt><dd>{grossLossText(m.gross_loss, currency)}</dd></div>
        <div><dt>Fees</dt><dd>{formatMoney(m.fees, currency, { signed: false })}</dd></div>
        <div><dt>Funding</dt><dd>{fundingText(m.funding, currency)}</dd></div>
      </dl>
      {m.realizations > m.closed_trades ? (
        <p className="cal-note">
          Includes {m.realizations - m.closed_trades} partial exit{m.realizations - m.closed_trades === 1 ? "" : "s"} of
          positions not yet fully closed; their realized P&amp;L is in the net figure.
        </p>
      ) : null}
    </div>
  );
}

function wl(b: Breakdown) {
  const ms = Object.values(b.by_currency);
  return `${ms.reduce((a, m) => a + m.wins, 0)} / ${ms.reduce((a, m) => a + m.losses, 0)}`;
}

function TradeRow({ t }: { t: Trade }) {
  const o = origin(t);
  return (
    <tr>
      <td className="mono">{clock(t.closed_at)}</td>
      <td className="cal-origin">
        <SourceChip source={t.source} label={t.source_label} />
        <span className="cal-sub">{o.name}{o.id ? <span className="mono" title={o.id}> · {shortId(o.id)}</span> : null}</span>
      </td>
      <td>{val(t.strategy)}</td>
      <td className="mono">
        {val(t.symbol)}
        {sideText(t.side) ? <span className="cal-sub"><span className={`cal-side cal-side-${t.side}`}>{sideText(t.side)}</span></span> : null}
      </td>
      <td className="mono"><Amount value={t.net} currency={t.currency} strong /></td>
      <td>{t.partial ? <span className="cal-tag">Partial exit</span> : t.outcome ? <span className={`cal-tag cal-${t.outcome === "win" ? "profit" : t.outcome}`}>{outcomeText[t.outcome]}</span> : NOT_RECORDED}</td>
      <td className="mono">{t.rr ? `${Number(t.rr).toFixed(2)}R` : NOT_RECORDED}</td>
      <td className="mono">{t.entry_price ? formatDecimal(t.entry_price) : NOT_RECORDED}<span className="cal-sub">{stamp(t.opened_at)}</span></td>
      <td className="mono">{t.exit_price ? formatDecimal(t.exit_price) : NOT_RECORDED}<span className="cal-sub">{stamp(t.closed_at)}</span></td>
      <td>{t.duration_s == null ? NOT_RECORDED : duration(t.duration_s)}</td>
      <td>{val(t.timeframe)}</td>
      <td className="mono">{formatMoney(t.gross, t.currency)}</td>
      <td className="mono">{formatMoney(t.fees, t.currency, { signed: false })}</td>
      <td className="mono">{fundingText(t.funding, t.currency)}</td>
      <td>{val(t.exit_reason)}</td>
    </tr>
  );
}

/** Mobile layout of one trade: the same fields, stacked. */
function TradeCard({ t }: { t: Trade }) {
  const o = origin(t);
  const rows: [string, ReactNode][] = [
    ["Bot / lab", <>{o.name}{o.id ? <span className="mono dim"> · {shortId(o.id)}</span> : null}</>],
    ["Strategy", val(t.strategy)],
    ["Timeframe", val(t.timeframe)],
    ["Entry", <>{t.entry_price ? formatDecimal(t.entry_price) : NOT_RECORDED} <span className="dim">{stamp(t.opened_at)}</span></>],
    ["Exit", <>{t.exit_price ? formatDecimal(t.exit_price) : NOT_RECORDED} <span className="dim">{stamp(t.closed_at)}</span></>],
    ["Duration", t.duration_s == null ? NOT_RECORDED : duration(t.duration_s)],
    ["Gross", formatMoney(t.gross, t.currency)],
    ["Fees", formatMoney(t.fees, t.currency, { signed: false })],
    ["Funding", fundingText(t.funding, t.currency)],
    ["R/R", t.rr ? `${Number(t.rr).toFixed(2)}R` : NOT_RECORDED],
    ["Exit reason", val(t.exit_reason)],
  ];
  return (
    <li className="cal-tcard">
      <div className="cal-tcard-head">
        <SourceChip source={t.source} label={t.source_label} />
        <span className="mono dim">{clock(t.closed_at)}</span>
      </div>
      <div className="cal-tcard-main">
        <span className="mono">{t.symbol ?? "Symbol not recorded"}</span>
        {sideText(t.side) ? <span className={`cal-side cal-side-${t.side}`}>{sideText(t.side)}</span> : null}
        <Amount value={t.net} currency={t.currency} strong />
        {t.partial ? <span className="cal-tag">Partial exit</span> : null}
      </div>
      <dl className="cal-kv cal-kv-tight">
        {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
      </dl>
    </li>
  );
}

export default function DayDrawer({ date, data, error, loading, onClose, returnFocus }: {
  date: string;
  data: DayResponse | null;
  error: string | null;
  loading: boolean;
  onClose: () => void;
  returnFocus: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeBtn.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      // keep keyboard focus inside the open dialog
      const items = panel.current.querySelectorAll<HTMLElement>(
        'button, a[href], select, input, [tabindex]:not([tabindex="-1"])');
      if (!items.length) return;
      const first = items[0]; const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = previousOverflow;
      returnFocus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const headingId = `cal-day-${date}`;
  const day = data && data.date === date ? data : null;

  return createPortal(
    <div className="cal-drawer-root">
      <div className="cal-drawer-backdrop" onClick={onClose} aria-hidden />
      <div className="cal-drawer" role="dialog" aria-modal="true" aria-labelledby={headingId} ref={panel}>
        <header className="cal-drawer-head">
          <div>
            <h2 id={headingId}>{longDate(date)}</h2>
            <div className="cal-drawer-meta">
              {day ? <StateTag state={day.state} /> : null}
              <span className="dim">Realized P&amp;L · times in {day?.timezone ?? "the calendar timezone"}</span>
            </div>
          </div>
          <button ref={closeBtn} type="button" className="icon-btn" onClick={onClose} aria-label="Close day details">
            <X size={18} aria-hidden />
          </button>
        </header>

        <div className="cal-drawer-body" aria-busy={loading && !day}>
          {error && !day ? (
            <div className="cal-banner cal-banner-error" role="alert">
              <b>This day could not be loaded.</b> {error}
              <div className="dim">No figures are shown rather than showing figures that may be wrong.</div>
            </div>
          ) : null}
          {!day && !error ? <div className="cal-drawer-skeleton" aria-label="Loading day details"><span /><span /><span /></div> : null}

          {day && !day.trades.length ? (
            <div className="cal-empty">
              <b>No closed trades on this day</b>
              <span className="dim">Nothing was realized by any source{Object.keys(day.filters).length ? " matching the current filters" : ""}.</span>
            </div>
          ) : null}

          {day && day.trades.length ? (
            <>
              {day.conversion.needed ? <p className="cal-note">{day.conversion.note}</p> : null}
              <section className="cal-section" aria-label="Day summary">
                <h3>Day summary</h3>
                <div className="cal-daysum-grid">
                  {day.currencies.map((c) => <SummaryBlock key={c} currency={c} summary={day.summary} />)}
                </div>
              </section>

              <section className="cal-section">
                <h3>Where it came from</h3>
                <ul className="cal-breakdown">
                  {day.sources.map((b) => (
                    <li key={b.key.join("|")}>
                      <div className="cal-breakdown-who">
                        <SourceChip source={b.key[0]} label={b.key[1]} />
                        <span className="cal-breakdown-name">
                          {b.key[2] ? <>{b.key[3] || "Not recorded"} <span className="mono dim" title={b.key[2]}>{shortId(b.key[2])}</span></>
                            : b.key[0] === "pa_lab" || b.key[0] === "smc_lab" ? "Paper account" : "No instance (signal engine)"}
                        </span>
                      </div>
                      <span className="dim">{b.closed_trades} closed{b.realizations > b.closed_trades ? ` · ${b.realizations - b.closed_trades} partial` : ""}</span>
                      <NetList byCurrency={b.by_currency} strong />
                    </li>
                  ))}
                </ul>
              </section>

              <section className="cal-section">
                <h3>By strategy</h3>
                <div className="cal-table-wrap">
                  <table className="data-table cal-table">
                    <thead><tr><th>Strategy</th><th>Trades</th><th>W / L</th><th>Net</th><th>Avg R/R</th></tr></thead>
                    <tbody>
                      {day.strategies.map((b) => (
                        <tr key={b.key.join("|")}>
                          <td>{b.key[0] || NOT_RECORDED}</td>
                          <td>{b.closed_trades}{b.realizations > b.closed_trades ? <span className="dim"> +{b.realizations - b.closed_trades} partial</span> : null}</td>
                          <td>{wl(b)}</td>
                          <td><NetList byCurrency={b.by_currency} /></td>
                          <td>{b.avg_rr ? `${Number(b.avg_rr).toFixed(2)}R` : NOT_RECORDED}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              <section className="cal-section">
                <h3>Time of day</h3>
                <ul className="cal-tod">
                  {day.time_of_day.map((b) => (
                    <li key={b.key} className={b.realizations ? "" : "is-empty"}>
                      <span className="cal-tod-label">{b.label}<span className="dim mono">{b.start}–{b.end}</span></span>
                      <span className="dim">{b.realizations ? `${b.realizations} exit${b.realizations === 1 ? "" : "s"}` : "No trades"}</span>
                      {b.realizations ? <NetList byCurrency={b.by_currency} /> : null}
                    </li>
                  ))}
                </ul>
                {day.hourly.length ? (
                  <details className="cal-hourly">
                    <summary>Hour by hour</summary>
                    <ul>
                      {day.hourly.map((h) => (
                        <li key={h.hour}>
                          <span className="mono">{String(h.hour).padStart(2, "0")}:00–{String(h.hour).padStart(2, "0")}:59</span>
                          <span className="dim">{h.realizations} exit{h.realizations === 1 ? "" : "s"}</span>
                          <NetList byCurrency={h.by_currency} />
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
              </section>

              <section className="cal-section">
                <h3>Trades <span className="dim">({day.trades.length} exit{day.trades.length === 1 ? "" : "s"})</span></h3>
                <div className="cal-table-wrap cal-trades-table">
                  <table className="data-table cal-table">
                    <thead>
                      <tr>
                        <th>Closed</th><th>Source · bot / lab</th><th>Strategy</th><th>Symbol · side</th>
                        <th>Net</th><th>Result</th><th>R/R</th><th>Entry</th><th>Exit</th><th>Duration</th><th>TF</th>
                        <th>Gross</th><th>Fees</th><th>Funding</th><th>Exit reason</th>
                      </tr>
                    </thead>
                    <tbody>{day.trades.map((t) => <TradeRow key={t.id} t={t} />)}</tbody>
                  </table>
                </div>
                <ul className="cal-trade-cards">{day.trades.map((t) => <TradeCard key={t.id} t={t} />)}</ul>
                <p className="cal-note">
                  R/R is the price move before costs in multiples of the initial stop distance, recorded only when the
                  trade had a stop. Net = gross − fees − funding paid + funding received; ledger trades store P&amp;L already net of
                  fees, and it is used as stored.
                </p>
              </section>
            </>
          ) : null}
        </div>
      </div>
    </div>,
    document.body,
  );
}
