import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { Nexus, NexusError } from "../dist/index.js";

const DECISIONS = [5, 4, 3, 2, 1].map((i) => ({ id: `dec_${i}`, symbol: "BTCUSDT", verdict: "rejected" }));

function fake() {
  const state = { calls: [], failOnce: new Set() };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://x");
    let raw = ""; for await (const c of req) raw += c;
    const body = raw ? JSON.parse(raw) : undefined;
    state.calls.push({ method: req.method, path: url.pathname, headers: req.headers });
    const send = (code, obj, headers = {}) => { res.writeHead(code, { "Content-Type": "application/json", ...headers }); res.end(JSON.stringify(obj)); };
    if (req.headers.authorization !== "Bearer nxs_test_key") return send(401, { error: { code: "unauthenticated", message: "no" } });
    if (state.failOnce.delete(url.pathname)) return send(429, { error: { code: "rate_limited", message: "slow" } }, { "Retry-After": "0" });
    if (url.pathname === "/v1/decisions") {
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const cursor = url.searchParams.get("cursor");
      const start = cursor ? DECISIONS.findIndex((d) => d.id === cursor) + 1 : 0;
      const page = DECISIONS.slice(start, start + limit);
      return send(200, { data: page, next_cursor: page.length === limit && start + limit < DECISIONS.length ? page.at(-1).id : null });
    }
    if (url.pathname === "/v1/strategies/decision_brain/promote")
      return body.mode === "live" ? send(409, { error: { code: "live_routing_locked", message: "locked" } }) : send(200, { id: "decision_brain", mode: "paper", changed: false });
    if (url.pathname === "/v1/backtests") return send(202, { id: "bt_1", status: "queued" });
    if (url.pathname === "/v1/backtests/bt_1") return send(200, { id: "bt_1", status: "complete", result: { net: { net_r: 0.5 } } });
    return send(404, { error: { code: "not_found", message: url.pathname } });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, state, base: `http://127.0.0.1:${server.address().port}` })));
}

test("decisions follow cursors and respect the limit; version header is sent", async () => {
  const { server, state, base } = await fake();
  const nexus = new Nexus({ apiKey: "nxs_test_key", baseUrl: base, backoffMs: 0 });
  const ids = []; for await (const d of nexus.decisions.list({ pageSize: 2 })) ids.push(d.id);
  assert.deepEqual(ids, DECISIONS.map((d) => d.id));
  const three = []; for await (const d of nexus.decisions.list({ limit: 3, pageSize: 2 })) three.push(d);
  assert.equal(three.length, 3);
  assert.equal(state.calls[0].headers["nexus-version"], "2026-09-24");
  server.close();
});

test("errors carry the stable code", async () => {
  const { server, base } = await fake();
  const nexus = new Nexus({ apiKey: "nxs_test_key", baseUrl: base });
  await assert.rejects(nexus.strategies.promote("decision_brain", "live"), (e) => e instanceof NexusError && e.status === 409 && e.code === "live_routing_locked");
  await assert.rejects(new Nexus({ apiKey: "nxs_wrong", baseUrl: base }).strategies.list(), (e) => e.code === "unauthenticated");
  server.close();
});

test("429 is retried; backtests.run waits for completion", async () => {
  const { server, state, base } = await fake();
  const nexus = new Nexus({ apiKey: "nxs_test_key", baseUrl: base, backoffMs: 0 });
  state.failOnce.add("/v1/decisions");
  const got = []; for await (const d of nexus.decisions.list({ limit: 1 })) got.push(d);
  assert.equal(got.length, 1);
  const job = await nexus.backtests.run("decision_brain", { pollMs: 0 });
  assert.equal(job.status, "complete");
  server.close();
});
