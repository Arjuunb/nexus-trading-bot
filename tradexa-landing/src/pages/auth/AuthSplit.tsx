import { useEffect, useRef } from "react";
import { Link, useLocation } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft } from "lucide-react";
import { Logo } from "@/components/Logo";
import { AppSurface } from "@/components/site/backdrops";
import { AuthShowcase } from "@/components/auth/AuthShowcase";
import { DemoModeNotice } from "@/components/auth/DemoModeNotice";
import { cn } from "@/lib/utils";
import Login from "./Login";
import Register from "./Register";

const TABS = [
  { to: "/auth/login", label: "Sign in", title: "Sign in" },
  { to: "/auth/register", label: "Create account", title: "Create account" },
] as const;

const EASE = [0.22, 1, 0.36, 1] as const;

// Moving between the two forms slides toward the tab that was picked, so the
// switch reads as one control rather than two unrelated pages.
const panel = {
  enter: (dir: number) => ({ opacity: 0, x: 18 * dir }),
  center: { opacity: 1, x: 0 },
  exit: (dir: number) => ({ opacity: 0, x: -18 * dir }),
};

/**
 * Sign in and Create account share one mounted frame: the showcase, the top
 * bar and the switch stay put and only the form changes. Both routes render
 * this layout (App.tsx), so switching never re-animates the whole page.
 */
export default function AuthSplit() {
  const { pathname } = useLocation();
  const index = Math.max(0, TABS.findIndex((t) => t.to === pathname));
  const previous = useRef(index);
  const direction = index === previous.current ? 0 : index > previous.current ? 1 : -1;
  useEffect(() => { previous.current = index; }, [index]);

  useEffect(() => {
    const before = document.title;
    document.title = `${TABS[index].title} | TradeLogX Nexus`;
    return () => { document.title = before; };
  }, [index]);

  return (
    <>
      <AppSurface />
      <main className="grid min-h-screen lg:grid-cols-2">
        <AuthShowcase />
        <div className="relative flex flex-col items-center justify-center px-5 pb-12 pt-24 sm:px-10 lg:pt-12">
          <div className="absolute inset-x-0 top-0 flex items-center justify-between px-5 py-5 sm:px-8">
            <Link to="/" aria-label="TradeLogX Nexus home" className="rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60">
              <Logo compact className="lg:hidden" />
            </Link>
            <Link
              to="/"
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg text-sm text-white/50 transition hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
            >
              <ArrowLeft className="h-4 w-4" aria-hidden="true" />
              Back to site
            </Link>
          </div>

          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.45, ease: EASE }}
            className="w-full max-w-[400px]"
          >
            <div className="mb-8 hidden lg:block">
              <Logo />
            </div>

            <nav aria-label="Account" className="mb-7 grid grid-cols-2 gap-1 rounded-xl border border-line bg-ink-700/50 p-1">
              {TABS.map((tab, i) => {
                const active = i === index;
                return (
                  <Link
                    key={tab.to}
                    to={tab.to}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "relative flex h-9 items-center justify-center rounded-lg text-[13px] font-medium transition-colors duration-200",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60",
                      active ? "text-white" : "text-white/50 hover:text-white/80",
                    )}
                  >
                    {active && (
                      <motion.span
                        layoutId="auth-tab"
                        className="absolute inset-0 rounded-lg bg-white/[0.08] ring-1 ring-inset ring-white/10"
                        transition={{ type: "spring", stiffness: 520, damping: 42 }}
                      />
                    )}
                    <span className="relative">{tab.label}</span>
                  </Link>
                );
              })}
            </nav>

            <AnimatePresence mode="wait" initial={false} custom={direction}>
              <motion.div
                key={pathname}
                custom={direction}
                variants={panel}
                initial="enter"
                animate="center"
                exit="exit"
                transition={{ duration: 0.2, ease: EASE }}
              >
                {index === 1 ? <Register /> : <Login />}
              </motion.div>
            </AnimatePresence>

            <DemoModeNotice className="mt-6" />
          </motion.div>
        </div>
      </main>
    </>
  );
}
