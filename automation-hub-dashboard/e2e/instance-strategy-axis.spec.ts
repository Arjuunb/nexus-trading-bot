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
    current_blocker_explanation: "The rejection formed, but the next candle did not close beyond it.",
    last_decision: { final_state: "PENDING_INTENT", blocker: "GATE_REJECTED: ORDER_PENDING", reason: "limit entry resting" } },
  { ...base, id: "xrp-1", symbol: "XRPUSDT", strategy_status: "IN_POSITION",
    current_blocker: "GATE_REJECTED: POSITION_MANAGED",
    current_blocker_explanation: "The engine is managing an open position rather than scanning for entries." },
  { ...base, id: "btc-1", symbol: "BTCUSDT", strategy_status: "SIGNAL_REFUSED",
    current_blocker: "GATE_REJECTED: BRAIN",
    current_blocker_explanation: "The Decision Brain quality gate refused this candle's signal. Reason: Hard block: ranging / unclear regime for a trend setup" },
  { ...base, id: "bnb-1", symbol: "BNBUSDT", strategy_status: "BLOCKED",
    current_blocker: "GATE_REJECTED: LOSS_COOLDOWN",
    current_blocker_explanation: "A cooldown after a loss is still holding entries. Reason: losing-streak pause: new entries are held until 2026-10-03 14:05 UTC" },
  { ...base, id: "eth-1", symbol: "ETHUSDT", strategy_status: "BLOCKED",
    current_blocker: "GATE_REJECTED: DAILY_LOSS_LIMIT",
    current_blocker_explanation: "The daily loss limit has paused new entries.",
    last_decision: { final_state: "GATE_REJECTED", blocker: "GATE_REJECTED: BRAIN",
                     reason: "Hard block: ranging / unclear regime for a trend setup" } },
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

  // one candle's signal refused by the Decision Brain: amber, not red
  await expect(strategyBadge("BTCUSDT")).toHaveText("SIGNAL_REFUSED");
  await expect(card("BTCUSDT").locator(".instance-blocker")).toHaveText(
    "Last candle: BRAIN — The Decision Brain quality gate refused this candle's signal. Reason: Hard block: ranging / unclear regime for a trend setup");
  // the losing-streak pause is a real hold, and says when it ends
  await expect(strategyBadge("BNBUSDT")).toHaveText("BLOCKED");
  await expect(card("BNBUSDT").locator(".instance-blocker")).toContainText(
    "Blocker: GATE_REJECTED: LOSS_COOLDOWN — A cooldown after a loss is still holding entries. Reason: losing-streak pause: new entries are held until 2026-10-03 14:05 UTC");

  await expect(strategyBadge("ETHUSDT")).toHaveText("BLOCKED");
  await expect(card("ETHUSDT").locator(".instance-blocker")).toHaveText(
    "Blocker: GATE_REJECTED: DAILY_LOSS_LIMIT — The daily loss limit has paused new entries.");
  // the last decision in words: a resting order is not a refusal, and a
  // refusal says which gate and why
  const lastDecision = (symbol: string) =>
    card(symbol).getByText("Last decision", { exact: true }).locator("xpath=following-sibling::div[1]");
  await expect(lastDecision("SOLUSDT")).toHaveText("Order waiting to fill");
  await expect(lastDecision("ETHUSDT")).toHaveText(
    "Refused · BRAIN — Hard block: ranging / unclear regime for a trend setup");

  // red is reserved for the real hold
  const color = (symbol: string) => strategyBadge(symbol).evaluate((el) => getComputedStyle(el).color);
  expect(await color("ETHUSDT")).not.toEqual(await color("SOLUSDT"));
  expect(await color("XRPUSDT")).not.toEqual(await color("ETHUSDT"));
  expect(await color("BTCUSDT")).not.toEqual(await color("ETHUSDT"));
  expect(await color("BNBUSDT")).toEqual(await color("ETHUSDT"));
});

// The header strip's state for the selected instance. "warning" is risk
// capacity almost used and entries still go on; only the daily loss limit
// holds them.
for (const [risk, expected] of [["warning", "RUNNING_ARMED"], ["daily_loss_limit_reached", "BLOCKED"]]) {
  test(`the header reads ${expected} when global risk is ${risk}`, async ({ page }) => {
    await mockApi(page);
    const armed = { ...base, id: "sol-1", symbol: "SOLUSDT", ui_status: "RUNNING_ARMED",
                    strategy_status: "WAITING_FOR_SETUP", market_data: { market_data_status: "healthy" } };
    await page.route((url) => url.host === "localhost:8000" && url.pathname === "/instances", (route) =>
      route.fulfill({ json: { instances: [armed], active_slots: 1, max_active_slots: 8, total_current_equity: 1000,
        paper_account_capital: 10000, available_paper_capital: 9000, current_global_risk_amount: 460,
        max_global_risk_amount: 500, total_open_positions: 1, global_risk_status: risk,
        global_risk_message: "Risk capacity almost reached", market_data_status: "healthy" } }));
    await page.goto("/#/dashboard");
    await expect(page.locator(".hdr-controls .state-label")).toHaveText(expected);
  });
}
