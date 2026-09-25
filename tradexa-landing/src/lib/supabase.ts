import { createClient, type SupabaseClient } from "@supabase/supabase-js";

interface RuntimeConfig {
  supabaseUrl?: string;
  supabaseAnonKey?: string;
}

const runtime = (typeof window !== "undefined" ? (window as Window & { __HUB_CONFIG__?: RuntimeConfig }).__HUB_CONFIG__ : undefined) ?? {};
const url = runtime.supabaseUrl ?? (import.meta.env.VITE_SUPABASE_URL as string | undefined);
const anonKey = runtime.supabaseAnonKey ?? (import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined);

/**
 * True when real Supabase credentials are present. When false, the auth layer
 * runs in a deliberately unavailable state: no demo accounts or local password
 * fallback exist in production. Supply only the public URL/anon key; the
 * service-role key must stay on the server.
 */
export const isSupabaseConfigured = Boolean(url && anonKey);

export interface AuthRedirect {
  /** "recovery", "signup", "email", … as the provider's link reports it. */
  type: string | null;
  /** The provider's own error text, e.g. an expired or already-used link. */
  error: string | null;
  errorCode: string | null;
  /** The link carried a session or a code to exchange for one. */
  hasToken: boolean;
}

function parseAuthRedirect(hash: string, search: string): AuthRedirect {
  const h = new URLSearchParams(hash.replace(/^#/, ""));
  const q = new URLSearchParams(search);
  const get = (key: string) => h.get(key) ?? q.get(key);
  return {
    type: get("type"),
    error: get("error_description") ?? get("error"),
    errorCode: get("error_code"),
    hasToken: Boolean(h.get("access_token") || q.get("code") || q.get("token_hash")),
  };
}

/**
 * The URL exactly as an email link delivered it, read before the client below
 * consumes the session fragment. Pages use it to tell "this link expired" from
 * "this link worked" instead of guessing after the fragment is gone.
 */
export const initialAuthRedirect: AuthRedirect | null = typeof window !== "undefined"
  ? parseAuthRedirect(window.location.hash, window.location.search)
  : null;

export const supabase: SupabaseClient | null = isSupabaseConfigured
  ? createClient(url as string, anonKey as string, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    })
  : null;
