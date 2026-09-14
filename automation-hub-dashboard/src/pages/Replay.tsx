import { useEffect, useRef, useState } from "react";
import { ENTRY_TIMEFRAMES } from "../lib/timeframes";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader } from "../components/common/ui";
import CandleChart, { type ChartToggles } from "../components/replay/CandleChart";
import { useApp } from "../app-context";
import { apiGet, apiPost, type ReplayData, type ReplayFrame, type ReplayTrade } from "../lib/api";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"];
const SPEEDS = [1, 2, 5, 10, 25];
const MACRO_TFS = ["1w", "1d", "4h"];
const CONF_TFS = ["1d", "4h", "15m"];
const trendTone = (v?: string) => (v === "Bullish" ? "green" : v === "Bearish" ? "red" : "default");

const DEFAULT_TOGGLES: ChartToggles = {
  ema8: false, ema20: true, ema30: false, ema50: true, sma20: false, sma50: false,
  vwap: true, bb: false, volume: true, structure: true, zones: true, osc: "rsi",
};
const OVERLAY_TOGGLES: { key: keyof ChartToggles; label: string }[] = [
  { key: "ema8", label: "EMA8" }, { key: "ema20", label: "EMA20" }, { key: "ema30", label: "EMA30" },
  { key: "ema50", label: "EMA50" }, { key: "sma20", label: "SMA20" }, { key: "sma50", label: "SMA50" },
  { key: "vwap", label: "VWAP" }, { key: "bb", label: "Bollinger" }, { key: "volume", label: "Volume" },
  { key: "structure", label: "Structure" }, { key: "zones", label: "Zones" },
];

