import { useEffect, useState } from "react";
import { ENTRY_TIMEFRAMES, DEFAULT_ENTRY_TIMEFRAME } from "../lib/timeframes";
import Card from "../components/common/Card";
import AreaLine from "../components/chart/AreaLine";
import FanChart from "../components/chart/FanChart";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { apiGet, apiPostJson, useLive, hhmmss, API_BASE,
  type StrategyPerformance, type WalkForward, type MonteCarlo, type OutOfSample, type SlicedPerf, type AttrBucket, type ResearchSummary, type ExecRealism, type PaperTradeRow } from "../lib/api";
import { useApp } from "../app-context";
import { markDone } from "../lib/progress";

const money = (n: number) => `${n >= 0 ? "+" : "-"}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

interface MonthRow { month: string; trades: number; net: number; winRate: number; }
function monthlyReturns(trades: PaperTradeRow[]): MonthRow[] {
  const m = new Map<string, { net: number; trades: number; wins: number }>();
  for (const t of trades) {
    if (!t.closed_at || t.pnl == null) continue;
    const key = t.closed_at.slice(0, 7);          // YYYY-MM
    const b = m.get(key) ?? { net: 0, trades: 0, wins: 0 };
    b.net += t.pnl; b.trades += 1; if (t.pnl > 0) b.wins += 1;
    m.set(key, b);
  }
  return [...m.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([month, b]) => ({ month, trades: b.trades, net: b.net, winRate: b.trades ? (b.wins / b.trades) * 100 : 0 }));
}

// R-multiple distribution buckets (uses realized R:R; falls back to nothing if absent)
const R_BINS: { label: string; lo: number; hi: number }[] = [
  { label: "≤ −2R", lo: -Infinity, hi: -2 }, { label: "−2…−1R", lo: -2, hi: -1 },
  { label: "−1…0R", lo: -1, hi: 0 }, { label: "0…1R", lo: 0, hi: 1 },
  { label: "1…2R", lo: 1, hi: 2 }, { label: "2…3R", lo: 2, hi: 3 }, { label: "> 3R", lo: 3, hi: Infinity },
];
function rDistribution(trades: PaperTradeRow[]): { bins: { label: string; count: number; win: boolean }[]; total: number } {
  const counts = R_BINS.map(() => 0);
  let total = 0;
  for (const t of trades) {
    if (t.rr == null || t.closed_at == null) continue;
    const i = R_BINS.findIndex((b) => t.rr! > b.lo && t.rr! <= b.hi);
    if (i >= 0) { counts[i] += 1; total += 1; }
  }
  return { bins: R_BINS.map((b, i) => ({ label: b.label, count: counts[i], win: b.lo >= 0 })), total };
}

export default function BacktestingPage() {
  const { go } = useApp();
  const { data, error } = useLive<StrategyPerformance>("/strategy/performance", 3000);
  const { data: allTrades } = useLive<PaperTradeRow[]>("/paper/trades", 5000);
  const offline = error && !data;
  const months = monthlyReturns(allTrades ?? []);
  const dist = rDistribution(allTrades ?? []);
  const distMax = Math.max(1, ...dist.bins.map((b) => b.count));

  // A real strategy track record counts as recorded backtest evidence for the
  // safety flow (Backtest -> Simulation -> Paper -> Live).
  useEffect(() => { if ((data?.trades ?? 0) > 0) markDone("backtest"); }, [data?.trades]);

  const curve = data?.equity_curve ?? [];
  const labels = curve.map((p, i) => (i === 0 ? "start" : hhmmss(p.t)));
  const equity = curve.map((p) => p.equity);

  return (
    <>
      <PageHeader
        title="Strategy Performance"
        subtitle={data ? `${data.strategy} · ${data.mode} · forward-paper record on live data` : "forward-paper record on live data"}
        actions={
          <>
            {data && <Badge text={`${data.trades} trades`} tone="blue" />}
            <button className="btn btn-soft btn-sm" onClick={() => go("Simulation")}><Icon name="history" size={13} /> Simulation</button>
            <button className="btn btn-soft btn-sm" onClick={() => go("Replay")}><Icon name="chart" size={13} /> Replay</button>
          </>
        }
      />

      {offline && (
        <div className="card" style={{ borderColor: "#ef4444", display: "flex", alignItems: "center", gap: 10 }}>
          <Icon name="warning" size={16} className="neg" />
          <span><b>Backend not reachable.</b> Start the API at <span className="mono">{API_BASE}</span> — this page shows the bot's real executed trades.</span>
        </div>
      )}

      <div className="stat-row">
        <StatCard label="Realized P&L" value={money(data?.realized_pnl ?? 0)} tone={(data?.realized_pnl ?? 0) >= 0 ? "green" : "red"} sub={`Balance $${(data?.balance ?? 0).toLocaleString()}`} />
        <StatCard label="Win Rate" value={`${(data?.win_rate ?? 0).toFixed(1)}%`} />
        <StatCard label="Profit Factor" value={(data?.profit_factor ?? 0).toFixed(2)} tone={(data?.profit_factor ?? 0) >= 1 ? "green" : "red"} />
        <StatCard label="Max Drawdown" value={`${(data?.max_drawdown_pct ?? 0).toFixed(1)}%`} tone="amber" sub={money(-(data?.max_drawdown_abs ?? 0))} />
        <StatCard label="Worst Streak" value={`${data?.longest_losing_streak ?? 0}`} sub="losses in a row" />
      </div>

      <Card title="Equity Curve" subtitle="realized P&L of executed paper trades">
        <div className="chart-md">
          <AreaLine labels={labels} series={[{ name: "Equity", data: equity, color: "#eab54f" }]}
                    valueFormatter={(v) => `$${v.toLocaleString()}`} />
        </div>
      </Card>

      <div className="grid-2-1">
        <Card title="Monthly Returns" subtitle="realized P&L by calendar month (paper)" className="span-2">
          {months.length === 0 ? <div className="dim" style={{ padding: 12 }}>No closed trades yet — monthly returns appear once the bot books its first trade.</div> : (
            <div className="tablewrap">
              <table className="data-table">
                <thead><tr><th>Month</th><th>Trades</th><th>Win rate</th><th style={{ textAlign: "right" }}>Net P&amp;L</th></tr></thead>
                <tbody>
                  {months.map((m) => (
                    <tr key={m.month}>
                      <td><b>{new Date(m.month + "-01").toLocaleDateString(undefined, { month: "short", year: "numeric" })}</b></td>
                      <td className="dim">{m.trades}</td>
                      <td className="dim">{m.winRate.toFixed(0)}%</td>
                      <td className={m.net >= 0 ? "pos" : "neg"} style={{ textAlign: "right", fontFamily: "var(--mono)" }}>{money(m.net)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Trade Distribution" subtitle={`by R-multiple · ${dist.total} closed`}>
          {dist.total === 0 ? <div className="dim" style={{ padding: 12 }}>No R-graded trades yet.</div> : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4 }}>
              {dist.bins.map((b) => (
                <div key={b.label} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11.5 }}>
                  <span className="dim" style={{ width: 58, textAlign: "right", fontFamily: "var(--mono)" }}>{b.label}</span>
                  <div style={{ flex: 1, background: "rgba(255,255,255,0.05)", borderRadius: 4, height: 16, overflow: "hidden" }}>
                    <div style={{ width: `${(b.count / distMax) * 100}%`, height: "100%",
                      background: b.win ? "var(--green)" : "var(--red)", opacity: b.count ? 0.85 : 0, borderRadius: 4,
                      transition: "width .3s ease" }} />
                  </div>
                  <span style={{ width: 22, textAlign: "right" }}>{b.count}</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <div className="grid-2-1">
        <Card title="Trade Statistics" className="span-2">
          <div className="tablewrap">
            <table className="data-table">
              <tbody>
                <tr><td className="dim">Expectancy / trade</td><td className={(data?.expectancy ?? 0) >= 0 ? "pos" : "neg"}>{money(data?.expectancy ?? 0)}</td>
                    <td className="dim">Avg win</td><td className="pos">{money(data?.avg_win ?? 0)}</td></tr>
                <tr><td className="dim">Total trades</td><td>{data?.trades ?? 0}</td>
                    <td className="dim">Avg loss</td><td className="neg">{money(data?.avg_loss ?? 0)}</td></tr>
                <tr><td className="dim">Best trade</td><td className="pos">{money(data?.best ?? 0)}</td>
                    <td className="dim">Worst trade</td><td className="neg">{money(data?.worst ?? 0)}</td></tr>
              </tbody>
            </table>
          </div>
          <p className="dim" style={{ marginTop: 8 }}>
            Validated reference (BTC/ETH 4h, walk-forward, fees + slippage): profit factor ~1.2, ~15%/yr, ~14% max drawdown.
          </p>
        </Card>

        <Card title="Recent Trades">
          <div className="tablewrap">
            <table className="data-table">
              <thead><tr><th>Symbol</th><th>P&amp;L</th><th>R:R</th></tr></thead>
              <tbody>
                {(data?.recent ?? []).map((t) => (
                  <tr key={t.id}>
                    <td><b>{t.symbol}</b></td>
                    <td className={(t.pnl ?? 0) >= 0 ? "pos" : "neg"}>{money(t.pnl ?? 0)}</td>
                    <td className="dim">{t.rr !== null && t.rr !== undefined ? `${t.rr.toFixed(2)}R` : "—"}</td>
                  </tr>
                ))}
                {(data?.recent?.length ?? 0) === 0 && <tr><td colSpan={3} className="dim ta-center" style={{ padding: 16 }}>No closed trades yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      <RobustnessLab />
      <ExecutionRealism />
      <ResearchLab />
    </>
  );
}

const ER_STRATS = ["Decision Brain", "Supply/Demand", "EMA 8/30", "EMA 20/50", "Liquidity Sweep"];

function ExecutionRealism() {
  const [strategy, setStrategy] = useState("Decision Brain");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [d, setD] = useState<ExecRealism | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try { setD(await apiGet<ExecRealism>(`/execution/realism?symbol=${symbol}&strategy=${encodeURIComponent(strategy)}&timeframe=15m&limit=800`)); }
    catch { /* ignore */ } finally { setBusy(false); }
  };
  const cmp = (label: string, ideal: number, real: number, suffix = "") => (
    <div className="perf-item"><span className="perf-label">{label}</span>
      <div className="perf-value-row"><span className="perf-value dim" style={{ fontSize: 14 }}>{ideal}{suffix}</span>
        <span className="perf-value" style={{ fontSize: 14 }}>→ <b className={real >= ideal ? "pos" : "neg"}>{real}{suffix}</b></span></div></div>
  );
  return (
    <Card title="Execution Realism" subtitle="ideal vs spread + slippage + latency + partial fills + rejections"
      right={<div className="row-actions" style={{ gap: 6 }}>
        <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>{ER_STRATS.map((s) => <option key={s}>{s}</option>)}</select>
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"].map((s) => <option key={s}>{s}</option>)}</select>
        <button className="btn btn-soft" disabled={busy} onClick={run}>{busy ? "…" : "Simulate fills"}</button>
      </div>}>
      {!d ? <div className="dim ta-center" style={{ padding: 12 }}>Re-price a real run with realistic execution friction.</div>
        : d.available === false ? <p className="neg"><Icon name="warning" size={13} /> {d.error}</p>
        : !d.ideal || !d.realistic ? <div className="dim ta-center" style={{ padding: 12 }}>No realism data for this run yet.</div>
        : (
        <>
          <div className="row-actions" style={{ justifyContent: "space-between" }}>
            <Badge text={d.edge_survives ? "edge survives" : "edge eroded"} tone={d.edge_survives ? "green" : "red"} />
            <span className="dim" style={{ fontSize: 12 }}>{d.rejected} rejected · {d.partial_fills} partial · cost {d.slippage_cost_r}R</span>
          </div>
          <div className="perf-grid" style={{ marginTop: 10 }}>
            {cmp("Net R", d.ideal.net_r, d.realistic.net_r, "R")}
            {cmp("Profit Factor", d.ideal.profit_factor, d.realistic.profit_factor)}
            {cmp("Win %", d.ideal.win_rate, d.realistic.win_rate, "%")}
            {cmp("Expectancy", d.ideal.expectancy_r, d.realistic.expectancy_r, "R")}
          </div>
        </>
      )}
    </Card>
  );
}

const emaSpec = (fast: number, slow: number) => ({
  symbol: "BTCUSDT", timeframe: DEFAULT_ENTRY_TIMEFRAME, side: "long",
  entry: { op: "AND", rules: [{ type: "ema_cross", fast, slow, dir: "above" }] },
  stop: { type: "atr", mult: 1.5, period: 14 }, target: { type: "rr", rr: 2.0 },
  risk_per_trade_pct: 0.01, min_score: 60,
});
const vTone2 = (v: string) => (v === "improvement" ? "green" : v === "overfit" ? "red" : "amber");

function ResearchLab() {
  const app = useApp();
  const list = useLive<{ experiments: ResearchSummary[] }>("/research", 8000);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<{ name: string; report: string } | null>(null);

  const run = async () => {
    setBusy(true);
    try {
      await apiPostJson("/research/run", { name: "EMA 20/50 vs 9/33", spec_a: emaSpec(20, 50), spec_b: emaSpec(9, 33), bars: 4000, label_a: "EMA 20/50", label_b: "EMA 9/33" });
      app.toast("Experiment saved", "success"); list.refetch();
    } catch { app.toast("Research run needs the webhook secret", "error"); } finally { setBusy(false); }
  };
  const view = async (id: string) => { try { setReport(await apiGet(`/research/${id}/report`)); } catch { /* ignore */ } };

  return (
    <Card title="Research Lab" subtitle="save A/B experiments + generate reports"
      right={<button className="btn btn-soft" disabled={busy} onClick={run}><Icon name="flask" size={13} /> {busy ? "Running…" : "Run EMA A/B"}</button>}>
      {(list.data?.experiments?.length ?? 0) === 0 ? (
        <div className="dim ta-center" style={{ padding: 12 }}>No saved experiments — run one to compare ideas out-of-sample.</div>
      ) : (
        <table className="data-table" style={{ fontSize: 12 }}>
          <thead><tr><th>Experiment</th><th>Market</th><th>Verdict</th><th>OOS gain</th><th>Saved</th><th></th></tr></thead>
          <tbody>{(list.data?.experiments ?? []).map((e) => (
            <tr key={e.id}>
              <td><b>{e.name}</b></td><td className="dim">{e.symbol} {e.timeframe}</td>
              <td><Badge text={e.verdict} tone={vTone2(e.verdict) as any} /></td>
              <td className={e.test_gain_r >= 0 ? "pos" : "neg"}>{e.test_gain_r >= 0 ? "+" : ""}{e.test_gain_r}R</td>
              <td className="dim mono">{(e.created_at || "").slice(5, 16).replace("T", " ")}</td>
              <td><button className="btn btn-soft sm" onClick={() => view(e.id)}>Report</button></td>
            </tr>
          ))}</tbody>
        </table>
      )}
      {report && (
        <div className="card" style={{ marginTop: 10, background: "var(--card-2)" }}>
          <div className="row-actions" style={{ justifyContent: "space-between" }}><b>{report.name}</b>
            <button className="icon-btn sm" onClick={() => setReport(null)}><Icon name="close" size={13} /></button></div>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, lineHeight: 1.5, margin: "8px 0 0", fontFamily: "inherit" }}>{report.report}</pre>
        </div>
      )}
    </Card>
  );
}

const LAB_SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"];
const LAB_STRATS = ["Decision Brain", "Trend Following", "Supply/Demand", "EMA 8/30", "EMA 20/50", "Liquidity Sweep"];
const vTone = (v: string) => (["robust", "holds"].includes(v) ? "green" : ["fragile", "overfit"].includes(v) ? "red" : "amber");
const rt = (n: number) => (n > 0 ? "pos" : n < 0 ? "neg" : "");

function RobustnessLab() {
  const [strategy, setStrategy] = useState("Decision Brain");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [tf, setTf] = useState<string>(DEFAULT_ENTRY_TIMEFRAME);
  const [busy, setBusy] = useState("");
  const [wf, setWf] = useState<WalkForward | null>(null);
  const [mc, setMc] = useState<MonteCarlo | null>(null);
  const [oos, setOos] = useState<OutOfSample | null>(null);
  const [sliced, setSliced] = useState<SlicedPerf | null>(null);

  const run = async (kind: string) => {
    setBusy(kind);
    const q = `symbol=${symbol}&strategy=${encodeURIComponent(strategy)}&timeframe=${tf}`;
    try {
      if (kind === "wf") setWf(await apiGet<WalkForward>(`/lab/walk-forward?${q}&bars=4000&folds=4`));
      else if (kind === "mc") setMc(await apiGet<MonteCarlo>(`/lab/monte-carlo?${q}&bars=4000&runs=1000`));
      else if (kind === "oos") setOos(await apiGet<OutOfSample>(`/lab/out-of-sample?${q}&bars=4000&split=0.7`));
      else setSliced(await apiGet<SlicedPerf>(`/lab/sliced?strategy=${encodeURIComponent(strategy)}&timeframe=15m&symbols=${LAB_SYMS.join(",")}&limit=800`));
    } catch { /* ignore */ } finally { setBusy(""); }
  };

  const bucketTable = (rows?: AttrBucket[]) => (
    <table className="data-table" style={{ fontSize: 12 }}><tbody>
      {(rows ?? []).map((b) => (
        <tr key={b.key}><td><b>{b.key.replace("USDT", "")}</b></td><td className="dim">{b.trades}t · {b.win_rate}%</td>
          <td className={rt(b.net_r)} style={{ textAlign: "right" }}>{b.net_r >= 0 ? "+" : ""}{b.net_r}R</td></tr>
      ))}
    </tbody></table>
  );

  return (
    <Card title="Robustness Lab" subtitle="walk-forward · Monte Carlo · out-of-sample · regime/session/symbol — real Binance data"
      right={<div className="row-actions" style={{ gap: 6 }}>
        <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>{LAB_STRATS.map((s) => <option key={s}>{s}</option>)}</select>
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{LAB_SYMS.map((s) => <option key={s}>{s}</option>)}</select>
        <select value={tf} onChange={(e) => setTf(e.target.value)}>{ENTRY_TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select>
      </div>}>
      <div className="row-actions" style={{ justifyContent: "flex-start", gap: 8, flexWrap: "wrap" }}>
        <button className="btn btn-primary" disabled={!!busy} onClick={() => run("wf")}>{busy === "wf" ? "…" : "Walk-forward"}</button>
        <button className="btn btn-soft" disabled={!!busy} onClick={() => run("mc")}>{busy === "mc" ? "…" : "Monte Carlo"}</button>
        <button className="btn btn-soft" disabled={!!busy} onClick={() => run("oos")}>{busy === "oos" ? "…" : "Out-of-sample"}</button>
        <button className="btn btn-soft" disabled={!!busy} onClick={() => run("sliced")}>{busy === "sliced" ? "…" : "Regime / Session / Symbol"}</button>
      </div>

      {wf && (wf.available
        ? <div style={{ marginTop: 12 }}>
            <div className="row-actions" style={{ justifyContent: "space-between" }}>
              <b>Walk-forward — {wf.positive_folds}/{wf.total_folds} folds positive, OOS net <span className={rt(wf.oos_net_r)}>{wf.oos_net_r >= 0 ? "+" : ""}{wf.oos_net_r}R</span></b>
              <Badge text={wf.verdict} tone={vTone(wf.verdict) as any} />
            </div>
            <p className="dim" style={{ margin: "4px 0 6px" }}>{wf.note}</p>
            <table className="data-table" style={{ fontSize: 12 }}>
              <thead><tr><th>Fold</th><th>Best score</th><th>Train R</th><th>Test R (OOS)</th><th>Test trades</th><th>Test PF</th></tr></thead>
              <tbody>{wf.folds.map((f) => (
                <tr key={f.fold}><td>{f.fold}</td><td className="dim">{f.best_min_score}</td>
                  <td className={rt(f.train_net_r)}>{f.train_net_r}R</td><td className={rt(f.test_net_r)}>{f.test_net_r}R</td>
                  <td className="dim">{f.test_trades}</td><td>{f.test_pf}</td></tr>
              ))}</tbody>
            </table>
          </div>
        : <p className="neg" style={{ marginTop: 8 }}><Icon name="warning" size={13} /> {wf.error}</p>)}

      {mc && (mc.available && !mc.error
        ? <>
          {mc.paths && mc.paths.steps > 1 && (
            <div style={{ marginTop: 12 }}>
              <div className="dim" style={{ fontSize: 11.5, marginBottom: 4 }}>
                {mc.runs} bootstrapped equity paths over {mc.trades} trades — gold band = 25–75th percentile, faint = 5–95th, line = median.
              </div>
              <FanChart bands={mc.paths.bands} samples={mc.paths.samples} />
            </div>
          )}
          <div className="perf-grid" style={{ marginTop: 12 }}>
            {[["P(profit)", `${mc.prob_profit_pct}%`, mc.prob_profit_pct >= 50 ? "pos" : "neg"],
              ["Survival", `${mc.survival_probability_pct ?? "—"}%`, (mc.survival_probability_pct ?? 100) >= 95 ? "pos" : "neg"],
              ["P(ruin)", `${mc.probability_of_ruin_pct ?? "—"}%`, (mc.probability_of_ruin_pct ?? 0) <= 5 ? "pos" : "neg"],
              ["Expected", `${(mc.expected_return_r ?? 0) >= 0 ? "+" : ""}${mc.expected_return_r ?? mc.net_r.mean}R`, rt(mc.expected_return_r ?? mc.net_r.mean)],
              ["Recovery", `${mc.recovery_probability_pct ?? "—"}%`, ""],
              ["5th–95th net", `${mc.net_r.p5} … ${mc.net_r.p95}R`, ""],
              ["Median DD", `${mc.max_drawdown_r.median}R`, "amber"],
              ["Worst DD", `${mc.max_drawdown_r.worst}R`, "neg"]].map(([l, v, t]) => (
              <div className="perf-item" key={l as string}><span className="perf-label">{l}</span><div className="perf-value-row"><span className={`perf-value ${t}`}>{v}</span></div></div>
            ))}
          </div>
          </>
        : mc && <p className="amber" style={{ marginTop: 8 }}><Icon name="warning" size={13} /> {mc.error || "unavailable"} · {mc.runs} runs over {mc.trades} trades</p>)}

      {oos && (oos.available
        ? <div style={{ marginTop: 12 }}>
            <div className="row-actions" style={{ justifyContent: "space-between" }}>
              <b>Out-of-sample ({Math.round(oos.split * 100)}/{Math.round((1 - oos.split) * 100)} split)</b>
              <Badge text={oos.verdict} tone={vTone(oos.verdict) as any} />
            </div>
            <p className="dim" style={{ margin: "4px 0 6px" }}>{oos.note}</p>
            <table className="data-table" style={{ fontSize: 12 }}>
              <thead><tr><th></th><th>Net R</th><th>PF</th><th>Win%</th><th>Trades</th></tr></thead>
              <tbody>
                <tr><td><b>Train</b></td><td className={rt(oos.train.net_r)}>{oos.train.net_r}R</td><td>{oos.train.profit_factor}</td><td className="dim">{oos.train.win_rate}%</td><td className="dim">{oos.train.trades}</td></tr>
                <tr><td><b>Test (unseen)</b></td><td className={rt(oos.test.net_r)}>{oos.test.net_r}R</td><td>{oos.test.profit_factor}</td><td className="dim">{oos.test.win_rate}%</td><td className="dim">{oos.test.trades}</td></tr>
              </tbody>
            </table>
          </div>
        : <p className="neg" style={{ marginTop: 8 }}><Icon name="warning" size={13} /> {oos.error}</p>)}

      {sliced && (
        <div style={{ marginTop: 12 }}>
          <div className="card-subtitle" style={{ marginBottom: 6 }}>Conditional performance · {sliced.total_trades} trades</div>
          <div className="grid-2-eq">
            <div><div className="card-subtitle" style={{ marginBottom: 4 }}>By regime</div>{bucketTable(sliced.by_regime)}</div>
            <div><div className="card-subtitle" style={{ marginBottom: 4 }}>By session</div>{bucketTable(sliced.by_session)}</div>
            <div><div className="card-subtitle" style={{ marginBottom: 4 }}>By symbol</div>{bucketTable(sliced.by_symbol)}</div>
          </div>
        </div>
      )}
    </Card>
  );
}
