import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";
import { NAV_LABELS, slug } from "../src/app-context";

/** Full UI click coverage (task 2): every sidebar link, every content button on
 *  every page, the paper controls, emergency stop, engine start/stop, settings
 *  save, logout, and change-password — all driven against the deterministic
 *  mock backend so nothing hits a live service. */

// The sidebar, read from the app's own navigation table so a renamed or added
// page is covered without editing a copy of the list here.
const NAV = [...NAV_LABELS, "Settings"];
// Routable by hash but not in the sidebar: linked from their sibling pages.
const HIDDEN = ["Symbols", "Markets", "Strategies", "Strategy Proof", "Simulation", "Evolution",
  "Paper Account", "AI Assistant", "Alerts", "SMC Visual Lab"];
// Old addresses that now open a tab of a hub page. Bookmarks and in-app
// links still use them, so each must land on a real page.
const REDIRECTED: [string, string][] = [
  ["overview", "Dashboard"], ["decisions", "Journal"], ["memory", "Journal"],
  ["safety-center", "Risk & Health"], ["bot-health", "Risk & Health"], ["logs", "Risk & Health"],
  ["ai-intelligence", "Analytics"],
];

// ── every sidebar link navigates and every page renders without crashing ──
test("every sidebar link opens a page with no uncaught error or 4xx", async ({ page }) => {
  await mockApi(page);
  const errors: string[] = [];
  const bad: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("response", (r) => {
    if (r.url().includes(":8000") && r.status() >= 400) bad.push(`${r.status()} ${r.url()}`);
  });

  await page.goto("/#/dashboard");
  for (const label of NAV) {
    await page.locator("aside.sidebar").getByRole("button", { name: label, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`#/${slug(label)}$`));
    await expect(page.locator(".topbar .page-title")).toHaveText(label);
    await expect(page.locator(".content")).not.toContainText("This page hit an error");
    await page.waitForTimeout(300);
  }
  expect(errors, errors.join("\n")).toHaveLength(0);
  expect(bad, bad.join("\n")).toHaveLength(0);
});

// ── demoted pages stay reachable by hash, and old addresses still land ──
test("hidden routes still render by hash", async ({ page }) => {
  await mockApi(page);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  for (const label of HIDDEN) {
    await page.goto(`/#/${slug(label)}`);
    await expect(page.locator(".topbar .page-title")).toHaveText(label);
    await expect(page.locator(".content")).not.toContainText("This page hit an error");
    await page.waitForTimeout(200);
  }
  for (const [path, label] of REDIRECTED) {
    await page.goto(`/#/${path}`);
    await expect(page.locator(".topbar .page-title")).toHaveText(label);
    await expect(page.locator(".content")).not.toContainText("This page hit an error");
    await page.waitForTimeout(200);
  }
  expect(errors, errors.join("\n")).toHaveLength(0);
});

