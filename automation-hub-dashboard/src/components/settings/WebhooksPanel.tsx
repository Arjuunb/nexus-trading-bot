import { useState } from "react";
import { apiDelete, apiGet, apiPost, apiPostJson, useLive } from "../../lib/api";
import { Field } from "../common/ui";
import SettingsSection, { type SaveState } from "./SettingsSection";

interface Webhook { id: string; url: string; events: string[]; description: string; created_at: string; active: boolean }
interface Delivery {
  id: number; event_id: string; event_type: string; status: "pending" | "delivered" | "failed" | "cancelled";
  attempts: number; last_status: number | null; last_error: string; last_attempt_at: string | null;
  next_attempt_at: string | null; delivered_at: string | null;
}
interface Created extends Webhook { secret: string }

const EVENTS = ["decision.accepted", "decision.rejected"];

function when(iso: string | null) {
  return iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}

/** Signed outbound webhooks (services/outbound_webhooks.py). */
export default function WebhooksPanel() {
  const hooks = useLive<{ webhooks: Webhook[] }>("/security/webhooks", 30000);
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>(EVENTS);
  const [fresh, setFresh] = useState<Created | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [state, setState] = useState<SaveState>("saved");
  const [message, setMessage] = useState("");

  const fail = (e: unknown) => { setMessage(e instanceof Error ? e.message.replace(/^[A-Z]+ \S+: HTTP \d+ · /, "") : "Request failed."); setState("error"); };
  const load = async (id: string) => {
    try { setDeliveries((await apiGet<{ deliveries: Delivery[] }>(`/security/webhooks/${id}/deliveries`)).deliveries); }
    catch (e) { fail(e); }
  };
  const create = async () => {
    setState("saving"); setMessage("");
    try {
      const created = await apiPostJson<Created>("/security/webhooks", { url: url.trim(), events });
      setFresh(created); setUrl(""); setState("saved"); await hooks.refetch();
    } catch (e) { fail(e); }
  };
  const test = async (id: string) => {
    try { await apiPost(`/security/webhooks/${id}/test`); setOpen(id); await load(id); }
    catch (e) { fail(e); }
  };
  const disable = async (id: string) => {
    try { await apiDelete(`/security/webhooks/${id}`); await hooks.refetch(); if (open === id) await load(id); }
    catch (e) { fail(e); }
  };
  const toggle = (event: string) => setEvents((prev) => prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event]);

  const list = hooks.data?.webhooks ?? [];
  return <SettingsSection title="Webhooks" description="Every decision the engine records, sent to your endpoint as a signed event. Delivery is at-least-once, retried for 24 hours." state={state}>
    {fresh && <div className="api-key-fresh" role="status">
      <p><b>Signing secret — copy it now.</b> Use it to verify the <code>Nexus-Signature</code> header; it will not be shown again.</p>
      <div className="api-key-token"><code>{fresh.secret}</code></div>
      <button className="btn btn-sm btn-ghost" onClick={() => setFresh(null)}>I have stored it</button>
    </div>}

    {list.length > 0 && <div className="audit-table-wrap"><table className="data-table">
      <thead><tr><th>Endpoint</th><th>Events</th><th>Created</th><th>Status</th><th /></tr></thead>
      <tbody>{list.map((w) => <tr key={w.id}>
        <td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }} title={w.url}>{w.url}</td>
        <td>{w.events.map((e) => <span key={e} className="lab-badge" style={{ marginRight: 4 }}>{e.replace("decision.", "")}</span>)}</td>
        <td className="dim">{when(w.created_at)}</td>
        <td className={w.active ? "pos" : "dim"}>{w.active ? "active" : "disabled"}</td>
        <td><span className="row-actions">
          <button className="btn btn-sm btn-ghost" onClick={() => { const next = open === w.id ? null : w.id; setOpen(next); if (next) void load(next); }}>{open === w.id ? "Hide" : "Deliveries"}</button>
          {w.active && <button className="btn btn-sm btn-ghost" onClick={() => void test(w.id)}>Send test</button>}
          {w.active && <button className="btn btn-sm btn-danger" onClick={() => void disable(w.id)}>Disable</button>}
        </span></td>
      </tr>)}</tbody>
    </table></div>}

    {open && <div className="audit-table-wrap"><table className="data-table audit-table">
      <thead><tr><th>Event</th><th>Type</th><th>Status</th><th>Attempts</th><th>Last answer</th><th>Next try</th></tr></thead>
      <tbody>
        {deliveries.map((d) => <tr key={d.id}>
          <td className="dim">{d.event_id}</td><td>{d.event_type}</td>
          <td className={d.status === "delivered" ? "pos" : d.status === "failed" ? "neg" : "dim"}>{d.status}</td>
          <td>{d.attempts}</td>
          <td className="dim">{d.last_status ?? (d.last_error || "—")} {d.last_attempt_at ? `· ${when(d.last_attempt_at)}` : ""}</td>
          <td className="dim">{when(d.next_attempt_at)}</td>
        </tr>)}
        {deliveries.length === 0 && <tr><td colSpan={6} className="dim">No deliveries yet.</td></tr>}
      </tbody>
    </table></div>}

    <div className="form-grid-2">
      <Field label="Endpoint URL" hint="https:// only.">
        <input value={url} placeholder="https://example.com/nexus-events" onChange={(e) => { setUrl(e.target.value); setState("dirty"); setMessage(""); }} />
      </Field>
      <Field label="Events">
        <span style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          {EVENTS.map((e) => <label key={e} className="audit-filter" style={{ marginLeft: 0 }}><input type="checkbox" checked={events.includes(e)} onChange={() => toggle(e)} /> {e}</label>)}
        </span>
      </Field>
    </div>
    <div className="row-actions" style={{ justifyContent: "flex-start" }}>
      <button className="btn btn-primary" disabled={state === "saving" || !url.trim() || events.length === 0} onClick={() => void create()}>{state === "saving" ? "Adding…" : "Add webhook"}</button>
    </div>
    {message && <p className={state === "error" ? "neg" : "dim"}>{message}</p>}
    <p className="dim">Each request carries <code>Nexus-Signature: t=…,v1=…</code> — HMAC-SHA256 of <code>t.body</code> with the secret. Verify it before parsing; every SDK has a helper. Handlers must be idempotent: use the event's <code>idempotency_key</code>.</p>
  </SettingsSection>;
}
