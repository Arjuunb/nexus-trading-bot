import { expect, type Page, test } from "@playwright/test";
import { mockApi } from "./mock";

const T0 = Date.UTC(2026, 8, 23, 12, 0, 0);
const CANDLES = Array.from({ length: 120 }, (_, i) => {
  const base = 0.52 + Math.sin(i / 9) * 0.01 + i * 0.0002;
  return { t: new Date(T0 + i * 300_000).toISOString(), o: base, h: base + 0.004,
           l: base - 0.004, c: base + (i % 2 ? 0.002 : -0.002), v: 1000 + i };
});

function statusFor(session: { symbol: string; mode: string; risk_pct: number }) {
  return {
    lab: "ADAPTIVE_MTF_TREND_PULLBACK",
    strategy: { key: "adaptive_trend_pullback", label: "Adaptive MTF Trend Pullback", version: "1.0.0", timeframe: "5m" },
    symbol: session.symbol, mode: session.mode, risk_pct: session.risk_pct, max_risk_pct: 1,
    modes: [{ id: "automatic", label: "Automatic paper" }, { id: "signals_only", label: "Signals only" }, { id: "off", label: "Off" }],
    supported_symbols: ["XRPUSDT", "ADAUSDT", "BTCUSDT"],
    bots: [{ symbol: session.symbol, id: "bot-1", mode: session.mode }], bot_id: "bot-1",
    bot: { id: "bot-1", symbol: session.symbol, state: "running", market_status: "LIVE",
           last_decision: { decision: "ENTER LONG", state: "ORDER_PENDING", reason: "LONG | 1H BULL_TREND 72% | quality 78/100 | RR 2.40" },
           metrics: { balance: 10_042.5, realized_pnl: 42.5, trades: 3, win_rate: 66.7 } },
    paper_only: true, real_execution_allowed: false,
  };
}

async function labServer(page: Page, { refuse = "" } = {}) {
  let session = { symbol: "XRPUSDT", mode: "automatic", risk_pct: 0.5 };
  const saves: any[] = [];
  await mockApi(page);
  await page.route("**/research/adaptive-lab/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/.*\/research\/adaptive-lab/, "");
    if (path === "/configuration") {
      const body = route.request().postDataJSON();
      saves.push(body);
      if (refuse) return route.fulfill({ status: 400, json: { detail: refuse } });
      session = { ...session, ...Object.fromEntries(Object.entries(body).filter(([, v]) => v !== null && v !== undefined)) } as any;
      return route.fulfill({ json: statusFor(session) });
    }
    if (path === "/status") return route.fulfill({ json: statusFor(session) });
    if (path === "/paper") return route.fulfill({ json: {
      bot_id: "bot-1", symbol: session.symbol,
      positions: [{ id: "p1", side: "long", size: 1900, entry: 0.5321, stop: 0.5268, target: 0.5448, opened_at: CANDLES[110].t }],
      orders: { forward_paper_intents: {}, strategy_limit_intents: { o1: { side: "long", entry: 0.5301, stop: 0.5260, target: 0.5400 } }, quarantined_intents: {} },
      trades: [{ id: "t1", side: "long", entry: 0.5102, exit: 0.5225, realized_pnl: 31.4, status: "closed", opened_at: CANDLES[20].t }],
      logs: [{ id: 1, ts: CANDLES[119].t, level: "info", message: "candle_processed" }],
      paper_only: true, real_execution_allowed: false } });
    if (path === "/state") return route.fulfill({ json: {
      decision_state: "POSITION_OPEN", required_next: "Trend resumption confirmed", blocker: null, blocker_explanation: "",
      position: { side: "long", entry: 0.5321, stop: 0.5268, target: 0.5448 },
      gates: [
        { id: "feed_synchronized", stage: "MARKET_DATA", label: "Feed synchronized", detail: "", state: "PASS", blocker: "", explanation: "" },
        { id: "htf_bias", stage: "CONTEXT", label: "4H bias established", detail: "", state: "PASS", blocker: "", explanation: "" },
        { id: "pullback", stage: "SETUP", label: "Pullback into trend support", detail: "", state: "PASS", blocker: "", explanation: "" },
        { id: "resume", stage: "CONFIRMATION", label: "Trend resumption confirmed", detail: "", state: "WAITING", blocker: "", explanation: "" },
      ] } });
    if (path.startsWith("/candles")) return route.fulfill({ json: { candles: CANDLES, source: "venue binance_usdm · live closed candles" } });
    if (path === "/features") return route.fulfill({ json: { overlays: [] } });
    if (path.startsWith("/timeline")) return route.fulfill({ json: { events: [
      { id: 7, timestamp: CANDLES[110].t, candle_identity: "c110", symbol: session.symbol, timeframe: "5m",
        strategy: "adaptive_trend_pullback", side: "long", regime: "BULL_TREND", htf_bias: "BULLISH",
        decision: "accepted", final_state: "FILLED", gate_stage: "FILL", blocker: null, blocker_explanation: "",
        reason: "LONG | quality 78/100 | RR 2.40", passed_rules: [], failed_rules: [], components: {}, executed: true }] } });
    return route.fallback();
  });
  return { saves };
}

