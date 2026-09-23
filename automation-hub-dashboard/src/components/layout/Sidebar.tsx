import Logo from "../common/Logo";
import {
  LayoutDashboard, CandlestickChart, Search, Layers, FlaskConical, RefreshCw, PlayCircle,
  NotebookPen, Rocket, Wallet, BarChart3, ShieldAlert, Brain,
  BookOpen, Activity, BadgeCheck, Settings, Lock, Blocks, SquareTerminal, SlidersHorizontal, GitCompareArrows, type LucideIcon,
} from "lucide-react";
import { NAV_GROUPS } from "../../app-context";

// Real icons (lucide), one per page — gold when active, sky on hover.
const NAV_LUCIDE: Record<string, LucideIcon> = {
  Dashboard: LayoutDashboard,
  Markets: CandlestickChart,
  "Market Data": Activity,
  Symbols: Search,
  Strategies: Layers,
  Backtesting: FlaskConical,
  "Optimization Lab": SlidersHorizontal,
  Simulation: RefreshCw,
  Replay: PlayCircle,
  "Paper Trading": SquareTerminal,   // the Bot Observation Terminal
  "Paper Account": NotebookPen,
  "Live Trading": Rocket,
  Portfolio: Wallet,
  Analytics: BarChart3,
  "Forward Validation": GitCompareArrows,
  "Strategy Proof": BadgeCheck,
  "Strategy Studio": Blocks,
  "SMC Strategy Lab": Brain,
  "SMC Visual Lab": CandlestickChart,
  "Adaptive MTF Lab": RefreshCw,
  "Price Action Lab": Activity,
  "Instance Visual Lab": Activity,
  "Risk & Health": ShieldAlert,
  Evolution: Brain,
  Journal: BookOpen,
  Settings: Settings,
  "Safety Center": Lock,
};

interface SidebarProps {
  active: string;
  onSelect: (item: string) => void;
  collapsed?: boolean;
}

export default function Sidebar({ active, onSelect, collapsed }: SidebarProps) {
  return (
    <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
      <a
        className="brand"
        href={(import.meta.env.VITE_LANDING_URL as string | undefined) || "/"}
        title="Back to TradeLogX Nexus home"
      >
        <span className="brand-mark"><Logo size={30} /></span>
        <span className="brand-name">
          TradeLogX
          <span className="brand-sub">Nexus</span>
        </span>
      </a>

      <nav className="nav">
        {NAV_GROUPS.map((group, gi) => (
          <div className="nav-group" key={group.title ?? gi}>
            {group.title && <div className="nav-group-title">{group.title}</div>}
            {group.items.map((item) => {
              const NavIcon = NAV_LUCIDE[item] ?? LayoutDashboard;
              return (
                <button
                  key={item}
                  className={`nav-item ${active === item ? "active" : ""}`}
                  onClick={() => onSelect(item)}
                  type="button"
                >
                  <NavIcon size={18} strokeWidth={1.9} className="nav-ico" aria-hidden />
                  <span>{item}</span>
                </button>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="sidebar-footer">
        <button
          className={`nav-item ${active === "Settings" ? "active" : ""}`}
          onClick={() => onSelect("Settings")}
          type="button"
        >
          <Settings size={18} strokeWidth={1.9} className="nav-ico" aria-hidden />
          <span>Settings</span>
        </button>
      </div>
    </aside>
  );
}
