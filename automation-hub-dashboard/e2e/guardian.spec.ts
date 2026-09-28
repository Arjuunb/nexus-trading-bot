import { expect, test } from "@playwright/test";
import { GUARDIAN, mockApi } from "./mock";

// The fixture is the real Guardian service's output over a fixed scene: an
// instance whose candles went stale (its feed FAILED, the instance BLOCKED by
// it), the SMC lab synchronised, the PA lab idle, and a journal that could not
// read a Supabase ledger.
const INSTANCE = "a3f9c2d1e8b74c0f";

test("Guardian is one sidebar entry and opens on the Command Center", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");
  await page.locator("nav.nav").getByRole("button", { name: "Guardian" }).click();
  await expect(page).toHaveURL(/#\/guardian/);
  const headline = page.getByTestId("guardian-headline");
  await expect(headline).toContainText(GUARDIAN.status.summary.state);
  // The verdict names what failed, from the components alone.
  await expect(headline).toContainText("has failed");
  await expect(page.getByTestId("guardian-group-instance")).toContainText("BLOCKED");
  await expect(page.getByTestId("guardian-group-journal")).toContainText("DEGRADED");
  await expect(page.getByText("READ-ONLY", { exact: true })).toBeVisible();
  // Only built tabs are offered.
  await expect(page.getByRole("tab")).toHaveText(["Command Center", "System Map", "Activity"]);
});

test("the system map says an instance is blocked by its feed, not broken", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=map");
  const node = page.getByTestId(`guardian-node-instance:${INSTANCE}`);
  await expect(node).toContainText("BLOCKED");
  await expect(node).toContainText(`Blocked by feed:instance:${INSTANCE}`);
  await expect(page.getByTestId(`guardian-node-feed:instance:${INSTANCE}`)).toContainText("FAILED");
  await expect(page.getByTestId("guardian-node-journal")).toContainText("ledger is not a local SQLite ledger");
  await node.click();
  await expect(page).toHaveURL(new RegExp(`tab=activity&component=instance%3A${INSTANCE}`));
  await expect(page.getByLabel("Component")).toHaveValue(`instance:${INSTANCE}`);
});

test("activity lists the recorded events with their state changes", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=activity");
  const table = page.getByTestId("guardian-events");
  await expect(table.locator("tbody tr")).toHaveCount(GUARDIAN.events.events.length);
  await expect(table).toContainText("health changed");
  await expect(table).toContainText("HEALTHY → BLOCKED");
  await expect(table).toContainText(`instance:${INSTANCE}`);
});
