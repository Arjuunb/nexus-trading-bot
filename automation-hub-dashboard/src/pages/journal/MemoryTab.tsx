import { useState } from "react";
import Card from "../../components/common/Card";
import { Badge } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import { dash, money, pct, rMult, when } from "../../lib/journal";
import Memory from "../Memory";
import EvidenceModal from "./Evidence";

/** Journal > Memory. Verified memory is computed from forward-paper trade
 *  records and opens to them. The old evolution counters are shown apart,
 *  labelled by how much of them can still be proven. */

type Verified = {
  setup_key: string; strategy: string; regime: string; side: string; trades: number; wins: number;
  losses: number; net_r: number | null; net_pnl: number | null; win_rate: number | null; stage: string;
  provenance: string; record_origin: string; record_sources: string[];
  period: { start: string | null; end: string | null }; evidence_count: number;
  journal_record_ids: string[]; last_reviewed: string | null; note: string;
};
type Legacy = {
  setup_key: string; strategy: string; regime: string; side: string; trades: number; wins: number;
  net_r: number; stage: string; updated_at: string; provenance: string; record_origin: string;
  evidence_rows: number; verified_records: number; unbacked_increments: number;
  period: { start: string | null; end: string | null }; journal_record_ids: string[];
  legacy_trade_ids: string[]; note: string;
};

const stageTone = (s: string) => (s === "evidence" ? "green" : s === "building" ? "amber" : "blue");
const provTone = (p: string) => (p === "VERIFIED" ? "green" : p === "LEGACY" ? "amber" : "red");

export default function JournalMemory() {
  const mem = useLive<{ verified: Verified[]; legacy: Legacy[]; explanation: string }>("/journal/memory", 20000);
  const [evidence, setEvidence] = useState<{ title: string; ids: string[] } | null>(null);
  const [legacyKey, setLegacyKey] = useState<string | null>(null);
  const legacyEvidence = useLive<{ records: { journal_record_id: string }[]; legacy_records?: { journal_record_id: string }[] }>(
    legacyKey ? `/journal/memory/evidence?setup_key=${encodeURIComponent(legacyKey)}` : null, 60000);
  const legacyIds = legacyKey && legacyEvidence.data
    ? [...(legacyEvidence.data.records ?? []), ...(legacyEvidence.data.legacy_records ?? [])]
      .filter(Boolean).map((r) => r.journal_record_id) : null;

  return (
    <>
      <Card title="Evolution Memory" subtitle="What each setup has done in forward paper — every figure opens to its trades">
        <p className="dim jr-note">{mem.data?.explanation}</p>
        <div className="tablewrap">
          <table className="data-table jr-table">
            <thead><tr><th>Setup</th><th>Regime</th><th>Side</th><th>Trades</th><th>Wins</th><th>Win rate</th>
              <th>Net R</th><th>Net P&amp;L</th><th>Source</th><th>Period</th><th>Last reviewed</th><th>Stage</th></tr></thead>
            <tbody>
              {(mem.data?.verified ?? []).map((m) => (
                <tr key={m.setup_key}>
                  <td><b>{m.strategy}</b></td>
                  <td className="dim">{m.regime}</td>
                  <td><Badge text={m.side} tone={m.side === "long" ? "green" : "red"} /></td>
                  <td>
                    <button type="button" className="jr-linkbtn" onClick={() => setEvidence({ title: `${m.strategy} · ${m.regime} · ${m.side}`, ids: m.journal_record_ids })}>
                      {m.trades} trades</button>
                  </td>
                  <td>{m.wins}</td>
                  <td>{pct(m.win_rate)}</td>
                  <td className={(m.net_r ?? 0) >= 0 ? "pos" : "neg"}>{rMult(m.net_r)}</td>
                  <td>{money(m.net_pnl)}</td>
                  <td><Badge text={m.record_origin.replace(/_/g, " ")} tone="blue" /> <Badge text={m.provenance} tone={provTone(m.provenance)} /></td>
                  <td className="dim mono">{m.period.start ? `${when(m.period.start).split(",")[0]} → ${when(m.period.end).split(",")[0]}` : dash}</td>
                  <td className="dim">{m.last_reviewed ? when(m.last_reviewed) : "Not yet"}</td>
                  <td><Badge text={m.stage} tone={stageTone(m.stage) as "green"} /></td>
                </tr>
              ))}
              {(mem.data?.verified ?? []).length === 0 && (
                <tr><td colSpan={12} className="dim ta-center" style={{ padding: 18 }}>
                  No verified memory yet. It builds from completed forward-paper trade records; under 30 trades a setup is an early signal only.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {(mem.data?.legacy ?? []).length > 0 && (
        <Card title="Legacy evolution counters" subtitle="From the old journal — counters without trade ids, labelled by what can still be proven">
          <div className="tablewrap">
            <table className="data-table jr-table">
              <thead><tr><th>Setup</th><th>Regime</th><th>Side</th><th>Trades</th><th>Wins</th><th>Net R</th>
                <th>Provenance</th><th>Backed by</th><th>Period</th><th>Stage</th></tr></thead>
              <tbody>
                {(mem.data?.legacy ?? []).map((m) => (
                  <tr key={m.setup_key}>
                    <td><b>{m.strategy}</b></td>
                    <td className="dim">{m.regime}</td>
                    <td><Badge text={m.side} tone={m.side === "long" ? "green" : "red"} /></td>
                    <td>
                      <button type="button" className="jr-linkbtn" onClick={() => setLegacyKey(m.setup_key)}
                        title="Open the records that still exist behind this counter">{m.trades} trades</button>
                    </td>
                    <td>{m.wins}</td>
                    <td className={m.net_r >= 0 ? "pos" : "neg"}>{rMult(m.net_r)}</td>
                    <td><Badge text={m.provenance} tone={provTone(m.provenance)} /> <Badge text="LEGACY" tone="amber" /></td>
                    <td className="dim">{m.evidence_rows} journal row(s) · {m.verified_records} ledger-verified
                      {m.unbacked_increments ? ` · ${m.unbacked_increments} with no record` : ""}</td>
                    <td className="dim mono">{m.period.start ? `${when(m.period.start).split(",")[0]} → ${when(m.period.end).split(",")[0]}` : "Unknown"}</td>
                    <td><Badge text={m.stage} tone={stageTone(m.stage) as "green"} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="dim jr-note">These counts are never added to forward-paper statistics. A counter whose increments have no
            surviving record is shown as UNVERIFIED rather than as trading history.</p>
        </Card>
      )}

      <p className="dim jr-note">
        The trade memory below is composed by the older journal path, so it covers trades that path recorded and does not
        yet include the forward-paper trade records above.
      </p>
      <Memory />

      <EvidenceModal title={evidence?.title ?? ""} ids={evidence?.ids ?? null} onClose={() => setEvidence(null)} />
      <EvidenceModal title={legacyKey ? `Legacy · ${legacyKey.replace(/\|/g, " · ")}` : ""}
        ids={legacyKey ? (legacyIds ?? []) : null} onClose={() => setLegacyKey(null)} />
    </>
  );
}
