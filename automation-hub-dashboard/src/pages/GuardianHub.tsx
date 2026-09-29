import SectionTabs from "../components/common/SectionTabs";
import { PageHeader } from "../components/common/ui";
import { useLive } from "../lib/api";
import Activity from "./guardian/Activity";
import CommandCenter from "./guardian/CommandCenter";
import Strategies from "./guardian/Strategies";
import SystemMap from "./guardian/SystemMap";
import type { GuardianStatus } from "./guardian/common";

/** Guardian: the platform's independent, read-only observer. Only what is
 *  built is shown -- incidents, risk and research come in later phases and
 *  get their tabs when they exist. */
const tabs = [
  { id: "command", label: "Command Center" }, { id: "map", label: "System Map" },
  { id: "strategies", label: "Strategies" }, { id: "activity", label: "Activity" },
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
          : active === "activity" ? <Activity status={status.data} />
            : <CommandCenter status={status.data} />}
    </>
  );
}
