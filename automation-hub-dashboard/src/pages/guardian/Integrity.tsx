import Card from "../../components/common/Card";
import { Badge, StatCard } from "../../components/common/ui";
import { useLive } from "../../lib/api";
import { clock, type IntegrityReport, SEVERITY_TONE } from "./common";

const money = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: 2 }));

/** Every stage of a trade reconciled, and open exposure across every account
 *  -- paper and live never added together (PRD §23-25). Read-only. */
export default function Integrity() {
  const live = useLive<IntegrityReport>("/guardian/integrity", 15000);
  const r = live.data;
  if (live.error && !r) return <p className="neg">Integrity is unavailable: {live.error}</p>;
  if (!r) return <p className="dim">Loading…</p>;
  const paper = r.exposure.paper;
  const liveSide = r.exposure.live;
  return (
    <>
      <div className="stat-row">
        <StatCard label="Integrity findings" value={String(r.findings.length)} tone={r.findings.length ? "red" : "green"}
          sub={`checked ${clock(r.at)}`} />
        <StatCard label="Paper open risk" value={money(paper.risk)} sub={`${paper.positions} open positions · USDT`} />
        <StatCard label="Unknown risk" value={String(paper.risk_unknown)} tone={paper.risk_unknown ? "amber" : "default"}
          sub="positions with no recorded stop" />
        <StatCard label="Live positions" value={String(liveSide.positions)} tone={liveSide.positions ? "red" : "default"}
          sub={liveSide.routing_locked ? "live routing locked" : "live routing state unknown"} />
      </div>

      <Card title="Execution and journal integrity" subtitle="Intent → fill → position → journal. A finding is a record with no counterpart at the next stage.">
        {Object.entries(r.errors).map(([name, error]) => <p key={name} className="neg">Could not read {name}: {error}</p>)}
        {!r.journal_checked && <p className="dim">The journal could not be read, so journal completeness was not judged.</p>}
        {r.findings.length ? (
          <ul className="gd-list" data-testid="guardian-integrity-findings">
            {r.findings.map((f, i) => (
              <li key={`${f.rule}-${f.item}-${i}`}>
                <Badge text={f.severity} tone={SEVERITY_TONE[f.severity] ?? "default"} />
                <span className="mono dim">{f.source}</span>
                <span><b>{f.meaning}</b> — {f.detail}</span>
              </li>
            ))}
          </ul>
        ) : <p className="dim" data-testid="guardian-integrity-clean">Every stage reconciles across {Object.keys(r.sources).length} account(s).</p>}
      </Card>

      <div className="grid-2-eq">
        <Card title="Paper exposure" subtitle="Across every instance and lab, from the positions themselves">
          {paper.by_symbol.length ? (
            <div className="tablewrap">
              <table className="data-table" data-testid="guardian-exposure">
                <thead><tr><th>Symbol</th><th>Side</th><th>Positions</th><th>Notional</th><th>Risk to stop</th><th>Accounts</th></tr></thead>
                <tbody>
                  {paper.by_symbol.map((s) => (
                    <tr key={`${s.symbol}-${s.side}`}>
                      <td className="mono">{s.symbol}</td><td>{s.side}</td><td className="mono">{s.positions}</td>
                      <td className="mono">{money(s.notional)}</td>
                      <td className="mono">{money(s.risk)}{s.risk_unknown ? ` + ${s.risk_unknown} unknown` : ""}</td>
                      <td className="mono dim">{s.accounts.join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="dim">No open paper positions.</p>}
          {Object.entries(paper.by_cluster).map(([name, c]) => (
            <p key={name} className="dim gd-cluster">Correlated cluster <b>{name}</b>: long {money(c.long)} · short {money(c.short)} · net {money(c.net)} ({c.positions} positions — one bet, not {c.positions})</p>
          ))}
        </Card>
        <Card title="Live exposure" subtitle="Kept apart from paper, always">
          <p data-testid="guardian-live-exposure">
            <Badge text={liveSide.routing_locked ? "LIVE ROUTING LOCKED" : "LIVE STATE UNKNOWN"} tone={liveSide.routing_locked ? "blue" : "amber"} />{" "}
            {liveSide.positions ? `${liveSide.positions} non-paper position(s) — see findings.` : "No live positions."}
          </p>
          <p className="dim">{r.exposure.note}</p>
        </Card>
      </div>
    </>
  );
}
