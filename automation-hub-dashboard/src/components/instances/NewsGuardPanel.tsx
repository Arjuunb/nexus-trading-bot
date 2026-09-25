import { useState } from "react";
import { Badge } from "../common/ui";
import { apiPatchJson, useLive, type InstanceEventGuard } from "../../lib/api";
import { useApp } from "../../app-context";

// Per-instance news blackout. Off by default; when on, the instance's risk
// pipeline refuses new entries around high-impact releases and trades half
// size shortly before them. Open positions keep their stops and targets.

function until(minutes: number | null): string {
  if (minutes === null) return "";
  if (minutes < 0) return `released ${Math.round(-minutes)} min ago`;
  if (minutes < 90) return `in ${Math.round(minutes)} min`;
  const hours = minutes / 60;
  return hours < 36 ? `in ${hours.toFixed(1)} h` : `in ${Math.round(hours / 24)} d`;
}

export default function NewsGuardPanel({ instanceId }: { instanceId: string }) {
  const app = useApp();
  const guard = useLive<InstanceEventGuard>(`/instances/${instanceId}/event-guard`, 30_000);
  const [busy, setBusy] = useState(false);
  const g = guard.data;

  async function toggle() {
    if (!g) return;
    setBusy(true);
    try {
      await apiPatchJson(`/instances/${instanceId}/event-guard`, { enabled: !g.enabled });
      await guard.refetch();
      app.toast(g.enabled ? "News blackout off" : "News blackout on for this instance", "success");
    } catch (err) {
      app.toast(err instanceof Error ? err.message : "Could not change the news blackout", "error");
    } finally {
      setBusy(false);
    }
  }

  if (guard.error && !g) return <p className="dim" style={{ marginTop: 10 }}>News blackout unavailable: {guard.error}</p>;
  if (!g) return null;

  const w = g.window;
  const tone = !g.enabled ? "default" : g.halt_new_entries ? "red" : g.mode === "caution" ? "amber" : "gold";
  const label = !g.enabled ? "Off" : g.halt_new_entries ? "Blocking entries" : g.mode === "caution" ? "Half size" : "On";

  return (
    <div className="news-guard" style={{ marginTop: 12, padding: 12, border: "1px solid var(--card-border)", borderRadius: 10 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <b>News blackout</b>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Badge text={label} tone={tone} />
          <button className={`btn btn-sm ${g.enabled ? "btn-soft" : "btn-primary"}`} disabled={busy} onClick={() => void toggle()} aria-pressed={g.enabled}>
            {busy ? "Saving…" : g.enabled ? "Turn off" : "Turn on"}
          </button>
        </span>
      </div>
      <p className="dim" style={{ margin: "6px 0 0", fontSize: 12.5 }}>
        No new entries from {w.blackout_before_min} min before to {w.blackout_after_min} min after a high-impact release; half size in the {w.caution_before_min / 60} h before. Open trades keep their stops and targets. Applies from the next signal.
      </p>
      <p style={{ margin: "8px 0 0", fontSize: 12.5 }}>
        {!g.calendar_connected
          ? <span className="dim">No economic calendar connected yet, so there is nothing to act on. Check the feed in Safety Center.</span>
          : g.next_event
            ? <>Next: <b>{g.next_event.name}</b> {until(g.minutes_to_event)}{!g.enabled && g.mode !== "normal" ? <span className="dim"> · would be {g.mode === "blackout" ? "blocking entries" : "half size"} if on</span> : null}</>
            : <span className="dim">No high-impact releases coming up in the calendar.</span>}
      </p>
    </div>
  );
}
