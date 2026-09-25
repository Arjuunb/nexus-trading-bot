import { Reveal } from "@/components/Reveal";
import { useCountUp } from "@/hooks/useCountUp";

interface Metric {
  value: number;
  decimals?: number;
  prefix?: string;
  suffix?: string;
  label: string;
  sub: string;
}

// Counts taken from the platform's own configuration (data.historical SYMBOLS,
// services/mtf_policy TIMEFRAME_SECONDS, auto_engine HUB_MIN_SCORE default). The band used to show "99.9%
// uptime" and "<100ms order routing" -- neither was measured, and there is
// no live order routing to time.
const METRICS: Metric[] = [
  { value: 8, label: "Markets supported", sub: "BTC, ETH, SOL, XRP and more on Binance USDⓈ-M" },
  { value: 6, label: "Timeframes", sub: "1m to 1d, decided on closed candles only" },
  { value: 60, label: "Minimum setup score", sub: "Out of 100 by default; anything weaker is skipped and logged" },
];

function MetricStat({ m }: { m: Metric }) {
  const { ref, value } = useCountUp(m.value, 1600, m.decimals ?? 0);
  return (
    <div className="text-center">
      <p className="tabular text-5xl font-extrabold tracking-tight text-white sm:text-6xl">
        {m.prefix}
        <span ref={ref} className="text-gold-gradient">
          {value.toLocaleString(undefined, {
            minimumFractionDigits: m.decimals ?? 0,
            maximumFractionDigits: m.decimals ?? 0,
          })}
        </span>
        {m.suffix}
      </p>
      <p className="mt-2 text-base font-semibold text-white">{m.label}</p>
      <p className="mt-1 text-sm text-white/45">{m.sub}</p>
    </div>
  );
}

export function Performance() {
  return (
    <section id="performance" className="section">
      <div className="container-x">
        <Reveal>
          <div className="surface relative overflow-hidden px-6 py-16 sm:px-12">
            <div className="pointer-events-none absolute inset-0 bg-radial-fade" />
            <div className="relative grid gap-12 sm:grid-cols-3">
              {METRICS.map((m, i) => (
                <Reveal key={m.label} delay={i * 0.12}>
                  <MetricStat m={m} />
                </Reveal>
              ))}
            </div>
            <p className="relative mt-12 text-center text-xs text-white/35">
              Counts from the platform&apos;s current configuration — not a measure or guarantee of
              trading returns.
            </p>
          </div>
        </Reveal>
      </div>
    </section>
  );
}
