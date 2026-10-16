import { expect, test } from "@playwright/test";
import { mockApi } from "./mock";

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
