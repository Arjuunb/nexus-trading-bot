/** Verified account identity and a bounded, per-session settings workspace. */
export interface WorkspaceIdentity {
  id: string;
  email: string;
  full_name: string;
  avatar_url: string;
  role: string;
  email_confirmed: boolean;
}

export interface WorkspaceSnapshot<TSettings> {
  settings: TSettings;
  identity: WorkspaceIdentity | null;
  loadState: "signed-out" | "loading" | "ready" | "error";
  saveState: "idle" | "saving" | "saved" | "error";
  backendConnected: boolean;
  error: string | null;
}

export interface WorkspaceRequest {
  method: "GET" | "POST";
  path: string;
  accessToken: string;
  body?: unknown;
}

export interface WorkspaceResponse {
  status: number;
  body: unknown;
}

export interface WorkspaceSession<TSettings extends object> {
  getSnapshot(): WorkspaceSnapshot<TSettings>;
  subscribe(listener: () => void): () => void;
  setSession(accessToken: string | null): Promise<void>;
  renewSession(accessToken: string): Promise<void>;
  update<K extends keyof TSettings>(section: K, patch: Partial<TSettings[K]>): void;
  captureUpdater(): WorkspaceSession<TSettings>["update"];
  dispose(): void;
}

interface WorkspaceOptions<TSettings, TTimer> {
  defaults: TSettings;
  normalizeSettings(incoming: unknown): TSettings;
  request(input: WorkspaceRequest): Promise<WorkspaceResponse>;
  schedule(callback: () => void, delayMs: number): TTimer;
  cancel(handle: TTimer): void;
  debounceMs?: number;
}

const NAMESPACE = "settings-center";
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const succeeded = (response: WorkspaceResponse) => response.status >= 200 && response.status < 300;
const copy = <T>(value: T): T => structuredClone(value);

function identityFrom(response: WorkspaceResponse): WorkspaceIdentity {
  const body = response.body;
  if (!succeeded(response) || !isRecord(body)
      || typeof body.id !== "string" || !body.id
      || typeof body.email !== "string" || !body.email
      || typeof body.role !== "string" || !body.role
      || body.email_confirmed !== true) {
    throw new Error("Unable to verify your account.");
  }
  return {
    id: body.id,
    email: body.email,
    full_name: typeof body.full_name === "string" ? body.full_name : "",
    avatar_url: typeof body.avatar_url === "string" ? body.avatar_url : "",
    role: body.role,
    email_confirmed: true,
  };
}

/**
 * Tokens stay inside this closure. The injected transport performs I/O, while
 * generation checks prevent old identity work from changing the new session.
 * Authenticated settings never read or write a browser-global settings cache.
 */
