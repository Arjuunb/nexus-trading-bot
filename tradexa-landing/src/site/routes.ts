/**
 * Every public page on the site, in one table.
 *
 * Navigation used to be hash links into one very long landing page, so
 * "Engine" and "Security" were positions in a scroll rather than places you
 * could link to, bookmark, or have indexed separately — and the footer's
 * "Documentation", "API" and "Terms" were fragments that resolved to nothing
 * at all once you had navigated away from the home page.
 *
 * Every entry below is a real route with its own document. This table is the
 * single source the navbar, the footer, the page-transition accent, the
 * prefetcher and the SEO metadata all read from, so adding a page cannot
 * leave one of them behind.
 *
 * `PRIMARY` are the six product pages carried in the top navigation and the
 * cross-page pager. `SECONDARY` are everything the footer reaches: product
 * detail, the developer portal, support and the legal documents. Both are
 * pages in exactly the same sense — the split is only about which chrome
 * advertises them.
 */

import type { ComponentType } from "react";

export type Accent = "gold" | "electric" | "terminal" | "aurum" | "spectrum" | "emerald";

export interface SitePage {
  path: string;
  /** Navbar label. */
  label: string;
  /** <title> for the route (the brand suffix is appended once, centrally). */
  title: string;
  /** <meta name="description"> — also the og/twitter description. */
  description: string;
  /** Which palette the page owns; drives the nav underline + transition tint. */
  accent: Accent;
  /** One-line summary used by the footer's product column. */
  blurb: string;
  /**
   * The page's opaque base colour, mirrored into `<meta name="theme-color">`.
   *
   * Mobile Safari and Chrome paint their own chrome with this, so without it
   * every page kept the landing page's near-black while its own background was
   * navy or graphite — a visible seam right at the top of the screen.
   */
  themeColor: string;
  /**
   * The page module.
   *
   * Declared once here so `React.lazy` and the hover prefetch cannot disagree
   * about which chunk a route needs. A dynamic `import()` inside a function
   * body is not evaluated until called, so listing them here does not pull six
   * pages into the entry bundle.
   */
  load: () => Promise<{ default: ComponentType }>;
}

const PRIMARY: SitePage[] = [
  {
    path: "/features",
    label: "Features",
    title: "Features — what the platform runs today",
    description:
      "Explore what TradeLogX Nexus runs today: the decision engine, risk checks, backtesting and validation, paper execution, the trade journal and alerts.",
    accent: "gold",
    blurb: "Every built capability, searchable",
    themeColor: "#070708",
    load: () => import("@/pages/site/Features"),
  },
  {
    path: "/engine",
    label: "Engine",
    title: "Nexus Engine — one path from candle to fill",
    description:
      "See how the Nexus Engine turns closed Binance candles into a scored, risk-checked decision and a paper fill, with every step written to the record.",
    accent: "electric",
    blurb: "The decision path, stage by stage",
    themeColor: "#0B0B0D",
    load: () => import("@/pages/site/Engine"),
  },
  {
    path: "/live-trade",
    label: "Live trade",
    title: "Live trade — the paper trading terminal",
    description:
      "Explore the TradeLogX Nexus terminal: live Binance candles, open positions, scored decisions and a timestamped record of every paper fill.",
    accent: "terminal",
    blurb: "Chart, positions and paper fills",
    themeColor: "#070708",
    load: () => import("@/pages/site/LiveTrade"),
  },
  {
    path: "/selectivity",
    label: "Selectivity",
    title: "Selectivity — a score before a trade",
    description:
      "See how TradeLogX Nexus rejects weak trades: an eight-factor quality score, a minimum of 60, hard blocks and risk checks, each with its reason recorded.",
    accent: "aurum",
    blurb: "Why most setups are rejected",
    themeColor: "#040404",
    load: () => import("@/pages/site/Selectivity"),
  },
  {
    path: "/how-it-works",
    label: "How it works",
    title: "How it works — exchange to analytics",
    description:
      "Follow a TradeLogX Nexus trade through market data, analysis, a scored decision, risk checks, paper execution, the journal and analytics.",
    accent: "spectrum",
    blurb: "The seven-stage journey",
    themeColor: "#060607",
    load: () => import("@/pages/site/HowItWorks"),
  },
  {
    path: "/security",
    label: "Security",
    title: "Security — checked, not promised",
    description:
      "TradeLogX Nexus security: encrypted exchange keys, withdrawal scopes refused, a hash-chained audit log, encrypted backups and how to report a vulnerability.",
    accent: "emerald",
    blurb: "Keys, scopes and audit trails",
    themeColor: "#070708",
    load: () => import("@/pages/site/Security"),
  },
];


/**
 * Everything the footer reaches.
 *
 * These are not "extra" pages: a reader who lands on /api or /risk-disclosure
 * from a search result should get a document that stands on its own, with its
 * own metadata and its own reason to exist. What separates them from PRIMARY
 * is only that the top navigation stays at six items — past that a nav bar
 * stops being navigation and becomes a directory.
 */
