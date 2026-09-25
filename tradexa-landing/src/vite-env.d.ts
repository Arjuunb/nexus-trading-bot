/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL?: string;
  readonly VITE_SUPABASE_ANON_KEY?: string;
  readonly VITE_APP_URL?: string;
}

interface Window {
  __HUB_CONFIG__?: {
    apiBase?: string;
    authMode?: string;
    supabaseUrl?: string | null;
    supabaseAnonKey?: string | null;
    oauthProviders?: string[];
    signedIn?: boolean;
  };
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
