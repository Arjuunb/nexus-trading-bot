import { expect, type Page, test } from "@playwright/test";
import { mockApi, SMC_PAPER } from "./mock";

// A strategy change must be saved the moment it is made and must then be what
// the bot runs. Before this, the strategy and mode pickers were drafts that
// only an "Apply" press sent: a pick without Apply was never saved and the
// page went back to the old strategy on the next reload.

const MODELS = [
  { id: "SMC_M1_SWEEP_REVERSAL", label: "Liquidity sweep reversal", status: "ACTIVE", narrative: "", ordered_rules: [] },
  // ACTIVE here only so the save path of a second strategy can be exercised.
  { id: "SMC_M2_BOS_CONTINUATION", label: "BOS continuation", status: "ACTIVE", narrative: "", ordered_rules: [] },
  { id: "SMC_M3_DISPLACEMENT_FVG", label: "Displacement FVG", status: "PARKED", narrative: "", ordered_rules: [] },
];

/** A stateful SMC backend: a save changes what every later read returns. */
async function smcServer(page: Page, { refuse = "" } = {}) {
  let session = structuredClone(SMC_PAPER.session);
  const saves: any[] = [];
  await mockApi(page);
  await page.route("**/research/smc/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/strategy-models")) return route.fulfill({ json: { models: MODELS } });
    if (url.pathname.endsWith("/configuration")) {
      const body = route.request().postDataJSON();
      saves.push(body);
      if (refuse) return route.fulfill({ status: 400, json: { detail: refuse } });
      session = { ...session, ...body };
      return route.fulfill({ json: { ...SMC_PAPER, session } });
    }
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { session } });
    if (url.pathname.endsWith("/paper")) return route.fulfill({ json: { ...SMC_PAPER, session } });
    return route.fallback();
  });
  return { saves, current: () => session };
}

test("SMC strategy, mode and risk are saved on change and survive a reload", async ({ page }) => {
  const server = await smcServer(page);
  await page.goto("/#/smc-strategy-lab");
  const saved = page.getByTestId("smc-saved-configuration");
  await expect(saved).toContainText("Liquidity sweep reversal");

  await page.getByLabel("SMC entry model").selectOption("SMC_M2_BOS_CONTINUATION");
  await expect.poll(() => server.saves.length).toBe(1);
  // Everything not changed is sent as SAVED: a missing field would be reset
  // to the server's default (operating_mode "automatic") without a word.
  expect(server.saves[0]).toMatchObject({ model_id: "SMC_M2_BOS_CONTINUATION",
    operating_mode: "signals_only", risk_pct: .5, symbol: "BTCUSDT", timeframe: "5m" });
  await expect(page.locator(".toast.success")).toContainText("Saved: strategy BOS continuation");
  await expect(saved).toContainText("BOS continuation");

  await page.getByLabel("Paper operating mode").selectOption("manual_approval");
  await expect.poll(() => server.saves.length).toBe(2);
  expect(server.saves[1]).toMatchObject({ operating_mode: "manual_approval", model_id: "SMC_M2_BOS_CONTINUATION" });
  await expect(saved).toContainText("Agent decides");

  const risk = page.getByLabel("SMC risk per trade");
  await risk.fill("0.8");
  await risk.press("Enter");
  await expect.poll(() => server.saves.length).toBe(3);
  expect(server.saves[2]).toMatchObject({ risk_pct: .8, operating_mode: "manual_approval",
    model_id: "SMC_M2_BOS_CONTINUATION" });
  await expect(saved).toContainText("risk 0.8%");

  await page.reload();
  await expect(saved).toContainText("BOS continuation");
  await expect(saved).toContainText("Agent decides");
  await expect(saved).toContainText("risk 0.8%");
  await expect(page.getByLabel("SMC entry model")).toHaveValue("SMC_M2_BOS_CONTINUATION");
  await expect(page.getByLabel("Paper operating mode")).toHaveValue("manual_approval");
  await expect(page.getByLabel("SMC risk per trade")).toHaveValue("0.8");
  expect(server.saves).toHaveLength(3); // a reload reads, it never saves
});

test("SMC refused strategy change says why and shows the strategy that is really saved", async ({ page }) => {
  const server = await smcServer(page, { refuse: "the selected SMC entry model is parked or unknown" });
  await page.goto("/#/smc-strategy-lab");
  await expect(page.getByTestId("smc-saved-configuration")).toContainText("Liquidity sweep reversal");

  await page.getByLabel("SMC entry model").selectOption("SMC_M2_BOS_CONTINUATION");
  await expect(page.locator(".toast.error")).toContainText(/Not saved: .*the selected SMC entry model is parked or unknown/);
  await expect(page.getByLabel("SMC entry model")).toHaveValue("SMC_M1_SWEEP_REVERSAL");
  await expect(page.getByTestId("smc-saved-configuration")).toContainText("Liquidity sweep reversal");
  expect(server.current().model_id).toBe("SMC_M1_SWEEP_REVERSAL");
});

