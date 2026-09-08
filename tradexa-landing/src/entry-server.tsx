import { MotionConfig } from "framer-motion";
import { renderToString } from "react-dom/server";
import { StaticRouter } from "react-router";
import { Link } from "react-router-dom";
import { Hero } from "@/components/landing/Hero";
import { SiteFooter } from "@/components/site/SiteFooter";
import { SiteNav } from "@/components/site/SiteNav";
import type { ComponentType } from "react";
import { PAGES } from "@/site/routes";

import ApiReference from "@/pages/site/ApiReference";
import Community from "@/pages/site/Community";
import Dashboard from "@/pages/site/Dashboard";
import Docs from "@/pages/site/Docs";
import Engine from "@/pages/site/Engine";
import Features from "@/pages/site/Features";
import GitHub from "@/pages/site/GitHub";
import HowItWorks from "@/pages/site/HowItWorks";
import LiveTrade from "@/pages/site/LiveTrade";
import OpenSource from "@/pages/site/OpenSource";
import Performance from "@/pages/site/Performance";
import Privacy from "@/pages/site/Privacy";
import RiskDisclosure from "@/pages/site/RiskDisclosure";
import Sdks from "@/pages/site/Sdks";
import Security from "@/pages/site/Security";
import Selectivity from "@/pages/site/Selectivity";
import Status from "@/pages/site/Status";
import Support from "@/pages/site/Support";
import Terms from "@/pages/site/Terms";

const BRAND = "TradeLogX Nexus";

const COMPONENTS: Record<string, ComponentType> = {
  "/api": ApiReference,
  "/community": Community,
  "/dashboard": Dashboard,
  "/docs": Docs,
  "/engine": Engine,
  "/features": Features,
  "/github": GitHub,
  "/how-it-works": HowItWorks,
  "/live-trade": LiveTrade,
  "/open-source": OpenSource,
  "/performance": Performance,
  "/privacy": Privacy,
  "/risk-disclosure": RiskDisclosure,
  "/sdks": Sdks,
  "/security": Security,
  "/selectivity": Selectivity,
  "/status": Status,
  "/support": Support,
  "/terms": Terms,
};

export interface SeoRoute {
  path: string;
  label: string;
  title: string;
  description: string;
  themeColor: string;
}

const HOME: SeoRoute = {
  path: "/",
  label: "Home",
  title: "AI Trading Intelligence Platform",
  description:
    "TradeLogX Nexus combines market analysis, strategy testing, paper trading, risk controls, execution workflows and transparent decision records.",
  themeColor: "#0a0a0c",
};

export const seoRoutes: SeoRoute[] = [
  HOME,
  ...PAGES.map(({ path, label, title, description, themeColor }) => ({
    path,
    label,
    title,
    description,
    themeColor,
  })),
];

function HomeDocument() {
  return (
    <>
      <SiteNav />
      <main id="site-main">
        <Hero />
        <section className="container-x py-20">
          <h2 className="text-3xl font-bold text-white">Explore the complete trading workflow</h2>
          <p className="mt-4 max-w-3xl text-white/60">
            Follow market data from analysis through strategy qualification, mandatory risk checks,
            paper execution and the journal. Every product page explains one part of that workflow,
            including the controls that can reject a trade before capital is exposed.
          </p>
          <nav aria-label="Explore TradeLogX Nexus" className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {PAGES.map((page) => (
              <Link key={page.path} to={page.path} className="rounded-xl border border-line p-4">
                <strong className="block text-white">{page.label}</strong>
                <span className="mt-1 block text-sm text-white/50">{page.blurb}</span>
              </Link>
            ))}
          </nav>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}

function PublicDocument({ path }: { path: string }) {
  if (path === "/") return <HomeDocument />;
  const Page = COMPONENTS[path];
  if (!Page) throw new Error(`No pre-render component registered for ${path}`);
  return (
    <>
      <SiteNav />
      <main id="site-main">
        <Page />
      </main>
      <SiteFooter />
    </>
  );
}

export function render(path: string): string {
  return renderToString(
    <MotionConfig reducedMotion="always">
      <StaticRouter location={path}>
        <PublicDocument path={path} />
      </StaticRouter>
    </MotionConfig>,
  );
}

export function fullTitle(route: SeoRoute): string {
  return route.path === "/" ? `${BRAND} | ${route.title}` : `${route.title} | ${BRAND}`;
}
