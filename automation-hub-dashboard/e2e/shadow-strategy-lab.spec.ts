import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./mock";

// Synthetic decision fixtures verify UI contracts, never forward profitability.
async function shadowApi(page: Page, mode: "ready" | "blocked" | "unauthorized" = "ready") {
  await mockApi(page);
  let registration: Record<string, any> | null = null;
  let gap = false;
  const actions: string[] = [];
  const candidate = { candidate_id: "fixture-shadow-candidate", strategy_id: "adaptive_trend_pullback", strategy_version: "1.0.0", config_fingerprint: "a".repeat(64), symbol: "TESTUSDT", timeframe: "5m" };
  await page.route("**/api/v2/strategy-intelligence/**", async route => {
    const url = new URL(route.request().url()), path = url.pathname;
    if (mode === "unauthorized") return route.fulfill({ status: 401, json: { detail: "Sign in required" } });
    if (path.endsWith("/research-candidates")) return route.fulfill({ json: { rows: [candidate] } });
    if (path.includes("/shadow/requirements/")) return route.fulfill({ json: { valid: mode !== "blocked", lookback_spec: mode === "blocked" ? null : { version: "fixture-lookback", required_candles: { "5m": 150, "1h": 70 } }, reviewed_source_hash: "b".repeat(64), errors: mode === "blocked" ? ["FULL_EFFECTIVE_CONFIGURATION_REQUIRED"] : [] } });
    if (path.endsWith("/shadow/registrations") && route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      expect(body.authorize).toBe(true); expect(body.authorization_reason).toBe("Reviewed observation only");
      expect(body.reviewed_source_hash).toBe("b".repeat(64)); expect(body).not.toHaveProperty("owner_id");
      registration = { ...candidate, registration_id: "fixture-registration", status: "DRAFT", readiness: "WARMING_UP", venue: "binance_usdm", market_type: "perpetual", feed_freshness: "UNKNOWN" };
      return route.fulfill({ status: 201, json: registration });
    }
    if (path.endsWith("/shadow/registrations")) return route.fulfill({ json: { rows: registration ? [registration] : [] } });
    if (route.request().method() === "POST" && registration) {
      const action = path.split("/").pop()!; actions.push(action);
      if (action === "validate") registration.status = "READY";
      if (action === "start" || action === "resume") { registration.status = "OBSERVING"; registration.readiness = "READY"; registration.feed_freshness = "FRESH"; }
      if (action === "pause") registration.status = "PAUSED";
      if (action === "resume") gap = true;
      if (action === "stop") registration.status = "STOPPED";
      return route.fulfill({ json: registration });
    }
    if (path.endsWith("/decisions")) return route.fulfill({ json: { rows: registration?.status === "OBSERVING" ? [{ decision_id: "fixture-decision", candle_close_timestamp: "2026-10-11T12:00:00Z", observed_at: "2026-10-11T12:00:01Z", status: "NO_SIGNAL", data_freshness: "FRESH", evaluation_latency_ms: 18, evidence_completeness: "COMPLETE", rejection_reasons: ["Fixture condition not met"], candle_hash: "c".repeat(64) }] : [] } });
    if (path.endsWith("/coverage")) return route.fulfill({ json: { eligible_opportunities: registration?.status === "OBSERVING" ? 1 : 0, signal_count: 0, data_block_count: 0, observation_gaps: gap ? 1 : 0, evidence_completeness: gap ? "PARTIAL" : "UNKNOWN" } });
    if (path.endsWith("/comparison")) return route.fulfill({ json: { active_state: "NO_DECISION_RECORDED", matched_opportunities: 0, agreement_rate: null, warning: "Signal agreement is not trading performance. Missing active evidence is not NO_SIGNAL." } });
    if (path.endsWith("/audit")) return route.fulfill({ json: { rows: gap ? [{ event_id: "fixture-gap", action: "OBSERVATION_GAP", timestamp: "2026-10-11T12:10:00Z", payload: { reason: "PAUSED_INTERVAL", backfilled: false } }] : [] } });
    if (registration && path.endsWith("/" + registration.registration_id)) return route.fulfill({ json: registration });
    return route.fulfill({ json: { rows: [], cohorts: [] } });
  });
  return actions;
}

test("shadow lab authorization, live decision evidence, lifecycle and gaps", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  page.on("console", m => { if (m.type() === "error" || m.type() === "warning") errors.push(m.text()); });
  const actions = await shadowApi(page);
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await expect(page.getByRole("heading", { name: "Shadow Strategy Lab", exact: true })).toBeVisible();
  await page.getByLabel("Shadow research candidate").selectOption("fixture-shadow-candidate");
  await expect(page.getByRole("button", { name: "Register shadow candidate" })).toBeDisabled();
  await page.getByLabel("Shadow authorization reason").fill("Reviewed observation only");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Register shadow candidate" }).click();
  await expect(page.getByText("Observation status: DRAFT", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "VALIDATE", exact: true }).click();
  await expect(page.getByText("Observation status: READY", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "START", exact: true }).click();
  await expect(page.getByText("Observation status: OBSERVING", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Shadow decision timeline" }).getByText("NO_SIGNAL", { exact: true })).toBeVisible();
  await expect(page.getByText("Feed freshness: FRESH", { exact: false })).toBeVisible();
  await expect(page.getByText("Signal agreement: UNAVAILABLE", { exact: true })).toBeVisible();
  await expect(page.getByText("Active evidence: NO_DECISION_RECORDED", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "PAUSE", exact: true }).click();
  await expect(page.getByText("Observation status: PAUSED", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "RESUME", exact: true }).click();
  await expect(page.getByText("Evidence completeness: PARTIAL", { exact: true })).toBeVisible();
  await expect(page.getByText(/OBSERVATION_GAP/)).toBeVisible();
  await page.getByRole("button", { name: "STOP", exact: true }).click();
  await expect(page.getByText("Observation status: STOPPED", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "START", exact: true })).toBeDisabled();
  expect(actions).toEqual(["validate", "start", "pause", "resume", "stop"]);
  await page.screenshot({ path: "/tmp/sprint7-shadow-lab-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  // Wait for the existing off-canvas sidebar transition, not its intermediate
  // screenshot frame, before asserting/inspecting the phone layout.
  await expect.poll(() => page.locator("aside.sidebar").evaluate(e => e.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "/tmp/sprint7-shadow-lab.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("unsupported frozen evidence stays blocked, no green readiness", async ({ page }) => {
  await shadowApi(page, "blocked");
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await page.getByLabel("Shadow research candidate").selectOption("fixture-shadow-candidate");
  await expect(page.getByText("FULL_EFFECTIVE_CONFIGURATION_REQUIRED", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Register shadow candidate" })).toBeDisabled();
  await expect(page.getByText("No committed shadow decisions.")).toHaveCount(0);
});

test("unauthorized shadow access has no observations or mutation controls", async ({ page }) => {
  await shadowApi(page, "unauthorized");
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await expect(page.getByRole("alert").first()).toContainText("HTTP 401");
  await expect(page.getByRole("button", { name: "Register shadow candidate" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "START", exact: true })).toHaveCount(0);
});