export default function ReplayPage() {
  const app = useApp();
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [tf, setTf] = useState("15m");
  const [strategy, setStrategy] = useState("Supply/Demand");
  const [src, setSrc] = useState("binance");
  const [limit, setLimit] = useState(800);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [macro, setMacro] = useState("1w");
  const [confirmation, setConfirmation] = useState("4h");
  const [strategies, setStrategies] = useState<{ id: string; name: string; version: string; description: string }[]>([]);
  const [data, setData] = useState<ReplayData | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(5);
  const [toggles, setToggles] = useState<ChartToggles>(DEFAULT_TOGGLES);
  const [fullscreen, setFullscreen] = useState(false);
  const [memoryFilter, setMemoryFilter] = useState(false);
  const [realistic, setRealistic] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    apiGet<{ strategies: any[] }>("/strategies/registry").then((r) => setStrategies(r.strategies)).catch(() => {});
  }, []);

  const load = async () => {
    setLoading(true); setPlaying(false);
    try {
      let q = `/replay/run?symbol=${symbol}&timeframe=${tf}&limit=${limit}&strategy=${encodeURIComponent(strategy)}&source=${src}`;
      q += `&macro=${macro}&confirmation=${confirmation}`;
      if (memoryFilter) q += `&memory_filter=true`;
      if (realistic) q += `&realistic=true`;
      if (startDate) q += `&start=${startDate}`;
      if (endDate) q += `&end=${endDate}`;
      const r = await apiGet<ReplayData>(q);
      if (r.meta.bars === 0) { app.toast(r.meta.data_warning || "No data in that range.", "info"); }
      else if (r.meta.data_warning) { app.toast(r.meta.data_warning, "info"); }
      // A substituted timeframe means the replayed strategy is not the strategy
      // that was backtested — that must not pass unremarked.
      if (r.meta.timeframe_substituted && r.meta.timeframe_note) {
        app.toast(r.meta.timeframe_note, "info");
      }
      setData(r); setIdx(0);
    } catch { app.toast("Replay failed — backend reachable?", "error"); }
    finally { setLoading(false); }
  };

  const toggle = (k: keyof ChartToggles) => setToggles((t) => ({ ...t, [k]: !t[k] }));
  const jumpTrade = (dir: 1 | -1) => {
    if (!data) return;
    setPlaying(false);
    const entries = data.trades.map((t) => t.entry_idx).sort((a, b) => a - b);
    const next = dir > 0 ? entries.find((e) => e > idx) : [...entries].reverse().find((e) => e < idx);
    if (next !== undefined) setIdx(next);
    else app.toast(dir > 0 ? "No later trade." : "No earlier trade.", "info");
  };

  const syncBinance = async () => {
    setSyncing(true);
    try {
      app.toast(`Fetching real ${symbol} ${tf} candles from Binance…`, "info");
      const r = await apiPost<any>(`/data/sync?symbol=${symbol}&timeframe=${tf}&target_candles=5000`);
      if (r?.error) app.toast(`Binance fetch failed: ${r.error}`, "error");
      else if (r?.detail) app.toast(r.detail, "error");
      else { app.toast(`Cached ${r.stored ?? r.candles ?? 0} real ${symbol} ${tf} candles.`, "success"); await load(); }
    } catch { app.toast("Sync needs the webhook secret (or no network on this host).", "error"); }
    finally { setSyncing(false); }
  };

  // playback loop
  useEffect(() => {
    if (!playing || !data) return;
    const ms = Math.max(40, 600 / speed);
    timer.current = window.setInterval(() => {
      setIdx((i) => {
        if (i >= data.candles.length - 1) { setPlaying(false); return i; }
        return i + 1;
      });
    }, ms);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [playing, speed, data]);

  const frame: ReplayFrame | null = data ? data.frames[idx] ?? null : null;
  const candle = data?.candles[idx];
  const visibleEvents = data ? data.events.filter((e) => e.idx <= idx).slice(-14).reverse() : [];
  const active = data?.trades?.find((t) => t.entry_idx <= idx && (t.exit_idx === null || t.exit_idx > idx));
  const lastClosed = data ? [...data.trades].reverse().find((t) => t.exit_idx !== null && t.exit_idx <= idx) : undefined;

  // live RR + status on the active trade
  let liveRR: number | null = null;
  if (active && candle) {
    const risk = Math.abs(active.entry - active.sl);
    const move = active.side === "long" ? candle.c - active.entry : active.entry - candle.c;
    liveRR = risk > 0 ? Math.round((move / risk) * 100) / 100 : null;
  }
  const tradeStatus = active
    ? (active.tp1_idx !== null && active.tp1_idx <= idx ? "Partial TP / Break-even" : "Open")
    : null;

  // current decision — the single state the brain is in on this candle
  const justExited = data ? data.trades.find((t) => t.exit_idx === idx) : undefined;
  const decision: { text: string; tone: "green" | "red" | "amber" | "default" } = justExited
    ? { text: "Exit trade", tone: justExited.result === "Winner" ? "green" : "red" }
    : active && active.entry_idx === idx
      ? { text: `Enter ${active.side}`, tone: active.side === "long" ? "green" : "red" }
      : active
        ? { text: "Manage trade", tone: "default" }
        : frame?.blocked
          ? { text: `Trade blocked — ${frame.reason}`, tone: "red" }
          : frame?.trigger === "Entry Confirmed"
            ? { text: "Enter — confirming", tone: "green" }
            : frame?.trigger === "Setup Found"
              ? { text: "Setup forming", tone: "amber" }
              : { text: "Waiting", tone: "default" };

  return (
    <>
      <PageHeader title="Strategy Replay" subtitle="Watch the bot analyse real history candle-by-candle — no lookahead" />

      <Card title="Load Replay"
        right={data ? <Badge text={data.meta.data_is_real ? "Binance data" : (data.meta.data_source_label ?? data.meta.data_source)}
          tone={data.meta.data_is_real ? "green" : "amber"} /> : undefined}>
        <div className="row-actions" style={{ justifyContent: "flex-start", gap: 10, flexWrap: "wrap" }}>
          <label className="row-actions" style={{ gap: 4 }}><span className="dim" style={{ fontSize: 11 }}>Strategy</span>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)} style={{ minWidth: 170 }}>
              {(strategies.length ? strategies.map((s) => s.name) : [strategy]).map((s) => <option key={s}>{s}</option>)}
            </select></label>
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{SYMBOLS.map((s) => <option key={s}>{s}</option>)}</select>
          <select value={tf} onChange={(e) => setTf(e.target.value)}>{ENTRY_TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select>
          <label className="row-actions" style={{ gap: 4 }}><span className="dim" style={{ fontSize: 11 }}>Data</span>
            <select value={src} onChange={(e) => setSrc(e.target.value)}>
              <option value="binance">Binance historical</option>
              <option value="demo">Demo sample</option>
            </select></label>
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>{[500, 800, 1200, 1500].map((b) => <option key={b} value={b}>{b} bars</option>)}</select>
          <label className="row-actions" style={{ gap: 4 }}><span className="dim">From</span>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} /></label>
          <label className="row-actions" style={{ gap: 4 }}><span className="dim">To</span>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} /></label>
          <label className="row-actions" style={{ gap: 4 }} title="Macro timeframe used by the entry gate">
            <span className="dim" style={{ fontSize: 11 }}>Macro</span>
            <select value={macro} onChange={(e) => setMacro(e.target.value)}>{MACRO_TFS.map((t) => <option key={t}>{t}</option>)}</select></label>
          <label className="row-actions" style={{ gap: 4 }} title="Confirmation timeframe used by the entry gate">
            <span className="dim" style={{ fontSize: 11 }}>Confirm</span>
            <select value={confirmation} onChange={(e) => setConfirmation(e.target.value)}>{CONF_TFS.map((t) => <option key={t}>{t}</option>)}</select></label>
          <button className={`chip-btn ${memoryFilter ? "active" : ""}`} onClick={() => setMemoryFilter((m) => !m)}
            title="Skip this strategy's historically-weak regimes (from its saved memory snapshot)">Memory filter</button>
          <button className={`chip-btn ${realistic ? "active" : ""}`} onClick={() => setRealistic((m) => !m)}
            title="Charge spread + slippage + latency on each trade (same fill model as the paper engine)">Realistic fills</button>
          <button className="btn btn-primary" disabled={loading} onClick={load}><Icon name="history" size={14} /> {loading ? "Loading…" : "Load"}</button>
          {src === "binance" && (
            <button className="btn btn-soft" disabled={syncing} onClick={syncBinance}
              title="Download real Binance candles for this symbol/timeframe and cache them locally">
              <Icon name="refresh" size={14} /> {syncing ? "Syncing…" : "Sync Binance"}
            </button>
          )}
        </div>
        {data?.meta.timeframe_substituted && (
          <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>
            <Icon name="warning" size={11} /> {data.meta.timeframe_note}
          </div>
        )}

        {data && !data.meta.data_is_real && src === "binance" && (
          <div className="card" style={{ marginTop: 8, borderColor: "#f59e0b", background: "#f59e0b14" }}>
            <Icon name="warning" size={14} className="amber" /> Not real Binance data yet — click <b>Sync Binance</b> to download and cache real candles (needs network + webhook secret).
          </div>
        )}
        <p className="dim" style={{ marginTop: 6, fontSize: 12 }}>
          Leave dates blank for the most recent window. Date ranges need a live data source
          (HUB_USE_LIVE_DATA) to jump to arbitrary months; otherwise they filter the available history.
        </p>
        {data && (
          <p className="dim" style={{ marginTop: 8 }}>
            <b style={{ color: data.meta.data_is_real ? "#22c55e" : "#f59e0b" }}>{data.meta.data_source_label}</b> ·{" "}
            {(data.meta.start ?? "").slice(0, 16).replace("T", " ")} → {(data.meta.end ?? "").slice(0, 16).replace("T", " ")} ·
            HTF: {Object.entries(data.meta.htf_available ?? {}).map(([k, v]) => `${k} ${v ? "✓" : "n/a"}`).join(" · ")}
          </p>
        )}
        {data?.meta?.data_warning && (
          <div className="card" style={{ marginTop: 8, borderColor: "#f59e0b", background: "#f59e0b14" }}>
            <Icon name="warning" size={14} className="amber" /> {data.meta.data_warning}
          </div>
        )}
        {data?.meta?.debug && (
          <details style={{ marginTop: 8 }}>
            <summary className="dim" style={{ cursor: "pointer", fontSize: 12 }}>Debug — engine wiring</summary>
            <div className="dim mono" style={{ fontSize: 11, marginTop: 6, lineHeight: 1.6 }}>
              strategy_id: <b>{data.meta.debug.strategy_id}</b> · class: <b>{data.meta.debug.strategy_class}</b> ·
              candles: {data.meta.debug.candles_loaded} (+{data.meta.debug.warmup_bars} warmup) ·
              trades: {data.meta.debug.trades_generated} · source: {data.meta.debug.data_source} ·
              MTF: {data.meta.debug.mtf_timeframes.join("/") || "—"} ·
              gate: {(data.meta.debug.gate_timeframes ?? []).join("/") || "—"} ·
              {(data.meta.debug.memory_filter?.length ?? 0) > 0 && <> DNA-skip: {data.meta.debug.memory_filter!.join("/")} ·</>}
              indicators: {(data.meta.debug.indicators ?? []).length} · {data.meta.debug.computed_at.slice(11, 19)}
              {data.meta.debug.error && <span className="neg"> · error: {data.meta.debug.error}</span>}
            </div>
          </details>
        )}
      </Card>

      {!data ? (
        <Card title=""><div className="dim ta-center" style={{ padding: 30 }}>Pick an asset and timeframe, then <b>Load</b> to replay real market history.</div></Card>
      ) : data.meta.bars === 0 ? (
        <Card title="">
          <div className="ta-center" style={{ padding: 30 }}>
            <Icon name="warning" size={22} className="amber" />
            <div style={{ fontWeight: 600, marginTop: 8, fontSize: 15 }}>
              {data.meta.needs_download ? "Historical data missing. Download data first." : (data.meta.note || "No data in the selected date range.")}
            </div>
            <p className="dim" style={{ marginTop: 6 }}>
              Replay uses real Binance history only — it never falls back to synthetic data.
              {data.meta.needs_download && <> Download {symbol} {tf} candles, or switch <b>Data</b> to Demo sample.</>}
            </p>
            {data.meta.needs_download && (
              <button className="btn btn-primary" disabled={syncing} onClick={syncBinance} style={{ marginTop: 6 }}>
                <Icon name="refresh" size={14} /> {syncing ? "Downloading…" : `Download ${symbol} ${tf} from Binance`}
              </button>
            )}
          </div>
        </Card>
      ) : (
        <>
          {/* ── flagship layout · LEFT chart+controls / RIGHT brain stack ── */}
          <div className="replay-main" style={{ display: "grid", gridTemplateColumns: fullscreen ? "1fr" : "minmax(0,2.2fr) minmax(320px,1fr)", gap: 14, alignItems: "start" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
              <Card title={`${symbol} · ${tf}`} subtitle={data.meta.data_source_label}
                right={<button className="btn btn-soft" onClick={() => setFullscreen((f) => !f)} title="Toggle wide chart">
                  <Icon name="external" size={14} /> {fullscreen ? "Exit" : "Fullscreen"}</button>}>
                <CandleChart data={data} index={idx} toggles={toggles} height={fullscreen ? 700 : 520} />
              </Card>

              <Card title="">
                <div className="row-actions" style={{ justifyContent: "flex-start", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                  <button className="btn btn-soft" onClick={() => { setPlaying(false); setIdx(0); }} title="Reset to the first candle"><Icon name="refresh" size={14} /> Reset</button>
                  <button className="btn btn-soft" onClick={() => jumpTrade(-1)} title="Jump to previous trade entry"><Icon name="skipBack" size={14} /> Trade</button>
                  <button className="btn btn-soft" onClick={() => setIdx((i) => Math.max(0, i - 1))}><Icon name="chevron" size={14} /> Step ←</button>
                  <button className="btn btn-primary" onClick={() => setPlaying((p) => !p)}>
                    <Icon name={playing ? "pause" : "play"} size={14} /> {playing ? "Pause" : "Play"}
                  </button>
                  <button className="btn btn-soft" onClick={() => setIdx((i) => Math.min(data.candles.length - 1, i + 1))}>Step → <Icon name="chevron" size={14} /></button>
                  <button className="btn btn-soft" onClick={() => jumpTrade(1)} title="Jump to next trade entry">Trade <Icon name="skipForward" size={14} /></button>
                  <span className="dim">Speed</span>
                  {SPEEDS.map((s) => <button key={s} className={`chip-btn ${speed === s ? "active" : ""}`} onClick={() => setSpeed(s)}>{s}x</button>)}
                  <span className="dim mono" style={{ marginLeft: "auto" }}>{idx + 1} / {data.candles.length} · {(candle?.t ?? "").replace("T", " ").slice(0, 16)}</span>
                </div>
                <input type="range" min={0} max={data.candles.length - 1} value={idx} onChange={(e) => { setPlaying(false); setIdx(Number(e.target.value)); }} style={{ width: "100%", marginTop: 10 }} />
                <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, flexWrap: "wrap", marginTop: 10, alignItems: "center" }}>
                  <span className="dim" style={{ fontSize: 11 }}>Indicators</span>
                  {OVERLAY_TOGGLES.map((o) => (
                    <button key={o.key} className={`chip-btn ${toggles[o.key] ? "active" : ""}`} onClick={() => toggle(o.key)}>{o.label}</button>
                  ))}
                  <span className="dim" style={{ fontSize: 11, marginLeft: 8 }}>Oscillator</span>
                  {(["none", "rsi", "macd", "atr"] as const).map((o) => (
                    <button key={o} className={`chip-btn ${toggles.osc === o ? "active" : ""}`} onClick={() => setToggles((t) => ({ ...t, osc: o }))}>{o === "none" ? "Off" : o.toUpperCase()}</button>
                  ))}
                </div>
              </Card>
            </div>

            {!fullscreen && (
              <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
                <StrategyState data={data} />
                <BrainPanel frame={frame} active={active} liveRR={liveRR} status={tradeStatus} decision={decision} />
                <TradeReview trade={lastClosed} />
              </div>
            )}
          </div>

          {/* ── BOTTOM zone · timeline / trade history / metrics ── */}
          <div className="replay-bottom" style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 14 }}>
            <Card title="Decision Timeline" subtitle="every candle generates reasoning">
              <div className="alert-stack" style={{ maxHeight: 300, overflowY: "auto" }}>
                {visibleEvents.length === 0 ? <div className="dim">Press Play to watch the bot think.</div> :
                  visibleEvents.map((e, i) => (
                    <div key={i} className="exec-line">
                      <span className="exec-time">{(data.candles[e.idx]?.t ?? "").slice(11, 16)}</span>{" "}
                      <EventDot kind={e.kind} /> {e.text}
                    </div>
                  ))}
              </div>
            </Card>

            <TradeHistory data={data} idx={idx} onJump={(i) => { setPlaying(false); setIdx(i); }} />

            <StatsPanel data={data} />
          </div>
        </>
      )}
    </>
  );
}

