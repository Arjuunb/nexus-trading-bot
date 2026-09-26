import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";
import { NAV_LABELS, slug } from "../src/app-context";

// The sidebar as the app defines it, so the list cannot drift from the app.
const NAV = [...NAV_LABELS, "Settings"];

test("sidebar nav — every item navigates and marks itself active", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");
  await page.waitForTimeout(500);

  for (const label of NAV) {
    const item = page.locator("aside.sidebar").getByRole("button", { name: label, exact: true });
    await item.click();
    await expect(page).toHaveURL(new RegExp(`#/${slug(label)}$`));
    await expect(item).toHaveClass(/active/);
  }
});

test("top bar icons navigate", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");
  // the bell opens recent notifications; "View all" is the way to the Alerts page
  await page.locator(".topbar").getByRole("button", { name: /^Notifications/ }).click();
  await page.getByRole("button", { name: "View all", exact: true }).click();
  await expect(page).toHaveURL(/#\/alerts$/);
  await page.locator(".topbar").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/#\/settings/);
});

test("Price Action Visual Lab — public stream truth and protected paper modes", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/price-action-lab");
  await expect(page.getByRole("heading", { name: "Price Action Visual Lab" })).toBeVisible();
  await expect(page.locator(".pa-titlebar")).toContainText("ISOLATED FORWARD-PAPER");
  await expect(page.locator(".pa-safety")).toContainText("LIVE ROUTING DISABLED");
  await expect(page.locator(".pa-stream-truth")).toContainText("SYNCHRONIZED");
  await expect(page.locator(".pa-stream-truth")).toContainText("Transport CONNECTED");
  await expect(page.locator(".pa-health-scope")).toContainText("Decision readiness: CLOSED-BAR ELIGIBLE");
  const ticker = page.locator(".smc-live-price-ticker");
  await expect(ticker).toBeVisible();
  await expect(ticker).not.toHaveClass(/stale/);
  await expect(ticker).toHaveAttribute(
    "aria-label", /Live price .* candle closes in \d{2}:\d{2}/);

  // Picking the mode saves it; there is no separate Apply step.
  const [request] = await Promise.all([
    page.waitForRequest((row) => row.url().includes("/research/price-action/sessions/current/configuration") && row.method() === "POST"),
    page.getByLabel("Paper operating mode").selectOption("automatic"),
  ]);
  expect(request.postDataJSON().operating_mode).toBe("automatic");
  await expect(page.locator(".toast.success")).toBeVisible();

  await page.getByRole("button", { name: /connection/i }).click();
  await expect(page.getByText("CLOSED BARS ONLY")).toBeVisible();
  await expect(page.getByText("DISABLED", { exact: true })).toBeVisible();
});

test("Price Action Visual Lab — rapid market switches settle on one session identity", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/price-action-lab");
  const symbol = page.getByLabel("Price Action session symbol");
  await expect(symbol).toHaveValue("BTCUSDT");
  await symbol.selectOption("ETHUSDT");
  await expect(symbol).toHaveValue("ETHUSDT");
  await expect(page.locator(".pa-chart-head")).toContainText("ETHUSDT · 5m");

  await page.getByRole("button", { name: "15m", exact: true }).click();
  await expect(page.locator(".pa-chart-head")).toContainText("ETHUSDT · 15m");
  await expect(page.locator(".pa-stream-truth")).toContainText("SYNCHRONIZED");
});

test("Price Action Visual Lab — chart presets and setup focus remain audit-safe", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto("/#/price-action-lab");
  const preset = page.getByLabel("Chart layer preset");
  await expect(preset).toHaveValue("clean");
  await preset.selectOption("debug");
  await expect(page.getByText(/All zones, setups, orders and trades remain/)).toBeVisible();
  await page.getByRole("button", { name: "orders", exact: false }).click();
  await expect(page.getByText("Pending paper audit")).toBeVisible();
  await expect(page.getByText("Research-engine orders · not paper broker orders")).toBeVisible();
  await page.getByRole("button", { name: "Reconcile strategy orders" }).click();
  await expect(page.getByText("Pending paper orders are already reconciled")).toBeVisible();
  await preset.selectOption("clean");
  await page.locator(".pa-chart-shell").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("price-action-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 768, height: 900 });
  await page.screenshot({ path: testInfo.outputPath("price-action-responsive.png"), fullPage: true });
});

