import { useState } from "react";
import { ENTRY_TIMEFRAMES, DEFAULT_ENTRY_TIMEFRAME } from "../lib/timeframes";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { useApp } from "../app-context";
import { apiGet, apiPost, apiPostJson, useLive,
  type EvoDashboard, type Lesson, type Upgrade, type Experiment, type VersionCompare, type StrategyMemory } from "../lib/api";
import MarketContextPanel from "../components/evolution/MarketContext";
import SelfLearningPanel from "../components/evolution/SelfLearning";

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"];
const statusTone = (s: string) => ({ Approved: "green", Rejected: "red", "Paper tested": "blue",
  Backtested: "blue", Testing: "amber", Suggested: "amber", Archived: "default" } as any)[s] ?? "default";

export default function EvolutionPage() {
  const app = useApp();
  const dash = useLive<EvoDashboard>("/evolution/dashboard", 8000);
  const lessons = useLive<{ lessons: Lesson[]; weekly: number }>("/evolution/lessons", 6000);
  const upgrades = useLive<{ upgrades: Upgrade[] }>("/evolution/upgrades", 6000);
  const versions = useLive<VersionCompare>("/evolution/versions?strategy=SMC", 6000);
  const [learnSym, setLearnSym] = useState("BTCUSDT");
  const [busy, setBusy] = useState(false);

  const d = dash.data;

  const learn = async () => {
    setBusy(true);
    try {
      const r = await apiPost<any>(`/evolution/learn?symbol=${learnSym}&limit=1200`);
      if (r.error) { app.toast(r.error, "error"); return; }
      app.toast(`Studied ${r.studied_trades} trades — ${r.lessons?.length ?? 0} lessons, ${r.upgrades?.length ?? 0} suggestions`, "success");
      lessons.refetch(); upgrades.refetch(); dash.refetch();
    } catch { app.toast("Learn failed — backend reachable?", "error"); }
    finally { setBusy(false); }
  };

  const setLessonStatus = async (id: string, status: string) => {
    try { await apiPost(`/evolution/lessons/${id}/status?status=${status}`); lessons.refetch(); }
    catch { app.toast("Update failed", "error"); }
  };
  const setUpgradeStatus = async (id: string, status: string) => {
    try {
      const r = await apiPost<any>(`/evolution/upgrades/${id}/status?status=${status}`);
      if (r?.error) app.toast(r.error, "error"); else { upgrades.refetch(); dash.refetch(); }
    } catch { app.toast("Approve/Reject requires the webhook secret", "error"); }
  };
  const promote = async (id: string) => {
    try {
      const r = await apiPostJson<any>(`/evolution/upgrades/${id}/promote`, {});
      if (r?.error || r?.detail) app.toast(r.error || r.detail, "error");
      else { app.toast(`Created ${r.version.label} — backtest done, sim/paper pending`, "success"); versions.refetch(); upgrades.refetch(); }
    } catch { app.toast("Promote failed — approve the upgrade first", "error"); }
  };
  const advanceVersion = async (vid: string, gate: string) => {
    try {
      const r = await apiPost<any>(`/evolution/versions/${vid}/advance?gate=${gate}`);
      if (gate === "live_unlock" && r?.live_unlocked === false) app.toast("Live stays locked — no broker connected (by design)", "info");
      else if (r?.error) app.toast(r.error, "error");
      versions.refetch();
    } catch { app.toast("Advance failed", "error"); }
  };

  return (
    <>
      <PageHeader title="Evolution Engine"
        subtitle="AI research lab — observe → diagnose → suggest → version → backtest → paper → your approval → live" />

      {/* dashboard widgets */}
      <div className="stat-row">
        <StatCard label="Market Sentiment" value={d?.sentiment?.available ? (d.sentiment.mood ?? "—") : "Unavailable"}
          tone={d?.sentiment?.mood?.includes("Greed") ? "red" : d?.sentiment?.mood?.includes("Fear") ? "amber" : "default"}
          sub={d?.sentiment?.available ? `F&G ${d.sentiment.fear_greed}` : "no live feed"} />
        <StatCard label="Risk Mode" value={(d?.sentiment?.risk_mode ?? "Normal").split("—")[0]} />
        <StatCard label="Lessons This Week" value={String(d?.lessons_weekly ?? 0)} sub={`${d?.lessons_total ?? 0} total`} />
        <StatCard label="Upgrades Approved" value={String(d?.upgrade_status?.Approved ?? 0)} tone="green"
          sub={`${d?.upgrade_status?.Suggested ?? 0} suggested · ${d?.upgrade_status?.Rejected ?? 0} rejected`} />
      </div>

      <Card title="Safe Evolution Workflow" right={<Badge text="live needs approval" tone="amber" />}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          {(d?.workflow ?? []).map((w, i) => (
            <span key={w} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className="ui-badge" style={{ background: "rgba(139,92,246,0.14)", color: "var(--purple-2)", border: "1px solid rgba(139,92,246,0.25)" }}>{w}</span>
              {i < (d?.workflow?.length ?? 0) - 1 && <Icon name="chevron" size={12} className="dim" />}
            </span>
          ))}
        </div>
        <p className="dim" style={{ marginTop: 8 }}><Icon name="lock" size={13} /> {d?.live_rule}</p>
      </Card>

      <MarketContextPanel />

      <SelfLearningPanel />

      <StrategyDNA />

      <div className="evo-grid">
       <div className="evo-col">
      <Card title="Study & Learn" subtitle="analyse real replay history → derive evidence-based lessons">
        <div className="row-actions" style={{ justifyContent: "flex-start", gap: 8 }}>
          <select value={learnSym} onChange={(e) => setLearnSym(e.target.value)}>{SYMBOLS.map((s) => <option key={s}>{s}</option>)}</select>
          <button className="btn btn-primary" disabled={busy} onClick={learn}><Icon name="bot" size={14} /> {busy ? "Studying…" : "Study & Learn"}</button>
        </div>
        <p className="dim" style={{ marginTop: 8 }}>The bot only records a lesson when the evidence supports it — it will not invent problems.</p>
      </Card>

      {/* suggested upgrades */}
      <Card title="Suggested Upgrades" subtitle="every suggestion shows reason, evidence, benefit, risk">
        {(upgrades.data?.upgrades?.length ?? 0) === 0 ? (
          <div className="dim ta-center" style={{ padding: 18 }}>No suggestions yet — run <b>Study &amp; Learn</b>.</div>
        ) : (upgrades.data!.upgrades.map((u) => (
          <div key={u.id} className="card" style={{ marginBottom: 8, background: "var(--card-2)" }}>
            <div className="row-actions" style={{ justifyContent: "space-between" }}>
              <b>{u.title}</b>
              <Badge text={u.status} tone={statusTone(u.status)} />
            </div>
            <div className="risk-list" style={{ marginTop: 6 }}>
              <div className="risk-item"><span className="dim">Reason</span> <span>{u.reason}</span></div>
              <div className="risk-item"><span className="dim">Evidence</span> <span className="dim">{u.evidence}</span></div>
              <div className="risk-item"><span className="dim">Expected benefit</span> <span>{u.expected_benefit}</span></div>
              <div className="risk-item"><span className="dim">Risk</span> <span className="amber">{u.risk}</span></div>
              <div className="risk-item"><span className="dim">Backtest required</span> <b>{u.backtest_required ? "Yes" : "No"}</b> · <span className="dim">confidence {u.confidence}</span></div>
            </div>
            <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginTop: 8 }}>
              <button className="btn btn-soft" onClick={() => setUpgradeStatus(u.id, "Backtested")}>Mark Backtested</button>
              <button className="btn btn-soft" onClick={() => setUpgradeStatus(u.id, "Paper tested")}>Mark Paper-tested</button>
              <button className="btn btn-primary" onClick={() => setUpgradeStatus(u.id, "Approved")}><Icon name="check" size={13} /> Approve</button>
              <button className="btn btn-danger" onClick={() => setUpgradeStatus(u.id, "Rejected")}><Icon name="close" size={13} /> Reject</button>
              {u.status === "Approved" && u.auto_applicable && (
                <button className="btn btn-soft" onClick={() => promote(u.id)} title="Apply the patch and create a new strategy version (enters the safety flow)">
                  <Icon name="rocket" size={13} /> Promote to Version
                </button>
              )}
              {u.status === "Approved" && !u.auto_applicable && (
                <span className="dim" style={{ fontSize: 12 }}>Manual change — edit the strategy by hand.</span>
              )}
            </div>
          </div>
        )))}
      </Card>
       </div>
       <div className="evo-col">
      <ExperimentLab />
      <VersionsPanel data={versions.data} onAdvance={advanceVersion} />
       </div>
      </div>

      {/* learning journal */}
      <Card title="Bot Learning Journal" subtitle={`${lessons.data?.weekly ?? 0} new this week`}>
        <div className="tablewrap">
          <table className="data-table">
            <thead><tr><th>Date</th><th>Symbol</th><th>Strategy</th><th>Lesson</th><th>Suggested fix</th><th>Conf.</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {(lessons.data?.lessons ?? []).map((l) => (
                <tr key={l.id}>
                  <td className="dim mono">{l.created_at.slice(0, 10)}</td>
                  <td>{l.symbol}</td><td className="dim">{l.strategy}</td>
                  <td>{l.lesson}</td><td className="dim">{l.suggested_fix}</td>
                  <td>{l.confidence}</td>
                  <td><Badge text={l.status} tone={statusTone(l.status)} /></td>
                  <td><div className="row-actions">
                    <button className="icon-btn sm ok" title="Approve" onClick={() => setLessonStatus(l.id, "Approved")}><Icon name="check" size={13} /></button>
                    <button className="icon-btn sm neg" title="Reject" onClick={() => setLessonStatus(l.id, "Rejected")}><Icon name="close" size={13} /></button>
                  </div></td>
                </tr>
              ))}
              {(lessons.data?.lessons?.length ?? 0) === 0 && <tr><td colSpan={8} className="dim ta-center" style={{ padding: 16 }}>No lessons recorded yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

    </>
  );
}

