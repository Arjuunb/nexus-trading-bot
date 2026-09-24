import { lazy } from "react";
import { DeferredSection } from "@/components/DeferredSection";

// Above the fold — eager. These are what a visitor sees before anything can
// scroll, so making them wait on a chunk request would defeat the point.
import { SiteNav } from "@/components/site/SiteNav";
import { LandingAmbient } from "@/components/site/backdrops";
import { Hero } from "@/components/landing/Hero";
import { EngineStatusBar } from "@/components/landing/EngineStatusBar";
import { ScrollProgress } from "@/components/motion/ScrollProgress";

// Everything below the fold is split AND deferred. Splitting alone would not
// help: a lazy component that renders immediately fetches its chunk
// immediately, so all fourteen would still be in flight at once — the same
// wait, just in more files. DeferredSection keeps each unmounted until it
// nears the viewport, so the first paint waits only on the navbar and hero.
const Features = lazy(() => import("@/components/landing/Features").then((m) => ({ default: m.Features })));
const BotThinking = lazy(() => import("@/components/landing/BotThinking").then((m) => ({ default: m.BotThinking })));
const EnginePipeline = lazy(() => import("@/components/landing/EnginePipeline").then((m) => ({ default: m.EnginePipeline })));
const ExecutionFlow = lazy(() => import("@/components/landing/ExecutionFlow").then((m) => ({ default: m.ExecutionFlow })));
const TradeInAction = lazy(() => import("@/components/landing/TradeInAction").then((m) => ({ default: m.TradeInAction })));
const MarketScanner = lazy(() => import("@/components/landing/MarketScanner").then((m) => ({ default: m.MarketScanner })));
const HowItWorks = lazy(() => import("@/components/landing/HowItWorks").then((m) => ({ default: m.HowItWorks })));
const Connectivity = lazy(() => import("@/components/landing/Connectivity").then((m) => ({ default: m.Connectivity })));
const Screenshots = lazy(() => import("@/components/landing/Screenshots").then((m) => ({ default: m.Screenshots })));
const Performance = lazy(() => import("@/components/landing/Performance").then((m) => ({ default: m.Performance })));
const RiskGuard = lazy(() => import("@/components/landing/RiskGuard").then((m) => ({ default: m.RiskGuard })));
const Security = lazy(() => import("@/components/landing/Security").then((m) => ({ default: m.Security })));
const FinalCta = lazy(() => import("@/components/landing/FinalCta").then((m) => ({ default: m.FinalCta })));
const Footer = lazy(() => import("@/components/site/SiteFooter").then((m) => ({ default: m.SiteFooter })));

export default function Landing() {
  return (
    <>
      {/* The landing page's own backdrop. It used to be rendered by the app
          for every route, which is why /engine, auth and the settings tree all
          sat on the same drifting grid. The grid itself is no longer part of
          it — it is a texture the hero and two sections opt into below. */}
      <LandingAmbient />
      <ScrollProgress />
      <SiteNav />
      <Hero />
      <div className="mt-16 sm:mt-24">
        <EngineStatusBar />
      </div>

      {/* minHeight reserves roughly what each section occupies, so the
          scrollbar does not jump as chunks arrive and #anchor links land in
          the right place. */}
      <DeferredSection minHeight={900}><Features /></DeferredSection>
      <DeferredSection minHeight={760}><BotThinking /></DeferredSection>
      <DeferredSection minHeight={720}><EnginePipeline /></DeferredSection>
      <DeferredSection minHeight={760}><ExecutionFlow /></DeferredSection>
      <DeferredSection minHeight={820}><TradeInAction /></DeferredSection>
      <DeferredSection minHeight={780}><MarketScanner /></DeferredSection>
      <DeferredSection minHeight={640}><HowItWorks /></DeferredSection>
      <DeferredSection minHeight={700}><Connectivity /></DeferredSection>
      <DeferredSection minHeight={760}><Screenshots /></DeferredSection>
      <DeferredSection minHeight={720}><Performance /></DeferredSection>
      <DeferredSection minHeight={780}><RiskGuard /></DeferredSection>
      <DeferredSection minHeight={700}><Security /></DeferredSection>
      <DeferredSection minHeight={480}><FinalCta /></DeferredSection>
      <DeferredSection minHeight={320}><Footer /></DeferredSection>
    </>
  );
}