function EventDot({ kind }: { kind: string }) {
  const c = kind === "entry" ? "#22c55e" : kind === "exit" ? "#3b82f6" : kind === "blocked" ? "#ef4444"
    : kind === "sweep" || kind === "structure" || kind === "fvg" ? "#eab54f" : "#7c8798";
  return <span style={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", background: c, marginRight: 4 }} />;
}

function StrategyState({ data }: { data: ReplayData }) {
  const m = data.meta; const d = m.debug;
  const gate = (d?.gate_timeframes ?? []).join(" + ") || "—";
  return (
    <Card title="Strategy State" right={<Badge text={m.data_is_real ? "Live data" : "Demo"} tone={m.data_is_real ? "green" : "amber"} />}>
      <div className="risk-list">
        <div className="risk-item"><span className="dim">Strategy</span> <b>{m.strategy}</b></div>
        <div className="risk-item"><span className="dim">Market</span> <b>{m.symbol} · {m.timeframe}</b></div>
        <div className="risk-item"><span className="dim">MTF gate</span> <b>{gate}</b></div>
        <div className="risk-item"><span className="dim">Candles loaded</span> <b>{m.bars}</b></div>
        <div className="risk-item"><span className="dim">Engine</span> <b className="mono" style={{ fontSize: 11 }}>{d?.strategy_id ?? "—"}</b></div>
      </div>
    </Card>
  );
}

function TradeHistory({ data, idx, onJump }: { data: ReplayData; idx: number; onJump: (i: number) => void }) {
  const trades = [...data.trades].reverse();
  return (
    <Card title="Trade History" subtitle={`${data.trades.length} trades · click to jump`}>
      <div style={{ maxHeight: 300, overflowY: "auto" }}>
        {trades.length === 0 ? <div className="dim">No trades generated for this run.</div> :
          trades.map((t) => {
            const closed = t.exit_idx !== null;
            const seen = t.entry_idx <= idx;
            return (
              <button key={t.id} onClick={() => onJump(t.entry_idx)} className="trade-row"
                style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%", gap: 8,
                  padding: "7px 9px", borderRadius: 10, border: "1px solid var(--card-border-soft)",
                  background: "transparent", marginBottom: 6, cursor: "pointer", textAlign: "left", opacity: seen ? 1 : 0.55 }}>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Badge text={t.side} tone={t.side === "long" ? "green" : "red"} />
                  <span className="dim mono" style={{ fontSize: 11 }}>#{t.id}</span>
                </span>
                <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span className="dim" style={{ fontSize: 11 }}>{t.score}/100</span>
                  {closed
                    ? <b className={t.rr! >= 0 ? "pos" : "neg"} style={{ minWidth: 52, textAlign: "right" }}>{t.rr! >= 0 ? "+" : ""}{t.rr}R</b>
                    : <Badge text="Open" tone="default" />}
                </span>
              </button>
            );
          })}
      </div>
    </Card>
  );
}

