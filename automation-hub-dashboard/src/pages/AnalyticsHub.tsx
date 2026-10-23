import SectionTabs from "../components/common/SectionTabs";
import Analytics from "./Analytics";
import AIInsights from "./AIIntelligence";
import ContextIntelligence from "./ContextIntelligence";

const tabs = [{ id: "overview", label: "Overview" }, { id: "performance", label: "Performance" }, { id: "strategies", label: "Strategies" }, { id: "assets", label: "Assets" }, { id: "sessions", label: "Sessions" }, { id: "risk", label: "Risk" }, { id: "context", label: "Context Intelligence" }, { id: "ai", label: "AI Insights" }];
export default function AnalyticsHub({ tab }: { tab?: string }) {
  const active = tabs.some((item) => item.id === tab) ? tab : "overview";
  return <><SectionTabs tabs={tabs} active={active} />{active === "ai" ? <AIInsights /> : active === "context" ? <ContextIntelligence /> : <Analytics />}</>;
}
