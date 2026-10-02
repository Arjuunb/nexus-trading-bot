import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";

// The STRATEGY badge and the line under it. On an ordinary candle the code is
// why the strategy is still waiting, so it reads "Last candle" with the same
// sentence the Instance Visual Lab shows; "Blocker" and red are kept for
// something actually holding entries back.
const base = {
  strategy_key: "three_candle_rejection", strategy_label: "3-Candle Rejection · EMA 9/33",
  strategy_version: "1.0.0", timeframe: "5m", mode: "trading", state: "running",
  risk_per_trade_pct: 0.005, capital_allocation: 1000, max_open_positions: 1,
  runtime_status: "RUNNING", market_status: "LIVE", execution_status: "FORWARD_PAPER",
  metrics: {}, performance: {}, strategy_health: { status: "Unhealthy" },
};
const instances = [
  { ...base, id: "sol-1", symbol: "SOLUSDT", strategy_status: "WAITING_FOR_SETUP",
    current_blocker: "GATE_REJECTED: NO_CONFIRMATION",
    current_blocker_explanation: "The rejection formed, but the next candle did not close beyond it." },
  { ...base, id: "xrp-1", symbol: "XRPUSDT", strategy_status: "IN_POSITION",
    current_blocker: "GATE_REJECTED: POSITION_MANAGED",
    current_blocker_explanation: "The engine is managing an open position rather than scanning for entries." },
  { ...base, id: "eth-1", symbol: "ETHUSDT", strategy_status: "BLOCKED",
    current_blocker: "GATE_REJECTED: DAILY_LOSS_LIMIT",
    current_blocker_explanation: "The daily loss limit has paused new entries." },
];

test("a waiting strategy is not shown as blocked; a real hold still is", async ({ page }) => {
  await mockApi(page);
  await page.route((url) => url.host === "localhost:8000" && url.pathname === "/instances", (route) =>
    route.fulfill({ json: { instances, active_slots: 3, max_active_slots: 8, total_current_equity: 3000,
      paper_account_capital: 10000, available_paper_capital: 7000, current_global_risk_amount: 0,
      max_global_risk_amount: 500, total_open_positions: 1, global_risk_status: "healthy",
      global_risk_message: "Within configured limits", market_data_status: "healthy" } }));
  await page.route((url) => url.host === "localhost:8000" && url.pathname.endsWith("/event-guard"), (route) =>
    route.fulfill({ json: { enabled: false, updated_at: null, calendar_connected: false, mode: "normal",
      halt_new_entries: false, risk_multiplier: 1, next_event: null, minutes_to_event: null,
      window: { blackout_before_min: 30, blackout_after_min: 15, caution_before_min: 120 } } }));
  await page.goto("/#/trading-instances");

  const card = (symbol: string) => page.locator("article.instance-worker-row", { has: page.locator("h3", { hasText: symbol }) });
  const strategyBadge = (symbol: string) => card(symbol).locator(".instance-axis", { hasText: "STRATEGY" }).locator(".ui-badge");

  await expect(strategyBadge("SOLUSDT")).toHaveText("WAITING_FOR_SETUP");
  await expect(card("SOLUSDT").locator(".instance-blocker")).toHaveText(
    "Last candle: NO_CONFIRMATION — The rejection formed, but the next candle did not close beyond it.");

  await expect(strategyBadge("XRPUSDT")).toHaveText("IN_POSITION");
  await expect(card("XRPUSDT").locator(".instance-blocker")).toContainText("Last candle: POSITION_MANAGED");

  await expect(strategyBadge("ETHUSDT")).toHaveText("BLOCKED");
  await expect(card("ETHUSDT").locator(".instance-blocker")).toHaveText(
    "Blocker: GATE_REJECTED: DAILY_LOSS_LIMIT — The daily loss limit has paused new entries.");
  // red is reserved for the real hold
  const color = (symbol: string) => strategyBadge(symbol).evaluate((el) => getComputedStyle(el).color);
  expect(await color("ETHUSDT")).not.toEqual(await color("SOLUSDT"));
  expect(await color("XRPUSDT")).not.toEqual(await color("ETHUSDT"));
});