function Gate({ label, done, locked }: { label: string; done: boolean; locked?: boolean }) {
  return (
    <span className="ui-badge" style={{
      background: locked ? "#ef444422" : done ? "#22c55e22" : "#5b647822",
      color: locked ? "#ef4444" : done ? "#22c55e" : "#8a93a6",
      display: "inline-flex", alignItems: "center", gap: 4,
    }}>
      <Icon name={locked ? "lock" : done ? "check" : "history"} size={11} /> {label}
    </span>
  );
}

function VersionsPanel({ data, onAdvance }: { data?: VersionCompare | null; onAdvance: (vid: string, gate: string) => void }) {
  const vers = data?.versions ?? [];
  return (
    <Card title="Strategy Versions (SMC)" subtitle={data?.best ? `best: ${data.best}` : "promote an approved upgrade to create a version"}
      right={<Badge text="live needs all gates + approval" tone="amber" />}>
      {vers.length === 0 ? (
        <div className="dim ta-center" style={{ padding: 18 }}>No versions yet. Approve an auto-applicable upgrade and click <b>Promote to Version</b>.</div>
      ) : vers.map((v) => (
        <div key={v.id} className="card" style={{ marginBottom: 8, background: "var(--card-2)" }}>
          <div className="row-actions" style={{ justifyContent: "space-between" }}>
            <b>{v.label}{data?.best === v.label && <span className="pos"> · best</span>}</b>
            <span className="dim mono">{v.created_at.slice(0, 10)}</span>
          </div>
          <div className="dim" style={{ fontSize: 12, marginTop: 2 }}>{v.note}</div>
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 12, marginTop: 6, flexWrap: "wrap" }}>
            <span className="dim">net {v.stats?.net_r ?? 0}R · PF {v.stats?.profit_factor ?? 0} · {v.stats?.total_trades ?? 0} trades</span>
          </div>
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
            <Gate label="Backtest" done={v.gates.backtest} />
            <Icon name="chevron" size={12} className="dim" />
            <Gate label="Simulation" done={v.gates.simulation} />
            <Icon name="chevron" size={12} className="dim" />
            <Gate label="Paper" done={v.gates.paper} />
            <Icon name="chevron" size={12} className="dim" />
            <Gate label="Live" done={v.gates.live_unlocked} locked={!v.gates.live_unlocked} />
          </div>
          <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginTop: 8 }}>
            {!v.gates.simulation && v.gates.backtest && <button className="btn btn-soft" onClick={() => onAdvance(v.id, "simulation")}>Run Simulation</button>}
            {v.gates.simulation && !v.gates.paper && <button className="btn btn-soft" onClick={() => onAdvance(v.id, "paper")}>Mark Paper-tested</button>}
            {v.gates.paper && !v.gates.live_unlocked && <button className="btn btn-danger" onClick={() => onAdvance(v.id, "live_unlock")}><Icon name="lock" size={13} /> Request Live Unlock</button>}
          </div>
        </div>
      ))}
    </Card>
  );
}