test("Settings > Discard Changes puts back what is saved", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/settings?section=general");
  const general = page.locator("section.settings-section").filter({ has: page.getByRole("heading", { name: "General", exact: true }) });
  const density = general.getByLabel("Density");
  await expect(density).toHaveValue("comfortable");
  await density.selectOption("compact");
  await expect(general.getByRole("button", { name: "Save Changes" })).toBeEnabled();
  await general.getByRole("button", { name: "Discard Changes" }).click();
  await expect(density).toHaveValue("comfortable");
  await expect(general.locator(".settings-state")).toHaveText("Saved");
  await expect(general.getByRole("button", { name: "Save Changes" })).toBeDisabled();
});

test("Change Password validates short and mismatched input before sending anything", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/settings?section=security");
  const posted: string[] = [];
  page.on("request", (r) => { if (r.url().includes("/auth/change-password")) posted.push(r.url()); });
  const security = page.locator("section.settings-section").filter({ has: page.getByRole("heading", { name: "Security", exact: true }) });
  const message = security.getByText("New passwords must match and contain at least 8 characters.");
  await security.getByLabel("Current password").fill("old-password");
  await security.getByLabel("New password").fill("short");
  await security.getByLabel("Confirm password").fill("short");
  await security.getByRole("button", { name: "Change Password" }).click();
  await expect(message).toBeVisible();
  await security.getByLabel("New password").fill("long-enough-1");
  await security.getByLabel("Confirm password").fill("long-enough-2");
  await security.getByRole("button", { name: "Change Password" }).click();
  await expect(message).toBeVisible();
  expect(posted).toHaveLength(0);
});

test("Log out from the account menu asks first, then POSTs /auth/logout", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("button", { name: "Log out", exact: true }).first().click();
  const confirm = page.locator(".pm-logout-confirm");
  await expect(confirm).toBeVisible();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/auth/logout") && r.method() === "POST"),
    confirm.click(),
  ]);
  expect(req).toBeTruthy();
});

test("no unexpected 4xx/5xx from the app's own requests during a page tour", async ({ page }) => {
  await mockApi(page);
  const bad: string[] = [];
  page.on("response", (r) => {
    const u = r.url();
    if (u.includes(":8000") && r.status() >= 400) bad.push(`${r.status()} ${u}`);
  });
  for (const label of ["Overview", "Analytics", "Settings", "Risk Manager", "Evolution"]) {
    await page.goto(`/#/${slug(label)}`);
    await page.waitForTimeout(700);
  }
  expect(bad, bad.join("\n")).toHaveLength(0);
});

test("Journal page — lists journaled trades and expands the full decision journal", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/journal");
  await page.waitForTimeout(700);
  // page + a journaled trade row render
  await expect(page.getByRole("heading", { name: /Bot Trade Journal/i })).toBeVisible();
  await expect(page.locator("table.data-table").first()).toContainText("BTCUSDT");
  // expand the decision journal for that trade
  await page.getByRole("button", { name: /^View$/ }).first().click();
  await page.waitForTimeout(400);
  // the 9-section panel is now visible with real captured data + honesty markers
  await expect(page.getByText("1 · Trade Summary")).toBeVisible();
  await expect(page.getByText(/Not checked/).first()).toBeVisible();
  await expect(page.getByText(/never bypassed/i)).toBeVisible();
  // evolution memory table shows the staged setup
  await expect(page.getByText(/Evolution Memory/i)).toBeVisible();
  await expect(page.locator("table.data-table").last()).toContainText("early-signal");
});

test("Memory — remembers trades, coaches from real data, and keeps honesty markers", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/memory");
  await page.waitForTimeout(700);
  await expect(page.locator("h1.pagehead-title", { hasText: "Memory" })).toBeVisible();
  // knowledge base coaching statement (sample-gated, real numbers)
  await expect(page.getByText(/London session/)).toBeVisible();
  await expect(page.getByText(/early-signal/).first()).toBeVisible();
  // mistake library shows a repeated mistake
  await expect(page.getByText(/Chased the entry/)).toBeVisible();
  await expect(page.getByText("repeated").first()).toBeVisible();
  // trade timeline row + expand the full 8-category memory
  await expect(page.locator("table.data-table").filter({ hasText: "BTCUSDT" }).first()).toBeVisible();
  await page.getByRole("button", { name: /View/ }).first().click();
  await page.waitForTimeout(300);
  // honesty markers survive — uncaptured/unchecked fields are never faked
  await expect(page.getByText(/Not checked/).first()).toBeVisible();
  await expect(page.getByText(/not captured/).first()).toBeVisible();
  // Growth Journey: performance memory from remembered trades
  await expect(page.getByText("Growth Journey")).toBeVisible();
  await expect(page.getByText("13W\u20138L \u00b7 61.9%")).toBeVisible();
  await expect(page.getByText(/early sample/)).toBeVisible();
  // AI reflection is present
  await expect(page.getByText(/A-grade win/)).toBeVisible();
  // notes field for the manual journal entry
  await expect(page.getByPlaceholder(/FOMO/)).toBeVisible();
});

