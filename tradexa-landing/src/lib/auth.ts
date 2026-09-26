import { supabase, isSupabaseConfigured } from "./supabase";
import { APP_URL } from "./utils";

export type OAuthProvider = "google" | "apple" | "github" | "azure" | "discord";

export interface AuthResult {
  ok: boolean;
  demo: false;
  message: string;
  /** Machine-readable reason, so a form can offer the right next step. */
  code?: "email_not_verified";
}

const FAIL = (message: string, code?: AuthResult["code"]): AuthResult => ({ ok: false, demo: false, message, code });
const OK = (message: string): AuthResult => ({ ok: true, demo: false, message });
const redirectTo = typeof window !== "undefined" ? `${window.location.origin}/auth/reset-password` : undefined;
const verificationRedirect = typeof window !== "undefined" ? `${window.location.origin}/auth/verify-email` : undefined;
const appUrl = typeof window !== "undefined"
  ? (APP_URL.startsWith("http") ? APP_URL : `${window.location.origin}${APP_URL}`)
  : undefined;
const REMEMBER_KEY = "tradexalx.auth.remember";

export interface SignUpInput {
  firstName: string;
  lastName: string;
  email: string;
  password: string;
  country: string;
}

function configuredError(): AuthResult {
  return FAIL("Authentication is not configured. Contact the TradeLogX administrator.");
}

function rememberPreference(): boolean {
  return typeof window === "undefined" || window.localStorage.getItem(REMEMBER_KEY) !== "false";
}

async function bridge(accessToken: string, remember = rememberPreference()): Promise<AuthResult> {
  const response = await fetch("/auth/supabase/session", {
    method: "POST",
    credentials: "include",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ remember }),
  });
  if (response.ok) return OK("Signed in.");
  const body = await response.json().catch(() => null) as { detail?: string } | null;
  return FAIL(body?.detail ?? "Unable to establish a secure session.");
}

export const auth = {
  configured: isSupabaseConfigured,

  providerEnabled(provider: OAuthProvider): boolean {
    const configuredProviders = typeof window !== "undefined" ? window.__HUB_CONFIG__?.oauthProviders : undefined;
    if (configuredProviders) return configuredProviders.includes(provider);
    const cfg = import.meta.env;
    const key = `VITE_AUTH_${provider.toUpperCase()}_ENABLED` as keyof typeof cfg;
    return cfg[key] === "true" || cfg[key] === "1";
  },

  async signIn(email: string, password: string, remember: boolean): Promise<AuthResult> {
    if (!supabase) return configuredError();
    window.localStorage.setItem(REMEMBER_KEY, String(remember));
    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    const unverified = "Verify your email before opening the dashboard. We can send another verification email.";
    if (error) {
      // Supabase refuses an unconfirmed address outright when confirmation is required.
      const notConfirmed = (error as { code?: string }).code === "email_not_confirmed" || /email not confirmed/i.test(error.message);
      return notConfirmed ? FAIL(unverified, "email_not_verified") : FAIL(error.message);
    }
    if (!data.user?.email_confirmed_at) {
      await supabase.auth.signOut({ scope: "local" });
      return FAIL(unverified, "email_not_verified");
    }
    return bridge(data.session?.access_token ?? "", remember);
  },

  async signUp(input: SignUpInput): Promise<AuthResult> {
    if (!supabase) return configuredError();
    const fullName = `${input.firstName} ${input.lastName}`.trim();
    const { error } = await supabase.auth.signUp({
      email: input.email,
      password: input.password,
      options: {
        data: { full_name: fullName, country: input.country },
        emailRedirectTo: verificationRedirect,
      },
    });
    return error ? FAIL(error.message) : OK("Account created. Check your email to verify it.");
  },

  async oauth(provider: OAuthProvider): Promise<AuthResult> {
    if (!supabase) return configuredError();
    if (!this.providerEnabled(provider)) return FAIL(`${provider} sign-in is not enabled.`);
    const { error } = await supabase.auth.signInWithOAuth({ provider, options: { redirectTo: appUrl } });
    return error ? FAIL(error.message) : OK(`Redirecting to ${provider}…`);
  },

  async forgotPassword(email: string): Promise<AuthResult> {
    if (!supabase) return configuredError();
    const { error } = await supabase.auth.resetPasswordForEmail(email, { redirectTo });
    return error ? FAIL(error.message) : OK("If that email exists, a reset link is on its way.");
  },

  async updatePassword(password: string): Promise<AuthResult> {
    if (!supabase) return configuredError();
    const { error } = await supabase.auth.updateUser({ password });
    return error ? FAIL(error.message) : OK("Password updated. You can sign in now.");
  },

  async resendVerification(email: string): Promise<AuthResult> {
    if (!supabase) return configuredError();
    const { error } = await supabase.auth.resend({ type: "signup", email, options: { emailRedirectTo: verificationRedirect } });
    return error ? FAIL(error.message) : OK("Verification email resent.");
  },

  async signOut(): Promise<void> {
    await supabase?.auth.signOut({ scope: "local" });
    await fetch("/auth/logout", { method: "POST", credentials: "include" }).catch(() => undefined);
  },

  async signOutAll(): Promise<AuthResult> {
    if (!supabase) return configuredError();
    const { error } = await supabase.auth.signOut({ scope: "global" });
    await fetch("/auth/logout", { method: "POST", credentials: "include" }).catch(() => undefined);
    return error ? FAIL(error.message) : OK("Signed out of all devices.");
  },

  async updateProfile(patch: { full_name?: string; timezone?: string; preferences?: Record<string, unknown> }): Promise<AuthResult> {
    const response = await fetch("/auth/me", {
      method: "PATCH", credentials: "include", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (response.ok) return OK("Profile updated.");
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    return FAIL(body?.detail ?? "Profile update failed.");
  },

  async deleteAccount(): Promise<AuthResult> {
    const response = await fetch("/auth/me", { method: "DELETE", credentials: "include" });
    if (response.ok) return OK("Account deleted.");
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    return FAIL(body?.detail ?? "Account deletion failed.");
  },

  async bridgeCurrentSession(): Promise<AuthResult | null> {
    if (!supabase) return null;
    const { data } = await supabase.auth.getSession();
    return data.session ? bridge(data.session.access_token) : null;
  },

  async verifyTotp(code: string): Promise<AuthResult> {
    if (!supabase) return configuredError();
    const { data: factors, error: fErr } = await supabase.auth.mfa.listFactors();
    if (fErr) return FAIL(fErr.message);
    const totp = factors?.totp?.[0];
    if (!totp) return FAIL("No authenticator app is enrolled for this account.");
    const { data: ch, error: cErr } = await supabase.auth.mfa.challenge({ factorId: totp.id });
    if (cErr || !ch) return FAIL(cErr?.message || "Could not start the 2FA challenge.");
    const { error } = await supabase.auth.mfa.verify({ factorId: totp.id, challengeId: ch.id, code });
    return error ? FAIL(error.message) : OK("Two-factor verified.");
  },
};
