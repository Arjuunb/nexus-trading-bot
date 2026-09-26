import Modal from "../../components/common/Modal";
import { Badge } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import { dash, money, originTone, outcomeTone, type RecordRow, rMult, when } from "../../lib/journal";

/** The exact trade records behind a claim. Every figure a review or the
 *  memory shows can be opened to this list. */
export default function EvidenceModal({ title, ids, onClose }: {
  title: string; ids: string[] | null; onClose: () => void;
}) {
  const path = ids && ids.length ? `/journal/records?origin=all&limit=500&ids=${ids.slice(0, 500).join(",")}` : null;
  const data = useLive<{ records: RecordRow[] }>(path, 60000);
  return (
    <Modal open={ids !== null} title={title} onClose={onClose}>
      <p className="dim" style={{ marginTop: 0 }}>
        {ids?.length ?? 0} record(s). Open one to see its full trade record.
      </p>
      <div className="tablewrap">
        <table className="data-table jr-table">
          <thead><tr><th>Closed</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Origin</th><th>R</th><th>Net</th><th>Result</th></tr></thead>
          <tbody>
            {(data.data?.records ?? []).map((r) => (
              <tr key={r.journal_record_id}>
                <td className="mono dim"><a href={`#/trade/${r.journal_record_id}`} onClick={onClose}>{when(r.position_closed_at ?? r.position_opened_at)}</a></td>
                <td>{r.strategy_name ?? dash}</td>
                <td><b>{r.symbol ?? dash}</b></td>
                <td>{r.side ?? dash}</td>
                <td><Badge text={r.record_origin.replace(/_/g, " ")} tone={originTone(r.record_origin)} /></td>
                <td className="mono">{rMult(r.realized_r)}</td>
                <td className="mono">{money(r.net_pnl)}</td>
                <td>{r.outcome ? <Badge text={r.outcome} tone={outcomeTone(r.outcome)} /> : dash}</td>
              </tr>
            ))}
            {ids && ids.length > 0 && !data.data && (
              <tr><td colSpan={8} className="dim ta-center">{data.error ? `Could not load: ${data.error}` : "Loading…"}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Modal>
  );
}
