/* Isolated browser checks for Guardian's actual static UI with test-only data.
 * Requires Playwright; no app, trading database or production keys are used. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");

const assets = path.resolve(__dirname, "../tradexa/guardian/assets");
let ledgerMode = "current";
const ledger = () => ({
  observation_state: ledgerMode === "stale" ? "UNKNOWN" : "CURRENT",
  observation_age_seconds: 1, snapshot_atomic: true, event_id: "fixture-ledger-01",
  instances: [{ instance_id: "paper-instance", open_positions: 1, open_trades: 1,
    risk_amount: ledgerMode === "stale" ? null : 10,
    risk_complete: ledgerMode !== "stale" }],
  findings: [{ instance_id: "paper-instance", codes: [] }],
});
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  const staticFiles = { "/": ["command_center.html", "text/html"],
    "/assets/command-center.js": ["command_center.js", "application/javascript"],
    "/assets/command-center.css": ["command_center.css", "text/css"] };
  if (staticFiles[url.pathname]) {
    const [file, type] = staticFiles[url.pathname];
    response.writeHead(200, { "Content-Type": type });
    response.end(fs.readFileSync(path.join(assets, file)));
    return;
  }
  if (url.pathname === "/favicon.ico") { response.writeHead(204); response.end(); return; }
  const pages = {
    "/v1/health": { state: "HEALTHY", evidence_complete: true,
      components: { guardian: { state: "HEALTHY", age_seconds: 1 } },
      active_incidents: { total: 0 } },
    "/v1/events": { events: [] }, "/v1/incidents": { incidents: [] },
    "/v1/decision-traces": { traces: [] },
    "/v1/instance-decision-traces": { traces: [] },
    "/v1/instance-ledger": ledger(),
    "/v1/notifications": { notifications: [] },
    "/v1/research/hypotheses": { hypotheses: [] },
    "/v1/reports": { reports: [{ kind: "DAILY", report_id: "fixture-report",
      window_start: "2026-10-01T00:00:00+00:00", window_end: "2026-10-02T00:00:00+00:00",
      coverage: { observed_events: 0, scan_truncated: false }, strategies: [],
      conclusions: ["Trade outcomes, currency, global exposure and uptime are not verified."] }] },
  };
  if (url.pathname === "/v1/instance-ledger" && ledgerMode === "failed") {
    response.writeHead(503, { "Content-Type": "application/json" });
    response.end('{"error":"PERSISTENCE_UNAVAILABLE"}'); return;
  }
  response.writeHead(pages[url.pathname] ? 200 : 404, { "Content-Type": "application/json" });
  response.end(JSON.stringify(pages[url.pathname] || { error: "NOT_FOUND" }));
});

(async () => {
  let browser;
  try {
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({ headless: true,
      ...(process.env.GUARDIAN_SMOKE_CHROME ? { executablePath: process.env.GUARDIAN_SMOKE_CHROME } : {}) });
    const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
    await context.route("**/*", (route) => route.request().url().startsWith(origin + "/")
      ? route.continue() : route.abort());
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.clock.install();
    await page.goto(origin);
    await page.locator("#read-key").fill("fixture-read-key-for-local-ui-test");
    await page.locator("#connect").click();
    const risk = page.locator("#instance-ledger td").nth(3);
    await page.waitForFunction(() => document.querySelector("#instance-ledger td:nth-child(4)")?.textContent === "10");
    assert.equal(await page.locator("#read-key").inputValue(), "");
    await page.locator("#reports summary").click();
    assert.match(await page.locator("#reports").textContent(), /outcomes.*not verified/);

    ledgerMode = "failed";
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector("#overall-state").textContent === "UNKNOWN");
    assert.equal(await risk.textContent(), "Unknown");
    assert.equal(await page.locator("#components .state").textContent(), "UNKNOWN");
    assert.equal(await page.locator("#incident-count").textContent(), "—");
    assert.match(await page.locator("#instance-ledger-coverage").textContent(), /unavailable/);

    ledgerMode = "current";
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector("#instance-ledger td:nth-child(4)")?.textContent === "10");
    ledgerMode = "stale";
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector("#instance-ledger td:nth-child(4)")?.textContent === "Unknown");
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    assert.equal(await page.locator("h1").count(), 1);
    await page.locator("#instance-ledger-title").scrollIntoViewIfNeeded();
    await page.clock.runFor(100);
    if (process.env.GUARDIAN_SMOKE_SCREENSHOT) {
      await page.screenshot({ path: process.env.GUARDIAN_SMOKE_SCREENSHOT });
    }
    await page.locator("#disconnect").click();
    assert.equal(await page.locator("#instance-ledger tr").count(), 0);
    assert.deepEqual(errors, []);
    console.log("PASS: current risk, failed-read masking, retry, stale-read masking, mobile containment, disconnect, zero JavaScript errors");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
