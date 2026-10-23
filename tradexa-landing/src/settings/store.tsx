import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { settingsSchema, defaultSettings, type Settings, type SettingsSection } from "./schema";
import { isSupabaseConfigured, supabase } from "@/lib/supabase";
import { createWorkspaceSession, type WorkspaceIdentity, type WorkspaceSnapshot } from "./workspace-session";

/**
 * Supabase customers use a verified, per-account server workspace. The legacy
 * operator/local mode retains its existing persistence separately.
 */

const STORAGE_KEY = "tradexa.settings.v1";

// Legacy operator sessions get window.__HUB_CONFIG__ injected by the
// backend (same-origin). That is the signal that a per-user server workspace
// exists: the DB becomes the source of truth and localStorage is only the
// fast-boot cache. Signed-out visitors keep local-only behavior.
/** The server says whether this visitor is signed in. Older servers sent the
 *  config only to signed-in visitors, so a config without the flag still counts. */
function signedIn(): boolean {
  if (typeof window === "undefined") return false;
  const cfg = (window as { __HUB_CONFIG__?: { signedIn?: boolean } }).__HUB_CONFIG__;
  return Boolean(cfg) && cfg?.signedIn !== false;
}

export type SaveState = "idle" | "saving" | "saved" | "error";

interface Ctx {
  settings: Settings;
  saveState: SaveState;
  backendConnected: boolean;
  identity: WorkspaceIdentity | null;
  loadState: WorkspaceSnapshot<Settings>["loadState"];
  error: string | null;
  /** Patch one section, merging a partial into the existing values. Autosaves. */
  update: <K extends SettingsSection>(section: K, patch: Partial<Settings[K]>) => void;
  /** Replace one section wholesale (used by RHF form submits). Autosaves. */
  setSection: <K extends SettingsSection>(section: K, value: Settings[K]) => void;
  reset: (section?: SettingsSection) => void;
}

const SettingsContext = createContext<Ctx | null>(null);

// eslint-disable-next-line react-refresh/only-export-components
export function useSettings() {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used within <SettingsProvider>");
  return ctx;
}

function load(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaultSettings;
    // Merge stored values over defaults so newly-added fields are always present.
    const parsed = settingsSchema.deepPartial().parse(JSON.parse(raw));
    return settingsSchema.parse(mergeDeep(defaultSettings, parsed));
  } catch {
    return defaultSettings;
  }
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  return isSupabaseConfigured
    ? <CustomerSettingsProvider>{children}</CustomerSettingsProvider>
    : <LegacySettingsProvider>{children}</LegacySettingsProvider>;
}

/** Customer workspaces are loaded only after server verification, without a
 * shared browser cache. A new identity clears pending work synchronously. */
