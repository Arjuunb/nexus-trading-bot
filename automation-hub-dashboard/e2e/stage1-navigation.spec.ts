import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";

const NAV = [
  "Dashboard",
  "Trading Instances", "Instance Visual Lab", "Strategy Studio", "Paper Trading", "Live Trading",
  "Price Action Lab", "SMC Strategy Lab", "SMC Agent", "Adaptive MTF Lab", "Replay", "Backtesting",
  "Optimization Lab", "Forward Validation",
  "Portfolio", "Analytics",
  "Calendar", "Journal",
  "Market Data", "Risk & Health",
];

test("Stage 1 sidebar has a scrollable nav and fixed Settings footer", async ({ page }) => {
  await mockApi(page);
  for (const viewport of [{ width: 1366, height: 768 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 }, { width: 1093, height: 614 }, { width: 910, height: 512 }]) {
    await page.setViewportSize(viewport);
    await page.goto("/#/dashboard");
    const sidebar = page.locator("aside.sidebar"); const nav = sidebar.locator("nav.nav"); const footer = sidebar.locator(".sidebar-footer");
    await expect(sidebar).toBeVisible(); await expect(footer.locator("button.nav-item")).toBeVisible();
    expect(await nav.locator("button.nav-item").allTextContents()).toEqual(NAV);
    expect(await page.evaluate(() => ({ sidebar: getComputedStyle(document.querySelector("aside.sidebar")!).overflowY, nav: getComputedStyle(document.querySelector("nav.nav")!).overflowY }))).toEqual({ sidebar: "hidden", nav: "auto" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const box = await footer.boundingBox(); expect(box).not.toBeNull(); expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(viewport.height);
  }
});

test("Settings Centre exposes working controls and keeps unsupported trading locked", async ({ page }) => {
  await mockApi(page); await page.goto("/#/settings?section=trading");
  await expect(page.getByText("These defaults are applied only when creating a new Trading Instance.")).toBeVisible();
  await expect(page.locator('input[value="Spot (only supported Trading Instance market)"]')).toBeVisible();
  await page.locator(".settings-nav").getByRole("button", { name: "Live Trading" }).click();
  await expect(page.getByText("LOCKED")).toBeVisible();
  await expect(page.getByRole("button", { name: /Enable Live Trading/i })).toHaveCount(0);
  await page.locator(".settings-nav").getByRole("button", { name: "Advanced" }).click();
  await expect(page.getByText("These settings control the legacy autonomous engine and do not configure Trading Instances.", { exact: true })).toBeVisible();
});

test("collapsed sidebar and mobile drawer preserve the Settings footer", async ({ page }) => {
  await mockApi(page); await page.setViewportSize({ width: 1440, height: 900 }); await page.goto("/#/dashboard");
  await page.getByLabel("Toggle menu").click(); await expect(page.locator(".app")).toHaveClass(/sidebar-collapsed/); await expect(page.locator(".sidebar-footer button")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 }); await page.getByLabel("Toggle menu").click(); await expect(page.locator(".app")).toHaveClass(/mobile-nav-open/); await expect(page.locator(".sidebar-footer").getByText("Settings")).toBeVisible();
});

test("legacy routes replace old bookmarks with canonical tab URLs", async ({ page }) => {
  await mockApi(page);
  const redirects = {
    "fleet-manager": "trading-instances\\?tab=fleet", "grid-dca": "strategy-studio\\?tab=grid-dca",
    allocation: "portfolio\\?tab=allocation", "ai-intelligence": "analytics\\?tab=ai",
    decisions: "journal\\?tab=decisions", memory: "journal\\?tab=memory",
    "risk-manager": "risk-health\\?tab=risk", "bot-health": "risk-health\\?tab=health", logs: "risk-health\\?tab=logs",
  };
  for (const [oldRoute, target] of Object.entries(redirects)) {
    await page.goto(`/#/${oldRoute}`); await expect(page).toHaveURL(new RegExp(`#/${target}$`));
  }
});

test("section tabs deep-link and participate in browser history", async ({ page }) => {
  await mockApi(page); await page.goto("/#/portfolio?tab=overview");
  await page.getByRole("tab", { name: "Allocation" }).click(); await expect(page).toHaveURL(/#\/portfolio\?tab=allocation$/);
  await page.goBack(); await expect(page).toHaveURL(/#\/portfolio\?tab=overview$/); await expect(page.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Positions" }).focus(); await page.keyboard.press("ArrowRight"); await expect(page).toHaveURL(/tab=allocation$/);
});
