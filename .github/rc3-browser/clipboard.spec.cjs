const path = require('node:path');
const dashboard = path.join(process.env.GITHUB_WORKSPACE, 'source', 'automation-hub-dashboard');
const { test, expect } = require(path.join(dashboard, 'node_modules', '@playwright/test'));
const { mockApi } = require(path.join(dashboard, 'e2e/mock.ts'));

test('Decisions copies its actual link through the real clipboard without page errors', async ({ page, context }) => {
  await mockApi(page);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#/decision/1');
  expect(await page.evaluate(async () => (await navigator.permissions.query({ name: 'clipboard-read' })).state)).toBe('granted');
  const copied = page.url();
  await page.getByText('Linked decision · cycle #1', { exact: true }).locator('..').getByRole('button', { name: 'Copy link', exact: true }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(copied);
  expect(errors).toEqual([]);
});

test('clipboard denial remains observable and is never filtered by the harness', async ({ page, context }) => {
  await context.clearPermissions();
  await context.grantPermissions([], { origin: 'http://localhost:4173' });
  await mockApi(page);
  await page.goto('/#/decision/1');
  // Native denial in the real RC3 UI is visible; no pageerror suppression.
  const rejection = page.waitForEvent('pageerror');
  await page.getByText('Linked decision · cycle #1', { exact: true }).locator('..').getByRole('button', { name: 'Copy link', exact: true }).click();
  expect((await rejection).message).toMatch(/Write permission denied/);
});
