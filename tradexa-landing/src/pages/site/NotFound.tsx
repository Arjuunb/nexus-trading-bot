import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion } from "framer-motion";
import { ArrowLeft, Compass } from "lucide-react";
import { SiteNav } from "@/components/site/SiteNav";
import { SiteFooter } from "@/components/site/SiteFooter";
import { NotFoundBackdrop } from "@/components/site/backdrops";
import { NAV_ROUTES, ACCENT_CLASSES, prefetchRoute } from "@/site/routes";
import { usePageMeta } from "@/site/seo";
import { cn } from "@/lib/utils";

const EASE = [0.22, 1, 0.36, 1] as const;

/**
 * 404.
 *
 * Not part of SiteLayout: the pager and the accent rule belong to a page that
 * has a place in the sequence, and this one deliberately does not. It carries
 * the nav and footer directly so a visitor who arrives on a dead link is not
 * stranded on a page with no way out.
 *
 * The route list is the recovery path — someone here mistyped or followed a
 * stale link, and the useful response is the set of URLs that do exist rather
 * than an apology.
 */
export default function NotFoundPage() {
  const { pathname } = useLocation();

  usePageMeta({
    title: "Page not found",
    description:
      "That page does not exist. The TradeLogX Nexus platform pages: features, engine, live trade, selectivity, how it works and security.",
    path: pathname,
    themeColor: "#070708",
  });

  // Tell a crawler this is genuinely missing rather than a thin page. A static
  // host cannot send a 404 status for a client-side route, and `noindex` is
  // the only signal available from here — without it every typo'd URL is a
  // candidate for the index.
  useEffect(() => {
    const el = document.createElement("meta");
    el.name = "robots";
    el.content = "noindex, follow";
    document.head.appendChild(el);
    return () => el.remove();
  }, []);

  return (
    <div className="relative min-h-screen">
      <SiteNav />

      <NotFoundBackdrop />

      <main className="container-x flex min-h-[70vh] flex-col justify-center pt-32">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: EASE }}
        >
          <span className="inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.22em] text-gold/70">
            <Compass className="h-3.5 w-3.5" />
            404
          </span>

          <h1 className="mt-5 text-balance text-4xl font-extrabold tracking-tight text-white sm:text-5xl">
            There is nothing at this address
          </h1>

          <p className="mt-4 max-w-xl leading-relaxed text-white/55">
            <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[13px] text-white/70">
              {pathname}
            </code>{" "}
            does not match any page. If you followed a link from somewhere on this site, that
            is a bug worth telling us about.
          </p>

          <Link
            to="/"
            className="mt-8 inline-flex items-center gap-2 rounded-xl border border-line-strong px-4 py-2.5 text-sm text-white/80 transition hover:bg-white/[0.05] hover:text-white"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to the home page
          </Link>

          <div className="mt-14">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-white/30">
              Or go somewhere that exists
            </p>
            <ul className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {NAV_ROUTES.map((r, i) => {
                const a = ACCENT_CLASSES[r.accent];
                return (
                  <motion.li
                    key={r.path}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.4, delay: 0.1 + i * 0.05, ease: EASE }}
                  >
                    <Link
                      to={r.path}
                      onPointerEnter={() => prefetchRoute(r.path)}
                      onFocus={() => prefetchRoute(r.path)}
                      className={cn(
                        "group flex h-full items-start gap-3 rounded-xl border border-line bg-white/[0.02] p-4 transition-all duration-300 hover:-translate-y-0.5 hover:bg-white/[0.04]",
                        a.hoverBorder,
                      )}
                    >
                      <span className={cn("mt-1 h-8 w-[3px] shrink-0 rounded-full", a.bg)} />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-white">{r.label}</span>
                        <span className="block text-xs text-white/45">{r.blurb}</span>
                      </span>
                    </Link>
                  </motion.li>
                );
              })}
            </ul>
          </div>
        </motion.div>
      </main>

      <SiteFooter />
    </div>
  );
}
