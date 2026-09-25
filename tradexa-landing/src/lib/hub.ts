/**
 * Runtime config the backend injects when this SPA is served single-origin
 * (see automation-hub/app.py::_serve_landing). Every visitor receives it; its
 * `signedIn` flag says whether this one is signed in. Pages that drive the
 * live engine degrade to an honest "sign in" state when this returns null.
 *
 * The browser never holds the control credential: a signed-in operator's
 * session cookie is enough, and the server supplies the credential to the
 * endpoint itself.
 */
export interface HubConfig {
  apiBase: string;
  signedIn?: boolean;
  /** Only from very old servers; current ones never send it. */
  secret?: string;
}

export function hubConfig(): HubConfig | null {
  if (typeof window === "undefined") return null;
  const cfg = (window as unknown as { __HUB_CONFIG__?: HubConfig }).__HUB_CONFIG__;
  if (!cfg || cfg.signedIn === false) return null;
  return cfg;
}

/** Call a hub API endpoint as the signed-in operator. */
export async function hubFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const cfg = hubConfig();
  if (!cfg) throw new Error("Not signed in");
  const res = await fetch(`${cfg.apiBase}${path}`, {
    credentials: "same-origin",
    ...init,
    headers: {
      // Sending an absent secret would transmit the string "undefined", which
      // the server would take as a (wrong) credential instead of using the session.
      ...(cfg.secret ? { "X-Webhook-Secret": cfg.secret } : {}),
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) throw new Error(`${init?.method ?? "GET"} ${path} → ${res.status}`);
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------- engine sync
import { useCallback, useEffect, useRef, useState } from "react";

/** The engine's REAL editable configuration (GET/POST /settings). Percent
 *  fields are FRACTIONS on the wire (0.03 = 3%) — convert at the UI edge. */
export interface EngineEditable {
  risk_per_trade_pct: number;
  exposure_limit_pct: number;
  max_drawdown_pct: number;
  max_open_positions: number;
  dedup_window_s: number;
  max_daily_loss_pct: number;
  session_start: number;
  session_end: number;
  max_weekly_loss_pct: number;
  max_trades_per_day: number;
  max_consecutive_losses: number;
  cooldown_after_loss_min: number;
  trading_days_mask: number;
  entry_mode: "limit" | "market";
  daily_report_hour: number;
  min_quality_score: number;
  streak_risk_scaling: boolean;
}

export interface EngineReadonly {
  strategy: string;
  strategy_key: string;
  timeframe: string;
  symbols: string[];
  starting_cash: number;
  data_source: string;
  mode: string;
  broker_connected: boolean;
  webhook_secret_set: boolean;
  telegram_configured: boolean;
  poll_seconds?: number | null;
}

export interface EngineSettings {
  editable: EngineEditable;
  readonly: EngineReadonly;
}

/**
 * Load + patch the live engine configuration. Patches are optimistic and
 * debounced (700ms) so slider/keystroke changes coalesce into one POST; on a
 * rejected patch the server state is re-fetched. Only functional for a
 * signed-in operator (hubConfig present) — callers must degrade honestly.
 */
export function useEngineSettings(onSaved?: (ok: boolean, message: string) => void) {
  const signedIn = hubConfig() !== null;
  const [engine, setEngine] = useState<EngineSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<Partial<EngineEditable>>({});
  const timer = useRef<number | null>(null);
  const cb = useRef(onSaved);
  cb.current = onSaved;

  const reload = useCallback(() => {
    if (!hubConfig()) return;
    hubFetch<EngineSettings>("/settings")
      .then((d) => {
        setEngine(d);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const push = useCallback(
    (patch: Partial<EngineEditable>) => {
      setEngine((d) => (d ? { ...d, editable: { ...d.editable, ...patch } } : d));
      pending.current = { ...pending.current, ...patch };
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        const body = pending.current;
        pending.current = {};
        hubFetch("/settings", { method: "POST", body: JSON.stringify(body) })
          .then(() => cb.current?.(true, "Engine updated — now enforced on Nexus."))
          .catch(() => {
            cb.current?.(false, "Engine rejected the change — value restored.");
            reload();
          });
      }, 700);
    },
    [reload],
  );

  return { signedIn, engine, error, reload, push };
}
