import { lazy, Suspense, useEffect, useState } from "react";
import Sidebar from "./components/layout/Sidebar";
import TopHeader from "./components/layout/TopHeader";
import TickerBar from "./components/layout/TickerBar";
import Toasts, { type ToastItem } from "./components/common/Toasts";
import ErrorBoundary from "./components/common/ErrorBoundary";
import { API_BASE } from "./lib/api";
// Pages are code-split (lazy) so the initial bundle is just the shell + the
// first page, not all ~25 pages and their chart libraries.
const Overview = lazy(() => import("./pages/Overview"));
const StrategiesPage = lazy(() => import("./pages/Strategies"));
const PaperTradingPage = lazy(() => import("./pages/PaperTrading"));
const BotTerminalPage = lazy(() => import("./pages/BotTerminal"));
const BacktestingPage = lazy(() => import("./pages/Backtesting"));
const AlertsPage = lazy(() => import("./pages/Alerts"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const BotDetail = lazy(() => import("./pages/BotDetail"));
const MarketsPage = lazy(() => import("./pages/Markets"));
const MarketDataManagerPage = lazy(() => import("./pages/MarketDataManager"));
const SymbolExplorerPage = lazy(() => import("./pages/SymbolExplorer"));
const SimulationPage = lazy(() => import("./pages/Simulation"));
const ReplayPage = lazy(() => import("./pages/Replay"));
const EvolutionPage = lazy(() => import("./pages/Evolution"));
const LiveTradingPage = lazy(() => import("./pages/LiveTrading"));
const AIAssistantPage = lazy(() => import("./pages/AIAssistant"));
const StrategyProofPage = lazy(() => import("./pages/StrategyProof"));
const OptimizationPage = lazy(() => import("./pages/Optimization"));
const TradingInstancesPage = lazy(() => import("./pages/TradingInstancesHub"));
const StrategyStudioPage = lazy(() => import("./pages/StrategyStudioHub"));
const PortfolioPage = lazy(() => import("./pages/PortfolioHub"));
const AnalyticsPage = lazy(() => import("./pages/AnalyticsHub"));
const JournalPage = lazy(() => import("./pages/JournalHub"));
const RiskHealthPage = lazy(() => import("./pages/RiskHealthHub"));
const ForwardValidationPage = lazy(() => import("./pages/ForwardValidation"));
const NativeSMCVisualPage = lazy(() => import("./pages/NativeSMCVisual"));
const SMCStrategyLabPage = lazy(() => import("./pages/SMCStrategyLab"));
const SMCAgentPage = lazy(() => import("./pages/SMCAgent"));
const AdaptiveLabPage = lazy(() => import("./pages/AdaptiveLab"));
const PriceActionVisualPage = lazy(() => import("./pages/PriceActionVisual"));
const InstanceVisualLabPage = lazy(() => import("./pages/InstanceVisualLab"));
import { AppContext, parseHash, slug } from "./app-context";

const MOBILE = "(max-width: 720px)";

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [collapsed, setCollapsed] = useState(() => {
    try { return JSON.parse(localStorage.getItem("hub.settings.general") || "{}").sidebar_default === "collapsed"; } catch { return false; }
  });
  const [mobileNav, setMobileNav] = useState(false);   // off-canvas drawer (small screens)
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(() => {
    try { return localStorage.getItem("tradexa.selectedInstanceId"); } catch { return null; }
  });
  const active = route.page;

  useEffect(() => {
    void fetch(`${API_BASE}/user/settings?ns=settings-center`, { credentials: "include" })
      .then((res) => res.ok ? res.json() : null)
      .then((body) => {
        const general = body?.data?.general;
        if (!general) return;
        localStorage.setItem("hub.settings.general", JSON.stringify(general));
        if (!window.matchMedia(MOBILE).matches) setCollapsed(general.sidebar_default === "collapsed");
        if (general.density === "compact" || general.density === "comfortable") document.documentElement.dataset.density = general.density;
      }).catch(() => undefined);
  }, []);

  const toast = (msg: string, tone: "success" | "error" | "info" = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 2600);
  };

  // Hash routing: the URL hash is the single source of truth for the page,
  // so the browser back/forward buttons work.
  useEffect(() => {
    const onHash = () => {
      const next = parseHash();
      if (next.redirectHash) {
        window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${next.redirectHash}`);
      }
      setRoute(next);
    };
    window.addEventListener("hashchange", onHash);
    if (!window.location.hash) window.location.hash = "/dashboard";
    else onHash();
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // On phones the hamburger opens an off-canvas drawer; on desktop it collapses
  // the rail to icons. Escape and picking a page both close the drawer, and
  // growing back to desktop width clears any stuck open state.
  const toggleSidebar = () => {
    if (window.matchMedia(MOBILE).matches) setMobileNav((o) => !o);
    else setCollapsed((c) => !c);
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMobileNav(false); };
    const onResize = () => { if (!window.matchMedia(MOBILE).matches) setMobileNav(false); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("resize", onResize); };
  }, []);

  const go = (page: string) => { window.location.hash = "/" + slug(page); setMobileNav(false); };
  const viewBot = (id: string) => { window.location.hash = "/bot/" + id; setMobileNav(false); };
  const selectInstance = (id: string) => {
    setSelectedInstanceId(id);
    try { localStorage.setItem("tradexa.selectedInstanceId", id); } catch { /* private mode */ }
  };
  const viewInstance = (id: string) => { selectInstance(id); window.location.hash = "/instance/" + id; setMobileNav(false); };

  const renderPage = () => {
    switch (active) {
      case "Markets": return <MarketsPage />;
      case "Market Data": return <MarketDataManagerPage />;
      case "Symbols": return <SymbolExplorerPage />;
      case "Strategies": return <StrategiesPage />;
      case "Backtesting": return <BacktestingPage />;
      case "Optimization Lab": return <OptimizationPage />;
      case "Simulation": return <SimulationPage />;
      case "Replay": return <ReplayPage />;
      // Paper Trading IS the Bot Observation Terminal (the heart of the app);
      // the classic account/blotter view lives on as "Paper Account".
      case "Paper Trading": return <BotTerminalPage />;
      case "Trading Instances": return <TradingInstancesPage instanceId={route.instanceId} tab={route.tab} />;
      case "Paper Account": return <PaperTradingPage />;
      case "Live Trading": return <LiveTradingPage />;
      case "Portfolio": return <PortfolioPage tab={route.tab} />;
      case "Analytics": return <AnalyticsPage tab={route.tab} />;
      case "Strategy Proof": return <StrategyProofPage />;
      case "Forward Validation": return <ForwardValidationPage />;
      case "SMC Strategy Lab": return <SMCStrategyLabPage />;
      case "SMC Agent": return <SMCAgentPage />;
      case "Adaptive MTF Lab": return <AdaptiveLabPage />;
      case "SMC Visual Lab": return <NativeSMCVisualPage />;
      case "Price Action Lab": return <PriceActionVisualPage />;
      case "Instance Visual Lab": return <InstanceVisualLabPage />;
      case "Strategy Studio": return <StrategyStudioPage tab={route.tab} />;
      case "AI Assistant": return <AIAssistantPage />;
      case "Risk & Health": return <RiskHealthPage tab={route.tab} />;
      case "Evolution": return <EvolutionPage />;
      case "Journal": return <JournalPage focusId={route.focusId} tab={route.tab} />;
      case "Settings": return <SettingsPage />;
      case "Alerts": return <AlertsPage />;
      case "BotDetail": return <BotDetail botId={route.botId} />;
      default: return <Overview />;
    }
  };

  const title = active === "BotDetail" ? route.botId : active;

  return (
    <AppContext.Provider value={{ go, viewBot, viewInstance, selectedInstanceId, selectInstance, toast }}>
      <div className={`app ${collapsed ? "sidebar-collapsed" : ""} ${mobileNav ? "mobile-nav-open" : ""}`}>
        <Toasts items={toasts} />
        {mobileNav && <div className="nav-backdrop" onClick={() => setMobileNav(false)} aria-hidden />}
        <Sidebar active={active} onSelect={go} collapsed={collapsed} />

        <div className="main">
          <TopHeader onToggleSidebar={toggleSidebar} title={title} />
          <div className="content">
            <ErrorBoundary resetKey={[active, route.tab, route.botId, route.instanceId, route.focusId].filter(Boolean).join(":")}>
              <Suspense fallback={<div className="dim" style={{ padding: 24 }}>Loading…</div>}>
                {renderPage()}
              </Suspense>
            </ErrorBoundary>
          </div>
          <TickerBar surface={active} />
        </div>
      </div>
    </AppContext.Provider>
  );
}
