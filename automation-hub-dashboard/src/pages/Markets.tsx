import { useState } from "react";
import { ENTRY_TIMEFRAMES, DEFAULT_ENTRY_TIMEFRAME } from "../lib/timeframes";
import { usePref } from "../lib/prefs";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import Sparkline from "../components/chart/Sparkline";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { apiGet, useLive, hhmmss, type LedgerPosition, type SystemStatus, type Watchlist, type ScanResult } from "../lib/api";
import LoadDataButton from "../components/common/LoadDataButton";
import { useApp } from "../app-context";

const TFS = ["1d", "4h", "1h"] as const;
const fmt = (n?: number) => (n == null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: n >= 100 ? 2 : 4 }));

export default function MarketsPage() {
  const { go } = useApp();
  const { data: sys } = useLive<SystemStatus>("/system/status", 3000);
  const { data: positions } = useLive<LedgerPosition[]>("/paper/positions", 3000);
  const [tf, setTf] = usePref<(typeof TFS)[number]>("markets.chartTf", "1d");

  const symbols = sys?.symbols ?? [];
  const symParam = symbols.length ? symbols.join(",") : "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT";
  const watch = useLive<Watchlist>(`/markets/watchlist?symbols=${symParam}&timeframe=${tf}`, 15000);
  const posBySym = new Map((positions ?? []).map((p) => [p.symbol, p]));
  const rows = watch.data?.symbols ?? [];
  const anyReal = rows.some((r) => r.available);

  return (
    <>
      <PageHeader
        title="Markets"
        subtitle="Symbols the engine tracks · real Binance quotes from the cached candle store"
        actions={<button className="btn btn-soft btn-sm" onClick={() => go("Symbols")}><Icon name="info" size={13} /> Symbol Explorer</button>}
      />

      <div className="stat-row">
        <StatCard label="Engine" value={sys?.engine_running ? "Running" : "Stopped"} tone={sys?.engine_running ? "green" : "red"} />
        <StatCard label="Mode" value={(sys?.mode ?? "paper").toUpperCase()} />
        <StatCard label="Tracked Symbols" value={String(symbols.length)} />
        <StatCard label="Open Positions" value={String((positions ?? []).length)} />
      </div>

      <div className="toolbar">
        <div className="chips">
          {TFS.map((t) => (
            <button key={t} className={`chip-btn ${tf === t ? "active" : ""}`} onClick={() => setTf(t)}>{t} change</button>
          ))}
        </div>
      </div>

      <div className="watch-grid">
        {rows.map((w) => {
          const p = posBySym.get(w.symbol);
          const up = (w.change_pct ?? 0) >= 0;
          return (
            <div className="watch-card" key={w.symbol} style={{ ["--wc-accent" as any]: up ? "var(--green)" : "var(--red)" }}>
              <div className="watch-top">
                <div className="watch-sym">
                  <span className="watch-avatar">{w.symbol.replace("USDT", "").slice(0, 3)}</span>
                  <div>
                    <b>{w.symbol.replace("USDT", "")}</b><span className="dim"> /USDT</span>
                    <div style={{ fontSize: 11, marginTop: 2 }}>{p ? <Badge text={`${p.side} open`} tone={p.side === "long" ? "green" : "red"} /> : <span className="dim">no position</span>}</div>
                  </div>
                </div>
                {w.available
                  ? <Badge text={`${up ? "+" : ""}${w.change_pct}%`} tone={up ? "green" : "red"} />
                  : <Badge text="no data" tone="default" />}
              </div>

              {w.available ? (
                <>
                  <div className="watch-price">{fmt(w.last)}</div>
                  <div className="watch-spark">
                    <Sparkline data={w.spark ?? []} color={up ? "#22c55e" : "#ef4444"} height={44} />
                  </div>
                  <div className="watch-foot">
                    <span><span className="dim">Volatility</span> <b>{w.vol_pct}%</b></span>
                    {p && <span><span className="dim">Entry</span> <b>{fmt(p.entry)}</b></span>}
                    {p && <span className="dim mono">{hhmmss(p.opened_at)}</span>}
                  </div>
                </>
              ) : (
                <div className="watch-empty">
                  <Icon name="info" size={14} className="dim" /> No cached candles for {w.symbol} ({tf}).
                  <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>Sync it on Replay or run /data/sync.</div>
                </div>
              )}
            </div>
          );
        })}
        {rows.length === 0 && <div className="dim ta-center" style={{ padding: 24, gridColumn: "1/-1" }}>{watch.loading ? "Loading quotes…" : "No symbols configured."}</div>}
      </div>

      {!anyReal && rows.length > 0 && (
        <Card title="">
          <p className="dim" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <Icon name="info" size={14} /> No real Binance candles are cached yet — quotes show "no data" rather than faked numbers.
          </p>
          <div style={{ marginTop: 8 }}>
            <LoadDataButton onDone={() => watch.refetch()} />
          </div>
        </Card>
      )}

      <OpportunityScanner symbols={symParam} />
    </>
  );
}

