import { AlertTriangle, CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { useLive } from "../../lib/api";
import SettingsSection from "./SettingsSection";

type Status = "pass" | "warn" | "fail";
interface Check { id: string; title: string; status: Status; detail: string; fix: string }
interface Checkup { checked_at: string; counts: Record<Status, number>; total: number; checks: Check[] }

const ICON = { pass: CheckCircle2, warn: AlertTriangle, fail: XCircle } as const;
const ORDER: Record<Status, number> = { fail: 0, warn: 1, pass: 2 };

/** What is protecting this deployment right now (services/security_checkup.py).
 *  Every line reads real state; nothing here is a score of how secure it "is". */
export default function SecurityCheckupPanel() {
  const q = useLive<Checkup>("/security/checkup", 60000);
  // Anything that is not a well-formed checkup is treated as "could not run".
  const c = q.data && Array.isArray(q.data.checks) && q.data.counts ? q.data : null;
  const checks = c ? c.checks.filter((x) => x && x.status in ICON)
    .sort((a, b) => ORDER[a.status] - ORDER[b.status]) : [];

  const summary = !c ? (q.error || q.data ? "The checkup could not run." : "Checking…")
    : c.counts.fail ? `${c.counts.fail} to fix · ${c.counts.warn ?? 0} to review · ${c.counts.pass ?? 0} of ${c.total} in place`
    : c.counts.warn ? `${c.counts.warn} to review · ${c.counts.pass ?? 0} of ${c.total} in place`
    : `All ${c.total} protections in place`;

  return <SettingsSection title="Security checkup"
    description="Which protections are switched on, and the one thing to do about each that is not.">
    <div className="checkup-head">
      <div>
        <b className={c ? (c.counts.fail ? "neg" : c.counts.warn ? "amber" : "pos") : ""}>{summary}</b>
        {c && <div className="checkup-bar" aria-hidden>
          {(["pass", "warn", "fail"] as Status[]).map((s) => (c.counts[s] ?? 0) > 0 &&
            <span key={s} className={`checkup-seg ${s}`} style={{ flexGrow: c.counts[s] }} />)}
        </div>}
      </div>
      <button className="btn btn-sm btn-ghost" onClick={() => void q.refetch()} aria-label="Run the checkup again">
        <RefreshCw size={13} /> Run again
      </button>
    </div>

    <ul className="checkup-list">
      {checks.map((check) => {
        const Icon = ICON[check.status];
        return <li key={check.id} className={`checkup-item ${check.status}`}>
          <Icon size={16} className="checkup-icon" aria-label={check.status} />
          <div>
            <div className="checkup-title">{check.title}</div>
            <div className="checkup-detail">{check.detail}</div>
            {check.fix && <div className="checkup-fix">{check.fix}</div>}
          </div>
        </li>;
      })}
    </ul>
  </SettingsSection>;
}