test("Memory — natural-language ask routes through the query endpoint", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/memory");
  await page.waitForTimeout(600);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/trade-memory/ask?q=") && r.method() === "GET"),
    (async () => {
      await page.getByLabel("Search memory").fill("show all losing BTC trades");
      await page.getByRole("button", { name: /Search/ }).click();
    })(),
  ]);
  expect(req).toBeTruthy();
  await expect(page.getByText(/Found 1 loss BTCUSDT trades/)).toBeVisible();
});

test("Header control strip — instance timeframe, strategy and risk are interactive", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });   // the risk chip folds into ••• below 1350px
  await mockApi(page);
  // One stopped instance: execution changes need no restart confirmation.
  const instance = {
    id: "inst-1", symbol: "BTCUSDT", strategy_key: "brain", strategy_label: "Decision Brain",
    strategy_version: "1.0", timeframe: "5m", state: "stopped", mode: "trading",
    risk_per_trade_pct: 0.005, capital_allocation: 1000, max_open_positions: 3,
  };
  await page.route((url) => url.host === "localhost:8000" && url.pathname === "/instances", (route) =>
    route.fulfill({ json: { instances: [instance], active_slots: 0, max_active_slots: 8, total_current_equity: 10000,
      paper_account_capital: 10000, available_paper_capital: 9000,
      current_global_risk_amount: 0, max_global_risk_amount: 500, total_open_positions: 0,
      global_risk_status: "healthy", global_risk_message: "Within configured limits", market_data_status: "idle" } }));
  await page.goto("/#/dashboard");
  const strip = page.locator(".hdr-controls");
  await expect(strip).toContainText("BTCUSDT");

  // timeframe: the current one is marked; picking another PATCHes the instance
  await strip.getByRole("button", { name: "5m" }).click();
  const timeframes = page.getByRole("menu", { name: "Execution timeframe" });
  await expect(timeframes.locator(".tf-btn.active")).toHaveText("5m");
  const [tfReq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith("/instances/inst-1") && r.method() === "PATCH"),
    timeframes.getByRole("button", { name: "15m", exact: true }).click(),
  ]);
  expect(tfReq.postDataJSON()).toEqual({ timeframe: "15m" });
  await expect(page.locator(".toast.success", { hasText: "Timeframe change to 15m applied" })).toBeVisible();

  // strategy menu lists the catalogue with the running one marked
  await strip.getByRole("button", { name: /Decision Brain 1\.0/ }).click();
  await expect(page.getByRole("menu", { name: "Instance strategy" }).locator(".hdr-item.active")).toContainText("Decision Brain 1.0");
  await page.keyboard.press("Escape");

  // risk: a preset PATCHes risk_per_trade_pct
  await strip.getByRole("button", { name: /Risk/ }).first().click();
  const [riskReq] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith("/instances/inst-1") && r.method() === "PATCH"),
    page.getByRole("menu", { name: "Risk per trade" }).getByRole("button", { name: "0.75%" }).click(),
  ]);
  expect(riskReq.postDataJSON()).toEqual({ risk_per_trade_pct: 0.0075 });
  await expect(page.locator(".toast.success", { hasText: "Risk updated" })).toBeVisible();

  // live stays gated behind the Safety Center
  await strip.getByRole("button", { name: /paper/i }).first().click();
  await expect(page.getByRole("menu", { name: "Trading mode" })).toContainText("gated");
});

