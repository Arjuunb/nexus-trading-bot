import { Fragment, useState } from "react";
import { apiDownload, apiGet, useLive } from "../../lib/api";
import SettingsSection from "./SettingsSection";

interface AuditEntry {
  seq: number; ts: string; kind: string; actor: string; auth: string; ip: string;
  method: string; path: string; status: number; action: string; detail: string; hash: string;
}
interface AuditPage { entries: AuditEntry[]; head: { seq: number; hash: string; ts: string | null } }
interface Verify { ok: boolean; entries: number; first_bad_seq: number | null; reason: string; head_hash: string }

const AUTH_LABEL: Record<string, string> = {
  session: "session", control_key: "control key", webhook: "webhook", password: "password", none: "none",
};

function when(ts: string) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

/** The append-only, hash-chained audit log (GET /security/audit*). */
export default function AuditLogPanel() {
  const [changesOnly, setChangesOnly] = useState(false);
  const page = useLive<AuditPage>(`/security/audit?limit=50${changesOnly ? "&kind=change" : ""}`, 15000);
  const [verify, setVerify] = useState<Verify | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const runVerify = async () => {
    setBusy(true); setError("");
    try { setVerify(await apiGet<Verify>("/security/audit/verify")); }
    catch (e) { setError(e instanceof Error ? e.message : "Verification failed."); }
    finally { setBusy(false); }
  };
  const exportLog = async () => {
    setError("");
    try { await apiDownload("/security/audit/export", `audit-${page.data?.head.seq ?? 0}.jsonl`); }
    catch (e) { setError(e instanceof Error ? e.message : "Export failed."); }
  };

  const head = page.data?.head;
  const entries = page.data?.entries ?? [];
  return <SettingsSection title="Audit log" description="Every state-changing request and every recorded change, append-only and chained with SHA-256. Secrets are removed before an entry is written.">
    <div className="risk-list">
      <div className="risk-item"><span>Entries</span><b>{typeof head?.seq === "number" ? head.seq.toLocaleString() : "—"}</b></div>
      <div className="risk-item"><span>Head hash</span><b className="audit-hash" title={head?.hash}>{head?.hash ? `${head.hash.slice(0, 16)}…` : "—"}</b></div>
      <div className="risk-item"><span>Chain</span><b className={verify ? (verify.ok ? "pos" : "neg") : "dim"}>
        {verify ? (verify.ok ? `Intact · ${verify.entries.toLocaleString()} entries checked`
          : `Broken at #${verify.first_bad_seq} · ${verify.reason}`) : "Not checked yet"}
      </b></div>
    </div>
    <div className="row-actions" style={{ justifyContent: "flex-start" }}>
      <button className="btn btn-primary" disabled={busy} onClick={() => void runVerify()}>{busy ? "Checking…" : "Verify chain"}</button>
      <button className="btn btn-ghost" onClick={() => void exportLog()}>Export JSONL</button>
      <label className="audit-filter"><input type="checkbox" checked={changesOnly} onChange={(e) => setChangesOnly(e.target.checked)} /> Changes only</label>
    </div>
    {(error || page.error) && <p className="neg">{error || page.error}</p>}
    <div className="audit-table-wrap">
      <table className="data-table audit-table">
        <thead><tr><th>#</th><th>Time</th><th>Actor</th><th>Auth</th><th>Action</th><th>Status</th><th>Source</th></tr></thead>
        <tbody>
          {entries.map((e) => <Fragment key={e.seq}>
            <tr className={open === e.seq ? "active-row" : ""} onClick={() => setOpen(open === e.seq ? null : e.seq)} style={{ cursor: "pointer" }}>
              <td className="dim">{e.seq}</td>
              <td>{when(e.ts)}</td>
              <td>{e.actor}</td>
              <td className="dim">{AUTH_LABEL[e.auth] ?? e.auth}</td>
              <td>{e.kind === "change" ? <span className="lab-badge ok">{e.action}</span> : e.action || `${e.method} ${e.path}`}</td>
              <td className={e.status >= 400 ? "neg" : e.status ? "pos" : "dim"}>{e.status || "—"}</td>
              <td className="dim">{e.ip || "—"}</td>
            </tr>
            {open === e.seq && <tr><td colSpan={7}><pre className="audit-detail">{pretty(e.detail)}{"\n"}hash {e.hash}</pre></td></tr>}
          </Fragment>)}
          {page.data && entries.length === 0 && <tr><td colSpan={7} className="dim">Nothing recorded yet.</td></tr>}
        </tbody>
      </table>
    </div>
    <p className="dim">Reads are not recorded. Refused attempts are. Export a copy regularly: an external copy of the head hash is what shows the newest entries were never cut off.</p>
  </SettingsSection>;
}

function pretty(detail: string) {
  if (!detail) return "(no detail)";
  try { return JSON.stringify(JSON.parse(detail), null, 2); } catch { return detail; }
}
