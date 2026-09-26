import { useState } from "react";
import Card from "../common/Card";
import Icon from "../common/Icon";
import { Badge } from "../common/ui";
import { useApp } from "../../app-context";
import { apiPostJson, useLive, type MarketContext } from "../../lib/api";

const moodTone = (m?: string | null) =>
  m?.includes("Greed") || m === "Euphoria" ? "red" : m?.includes("Fear") || m === "Panic" ? "amber" : "default";
const trendTone = (t?: string | null) => (t === "Bullish" ? "green" : t === "Bearish" ? "red" : "default");
const usd = (n?: number | null) => (n == null ? "—" : n >= 1e12 ? `$${(n / 1e12).toFixed(2)}T` : n >= 1e9 ? `$${(n / 1e9).toFixed(1)}B` : `$${n.toLocaleString()}`);

function W({ label, ok, children, note }: { label: string; ok: boolean; children: React.ReactNode; note?: string }) {
  return (
    <div className="stat-card">
      <span className="stat-label">{label}</span>
      {ok ? <span className="stat-value">{children}</span>
          : <span className="stat-value dim" style={{ fontSize: 14 }} title={note}>—</span>}
      {!ok && note && <span className="stat-sub dim" style={{ fontSize: 10 }}>{note.includes("Not connected") ? "Not connected" : "unavailable"}</span>}
    </div>
  );
}

/** Real live-market widgets (Evolution). Real APIs only; key-gated sources show
 *  'Not connected' and never fabricate data. */