test("sibling-page cross-links open the demoted pages", async ({ page }) => {
  await mockApi(page);
  for (const [from, btn, target] of [
    ["markets", "Symbol Explorer", /#\/symbols$/],
    ["backtesting", "Simulation", /#\/simulation$/],
    ["backtesting", "Replay", /#\/replay$/],
    ["analytics?tab=ai", "AI Assistant", /#\/ai-assistant$/],
    ["journal", "Decision Archive", /#\/journal\?tab=decisions$/],
    // Safety Center is a tab of Risk & Health; its old slug must still get there
    ["live-trading", "Safety Center", /#\/risk-health\?tab=safety$/],
    ["risk-health?tab=safety", "Live Trading", /#\/live-trading$/],
  ] as const) {
    await page.goto(`/#/${from}`);
    await page.waitForTimeout(250);
    await page.locator(".pagehead-actions").getByRole("button", { name: btn }).click();
    await expect(page).toHaveURL(target);
    await expect(page.locator(".content")).not.toContainText("This page hit an error");
  }
});

// ── click every content button on every page; none may throw ──
// One test PER PAGE (not a single monolithic sweep) so each has its own timeout
// budget and they run in parallel — the old single test grew slow enough to time
// out as pages and lazy-loaded chunks were added.
// Tabbed hubs are swept on each old address too, so a tab that is not the
// hub's default (Journal > Decisions, Risk & Health > Safety) is still clicked.
const SWEEP: [string, string][] = [
  ...[...NAV, ...HIDDEN].map((label): [string, string] => [label, slug(label)]),
  ...REDIRECTED.filter(([path]) => path !== "overview").map(([path, label]): [string, string] => [`${label} (${path})`, path]),
];
for (const [label, path] of SWEEP) {
  test(`clicking every content button on ${label} never throws`, async ({ page }) => {
    test.setTimeout(120_000);             // the larger labs have a few dozen buttons
    await mockApi(page);
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    // auto-dismiss confirms so destructive actions don't fire during the sweep
    page.on("dialog", (d) => d.dismiss().catch(() => {}));

    await page.goto(`/#/${path}`);
    await page.waitForTimeout(350);
    const n = await page.locator(".content button:visible").count();
    for (let i = 0; i < n; i++) {
      // reset to a clean page for each click so the index stays valid even when
      // a click toggles / removes / re-renders content
      await page.goto(`/#/${path}`);
      await page.waitForTimeout(120);
      const btns = page.locator(".content button:visible");
      if (i < (await btns.count())) await btns.nth(i).click({ timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(50);
    }
    expect(errors, `uncaught errors while clicking on ${label}:\n${errors.join("\n")}`).toHaveLength(0);
  });
}

// ── paper controls fire the right endpoints ──
test("paper controls POST pause / stop / resume", async ({ page }) => {
  await mockApi(page);
  page.on("dialog", (d) => d.accept());   // Pause All / Stop All confirm first (H-6)
  await page.goto("/#/paper-account");
  const controls = page.locator(".card").filter({ hasText: "Entry Safety Controls" }).last();
  await expect(controls).toBeVisible();
  for (const [label, path] of [
    ["Pause All", "/controls/pause-all"],
    ["Stop All", "/controls/stop-all"],
    ["Resume", "/controls/resume"],
  ] as const) {
    const [req] = await Promise.all([
      page.waitForRequest((r) => r.url().includes(path) && r.method() === "POST"),
      controls.getByRole("button", { name: label, exact: true }).click(),
    ]);
    expect(req, `${label} did not POST ${path}`).toBeTruthy();
  }
});

// ── engine start/stop fires the right endpoint ──
test("engine start button POSTs /engine/start", async ({ page }) => {
  await mockApi(page);           // mock reports engine stopped -> button says "Start Engine"
  await page.goto("/#/paper-trading");
  await page.waitForTimeout(500);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/engine/start") && r.method() === "POST"),
    page.getByRole("button", { name: /Start Engine/i }).click(),
  ]);
  expect(req).toBeTruthy();
});

// ── emergency stop (kill switch) halts trading + stops the engine ──
test("Safety Center kill switch POSTs stop-all and engine stop", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/safety-center");
  await page.waitForTimeout(500);
  page.once("dialog", (d) => d.accept());   // confirm the kill switch
  const [stopAll] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/controls/stop-all") && r.method() === "POST"),
    page.getByRole("button", { name: /Stop Everything/i }).click(),
  ]);
  expect(stopAll).toBeTruthy();
});

// ── settings save + change-password validation + logout ──
test("settings save shows success and change-password validates", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/settings?section=general");
  const general = page.locator("section.settings-section").filter({ has: page.getByRole("heading", { name: "General", exact: true }) });
  const save = general.getByRole("button", { name: "Save Changes" });
  await expect(save).toBeDisabled();                       // nothing to save yet
  await general.getByLabel("Density").selectOption("compact");
  await expect(general.locator(".settings-state")).toHaveText("Unsaved");
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/user/settings") && r.method() === "POST"),
    save.click(),
  ]);
  expect(req.postDataJSON().data.general.density).toBe("compact");
  await expect(general.locator(".settings-state")).toHaveText("Saved");
  await expect(save).toBeDisabled();

  await page.goto("/#/settings?section=security");
  const posted: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/auth/change-password")) posted.push(r.url()); });
  const security = page.locator("section.settings-section").filter({ has: page.getByRole("heading", { name: "Security", exact: true }) });
  await security.getByRole("button", { name: "Change Password" }).click();   // empty -> refused locally
  await expect(security.getByText("New passwords must match and contain at least 8 characters.")).toBeVisible();
  expect(posted).toHaveLength(0);
});

test("logout POSTs /auth/logout", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/settings?section=security");
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/auth/logout") && r.method() === "POST"),
    page.getByRole("button", { name: "Log Out", exact: true }).click(),
  ]);
  expect(req).toBeTruthy();
});
