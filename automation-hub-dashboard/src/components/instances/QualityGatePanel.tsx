import { useState } from "react";
import { Badge } from "../common/ui";
import { apiPatchJson, useLive, type InstanceQualityGate } from "../../lib/api";
import { useApp } from "../../app-context";

// Per-instance switch for the Decision Brain quality gate. On by default.
// Off lets this instance's own strategy decide: the Brain's score and its
// views on trend, regime and volatility stop blocking entries. Blocks that
// protect position size (target under 1R, stop too tight or too wide, the
// losing-streak cooldown) and every risk limit still apply.

export default function QualityGatePanel({ instanceId }: { instanceId: string }) {
  const app = useApp();
  const gate = useLive<InstanceQualityGate>(`/instances/${instanceId}/quality-gate`, 60_000);
  const [busy, setBusy] = useState(false);
  const g = gate.data;

  async function toggle() {
    if (!g) return;
    if (g.enforced && !window.confirm(
      "Turn the Decision Brain quality gate off for this instance? Its strategy's signals will no longer " +
      "be blocked for a low quality score or for the Brain's view of trend, regime or volatility. Size " +
      "protections and risk limits still apply. This changes which trades the instance takes.")) return;
    setBusy(true);
    try {
      await apiPatchJson(`/instances/${instanceId}/quality-gate`, { enforced: !g.enforced });
      await gate.refetch();
      app.toast(g.enforced ? "Quality gate off for this instance" : "Quality gate back on", "success");
    } catch (err) {
      app.toast(err instanceof Error ? err.message : "Could not change the quality gate", "error");
    } finally {
      setBusy(false);
    }
  }

  if (gate.error && !g) return <p className="dim" style={{ marginTop: 10 }}>Quality gate switch unavailable: {gate.error}</p>;
  if (!g) return null;

  return (
    <div className="quality-gate" style={{ marginTop: 12, padding: 12, border: "1px solid var(--card-border)", borderRadius: 10 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <b>Decision Brain quality gate</b>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Badge text={g.enforced ? `On · min ${g.min_score}` : "Off"} tone={g.enforced ? "gold" : "amber"} />
          <button className={`btn btn-sm ${g.enforced ? "btn-soft" : "btn-primary"}`} disabled={busy} onClick={() => void toggle()} aria-pressed={!g.enforced}>
            {busy ? "Saving…" : g.enforced ? "Turn off" : "Turn on"}
          </button>
        </span>
      </div>
      <p className="dim" style={{ margin: "6px 0 0", fontSize: 12.5 }}>
        {g.enforced
          ? `Every entry is scored by the Decision Brain and blocked below ${g.min_score}, or when the Brain judges the trend, regime or volatility wrong for it.`
          : "The strategy's own rules decide. Setups are still scored and journaled, but only the size protections (target under 1R, stop too tight or too wide, losing-streak cooldown) and the risk limits can block an entry."}
        {" "}Applies from the next signal.
      </p>
    </div>
  );
}
