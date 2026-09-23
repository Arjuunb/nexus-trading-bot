import { createContext, useContext } from "react";

export interface AppApi {
  /** Navigate to a page by its sidebar label (updates the URL hash). */
  go: (page: string) => void;
  /** Open a single bot's detail page (route #/bot/<id>). */
  viewBot: (id: string) => void;
  /** Open one isolated Trading Instance (route #/instance/<id>). */
  viewInstance: (id: string) => void;
  /** Dashboard/header context. Persisted locally; execution state remains server-owned. */
  selectedInstanceId: string | null;
  selectInstance: (id: string) => void;
  /** Show a transient toast notification. */
  toast: (msg: string, tone?: "success" | "error" | "info") => void;
}

export const AppContext = createContext<AppApi>({
  go: () => {},
  viewBot: () => {},
  viewInstance: () => {},
  selectedInstanceId: null,
  selectInstance: () => {},
  toast: () => {},
});

export const useApp = () => useContext(AppContext);

// The sidebar, organised as the trading lifecycle: observe the bot → build a
// strategy → prove it → run it → study the results → govern the system.
// Grouped sections keep the platform feeling like one operating system
// instead of a flat list of pages.
export const NAV_GROUPS: { title: string | null; items: string[] }[] = [
  { title: null, items: ["Dashboard"] },
  { title: "Trading", items: ["Trading Instances", "Instance Visual Lab", "Strategy Studio", "Paper Trading", "Live Trading"] },
  { title: "Research", items: ["Price Action Lab", "SMC Strategy Lab", "SMC Agent", "Adaptive MTF Lab", "Replay", "Backtesting", "Optimization Lab", "Forward Validation"] },
  { title: "Performance", items: ["Portfolio", "Analytics"] },
  { title: "Records", items: ["Journal"] },
  { title: "System", items: ["Market Data", "Risk & Health"] },
];

export const NAV_LABELS: string[] = NAV_GROUPS.flatMap((g) => g.items);

// Extra routes reachable by hash but not shown in the main nav. Every page
// still works — these are linked from their sibling pages instead of taking
// a sidebar slot (Markets/Symbols from Portfolio, Strategies + Strategy Proof
// from Strategy Studio, Simulation from Backtesting, Safety Center from Live
// Trading + Risk Manager, Evolution from Memory, Paper Account from the Paper
// Trading terminal, AI Assistant from AI Intelligence, SMC Visual Lab from the
// SMC Strategy Lab, whose market section points at it for the Pine reference
// and parity review).
const EXTRA_ROUTES = [
  "Alerts", "Symbols", "Markets", "Strategies", "Strategy Proof",
  "Simulation", "Evolution", "Safety Center", "Paper Account", "AI Assistant", "Settings",
  "SMC Visual Lab",
] as const;

// Old bookmarks / saved hashes keep working after the reorganisation.
const LEGACY_SLUGS: Record<string, string> = {
  "overview": "Dashboard",
  "bot-terminal": "Paper Trading",   // the terminal IS the paper-trading page now
  "bots": "Trading Instances",
};

export const LEGACY_REDIRECTS: Record<string, { page: string; tab: string }> = {
  "fleet-manager": { page: "Trading Instances", tab: "fleet" },
  "grid-dca": { page: "Strategy Studio", tab: "grid-dca" },
  "allocation": { page: "Portfolio", tab: "allocation" },
  "ai-intelligence": { page: "Analytics", tab: "ai" },
  "decisions": { page: "Journal", tab: "decisions" },
  "decision-archive": { page: "Journal", tab: "decisions" },
  "memory": { page: "Journal", tab: "memory" },
  "risk-manager": { page: "Risk & Health", tab: "risk" },
  "bot-health": { page: "Risk & Health", tab: "health" },
  "logs": { page: "Risk & Health", tab: "logs" },
};

export const slug = (page: string) => page.toLowerCase().replace(/&/g, "").trim().replace(/\s+/g, "-").replace(/-+/g, "-");

export interface Route {
  page: string;
  botId: string;
  instanceId?: string;
  /** Deep-link target id — the decision cycle or trade to focus on arrival. */
  focusId?: string;
  tab?: string;
  redirectHash?: string;
}

export const parseHash = (): Route => {
  const raw = window.location.hash.replace(/^#\/?/, "").trim();
  const [h, query = ""] = raw.split("?", 2);
  const tab = new URLSearchParams(query).get("tab") ?? undefined;
  const bot = h.match(/^bot\/(.+)$/);
  if (bot) return { page: "BotDetail", botId: bot[1] };
  const instance = h.match(/^instance\/([a-zA-Z0-9_-]+)$/);
  if (instance) return { page: "Trading Instances", botId: "", instanceId: instance[1] };
  // shareable deep links to a single decision or trade (for audit/sharing)
  const dec = h.match(/^decision\/(.+)$/);
  if (dec) return { page: "Journal", botId: "", focusId: decodeURIComponent(dec[1]), tab: "decisions" };
  const trd = h.match(/^trade\/(.+)$/);
  if (trd) return { page: "Journal", botId: "", focusId: decodeURIComponent(trd[1]) };
  const redirected = LEGACY_REDIRECTS[h];
  if (redirected) {
    const redirectHash = `/${slug(redirected.page)}?tab=${redirected.tab}`;
    return { page: redirected.page, botId: "", tab: redirected.tab, redirectHash };
  }
  if (LEGACY_SLUGS[h]) return { page: LEGACY_SLUGS[h], botId: "" };
  const found = [...NAV_LABELS, ...EXTRA_ROUTES].find((n) => slug(n) === h);
  return { page: found ?? "Dashboard", botId: "", tab };
};