export function createWorkspaceSession<TSettings extends object, TTimer = number>(
  options: WorkspaceOptions<TSettings, TTimer>,
): WorkspaceSession<TSettings> {
  let snapshot: WorkspaceSnapshot<TSettings> = {
    settings: copy(options.defaults), identity: null, loadState: "signed-out",
    saveState: "idle", backendConnected: false, error: null,
  };
  const listeners = new Set<() => void>();
  let disposed = false;
  let accessToken: string | null = null;
  let generation = 0;
  let renewalSequence = 0;
  let renewal: Promise<void> | null = null;
  let timer: TTimer | null = null;
  let revision = 0;
  let writeQueue = Promise.resolve();

  const active = (captured: number) => !disposed && captured === generation;
  const publish = (patch: Partial<WorkspaceSnapshot<TSettings>>) => {
    snapshot = { ...snapshot, ...patch };
    if (!disposed) for (const listener of listeners) listener();
  };
  const cancelTimer = () => {
    if (timer !== null) options.cancel(timer);
    timer = null;
  };

  // This must run synchronously when account ownership changes, before I/O.
  const clear = (loadState: WorkspaceSnapshot<TSettings>["loadState"], error: string | null = null) => {
    generation += 1;
    renewalSequence += 1;
    accessToken = null;
    renewal = null;
    revision = 0;
    writeQueue = Promise.resolve();
    cancelTimer();
    publish({
      settings: copy(options.defaults), identity: null, loadState,
      saveState: "idle", backendConnected: false, error,
    });
  };

  async function setSession(nextToken: string | null): Promise<void> {
    if (disposed) return;
    if (nextToken && nextToken === accessToken && snapshot.loadState === "ready"
        && snapshot.backendConnected && snapshot.saveState !== "error") return;
    clear(nextToken ? "loading" : "signed-out");
    if (!nextToken) return;
    const captured = generation;
    accessToken = nextToken;
    let identity: WorkspaceIdentity;
    try {
      const response = await options.request({ method: "GET", path: "/auth/me", accessToken: nextToken });
      if (!active(captured)) return;
      identity = identityFrom(response);
      publish({ identity });
    } catch {
      if (active(captured)) clear("error", "Unable to verify your account.");
      return;
    }
    try {
      const response = await options.request({
        method: "GET", path: `/user/settings?ns=${NAMESPACE}`, accessToken: nextToken,
      });
      if (!active(captured)) return;
      const body = response.body;
      if (!succeeded(response) || !isRecord(body) || body.ns !== NAMESPACE || !isRecord(body.data)) {
        throw new Error("Workspace settings unavailable.");
      }
      const settings = copy(options.normalizeSettings(body.data));
      publish({ settings, identity, loadState: "ready", backendConnected: true, error: null });
    } catch {
      if (active(captured)) {
        publish({ loadState: "error", backendConnected: false, error: "Workspace settings unavailable." });
      }
    }
  }

  async function renewSession(nextToken: string): Promise<void> {
    if (disposed) return;
    if (nextToken && nextToken === accessToken && snapshot.identity) return;
    if (!nextToken || !accessToken || !snapshot.identity) {
      clear("error", "Unable to renew your verified session.");
      return;
    }
    const captured = generation;
    const sequence = ++renewalSequence;
    const ownerId = snapshot.identity.id;
    const task = (async () => {
      try {
        const response = await options.request({ method: "GET", path: "/auth/me", accessToken: nextToken });
        if (!active(captured) || sequence !== renewalSequence) return;
        const identity = identityFrom(response);
        if (identity.id !== ownerId) throw new Error("Session identity changed.");
        accessToken = nextToken;
        publish({ identity });
      } catch {
        if (active(captured) && sequence === renewalSequence) {
          clear("error", "Unable to renew your verified session.");
        }
      }
    })();
    renewal = task;
    await task;
    if (renewal === task) renewal = null;
  }

  async function save(settings: TSettings, captured: number, ownerId: string, editRevision: number) {
    // A debounce may fire while a fresh token is still being verified. Preserve
    // the pending edit and wait before choosing credentials for its request.
    let pendingRenewal = renewal;
    while (pendingRenewal) {
      await pendingRenewal;
      if (!active(captured)) return;
      if (renewal === pendingRenewal) break;
      pendingRenewal = renewal;
    }
    if (!active(captured) || snapshot.identity?.id !== ownerId || !accessToken) return;
    const writeToken = accessToken;
    publish({ saveState: "saving", error: null });
    try {
      const response = await options.request({
        method: "POST", path: "/user/settings", accessToken: writeToken,
        body: { ns: NAMESPACE, data: copy(settings) },
      });
      if (!active(captured) || editRevision !== revision) return;
      const body = response.body;
      if (!succeeded(response) || !isRecord(body) || body.saved !== true || body.ns !== NAMESPACE) {
        throw new Error("Settings were not saved.");
      }
      publish({ saveState: "saved", backendConnected: true, error: null });
    } catch {
      if (active(captured) && editRevision === revision) {
        publish({ saveState: "error", backendConnected: false, error: "Settings were not saved." });
      }
    }
  }

  function update<K extends keyof TSettings>(section: K, patch: Partial<TSettings[K]>) {
    if (disposed || !accessToken || !snapshot.identity || snapshot.loadState !== "ready") return;
    const captured = generation;
    const ownerId = snapshot.identity.id;
    let settings: TSettings;
    try {
      settings = copy(options.normalizeSettings({
        ...snapshot.settings,
        [section]: Object.assign({}, snapshot.settings[section], patch),
      }));
    } catch {
      publish({ saveState: "error", error: "Settings are invalid." });
      return;
    }
    const editRevision = ++revision;
    cancelTimer();
    publish({ settings, saveState: "idle", error: null });
    timer = options.schedule(() => {
      if (!active(captured)) return;
      timer = null;
      // Each identity has its own queue. Older responses cannot acknowledge
      // newer edits, and writes cannot finish in reverse order on the server.
      writeQueue = writeQueue.then(() => save(settings, captured, ownerId, editRevision));
    }, options.debounceMs ?? 400);
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener) {
      if (!disposed) listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    setSession,
    renewSession,
    update,
    captureUpdater() {
      const captured = generation;
      const ownerId = snapshot.identity?.id;
      return (section, patch) => {
        if (!ownerId || !active(captured) || snapshot.identity?.id !== ownerId) return;
        update(section, patch);
      };
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      clear("signed-out");
      listeners.clear();
    },
  };
}
