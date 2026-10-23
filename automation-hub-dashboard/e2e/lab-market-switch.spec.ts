import { expect, test } from "@playwright/test";
import { mockApi, SMC_CHART, SMC_PAPER } from "./mock";

// The Price Action Lab is retired (PRICE_ACTION_LAB_ENABLED off); its page is
// unreachable, and flows.spec.ts checks that it never loads or polls.

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
