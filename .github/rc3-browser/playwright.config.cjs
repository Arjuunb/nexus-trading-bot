// CI-only wrapper. Import the exact RC3 harness; never patch its application/tests.
const path = require('node:path');
const dashboard = path.join(process.env.GITHUB_WORKSPACE, 'source', 'automation-hub-dashboard');
const { defineConfig } = require(path.join(dashboard, 'node_modules', '@playwright/test'));
const original = require(path.join(dashboard, 'playwright.config.ts')).default;

if (original.use.baseURL !== 'http://localhost:4173') {
  throw new Error('CI clipboard permissions require the isolated mocked localhost origin');
}

module.exports = defineConfig({
  ...original,
  use: { ...original.use, permissions: ['clipboard-read', 'clipboard-write'] },
  webServer: { ...original.webServer, cwd: dashboard, reuseExistingServer: false },
  projects: [
    { name: 'accepted-rc3', testDir: path.join(dashboard, 'e2e') },
    { name: 'clipboard-harness', testDir: __dirname, testMatch: 'clipboard.spec.cjs' },
  ],
});
