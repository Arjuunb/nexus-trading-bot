import { useState } from "react";
import { ENTRY_TIMEFRAMES } from "../../lib/timeframes";
import Card from "../common/Card";
import Icon from "../common/Icon";
import { Badge } from "../common/ui";
import { apiPostJson, type CustomSpec } from "../../lib/api";

/**
 * Multi-asset / multi-timeframe sweep — answers "best asset" and "best
 * timeframe", which a single backtest cannot.
 *
 * Every cell is a real backtest. Cells whose data did not come back at the
 * requested candle size are excluded from the TIMEFRAME ranking and the reason
 * is shown, because ranking timeframes on a source that returns one fixed
 * granularity would be comparing a series against itself.
 */

interface Cell {
  symbol: string; timeframe: string; trades: number | null;
  net_r: number | null; net_pct: number | null; win_rate: number | null;
  profit_factor: number | null; max_drawdown_r: number | null;
  thin: boolean; tf_verified: boolean;
}
interface SweepResult {
  available: boolean; note?: string;
  cells: Cell[];
  skipped: { symbol: string; timeframe: string; why: string }[];
  truncated: boolean; max_cells: number;
  best_asset: { symbol: string; net_r: number; cells: number; trades: number } | null;
  worst_asset: { symbol: string; net_r: number } | null;
  best_timeframe: { timeframe: string; net_r: number; cells: number } | null;
  worst_timeframe: { timeframe: string; net_r: number } | null;
  timeframe_comparable: boolean;
  caveats: string[];
}

const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "BNBUSDT"];
const TFS = ENTRY_TIMEFRAMES;
const n = (v: unknown, dp = 2) => (typeof v === "number" ? v.toFixed(dp) : "—");

export default function StrategySweep({ spec, range, toast }: {
  spec: CustomSpec; range: string; toast: (m: string, t?: "success" | "error" | "info") => void;
}) {
  const [symbols, setSymbols] = useState<string[]>([spec.symbol, "ETHUSDT", "SOLUSDT"]
    .filter((v, i, a) => a.indexOf(v) === i));
  const [tfs, setTfs] = useState<string[]>([spec.timeframe]);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<SweepResult | null>(null);

  const toggle = (list: string[], set: (v: string[]) => void, v: string) =>
    set(list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

  const cells = symbols.length * tfs.length;

  const run = async () => {
    if (!symbols.length || !tfs.length) return;
    setBusy(true);
    try {
      setRes(await apiPostJson<SweepResult>("/strategy/sweep",
        { spec, symbols, timeframes: tfs, range }));
    } catch { toast("Sweep failed", "error"); }
    finally { setBusy(false); }
  };

  return (
    <Card title="Asset & Timeframe Sweep"
          subtitle="Runs the same strategy across markets — the only way to answer 'best asset'"
          right={res?.available ? <Badge text={`${res.cells.length} tested`} tone="green" /> : null}>
      <div className="dim" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .6, marginBottom: 4 }}>Assets</div>
      <div className="chips" style={{ gap: 4, flexWrap: "wrap" }}>
        {SYMBOLS.map((s) => (
          <button key={s} className={`chip-btn ${symbols.includes(s) ? "active" : ""}`}
                  onClick={() => toggle(symbols, setSymbols, s)}>{s.replace("USDT", "")}</button>
        ))}
      </div>
      <div className="dim" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .6, margin: "10px 0 4px" }}>Timeframes</div>
      <div className="chips" style={{ gap: 4 }}>
        {TFS.map((t) => (
          <button key={t} className={`chip-btn ${tfs.includes(t) ? "active" : ""}`}
                  onClick={() => toggle(tfs, setTfs, t)}>{t}</button>
        ))}
      </div>

      <div className="toolbar" style={{ marginTop: 10, gap: 8 }}>
        <button className="btn btn-primary btn-sm" disabled={busy || !cells} onClick={run}>
          <Icon name="history" size={13} /> {busy ? `Running ${cells} backtests…` : `Run sweep (${cells} backtests)`}
        </button>
        <span className="dim" style={{ fontSize: 11 }}>
          {cells > 12 ? "Larger sweeps take longer — each cell is a real backtest." : `Window: ${range}`}
        </span>
      </div>

      {res && !res.available && (
        <div className="dim" style={{ marginTop: 10, fontSize: 12.5 }}>{res.note}</div>
      )}

      {res?.available && (
        <>
          <div className="grid-2-eq" style={{ marginTop: 12 }}>
            <div>
              <div className="stat-row"><span className="dim">Best asset</span>
                <b className="pos">{res.best_asset
                  ? `${res.best_asset.symbol} (+${res.best_asset.net_r}R)` : "—"}</b></div>
              <div className="stat-row"><span className="dim">Worst asset</span>
                <b className={(res.worst_asset?.net_r ?? 0) < 0 ? "neg" : ""}>{res.worst_asset
                  ? `${res.worst_asset.symbol} (${res.worst_asset.net_r}R)` : "—"}</b></div>
            </div>
            <div>
              <div className="stat-row"><span className="dim">Best timeframe</span>
                <b className="pos">{res.best_timeframe
                  ? `${res.best_timeframe.timeframe} (+${res.best_timeframe.net_r}R)`
                  : <span className="dim">not comparable</span>}</b></div>
              <div className="stat-row"><span className="dim">Worst timeframe</span>
                <b>{res.worst_timeframe
                  ? `${res.worst_timeframe.timeframe} (${res.worst_timeframe.net_r}R)` : "—"}</b></div>
            </div>
          </div>

          <table className="data-table" style={{ marginTop: 10 }}>
            <thead><tr>
              <th>Asset</th><th>TF</th><th>Trades</th><th>Net R</th>
              <th>PF</th><th>Win</th><th>Max DD</th>
            </tr></thead>
            <tbody>
              {res.cells.map((c, i) => (
                <tr key={i}>
                  <td>{c.symbol}</td>
                  <td className="mono">
                    {c.timeframe}
                    {!c.tf_verified && (
                      <span className="dim" title="This source returned a fixed candle size, so this timeframe is not distinct — excluded from the timeframe ranking."> *</span>
                    )}
                  </td>
                  <td className="mono dim">{c.trades}{c.thin && <span className="neg" title="Too few trades to rank"> thin</span>}</td>
                  <td className={`mono ${(c.net_r ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {(c.net_r ?? 0) > 0 ? "+" : ""}{n(c.net_r)}</td>
                  <td className="mono">{n(c.profit_factor)}</td>
                  <td className="mono dim">{n(c.win_rate, 1)}%</td>
                  <td className="mono neg">{n(c.max_drawdown_r)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* limits of this sweep — stated, never hidden */}
          {res.caveats.map((c, i) => (
            <div key={i} className="dim" style={{ fontSize: 11.5, marginTop: 8, padding: 7,
              borderRadius: 7, border: "1px solid rgba(234,181,79,.25)", background: "rgba(234,181,79,.05)" }}>
              <Icon name="alert" size={11} /> {c}
            </div>
          ))}
          {res.truncated && (
            <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>
              Capped at {res.max_cells} combinations — narrow the selection to test the rest.
            </div>
          )}
          {!!res.skipped.length && (
            <div style={{ marginTop: 8 }}>
              <div className="dim" style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .6 }}>Skipped</div>
              {res.skipped.map((s, i) => (
                <div key={i} className="dim" style={{ fontSize: 11.5 }}>
                  {s.symbol} {s.timeframe} — {s.why}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}
