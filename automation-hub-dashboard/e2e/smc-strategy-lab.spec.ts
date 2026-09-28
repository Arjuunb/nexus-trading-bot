import { expect, test, type Page } from "@playwright/test";
import { mockApi, SMC_CHART } from "./mock";

async function panToOldest(page: Page) {
  const box = await page.locator(".smc-chart-canvas").boundingBox();
  if (!box) throw new Error("SMC chart has no visible bounds");
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.85, box.y + box.height * 0.5, { steps: 12 });
  await page.mouse.up();
}

for (const viewport of [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "tablet", width: 900, height: 1100 },
  { name: "mobile", width: 390, height: 844 },
]) {
  test(`SMC Strategy Lab is readable and operable on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockApi(page);
    await page.goto("/#/smc-strategy-lab");
    await expect(page.getByRole("heading", { name: "SMC Strategy Lab" })).toBeVisible();
    await expect(page.locator(".pa-lab.smc-strategy-lab")).toBeVisible();
    await expect(page.locator(".pa-workspace > .pa-sidebar")).toHaveCount(1);
    await expect(page.locator(".pa-workspace > .pa-main")).toHaveCount(1);
    await expect(page.locator(".pa-chart-shell")).toBeVisible();
    await expect(page.getByRole("button", { name: "Pine reference" })).toHaveCount(0);
    await expect(page.getByLabel("Native SMC chart workspace")).toBeVisible();
    const terminal = page.locator(".pa-bottom");
    await terminal.getByRole("button", { name: "journal 0", exact: true }).click();
    await expect(page.getByText("Immutable SMC decision journal")).toBeVisible();
    await terminal.getByRole("button", { name: "connection", exact: true }).click();
    await expect(terminal.locator(".pa-session span").filter({ hasText: "Overall health" }).getByText("SYNCHRONIZED", { exact: true })).toBeVisible();
    await expect(terminal.locator(".pa-session span").filter({ hasText: "New entries" }).getByText("CLOSED BARS ONLY", { exact: true })).toBeVisible();

    if (viewport.name === "mobile") {
      await page.getByRole("button", { name: "Controls" }).click();
      await expect(page.getByLabel("SMC Strategy controls")).toHaveClass(/is-open/);
    }

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(overflow).toBe(false);
  });
}

test("SMC Visual Lab and SMC Strategy Lab have separate sidebar routes and page identities", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/smc-visual-lab");

  await expect(page).toHaveURL(/#\/smc-visual-lab$/);
  await expect(page.getByRole("heading", { name: "Native SMC Visual Lab" })).toBeVisible();
  await expect(page.getByText("SMC PAPER ACCOUNT", { exact: true })).toHaveCount(0);

  await page.locator("aside.sidebar").getByRole("button", { name: "SMC Strategy Lab" }).click();
  await expect(page).toHaveURL(/#\/smc-strategy-lab$/);
  await expect(page.getByRole("heading", { name: "SMC Strategy Lab" })).toBeVisible();
  await expect(page.getByText("SMC session market", { exact: true })).toBeVisible();
  await expect(page.locator(".pa-lab.smc-strategy-lab")).toBeVisible();

  await page.locator(".pa-context-note").getByRole("button", { name: "SMC Visual Lab", exact: true }).click();
  await expect(page).toHaveURL(/#\/smc-visual-lab$/);
  await expect(page.getByRole("heading", { name: "Native SMC Visual Lab" })).toBeVisible();
});

test("SMC strategy chart starts legible while full labels remain available", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/smc-strategy-lab");
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
  const display = page.locator(".pa-sidebar .pa-details");
  await display.locator("summary").click();
  await expect(page.getByLabel("SMC chart layer preset")).toHaveValue("strategy");
  const labels = display.getByRole("checkbox", { name: "Labels" });
  await expect(labels).not.toBeChecked();
  await labels.check();
  await expect(labels).toBeChecked();
  await expect(page.locator(".smc-chart-canvas")).toBeVisible();
});

test("failed older-candle loading stops and offers an explicit retry", async ({ page }) => {
  await mockApi(page);
  let attempts = 0;
  await page.route("**/research/smc/live-history?**", async (route) => {
    attempts += 1;
    await route.abort("failed");
  });
  await page.goto("/#/smc-strategy-lab");
  const chart = page.locator(".smc-chart-canvas");
  await expect(chart).toBeVisible();
  await panToOldest(page);
  await expect.poll(() => attempts).toBe(1);
  await expect(chart.getByText("Loading history…")).toBeHidden();
  const retry = chart.getByRole("button", { name: "Retry older candles" });
  await expect(retry).toBeVisible();
  await page.waitForTimeout(3_000);
  expect(attempts).toBe(1);
  await retry.click();
  await expect.poll(() => attempts).toBe(2);
});

test("a stalled older-candle request times out instead of loading forever", async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    (window as any).__historyAttempts = 0;
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
      if (!url.includes("/research/smc/live-history?")) return originalFetch(input, init);
      (window as any).__historyAttempts += 1;
      return new Promise<Response>((_, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    };
  });
  await mockApi(page);
  await page.goto("/#/smc-strategy-lab");
  const chart = page.locator(".smc-chart-canvas");
  await panToOldest(page);
  await expect.poll(() => page.evaluate(() => (window as any).__historyAttempts)).toBe(1);
  await page.clock.fastForward(20_100);
  await expect(chart.getByText("Loading history…")).toBeHidden();
  await expect(chart.getByText("Older candles timed out.")).toBeVisible();
  await expect(chart.getByRole("button", { name: "Retry older candles" })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__historyAttempts)).toBe(1);
});

test("successful older-candle loading returns to the chart without another request", async ({ page }) => {
  await mockApi(page);
  let attempts = 0;
  const first = Date.parse(SMC_CHART.candles[0].timestamp);
  const older = Array.from({ length: 20 }, (_, index) => ({
    ...SMC_CHART.candles[0], timestamp: new Date(first - (20 - index) * 5 * 60_000).toISOString(),
  }));
  await page.route("**/research/smc/live-history?**", async (route) => {
    attempts += 1;
    await route.fulfill({ json: { candles: older, has_more_history: false } });
  });
  await page.goto("/#/smc-strategy-lab");
  const chart = page.locator(".smc-chart-canvas");
  await panToOldest(page);
  await expect.poll(() => attempts).toBe(1);
  await expect(chart.getByText("Loading history…")).toBeHidden();
  await expect(chart.getByRole("button", { name: "Retry older candles" })).toHaveCount(0);
  await page.waitForTimeout(2_000);
  expect(attempts).toBe(1);
});
