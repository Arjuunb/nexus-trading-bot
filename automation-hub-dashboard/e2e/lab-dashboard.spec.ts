import { expect, test } from "@playwright/test";
import { mockApi } from "./mock";

test("dashboard retains isolated SMC account scope after PA retirement", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");

  const pa = page.getByTestId("dashboard-price-action-lab");
  const smc = page.getByTestId("dashboard-smc-strategy-lab");
  await expect(pa).toHaveCount(0);

  await expect(smc).toContainText("SMC Bot");
  await expect(smc).toContainText("10,000 / 10,000 USDT");
  await expect(smc).toContainText("SMC_STRATEGY_LAB_ONLY");
  await expect(smc).toContainText("COMPLETED_CANDLE_RECONCILIATION");
  await expect(smc).toContainText("BLOCKED");

  await smc.getByRole("button", { name: "Open Lab" }).click();
  await expect(page).toHaveURL(/#\/smc-strategy-lab$/);
});
