import { useMemo, useState } from "react";
import { ENTRY_TIMEFRAMES } from "../lib/timeframes";
import Card from "../components/common/Card";
import Icon from "../components/common/Icon";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { useLive, apiPostJson, type AIAnalysis, type AIProfile, type AIConfidenceAccuracy, type AIAlerts, type AIInsights, type AICoach, type TradeMemoryInsights, type AIRecommendations, type AIRecommendation } from "../lib/api";
import { useApp } from "../app-context";

// format a setting value for display (fractions shown as %)
const fmtVal = (v: number | boolean, unit: string) => {
  if (typeof v === "boolean") return v ? "on" : "off";
  if (unit === "%") return `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`;
  return `${v}${unit ? " " + unit : ""}`;
};

const SEV_TONE: Record<string, string> = { critical: "red", warning: "amber", success: "green", info: "default" };

const CONF_TONE: Record<string, string> = {
  "Very High": "green", High: "green", Medium: "amber", Low: "red", "Very Low": "red",
};
const DECISION_TONE: Record<string, string> = { BUY: "green", SELL: "red", WAIT: "amber", SKIP: "red" };
const money = (n?: number | null) => (n == null ? "—" : `$${n.toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
const TFS = ENTRY_TIMEFRAMES;
const SIDES = [["", "Auto"], ["long", "Long"], ["short", "Short"]] as const;

export default function AIIntelligencePage() {
  const { go, toast } = useApp();
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [tf, setTf] = useState<(typeof TFS)[number]>("1h");
  const [side, setSide] = useState("");
  const [lev, setLev] = useState(1);
  const [input, setInput] = useState("BTCUSDT");

  const qs = `symbol=${encodeURIComponent(symbol)}&timeframe=${tf}${side ? `&side=${side}` : ""}&leverage=${lev}`;
  const { data: a } = useLive<AIAnalysis>(`/ai/analyze?${qs}`, 20000);
  const { data: profile } = useLive<AIProfile>("/ai/profile", 30000);
  const { data: calib } = useLive<AIConfidenceAccuracy>("/ai/confidence-accuracy", 30000);
  const { data: alerts } = useLive<AIAlerts>("/ai/alerts", 30000);
  const { data: insights } = useLive<AIInsights>("/ai/insights", 30000);
  const { data: coach } = useLive<AICoach>("/ai/coach", 30000);
  const { data: patterns } = useLive<TradeMemoryInsights>("/trade-memory/insights", 30000);
  const recs = useLive<AIRecommendations>("/ai/recommendations", 30000);
  const [applying, setApplying] = useState<string>("");

  const applyRec = async (r: AIRecommendation) => {
    setApplying(r.id);
    try {
      await apiPostJson("/settings", { [r.setting]: r.suggested });
      toast(`Applied: ${r.title}`, "success");
      recs.refetch();
    } catch {
      toast("Could not apply — needs the control secret", "error");
    } finally { setApplying(""); }
  };

  const ma = a?.market_analysis;
  const bias = ma?.available ? ma.bias : "—";
  const trendStrength = ma?.available ? ma.trend?.strength_label : "—";
  const risk = a?.risk_analysis;

  const scoreColor = useMemo(() => {
    const s = a?.overall_score ?? 0;
    return s >= 70 ? "var(--green)" : s >= 55 ? "var(--amber, #f59e0b)" : "var(--red)";
  }, [a]);

  const apply = () => setSymbol(input.trim().toUpperCase() || "BTCUSDT");

  return (
    <>
      <PageHeader title="AI Trading Intelligence"
        subtitle="On-demand pre-trade analysis — setup score, confidence, explanation and risk, composed from the engine's own reads"
        actions={<button className="btn btn-soft btn-sm" onClick={() => go("AI Assistant")}><Icon name="bot" size={13} /> AI Assistant</button>}
      />

      {/* controls */}
      <div className="toolbar" style={{ gap: 8, flexWrap: "wrap" }}>
        <div className="search" style={{ maxWidth: 220 }}>
          <Icon name="search" size={15} className="search-icon" />
          <input value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()} placeholder="Symbol e.g. BTCUSDT" />
        </div>
        <button className="btn btn-soft" onClick={apply}>Analyze</button>
        <div className="chips">{TFS.map((t) => <button key={t} className={`chip-btn ${tf === t ? "active" : ""}`} onClick={() => setTf(t)}>{t}</button>)}</div>
        <div className="chips">{SIDES.map(([v, l]) => <button key={v} className={`chip-btn ${side === v ? "active" : ""}`} onClick={() => setSide(v)}>{l}</button>)}</div>
        <div className="chips">{[1, 5, 10, 20].map((x) => <button key={x} className={`chip-btn ${lev === x ? "active" : ""}`} onClick={() => setLev(x)}>{x}x</button>)}</div>
      </div>

      {/* AI recommendations — one-click apply to the live risk settings */}
      {!!recs.data && (
        <Card title="AI Recommendations"
          subtitle={recs.data.count > 0 ? `${recs.data.count} setting${recs.data.count === 1 ? "" : "s"} the AI would change` : "Your risk guardrails look sound"}
          right={<button className="btn btn-soft btn-sm" onClick={() => go("Settings")}>Open Settings</button>}>
          {recs.data.count === 0 ? (
            <div className="dim" style={{ fontSize: 12.5, padding: 6 }}><Icon name="check" size={13} className="pos" /> {recs.data.note}</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {recs.data.recommendations.map((r) => (
                <div key={r.id} style={{ display: "flex", alignItems: "center", gap: 12, border: "1px solid var(--card-border)", borderRadius: 9, padding: "10px 12px" }}>
                  <Badge text={r.severity === "warning" ? "FIX" : "TIP"} tone={r.severity === "warning" ? "amber" : "default"} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{r.title}</div>
                    <div className="dim" style={{ fontSize: 11.5 }}>{r.why}</div>
                  </div>
                  <div style={{ fontFamily: "var(--mono)", fontSize: 12, whiteSpace: "nowrap" }}>
                    <span className="neg">{fmtVal(r.current, r.unit)}</span>
                    <span className="dim"> → </span>
                    <span className="pos">{fmtVal(r.suggested, r.unit)}</span>
                  </div>
                  <button className="btn btn-primary btn-sm" disabled={applying === r.id} onClick={() => applyRec(r)}>
                    {applying === r.id ? "Applying…" : "Apply"}</button>
                </div>
              ))}
              <div className="dim" style={{ fontSize: 11 }}>{recs.data.note}</div>
            </div>
          )}
        </Card>
      )}

      {/* headline widgets */}
      <div className="stat-row">
        <StatCard label="Decision" value={a?.decision ?? "—"} tone={(DECISION_TONE[a?.decision ?? ""] ?? "default") as never}
          sub={a?.side ? `${a.side} · ${a.symbol}` : a?.symbol} />
        <StatCard label="AI Confidence" value={a?.confidence_level ?? "—"} tone={(CONF_TONE[a?.confidence_level ?? ""] ?? "default") as never}
          sub={a ? `${a.confidence_pct}%` : ""} />
        <StatCard label="Setup Score" value={a ? `${a.overall_score}/100` : "—"} color={scoreColor}
          sub={a?.engine_score != null ? `engine gate ${a.engine_score}` : ""} />
        <StatCard label="Market Bias" value={bias} sub={`trend ${trendStrength}`} />
        <StatCard label="Min Score" value={a ? String(a.min_score) : "—"} sub={a?.allowed ? "setup qualifies" : "below threshold"}
          tone={a?.allowed ? "green" : "amber"} />
      </div>

      {/* AI alert feed */}
      {!!alerts?.alerts?.length && (
        <Card title="AI Alerts" subtitle={`${alerts.count} live — across ${alerts.checked?.length ?? 0} tracked symbols`}>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {alerts.alerts.map((al, i) => (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "7px 10px",
                borderRadius: 8, background: "rgba(255,255,255,0.03)", border: "1px solid var(--card-border-soft)" }}>
                <Badge text={al.severity} tone={(SEV_TONE[al.severity] ?? "default") as never} />
                <b style={{ fontSize: 13 }}>{al.title}</b>
                <span className="dim" style={{ fontSize: 12.5 }}>{al.detail}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* live market insights */}
      {!!insights?.insights?.length && (
        <Card title="Live Market Insights" subtitle={`Real-time reads across ${insights.symbols?.length ?? 0} symbols · ${insights.timeframe ?? ""}`}>
          <div className="chips" style={{ gap: 8 }}>
            {insights.insights.map((ins, i) => (
              <span key={i} className="ui-badge" style={{ padding: "6px 11px", fontSize: 12.5,
                background: "rgba(124,185,232,0.08)", border: "1px solid var(--card-border-soft)", color: "var(--text)" }}>
                <Icon name={ins.kind === "volatility" ? "warning" : ins.kind === "reversal" ? "refresh" : "chart"}
                  size={12} className="dim" /> {ins.text}
              </span>
            ))}
          </div>
        </Card>
      )}

      <div className="grid-2-eq">
        {/* score breakdown */}
        <Card title="Setup Score" subtitle={`${a?.overall_score ?? 0} / 100 — ${a?.confidence_level ?? ""}`}>
          {(a?.score_breakdown ?? []).map((c) => {
            const pct = (c.score / c.max) * 100;
            const col = pct >= 75 ? "var(--green)" : pct >= 50 ? "var(--amber, #f59e0b)" : "var(--red)";
            return (
              <div key={c.category} style={{ marginBottom: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 4 }}>
                  <span className="dim">{c.category}</span><b>{c.score}/{c.max}</b>
                </div>
                <div style={{ height: 7, borderRadius: 6, background: "rgba(255,255,255,0.06)" }}>
                  <div style={{ width: `${pct}%`, height: "100%", borderRadius: 6, background: col }} />
                </div>
              </div>
            );
          })}
          {!a?.score_breakdown?.length && <div className="dim" style={{ padding: 10 }}>Analyzing…</div>}
        </Card>

        {/* explanation */}
        <Card title="AI Explanation" subtitle={a?.recommendation ?? ""}>
          <div style={{ marginBottom: 8 }}>
            <Badge text={a?.decision ?? "—"} tone={(DECISION_TONE[a?.decision ?? ""] ?? "default") as never} />
            {a?.side && <span className="dim" style={{ marginLeft: 8 }}>{a.side} · confidence {a.confidence_pct}%</span>}
          </div>
          <div className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, margin: "8px 0 4px" }}>Reasons</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.6 }}>
            {(a?.reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}
            {!a?.reasons?.length && <li className="dim">No confirming reasons yet.</li>}
          </ul>
          {!!a?.failed_checks?.length && (
            <>
              <div className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, margin: "10px 0 4px" }}>Not confirmed</div>
              <div className="chips">{a.failed_checks.map((f) => <Badge key={f} text={f} tone="red" />)}</div>
            </>
          )}
        </Card>
      </div>

      {/* risk analysis */}
      <Card title="Risk Analysis" subtitle={risk?.warning ?? "Pre-trade risk for the proposed setup"}>
        {!risk ? <div className="dim" style={{ padding: 10 }}>No directional setup — pick Long/Short or wait for a bias.</div> : (
          <>
            {risk.warning && <div className="banner" style={{ marginBottom: 10, borderColor: "rgba(239,68,68,0.4)", background: "rgba(239,68,68,0.08)" }}>
              <Icon name="warning" size={14} className="neg" /> {risk.warning}</div>}
            <div className="stat-row">
              <StatCard label="Max Loss" value={money(risk.max_loss)} tone="red" sub={`${risk.risk_pct}% of equity`} />
              <StatCard label="Expected Profit" value={money(risk.expected_profit)} tone="green" sub={`RR ${risk.risk_reward}`} />
              <StatCard label="Margin Used" value={money(risk.margin_used)} sub={`${risk.leverage}x leverage`} />
              <StatCard label="Liquidation" value={risk.liquidation_price != null ? money(risk.liquidation_price) : "— (spot)"}
                tone={risk.liquidation_price != null ? "amber" : "default"} />
              <StatCard label="Exposure" value={`${risk.portfolio_exposure_pct}%`} tone={risk.excessive ? "red" : "default"} sub="of equity" />
            </div>
            {a?.setup && <div className="detail-grid" style={{ marginTop: 12 }}>
              <div><span className="dim">Entry</span><b>{money(a.setup.entry)}</b></div>
              <div><span className="dim">Stop</span><b>{money(a.setup.stop)}</b></div>
              <div><span className="dim">Target</span><b>{money(a.setup.target)}</b></div>
            </div>}
          </>
        )}
      </Card>

      {/* trader profile */}
      <div className="grid-2-eq">
        <Card title="Your Trading Profile" subtitle={profile?.note ?? ""}>
          <div className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4 }}>Strengths</div>
          <ul style={{ margin: "0 0 10px", paddingLeft: 18, fontSize: 13, lineHeight: 1.6 }}>
            {(profile?.strengths ?? []).map((s, i) => <li key={i} className="pos">{s}</li>)}
            {!profile?.strengths?.length && <li className="dim">Building as trades close…</li>}
          </ul>
          <div className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4 }}>Weaknesses</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.6 }}>
            {(profile?.weaknesses ?? []).map((w, i) => <li key={i} className="neg">{w}</li>)}
            {!profile?.weaknesses?.length && <li className="dim">None flagged yet.</li>}
          </ul>
        </Card>
        <Card title="Confidence Accuracy" subtitle={calib?.verdict ?? "Do higher-confidence setups actually win more?"}>
          {!calib?.ready ? <div className="dim" style={{ padding: 10 }}>{calib?.verdict ?? "Grading trades as they close…"}</div> : (
            <>
              <div style={{ marginBottom: 8 }}>
                <Badge text={calib.calibrated ? "Well calibrated" : "Miscalibrated"} tone={calib.calibrated ? "green" : "red"} />
                <span className="dim" style={{ marginLeft: 8, fontSize: 12 }}>
                  high {calib.high_conf_win_rate}% vs low {calib.low_conf_win_rate}%
                  {calib.spread_pts != null && ` (${calib.spread_pts > 0 ? "+" : ""}${calib.spread_pts} pts)`}</span>
              </div>
              <table className="data-table" style={{ fontSize: 12.5 }}>
                <thead><tr><th>Confidence</th><th>Trades</th><th>Win %</th><th>Avg R</th></tr></thead>
                <tbody>
                  {calib.by_confidence.filter((b) => b.trades > 0).map((b) => (
                    <tr key={b.level}>
                      <td><b>{b.level}</b></td><td className="dim">{b.trades}</td>
                      <td className={b.win_rate >= 50 ? "pos" : "neg"}>{b.win_rate}%</td>
                      <td className="dim">{b.avg_rr ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </Card>
        <Card title="Market Read" subtitle={`${a?.symbol ?? ""} · ${a?.timeframe ?? ""}${a?.data_source ? ` · ${a.data_source}` : ""}`}>
          {!ma?.available ? <div className="dim" style={{ padding: 10 }}>{ma?.note ?? "Analyzing…"}</div> : (
            <div className="detail-grid">
              <div><span className="dim">Bias</span><b>{ma.bias}</b></div>
              <div><span className="dim">Trend strength</span><b>{ma.trend?.strength_label}</b></div>
              <div><span className="dim">Structure</span><b>{ma.structure?.state}</b></div>
              <div><span className="dim">Break of structure</span><b>{String(ma.structure?.break_of_structure)}</b></div>
              <div><span className="dim">Change of character</span><b>{ma.structure?.change_of_character ? "yes" : "no"}</b></div>
              <div><span className="dim">Liquidity sweep</span><b>{String(ma.liquidity?.sweep)}</b></div>
              <div><span className="dim">Volume</span><b>{ma.volume?.label}</b></div>
              <div><span className="dim">Volatility</span><b>{ma.volatility?.label}</b></div>
            </div>
          )}
        </Card>
      </div>

      {/* AI coach + pattern detection */}
      <div className="grid-2-eq">
        <Card title="AI Coach" subtitle={coach?.headline ?? "Coaching over your closed trades"}>
          {!coach?.ready ? <div className="dim" style={{ padding: 10 }}>{coach?.headline ?? "No closed trades yet."}</div> : (
            <>
              <div className="stat-row">
                <StatCard label="Trades" value={String(coach.trades)} />
                <StatCard label="Win Rate" value={coach.win_rate != null ? `${coach.win_rate}%` : "—"}
                  tone={(coach.win_rate ?? 0) >= 50 ? "green" : "amber"} />
                <StatCard label="Risk Discipline" value={coach.risk_discipline}
                  tone={coach.risk_discipline === "Excellent" ? "green" : coach.risk_discipline === "Needs work" ? "red" : "default"} />
              </div>
              {coach.main_mistake && <div style={{ marginTop: 10 }}>
                <span className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 }}>Main mistake</span>
                <div className="neg" style={{ fontSize: 13 }}>{coach.main_mistake}</div></div>}
              <div style={{ marginTop: 10 }}>
                <span className="dim" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 }}>Suggestion</span>
                <div style={{ fontSize: 13 }}>{coach.suggestion}</div></div>
            </>
          )}
        </Card>

        <Card title="Pattern Detection" subtitle="Best/worst sessions, symbols, strategies & repeated mistakes">
          {!patterns?.overall?.trades ? <div className="dim" style={{ padding: 10 }}>Detecting patterns as trades close…</div> : (
            <div className="detail-grid">
              <div><span className="dim">Best session</span><b className="pos">{patterns.best_session?.name ?? "—"}</b></div>
              <div><span className="dim">Worst session</span><b className="neg">{patterns.worst_session?.name ?? "—"}</b></div>
              <div><span className="dim">Best symbol</span><b className="pos">{patterns.by_symbol?.[0]?.name ?? "—"}</b></div>
              <div><span className="dim">Best strategy</span><b>{patterns.by_strategy?.[0]?.name ?? "—"}</b></div>
              <div><span className="dim">Avg hold</span><b>{patterns.avg_hold_seconds != null ? `${Math.round(patterns.avg_hold_seconds / 60)}m` : "—"}</b></div>
              <div><span className="dim">Avg expectancy</span><b>{patterns.overall?.expectancy != null ? `${patterns.overall.expectancy}R` : "—"}</b></div>
              {(patterns.mistakes ?? []).slice(0, 2).map((m, i) => (
                <div key={i} style={{ gridColumn: "1 / -1" }}><span className="dim">Repeated mistake</span>
                  <b className="neg">{m.mistake}{m.count ? ` (×${m.count})` : ""}</b></div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
