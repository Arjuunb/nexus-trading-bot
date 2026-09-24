# @tradelogx/nexus (TypeScript)

A dependency-free client for the TradeLogX Nexus public API (`/v1`). Node 18+,
Deno, Bun or a browser (anything with `fetch`).

Until the package is published, build it from this folder:

```sh
npm run build   # emits dist/
```

```ts
import { Nexus } from "@tradelogx/nexus";

const nexus = new Nexus(); // NEXUS_API_KEY, and optionally NEXUS_API_BASE
for await (const d of nexus.decisions.list({ verdict: "rejected", limit: 20 })) {
  console.log(d.symbol, d.quality_score, d.blocked_by, d.reason);
}
```

Errors throw `NexusError` with `.status`, `.code` and `.message`.