function CustomerSettingsProvider({ children }: { children: ReactNode }) {
  const controller = useRef<ReturnType<typeof createWorkspaceSession<Settings>> | null>(null);
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot<Settings>>({
    settings: defaultSettings, identity: null, loadState: "signed-out",
    saveState: "idle", backendConnected: false, error: null,
  });

  useEffect(() => {
    if (!supabase) return;
    let active = true;
    let authRevision = 0;
    const session = createWorkspaceSession<Settings>({
      defaults: defaultSettings,
      normalizeSettings: (data) => settingsSchema.parse(
        mergeDeep(defaultSettings, settingsSchema.deepPartial().parse(data)),
      ),
      request: async ({ method, path, accessToken, body }) => {
        const response = await fetch(path, {
          method, credentials: "include", cache: "no-store",
          headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        const result: unknown = await response.json().catch(() => null);
        return { status: response.status, body: result };
      },
      schedule: (callback, delay) => window.setTimeout(callback, delay),
      cancel: (handle) => window.clearTimeout(handle as number),
      debounceMs: 400,
    });
    controller.current = session;
    const unsubscribe = session.subscribe(() => {
      if (active) setSnapshot(session.getSnapshot());
    });
    setSnapshot(session.getSnapshot());

    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, value) => {
      authRevision += 1;
      if (!active) return;
      if (event === "TOKEN_REFRESHED" && value?.access_token && session.getSnapshot().loadState === "ready") {
        void session.renewSession(value.access_token);
      } else {
        void session.setSession(value?.access_token ?? null);
      }
    });
    const initialRevision = authRevision;
    void supabase.auth.getSession().then(({ data, error }) => {
      if (active && initialRevision === authRevision) {
        void session.setSession(error ? null : data.session?.access_token ?? null);
      }
    }).catch(() => {
      if (active && initialRevision === authRevision) void session.setSession(null);
    });
    return () => {
      active = false;
      subscription.unsubscribe();
      unsubscribe();
      session.dispose();
      if (controller.current === session) controller.current = null;
    };
  }, []);

  // Async form submissions can finish after their screen is unmounted. Their
  // callback must retain its original session generation, never the new user.
  const ownedUpdate = useMemo(() => controller.current?.captureUpdater(), [snapshot]);
  const update = useCallback<Ctx["update"]>((section, patch) => {
    ownedUpdate?.(section, patch);
  }, [ownedUpdate]);
  const setSection = useCallback<Ctx["setSection"]>((section, value) => {
    ownedUpdate?.(section, value);
  }, [ownedUpdate]);
  const reset = useCallback((section?: SettingsSection) => {
    const sections = section ? [section] : Object.keys(defaultSettings) as SettingsSection[];
    for (const key of sections) ownedUpdate?.(key, defaultSettings[key]);
  }, [ownedUpdate]);
  const value = useMemo<Ctx>(() => ({ ...snapshot, update, setSection, reset }),
    [snapshot, update, setSection, reset]);
  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

function LegacySettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>(() =>
    typeof window === "undefined" ? defaultSettings : load(),
  );
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const timer = useRef<number | null>(null);

  const persist = useCallback((next: Settings) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {
      /* storage full / disabled — the in-memory state still works */
    }
    if (!signedIn()) {
      setSaveState("saved");
      window.setTimeout(() => setSaveState("idle"), 1400);
      return;
    }
    // Immediately save to the per-user workspace on the backend (session
    // cookie authenticates; each user's blob is isolated by username).
    setSaveState("saving");
    fetch("/user/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ns: "settings-center", data: next }),
    })
      .then((r) => setSaveState(r.ok ? "saved" : "error"))
      .catch(() => setSaveState("error"))
      .finally(() => window.setTimeout(() => setSaveState("idle"), 1600));
  }, []);

  // On login/mount: restore the saved workspace from the database so the user
  // continues exactly where they left off (server wins over the local cache).
  useEffect(() => {
    if (!signedIn()) return;
    fetch("/user/settings?ns=settings-center")
      .then((r) => (r.ok ? r.json() : null))
      .then((body: { data?: Record<string, unknown> } | null) => {
        const data = body?.data;
        if (!data || Object.keys(data).length === 0) return;
        const parsed = settingsSchema.deepPartial().parse(data);
        const restored = settingsSchema.parse(mergeDeep(defaultSettings, parsed));
        setSettings(restored);
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(restored)); } catch { /* cache only */ }
      })
      .catch(() => { /* offline — the local cache already loaded */ });
  }, []);

  const commit = useCallback(
    (next: Settings) => {
      setSettings(next);
      if (timer.current) window.clearTimeout(timer.current);
      // debounce autosave so slider drags don't hammer storage/network
      timer.current = window.setTimeout(() => persist(next), 400);
    },
    [persist],
  );

  const update = useCallback<Ctx["update"]>(
    (section, patch) => {
      setSettings((prev) => {
        const next = { ...prev, [section]: { ...prev[section], ...patch } } as Settings;
        if (timer.current) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => persist(next), 400);
        return next;
      });
    },
    [persist],
  );

  const setSection = useCallback<Ctx["setSection"]>(
    (section, value) => {
      commit({ ...settings, [section]: value } as Settings);
    },
    [commit, settings],
  );

  const reset = useCallback(
    (section?: SettingsSection) => {
      commit(section ? ({ ...settings, [section]: defaultSettings[section] } as Settings) : defaultSettings);
    },
    [commit, settings],
  );

  const value = useMemo<Ctx>(
    () => ({ settings, saveState, backendConnected: signedIn(), identity: null,
      loadState: "ready", error: null, update, setSection, reset }),
    [settings, saveState, update, setSection, reset],
  );

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

/** Apply theme + reduced-animation preferences globally as they change. */
// eslint-disable-next-line react-refresh/only-export-components
export function useApplyAppearance() {
  const { settings } = useSettings();
  const { theme, animations, accent } = settings.appearance;
  useEffect(() => {
    const root = document.documentElement;
    const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const dark = theme === "dark" || (theme === "system" && sysDark);
    root.classList.toggle("dark", dark);
    root.classList.toggle("light", !dark);
    root.dataset.animations = animations ? "on" : "off";
    root.style.setProperty("--accent", accent);
  }, [theme, animations, accent]);
}

// tiny deep-merge for plain objects (settings are 2 levels deep)
function mergeDeep<T>(base: T, over: unknown): T {
  if (typeof base !== "object" || base === null || Array.isArray(base)) {
    return (over === undefined ? base : (over as T));
  }
  const out: Record<string, unknown> = { ...(base as Record<string, unknown>) };
  const o = (over ?? {}) as Record<string, unknown>;
  for (const k of Object.keys(out)) {
    if (k in o) out[k] = mergeDeep((base as Record<string, unknown>)[k], o[k]);
  }
  return out as T;
}