test("SMC strategies that are not built cannot be picked, and the setup list says it is view only", async ({ page }) => {
  await smcServer(page);
  await page.goto("/#/smc-strategy-lab");
  const parked = page.getByLabel("SMC entry model").locator("option", { hasText: "Displacement FVG" });
  await expect(parked).toHaveText(/not built yet/);
  await expect(parked).toHaveJSProperty("disabled", true);
  await expect(page.getByText("Chart focus · view only, not a strategy")).toBeAttached();
});

/** A stateful Price Action backend: a save changes what every later read returns. */
async function paServer(page: Page, { refuse = "" } = {}) {
  const { PA_PAPER } = await import("./mock");
  let session: any = structuredClone(PA_PAPER.session);
  const saves: any[] = [];
  await mockApi(page);
  await page.route("**/research/price-action/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/sessions/current/configuration")) {
      const body = route.request().postDataJSON();
      saves.push(body);
      if (refuse) return route.fulfill({ status: 400, json: { detail: refuse } });
      session = { ...session, mode: body.mode, symbol: body.symbol, timeframe: body.timeframe,
        operating_mode: body.operating_mode,
        execution_config: { strategy_id: body.strategy_id, risk_pct: body.risk_pct } };
      return route.fulfill({ json: { ...PA_PAPER, session } });
    }
    if (url.pathname.endsWith("/research/price-action/session")) return route.fulfill({ json: { session } });
    if (url.pathname.endsWith("/research/price-action/paper") && route.request().method() === "GET") {
      return route.fulfill({ json: { ...PA_PAPER, session } });
    }
    return route.fallback();
  });
  return { saves, current: () => session };
}

test("Price Action strategy, mode and risk are saved on change and survive a reload", async ({ page }) => {
  const server = await paServer(page);
  await page.goto("/#/price-action-lab");
  const saved = page.getByTestId("pa-saved-configuration");
  await expect(saved).toContainText("Signals only");

  await page.getByLabel("Price Action strategy").selectOption("PA2_TREND_PULLBACK");
  await expect.poll(() => server.saves.length).toBe(1);
  expect(server.saves[0]).toMatchObject({ strategy_id: "PA2_TREND_PULLBACK", operating_mode: "signals_only",
    risk_pct: .5, mode: "LIVE_PAPER", symbol: "BTCUSDT", timeframe: "5m" });
  await expect(page.locator(".toast.success")).toContainText("Saved: strategy");

  await page.getByLabel("Paper operating mode").selectOption("automatic");
  await expect.poll(() => server.saves.length).toBe(2);
  expect(server.saves[1]).toMatchObject({ operating_mode: "automatic", strategy_id: "PA2_TREND_PULLBACK" });
  await expect(saved).toContainText("Automatic paper");

  const risk = page.getByLabel("Price Action risk per trade");
  await risk.fill("0.7");
  await risk.press("Enter");
  await expect.poll(() => server.saves.length).toBe(3);
  expect(server.saves[2]).toMatchObject({ risk_pct: .7, operating_mode: "automatic", strategy_id: "PA2_TREND_PULLBACK" });

  await page.reload();
  await expect(saved).toContainText("Automatic paper");
  await expect(saved).toContainText("risk 0.7%");
  await expect(page.getByLabel("Price Action strategy")).toHaveValue("PA2_TREND_PULLBACK");
  await expect(page.getByLabel("Paper operating mode")).toHaveValue("automatic");
  expect(server.saves).toHaveLength(3); // a reload reads, it never saves
});

test("Price Action refused strategy change says why and shows the strategy that is really saved", async ({ page }) => {
  const server = await paServer(page, { refuse: "strategy is not available" });
  await page.goto("/#/price-action-lab");
  await expect(page.getByTestId("pa-saved-configuration")).toContainText("Signals only");
  await page.getByLabel("Price Action strategy").selectOption("PA3_FLIP_RETEST");
  await expect(page.locator(".toast.error")).toContainText(/Not saved: .*strategy is not available/);
  await expect(page.getByLabel("Price Action strategy")).toHaveValue("PA1_SR_REJECTION");
  expect(server.current().execution_config.strategy_id).toBe("PA1_SR_REJECTION");
});