const SECONDARY: SitePage[] = [
  // ── Product ──────────────────────────────────────────────────────────
  {
    path: "/performance",
    label: "Performance",
    title: "Performance — what is verified, and what is not",
    description:
      "TradeLogX Nexus publishes no performance figure it cannot back. See the recorded strategy validation and the method a result must pass to appear here.",
    accent: "emerald",
    blurb: "Recorded validation and method",
    themeColor: "#070708",
    load: () => import("@/pages/site/Performance"),
  },
  {
    path: "/dashboard",
    label: "Dashboard",
    title: "Dashboard — the workspace you actually use",
    description:
      "A tour of the TradeLogX Nexus dashboard: the overview, open positions, the trading journal, the Strategy Lab and the risk console, panel by panel.",
    accent: "spectrum",
    blurb: "Every panel, toured",
    themeColor: "#070708",
    load: () => import("@/pages/site/Dashboard"),
  },

  // ── Developers ───────────────────────────────────────────────────────
  {
    path: "/docs",
    label: "Documentation",
    title: "Documentation — quickstart, concepts and guides",
    description:
      "Use TradeLogX Nexus documentation to install, connect an exchange, run backtests, configure paper trading and understand core platform concepts.",
    accent: "electric",
    blurb: "Quickstart, concepts, guides",
    themeColor: "#070708",
    load: () => import("@/pages/site/Docs"),
  },
  {
    path: "/api",
    label: "API reference",
    title: "API reference — endpoints, auth and webhooks",
    description:
      "Explore the TradeLogX Nexus API for authentication, strategies, positions, decisions, backtests, webhooks, rate limits and structured errors.",
    accent: "electric",
    blurb: "Endpoints, auth, webhooks",
    themeColor: "#070708",
    load: () => import("@/pages/site/ApiReference"),
  },
  {
    path: "/sdks",
    label: "SDKs",
    title: "SDKs — Python, TypeScript, Go and Rust",
    description:
      "TradeLogX Nexus clients in Python, TypeScript, Go and Rust: install from the repository, a first request in each, and an honest feature-parity table.",
    accent: "electric",
    blurb: "Four clients, one API",
    themeColor: "#070708",
    load: () => import("@/pages/site/Sdks"),
  },
  {
    path: "/open-source",
    label: "Open source",
    title: "Open source — the whole platform, MIT-licensed",
    description:
      "The whole TradeLogX Nexus platform is in one public MIT-licensed repository: risk gates, backtests, the API, the SDKs and the deployment, and how to contribute.",
    accent: "electric",
    blurb: "All of it, MIT-licensed",
    themeColor: "#070708",
    load: () => import("@/pages/site/OpenSource"),
  },
  {
    path: "/github",
    label: "GitHub",
    title: "GitHub — the repository, issues and pull requests",
    description:
      "The TradeLogX Nexus repository: what lives where, how changes ship, how to file an issue that gets fixed, and what a good pull request looks like.",
    accent: "electric",
    blurb: "The repository and how to contribute",
    themeColor: "#070708",
    load: () => import("@/pages/site/GitHub"),
  },

  // ── Company ──────────────────────────────────────────────────────────
  {
    path: "/support",
    label: "Support center",
    title: "Support center — answers, and where to take the rest",
    description:
      "TradeLogX Nexus answers for exchange keys, stale data, risk limits, exports and two-factor, and where to take anything else: issues, security reports, status.",
    accent: "gold",
    blurb: "Answers, and where to go next",
    themeColor: "#070708",
    load: () => import("@/pages/site/Support"),
  },
  {
    path: "/community",
    label: "Community",
    title: "Community — where traders and builders talk",
    description:
      "The TradeLogX Nexus community on the public repository: issues, proposals argued before they are built, the code of conduct, and what is still planned.",
    accent: "gold",
    blurb: "Issues, proposals, conduct",
    themeColor: "#070708",
    load: () => import("@/pages/site/Community"),
  },
  {
    path: "/status",
    label: "Status",
    title: "Status — every service, and its recent history",
    description:
      "Live TradeLogX Nexus service status, checked every minute: API, trading workers, market data and database, with measured uptime and incident history.",
    accent: "terminal",
    blurb: "Live service health",
    themeColor: "#060607",
    load: () => import("@/pages/site/Status"),
  },

  // ── Legal ────────────────────────────────────────────────────────────
  {
    path: "/privacy",
    label: "Privacy policy",
    title: "Privacy policy",
    description:
      "What TradeLogX Nexus collects, why, how long it is kept, who it is shared with, and the rights you have over it — written to be read rather than survived.",
    accent: "aurum",
    blurb: "What we collect, and why",
    themeColor: "#0A0908",
    load: () => import("@/pages/site/Privacy"),
  },
  {
    path: "/terms",
    label: "Terms of service",
    title: "Terms of service",
    description:
      "The terms governing use of TradeLogX Nexus: what the service does and does not do, acceptable use, availability, liability, and how the agreement ends.",
    accent: "aurum",
    blurb: "The agreement, in plain terms",
    themeColor: "#0A0908",
    load: () => import("@/pages/site/Terms"),
  },
  {
    path: "/risk-disclosure",
    label: "Risk disclosure",
    title: "Risk disclosure",
    description:
      "Understand trading loss risk, what automation can and cannot change, automated-system failure modes and the limits of TradeLogX Nexus.",
    accent: "aurum",
    blurb: "What can go wrong, stated plainly",
    themeColor: "#0A0908",
    load: () => import("@/pages/site/RiskDisclosure"),
  },
];

