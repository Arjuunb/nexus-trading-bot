import { test, expect, type Page } from "@playwright/test";
import { CALENDAR, mockApi } from "./mock";

// The app-wide realized P&L calendar. The mocked responses come from the real
// calendar service over real engine trades (e2e/fixtures/calendar.json).

const SEPT = "/#/calendar?month=2026-09";
const day = (page: Page, date: string) => page.locator(`button[data-date="${date}"]`);

test("Calendar is its own sidebar entry, next to (not inside) Performance", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");
  const groups = await page.locator("nav.nav .nav-group").evaluateAll((els) => els.map((g) => ({
    title: g.querySelector(".nav-group-title")?.textContent ?? null,
    items: [...g.querySelectorAll("button.nav-item")].map((b) => b.textContent),
  })));
  const performance = groups.find((g) => g.title === "Performance");
  expect(performance?.items).toEqual(["Portfolio", "Analytics"]);
  expect(groups.find((g) => g.title === "Records")?.items).toEqual(["Calendar", "Journal"]);
  const link = page.locator("nav.nav button.nav-item", { hasText: "Calendar" });
  await expect(link.locator("svg")).toHaveCount(1);
  await link.click();
  await expect(page).toHaveURL(/#\/calendar/);
  await expect(link).toHaveClass(/active/);
  await expect(page.getByRole("heading", { name: "Calendar", level: 1 })).toBeVisible();
});

test("month view shows real realized totals per currency, never added together", async ({ page }) => {
  await mockApi(page);
  await page.goto(SEPT);
  await expect(page.locator("#cal-month-title")).toHaveText("September 2026");
  const summary = page.getByRole("region", { name: "Month summary" });
  await expect(summary.getByText("+193.87 USDT").first()).toBeVisible();
  await expect(summary.getByText("+£21.57").first()).toBeVisible();
  await expect(page.getByText(/never added to USDT/)).toBeVisible();
  // state is in words and signs, not colour alone
  await expect(day(page, "2026-09-08")).toHaveAttribute("aria-label", /Loss, -97\.73 USDT, 2 closed trades/);
  await expect(day(page, "2026-09-10")).toHaveAttribute("aria-label", /Mixed currencies, \+£21\.57, -1\.12 USDT/);
  await expect(day(page, "2026-09-17")).toHaveAttribute("aria-label", /Profit, \+254\.37 USDT, 2 closed trades, 1 partial exit/);
  await expect(day(page, "2026-09-20")).toHaveAttribute("aria-label", /No trades/);
  await expect(day(page, "2026-09-20")).not.toContainText("0.00");
  // unrealized P&L is reported separately, never in the totals
  await expect(summary.getByText("Unrealized")).toBeVisible();
});

test("a day opens a drawer with source, strategy, time-of-day and trade detail", async ({ page }) => {
  await mockApi(page);
  await page.goto(SEPT);
  await day(page, "2026-09-17").click();
  const drawer = page.getByRole("dialog", { name: /Thursday 17 September 2026/ });
  await expect(drawer).toBeVisible();
  await expect(page).toHaveURL(/date=2026-09-17/);
  const sources = drawer.locator(".cal-breakdown");
  await expect(sources.getByText("Price Action Lab")).toBeVisible();
  await expect(sources.getByText("Native SMC · BTCUSDT")).toBeVisible();
  await expect(sources.getByText("+199.30 USDT")).toBeVisible();
  await expect(drawer.locator(".cal-tod")).toContainText("Night");
  await expect(drawer.locator(".cal-tod")).toContainText("22:00–06:59");
  const trades = drawer.locator(".cal-trades-table tbody tr");
  await expect(trades).toHaveCount(3);
  await expect(trades.first()).toContainText("00:10");
  await expect(trades.first()).toContainText("Partial exit");
  await expect(trades.first()).toContainText("-0.63 USDT paid");       // funding share, shown as a cost
  await expect(trades.nth(1)).toContainText("Not recorded");          // timeframe was never journaled
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(day(page, "2026-09-17")).toBeFocused();
});

test("keyboard moves through days and months", async ({ page }) => {
  await mockApi(page);
  await page.goto(SEPT);
  await day(page, "2026-09-17").focus();
  await page.keyboard.press("ArrowRight");
  await expect(day(page, "2026-09-18")).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(day(page, "2026-09-25")).toBeFocused();
  await page.keyboard.press("PageDown");
  await expect(page.locator("#cal-month-title")).toHaveText("October 2026");
  await expect(day(page, "2026-10-25")).toBeFocused();
  await expect(page.getByText("No closed trades in October 2026")).toBeVisible();
  await page.getByRole("button", { name: "Previous month" }).click();
  await expect(page.locator("#cal-month-title")).toHaveText("September 2026");
  await day(page, "2026-09-02").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: /2 September 2026/ })).toBeVisible();
});

