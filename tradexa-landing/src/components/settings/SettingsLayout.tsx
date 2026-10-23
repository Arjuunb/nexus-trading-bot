import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, Check, CloudOff, Loader2, Menu, X } from "lucide-react";
import { Logo } from "@/components/Logo";
import { Badge } from "@/components/ui/Badge";
import { SETTINGS_NAV, findItem } from "@/settings/nav";
import { useSettings } from "@/settings/store";
import { cn, APP_URL } from "@/lib/utils";
import { AppSurface } from "@/components/site/backdrops";

function SaveIndicator() {
  const { saveState, backendConnected } = useSettings();
  const map = {
    saving: { icon: Loader2, text: "Saving…", cls: "text-white/50", spin: true },
    saved: { icon: Check, text: backendConnected ? "Saved" : "Saved locally", cls: "text-emerald-soft", spin: false },
    error: { icon: CloudOff, text: "Save failed", cls: "text-loss-soft", spin: false },
    idle: null,
  } as const;
  const s = map[saveState];
  return (
    <div className="h-5">
      <AnimatePresence mode="wait">
        {s && (
          <motion.span
            key={saveState}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className={cn("inline-flex items-center gap-1.5 text-xs font-medium", s.cls)}
          >
            <s.icon className={cn("h-3.5 w-3.5", s.spin && "animate-spin")} />
            {s.text}
          </motion.span>
        )}
      </AnimatePresence>
    </div>
  );
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="space-y-6">
      {SETTINGS_NAV.map((group) => (
        <div key={group.title}>
          <p className="mb-1.5 px-3 text-[10px] font-semibold uppercase tracking-[0.16em] text-white/30">
            {group.title}
          </p>
          <div className="space-y-0.5">
            {group.items.map((item) => (
              <NavLink
                key={item.slug}
                to={`/settings/${item.slug}`}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cn(
                    "group flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors",
                    isActive
                      ? item.danger
                        ? "bg-loss/10 text-loss-soft"
                        : "bg-white/[0.06] text-white"
                      : item.danger
                        ? "text-loss/70 hover:bg-loss/[0.06] hover:text-loss-soft"
                        : "text-white/55 hover:bg-white/[0.04] hover:text-white",
                  )
                }
              >
                <item.icon className="h-4 w-4 shrink-0" />
                <span className="flex-1 truncate">{item.label}</span>
                {item.badge && <Badge tone="neutral">{item.badge}</Badge>}
              </NavLink>
            ))}
          </div>
        </div>
      ))}
    </nav>
  );
}

export default function SettingsLayout() {
  const { loadState, error, identity } = useSettings();
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const slug = location.pathname.split("/")[2] || "overview";
  const active = findItem(slug);

  return (
    <div className="min-h-screen">
      <AppSurface />
      {/* top bar */}
      <header className="sticky top-0 z-40 border-b border-line bg-ink/80 backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-3 px-4 sm:px-6">
          <div className="flex items-center gap-3">
            <button
              className="rounded-lg p-1.5 text-white/70 hover:bg-white/5 lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open settings menu"
            >
              <Menu className="h-5 w-5" />
            </button>
            <Link to="/" aria-label="Home">
              <Logo />
            </Link>
            <span className="hidden text-white/20 sm:inline">/</span>
            <span className="hidden text-sm font-medium text-white/70 sm:inline">Settings</span>
          </div>
          <div className="flex items-center gap-4">
            <SaveIndicator />
            <a href={APP_URL} className="text-sm text-white/55 transition hover:text-white">
              <ArrowLeft className="mr-1 inline h-4 w-4" />
              Back to app
            </a>
          </div>
        </div>
      </header>

      <div className="mx-auto flex max-w-7xl gap-8 px-4 py-8 sm:px-6">
        {/* desktop sidebar */}
        <aside className="hidden w-60 shrink-0 lg:block">
          <div className="sticky top-24">
            <NavList />
          </div>
        </aside>

        {/* content */}
        <main className="min-w-0 flex-1">
          <div className="mx-auto max-w-3xl">
            {loadState !== "ready" && (
              <p role="status" className="mb-5 rounded-lg border border-line p-3 text-sm text-white/65">
                {loadState === "loading" ? "Loading your saved settings…"
                  : loadState === "error" ? (error ?? "Your saved settings could not be loaded. Please sign in again.")
                  : "Sign in to load your saved settings."}
              </p>
            )}
            <p className="mb-4 text-[11px] font-medium uppercase tracking-wider text-white/30 lg:hidden">
              {active?.label}
            </p>
            {loadState === "ready" && <Outlet key={identity?.id ?? "legacy-workspace"} />}
          </div>
        </main>
      </div>

      {/* mobile drawer */}
      <AnimatePresence>
        {mobileOpen && (
          <motion.div
            className="fixed inset-0 z-50 lg:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div className="absolute inset-0 bg-black/60" onClick={() => setMobileOpen(false)} />
            <motion.div
              initial={{ x: -300 }}
              animate={{ x: 0 }}
              exit={{ x: -300 }}
              transition={{ type: "spring", stiffness: 400, damping: 40 }}
              className="absolute inset-y-0 left-0 w-72 overflow-y-auto border-r border-line bg-ink-800 p-4"
            >
              <div className="mb-6 flex items-center justify-between">
                <span className="text-sm font-semibold text-white">Settings</span>
                <button onClick={() => setMobileOpen(false)} aria-label="Close menu">
                  <X className="h-5 w-5 text-white/60" />
                </button>
              </div>
              <NavList onNavigate={() => setMobileOpen(false)} />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
