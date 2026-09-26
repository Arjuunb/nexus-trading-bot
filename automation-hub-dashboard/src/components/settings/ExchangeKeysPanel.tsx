import { useState } from "react";
import { apiDelete, apiPost, apiPostJson, useLive } from "../../lib/api";
import { Field } from "../common/ui";
import SettingsSection, { type SaveState } from "./SettingsSection";

interface Scope {
  allowed: boolean; refusals: string[]; warnings: string[]; read_only: boolean;
  can_trade: string[]; ip_restricted: boolean; checked_at: number;
}
interface KeyMeta {
  id: string; venue: string; label: string; key_hint: string; scope: Scope;
  status: "active" | "rotated" | "revoked"; created_at: string; retired_at: string | null;
}
interface VaultStatus {
  configured: boolean; encryption: string; master_key_id: string | null; active_keys: number;
  problem: string; scope_check_venues: string[];
}
interface SecurityStatus {
  vault: VaultStatus; keys: KeyMeta[]; live_routing_locked: boolean;
  redaction: { active: boolean; live_secrets_guarded: number };
}

const EMPTY = { label: "", api_key: "", api_secret: "" };

/** Exchange-key custody (/security/keys) plus the redaction status. Keys are
 *  sent once, scope-checked by the exchange, encrypted, and never shown again. */
export default function ExchangeKeysPanel() {
  const status = useLive<SecurityStatus>("/security/status", 20000);
  const [form, setForm] = useState(EMPTY);
  const [state, setState] = useState<SaveState>("saved");
  const [message, setMessage] = useState("");

  const edit = (patch: Partial<typeof EMPTY>) => { setForm({ ...form, ...patch }); setState("dirty"); setMessage(""); };
  const fail = (e: unknown) => { setMessage(e instanceof Error ? e.message.replace(/^[A-Z]+ \S+: HTTP \d+ · /, "") : "Request failed."); setState("error"); };

  const attach = async () => {
    if (form.api_key.trim().length < 8 || form.api_secret.trim().length < 8) {
      setMessage("Paste both the API key and the API secret."); setState("error"); return;
    }
    setState("saving");
    try {
      await apiPostJson("/security/keys", { venue: "binance", ...form });
      setForm(EMPTY); setState("saved");
      setMessage("Key checked with Binance, encrypted and stored. It will not be shown again.");
      await status.refetch();
    } catch (e) { fail(e); }
  };
  const recheck = async (id: string) => {
    try { await apiPost(`/security/keys/${id}/check`); setMessage("Permissions re-checked with Binance."); await status.refetch(); }
    catch (e) { fail(e); }
  };
  const revoke = async (id: string) => {
    try { await apiDelete(`/security/keys/${id}`); setMessage("Key revoked. It can no longer be used."); await status.refetch(); }
    catch (e) { fail(e); }
  };

  const vault = status.data?.vault;
  const keys = status.data?.keys ?? [];
  return <SettingsSection title="Exchange keys" description="Encrypted at rest with a per-account data key, checked with the exchange before it is kept, and never returned once stored." state={state}>
    <div className="risk-list">
      <div className="risk-item"><span>Encryption</span><b>{vault ? (vault.configured ? `${vault.encryption}` : "Not configured") : "—"}</b></div>
      <div className="risk-item"><span>Master key</span><b className={vault?.configured ? "" : "neg"}>{vault ? (vault.configured ? `id ${vault.master_key_id}` : "HUB_MASTER_KEY not set") : "—"}</b></div>
      <div className="risk-item"><span>Live order routing</span><b>{status.data?.live_routing_locked ? "Locked · keys are stored for when it is enabled" : "—"}</b></div>
      <div className="risk-item"><span>Secret redaction</span><b className={status.data?.redaction ? "pos" : ""}>{status.data?.redaction ? `On · ${status.data.redaction.live_secrets_guarded} live secrets guarded in every response and log` : "—"}</b></div>
    </div>
    {vault && !vault.configured && <p className="neg keys-problem">{vault.problem}</p>}

    {keys.length > 0 && <div className="audit-table-wrap"><table className="data-table">
      <thead><tr><th>Venue</th><th>Label</th><th>Key</th><th>Permissions</th><th>Status</th><th>Added</th><th /></tr></thead>
      <tbody>{keys.map((k) => <tr key={k.id}>
        <td>{k.venue}</td><td>{k.label}</td><td className="dim">{k.key_hint}</td>
        <td>
          <span className={`lab-badge ${k.scope.allowed ? "ok" : ""}`}>{k.scope.allowed ? "no withdrawals" : "refused"}</span>{" "}
          <span className="lab-badge">{k.scope.read_only ? "read-only" : "can trade"}</span>{" "}
          <span className={`lab-badge ${k.scope.ip_restricted ? "ok" : "paper"}`}>{k.scope.ip_restricted ? "IP-bound" : "not IP-bound"}</span>
        </td>
        <td className={k.status === "active" ? "pos" : "dim"}>{k.status}</td>
        <td className="dim">{new Date(k.created_at).toLocaleDateString()}</td>
        <td>{k.status === "active" && <span className="row-actions">
          <button className="btn btn-sm btn-ghost" onClick={() => void recheck(k.id)}>Re-check</button>
          <button className="btn btn-sm btn-danger" onClick={() => void revoke(k.id)}>Revoke</button>
        </span>}</td>
      </tr>)}</tbody>
    </table></div>}

    {vault?.configured && <>
      <div className="form-grid-2">
        <Field label="Exchange"><input value="Binance" disabled /></Field>
        <Field label="Label"><input value={form.label} placeholder="Main account" onChange={(e) => edit({ label: e.target.value })} /></Field>
        <Field label="API key"><input type="password" autoComplete="off" value={form.api_key} onChange={(e) => edit({ api_key: e.target.value })} /></Field>
        <Field label="API secret"><input type="password" autoComplete="off" value={form.api_secret} onChange={(e) => edit({ api_secret: e.target.value })} /></Field>
      </div>
      <div className="row-actions" style={{ justifyContent: "flex-start" }}>
        <button className="btn btn-primary" disabled={state === "saving"} onClick={() => void attach()}>{state === "saving" ? "Checking with Binance…" : keys.some((k) => k.status === "active") ? "Replace key" : "Attach key"}</button>
      </div>
    </>}
    {message && <p className={state === "error" ? "neg" : "dim"}>{message}</p>}
    <p className="dim">A key that can withdraw or transfer funds is refused before it is stored, and so is a key whose permissions Binance cannot confirm. Restrict the key to this server's IP address on Binance for a leaked key to be useless anywhere else.</p>
  </SettingsSection>;
}
