import { useState } from "react";
import { Link } from "react-router-dom";
import { DevShell, DevSection, Code, Callout, Method } from "@/components/site/dev/DevShell";
import { useRouteMeta } from "@/site/seo";
import { routeFor } from "@/site/routes";
import { cn } from "@/lib/utils";

type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

interface Endpoint {
  method: HttpMethod;
  path: string;
  summary: string;
  detail: string;
  sample: string;
}

const GROUPS: { name: string; endpoints: Endpoint[] }[] = [
  {
    name: "Strategies",
    endpoints: [
      {
        method: "GET",
        path: "/v1/strategies",
        summary: "Every strategy, with its immutable version",
        detail:
          "Returns the strategies the platform can run, each with the version that produced its record, its lifecycle (production or research-only), the timeframes it supports and its execution mode. Every strategy is in paper mode: live order routing is locked.",
        sample: `{
  "data": [
    {
      "id": "brain",
      "name": "Decision Brain",
      "version": "1.0.0",
      "lifecycle": "production",
      "mode": "paper",
      "timeframes": ["15m", "1h", "4h"],
      "markets": ["binance_usdm:perpetual"]
    }
  ],
  "live_routing": "locked"
}`,
      },
      {
        method: "POST",
        path: "/v1/strategies/:id/promote",
        summary: "Ask for paper or live — live is refused",
        detail:
          "Needs the control scope. Paper is the only mode available, so a request for paper succeeds without changing anything, and a request for live is refused while live order routing is locked.",
        sample: `{ "mode": "live" }

→ 409 { "error": { "code": "live_routing_locked",
                   "message": "Live order routing is locked on this platform; every strategy runs in paper mode." } }
→ 200 { "id": "brain", "mode": "paper", "changed": false }   // for { "mode": "paper" }`,
      },
    ],
  },
  {
    name: "Decisions",
    endpoints: [
      {
        method: "GET",
        path: "/v1/decisions",
        summary: "Every evaluation, including the rejections",
        detail:
          "Newest first. Filter by verdict (accepted or rejected), symbol and since (an ISO date). The rejections are the point: they are the only place you can see what the system nearly did, and why it did not. Pages of up to 200; pass next_cursor back as cursor for the next page.",
        sample: `GET /v1/decisions?verdict=rejected&since=2026-09-01&limit=50

{
  "data": [
    {
      "id": "dec_41",
      "ts": "2026-09-24T09:15:00+00:00",
      "symbol": "XRPUSDT",
      "strategy": "Decision Brain",
      "verdict": "rejected",
      "quality_score": 48,
      "blocked_by": "quality",
      "reason": "Quality score 48 is below the minimum of 60.",
      "rules_failed": ["min_quality_score"],
      "executed": false
    }
  ],
  "next_cursor": "dec_41"
}`,
      },
      {
        method: "GET",
        path: "/v1/decisions/:id/replay",
        summary: "Not available yet",
        detail:
          "Decisions are stored with their scores, rules and reason, but not the full market inputs they were made from, so they cannot be re-run. The endpoint answers 501 until those inputs are kept.",
        sample: `→ 501 { "error": { "code": "not_available", "message": "Decisions are stored with their scores and rules, not the full market inputs they were made from, so they cannot be re-run yet." } }`,
      },
    ],
  },
  {
    name: "Positions",
    endpoints: [
      {
        method: "GET",
        path: "/v1/positions",
        summary: "Open paper positions with mark and R multiple",
        detail:
          "Every open paper position, with the latest observed mark, unrealised P&L and R multiple. Stops and targets are managed by the engine on every candle — they are not orders held at an exchange, and the response says so.",
        sample: `{
  "data": [
    {
      "id": "7c1e…", "instance_id": "eb1a2ce8", "symbol": "XRPUSDT",
      "side": "long", "size": 1250.0, "entry": 0.5412, "mark": 0.5498,
      "r_multiple": 0.86, "unrealized_pnl": 10.75, "mode": "paper",
      "protective": { "stop": 0.5312, "target": 0.5712, "managed_by": "engine" }
    }
  ]
}`,
      },
      {
        method: "POST",
        path: "/v1/positions/:id/close",
        summary: "Close a paper position at the current mark",
        detail:
          "Needs the control scope. Closes at a fresh observed price and never at a guessed one — if there is no recent mark the request is refused with no_mark. The close is written to the audit log under the key's name.",
        sample: `{ "reason": "manual flatten before travel" }

→ 200 { "id": "7c1e…", "status": "closed", "close": { "exit": 0.5498, "pnl": 10.75 } }
→ 409 { "error": { "code": "no_mark", ... } }`,
      },
    ],
  },
  {
    name: "Backtests",
    endpoints: [
      {
        method: "POST",
        path: "/v1/backtests",
        summary: "Queue a backtest of one strategy",
        detail:
          "Needs the control scope. Runs a built-in strategy over the Binance candles cached on the server (300 to 1,500 bars). If the server has no data for that symbol and timeframe, the backtest fails with that reason rather than reporting an empty result. Parameter sweeps are not available through the API yet.",
        sample: `{ "strategy": "brain", "symbol": "BTCUSDT", "timeframe": "15m", "bars": 1000 }

→ 202 { "id": "bt_2f77a1c9e0b4", "status": "queued" }`,
      },
      {
        method: "GET",
        path: "/v1/backtests/:id",
        summary: "Results, gross and net of costs",
        detail:
          "Gross performance and performance after modelled fees, spread and slippage are reported side by side, with the difference in R. A strategy whose edge disappears once costs are applied shows as exactly that. Results are kept while the server runs; only the key that queued a backtest can read it.",
        sample: `{
  "id": "bt_2f77a1c9e0b4",
  "status": "complete",
  "result": {
    "gross": { "trades": 38, "win_rate": 42.1, "expectancy_r": 0.12, "net_r": 4.56 },
    "net":   { "trades": 38, "win_rate": 39.5, "expectancy_r": -0.05, "net_r": -1.9 },
    "costs": { "cost_pct_per_side": 0.0006, "net_r_drag": 6.46 }
  }
}`,
      },
    ],
  },
];

