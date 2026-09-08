import { useState } from "react";
import Card from "../components/common/Card";
import { Badge, PageHeader, StatCard } from "../components/common/ui";
import { apiPostJson, useLive } from "../lib/api";
import { useApp } from "../app-context";

export default function MarketDataManager() {
  const { toast } = useApp(); const [symbol, setSymbol] = useState("BTCUSDT"); const [timeframe, setTimeframe] = useState("1h");
  const providers = useLive<any>("/market-data/providers", 8000);
  const status = useLive<any>(`/market-data/status?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`, 5000);
  const quality = useLive<any>(`/market-data/quality?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`, 5000);
  const run = async (path: string) => { try { await apiPostJson(path, { symbol, timeframe, period: "90d" }); toast("Market-data job started", "success"); status.refetch(); quality.refetch(); } catch { toast("Market-data request failed", "error"); } };
  const q = quality.data; const tone = q?.status === "healthy" ? "green" : q?.status === "incomplete" ? "amber" : "red";
  return <>
    <PageHeader title="Market Data Manager" subtitle="provider provenance · cache integrity · no synthetic candles" />
    <div className="stat-row"><StatCard label="Quality" value={q?.status ?? "Unavailable"} tone={tone as any} /><StatCard label="Score" value={String(q?.quality_score ?? 0)} /><StatCard label="Freshness" value={status.data?.freshness_seconds == null ? "—" : `${status.data.freshness_seconds}s`} /><StatCard label="Gaps" value={String(q?.gaps?.length ?? 0)} tone={q?.gaps?.length ? "amber" : "green"} /></div>
    <Card title="Dataset controls" subtitle="downloads and repairs use the registered provider only">
      <div className="row-actions market-data-controls" style={{ justifyContent: "flex-start", gap: 8 }}><input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} aria-label="Symbol" /><select value={timeframe} onChange={e => setTimeframe(e.target.value)}>{["1m","3m","5m","15m","30m","1h","4h","1d"].map(t => <option key={t}>{t}</option>)}</select><button className="btn btn-primary" onClick={() => run("/market-data/download")}>Download 90D</button><button className="btn btn-soft" onClick={() => run("/market-data/update")}>Update</button><button className="btn btn-warn" onClick={() => run("/market-data/repair")}>Repair gaps</button></div>
      <p className="dim">Canonical: {status.data?.metadata?.canonical_symbol ?? symbol} · Provider: {status.data?.metadata?.provider ?? "not downloaded"} · Dataset: {status.data?.metadata?.dataset_version ?? "—"}</p>
    </Card>
    <Card title="Quality report"><div className="tablewrap"><table className="data-table"><tbody><tr><th>Checksum</th><td><Badge text={q?.checksum_ok ? "Verified" : "Invalid"} tone={q?.checksum_ok ? "green" : "red"} /></td></tr><tr><th>Status</th><td>{q?.status ?? "Unavailable"}</td></tr><tr><th>Corrupt records</th><td>{q?.corrupt ?? 0}</td></tr><tr><th>Exact gaps</th><td>{(q?.gaps ?? []).map((g: any) => `${g.from} → ${g.to}`).join(", ") || "None"}</td></tr></tbody></table></div></Card>
    <Card title="Providers"><div className="tablewrap"><table className="data-table"><thead><tr><th>Provider</th><th>Markets</th><th>Availability</th><th>Requests</th><th>Failures</th><th>Last success</th></tr></thead><tbody>{(providers.data?.providers ?? []).map((p: any) => <tr key={p.name}><td>{p.name}</td><td>{p.markets.join(", ")}</td><td><Badge text={p.current_availability} tone={p.current_availability === "available" ? "green" : "amber"} /></td><td>{p.metrics.requests}</td><td>{p.metrics.failed}</td><td>{p.last_successful_request ?? "—"}</td></tr>)}</tbody></table></div></Card>
  </>;
}
