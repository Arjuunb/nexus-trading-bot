import SectionTabs from "../components/common/SectionTabs";
import TradeJournal from "./TradeJournal";
import JournalAnalytics from "./JournalAnalytics";
import WeeklyReview from "./WeeklyReview";
import DecisionJournal from "./Journal";
import Decisions from "./Decisions";
import Memory from "./Memory";

const tabs = [
  { id: "trades", label: "Trades" }, { id: "analytics", label: "Analytics" },
  { id: "weekly", label: "Weekly Review" }, { id: "decision-journal", label: "Decision Journal" },
  { id: "decisions", label: "Decisions" }, { id: "memory", label: "Memory" }, { id: "notes", label: "Notes" },
];

export default function JournalHub({ tab, focusId }: { tab?: string; focusId?: string }) {
  const active = tabs.some((item) => item.id === tab) ? tab : "trades";
  const page = (() => {
    switch (active) {
      case "analytics": return <JournalAnalytics />;
      case "weekly": return <WeeklyReview />;
      case "decision-journal": return <DecisionJournal focusId={focusId} />;
      case "decisions": return <Decisions focusId={focusId} />;
      case "memory": case "notes": return <Memory />;
      default: return <TradeJournal focusId={focusId} />;
    }
  })();
  return <><SectionTabs tabs={tabs} active={active} />{page}</>;
}