test("source filter queries the backend and keeps the choice in the URL", async ({ page }) => {
  await mockApi(page);
  const asked: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/calendar/month")) asked.push(r.url()); });
  await page.goto(SEPT);
  await page.getByRole("radio", { name: /SMC Lab/ }).click();
  await expect(page).toHaveURL(/source=smc_lab/);
  await expect(page.getByRole("region", { name: "Month summary" }).getByText("-47.99 USDT").first()).toBeVisible();
  expect(asked.some((u) => u.includes("source=smc_lab"))).toBe(true);
  await page.getByRole("button", { name: /Clear filters/ }).click();
  await expect(page.getByRole("radio", { name: "All sources" })).toHaveAttribute("aria-checked", "true");
});

test("an unreadable API shows an error and no P&L", async ({ page }) => {
  await mockApi(page);
  await page.route((u) => u.pathname === "/calendar/month", (r) => r.fulfill({ status: 500, json: { detail: "ledger offline" } }));
  await page.goto(SEPT);
  await expect(page.getByText("The calendar could not be loaded.")).toBeVisible();
  await expect(page.getByText("ledger offline")).toBeVisible();
  await expect(page.locator(".cal-day-net")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Month summary" })).not.toContainText("USDT");
});

test("a source that cannot be read is named, not hidden", async ({ page }) => {
  await mockApi(page);
  const body = structuredClone(CALENDAR.month);
  body.diagnostics.sources.smc_lab = { ok: false, error: "OperationalError: database is locked" };
  await page.route((u) => u.pathname === "/calendar/month", (r) => r.fulfill({ json: body }));
  await page.goto(SEPT);
  await expect(page.getByText(/SMC Lab paper account could not be read \(OperationalError: database is locked\)/)).toBeVisible();
});

test("phone layout: no sideways scroll, drawer becomes a sheet with trade cards", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(SEPT);
  await expect(day(page, "2026-09-17")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.getByRole("region", { name: "Trading days this month" })).toBeVisible();
  await day(page, "2026-09-17").click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.locator(".cal-trade-cards li")).toHaveCount(3);
  await expect(drawer.locator(".cal-trades-table")).toBeHidden();
  const box = await drawer.boundingBox();
  expect(box && box.width).toBeLessThanOrEqual(390);
});

test("reduced motion turns the calendar animations off", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockApi(page);
  await page.goto(SEPT);
  await expect(page.locator(".cal-grid")).toHaveCSS("animation-name", "none");
  await day(page, "2026-09-17").click();
  await expect(page.locator(".cal-drawer")).toHaveCSS("animation-name", "none");
});

test("weekly totals, trade statistics and the daily chart come from the API", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(SEPT);
  const weeks = page.locator(".cal-weekcell");
  await expect(weeks).toHaveCount(5);
  await expect(weeks.nth(1)).toHaveAttribute("aria-label", /\+£21\.57, -98\.85 USDT, 4 closed trades/);
  await expect(weeks.nth(3)).toHaveAttribute("aria-label", /no trades/);
  const summary = page.getByRole("region", { name: "Month summary" });
  const usdt = summary.locator(".cal-sum").filter({ hasText: "USDT" }).last();
  await expect(usdt.locator(".cal-stats")).toContainText("Profit factor2.38");
  await expect(usdt.locator(".cal-stats")).toContainText("Average win+111.58 USDT");
  await expect(usdt.locator(".cal-stats")).toContainText("Largest loss-49.74 USDT");
  await expect(usdt.locator(".cal-stats")).toContainText("1 up · 2 down");
  // GBP had no loss: the profit factor is undefined, not infinite
  await expect(summary.locator(".cal-sum").filter({ hasText: "GBP" }).first().locator(".cal-stats")).toContainText("No losses");
  const charts = page.locator("figure.cal-chart");
  await expect(charts).toHaveCount(2);
  await expect(charts.filter({ hasText: "USDT" }).getByRole("img")).toHaveAttribute("aria-label", /month net \+193\.87 USDT/);
  await expect(charts.first().locator("canvas")).toHaveCount(1);
  // the largest day is shaded deepest; mixed-currency days are not shaded
  const heat = (d: string) => day(page, d).evaluate((el) => Number(getComputedStyle(el).getPropertyValue("--heat")));
  expect(await heat("2026-09-17")).toBeCloseTo(1, 3);
  expect(await heat("2026-09-02")).toBeLessThan(0.3);
  expect(await heat("2026-09-10")).toBe(0);
});

test("CSV export asks the backend for the month, then for a single day", async ({ page }) => {
  await mockApi(page);
  const asked: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/calendar/export.csv")) asked.push(r.url()); });
  await page.goto(SEPT + "&source=pa_lab");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: /Export CSV/ }).first().click();
  expect((await download).suggestedFilename()).toBe("realized-pnl_2026-09-01_2026-09-30.csv");
  expect(asked[0]).toContain("start=2026-09-01&end=2026-09-30");
  expect(asked[0]).toContain("source=pa_lab");
  await day(page, "2026-09-17").click();
  const dayDownload = page.waitForEvent("download");
  await page.getByRole("dialog").getByRole("button", { name: /Export CSV/ }).click();
  expect((await dayDownload).suggestedFilename()).toBe("realized-pnl_2026-09-17_2026-09-17.csv");
});
