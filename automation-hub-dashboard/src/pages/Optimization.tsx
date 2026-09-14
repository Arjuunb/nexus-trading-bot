import { useEffect, useMemo, useState } from "react";
import { ENTRY_TIMEFRAMES, DEFAULT_ENTRY_TIMEFRAME } from "../lib/timeframes";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { useApp } from "../app-context";
import { apiGet, apiPostJson, type ControlOptions, type ControlAutoTune } from "../lib/api";

/** Optimization Lab — a parameter sweep over the REAL backtest engine with a
 *  train/validate split. The results matrix shows every (min-score × R:R) combo;
 *  the best is validated on unseen data with an honest overfit verdict, so a
 *  "winner" that only worked in-sample is called out, not adopted. */

const rt = (n: number) => (n >= 0 ? "pos" : "neg");
const tone = (n: number) => (n >= 0 ? "green" : "red") as never;
const VERDICT_TONE: Record<string, string> = { improvement: "green", overfit: "red", no_improvement: "amber" };
const MS = [55, 65, 75];   // the grid the backend searches (min score)
const RR = [1.5, 2.0, 2.5];

export default function OptimizationPage() {
  const { go } = useApp();
  const [opt, setOpt] = useState<ControlOptions | null>(null);
  const [strategy, setStrategy] = useState("Decision Brain");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [tf, setTf] = useState<string>(DEFAULT_ENTRY_TIMEFRAME);
  const [bars, setBars] = useState(4000);
  const [res, setRes] = useState<ControlAutoTune | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>("");

  useEffect(() => { apiGet<ControlOptions>("/control/options").then(setOpt).catch(() => {}); }, []);

  const run = async () => {
    setBusy(true); setErr(""); setRes(null);
    try {
      const r = await apiPostJson<ControlAutoTune>("/control/auto-tune", { strategy, symbol, timeframe: tf, bars });
      if (!r.available) setErr(r.error || "Optimization unavailable — load real market data first.");
      else setRes(r);
    } catch { setErr("Optimization failed — is the backend reachable?"); }
    finally { setBusy(false); }
  };

  // index trials by (min_score, rr) for the matrix; find best net_r for highlight
  const trialAt = useMemo(() => {
    const m = new Map<string, ControlAutoTune["trials"][number]>();
    (res?.trials ?? []).forEach((t) => m.set(`${t.min_score}|${t.rr}`, t));
    return m;
  }, [res]);
  const bestNet = useMemo(() => Math.max(-Infinity, ...(res?.trials ?? []).map((t) => t.net_r)), [res]);
  const best = res?.best_tuning;

  return (
    <>
      <PageHeader title="Optimization Lab"
        subtitle="Parameter sweep on real history with a train/validate split — find the settings that hold up out-of-sample, not just the ones that curve-fit."
        actions={<>
          <button className="btn btn-soft btn-sm" onClick={() => go("Backtesting")}><Icon name="chart" size={13} /> Backtesting</button>
          <button className="btn btn-soft btn-sm" onClick={() => go("Strategy Studio")}><Icon name="settings" size={13} /> Strategy Studio</button>
        </>}
      />

      <Card title="Sweep configuration" subtitle="optimises the min-score gate × reward:risk over a 70/30 train/test split">
        <div className="toolbar" style={{ gap: 10, flexWrap: "wrap" }}>
          <label className="dim" style={{ fontSize: 12 }}>Strategy
            <select style={{ marginLeft: 6 }} value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {(opt?.strategies ?? [strategy]).filter((s) => s !== "Custom Strategy").map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label className="dim" style={{ fontSize: 12 }}>Symbol
            <select style={{ marginLeft: 6 }} value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {(opt?.symbols ?? [symbol]).map((s) => <option key={s}>{s}</option>)}
            </select>
          </label>
          <label className="dim" style={{ fontSize: 12 }}>Timeframe
            <select style={{ marginLeft: 6 }} value={tf} onChange={(e) => setTf(e.target.value)}>
              {(opt?.timeframes ?? ENTRY_TIMEFRAMES).map((t) => <option key={t}>{t}</option>)}
            </select>
          </label>
          <label className="dim" style={{ fontSize: 12 }}>Bars
            <select style={{ marginLeft: 6 }} value={bars} onChange={(e) => setBars(Number(e.target.value))}>
              {[2000, 4000, 6000, 8000].map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </label>
          <button className="btn btn-primary" disabled={busy} onClick={run}>
            <Icon name="play" size={13} /> {busy ? "Sweeping…" : "Run sweep"}</button>
        </div>
        {err && <div className="banner" style={{ marginTop: 10, fontSize: 12 }}><Icon name="warning" size={13} className="amber" /> {err}</div>}
      </Card>

      {res && (
        <>
          <div className="stat-row">
            <StatCard label="Verdict" value={res.verdict.replace("_", " ")} tone={(VERDICT_TONE[res.verdict] ?? "default") as never} />
            <StatCard label="Best min-score × R:R" value={best ? `${best.min_score} · ${best.rr}R` : "—"} sub="in-sample winner" />
            <StatCard label="Train net R" value={`${(res.train?.net_r ?? 0) >= 0 ? "+" : ""}${res.train?.net_r ?? 0}R`} tone={tone(res.train?.net_r ?? 0)} sub={`${res.train?.trades ?? 0} trades`} />
            <StatCard label="Validation net R (unseen)" value={`${(res.validation?.net_r ?? 0) >= 0 ? "+" : ""}${res.validation?.net_r ?? 0}R`} tone={tone(res.validation?.net_r ?? 0)} sub={`PF ${res.validation?.profit_factor ?? 0}`} />
          </div>

          <Card title="Results matrix" subtitle="net R per parameter combo on the training slice · gold = best · brighter = stronger">
            <div style={{ overflowX: "auto" }}>
              <table className="data-table" style={{ textAlign: "center", minWidth: 380 }}>
                <thead>
                  <tr><th style={{ textAlign: "left" }}>min-score ↓ / R:R →</th>{RR.map((rr) => <th key={rr}>{rr}R</th>)}</tr>
                </thead>
                <tbody>
                  {MS.map((ms) => (
                    <tr key={ms}>
                      <td style={{ textAlign: "left" }}><b>{ms}</b></td>
                      {RR.map((rr) => {
                        const t = trialAt.get(`${ms}|${rr}`);
                        if (!t) return <td key={rr} className="dim">—</td>;
                        const isBest = best && best.min_score === ms && best.rr === rr;
                        const strength = bestNet > 0 ? Math.max(0, t.net_r) / bestNet : 0;
                        return (
                          <td key={rr} title={`${t.trades} trades · PF ${t.profit_factor} · net ${t.net_r}R`}
                            style={{
                              background: isBest ? "rgba(234,181,79,0.28)" : t.net_r >= 0 ? `rgba(34,197,94,${(strength * 0.4).toFixed(2)})` : "rgba(239,68,68,0.16)",
                              border: isBest ? "1px solid var(--gold)" : undefined, fontWeight: 600,
                            }}>
                            <div className={rt(t.net_r)}>{t.net_r >= 0 ? "+" : ""}{t.net_r}R</div>
                            <div className="dim" style={{ fontSize: 10.5 }}>{t.trades}t · PF {t.profit_factor}</div>
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="Out-of-sample check" subtitle="does the in-sample winner survive on unseen data?">
            <div className="row-actions" style={{ justifyContent: "space-between", marginBottom: 8 }}>
              <b>Best config: min-score {best?.min_score}, {best?.rr}R</b>
              <Badge text={res.verdict.replace("_", " ")} tone={(VERDICT_TONE[res.verdict] ?? "default") as never} />
            </div>
            <p className="dim" style={{ margin: "0 0 10px" }}>{res.note}</p>
            <table className="data-table" style={{ fontSize: 12.5 }}>
              <thead><tr><th></th><th>Net R</th><th>Profit factor</th><th>Trades</th></tr></thead>
              <tbody>
                <tr><td className="dim">Baseline (train)</td><td className={rt(res.baseline_train?.net_r ?? 0)}>{res.baseline_train?.net_r ?? 0}R</td><td>{res.baseline_train?.profit_factor ?? "—"}</td><td className="dim">{res.baseline_train?.trades ?? 0}</td></tr>
                <tr><td className="dim">Baseline (test)</td><td className={rt(res.baseline_test?.net_r ?? 0)}>{res.baseline_test?.net_r ?? 0}R</td><td>{res.baseline_test?.profit_factor ?? "—"}</td><td className="dim">{res.baseline_test?.trades ?? 0}</td></tr>
                <tr><td><b>Tuned (train)</b></td><td className={rt(res.train?.net_r ?? 0)}>{res.train?.net_r ?? 0}R</td><td>{res.train?.profit_factor ?? "—"}</td><td className="dim">{res.train?.trades ?? 0}</td></tr>
                <tr style={{ background: "rgba(234,181,79,0.06)" }}><td><b>Tuned (test · unseen)</b></td><td className={rt(res.validation?.net_r ?? 0)}>{res.validation?.net_r ?? 0}R</td><td>{res.validation?.profit_factor ?? "—"}</td><td className="dim">{res.validation?.trades ?? 0}</td></tr>
              </tbody>
            </table>
            <div className="banner" style={{ marginTop: 10, fontSize: 11.5 }}><Icon name="info" size={12} />
              A higher training score means nothing if the unseen (test) row doesn't also improve — that's how you tell a real edge from curve-fitting. Validate any adopted config in paper trading before sizing up.</div>
          </Card>
        </>
      )}
    </>
  );
}