export default function MarketContextPanel() {
  const app = useApp();
  const ctx = useLive<MarketContext>("/evolution/market-context", 60000);
  const [showProviders, setShowProviders] = useState(false);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const c = ctx.data;

  const saveKeys = async () => {
    try {
      const r = await apiPostJson<any>("/evolution/providers", keys);
      if (r?.error || r?.detail) app.toast(r.error || r.detail, "error");
      else { app.toast("Provider keys saved", "success"); setKeys({}); ctx.refetch(); }
    } catch { app.toast("Saving keys needs the webhook secret", "error"); }
  };

  const fresh = (s: number | null) => (s == null ? "—" : s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`);

  return (
    <Card title="Live Market Context"
      subtitle={c?.sentiment_summary ?? "real-world market data"}
      right={<div className="row-actions" style={{ gap: 8 }}>
        {c?.last_updated && <span className="dim" style={{ fontSize: 11 }}>updated {c.last_updated.slice(11, 19)} UTC</span>}
        <button className="btn btn-soft" onClick={() => setShowProviders((x) => !x)}><Icon name="settings" size={13} /> Data Providers</button>
      </div>}>
      <div className="stat-row" style={{ flexWrap: "wrap" }}>
        <W label="Fear & Greed" ok={!!c?.fear_greed?.available} note="Live source unavailable">
          {c?.fear_greed?.value} <Badge text={c?.fear_greed?.mood ?? ""} tone={moodTone(c?.fear_greed?.mood) as any} />
        </W>
        <W label="BTC Dominance" ok={!!c?.btc_dominance?.available}>{c?.btc_dominance?.value}%</W>
        <W label="Total Market Cap" ok={!!c?.total_mcap_usd?.available}>{usd(c?.total_mcap_usd?.value)}</W>
        <W label="ETH/BTC (30d)" ok={!!c?.eth_btc?.available} note={c?.eth_btc?.note}>
          <Badge text={c?.eth_btc?.trend ?? ""} tone={trendTone(c?.eth_btc?.trend) as any} /> {c?.eth_btc?.change_30d_pct != null ? `${c.eth_btc.change_30d_pct > 0 ? "+" : ""}${c.eth_btc.change_30d_pct}%` : ""}
        </W>
        <W label="BTC Funding" ok={!!c?.funding_rate?.available} note={c?.funding_rate?.note}>{c?.funding_rate?.value}%</W>
        <W label="BTC Open Interest" ok={!!c?.open_interest?.available} note={c?.open_interest?.note}>{c?.open_interest?.value?.toLocaleString()}</W>
        <W label="Liquidations" ok={false} note={c?.liquidations?.note}><span /></W>
        <W label="Econ Calendar" ok={!!c?.economic_calendar?.connected} note={c?.economic_calendar?.note}><span /></W>
      </div>

      {/* news */}
      <div style={{ marginTop: 10 }}>
        <WorldNews />

        <div className="card-subtitle" style={{ marginBottom: 6, marginTop: 12 }}>Crypto News — CryptoPanic {c?.news?.connected ? "" : "(optional, not connected)"}</div>
        {c?.news?.available && c.news.headlines.length ? (
          <div className="alert-stack">
            {c.news.headlines.map((h, i) => (
              <div key={i} className="exec-line">
                <span className="exec-time">{(h.published || "").slice(5, 10)}</span>{" "}
                {h.url ? <a href={h.url} target="_blank" rel="noreferrer" style={{ color: "#cfd6e4" }}>{h.title}</a> : h.title}
              </div>
            ))}
          </div>
        ) : (
          <div className="dim" style={{ fontSize: 13 }}><Icon name="info" size={13} /> {c?.news?.note ?? "No news yet."}</div>
        )}
      </div>

      {/* developer debug panel — provider name / status / last update / errors / freshness */}
      {c?.provider_debug && (
        <details style={{ marginTop: 10 }}>
          <summary className="dim" style={{ cursor: "pointer", fontSize: 12 }}>Debug — provider status &amp; freshness</summary>
          <div style={{ marginTop: 6, overflowX: "auto" }}>
            <table className="mono" style={{ fontSize: 11, width: "100%", borderCollapse: "collapse" }}>
              <thead><tr className="dim" style={{ textAlign: "left" }}>
                <th style={{ padding: "2px 8px 2px 0" }}>Provider</th><th>Status</th><th>Last update</th><th>Fresh</th><th>Error</th>
              </tr></thead>
              <tbody>
                {c.provider_debug.map((d) => (
                  <tr key={d.id} style={{ borderTop: "1px solid #1b1b1f" }}>
                    <td style={{ padding: "3px 8px 3px 0" }}>{d.label}</td>
                    <td style={{ color: d.status === "Not connected" ? "#8a93a6" : d.status.startsWith("Live") ? "#22c55e" : "#f59e0b" }}>{d.status}</td>
                    <td className="dim">{d.last_update ? d.last_update.slice(11, 19) : "—"}</td>
                    <td className="dim">{fresh(d.freshness_s)}</td>
                    <td className="neg" style={{ maxWidth: 220, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }} title={d.error ?? ""}>{d.error ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {/* provider settings */}
      {showProviders && (
        <div className="card" style={{ marginTop: 10, background: "#1b1b1f" }}>
          <div className="card-subtitle" style={{ marginBottom: 8 }}>Data Providers — connect real sources (keys stored locally)</div>
          {(c?.providers ?? []).map((p) => (
            <div key={p.id} className="row-actions" style={{ justifyContent: "space-between", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
              <span style={{ minWidth: 220 }}>{p.label}</span>
              {p.needs_key ? (
                <input type="password" placeholder={p.connected ? "•••• connected (leave blank to keep)" : "paste API key"}
                  value={keys[p.id] ?? ""} onChange={(e) => setKeys((k) => ({ ...k, [p.id]: e.target.value }))} style={{ flex: 1, minWidth: 160 }} />
              ) : <span className="dim" style={{ flex: 1 }}>no key required</span>}
              <Badge text={p.connected ? "Connected" : "Not connected"} tone={p.connected ? "green" : "default"} />
            </div>
          ))}
          <div className="row-actions" style={{ justifyContent: "flex-start", marginTop: 8 }}>
            <button className="btn btn-primary" onClick={saveKeys}><Icon name="check" size={13} /> Save Keys</button>
            <span className="dim" style={{ fontSize: 12, marginLeft: 8 }}>Missing keys show “Not connected” — never fake data.</span>
          </div>
        </div>
      )}
    </Card>
  );
}


type WorldNewsData = {
  available: boolean; note?: string; updated?: string;
  headlines: { title: string; url: string; source: string; published: string; markets: string[] }[];
  sources_ok?: string[]; sources_down?: string[];
  snapshot?: Record<string, { available: boolean; last?: number; change_pct?: number; note?: string }>;
};

const MARKET_TONES: Record<string, string> = { crypto: "var(--purple-2)", stocks: "#38bdf8", macro: "#f59e0b" };

function WorldNews() {
  const [filter, setFilter] = useState<string>("all");
  const news = useLive<WorldNewsData>("/news/world", 300000);
  const d = news.data;
  const rows = (d?.headlines ?? []).filter((h) => filter === "all" || h.markets.includes(filter));

  return (
    <div style={{ marginTop: 12 }}>
      <div className="card-subtitle" style={{ marginBottom: 6 }}>
        World &amp; Market News <span className="dim">(free RSS · crypto + stocks + macro)</span>
      </div>

      {/* real daily moves — the "impact" context, with numbers not narratives */}
      {d?.snapshot && (
        <div className="row-actions" style={{ justifyContent: "flex-start", gap: 10, flexWrap: "wrap", marginBottom: 8 }}>
          {Object.entries(d.snapshot).map(([label, q]) => (
            <span key={label} className="ui-badge" style={{ background: "var(--card-2)" }}>
              {label}:{" "}
              {q.available ? (
                <b className={(q.change_pct ?? 0) >= 0 ? "green" : "red"}>
                  {(q.change_pct ?? 0) >= 0 ? "+" : ""}{q.change_pct}%
                </b>
              ) : <span className="dim">n/a</span>}
            </span>
          ))}
        </div>
      )}

      <div className="row-actions" style={{ justifyContent: "flex-start", gap: 6, marginBottom: 8 }}>
        {["all", "crypto", "stocks", "macro"].map((m) => (
          <button key={m} className={`chip-btn ${filter === m ? "active" : ""}`} onClick={() => setFilter(m)}>
            {m}
          </button>
        ))}
      </div>

      {rows.length ? (
        <div className="alert-stack">
          {rows.slice(0, 12).map((h, i) => (
            <div key={i} className="exec-line">
              <span className="exec-time">{(h.published || "").slice(5, 16).replace("T", " ")}</span>{" "}
              {h.url ? <a href={h.url} target="_blank" rel="noreferrer" style={{ color: "#cfd6e4" }}>{h.title}</a> : h.title}
              {" "}
              {h.markets.map((m) => (
                <span key={m} className="ui-badge" style={{ fontSize: 10, marginLeft: 4, color: MARKET_TONES[m] ?? "inherit", border: `1px solid ${MARKET_TONES[m] ?? "#444"}` }}>{m}</span>
              ))}
              <span className="dim" style={{ fontSize: 11, marginLeft: 6 }}>{h.source}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="dim" style={{ fontSize: 13 }}>
          {d ? (d.note ?? "No headlines match this filter.") : "Loading news feeds…"}
        </div>
      )}
      {d?.sources_down && d.sources_down.length > 0 && (
        <p className="dim" style={{ fontSize: 11, marginTop: 6 }}>
          Feeds unreachable right now: {d.sources_down.join(", ")} — shown feeds: {d.sources_ok?.join(", ")}.
        </p>
      )}
    </div>
  );
}
