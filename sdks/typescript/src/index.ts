/**
 * TypeScript client for the TradeLogX Nexus public API (`/v1`).
 *
 *   import { Nexus } from "@tradelogx/nexus";
 *   const nexus = new Nexus();               // reads NEXUS_API_KEY
 *   for await (const d of nexus.decisions.list({ verdict: "rejected" })) {
 *     console.log(d.symbol, d.quality_score, d.blocked_by, d.reason);
 *   }
 *
 * No dependencies: uses the runtime's fetch (Node 18+, Deno, Bun, browsers).
 * Pages are followed automatically; 429 and temporary 5xx answers are retried
 * with exponential backoff honouring Retry-After; writes are retried only on
 * 429, when the server says it did not process them.
 */

export const API_VERSION = "2026-09-24";
export const VERSION = "0.1.0";
const DEFAULT_BASE = "https://trade-logx.com";
const RETRYABLE = new Set([429, 502, 503, 504]);

export interface Strategy {
  id: string; name: string; version: string; lifecycle: string; mode: "paper";
  timeframes: string[]; markets: string[]; description: string;
}
export interface Decision {
  id: string; ts: string; symbol: string; timeframe: string | null; strategy: string | null;
  side: string | null; regime: string | null; verdict: "accepted" | "rejected";
  quality_score: number | null; blocked_by: string | null; reason: string | null;
  rules_passed: unknown[]; rules_failed: unknown[]; executed: boolean;
  instance_id: string | null; components: Record<string, unknown>;
}
export interface Position {
  id: string; instance_id: string; symbol: string; side: string; size: number; entry: number;
  mark: number | null; mark_available: boolean; unrealized_pnl: number | null; r_multiple: number | null;
  protective: { stop: number | null; target: number | null; managed_by: "engine" };
  opened_at: string | null; mode: "paper";
}
export interface BacktestFigures {
  trades: number; win_rate: number; expectancy_r: number; net_r: number; profit_factor: number; max_drawdown_r: number;
}
export interface Backtest {
  id: string; status: "queued" | "running" | "complete" | "failed";
  request: { strategy: string; symbol: string; timeframe: string; bars: number };
  queued_at: string; finished_at?: string;
  result: null | { gross: BacktestFigures; net: BacktestFigures; costs: { cost_pct_per_side: number; net_r_drag: number };
    data: Record<string, unknown> } | { error: string };
}
export interface DecisionQuery { verdict?: "accepted" | "rejected"; symbol?: string; since?: string; limit?: number; pageSize?: number }
export interface Options { apiKey?: string; baseUrl?: string; version?: string; maxRetries?: number; backoffMs?: number; fetch?: typeof fetch }

/** An error answer from the API: HTTP status, a stable code and a message. */
export class NexusError extends Error {
  constructor(public status: number, public code: string, message: string, public body?: unknown) {
    super(`${status} ${code}: ${message}`);
    this.name = "NexusError";
  }
}

