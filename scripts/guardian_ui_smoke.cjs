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
let analysisMode = "current";
const incidentId = "a".repeat(32);
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
    "/v1/health": { state: "DEGRADED", evidence_complete: true,
      components: { guardian: { state: "HEALTHY", age_seconds: 1 } },
      active_incidents: { total: 1 } },
    "/v1/events": { events: [] }, "/v1/incidents": { incidents: [{ incident_id: incidentId,
      last_seen_at: "2026-10-04T12:10:00Z", state: "OPEN", title: "Market data disruption",
      root_cause: "Public websocket disconnected", confidence: "CONFIRMED", evidence_count: 2 }] },
    [`/v1/incidents/${incidentId}/investigation`]: {
      root_cause_candidates: [{ summary: "Public websocket disconnected <example>",
        failure_fact_confidence: "CONFIRMED", causal_confidence: "POSSIBLE", evidence_ids: ["fixture-down"] }],
      coverage: { context_scan_truncated: false },
      timeline: [{ source_time: "2026-10-04T12:05:00Z", received_at: "2026-10-04T12:05:01Z",
        clock_valid: true, relationship: "DIRECT_INCIDENT_EVIDENCE", event_type: "websocket_disconnected",
        reason: "SOURCE_DISCONNECTED", event_id: "fixture-down" }],
    },
    "/v1/system-map": { required_dependency_readiness: "BLOCKED_BY_DEPENDENCY",
      nodes: [{ component: "smc_lab", observed_state: "HEALTHY", dependency_readiness: "BLOCKED_BY_DEPENDENCY",
        requires: ["smc_feed"], blocked_by: ["smc_feed"], unknown_dependencies: [] }] },
    "/v1/anomalies": { cutoff: "2026-10-04T12:10:00Z", scan_truncated: false,
      anomalies: analysisMode === "sparse" ? [] : [{ identity: { source_service: "smc_lab",
        source_component: "strategy", latency_kind: "strategy_evaluation", symbol: "BTCUSDT", timeframe: "5m",
        session_id: "fixture-session", strategy_version: "1.0" }, state: "LATENCY_DEVIATION",
        severity: "WATCH", baseline_samples: 20, current_samples: 5, current_median_ms: 100,
        threshold_ms: 50, reasons: [], current_evidence_ids: ["fixture-latency"] }] },
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
    await page.locator("#dependencies-title").locator("..").locator("summary").click();
    assert.match(await page.locator("#dependency-map").textContent(), /HEALTHY.*BLOCKED_BY_DEPENDENCY.*smc_feed/);
    assert.match(await page.locator("#anomalies").textContent(), /LATENCY_DEVIATION.*WATCH/);
    await page.locator("#incidents .timeline-control").click();
    await page.waitForFunction(() => document.querySelector(".timeline-row")?.textContent.includes("Cause: POSSIBLE"));
    assert.match(await page.locator(".timeline-row").textContent(), /Failure fact: CONFIRMED.*Cause: POSSIBLE/);
    assert.match(await page.locator(".timeline-row").textContent(), /<example>/);
    assert.equal(await page.locator(".timeline-row example").count(), 0);
    if (process.env.GUARDIAN_SMOKE_PHASE3_SCREENSHOT) {
      await page.locator("#dependencies-title").scrollIntoViewIfNeeded();
      await page.clock.runFor(100);
      await page.screenshot({ path: process.env.GUARDIAN_SMOKE_PHASE3_SCREENSHOT });
    }

    ledgerMode = "failed";
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector("#overall-state").textContent === "UNKNOWN");
    assert.equal(await risk.textContent(), "Unknown");
    assert.equal(await page.locator("#components .state").textContent(), "UNKNOWN");
    assert.equal(await page.locator("#incident-count").textContent(), "—");
    assert.match(await page.locator("#instance-ledger-coverage").textContent(), /unavailable/);
    assert.match(await page.locator("#dependency-coverage").textContent(), /^UNKNOWN/);
    assert.match(await page.locator("#anomalies").textContent(), /Insufficient evidence/);

    ledgerMode = "current";
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector("#instance-ledger td:nth-child(4)")?.textContent === "10");
    ledgerMode = "stale";
    analysisMode = "sparse";
    await page.clock.fastForward(15001);
    await page.waitForFunction(() => document.querySelector("#instance-ledger td:nth-child(4)")?.textContent === "Unknown");
    assert.match(await page.locator("#anomalies").textContent(), /not proof of normal operation/);
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
    assert.equal(await page.locator("#dependency-map tr").count(), 0);
    assert.equal(await page.locator("#anomalies tr").count(), 0);
    assert.deepEqual(errors, []);
    console.log("PASS: risk/dependency/anomaly masking, sparse evidence, source/cause separation, text-only investigation, retry, stale reads, mobile containment, disconnect, zero JavaScript errors");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
