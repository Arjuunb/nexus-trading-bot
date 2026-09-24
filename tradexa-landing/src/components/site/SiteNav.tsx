import { useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Menu, X } from "lucide-react";
import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/Button";
import { NAV_ROUTES, ACCENT_CLASSES, routeFor, prefetchRoute } from "@/site/routes";
import { cn, APP_URL, LOGIN_URL } from "@/lib/utils";

/**
 * The one navigation bar for the whole site.
 *
 * Every item is a route, not a hash. The previous bar scrolled you to a
 * position in a single document, which meant the six "pages" had no URLs, no
 * back-button behaviour and no way to be linked to directly. `NavLink` gives
 * each one an honest active state, and the active underline is tinted with the
 * destination page's own accent — so the chrome acknowledges which product
 * surface you are standing on instead of looking identical everywhere.
 */
export function SiteNav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Close the mobile sheet on navigation — otherwise tapping a link leaves the
  // menu covering the page you just asked for.
  useEffect(() => setOpen(false), [pathname]);

  // Lock the page behind the open mobile sheet so the sheet scrolls, not the
  // page underneath it.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  const current = routeFor(pathname);
  const accent = current ? ACCENT_CLASSES[current.accent] : ACCENT_CLASSES.gold;

  return (
    <motion.header
      initial={{ y: -24, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "fixed inset-x-0 top-0 z-50 transition-all duration-300",
        scrolled || open
          ? "border-b border-line bg-black/70 backdrop-blur-xl"
          : "border-b border-transparent",
      )}
    >
      {/* hairline that carries the current page's accent across the top */}
      <div
        aria-hidden
        className={cn(
          "absolute inset-x-0 top-0 h-px opacity-70 transition-colors duration-500",
          accent.bg,
        )}
        style={{ maskImage: "linear-gradient(to right, transparent, black 20%, black 80%, transparent)" }}
      />

      <nav className="container-x flex h-16 items-center justify-between">
        <Link to="/" aria-label="TradeLogX Nexus home" className="shrink-0">
          <Logo />
        </Link>

        <div className="hidden items-center gap-1 lg:flex">
          {NAV_ROUTES.map((r) => {
            const a = ACCENT_CLASSES[r.accent];
            return (
              <NavLink
                key={r.path}
                to={r.path}
                // Pointing at a link is a reliable signal a click is coming;
                // starting the chunk here removes the round trip from the
                // transition. Focus counts too, or keyboard users pay a cost
                // pointer users do not.
                onPointerEnter={() => prefetchRoute(r.path)}
                onFocus={() => prefetchRoute(r.path)}
                className={({ isActive }) =>
                  cn(
                    "group relative rounded-lg px-3 py-2 text-sm transition-colors",
                    isActive ? "text-white" : "text-white/55 hover:text-white",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    {r.label}
                    {/* hover: a faint underline draws out from the centre;
                        the active route keeps its own sliding accent bar */}
                    {!isActive && (
                      <span aria-hidden className="absolute inset-x-3 -bottom-px h-px origin-center scale-x-0 rounded-full bg-white/35 transition-transform duration-300 ease-out group-hover:scale-x-100" />
                    )}
                    {isActive && (
                      <motion.span
                        layoutId="nav-active"
                        transition={{ type: "spring", stiffness: 420, damping: 34 }}
                        className={cn("absolute inset-x-2 -bottom-px h-[2px] rounded-full", a.bg, a.glow)}
                      />
                    )}
                  </>
                )}
              </NavLink>
            );
          })}
        </div>

        <div className="hidden items-center gap-2 lg:flex">
          <a href={LOGIN_URL}>
            <Button variant="ghost" size="sm">
              Sign in
            </Button>
          </a>
          <a href={APP_URL}>
            <Button size="sm">Launch Platform</Button>
          </a>
        </div>

        <button
          className="text-white/80 lg:hidden"
          onClick={() => setOpen((o) => !o)}
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
        >
          {open ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
        </button>
      </nav>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
            // Solid, not translucent. At bg-black/95 the page behind showed
            // through the sheet — which on the landing page meant the grid, so
            // the menu read as part of the page it was covering rather than as
            // a surface above it.
            className="overflow-hidden border-t border-line bg-[#050505] shadow-[0_24px_60px_-12px_rgba(0,0,0,0.9)] backdrop-blur-2xl lg:hidden"
          >
            <div className="container-x flex max-h-[calc(100vh-4rem)] flex-col gap-1 overflow-y-auto py-4">
              {NAV_ROUTES.map((r) => {
                const a = ACCENT_CLASSES[r.accent];
                return (
                  <NavLink
                    key={r.path}
                    to={r.path}
                    onPointerEnter={() => prefetchRoute(r.path)}
                    onFocus={() => prefetchRoute(r.path)}
                    className={({ isActive }) =>
                      cn(
                        "flex items-center gap-3 rounded-xl px-3 py-3 transition-colors",
                        isActive ? "bg-white/[0.06]" : "hover:bg-white/[0.04]",
                      )
                    }
                  >
                    <span className={cn("h-8 w-[3px] shrink-0 rounded-full", a.bg)} />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-white">{r.label}</span>
                      <span className="block truncate text-xs text-white/45">{r.blurb}</span>
                    </span>
                  </NavLink>
                );
              })}
              <div className="mt-3 flex gap-2">
                <a href={LOGIN_URL} className="flex-1">
                  <Button variant="secondary" fullWidth size="sm">
                    Sign in
                  </Button>
                </a>
                <a href={APP_URL} className="flex-1">
                  <Button fullWidth size="sm">
                    Launch Platform
                  </Button>
                </a>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.header>
  );
}
