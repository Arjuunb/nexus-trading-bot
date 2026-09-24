import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";

test("Security settings show key custody, redaction and a verifiable audit log", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await mockApi(page);
  await page.goto("/#/settings?section=security");

  const keys = page.locator("section", { has: page.getByRole("heading", { name: "Exchange keys" }) });
  await expect(keys.getByText("AES-256-GCM envelope (per-tenant data keys)")).toBeVisible();
  await expect(keys.getByText("…9Q2x")).toBeVisible();
  await expect(keys.getByText("no withdrawals")).toBeVisible();
  await expect(keys.getByText(/5 live secrets guarded/)).toBeVisible();
  // key fields are password inputs and nothing pre-fills them
  await expect(keys.locator('input[type="password"]')).toHaveCount(2);
  for (const input of await keys.locator('input[type="password"]').all()) await expect(input).toHaveValue("");

  const audit = page.locator("section", { has: page.getByRole("heading", { name: "Audit log" }) });
  await expect(audit.locator(".audit-table tbody tr")).toHaveCount(3);
  await audit.getByRole("button", { name: "Verify chain" }).click();
  await expect(audit.getByText("Intact · 3 entries checked")).toBeVisible();
  await audit.locator(".audit-table tbody tr").first().click();
  await expect(audit.locator(".audit-detail")).toContainText("[redacted]");

  await expect(audit.getByText("Up to date · https://siem.example.com")).toBeVisible();

  const apiKeys = page.locator("section", { has: page.getByRole("heading", { name: "API keys" }) });
  await expect(apiKeys.getByText("nxs_a1b2c3d4_…")).toBeVisible();
  await expect(apiKeys.getByRole("button", { name: "Revoke" })).toBeVisible();

  const backups = page.locator("section", { has: page.getByRole("heading", { name: "Backups" }) });
  await expect(backups.getByText("On · AES-256-GCM, sealed with the master key")).toBeVisible();
  await expect(backups.getByText(/1 from before encryption/)).toBeVisible();
  await backups.getByRole("button", { name: "Check the latest restores" }).click();
  await expect(backups.getByText("Restores cleanly: decrypted, and all 2 databases open.")).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("Security settings survive an API that returns nothing useful", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await mockApi(page);
  await page.route((url) => url.pathname.startsWith("/security/"), (route) => route.fulfill({ json: {} }));
  await page.goto("/#/settings?section=security");
  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Exchange keys" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Backups" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();
  expect(errors).toEqual([]);
});