const ERRORS = [
  ["400", "invalid_request", "Malformed body, an unknown Nexus-Version, or a parameter outside its allowed range."],
  ["401", "unauthenticated", "Missing, malformed or revoked API key."],
  ["403", "insufficient_scope", "The key is valid but read-only; this operation needs the control scope."],
  ["404", "not_found", "No such strategy, decision, position or backtest (for this key)."],
  ["409", "live_routing_locked", "A request to trade live. Live order routing is locked for every caller."],
  ["409", "no_mark", "A close without a fresh price to close at. Nothing is closed at a guessed price."],
  ["409", "ambiguous_close", "The position's instance holds several positions; close them from the dashboard."],
  ["429", "rate_limited", "Retry after the seconds given in Retry-After."],
  ["501", "not_available", "The endpoint exists but the capability does not yet (decision replay)."],
];

export default function ApiReferencePage() {
  const route = routeFor("/api")!;
  useRouteMeta(route);
  const [open, setOpen] = useState<string | null>("/v1/decisions");

  return (
    <DevShell
      eyebrow="API reference"
      title="One HTTP API, keyed and versioned"
      intro="JSON over HTTPS at trade-logx.com/v1: personal API keys, cursor pagination, stable error codes and signed webhooks. It covers strategies, decisions, positions and backtests; the dashboard also uses internal endpoints that are not part of it."
    >
      <DevSection
        id="auth"
        title="Authentication"
        lead="A bearer token in the header. Create keys in the dashboard under Settings → Security → API keys; each is scoped (read, or read and control), revocable, and shown only once."
      >
        <Code
          lang="bash"
          code={`curl https://trade-logx.com/v1/positions \\
  -H "Authorization: Bearer $NEXUS_API_KEY" \\
  -H "Nexus-Version: 2026-09-24"`}
        />
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Callout title="Versioning">
            The <code className="font-mono text-white/70">Nexus-Version</code> header pins the
            response shape to a date. Omit it and you get the version your key was created
            against — never the newest, so a deploy on our side cannot change your parsing.
            The current version is 2026-09-24.
          </Callout>
          <Callout title="Rate limits">
            600 requests per minute per key, 20 per second burst. The remaining allowance is
            returned on every response in{" "}
            <code className="font-mono text-white/70">X-RateLimit-Remaining</code>; a 429 always
            carries <code className="font-mono text-white/70">Retry-After</code>.
          </Callout>
        </div>
      </DevSection>

      {GROUPS.map((group) => (
        <DevSection key={group.name} id={group.name.toLowerCase()} title={group.name}>
          <ul className="divide-y divide-white/[0.06] overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.015]">
            {group.endpoints.map((e) => {
              const isOpen = open === e.path;
              return (
                <li key={e.path}>
                  <button
                    onClick={() => setOpen(isOpen ? null : e.path)}
                    aria-expanded={isOpen}
                    className="flex w-full items-center gap-3 p-4 text-left transition-colors hover:bg-white/[0.03]"
                  >
                    <Method method={e.method} />
                    <code className="shrink-0 font-mono text-[13px] text-white/80">{e.path}</code>
                    <span className="ml-auto hidden truncate text-xs text-white/35 sm:block">
                      {e.summary}
                    </span>
                    <span
                      className={cn(
                        "shrink-0 font-mono text-[13px] text-white/25 transition-transform duration-300",
                        isOpen && "rotate-45",
                      )}
                    >
                      +
                    </span>
                  </button>

                  <div
                    className="grid transition-[grid-template-rows] duration-400 ease-out motion-reduce:transition-none"
                    style={{ gridTemplateRows: isOpen ? "1fr" : "0fr" }}
                  >
                    <div className="overflow-hidden">
                      <div className="border-t border-white/[0.06] p-4">
                        <p className="mb-4 max-w-2xl text-sm leading-relaxed text-white/55">
                          {e.detail}
                        </p>
                        <Code lang="json" label="example" code={e.sample} />
                      </div>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </DevSection>
      ))}

      <DevSection
        id="webhooks"
        title="Webhooks"
        lead="Every decision the engine records, pushed to your endpoint as it happens. Add endpoints in the dashboard under Settings → Security → Webhooks; the signing secret is shown once."
      >
        <Code
          lang="json"
          label="envelope"
          code={`{
  "id": "evt_dec_41",
  "type": "decision.rejected",
  "occurred_at": "2026-09-24T09:18:00Z",
  "sequence": 41,
  "idempotency_key": "dec_41:rejected",
  "data": { "...": "the same object GET /v1/decisions/dec_41 returns" }
}`}
        />
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Callout title="Events">
            <code className="font-mono text-white/70">decision.accepted</code>,{" "}
            <code className="font-mono text-white/70">decision.rejected</code>, and{" "}
            <code className="font-mono text-white/70">webhook.test</code> when you press Send
            test. A new endpoint starts from the next decision; history is not replayed to it.
          </Callout>
          <Callout title="Signatures">
            <code className="font-mono text-white/70">Nexus-Signature: t=…,v1=…</code> is
            HMAC-SHA256 of <code className="font-mono text-white/70">t.body</code> with your
            secret. Verify the raw body before parsing and reject stale timestamps; every SDK
            has a helper for it.
          </Callout>
        </div>
        <p className="mt-4 max-w-2xl text-sm leading-relaxed text-white/55">
          Delivery is at-least-once, so handlers must be idempotent — the{" "}
          <code className="font-mono text-white/70">idempotency_key</code> is stable across
          retries. Any answer other than 2xx is retried after 30 seconds, doubling up to an hour,
          for 24 hours. Every attempt, with the status code your endpoint returned, is listed
          under the endpoint in the dashboard.
        </p>
      </DevSection>

      <DevSection
        id="errors"
        title="Errors"
        lead={`Every error has the same shape: {"error": {"code": "…", "message": "…"}}. Branch on the code; the message is for people.`}
      >
        <div className="overflow-x-auto rounded-xl border border-white/[0.08]">
          <table className="w-full min-w-[560px] border-collapse text-left">
            <thead>
              <tr className="border-b border-white/[0.08] bg-white/[0.02]">
                {["Status", "Code", "Means"].map((h) => (
                  <th
                    key={h}
                    className="px-4 py-2.5 font-mono text-[10px] uppercase tracking-wider text-white/30"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ERRORS.map(([status, code, means]) => (
                <tr key={code} className="border-b border-white/[0.05] last:border-0">
                  <td className="px-4 py-2.5 font-mono text-[12px] text-white/70">{status}</td>
                  <td className="px-4 py-2.5 font-mono text-[12px] text-aqua-soft">{code}</td>
                  <td className="px-4 py-2.5 text-[13px] text-white/50">{means}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-4">
          <Callout tone="warn" title="409 live_routing_locked is by design">
            Live order routing is locked on this platform, so promoting a strategy to live is
            refused for every key and every strategy runs in paper mode. Whether the API itself
            is up is on the{" "}
            <Link to="/status" className="text-gold-soft underline-offset-2 hover:underline">
              status page
            </Link>
            .
          </Callout>
        </div>
      </DevSection>
    </DevShell>
  );
}
