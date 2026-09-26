import { useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { ArrowUpRight, Check, ChevronDown, ShieldCheck } from "lucide-react";
import { Logo } from "@/components/Logo";
import { FOOTER_GROUPS, ACCENT_CLASSES, routeFor, prefetchRoute } from "@/site/routes";
import { PLATFORM_STATS, TRUST_BADGES, VENUES } from "@/site/platform";
import { APP_URL, LOGIN_URL, cn } from "@/lib/utils";

/**
 * The footer, as the site's second navigation surface.
 *
 * It used to be three columns of hash fragments — "#docs", "#api", "#terms" —
 * which resolved to nothing from any page except the home page, and to nothing
 * at all even there because no such sections existed. Eleven of its links went
 * nowhere. Every entry is now a real route, generated from the page table, so
 * the copy cannot drift from the pages and a new page cannot be forgotten here.
 *
 * Visually it is deliberately not the landing page. The site's ambient
 * backdrop is a warm charcoal under a drifting grid; this is flat matte black
 * with a single gold hairline across the top — the seam between the page and
 * the hub underneath it. Nothing here glows, drifts or blooms. It reads as
 * infrastructure, which is what a footer of this size has to be to stay
 * navigable rather than decorative.
 */

const EASE = [0.22, 1, 0.36, 1] as const;

/* ── Statistics band ──────────────────────────────────────────────────── */

function StatsBand() {
  return (
    <div className="border-b border-white/[0.06]">
      <div className="container-x grid grid-cols-2 divide-x divide-white/[0.06] lg:grid-cols-4">
        {PLATFORM_STATS.map((s, i) => (
          <motion.div
            key={s.label}
            initial={{ opacity: 0, y: 10 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-40px" }}
            transition={{ duration: 0.45, delay: i * 0.06, ease: EASE }}
            className={cn(
              "group px-5 py-6 first:pl-0 sm:px-7",
              // On two columns the third and fourth items start a new row, so
              // their left divider would hang in the middle of the grid.
              i === 2 && "border-l-0 lg:border-l",
            )}
          >
            <p className="font-mono text-2xl font-semibold tabular tracking-tight text-white transition-colors duration-300 group-hover:text-gold-soft sm:text-[28px]">
              {s.value}
            </p>
            <p className="mt-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-white/45">
              {s.label}
            </p>
            <p className="mt-0.5 font-mono text-[10px] text-white/25">{s.note}</p>
          </motion.div>
        ))}
      </div>
    </div>
  );
}

/* ── Link groups ──────────────────────────────────────────────────────── */

/**
 * One navigation group.
 *
 * Collapsible below `sm` and always open from there up. Seven Company links
 * plus five Developers links plus four Product links is fifty lines of footer
 * on a phone — long enough that the copyright is genuinely hard to reach —
 * and an accordion is the honest fix rather than hiding half the site.
 */
function LinkGroup({ title, paths }: { title: string; paths: string[] }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="border-b border-white/[0.06] py-4 sm:border-0 sm:py-0">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center justify-between text-left sm:pointer-events-none"
      >
        <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-white/45">
          {title}
        </span>
        <ChevronDown
          className={cn(
            "h-4 w-4 text-white/30 transition-transform duration-300 sm:hidden",
            open && "rotate-180",
          )}
        />
      </button>

      <div
        className={cn(
          "grid transition-[grid-template-rows] duration-300 ease-out sm:!grid-rows-[1fr] motion-reduce:transition-none",
        )}
        style={{ gridTemplateRows: open ? "1fr" : "0fr" }}
      >
        <div className="overflow-hidden">
          <ul className="mt-4 space-y-1">
            {paths.map((path) => {
              const page = routeFor(path);
              if (!page) return null;
              const accent = ACCENT_CLASSES[page.accent];
              return (
                <li key={path}>
                  <Link
                    to={path}
                    onPointerEnter={() => prefetchRoute(path)}
                    onFocus={() => prefetchRoute(path)}
                    className="group -mx-2 flex items-baseline gap-2.5 rounded-lg px-2 py-1.5 transition-colors duration-200 hover:bg-white/[0.04]"
                  >
                    {/* the accent dot grows into a dash on hover — a small,
                        cheap signal that the row is live */}
                    <span
                      className={cn(
                        "mt-[7px] h-[3px] w-[3px] shrink-0 rounded-full opacity-40 transition-all duration-300 group-hover:w-3 group-hover:opacity-100",
                        accent.bg,
                      )}
                    />
                    <span className="min-w-0">
                      <span className="block text-[13.5px] text-white/65 transition-colors duration-200 group-hover:text-white">
                        {page.label}
                      </span>
                      <span className="block text-[11px] text-white/25 transition-colors duration-200 group-hover:text-white/40">
                        {page.blurb}
                      </span>
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </div>
  );
}

/* ── Footer ───────────────────────────────────────────────────────────── */

export function SiteFooter() {
  // Rendering the year at module scope would freeze it at build time — a
  // static site deployed in December still says the old year in March.
  const [year] = useState(() => new Date().getFullYear());

  return (
    <footer className="relative isolate overflow-hidden bg-[#040404] text-white">
      {/*
        The footer's own surface, and nothing borrowed from the page above it.
        A near-black base with one very low warm lift at the top edge, so the
        block reads as a solid object the page ends against rather than as more
        page. Explicitly no grid: this used to sit on the application-wide grid
        and looked like the landing page continuing past the content.
      */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 -z-10 bg-[linear-gradient(to_bottom,rgba(234,181,79,0.045),rgba(0,0,0,0)_22rem)]"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 bottom-0 -z-10 h-64 bg-[linear-gradient(to_top,rgba(0,0,0,0.65),transparent)]"
      />

      {/* the seam: one gold hairline, brightest in the middle */}
      <div
        aria-hidden
        className="h-px w-full bg-gradient-to-r from-transparent via-gold/70 to-transparent"
      />
      <div
        aria-hidden
        className="h-px w-full bg-gradient-to-r from-transparent via-gold/15 to-transparent blur-[2px]"
      />

      <StatsBand />

      {/* main navigation */}
      <div className="container-x grid gap-10 py-14 lg:grid-cols-[1.15fr_2.4fr] lg:gap-16">
        <div>
          <Link to="/" aria-label="TradeLogX Nexus home" className="inline-block">
            <Logo />
          </Link>
          <p className="mt-4 max-w-xs text-sm leading-relaxed text-white/45">
            It records every trade and every decision, trades smaller on losing patterns it
            has seen repeat, and shows you the reasoning behind all of it.
          </p>

          <div className="mt-6 flex flex-wrap gap-2">
            <a href={APP_URL} className="group">
              <span className="inline-flex items-center gap-1.5 rounded-lg border border-gold/35 bg-gold/[0.08] px-3.5 py-2 text-[13px] font-medium text-gold-soft transition-all duration-200 hover:border-gold/60 hover:bg-gold/[0.14]">
                Launch Platform
                <ArrowUpRight className="h-3.5 w-3.5 transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
              </span>
            </a>
            <a href={LOGIN_URL}>
              <span className="inline-flex items-center rounded-lg border border-white/10 px-3.5 py-2 text-[13px] text-white/65 transition-colors duration-200 hover:border-white/20 hover:text-white">
                Sign in
              </span>
            </a>
          </div>

          {/* venues */}
          <div className="mt-9">
            <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-white/35">
              Exchanges
            </p>
            <p className="mt-1.5 text-[11px] text-white/30">Live market data · paper execution everywhere</p>
            <ul className="mt-3 flex flex-wrap gap-1.5">
              {VENUES.map((v) => (
                <li key={v.name}>
                  <span
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 font-mono text-[11px] transition-colors duration-200",
                      v.live
                        ? "border-white/10 text-white/60 hover:border-gold/40 hover:text-gold-soft"
                        : "border-white/[0.06] text-white/25",
                    )}
                  >
                    <span
                      className={cn(
                        "h-1.5 w-1.5 rounded-full",
                        v.live ? "bg-emerald" : "bg-white/20",
                      )}
                    />
                    {v.name}
                    {!v.live && <span className="text-white/20">roadmap</span>}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* the three groups */}
        <nav aria-label="Footer" className="grid gap-x-8 sm:grid-cols-3">
          {FOOTER_GROUPS.map((g) => (
            <LinkGroup key={g.title} title={g.title} paths={g.paths} />
          ))}
        </nav>
      </div>

      {/* trust badges */}
      <div className="border-t border-white/[0.06]">
        <div className="container-x py-6">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.16em] text-white/35">
              <ShieldCheck className="h-3.5 w-3.5 text-gold" />
              Built in
            </span>
            {TRUST_BADGES.map((b) => (
              <span
                key={b.label}
                title={b.detail}
                className="group inline-flex items-center gap-1.5 rounded-md border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 text-[11px] text-white/50 transition-colors duration-200 hover:border-gold/30 hover:text-white/80"
              >
                <Check className="h-3 w-3 text-gold/70" />
                {b.label}
              </span>
            ))}
            <Link
              to="/security"
              onPointerEnter={() => prefetchRoute("/security")}
              className="text-[11px] text-white/35 underline-offset-4 transition-colors hover:text-gold-soft hover:underline"
            >
              How each of these works →
            </Link>
          </div>
        </div>
      </div>

      {/* bottom bar */}
      <div className="border-t border-white/[0.06]">
        <div className="container-x flex flex-col gap-4 py-6 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs text-white/35">
            © {year} TradeLogX Nexus. All rights reserved.
          </p>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
            {["/privacy", "/terms", "/risk-disclosure", "/status"].map((path) => {
              const page = routeFor(path);
              if (!page) return null;
              return (
                <Link
                  key={path}
                  to={path}
                  onPointerEnter={() => prefetchRoute(path)}
                  className="text-white/35 transition-colors duration-200 hover:text-white"
                >
                  {page.label}
                </Link>
              );
            })}
          </div>
        </div>
      </div>

      <p className="container-x pb-6 text-[10px] leading-relaxed text-white/20">
        Trading carries risk of loss. Figures shown across this site are illustrative unless
        stated otherwise. Nothing here is financial advice —{" "}
        <Link to="/risk-disclosure" className="underline underline-offset-2 hover:text-white/40">
          read the risk disclosure
        </Link>
        .
      </p>
    </footer>
  );
}