/** Every page, primary and secondary. */
export const PAGES: SitePage[] = [...PRIMARY, ...SECONDARY];

/** The six pages carried in the top navigation and the cross-page pager. */
export const NAV_ROUTES: SitePage[] = PRIMARY;

/**
 * The footer's navigation groups.
 *
 * Referenced by path rather than duplicated, so a title or blurb is written
 * once. The footer used to hard-code its own list of labels pointing at
 * fragments like "#docs" — which meant the copy could drift from the pages and,
 * worse, that six of the eleven links did nothing at all from any page other
 * than the home page.
 */
export interface FooterGroup {
  title: string;
  paths: string[];
}

export const FOOTER_GROUPS: FooterGroup[] = [
  { title: "Product", paths: ["/features", "/how-it-works", "/performance", "/dashboard"] },
  { title: "Developers", paths: ["/docs", "/api", "/sdks", "/open-source", "/github"] },
  {
    title: "Company",
    paths: ["/support", "/community", "/security", "/privacy", "/terms", "/risk-disclosure", "/status"],
  },
];

/** Route paths that own their own chrome + backdrop (i.e. use SiteLayout). */
export const SITE_PATHS: ReadonlySet<string> = new Set(PAGES.map((r) => r.path));

export function isSitePath(pathname: string): boolean {
  return SITE_PATHS.has(pathname.replace(/\/+$/, "") || "/");
}

export function routeFor(pathname: string): SitePage | undefined {
  const clean = pathname.replace(/\/+$/, "") || "/";
  return PAGES.find((r) => r.path === clean);
}

/**
 * Start fetching a route's chunk before it is asked for.
 *
 * Every page is split, so clicking a nav item means a network round trip
 * before anything can render — on a slow connection that is the transition
 * playing over a skeleton. Pointing at a link is a reliable signal that a
 * click is coming and buys a few hundred milliseconds of head start.
 *
 * Fired on pointer-enter *and* focus, so keyboard users get the same benefit,
 * and tracked so repeated hovers over the same link do not queue work.
 */
const prefetched = new Set<string>();

export function prefetchRoute(path: string) {
  if (prefetched.has(path)) return;
  const route = routeFor(path);
  if (!route) return;
  prefetched.add(path);
  // A failed prefetch is not an error worth surfacing: the click that follows
  // will request the same chunk again through the normal Suspense path.
  void route.load().catch(() => prefetched.delete(path));
}

/**
 * Accent → the handful of classes the shared chrome needs to tint itself.
 * Written out in full because Tailwind scans source text: `text-${accent}`
 * would compile to nothing.
 */
export interface AccentClasses {
  text: string;
  bg: string;
  border: string;
  /** Written out in full — Tailwind cannot see `hover:${...}`. */
  hoverBorder: string;
  glow: string;
}

export const ACCENT_CLASSES: Record<Accent, AccentClasses> = {
  gold: {
    text: "text-gold",
    bg: "bg-gold",
    border: "border-gold/30",
    hoverBorder: "hover:border-gold/40",
    glow: "shadow-[0_0_24px_-6px_rgba(234,181,79,0.65)]",
  },
  electric: {
    text: "text-electric-soft",
    bg: "bg-electric",
    border: "border-electric/40",
    hoverBorder: "hover:border-electric/50",
    glow: "shadow-[0_0_24px_-6px_rgba(234,181,79,0.7)]",
  },
  terminal: {
    text: "text-emerald-soft",
    bg: "bg-emerald",
    border: "border-emerald/40",
    hoverBorder: "hover:border-emerald/50",
    glow: "shadow-[0_0_24px_-6px_rgba(34,197,94,0.7)]",
  },
  aurum: {
    text: "text-gold-soft",
    bg: "bg-gold-soft",
    border: "border-gold/40",
    hoverBorder: "hover:border-gold-soft/50",
    glow: "shadow-[0_0_24px_-6px_rgba(242,199,102,0.7)]",
  },
  spectrum: {
    text: "text-aqua-soft",
    bg: "bg-aqua",
    border: "border-aqua/40",
    hoverBorder: "hover:border-aqua/50",
    glow: "shadow-[0_0_24px_-6px_rgba(34,211,238,0.7)]",
  },
  emerald: {
    text: "text-emerald-soft",
    bg: "bg-emerald",
    border: "border-emerald/40",
    hoverBorder: "hover:border-emerald/50",
    glow: "shadow-[0_0_24px_-6px_rgba(34,197,94,0.7)]",
  },
};
