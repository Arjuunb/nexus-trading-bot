import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { MotionConfig } from "framer-motion";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { ToastProvider } from "@/lib/toast";
import { Skeleton } from "@/components/ui/Skeleton";
import { SettingsProvider, useApplyAppearance } from "@/settings/store";
import { PAGES } from "@/site/routes";
import { ScrollManager } from "@/site/ScrollManager";
import { auth } from "@/lib/auth";
import { supabase } from "@/lib/supabase";

// Landing renders eagerly (it's the entry point); auth + settings are code-split
// so the marketing page ships the smallest possible bundle.
import Landing from "@/pages/Landing";

// The dedicated product pages. Each is a self-contained document with its own
// palette, illustrations and interactions, so they are split individually —
// visiting /engine should not download the trading terminal. The module for
// each comes from the route table, which is also what the hover prefetch
// reads, so the two cannot drift apart.
const SiteLayout = lazy(() => import("@/components/site/SiteLayout"));
const NotFound = lazy(() => import("@/pages/site/NotFound"));
const SITE_ELEMENTS = PAGES.map((r) => ({ path: r.path, Component: lazy(r.load) }));
// Sign in and Create account are one mounted layout (see AuthSplit).
const AuthSplit = lazy(() => import("@/pages/auth/AuthSplit"));
const ForgotPassword = lazy(() => import("@/pages/auth/ForgotPassword"));
const ResetPassword = lazy(() => import("@/pages/auth/ResetPassword"));
const VerifyEmail = lazy(() => import("@/pages/auth/VerifyEmail"));
const TwoFactor = lazy(() => import("@/pages/auth/TwoFactor"));
const SessionExpired = lazy(() => import("@/pages/auth/SessionExpired"));
const Admin = lazy(() => import("@/pages/Admin"));

const SettingsLayout = lazy(() => import("@/components/settings/SettingsLayout"));
const SettingsOverview = lazy(() => import("@/pages/settings/Overview"));
const Profile = lazy(() => import("@/pages/settings/Profile"));
const Account = lazy(() => import("@/pages/settings/Account"));
const Security = lazy(() => import("@/pages/settings/Security"));
const Notifications = lazy(() => import("@/pages/settings/Notifications"));
const Trading = lazy(() => import("@/pages/settings/Trading"));
const Exchanges = lazy(() => import("@/pages/settings/Exchanges"));
const Strategies = lazy(() => import("@/pages/settings/Strategies"));
const Risk = lazy(() => import("@/pages/settings/Risk"));
const AI = lazy(() => import("@/pages/settings/AI"));
const Automation = lazy(() => import("@/pages/settings/Automation"));
const Scheduler = lazy(() => import("@/pages/settings/Scheduler"));
const Portfolio = lazy(() => import("@/pages/settings/Portfolio"));
const ApiKeys = lazy(() => import("@/pages/settings/ApiKeys"));
const Integrations = lazy(() => import("@/pages/settings/Integrations"));
const Team = lazy(() => import("@/pages/settings/Team"));
const Billing = lazy(() => import("@/pages/settings/Billing"));
const Usage = lazy(() => import("@/pages/settings/Usage"));
const Logs = lazy(() => import("@/pages/settings/Logs"));
const Audit = lazy(() => import("@/pages/settings/Audit"));
const Backup = lazy(() => import("@/pages/settings/Backup"));
const Appearance = lazy(() => import("@/pages/settings/Appearance"));
const Region = lazy(() => import("@/pages/settings/Region"));
const Privacy = lazy(() => import("@/pages/settings/Privacy"));
const Advanced = lazy(() => import("@/pages/settings/Advanced"));
const Danger = lazy(() => import("@/pages/settings/Danger"));

/**
 * Route-level loading state.
 *
 * This used to be a small centred stack of skeleton bars — which, on an
 * otherwise empty screen, read as a floating card rather than a page loading,
 * and was the first thing a visitor saw. It now sketches the shape of what is
 * arriving: a header bar, a headline block, and a wide panel. Same purpose,
 * but it reads as "this page is filling in" instead of "this is the page".
 */
function Fallback() {
  return (
    <div className="min-h-screen" aria-busy="true" aria-label="Loading">
      <div className="mx-auto w-full max-w-6xl px-6 pt-6">
        <div className="flex items-center justify-between">
          <Skeleton className="h-8 w-36" />
          <Skeleton className="h-8 w-24" />
        </div>
        <div className="mt-24 space-y-4">
          <Skeleton className="h-12 w-3/4 max-w-2xl" />
          <Skeleton className="h-12 w-1/2 max-w-xl" />
          <Skeleton className="h-5 w-2/3 max-w-lg" />
        </div>
        <Skeleton className="mt-12 h-64 w-full rounded-2xl" />
      </div>
    </div>
  );
}

