import { useState } from "react";
import { apiPost, useLive } from "../../lib/api";
import SettingsSection from "./SettingsSection";

interface Snapshot { snapshot: string; encrypted: boolean; bytes: number; files: number }
interface BackupStatus {
  encrypting: boolean; count: number; keep: number; latest: Snapshot | null;
  unencrypted_kept: number; problem: string;
}
interface RestoreCheck { ok: boolean; error?: string; databases?: Record<string, { ok: boolean }> }

function stampLabel(stamp: string) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp);
  if (!m) return stamp;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function size(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Nightly snapshots of every database and settings file, sealed with the
 *  master key (services/backup.py). */
export default function BackupsPanel() {
  const status = useLive<{ backups?: BackupStatus }>("/security/status", 20000);
  const [busy, setBusy] = useState<"" | "backup" | "verify">("");
  const [message, setMessage] = useState<{ text: string; tone: string } | null>(null);
  const b = status.data?.backups;
  const latest = b?.latest ?? null;

  const backupNow = async () => {
    setBusy("backup"); setMessage(null);
    try {
      const r = await apiPost<{ ok: boolean; snapshot?: string; encrypted?: boolean; warning?: string; errors?: string[] }>("/ops/backup");
      setMessage(r.ok
        ? { text: `Snapshot ${r.snapshot ?? ""} saved${r.encrypted ? ", encrypted" : ""}.${r.warning ? ` ${r.warning}` : ""}`, tone: r.encrypted ? "pos" : "" }
        : { text: `Backup incomplete: ${(r.errors ?? []).join("; ") || "see the server log"}`, tone: "neg" });
      await status.refetch();
    } catch (e) { setMessage({ text: e instanceof Error ? e.message : "Backup failed.", tone: "neg" }); }
    finally { setBusy(""); }
  };

  const verify = async () => {
    if (!latest) return;
    setBusy("verify"); setMessage(null);
    try {
      const r = await apiPost<RestoreCheck>(`/security/backups/${latest.snapshot}/verify`);
      const dbs = Object.keys(r.databases ?? {}).length;
      setMessage(r.ok ? { text: `Restores cleanly: decrypted, and all ${dbs} databases open.`, tone: "pos" }
        : { text: `Does not restore: ${r.error ?? "a database failed to open"}`, tone: "neg" });
    } catch (e) { setMessage({ text: e instanceof Error ? e.message : "Verification failed.", tone: "neg" }); }
    finally { setBusy(""); }
  };

  return <SettingsSection title="Backups" description="A consistent snapshot of every database and settings file, taken nightly and kept for a week.">
    <div className="risk-list">
      <div className="risk-item"><span>Encryption</span><b className={b ? (b.encrypting ? "pos" : "neg") : ""}>
        {b ? (b.encrypting ? "On · AES-256-GCM, sealed with the master key" : "Off · HUB_MASTER_KEY is not set") : "—"}
      </b></div>
      <div className="risk-item"><span>Latest</span><b className={latest && !latest.encrypted ? "neg" : ""}>
        {latest ? `${stampLabel(latest.snapshot)} · ${size(latest.bytes)} · ${latest.files} files · ${latest.encrypted ? "encrypted" : "NOT encrypted"}` : b ? "None yet" : "—"}
      </b></div>
      <div className="risk-item"><span>Kept</span><b>
        {b ? `${b.count} of ${b.keep}${b.unencrypted_kept ? ` · ${b.unencrypted_kept} from before encryption` : ""}` : "—"}
      </b></div>
    </div>
    <div className="row-actions" style={{ justifyContent: "flex-start" }}>
      <button className="btn btn-primary" disabled={busy !== ""} onClick={() => void backupNow()}>{busy === "backup" ? "Backing up…" : "Back up now"}</button>
      <button className="btn btn-ghost" disabled={busy !== "" || !latest} onClick={() => void verify()}>{busy === "verify" ? "Checking…" : "Check the latest restores"}</button>
    </div>
    {message && <p className={message.tone || "dim"}>{message.text}</p>}
    <p className="dim">Restoring is deliberate: <code>python -m services.backup restore &lt;snapshot&gt; &lt;folder&gt;</code> writes a snapshot into a folder you choose and never overwrites live data. Keep the master key somewhere safe; without it an encrypted backup cannot be opened.</p>
  </SettingsSection>;
}
