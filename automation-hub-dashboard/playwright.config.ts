import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";

const bundledChromium = "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell";
const localChrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const linuxChrome = "/usr/bin/chromium";

// E2E clickability audit. Serves the production build via `vite preview` and
// drives it with a fully mocked backend (see e2e/mock.ts), so no API/auth is
// needed. Chromium is the environment's pre-installed browser.
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
    // Use the environment's pre-installed Chromium (our pinned @playwright/test
    // expects a different revision; do not download).
    launchOptions: {
      executablePath: process.env.PW_CHROMIUM
        || (process.platform === "darwin" ? localChrome : (existsSync(bundledChromium) ? bundledChromium : linuxChrome)),
      args: process.platform === "darwin" ? ["--headless=new"] : [],
    },
  },
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    url: "http://localhost:4173",
    timeout: 180_000,
    reuseExistingServer: true,
  },
});
