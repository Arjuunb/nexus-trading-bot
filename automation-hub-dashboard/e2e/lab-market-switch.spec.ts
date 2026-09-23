import { expect, test } from "@playwright/test";
import { mockApi, SMC_CHART, SMC_PAPER, PA_CHART, PA_PAPER } from "./mock";

test("PA failed refresh replaces cached healthy labels with a blocked error", async ({ page }) => {
  await mockApi(page);
  let fail = false;
  await page.route("**/research/price-action/live-chart?**", (route) => {
    if (fail) return route.fulfill({ status: 503, json: { detail: "feed reconciliation unavailable" } });
    return route.fallback();
  });
  await page.goto("/#/price-action-lab");
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
  await expect(page.locator(".pa-health-scope")).toHaveClass(/is-healthy/);
  fail = true;
  await expect(page.locator(".pa-health-scope")).toHaveClass(/is-stale/, { timeout: 10000 });
  await expect(page.locator(".pa-health-scope")).toContainText("ERROR");
  await expect(page.locator(".pa-health-scope")).toContainText("Paper execution: BLOCKED");
  await expect(page.locator(".pa-stream-truth")).toContainText("entries PAUSED");
});

test("SMC saves a mode change at once; a timeframe switch then carries the saved mode and clears old candles", async ({ page }) => {
  await mockApi(page);
  let session = structuredClone(SMC_PAPER.session);
  const changes: any[] = [];
  const mismatches: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/research/smc/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/configuration")) {
      const body = route.request().postDataJSON(); changes.push(body);
      await new Promise((resolve) => setTimeout(resolve, 400));
      session = { ...session, ...body };
      return route.fulfill({ json: { ...SMC_PAPER, session } });
    }
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { session } });
    if (url.pathname.endsWith("/paper")) return route.fulfill({ json: { ...SMC_PAPER, session } });
    if (url.pathname.endsWith("/live-chart")) {
      if (url.searchParams.get("timeframe") !== session.timeframe) mismatches.push(url.search);
      if (session.timeframe === "15m") await new Promise((resolve) => setTimeout(resolve, 1800));
      return route.fulfill({ json: SMC_CHART });
    }
    return route.fallback();
  });
  await page.goto("/#/smc-strategy-lab");
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
  await page.getByLabel("Paper operating mode").selectOption("automatic");
  // Saved the moment it is picked -- no Apply press, no unsaved draft.
  await expect.poll(() => changes.length).toBe(1);
  expect(changes[0]).toMatchObject({ timeframe: "5m", operating_mode: "automatic", risk_pct: .5 });
  await page.locator(".pa-timeframes").getByRole("button", { name: "15m", exact: true }).click();
  await expect(page.locator(".smc-chart-canvas")).toHaveCount(0);
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
  expect(changes).toHaveLength(2);
  // The switch resends what is SAVED, including the mode saved a moment ago.
  expect(changes[1]).toMatchObject({ timeframe: "15m", operating_mode: "automatic", risk_pct: .5,
    model_id: "SMC_M1_SWEEP_REVERSAL" });
  expect(mismatches).toEqual([]);
  expect(errors).toEqual([]);
  await expect(page.locator(".pa-error")).toHaveCount(0);
  await page.getByRole("button", { name: "Frozen review", exact: true }).click();
  expect(changes).toHaveLength(2); // review is read-only, never a session mutation
});

test("SMC rejected market change retains the saved chart and explains why", async ({ page }) => {
  await mockApi(page);
  await page.route("**/research/smc/sessions/current/configuration", (route) =>
    route.fulfill({ status: 409, json: { detail: "cannot change timeframe with an open position" } }));
  await page.goto("/#/smc-strategy-lab");
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
  await page.locator(".pa-timeframes").getByRole("button", { name: "15m", exact: true }).click();
  await expect(page.getByText(/cannot change timeframe with an open position/)).toBeVisible();
  await expect(page.locator(".pa-timeframes button.active")).toHaveText("5m");
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
  await expect(page.locator(".pa-error")).toHaveCount(0);
});

test("PA slow chart loads beyond polling interval, then switches to 15m", async ({ page }) => {
  await mockApi(page);
  let session = structuredClone(PA_PAPER.session);
  let active = 0, peak = 0;
  const changes: any[] = [];
  await page.route("**/research/price-action/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/configuration")) {
      const body = route.request().postDataJSON(); changes.push(body);
      session = { ...session, ...body };
      return route.fulfill({ json: { ...PA_PAPER, session } });
    }
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { session } });
    if (url.pathname.endsWith("/paper")) return route.fulfill({ json: { ...PA_PAPER, session } });
    if (url.pathname.endsWith("/live-chart")) {
      active++; peak = Math.max(peak, active);
      const identity = { ...session, session_id: session.id, request_id: url.searchParams.get("request_id") };
      await new Promise((resolve) => setTimeout(resolve, 3700));
      active--;
      return route.fulfill({ json: { ...PA_CHART, symbol: identity.symbol, timeframe: identity.timeframe, data_identity: identity,
        mtf_policy: { available_entry_timeframes: ["1m", "5m", "15m", "1h", "4h"] } } });
    }
    return route.fallback();
  });
  await page.goto("/#/price-action-lab");
  await expect(page.locator(".smc-chart-canvas")).toBeVisible({ timeout: 12000 });
  expect(peak).toBe(1);
  await page.locator(".pa-timeframes").getByRole("button", { name: "15m", exact: true }).click();
  await expect(page.locator(".pa-timeframes button.active")).toHaveText("15m");
  await expect(page.locator(".smc-chart-canvas")).toHaveCount(0);
  await expect(page.locator(".smc-chart-canvas")).toBeVisible({ timeout: 12000 });
  expect(changes[0]).toMatchObject({ timeframe: "15m", operating_mode: "signals_only" });
  await expect(page.locator(".pa-error")).toHaveCount(0);
});