test("Decisions — every cycle explained: checklist, scores, reasons, recommendation", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/decisions");               // old address: now Journal > Decisions
  await page.waitForTimeout(700);
  await expect(page).toHaveURL(/#\/journal\?tab=decisions$/);
  await expect(page.locator("h1.pagehead-title", { hasText: "Decision Archive" })).toBeVisible();
  // cycle rows render with decision badges (SKIP + WAIT from the mock)
  await expect(page.getByText("SKIP").first()).toBeVisible();
  await expect(page.getByText("WAIT").first()).toBeVisible();
  // expand the full report
  await page.getByRole("button", { name: /View/ }).first().click();
  await page.waitForTimeout(400);
  // a skip is never silent: explicit reasons + recommendation
  await expect(page.getByText(/Risk:reward only 1.4/)).toBeVisible();
  await expect(page.getByText(/Wait for a pullback/)).toBeVisible();
  // rule-by-rule checklist with PASS/FAIL
  await expect(page.getByText("PASS").first()).toBeVisible();
  await expect(page.getByText("FAIL").first()).toBeVisible();
  // five-category confidence breakdown
  await expect(page.getByText("Supply/Demand")).toBeVisible();
  await expect(page.getByText(/Confidence 54\/100/)).toBeVisible();
});

test("Safety Center — live readiness is locked and the kill-switch test verifies", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/safety-center");
  await page.waitForTimeout(600);
  await expect(page.getByRole("heading", { name: /Live Trading Readiness/i })).toBeVisible();
  await expect(page.getByText("Live trading is LOCKED.")).toBeVisible();
  await expect(page.getByText(/Paper trading track record/)).toBeVisible();
  // kill-switch test: accept the confirm dialog, expect the verified toast
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: /Test Emergency Stop/i }).click();
  await expect(page.locator(".toast.success")).toBeVisible();
});

test("Logs — skipped trades are listed with failed gate and expandable snapshot", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/logs");
  await page.waitForTimeout(600);
  await expect(page.getByRole("heading", { name: /Skipped Trades/i })).toBeVisible();
  // the failed gate + reason render
  await expect(page.getByText("Max open positions (3) reached")).toBeVisible();
  // expand the market snapshot for the row that has one
  await page.getByRole("button", { name: /^View$/ }).first().click();
  await page.waitForTimeout(300);
  await expect(page.getByText(/regime/i).first()).toBeVisible();
});

test("Bot Health — shows real engine/feed/risk/watchdog status", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/bot-health");
  await page.waitForTimeout(600);
  await expect(page.locator("h1.pagehead-title", { hasText: "Bot Health" })).toBeVisible();
  await expect(page.getByText(/Decision Brain/).first()).toBeVisible();
  // last rejected signal (from the skip log) surfaces here
  await expect(page.getByText("Max open positions (3) reached")).toBeVisible();
  // watchdog + no-errors states render honestly
  await expect(page.getByText(/all clear/i)).toBeVisible();
  await expect(page.getByText(/No errors logged/i)).toBeVisible();
});

test("Strategy Proof — shows risk-adjusted stats, walk-forward, and breakdowns", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/strategy-proof");
  await page.waitForTimeout(600);
  await expect(page.locator("h1.pagehead-title", { hasText: "Strategy Proof" })).toBeVisible();
  // risk-adjusted ratios surface (computed from real R returns)
  await expect(page.getByText("Sharpe").first()).toBeVisible();
  await expect(page.getByText("Sortino").first()).toBeVisible();
  // per-symbol / per-session breakdowns render real rows
  await expect(page.getByText(/Per-Symbol Performance/i)).toBeVisible();
  await expect(page.getByText("BTCUSDT").first()).toBeVisible();
  // walk-forward on demand
  await page.getByRole("button", { name: /Run walk-forward/i }).click();
  await page.waitForTimeout(300);
  await expect(page.getByText(/folds positive/i)).toBeVisible();
});

test("Strategy Proof — Paper Validation panel shows readiness and keeps live locked", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/strategy-proof");
  await page.waitForTimeout(600);
  await expect(page.getByRole("heading", { name: /Paper Validation/i })).toBeVisible();
  // eligibility verdict + the never-unlock guarantee
  await expect(page.getByText(/NOT ELIGIBLE/)).toBeVisible();
  await expect(page.getByText(/Live trading LOCKED\./)).toBeVisible();
  await expect(page.getByText(/never auto-enables real-money trading/i)).toBeVisible();
  // real sample-size reason surfaces
  await expect(page.getByText(/Need ≥ 30 closed paper trades/)).toBeVisible();
});

