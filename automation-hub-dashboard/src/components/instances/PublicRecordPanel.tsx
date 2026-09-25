import { useState } from "react";
import { Badge } from "../common/ui";
import { apiPatchJson, useLive, type InstancePublicRecord } from "../../lib/api";
import { useApp } from "../../app-context";

// Per-instance public paper record. Off by default; when on, this instance's
// closed paper trades appear on the public site's Performance page as
// percentages of the paper account (never balances or amounts). The preview
// is the exact record the public sees.

function pct(value: number, signed = false): string {
  const text = `${value.toFixed(2)}%`;
  return signed && value > 0 ? `+${text}` : text;
}

export default function PublicRecordPanel({ instanceId }: { instanceId: string }) {
  const app = useApp();
  const record = useLive<InstancePublicRecord>(`/instances/${instanceId}/public-record`, 60_000);
  const [busy, setBusy] = useState(false);
  const r = record.data;

  async function toggle() {
    if (!r) return;
    if (!r.published && !window.confirm(
      "Publish this instance's paper record on the public site? Anyone will see its strategy, symbol, " +
      "trade count, win rate, return % and drawdown %, but no balances or amounts. You can unpublish at any time.")) return;
    setBusy(true);
    try {
      await apiPatchJson(`/instances/${instanceId}/public-record`, { published: !r.published });
      await record.refetch();
      app.toast(r.published ? "Paper record unpublished" : "Paper record published on the public site", "success");
    } catch (err) {
      app.toast(err instanceof Error ? err.message : "Could not change the public record", "error");
    } finally {
      setBusy(false);
    }
  }

  if (record.error && !r) return <p className="dim" style={{ marginTop: 10 }}>Public record unavailable: {record.error}</p>;
  if (!r) return null;
  const p = r.preview;

  return (
    <div className="public-record" style={{ marginTop: 12, padding: 12, border: "1px solid var(--card-border)", borderRadius: 10 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <b>Public paper record</b>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Badge text={r.published ? "Published" : "Private"} tone={r.published ? "gold" : "default"} />
          <button className={`btn btn-sm ${r.published ? "btn-soft" : "btn-primary"}`} disabled={busy || (!r.eligible && !r.published)}
            onClick={() => void toggle()} aria-pressed={r.published}>
            {busy ? "Saving…" : r.published ? "Unpublish" : "Publish"}
          </button>
        </span>
      </div>
      <p className="dim" style={{ margin: "6px 0 0", fontSize: 12.5 }}>
        {r.eligible
          ? "Shows this instance's closed paper trades on the public Performance page, as percentages only. No balances, amounts or position sizes."
          : "Research replays cannot be published; only forward paper instances can."}
        {r.published && r.since ? ` Published since ${new Date(r.since).toLocaleDateString()}.` : ""}
      </p>
      {p && <p style={{ margin: "8px 0 0", fontSize: 12.5 }}>
        <span className="dim">{r.published ? "The public sees: " : "Would show: "}</span>
        {p.closed_trades} closed trade{p.closed_trades === 1 ? "" : "s"} · win rate {p.win_rate_pct.toFixed(1)}% ·
        return {pct(p.return_pct, true)} · max drawdown {pct(p.max_drawdown_pct)}
        {p.session_number > 1 ? ` · session ${p.session_number}` : ""}
        {p.sample_note ? <span className="dim"> · {p.sample_note}</span> : null}
      </p>}
    </div>
  );
}
