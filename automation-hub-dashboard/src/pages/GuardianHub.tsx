import SectionTabs from "../components/common/SectionTabs";
import { PageHeader } from "../components/common/ui";
import { useLive } from "../lib/api";
import Activity from "./guardian/Activity";
import AskGuardian from "./guardian/AskGuardian";
import CommandCenter from "./guardian/CommandCenter";
import Incidents from "./guardian/Incidents";
import Integrity from "./guardian/Integrity";
import Reports from "./guardian/Reports";
import Research from "./guardian/Research";
import Strategies from "./guardian/Strategies";
import SystemMap from "./guardian/SystemMap";
import type { GuardianStatus } from "./guardian/common";

/** Guardian: the platform's independent observer. It reads the platform and
 *  never trades; research is hypotheses under test, reasoning is advice, and
 *  recovery is operational and off unless the owner enables it. */
const tabs = [
  { id: "command", label: "Command Center" }, { id: "incidents", label: "Incidents" },
  { id: "map", label: "System Map" }, { id: "strategies", label: "Strategies" },
  { id: "integrity", label: "Risk & Integrity" }, { id: "research", label: "Research" },
  { id: "ask", label: "Ask Guardian" }, { id: "reports", label: "Reports & Recovery" },
  { id: "activity", label: "Activity" },
];

export default function GuardianHub({ tab }: { tab?: string }) {
  const active = tabs.some((item) => item.id === tab) ? tab : "command";
  const status = useLive<GuardianStatus>("/guardian/status", 5000);
  return (
    <>
      <PageHeader title="Guardian"
        subtitle="Observes the whole platform, read-only — what is happening, why, and whether it is behaving correctly" />
      <SectionTabs tabs={tabs} active={active} />
      {status.error && !status.data && <p className="neg">Guardian status is unavailable: {status.error}</p>}
      {!status.data ? (!status.error && <p className="dim">Loading…</p>)
        : active === "map" ? <SystemMap status={status.data} />
          : active === "strategies" ? <Strategies />
          : active === "incidents" ? <Incidents />
          : active === "integrity" ? <Integrity />
          : active === "research" ? <Research />
          : active === "ask" ? <AskGuardian />
          : active === "reports" ? <Reports />
          : active === "activity" ? <Activity status={status.data} />
            : <CommandCenter status={status.data} />}
    </>
  );
}