function ExperimentLab() {
  const app = useApp();
  const [symbol, setSymbol] = useState("BTCUSDT");
  // Was hardcoded to 4h with no way to change it, so a preset could only
  // ever be tested on a frame this system does not trade.
  const [tf, setTf] = useState<string>(DEFAULT_ENTRY_TIMEFRAME);
  const [presetA, setPresetA] = useState("ema_20_50");
  const [presetB, setPresetB] = useState("ema_9_33");
  const [res, setRes] = useState<Experiment | null>(null);
  const [busy, setBusy] = useState(false);

  const PRESETS: Record<string, any> = {
    ema_20_50: { rules: [{ type: "ema_cross", fast: 20, slow: 50, dir: "above" }], min_score: 60, label: "EMA 20/50" },
    ema_9_33: { rules: [{ type: "ema_cross", fast: 9, slow: 33, dir: "above" }], min_score: 60, label: "EMA 9/33" },
    score_75: { rules: [{ type: "ema_cross", fast: 20, slow: 50, dir: "above" }], min_score: 75, label: "Min score 75" },
    rsi_55: { rules: [{ type: "rsi", op: "above", value: 55 }], min_score: 60, label: "RSI > 55" },
  };
  const spec = (p: any) => ({ symbol, timeframe: tf, side: "long", entry: { op: "AND", rules: p.rules },
    stop: { type: "atr", mult: 1.5, period: 14 }, target: { type: "rr", rr: 2.0 }, risk_per_trade_pct: 0.01, min_score: p.min_score });

  const run = async () => {
    setBusy(true);
    try {
      const r = await apiPostJson<Experiment>("/evolution/experiment", {
        base: spec(PRESETS[presetA]), variant: spec(PRESETS[presetB]), bars: 4000,
      });
      setRes(r);
    } catch { app.toast("Experiment failed", "error"); }
    finally { setBusy(false); }
  };

  const vTone = (v: string) => v === "improvement" ? "green" : v === "overfit" ? "red" : v === "marginal" ? "amber" : "default";

  return (
    <Card title="Strategy Experiment Lab" subtitle="A/B with train/test split + overfitting guard">
      <div className="row-actions" style={{ justifyContent: "flex-start", gap: 8, flexWrap: "wrap" }}>
        <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>{SYMBOLS.map((s) => <option key={s}>{s}</option>)}</select>
        <select value={tf} onChange={(e) => setTf(e.target.value)} aria-label="Timeframe">{ENTRY_TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}</select>
        <span className="dim">Base</span>
        <select value={presetA} onChange={(e) => setPresetA(e.target.value)}>{Object.entries(PRESETS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>
        <span className="dim">vs Variant</span>
        <select value={presetB} onChange={(e) => setPresetB(e.target.value)}>{Object.entries(PRESETS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}</select>
        <button className="btn btn-primary" disabled={busy} onClick={run}><Icon name="chart" size={14} /> {busy ? "Testing…" : "Run Experiment"}</button>
      </div>

      {res && (
        <div style={{ marginTop: 10 }}>
          <div className="row-actions" style={{ justifyContent: "space-between" }}>
            <Badge text={res.verdict.replace("_", " ")} tone={vTone(res.verdict) as any} />
            <span className="dim">out-of-sample gain {res.test_gain_r >= 0 ? "+" : ""}{res.test_gain_r}R</span>
          </div>
          <p className="dim" style={{ marginTop: 6 }}>{res.note}</p>
          <div className="tablewrap">
            <table className="data-table">
              <thead><tr><th></th><th>Train net R</th><th>Train PF</th><th>Test net R</th><th>Test PF</th><th>Test trades</th></tr></thead>
              <tbody>
                {[res.a, res.b].map((x, i) => (
                  <tr key={i}>
                    <td><b>{x.label}</b></td>
                    <td>{x.train.net_r}</td><td>{x.train.profit_factor}</td>
                    <td className={x.test.net_r >= 0 ? "pos" : "neg"}>{x.test.net_r}</td>
                    <td>{x.test.profit_factor}</td><td>{x.test.trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {res.warnings.length > 0 && res.warnings.map((w, i) => (
            <p key={i} className="amber" style={{ marginTop: 4, display: "flex", gap: 6 }}><Icon name="warning" size={13} /> {w}</p>
          ))}
        </div>
      )}
    </Card>
  );
}

const DNA_STRATS = ["Decision Brain", "Trend Following", "Supply/Demand", "EMA 8/30", "EMA 20/50", "Liquidity Sweep"];

function StrategyDNA() {
  const [strategy, setStrategy] = useState("Decision Brain");
  const [m, setM] = useState<StrategyMemory | null>(null);
  const [busy, setBusy] = useState(false);
  const load = async () => {
    setBusy(true);
    try { setM(await apiGet<StrategyMemory>(`/memory/strategy?strategy=${encodeURIComponent(strategy)}&timeframe=15m&limit=800`)); }
    catch { /* ignore */ } finally { setBusy(false); }
  };
  const row = (label: string, b?: any) => b && (
    <div className="risk-item"><span className="dim">{label}</span>
      <b className={b.net_r >= 0 ? "pos" : "neg"}>{b.key} ({b.net_r >= 0 ? "+" : ""}{b.net_r}R)</b></div>
  );
  return (
    <Card title="Market Memory & Strategy DNA" subtitle="where this strategy performs best/worst — used as a live filter"
      right={<div className="row-actions" style={{ gap: 6 }}>
        <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>{DNA_STRATS.map((s) => <option key={s}>{s}</option>)}</select>
        <button className="btn btn-primary" disabled={busy} onClick={load}><Icon name="bot" size={14} /> {busy ? "Mining…" : "Build DNA"}</button>
      </div>}>
      {!m || !m.dna ? <div className="dim ta-center" style={{ padding: 14 }}>Mine real results into this strategy's memory + DNA.</div> : (
        <div className="grid-2-eq">
          <div>
            <div className="card-subtitle" style={{ marginBottom: 6 }}>DNA profile <Badge text={`${m.confidence} confidence`} tone={m.confidence === "high" ? "green" : m.confidence === "medium" ? "amber" : "default"} /></div>
            <div className="risk-list">
              <div className="risk-item"><span className="dim">Preferred market</span> <b>{m.dna.preferred_market}</b></div>
              <div className="risk-item"><span className="dim">Volatility / trend</span> <b>{m.dna.preferred_volatility} · {m.dna.preferred_trend}</b></div>
              <div className="risk-item"><span className="dim">Best session</span> <b>{m.dna.preferred_session}</b></div>
              <div className="risk-item"><span className="dim">Preferred symbols</span> <b className="pos">{m.dna.preferred_symbols.map((s) => s.replace("USDT", "")).join(", ") || "—"}</b></div>
              <div className="risk-item"><span className="dim">Avoid symbols</span> <b className="neg">{m.dna.avoid_symbols.map((s) => s.replace("USDT", "")).join(", ") || "—"}</b></div>
            </div>
          </div>
          <div>
            <div className="card-subtitle" style={{ marginBottom: 6 }}>Memory ({m.sample} trades)</div>
            <div className="risk-list">
              {row("Best regime", m.memory.best_regime)}
              {row("Worst regime", m.memory.worst_regime)}
              {row("Best session", m.memory.best_session)}
              {row("Best symbol", m.memory.best_symbol)}
              {row("Worst symbol", m.memory.worst_symbol)}
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}