test("Adaptive MTF Lab shows its bot, orders and decisions, and saves each change at once", async ({ page }) => {
  const server = await labServer(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/#/adaptive-mtf-lab");
  const saved = page.getByTestId("adaptive-saved-configuration");
  await expect(saved).toContainText("Adaptive MTF Trend Pullback");
  await expect(saved).toContainText("XRPUSDT 5m · Automatic paper · risk 0.5%");
  await expect(page.getByTestId("adaptive-waiting")).toContainText("Trend resumption confirmed");
  await expect(page.locator(".pa-table")).toContainText("0.5321");           // the open position
  await page.locator(".pa-bottom nav").getByRole("button", { name: /orders/ }).click();
  await expect(page.locator(".pa-table")).toContainText("0.5301");           // the working order
  await page.locator(".pa-bottom nav").getByRole("button", { name: /trades/ }).click();
  await expect(page.locator(".pa-table")).toContainText("31.4");
  await page.locator(".pa-bottom nav").getByRole("button", { name: /decisions/ }).click();
  await expect(page.locator(".pa-table")).toContainText("accepted");
  await page.screenshot({ path: "test-results/adaptive-lab.png", fullPage: false });

  await page.getByLabel("Adaptive lab mode").selectOption("signals_only");
  await expect.poll(() => server.saves.length).toBe(1);
  expect(server.saves[0]).toEqual({ mode: "signals_only" });
  await expect(saved).toContainText("Signals only");

  await page.getByLabel("Adaptive lab symbol").selectOption("ADAUSDT");
  await expect.poll(() => server.saves.length).toBe(2);
  expect(server.saves[1]).toEqual({ symbol: "ADAUSDT" });
  await expect(saved).toContainText("ADAUSDT 5m");

  const risk = page.getByLabel("Adaptive lab risk per trade");
  await risk.fill("0.8");
  await risk.press("Enter");
  await expect.poll(() => server.saves.length).toBe(3);
  expect(server.saves[2]).toEqual({ risk_pct: 0.8 });
  await expect(saved).toContainText("risk 0.8%");
});

test("Adaptive MTF Lab refused change says why and keeps what is saved", async ({ page }) => {
  await labServer(page, { refuse: "XRPUSDT has an open paper position. Close it before switching symbol" });
  await page.goto("/#/adaptive-mtf-lab");
  await expect(page.getByTestId("adaptive-saved-configuration")).toContainText("XRPUSDT");
  await page.getByLabel("Adaptive lab symbol").selectOption("ADAUSDT");
  await expect(page.locator(".toast.error")).toContainText(/Not saved: .*open paper position/);
  await expect(page.getByLabel("Adaptive lab symbol")).toHaveValue("XRPUSDT");
  await expect(page.getByTestId("adaptive-saved-configuration")).toContainText("XRPUSDT");
});