function BrainPanel({ frame, active, liveRR, status, decision }: {
  frame: any; active?: ReplayTrade; liveRR: number | null; status: string | null;
  decision: { text: string; tone: "green" | "red" | "amber" | "default" };
}) {
  if (!frame) return <Card title="Bot Brain"><div className="dim">—</div></Card>;
  const trigTone = frame.trigger === "Entry Confirmed" ? "green" : frame.trigger === "Setup Found" ? "amber" : "default";
  const volText = frame.vol_ratio >= 1.2 ? "Above average" : frame.vol_ratio >= 0.8 ? "Normal" : "Thin";
  const mreg = frame.market_regime ?? frame.regime;
  const mregTone = mreg === "Bull trend" ? "green" : mreg === "Bear trend" ? "red"
    : mreg === "Choppy market" || mreg === "High volatility" ? "amber" : "default";
  return (
    <Card title="Bot Brain" right={<Badge text={mreg} tone={mregTone as any} />}>
      <div className="card" style={{ marginBottom: 10, padding: "8px 10px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span className="dim" style={{ fontSize: 12 }}>Current decision</span>
        <Badge text={decision.text} tone={decision.tone} />
      </div>
      <div className="risk-list">
        {["Weekly", "Daily", "4H", "15M"].map((tf) => (
          <div className="risk-item" key={tf}><span className="dim">{tf} trend</span> <Badge text={frame.trends[tf] ?? "n/a"} tone={trendTone(frame.trends[tf])} /></div>
        ))}
        <div className="risk-item"><span className="dim">5M trigger</span> <Badge text={frame.trigger} tone={trigTone as any} /></div>
        <div className="risk-item"><span className="dim">Volatility</span> <Badge text={frame.regime} tone={frame.regime?.includes("Volatility") ? "amber" : "default"} /></div>
        <div className="risk-item"><span className="dim">Volume</span> <Badge text={`${volText} (${frame.vol_ratio}×)`} tone={frame.vol_ratio >= 1.2 ? "green" : "default"} /></div>
      </div>

      <div style={{ marginTop: 10 }}>
        <div className="card-subtitle" style={{ marginBottom: 6 }}>
          Trade Quality {frame.score ? <b style={{ color: frame.score >= 75 ? "#089981" : frame.score >= 60 ? "#f59e0b" : "#f23645" }}>{frame.score}/100</b> : <span className="dim">no setup</span>}
        </div>
        {frame.breakdown && Object.entries(frame.breakdown).map(([k, v]: any) => (
          <div key={k} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
            <span className="dim" style={{ width: 130, fontSize: 11 }}>{k}</span>
            <div style={{ flex: 1, height: 6, background: "#161618", borderRadius: 3 }}>
              <div style={{ width: `${Math.min(100, (v / 25) * 100)}%`, height: 6, background: "#eab54f", borderRadius: 3 }} />
            </div>
            <b style={{ width: 24, textAlign: "right", fontSize: 11 }}>+{v}</b>
          </div>
        ))}
        {frame.blocked && <div className="risk-item" style={{ marginTop: 6 }}><Badge text={`Trade Blocked — ${frame.reason}`} tone="red" /></div>}
      </div>

      {active && (
        <div style={{ marginTop: 10, borderTop: "1px solid #1b1b1f", paddingTop: 8 }}>
          <div className="card-subtitle" style={{ marginBottom: 4 }}>Open {active.side} #{active.id}</div>
          <div className="risk-item"><span className="dim">Status</span>
            <Badge text={status ?? "Open"} tone={status?.includes("Partial") ? "amber" : "default"} /></div>
          <div className="risk-item"><span className="dim">Current RR</span> <b className={(liveRR ?? 0) >= 0 ? "pos" : "neg"}>{liveRR !== null ? `${liveRR >= 0 ? "+" : ""}${liveRR}R` : "—"}</b></div>
          <div className="risk-item"><span className="dim">{status?.includes("Partial") ? "SL→BE / TP" : "SL / TP"}</span> <b>{status?.includes("Partial") ? active.entry : active.sl} / {active.tp}</b></div>
        </div>
      )}
    </Card>
  );
}

function reviewInsights(trade: ReplayTrade) {
  const bd = trade.breakdown || {};
  const entries = Object.entries(bd);
  const best = entries.length ? entries.reduce((a, b) => (b[1] > a[1] ? b : a)) : null;
  const worst = entries.length ? entries.reduce((a, b) => (b[1] < a[1] ? b : a)) : null;
  const win = trade.result === "Winner";
  const helped = win
    ? best ? `${best[0]} was strongest (+${best[1]})${trade.mtf?.aligned ? " with higher-timeframe alignment" : ""}.`
      : "Confluence held through to target."
    : trade.mtf?.aligned ? "Higher-timeframe gate was aligned — the read wasn't the problem."
      : "Entry passed the score gate but the trade still lost.";
  const failed = win
    ? worst && worst[1] <= 8 ? `Weakest input was ${worst[0]} (+${worst[1]}) — the edge was narrower than it looks.`
      : "Nothing material — a clean trade."
    : trade.loss_analysis || (worst ? `${worst[0]} was the weakest input (+${worst[1]}).` : "Setup invalidated.");
  let improve: string;
  if (win) improve = (trade.rr ?? 0) >= 2 ? "Repeatable — log this setup; consider scaling size on this confluence."
    : "Winner but small RR — let runners breathe or tighten the entry for a better stop.";
  else if ((trade.bars_held ?? 9) <= 2) improve = "Stopped fast — wait for a confirmation candle / retest before entering.";
  else if (worst && worst[0] === "Volatility Condition") improve = "Add a regime filter — skip choppy / ranging conditions.";
  else if (worst && worst[0] === "Volume Confirmation") improve = "Require above-average volume on the trigger candle.";
  else if (worst && worst[0] === "Trend Alignment") improve = "Tighten the multi-timeframe gate so entries follow the higher trend.";
  else improve = "Raise the minimum quality score for this strategy to filter marginal setups.";
  return { helped, failed, improve };
}

function TradeReview({ trade }: { trade?: ReplayTrade }) {
  if (!trade) return <Card title="Trade Review"><div className="dim ta-center" style={{ padding: 24 }}>No closed trade yet — keep replaying.</div></Card>;
  const win = trade.result === "Winner";
  const ins = reviewInsights(trade);
  return (
    <Card title={`Trade Review #${trade.id}`} right={<Badge text={trade.result} tone={win ? "green" : "red"} />}>
      <div className="risk-list">
        <div className="risk-item"><span className="dim">Asset / Direction</span> <b>{trade.symbol} · {trade.side}</b></div>
        <div className="risk-item"><span className="dim">Result / RR</span> <b className={win ? "pos" : "neg"}>{trade.result} · {trade.rr! >= 0 ? "+" : ""}{trade.rr}R</b></div>
        <div className="risk-item"><span className="dim">Quality score</span> <b>{trade.score}/100</b></div>
        {trade.tp1_idx !== null && (
          <div className="risk-item"><span className="dim">Scale-out</span>
            <b className="pos">50% booked at +1R → break-even runner</b></div>
        )}
      </div>
      <div style={{ marginTop: 8 }}>
        <div className="card-subtitle" style={{ marginBottom: 4 }}>Reason for entry</div>
        <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.5 }}>{trade.entry_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
      </div>
      <div style={{ marginTop: 8 }}>
        <div className="card-subtitle" style={{ marginBottom: 4 }}>Reason for exit</div>
        <p style={{ margin: 0 }}>{trade.exit_reason}</p>
        {!win && trade.loss_analysis && (
          <p className="neg" style={{ marginTop: 6, display: "flex", gap: 6, alignItems: "flex-start" }}>
            <Icon name="warning" size={14} /> {trade.loss_analysis}
          </p>
        )}
      </div>
      <div style={{ marginTop: 10, borderTop: "1px solid #1b1b1f", paddingTop: 8 }}>
        <div className="risk-item" style={{ alignItems: "flex-start" }}>
          <span className="dim" style={{ minWidth: 110 }}>What helped</span>
          <span className="pos" style={{ textAlign: "right" }}>{ins.helped}</span></div>
        <div className="risk-item" style={{ alignItems: "flex-start" }}>
          <span className="dim" style={{ minWidth: 110 }}>What failed</span>
          <span className={win ? "" : "neg"} style={{ textAlign: "right" }}>{ins.failed}</span></div>
        <div className="risk-item" style={{ alignItems: "flex-start" }}>
          <span className="dim" style={{ minWidth: 110 }}>Suggested improvement</span>
          <span style={{ textAlign: "right", color: "#a855f7" }}>{ins.improve}</span></div>
      </div>
    </Card>
  );
}

function StatsPanel({ data }: { data: ReplayData }) {
  const s = data.stats;
  const cells: [string, string, string][] = [
    ["Win Rate", `${s.win_rate}%`, ""],
    ["Profit Factor", s.profit_factor.toFixed(2), s.profit_factor >= 1 ? "pos" : "neg"],
    ["Net", `${s.net_r >= 0 ? "+" : ""}${s.net_r}R`, s.net_r >= 0 ? "pos" : "neg"],
    ["Max Drawdown", `${s.max_drawdown_r}R`, ""],
    ["Avg RR", s.avg_rr.toFixed(2), ""],
    ["Expectancy", `${s.expectancy_r}R`, s.expectancy_r >= 0 ? "pos" : "neg"],
    ["Best / Worst", `${s.best_r} / ${s.worst_r}R`, ""],
    ["Long / Short", `${s.long_trades} / ${s.short_trades}`, ""],
    ["Max Win Streak", `${s.max_consecutive_wins}`, s.max_consecutive_wins ? "pos" : ""],
    ["Max Loss Streak", `${s.max_consecutive_losses}`, s.max_consecutive_losses ? "neg" : ""],
    ["Current Streak", s.current_streak === 0 ? "—" : `${s.current_streak > 0 ? `${s.current_streak}W` : `${-s.current_streak}L`}`, s.current_streak > 0 ? "pos" : s.current_streak < 0 ? "neg" : ""],
    ["Long / Short Net", `${s.long_net_r >= 0 ? "+" : ""}${s.long_net_r} / ${s.short_net_r >= 0 ? "+" : ""}${s.short_net_r}R`, ""],
  ];
  return (
    <Card title="Simulation Statistics" subtitle={`${s.trades} trades · ${s.symbol} (run another asset to compare)`}>
      <div className="perf-grid">
        {cells.map(([l, v, tone]) => (
          <div className="perf-item" key={l}><span className="perf-label">{l}</span><div className="perf-value-row"><span className={`perf-value ${tone}`}>{v}</span></div></div>
        ))}
      </div>
    </Card>
  );
}
