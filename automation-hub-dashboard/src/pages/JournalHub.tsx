import Icon from "../components/common/Icon";
import SectionTabs from "../components/common/SectionTabs";
import { PageHeader } from "../components/common/ui";
import { useApp } from "../app-context";
import Decisions from "./journal/Decisions";
import MemoryTab from "./journal/MemoryTab";
import Notes from "./journal/Notes";
import Trades from "./journal/Trades";
import Weekly from "./journal/Weekly";

/** The Journal: one canonical record per executed trade, the material
 *  decisions behind them, weekly reviews, memory with provenance, and notes.
 *  Records are built from execution facts by the backend's journal recorder;
 *  this page only reads them (and adds notes and proposal decisions). */
const tabs = [
  { id: "trades", label: "Trades" }, { id: "decisions", label: "Decisions" },
  { id: "weekly", label: "Weekly Reviews" }, { id: "memory", label: "Memory" }, { id: "notes", label: "Notes" },
];

export default function JournalHub({ tab, focusId }: { tab?: string; focusId?: string }) {
  const { go } = useApp();
  const active = tabs.some((item) => item.id === tab) ? tab : "trades";
  return (
    <>
      <PageHeader title="Journal"
        subtitle="Every executed trade as one structured record — facts from execution, reviews kept separate"
        actions={<button type="button" className="btn btn-soft btn-sm" onClick={() => go("Decision Archive")}>
          <Icon name="history" size={13} /> Decision Archive</button>} />
      <SectionTabs tabs={tabs} active={active} />
      {active === "decisions" ? <Decisions focusId={focusId} />
        : active === "weekly" ? <Weekly />
          : active === "memory" ? <MemoryTab />
            : active === "notes" ? <Notes />
              : <Trades focusId={focusId} />}
    </>
  );
}
