import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart from "../chart/EChart";
import { formatMoney } from "../../lib/money";
import type { MonthDay, Money } from "./types";

// Mark colours, validated together on the card surface (#121214) with the
// dataviz palette checks: lightness band, chroma floor, colour-blind
// separation (deutan ΔE 10.8) and 3:1 contrast. Profit and loss are also told
// apart by position (above or below zero), never by colour alone. Text uses
// the text tokens, never these.
const PROFIT = "#1eae59";
const LOSS = "#c42f2f";
const CUMULATIVE = "#3b82f6";
const SURFACE = "#121214";

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Compact axis ticks: 1,250 -> 1.25K. Display only. */
function compact(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e6) return `${(n / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e3) return `${(n / 1e3).toFixed(a >= 1e4 ? 0 : 1)}K`;
  return String(Math.round(n * 100) / 100);
}

/**
 * Daily realized P&L for one currency: a column per trading day (up for a
 * profit, down for a loss) and the month's running total as a line on the same
 * axis. Every figure is the backend's; the running total arrives computed.
 */
export default function PnlChart({ currency, days, summary, monthLabel }: {
  currency: string;
  days: MonthDay[];
  summary: Money;
  monthLabel: string;
}) {
  const curve = useMemo(() => new Map((summary.cumulative ?? []).map((p) => [p.date, p])), [summary]);

  const option = useMemo<EChartsOption>(() => {
    const labels = days.map((d) => String(Number(d.date.slice(8))));
    const bars = days.map((d) => {
      const m = d.by_currency[currency];
      if (!m) return null;
      const v = Number(m.net);
      return {
        value: v,
        itemStyle: { color: v >= 0 ? PROFIT : LOSS, borderRadius: v >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] },
      };
    });
    const line = days.map((d) => {
      const p = curve.get(d.date);
      return p ? Number(p.cumulative) : null;
    });
    return {
      animation: !reducedMotion(),
      animationDuration: 280,
      grid: { left: 4, right: 12, top: 14, bottom: 2, containLabel: true },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow", shadowStyle: { color: "rgba(255,255,255,0.04)" } },
        backgroundColor: "#17171a",
        borderColor: "#2a2a2f",
        padding: [8, 10],
        textStyle: { color: "#ffffff", fontSize: 12 },
        formatter: (params: unknown) => {
          const first = (Array.isArray(params) ? params[0] : params) as { dataIndex: number };
          const d = days[first.dataIndex];
          const m = d.by_currency[currency];
          const p = curve.get(d.date);
          const date = new Date(`${d.date}T00:00:00Z`).toLocaleDateString("en-GB", {
            weekday: "short", day: "numeric", month: "short", timeZone: "UTC",
          });
          if (!m) return `<b>${date}</b><br/><span style="color:#b0b8c4">No trades</span>`;
          return `<b>${date}</b><br/>Daily net <b>${formatMoney(m.net, currency)}</b>` +
            `<br/><span style="color:#b0b8c4">${m.closed_trades} closed trade${m.closed_trades === 1 ? "" : "s"}</span>` +
            (p ? `<br/>Month to date <b>${formatMoney(p.cumulative, currency)}</b>` : "");
        },
      },
      xAxis: {
        type: "category",
        data: labels,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: "#2a2a2f" } },
        axisLabel: { color: "#8a93a6", fontSize: 10.5, interval: "auto" },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: "#1c1c20", width: 1 } },
        axisLabel: { color: "#8a93a6", fontSize: 10.5, formatter: (v: number) => compact(v) },
      },
      series: [
        { name: "Daily net", type: "bar", barMaxWidth: 24, data: bars },
        {
          name: "Month to date",
          type: "line",
          data: line,
          connectNulls: true,
          symbol: "circle",
          symbolSize: 8,
          lineStyle: { width: 2, color: CUMULATIVE, cap: "round", join: "round" },
          itemStyle: { color: CUMULATIVE, borderColor: SURFACE, borderWidth: 2 },
          z: 3,
        },
      ],
    };
  }, [days, currency, curve]);

  const trading = summary.trading_days ?? 0;
  const label = `Daily realized P&L in ${currency} for ${monthLabel}: ${trading} trading day${trading === 1 ? "" : "s"}, ` +
    `month net ${formatMoney(summary.net, currency)}` +
    (summary.best_day ? `, best day ${formatMoney(summary.best_day.net, currency)}` : "") +
    (summary.worst_day ? `, worst day ${formatMoney(summary.worst_day.net, currency)}` : "") +
    ". The calendar above lists every day's figure.";

  return (
    <figure className="cal-chart">
      <figcaption className="cal-chart-head">
        <span className="cal-chart-title">Daily realized P&amp;L · {currency === "UNKNOWN" ? "currency not recorded" : currency}</span>
        <span className="cal-chart-legend" aria-hidden>
          <span><i className="cal-key cal-key-split" /> Daily net (profit up, loss down)</span>
          <span><i className="cal-key cal-key-line" /> Month to date</span>
        </span>
      </figcaption>
      <div role="img" aria-label={label}>
        <EChart option={option} height={220} />
      </div>
    </figure>
  );
}
