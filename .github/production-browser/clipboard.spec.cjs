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

test('clipboard denial reaches the user and is never filtered by the harness', async ({ page, context }) => {
  await context.clearPermissions();
  await context.grantPermissions([], { origin: 'http://localhost:4173' });
  await mockApi(page);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/#/decision/1');
  // The browser refuses the write. The production UI catches it (src/lib/clipboard.ts, added in
  // e801b5d) and tells the user. Nothing is filtered here: an unhandled rejection would still
  // be recorded and fail the last assertion.
  await page.getByText('Linked decision · cycle #1', { exact: true }).locator('..').getByRole('button', { name: 'Copy link', exact: true }).click();
  await expect(page.getByText('Could not copy: the browser blocked clipboard access', { exact: true })).toBeVisible();
  await expect(page.getByText('Link copied', { exact: true })).toHaveCount(0);
  expect(errors).toEqual([]);
});
