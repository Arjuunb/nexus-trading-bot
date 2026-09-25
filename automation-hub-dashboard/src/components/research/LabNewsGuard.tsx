import { useState } from "react";
import { apiPatchJson, useLive, type LabEventGuard } from "../../lib/api";
import { useApp } from "../../app-context";

// News blackout for one research lab (Price Action, SMC or Adaptive). Off by
// default. When on, the lab places no new strategy entry from 30 min before a
// high-impact release until 15 min after it; open positions keep their stops
// and targets, and manual orders are not affected. Renders as a section of the
// lab's Controls sidebar.

function until(minutes: number | null): string {
  if (minutes === null) return "";
  if (minutes < 0) return `released ${Math.round(-minutes)} min ago`;
  if (minutes < 90) return `in ${Math.round(minutes)} min`;
  const hours = minutes / 60;
  return hours < 36 ? `in ${hours.toFixed(1)} h` : `in ${Math.round(hours / 24)} d`;
}

// An older server has no such route, and a proxy can answer with anything:
// render only a response that has the fields this section reads.
function isGuard(value: unknown): value is LabEventGuard {
  const v = value as LabEventGuard | null;
  return !!v && typeof v.enabled === "boolean" && !!v.window && typeof v.window.blackout_before_min === "number";
}

export default function LabNewsGuard({ lab }: { lab: LabEventGuard["lab"] }) {
  const app = useApp();
  const guard = useLive<LabEventGuard>(`/research/event-guard/${lab}`, 30_000);
  const [busy, setBusy] = useState(false);
  const g = isGuard(guard.data) ? guard.data : null;

  async function toggle() {
    if (!g) return;
    setBusy(true);
    try {
      await apiPatchJson(`/research/event-guard/${lab}`, { enabled: !g.enabled });
      await guard.refetch();
      app.toast(g.enabled ? `News blackout off for the ${g.label}` : `News blackout on for the ${g.label}`, "success");
    } catch (err) {
      app.toast(err instanceof Error ? err.message : "Could not change the news blackout", "error");
    } finally {
      setBusy(false);
    }
  }

  const status = !g ? "" : !g.enabled ? "Off" : g.halt_new_entries ? "On · pausing entries now" : "On";
  return (
    <section className="lab-news-guard" data-testid={`lab-news-guard-${lab}`}>
      <h2>News blackout</h2>
      {!g && (guard.error || guard.data) ? <small>Unavailable{guard.error ? `: ${guard.error}` : " on this server"}</small> : !g ? <small>Loading…</small> : <>
        <p className="pa-saved-config">
          <b>{status}</b>
          <span>{!g.calendar_connected ? "No economic calendar connected yet"
            : g.next_event ? `Next: ${g.next_event.name} ${until(g.minutes_to_event)}${!g.enabled && g.mode === "blackout" ? " · would pause entries if on" : ""}`
            : "No high-impact releases coming up"}</span>
        </p>
        <button type="button" className={`btn btn-sm ${g.enabled ? "btn-soft" : "btn-primary"}`} disabled={busy}
          onClick={() => void toggle()} aria-pressed={g.enabled}>
          {busy ? "Saving…" : g.enabled ? "Turn off" : "Turn on"}
        </button>
        <small>No new strategy entries from {g.window.blackout_before_min} min before to {g.window.blackout_after_min} min after a high-impact release. Open positions keep their stops and targets; manual orders are not affected.</small>
      </>}
    </section>
  );
}
