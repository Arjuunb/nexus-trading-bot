import { useState } from "react";
import { API_BASE, apiDelete, apiPostJson, useLive } from "../../lib/api";
import { Field } from "../common/ui";
import SettingsSection, { type SaveState } from "./SettingsSection";

interface ApiKey {
  id: string; name: string; scopes: string[]; version: string; hint: string;
  created_at: string; last_used_at: string | null; revoked_at: string | null; active: boolean;
}
interface Created extends ApiKey { token: string }

function day(iso: string | null) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "never";
}

/** Personal API keys for the public /v1 API (routers/public_api.py). */
export default function ApiKeysPanel() {
  const keys = useLive<{ keys: ApiKey[] }>("/security/api-keys", 30000);
  const [name, setName] = useState("");
  const [control, setControl] = useState(false);
  const [fresh, setFresh] = useState<Created | null>(null);
  const [copied, setCopied] = useState(false);
  const [state, setState] = useState<SaveState>("saved");
  const [message, setMessage] = useState("");

  const create = async () => {
    if (!name.trim()) { setMessage("Give the key a name you will recognise later."); setState("error"); return; }
    setState("saving"); setMessage(""); setCopied(false);
    try {
      const created = await apiPostJson<Created>("/security/api-keys", { name: name.trim(), scopes: control ? ["read", "control"] : ["read"] });
      setFresh(created); setName(""); setControl(false); setState("saved");
      await keys.refetch();
    } catch (e) { setMessage(e instanceof Error ? e.message.replace(/^[A-Z]+ \S+: HTTP \d+ · /, "") : "Could not create the key."); setState("error"); }
  };
  const revoke = async (id: string) => {
    try { await apiDelete(`/security/api-keys/${id}`); if (fresh?.id === id) setFresh(null); await keys.refetch(); }
    catch (e) { setMessage(e instanceof Error ? e.message : "Could not revoke the key."); setState("error"); }
  };
  const copy = async () => {
    if (!fresh) return;
    try { await navigator.clipboard.writeText(fresh.token); setCopied(true); } catch { setCopied(false); }
  };

  const list = keys.data?.keys ?? [];
  const base = API_BASE && API_BASE !== "http://localhost:8000" ? API_BASE : window.location.origin;
  return <SettingsSection title="API keys" description="Keys for the public /v1 API. A key is shown once when it is created; only its hash is kept." state={state}>
    {fresh && <div className="api-key-fresh" role="status">
      <p><b>Copy this key now.</b> It will not be shown again — if it is lost, revoke it and create another.</p>
      <div className="api-key-token"><code>{fresh.token}</code><button className="btn btn-sm btn-primary" onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button></div>
      <p className="dim">Try it: <code>curl {base}/v1/strategies -H "Authorization: Bearer $NEXUS_API_KEY"</code></p>
      <button className="btn btn-sm btn-ghost" onClick={() => setFresh(null)}>I have stored it</button>
    </div>}

    {list.length > 0 && <div className="audit-table-wrap"><table className="data-table">
      <thead><tr><th>Name</th><th>Key</th><th>Scopes</th><th>Created</th><th>Last used</th><th>Status</th><th /></tr></thead>
      <tbody>{list.map((k) => <tr key={k.id}>
        <td>{k.name}</td><td className="dim">{k.hint}</td>
        <td>{k.scopes.map((s) => <span key={s} className={`lab-badge ${s === "control" ? "paper" : ""}`} style={{ marginRight: 4 }}>{s}</span>)}</td>
        <td className="dim">{day(k.created_at)}</td><td className="dim">{day(k.last_used_at)}</td>
        <td className={k.active ? "pos" : "dim"}>{k.active ? "active" : "revoked"}</td>
        <td>{k.active && <button className="btn btn-sm btn-danger" onClick={() => void revoke(k.id)}>Revoke</button>}</td>
      </tr>)}</tbody>
    </table></div>}

    <div className="form-grid-2">
      <Field label="Name"><input value={name} placeholder="e.g. research notebook" onChange={(e) => { setName(e.target.value); setState("dirty"); setMessage(""); }} /></Field>
      <Field label="Scope" hint="Control can close paper positions and queue backtests. No key can trade live: live routing is locked.">
        <label className="audit-filter" style={{ marginLeft: 0 }}><input type="checkbox" checked={control} onChange={(e) => setControl(e.target.checked)} /> Allow control (read is always included)</label>
      </Field>
    </div>
    <div className="row-actions" style={{ justifyContent: "flex-start" }}>
      <button className="btn btn-primary" disabled={state === "saving"} onClick={() => void create()}>{state === "saving" ? "Creating…" : "Create key"}</button>
    </div>
    {message && <p className={state === "error" ? "neg" : "dim"}>{message}</p>}
    <p className="dim">Every call made with a key is rate limited (600 a minute) and every change it makes is written to the audit log under the key's name. Reference: trade-logx.com/api.</p>
  </SettingsSection>;
}