test("Logs — skipped trades show a rejection category", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/logs");
  await page.waitForTimeout(500);
  // category column badges (risk / safety) render
  await expect(page.locator("table.data-table").filter({ hasText: "Failed gate" }).getByText("risk").first()).toBeVisible();
});

test("Paper capital — Current Equity and Initial Capital shown separately", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/paper-account");
  await page.waitForTimeout(600);
  // stat card shows current equity with the initial capital beneath it
  const equity = page.locator(".content .stat-card, .content .card").filter({ hasText: "Current Equity" }).first();
  await expect(equity).toBeVisible();
  await expect(equity).toContainText("$10,300");
  await expect(equity).toContainText(/Initial \$10,000/);
});

test("Settings — legacy engine timeframe chips switch the candle interval", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/settings?section=advanced");
  await page.waitForTimeout(700);
  // the legacy engine's controls are folded away by default; open them first
  await page.locator("summary", { hasText: "Legacy Autonomous Engine" }).click();
  const card = page.locator("section.card").filter({ has: page.getByRole("heading", { name: "Legacy Engine Timeframe", exact: true }) }).last();
  await expect(card).toBeVisible();
  // all six options offered; current (4h from mock) highlighted
  for (const tf of ["1m", "5m", "15m", "1h", "4h", "1d"])
    await expect(card.getByRole("button", { name: tf, exact: true })).toBeVisible();
  await expect(card.getByRole("button", { name: "4h", exact: true })).toHaveClass(/active/);
  // clicking 15m POSTs the switch
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().includes("/engine/timeframe?timeframe=15m") && r.method() === "POST"),
    card.getByRole("button", { name: "15m", exact: true }).click(),
  ]);
  expect(req).toBeTruthy();
  await expect(page.locator(".toast.success")).toBeVisible();
});

test("Trading Mode & Approvals — modes render and a pending idea shows Approve/Reject", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/paper-account");
  await page.waitForTimeout(700);
  await expect(page.getByText("Trading Mode & Approvals")).toBeVisible();
  // the three modes are present; semi is active (mock)
  await expect(page.locator(".mode-btn", { hasText: "Full Auto" })).toBeVisible();
  await expect(page.locator(".mode-btn.active", { hasText: "Semi-Auto" })).toBeVisible();
  await expect(page.locator(".mode-btn", { hasText: "Signal" })).toBeVisible();
  // the pending approval card with real levels + actions
  await expect(page.locator(".approval-card", { hasText: "BTCUSDT" })).toBeVisible();
  await expect(page.getByText("3:1")).toBeVisible();
  await expect(page.getByRole("button", { name: /Approve/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Reject/ })).toBeVisible();
});

test("Risk Profile presets — Conservative/Balanced/Aggressive with active marker", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/risk-manager");
  await page.waitForTimeout(700);
  await expect(page.getByText("Risk Profile")).toBeVisible();
  await expect(page.locator(".preset-btn", { hasText: "Conservative" })).toBeVisible();
  await expect(page.locator(".preset-btn.active", { hasText: "Balanced" })).toBeVisible();
  await expect(page.locator(".preset-btn", { hasText: "Aggressive" })).toBeVisible();
  // headline risk-per-trade numbers render
  await expect(page.getByText("0.5%", { exact: false }).first()).toBeVisible();
});

test("research labs — each lab's News blackout switch sends its own PATCH", async ({ page }) => {
  await mockApi(page);
  for (const [label, lab] of [["Price Action Lab", "price_action"], ["SMC Strategy Lab", "smc"], ["Adaptive MTF Lab", "adaptive"]]) {
    await page.goto(`/#/${slug(label)}`);
    await page.reload();
    const section = page.getByTestId(`lab-news-guard-${lab}`);
    await expect(section).toContainText("News blackout");
    await expect(section).toContainText("Off");
    await expect(section).toContainText("Non-Farm Employment Change");
    await expect(section).toContainText("manual orders are not affected");
    const patch = page.waitForRequest((r) => r.url().endsWith(`/research/event-guard/${lab}`) && r.method() === "PATCH");
    await section.getByRole("button", { name: "Turn on", exact: true }).click();
    expect((await patch).postDataJSON()).toEqual({ enabled: true });
  }
});
