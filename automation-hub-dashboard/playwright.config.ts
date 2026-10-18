import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";

const bundledChromium = "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell";
const localChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const linuxChrome = "/usr/bin/chromium";

const researchAcceptance = process.env.NEXUS_RESEARCH_E2E === "1";
const externalAcceptance = process.env.NEXUS_RESEARCH_E2E_EXTERNAL === "1";
const python = process.env.NEXUS_TEST_PYTHON || (existsSync("../.venv/bin/python") ? "../.venv/bin/python" : "python");
const dashboardServer = {
  command: "npm run build && npm run preview -- --port 4173 --strictPort",
  url: "http://localhost:4173",
  timeout: 180_000,
  reuseExistingServer: true,
};
// Production-build browser checks. Most use deterministic API fixtures;
// explicit acceptance mode also starts the isolated loopback SQLite API.
export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:4173",
    headless: true,
    actionTimeout: 8_000,
    launchOptions: {
      executablePath: process.env.PW_CHROMIUM
        || [localChrome, bundledChromium, linuxChrome].find(path => existsSync(path)),
      args: process.platform === "darwin" ? ["--headless=new"] : [],
    },
  },
  // The reviewed RC3 clipboard wrapper extends the single dashboard server.
  // Its release runner owns the separate API fixture lifecycle explicitly.
  webServer: researchAcceptance && !externalAcceptance ? [dashboardServer, {
    command: `${python} e2e/fixtures/strategy_api.py`,
    url: "http://127.0.0.1:18005/__acceptance__/state",
    timeout: 60_000,
    reuseExistingServer: false,
  }] : dashboardServer,
});