const env = (name: string): string | undefined =>
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export class Nexus {
  readonly apiKey: string;
  readonly baseUrl: string;
  readonly version: string;
  private readonly maxRetries: number;
  private readonly backoffMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: Options = {}) {
    this.apiKey = options.apiKey ?? env("NEXUS_API_KEY") ?? "";
    if (!this.apiKey) throw new Error("No API key: pass apiKey or set NEXUS_API_KEY.");
    this.baseUrl = (options.baseUrl ?? env("NEXUS_API_BASE") ?? DEFAULT_BASE).replace(/\/+$/, "");
    this.version = options.version ?? API_VERSION;
    this.maxRetries = options.maxRetries ?? 3;
    this.backoffMs = options.backoffMs ?? 500;
    this.fetchImpl = options.fetch ?? fetch;
  }

  async request<T>(method: "GET" | "POST", path: string, opts: { params?: Record<string, unknown>; body?: unknown } = {}): Promise<T> {
    const query = new URLSearchParams();
    for (const [k, v] of Object.entries(opts.params ?? {})) if (v !== undefined && v !== null) query.set(k, String(v));
    const url = `${this.baseUrl}/v1${path}${query.size ? `?${query}` : ""}`;
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`, "Nexus-Version": this.version, Accept: "application/json",
    };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    for (let attempt = 0; ; attempt++) {
      let res: Response;
      try {
        res = await this.fetchImpl(url, { method, headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
      } catch (err) {
        if (method === "GET" && attempt < this.maxRetries) { await sleep(this.backoffMs * 2 ** attempt); continue; }
        throw new NexusError(0, "network_error", err instanceof Error ? err.message : String(err));
      }
      if (res.ok) return (res.status === 204 ? undefined : await res.json()) as T;
      const retryable = res.status === 429 || (method === "GET" && RETRYABLE.has(res.status));
      if (retryable && attempt < this.maxRetries) {
        const after = Number(res.headers.get("Retry-After"));
        await sleep(Number.isFinite(after) && res.headers.get("Retry-After") !== null ? after * 1000 : this.backoffMs * 2 ** attempt);
        continue;
      }
      let payload: { error?: { code?: string; message?: string } } = {};
      try { payload = await res.json(); } catch { /* not JSON */ }
      throw new NexusError(res.status, payload.error?.code ?? "http_error", payload.error?.message ?? res.statusText, payload);
    }
  }

  readonly strategies = {
    list: async (): Promise<Strategy[]> => (await this.request<{ data: Strategy[] }>("GET", "/strategies")).data,
    /** mode "live" is refused (code live_routing_locked) while live routing is locked. */
    promote: (id: string, mode: "paper" | "live") =>
      this.request<{ id: string; mode: "paper"; changed: boolean }>("POST", `/strategies/${encodeURIComponent(id)}/promote`, { body: { mode } }),
  };

  readonly decisions = {
    /** Every matching decision, newest first, following pages as needed. */
    list: (query: DecisionQuery = {}): AsyncIterable<Decision> => {
      const self = this;
      return {
        async *[Symbol.asyncIterator]() {
          let cursor: string | null = null;
          let seen = 0;
          const pageSize = query.pageSize ?? 100;
          for (;;) {
            const size = query.limit === undefined ? pageSize : Math.min(pageSize, query.limit - seen);
            if (size <= 0) return;
            const page: { data: Decision[]; next_cursor: string | null } = await self.request("GET", "/decisions", {
              params: { verdict: query.verdict, symbol: query.symbol, since: query.since, limit: size, cursor } });
            for (const d of page.data) { yield d; seen++; }
            cursor = page.next_cursor;
            if (!cursor) return;
          }
        },
      };
    },
    get: (id: string) => this.request<Decision>("GET", `/decisions/${encodeURIComponent(id)}`),
  };

  readonly positions = {
    list: async (): Promise<Position[]> => (await this.request<{ data: Position[] }>("GET", "/positions")).data,
    close: (id: string, reason = "") => this.request<{ id: string; status: "closed" }>("POST", `/positions/${encodeURIComponent(id)}/close`, { body: { reason } }),
  };

  readonly backtests = {
    create: (strategy: string, opts: { symbol?: string; timeframe?: string; bars?: number } = {}) =>
      this.request<{ id: string; status: string }>("POST", "/backtests", { body: { strategy, ...opts } }),
    get: (id: string) => this.request<Backtest>("GET", `/backtests/${encodeURIComponent(id)}`),
    /** Queue a backtest and wait for it to finish. */
    run: async (strategy: string, opts: { symbol?: string; timeframe?: string; bars?: number; pollMs?: number; timeoutMs?: number } = {}): Promise<Backtest> => {
      const { pollMs = 2000, timeoutMs = 600_000, ...params } = opts;
      const queued = await this.backtests.create(strategy, params);
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const job = await this.backtests.get(queued.id);
        if (job.status === "complete") return job;
        if (job.status === "failed") throw new NexusError(200, "backtest_failed", String((job.result as { error?: string })?.error ?? ""), job);
        if (Date.now() > deadline) throw new Error(`backtest ${job.id} still ${job.status} after ${timeoutMs} ms`);
        await sleep(pollMs);
      }
    },
  };
}
