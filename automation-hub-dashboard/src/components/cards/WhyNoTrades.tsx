import Icon from "../common/Icon";
import { useLive } from "../../lib/api";

type Instance = { symbol: string; state: string; last_error?: string; market_data?: { market_data_status?: string } };
type Snapshot = { instances: Instance[]; active_slots: number; max_active_slots: number };

/** Instance-first explanation: a stopped legacy worker is never presented as a paper-engine failure. */
export default function WhyNoTrades() {
  const { data } = useLive<Snapshot>("/instances", 5000);
  if (!data) return null;
  const running = data.instances.filter((row) => row.state === "running");
  if (running.length) return null;
  const errored = data.instances.find((row) => row.state === "error");
  const headline = errored ? `${errored.symbol} instance needs recovery` : "No Paper Trading Instance is running";
  const detail = errored ? (errored.last_error || "Open Trading Instances and restart or inspect this worker.") : "Create or start a Trading Instance. The legacy autonomous engine is not used for Paper Trading.";
  return <div className="card" style={{ borderColor: "#f59e0b", background: "#f59e0b18", display: "flex", flexDirection: "row", padding: 16, gap: 12, alignItems: "flex-start" }}>
    <Icon name="warning" size={18} color="#f59e0b" />
    <div><div style={{ fontWeight: 600, color: "#f59e0b" }}>{headline}</div><div className="dim" style={{ marginTop: 4, lineHeight: 1.55 }}>{detail}</div><div className="dim mono" style={{ marginTop: 6, fontSize: 12 }}>{data.active_slots} / {data.max_active_slots} active slots</div></div>
  </div>;
}