function AppearanceApplier() {
  useApplyAppearance();
  return null;
}

/** Keeps the backend's HttpOnly dashboard cookie synchronized with Supabase's
 * refreshed browser session. Supabase remains the credential authority. */
function SessionBridge() {
  useEffect(() => {
    void auth.bridgeCurrentSession();
    const subscription = supabase?.auth.onAuthStateChange((_event, session) => {
      if (session) void auth.bridgeCurrentSession();
    }).data.subscription;
    return () => subscription?.unsubscribe();
  }, []);
  return null;
}

function Protected({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(!auth.configured);
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => {
    if (!supabase) return;
    void supabase.auth.getUser().then(({ data }) => {
      setSignedIn(Boolean(data.user?.email_confirmed_at));
      setReady(true);
    });
  }, []);
  if (!ready) return <Fallback />;
  return signedIn ? <>{children}</> : <Navigate to="/auth/login" replace />;
}

export default function App() {
  return (
    // reducedMotion="user" makes EVERY framer-motion animation on the site
    // honour the OS "reduce motion" setting: transforms are dropped, opacity
    // still crossfades. Seven components handled this individually and
    // twenty-three did not, which meant someone with the preference set got a
    // page that was mostly still and occasionally lurched. One switch at the
    // root is the only way this stays true as components are added.
    <MotionConfig reducedMotion="user">
    <BrowserRouter>
      <SettingsProvider>
        <ToastProvider>
          <AppearanceApplier />
          <SessionBridge />
          <ScrollManager />
          <Suspense fallback={<Fallback />}>
            <Routes>
              <Route path="/" element={<Landing />} />

              {/* Dedicated product pages share one layout: navigation, the
                  cross-page pager, the footer and the page transition. */}
              <Route element={<SiteLayout />}>
                {SITE_ELEMENTS.map(({ path, Component }) => (
                  <Route key={path} path={path} element={<Component />} />
                ))}
              </Route>

              <Route element={<AuthSplit />}>
                <Route path="/auth/login" element={null} />
                <Route path="/auth/register" element={null} />
              </Route>
              <Route path="/auth/forgot-password" element={<ForgotPassword />} />
              <Route path="/auth/reset-password" element={<ResetPassword />} />
              <Route path="/auth/verify-email" element={<VerifyEmail />} />
              <Route path="/auth/two-factor" element={<TwoFactor />} />
              <Route path="/auth/session-expired" element={<SessionExpired />} />
              <Route path="/admin" element={<Protected><Admin /></Protected>} />

              <Route path="/settings" element={<Protected><SettingsLayout /></Protected>}>
                <Route index element={<Navigate to="/settings/overview" replace />} />
                <Route path="overview" element={<SettingsOverview />} />
                <Route path="profile" element={<Profile />} />
                <Route path="account" element={<Account />} />
                <Route path="security" element={<Security />} />
                <Route path="notifications" element={<Notifications />} />
                <Route path="trading" element={<Trading />} />
                <Route path="exchanges" element={<Exchanges />} />
                <Route path="strategies" element={<Strategies />} />
                <Route path="risk" element={<Risk />} />
                <Route path="ai" element={<AI />} />
                <Route path="automation" element={<Automation />} />
                <Route path="scheduler" element={<Scheduler />} />
                <Route path="portfolio" element={<Portfolio />} />
                <Route path="api-keys" element={<ApiKeys />} />
                <Route path="integrations" element={<Integrations />} />
                <Route path="team" element={<Team />} />
                <Route path="billing" element={<Billing />} />
                <Route path="usage" element={<Usage />} />
                <Route path="logs" element={<Logs />} />
                <Route path="audit" element={<Audit />} />
                <Route path="backup" element={<Backup />} />
                <Route path="appearance" element={<Appearance />} />
                <Route path="region" element={<Region />} />
                <Route path="privacy" element={<Privacy />} />
                <Route path="advanced" element={<Advanced />} />
                <Route path="danger" element={<Danger />} />
              </Route>

              {/* A mistyped URL used to land silently on the home page, which
                  reads to a visitor as "the link worked, this is just the
                  wrong content" and to a crawler as a soft 404 — every dead
                  URL indexing as a duplicate of "/". It now says what
                  happened and offers the routes that do exist. */}
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </ToastProvider>
      </SettingsProvider>
    </BrowserRouter>
    </MotionConfig>
  );
}