const SCAN_TFS = ENTRY_TIMEFRAMES;
const sideTone = (s: string) => (s === "long" ? "green" : s === "short" ? "red" : "default");
const strColor = (n: number) => (n >= 70 ? "var(--green)" : n >= 50 ? "var(--gold)" : "var(--dim-2)");

function OpportunityScanner({ symbols }: { symbols: string }) {
  const [tf, setTf] = useState<string>(DEFAULT_ENTRY_TIMEFRAME);
  const [data, setData] = useState<ScanResult | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    try { setData(await apiGet<ScanResult>(`/scanner/scan?symbols=${symbols}&timeframe=${tf}&bars=300`)); }
    catch { /* ignore */ } finally { setBusy(false); }
  };

  return (
    <Card title="Opportunity Scanner" subtitle="breakouts · sweeps · volume · momentum · trend · pullbacks — ranked from the cached candles, at the age shown"
      right={<div className="row-actions" style={{ gap: 6 }}>
        <select value={tf} onChange={(e) => setTf(e.target.value)}>{SCAN_TFS.map((t) => <option key={t}>{t}</option>)}</select>
        <button className="btn btn-primary" disabled={busy} onClick={run}><Icon name="target" size={14} /> {busy ? "Scanning…" : "Scan"}</button>
      </div>}>
      {!data ? (
        <div className="dim ta-center" style={{ padding: 16 }}>Run a scan to rank setups across your symbols.</div>
      ) : (data.opportunities?.length ?? 0) === 0 ? (
        <div className="ta-center" style={{ padding: 16 }}>
          <div className="dim">No setups in these candles ({(data.symbols ?? []).filter((s) => s.available).length} symbols scanned). Try another timeframe.</div>
          {/* Without this, a scan over day-old candles and a scan that genuinely
              found nothing are the same empty box. */}
          <StaleNote data={data} />
        </div>
      ) : (
        <div className="scan-grid">
          {(data.opportunities ?? []).slice(0, 12).map((o, i) => (
            <div className="scan-card" key={i} style={{ ["--sc-accent" as any]: strColor(o.strength) }}>
              <div className="row-actions" style={{ justifyContent: "space-between" }}>
                <b>{o.symbol?.replace("USDT", "")}</b>
                <Badge text={o.side} tone={sideTone(o.side) as any} />
              </div>
              <div className="scan-type">{o.type}</div>
              <div className="scan-strength">
                <span className="scan-bar"><span className="scan-bar-fill" style={{ width: `${o.strength}%`, background: strColor(o.strength) }} /></span>
                <b style={{ color: strColor(o.strength) }}>{o.strength}</b>
              </div>
              <span className="dim" style={{ fontSize: 11 }}>{o.detail}</span>
              {o.stale ? (
                <span className="scan-stale" title={`Computed from candles that closed ${fmtAge(o.as_of)} ago`}>
                  <Icon name="alert" size={11} /> {fmtAge(o.as_of)} old
                </span>
              ) : null}
            </div>
          ))}
        </div>
      )}
      {data && (data.opportunities?.length ?? 0) > 0 ? <StaleNote data={data} /> : null}
    </Card>
  );
}

/** How long ago a candle closed, in the coarsest unit that stays honest. */
function fmtAge(iso?: string | null): string {
  if (!iso) return "unknown";
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "unknown";
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = ms / 3600000;
  return h < 48 ? `${h.toFixed(1)}h` : `${Math.round(h / 24)}d`;
}

/** What the ranking was actually computed on.
 *
 * The scanner reads a local candle cache that nothing refreshes on a schedule
 * -- it was measured 15.8h behind for BTCUSDT and empty for BNBUSDT. The page
 * used to present that as "live setups" with a last price. It reports the age
 * the server measured; it never decides freshness itself. */
function StaleNote({ data }: { data: ScanResult }) {
  const stale = data.stale_symbols ?? [];
  if (!stale.length) return null;
  const missing = (data.symbols ?? []).filter((r) => r.stale && !r.available).map((r) => r.symbol);
  const behind = (data.symbols ?? []).filter((r) => r.stale && r.available);
  const oldest = behind.reduce<string | null>(
    (acc, r) => (!acc || (r.as_of ?? "") < acc ? r.as_of ?? acc : acc), null);
  return (
    <div className="scan-stale-note">
      <Icon name="alert" size={13} />
      <span>
        <b>Not current.</b>{" "}
        {behind.length ? <>{behind.length} of {(data.symbols ?? []).length} symbols ranked from candles up to <b>{fmtAge(oldest)}</b> old ({behind.map((r) => r.symbol.replace("USDT", "")).join(", ")}). </> : null}
        {missing.length ? <>No candles at all for {missing.map((s) => s.replace("USDT", "")).join(", ")}. </> : null}
        Refresh the cache with <code>/data/sync</code> — nothing does it on a schedule.
      </span>
    </div>
  );
}
