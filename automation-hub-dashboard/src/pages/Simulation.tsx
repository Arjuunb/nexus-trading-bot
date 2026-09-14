import { useState } from "react";
import { ENTRY_TIMEFRAMES } from "../lib/timeframes";
import Card from "../components/common/Card";
import AreaLine from "../components/chart/AreaLine";
import BarChart from "../components/chart/BarChart";
import Icon from "../components/common/Icon";
import { Badge, PageHeader } from "../components/common/ui";
import OfflineBanner from "../components/common/OfflineBanner";
import { useApp } from "../app-context";
import { apiGet, apiPostJson, useLive, hhmmss, type CustomSpec, type SimResult } from "../lib/api";
import ControlBar from "../components/control/ControlBar";
import { markDone } from "../lib/progress";

const money = (n: number) => `${n >= 0 ? "+" : "-"}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
const scoreTone = (s?: number) => (s == null ? "default" : s >= 80 ? "green" : s >= 60 ? "amber" : "red");

const BUILTINS = [
  { key: "smc", label: "SMC (Smart Money)",
    desc: "trades liquidity sweeps + CHoCH/BOS + fair-value gaps in line with the higher-timeframe bias." },
  { key: "brain", label: "Decision Brain",
    desc: "the live engine's multi-signal brain — trend + filter + momentum + RSI votes, regime-aware, 3R targets." },
  { key: "supertrend", label: "Supertrend",
    desc: "ATR trend-following — rides trends, exits on the supertrend flip." },
  { key: "donchian", label: "Donchian Breakout",
    desc: "classic channel breakout — buys N-bar highs, sells N-bar lows." },
  { key: "ensemble", label: "Confirmation Ensemble",
    desc: "only trades when multiple independent strategies agree on direction." },
  { key: "ema", label: "EMA Crossover",
    desc: "fast EMA over slow EMA — the simplest trend baseline to compare against." },
];

// Crypto pairs have real data (live/backfilled); stocks are for simulation —
// AAPL ships with a real daily sample, the rest run on labeled synthetic data.
const SYMBOL_GROUPS: [string, string[]][] = [
  ["Crypto", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
              "ADAUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "MATICUSDT"]],
  ["Stocks (simulation)", ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "SPY"]],
];
const SIM_TIMEFRAMES = ENTRY_TIMEFRAMES;
const BAR_CHOICES = [500, 1000, 2000, 3000, 5000, 8000, 10000];

export default function SimulationPage() {
  const app = useApp();
  const saved = useLive<CustomSpec[]>("/strategy/custom", 6000);
  const sysErr = useLive("/system/status", 5000).error;
  const [mode, setMode] = useState<"builtin" | "custom">("builtin");
  const [selId, setSelId] = useState<string>("");
  const [bars, setBars] = useState(3000);
  const [minScore, setMinScore] = useState(60);
  const [filterOn, setFilterOn] = useState(true);
  const [sim, setSim] = useState<SimResult | null>(null);
  const [running, setRunning] = useState(false);
  // built-in mode
  const [biKey, setBiKey] = useState("smc");
  const [biSymbol, setBiSymbol] = useState("BTCUSDT");
  const [biTf, setBiTf] = useState("4h");

  const specs = saved.data ?? [];
  const spec = specs.find((s) => s.id === selId) ?? specs[0];

  const run = async () => {
    if (!spec) return;
    setRunning(true);
    try {
      const payload = { ...spec, quality_filter: filterOn, min_score: minScore };
      const res = await apiPostJson<SimResult>("/strategy/custom/simulate", { spec: payload, bars });
      setSim(res);
      markDone("simulation");
      app.toast("Simulation complete — recorded for the safety flow", "success");
    } catch {
      app.toast("Simulation failed — backend unreachable?", "error");
    } finally {
      setRunning(false);
    }
  };

  const runBuiltin = async () => {
    setRunning(true);
    try {
      const q = `strategy=${biKey}&symbol=${encodeURIComponent(biSymbol)}&timeframe=${biTf}&bars=${bars}`;
      const res = await apiGet<SimResult>(`/strategy/builtin/simulate?${q}`);
      setSim(res);
      markDone("simulation");
      app.toast("Simulation complete — recorded for the safety flow", "success");
    } catch {
      app.toast("Simulation failed — backend unreachable?", "error");
    } finally {
      setRunning(false);
    }
  };

  const r = sim?.results;
  const diag = r?.diagnosis;
  const curve = (r?.equity_curve ?? []).map((p) => p.equity);
  const blocked = r?.blocked ?? [];

  const onControlResult = (cr: any) => {
    if (cr?.available && cr.results) {
      setSim({
        results: cr.results, warnings: [], description: `${cr.strategy} · ${cr.symbol} ${cr.timeframe}`,
        data_source: cr.data_source, symbol: cr.symbol, timeframe: cr.timeframe,
        label: "Simulation Result",
        brain: { quality_filter: true, min_score: 60, blocked_count: cr.results.blocked_count ?? 0 },
      } as SimResult);
      markDone("simulation");
    }
  };

  return (
    <>
      <PageHeader title="Simulation" subtitle="Forward simulation with the trade-quality brain · Backtest → Simulation → Paper → Live" />

      <OfflineBanner show={Boolean(sysErr) && !saved.data} what="the simulator" />

      <ControlBar onResult={onControlResult} />

      <div className="tabs standalone" style={{ marginBottom: 12, marginTop: 12 }}>
        {([["builtin", "Built-in Strategy"], ["custom", "Custom Strategy"]] as const).map(([m, lbl]) => (
          <button key={m} className={`tab ${mode === m ? "active" : ""}`}
            onClick={() => { setMode(m); setSim(null); }}>{lbl}</button>
        ))}
      </div>

      {mode === "builtin" && (
        <Card title="Run a Built-in Strategy" right={<Badge text="SIMULATED DATA" tone="amber" />}>
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 10, flexWrap: "wrap" }}>
            <select value={biKey} onChange={(e) => { setBiKey(e.target.value); setSim(null); }} style={{ minWidth: 220 }}>
              {BUILTINS.map((b) => <option key={b.key} value={b.key}>{b.label}</option>)}
            </select>
            <select value={SYMBOL_GROUPS.some(([, syms]) => syms.includes(biSymbol)) ? biSymbol : "__custom"}
              onChange={(e) => { if (e.target.value !== "__custom") setBiSymbol(e.target.value); }}
              style={{ minWidth: 150 }}>
              {SYMBOL_GROUPS.map(([group, syms]) => (
                <optgroup key={group} label={group}>
                  {syms.map((sym) => <option key={sym} value={sym}>{sym}</option>)}
                </optgroup>
              ))}
              <option value="__custom">Custom…</option>
            </select>
            <input value={biSymbol} onChange={(e) => setBiSymbol(e.target.value.toUpperCase())}
              placeholder="or type any symbol" style={{ width: 130 }} />
            <select value={biTf} onChange={(e) => setBiTf(e.target.value)}>
              {SIM_TIMEFRAMES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            <select value={bars} onChange={(e) => setBars(Number(e.target.value))}>
              {BAR_CHOICES.map((b) => <option key={b} value={b}>{b.toLocaleString()} bars</option>)}
            </select>
            <button className="btn btn-primary" disabled={running} onClick={runBuiltin}>
              <Icon name="play" size={14} /> {running ? "Simulating…" : "Run Simulation"}
            </button>
          </div>
          <p className="dim" style={{ marginTop: 10 }}>
            <Icon name="info" size={13} /> <b>{BUILTINS.find((b) => b.key === biKey)?.label}</b>{" "}
            {BUILTINS.find((b) => b.key === biKey)?.desc} Crypto pairs use real cached/live candles when
            available; stocks run on the bundled sample (AAPL, real daily) or labeled synthetic data.
            Never shown as live performance.
          </p>
        </Card>
      )}

      {mode === "custom" && (
      <Card title="Run a Custom Simulation" right={<Badge text="SIMULATED DATA" tone="amber" />}>
        {specs.length === 0 ? (
          <div className="dim">No custom strategies to simulate. Build and save one on the <b>Strategies</b> page.</div>
        ) : (
          <>
            <div className="row-actions" style={{ justifyContent: "flex-start", gap: 10, flexWrap: "wrap" }}>
              <select value={spec?.id ?? ""} onChange={(e) => { setSelId(e.target.value); setSim(null); }} style={{ minWidth: 220 }}>
                {specs.map((s) => <option key={s.id} value={s.id}>{s.name} · {s.symbol} {s.timeframe}</option>)}
              </select>
              <select value={bars} onChange={(e) => setBars(Number(e.target.value))}>
                {BAR_CHOICES.map((b) => <option key={b} value={b}>{b.toLocaleString()} bars</option>)}
              </select>
              <button className="btn btn-primary" disabled={running} onClick={run}>
                <Icon name="play" size={14} /> {running ? "Simulating…" : "Run Simulation"}
              </button>
            </div>
            <div className="row-actions" style={{ justifyContent: "flex-start", gap: 14, marginTop: 10, flexWrap: "wrap" }}>
              <label className="row-actions" style={{ gap: 6, cursor: "pointer" }}>
                <input type="checkbox" checked={filterOn} onChange={(e) => setFilterOn(e.target.checked)} />
                <span>Trade-quality brain filter</span>
              </label>
              <label className="row-actions" style={{ gap: 6 }}>
                <span className="dim">Min score</span>
                <input type="range" min={0} max={90} step={5} value={minScore} disabled={!filterOn}
                  onChange={(e) => setMinScore(Number(e.target.value))} />
                <b>{minScore}</b>
              </label>
            </div>
          </>
        )}
        <p className="dim" style={{ marginTop: 10 }}>
          <Icon name="info" size={13} /> The brain blocks weak setups (wrong regime, against higher-timeframe trend,
          poor reward:risk, unsafe stop). Simulated data is never shown as live performance.
        </p>
      </Card>
      )}

      {r && (
        <>
          <Card title="Simulation Result" subtitle={`${sim!.data_source} · ${sim!.symbol} ${sim!.timeframe} · ${r.span_days}d`}
            right={<Badge text={sim!.brain?.quality_filter ? `BRAIN ON · min ${sim!.brain.min_score}` : "RAW (no filter)"}
              tone={sim!.brain?.quality_filter ? "purple" : "default"} />}>
            <div className="perf-grid">
              {[
                ["Total Trades", String(r.total_trades), ""],
                ["Win Rate", `${r.win_rate}%`, ""],
                ["Profit Factor", r.profit_factor.toFixed(2), r.profit_factor >= 1 ? "pos" : "neg"],
                ["Net P&L", `${money(r.net_r)}R (${r.net_pct >= 0 ? "+" : ""}${r.net_pct}%)`, r.net_r >= 0 ? "pos" : "neg"],
                ["Expectancy", `${(r.expectancy_r ?? 0).toFixed(3)}R`, (r.expectancy_r ?? 0) >= 0 ? "pos" : "neg"],
                ["Sharpe", (r.sharpe ?? 0).toFixed(2), (r.sharpe ?? 0) >= 0 ? "pos" : "neg"],
                ["Max Drawdown", `${r.max_drawdown_pct}%`, "neg"],
                ["Recovery", (r.recovery_factor ?? 0).toFixed(2), ""],
                ["Avg R:R", r.avg_rr.toFixed(2), ""],
                ["Avg Hold", `${r.avg_hold_bars ?? 0} bars`, ""],
                ["Long / Short", `${r.long_trades ?? 0} / ${r.short_trades ?? 0}`, ""],
                ["Blocked", String(r.blocked_count ?? 0), r.blocked_count ? "amber" : ""],
              ].map(([l, v, tone]) => (
                <div className="perf-item" key={l}><span className="perf-label">{l}</span><div className="perf-value-row"><span className={`perf-value ${tone}`}>{v}</span></div></div>
              ))}
            </div>
            {curve.length > 1 && (
              <div className="chart-md" style={{ marginTop: 12 }}>
                <AreaLine labels={curve.map((_, i) => String(i))} series={[{ name: "Equity", data: curve, color: "#eab54f" }]} valueFormatter={(x) => `$${x.toLocaleString()}`} />
              </div>
            )}
          </Card>

          {diag && (
            <Card title="Brain Diagnosis" subtitle="why trades won/lost — and what to fix"
              right={diag.avg_quality_score != null ? <Badge text={`avg score ${diag.avg_quality_score}`} tone={scoreTone(diag.avg_quality_score)} /> : undefined}>
              <p style={{ lineHeight: 1.6 }}><b>{diag.headline_problem}</b></p>
              <p className="dim">{diag.summary}</p>
              <div className="grid-2-eq" style={{ marginTop: 10 }}>
                <div>
                  <div className="card-subtitle" style={{ marginBottom: 6 }}>Recommendations</div>
                  <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.6 }}>
                    {diag.recommendations.map((rec, i) => <li key={i}>{rec}</li>)}
                  </ul>
                </div>
                <div>
                  <div className="card-subtitle" style={{ marginBottom: 6 }}>Signals</div>
                  <div className="risk-list">
                    {diag.worst_regime && <div className="risk-item"><span className="dim">Worst regime</span> <b className="neg">{diag.worst_regime.name}</b> <span className="dim">({diag.worst_regime.net_r}R)</span></div>}
                    {diag.worst_session && <div className="risk-item"><span className="dim">Worst session</span> <b>{diag.worst_session.name}</b> <span className="dim">({diag.worst_session.net_r}R)</span></div>}
                    {diag.avg_losing_setup_score != null && <div className="risk-item"><span className="dim">Avg losing setup score</span> <b className={scoreTone(diag.avg_losing_setup_score) === "red" ? "neg" : ""}>{diag.avg_losing_setup_score}/100</b></div>}
                    <div className="risk-item"><span className="dim">Overtrading</span> <b className={diag.overtrading ? "neg" : "pos"}>{diag.overtrading ? "yes" : "no"}</b> <span className="dim">({diag.trades_per_day}/day)</span></div>
                    <div className="risk-item"><span className="dim">Choppy markets</span> <b className={diag.choppy_markets ? "neg" : "pos"}>{diag.choppy_markets ? "yes" : "no"}</b></div>
                  </div>
                </div>
              </div>
              {Object.keys(diag.loss_reasons).length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div className="card-subtitle" style={{ marginBottom: 6 }}>Loss reasons (entry rule present in losers)</div>
                  <div className="chart-sm">
                    <BarChart labels={Object.keys(diag.loss_reasons)} data={Object.values(diag.loss_reasons)} color="#ef4444" horizontal />
                  </div>
                </div>
              )}
            </Card>
          )}

          {blocked.length > 0 && (
            <Card title="Blocked Trades" subtitle={`${r.blocked_count} weak setups the brain avoided — often the real edge`}>
              <div className="tablewrap">
                <table className="data-table">
                  <thead><tr><th>Time</th><th>Side</th><th>Score</th><th>Regime</th><th>HTF</th><th>Reason</th></tr></thead>
                  <tbody>
                    {blocked.slice().reverse().slice(0, 30).map((b, i) => (
                      <tr key={i}>
                        <td className="dim mono">{hhmmss(b.time)}</td>
                        <td><Badge text={b.side} tone={b.side === "long" ? "green" : "red"} /></td>
                        <td><Badge text={String(b.score)} tone={scoreTone(b.score)} /></td>
                        <td className="dim">{b.regime}</td>
                        <td className="dim">{b.htf_bias}</td>
                        <td>{b.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          <Card title="Taken Trades" subtitle="quality score + rule checklist for every entry">
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Time</th><th>Side</th><th>Score</th><th>Regime</th><th>R</th><th>Result</th><th>Exit</th><th>Setup</th></tr></thead>
                <tbody>
                  {(r.trades ?? []).slice().reverse().slice(0, 40).map((t, i) => (
                    <tr key={i}>
                      <td className="dim mono">{hhmmss(t.exit_time)}</td>
                      <td><Badge text={t.side} tone={t.side === "long" ? "green" : "red"} /></td>
                      <td>{t.score != null ? <Badge text={String(t.score)} tone={scoreTone(t.score)} /> : <span className="dim">—</span>}</td>
                      <td className="dim">{t.regime ?? "—"}</td>
                      <td className={t.r >= 0 ? "pos" : "neg"}>{t.r >= 0 ? "+" : ""}{t.r}R</td>
                      <td><Badge text={t.result} tone={t.result === "win" ? "green" : "red"} /></td>
                      <td className="dim">{t.exit_reason ?? "—"}</td>
                      <td className="dim">{t.setup_type ?? "—"}</td>
                    </tr>
                  ))}
                  {(r.trades?.length ?? 0) === 0 && <tr><td colSpan={8} className="dim ta-center" style={{ padding: 16 }}>No trades were taken — see the diagnosis above.</td></tr>}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      )}
    </>
  );
}
