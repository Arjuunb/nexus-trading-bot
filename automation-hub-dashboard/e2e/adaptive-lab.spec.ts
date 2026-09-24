import { expect, type Page, test } from "@playwright/test";
import { mockApi } from "./mock";

const T0 = Date.UTC(2026, 8, 23, 12, 0, 0);
const CANDLES = Array.from({ length: 120 }, (_, i) => {
  const base = 0.52 + Math.sin(i / 9) * 0.01 + i * 0.0002;
  return { timestamp: new Date(T0 + i * 300_000).toISOString(), open: base, high: base + 0.004,
           low: base - 0.004, close: base + (i % 2 ? 0.002 : -0.002), volume: 1000 + i };
});
const FORMING = { timestamp: new Date(T0 + 120 * 300_000).toISOString(), open: 0.5451, high: 0.5467,
                  low: 0.5442, close: 0.5463, volume: 311 };
const LIVE_CHART = {
  bot_id: "bot-1", symbol: "XRPUSDT", timeframe: "5m", candles: CANDLES, forming_candle: FORMING,
  live_display: { is_forming: true, observed_at: new Date(T0 + 120 * 300_000 + 95_000).toISOString(),
    refresh_interval_seconds: 2.5, candle_closes_at: new Date(T0 + 121 * 300_000).toISOString(),
    last_price: 0.5463, bid: 0.5462, ask: 0.5464, mark: 0.54635, connection_state: "LIVE",
    reliable: true, new_entries_paused: false, health_reason: "Closed candles, quote and mark reconciled",
    quote_source: "BINANCE_USDM_PUBLIC_WEBSOCKET", execution_uses_closed_bars_only: true },
  data_provenance: { last_closed_candle: CANDLES[119].timestamp, closed_candles_loaded: 120 },
  trade_plan: { entry: 0.5321, stop: 0.5268, target_1: 0.5448, target_2: 0.5448 },
  fills: [{ timestamp: CANDLES[110].timestamp, price: 0.5321, side: "BUY" }],
  paper_only: true, real_execution_allowed: false,
};
const JOURNAL = {
  bot_id: "bot-1", symbol: "XRPUSDT",
  state_counts: { WAITING_FOR_PULLBACK: 108, ORDER_PENDING: 1, POSITION_OPEN: 9 },
  entries: [
    { id: 2, candle_time: CANDLES[119].timestamp, engine_decision: "WAIT", price: 0.5451,
      strategy_state: "POSITION_OPEN", strategy_decision: "HOLD", direction: "long",
      reason: "Managing open long toward 0.5448", quality: null, rr: null,
      entry: null, stop: null, target: null, engine_reasons: [] },
    { id: 1, candle_time: CANDLES[110].timestamp, engine_decision: "BUY", price: 0.5321,
      strategy_state: "ORDER_PENDING", strategy_decision: "ENTER LONG", direction: "long",
      reason: "LONG | 1H BULL_TREND 72% | quality 78/100 | RR 2.40", quality: 78, rr: 2.4,
      entry: 0.5321, stop: 0.5268, target: 0.5448, engine_reasons: [] },
  ],
};

const INSTANCE_ID = "inst-7f3a91c2";
const INSTANCE_BOT = { id: INSTANCE_ID, symbol: "XRPUSDT", state: "running", market_status: "LIVE",
  last_decision: { decision: "ENTER SHORT", state: "POSITION_OPEN", reason: "SHORT | 1H BEAR_TREND 68% | quality 74/100 | RR 2.10" },
  metrics: { balance: 1_018.3, realized_pnl: 18.3, trades: 1, win_rate: 100 } };

type Session = { symbol: string; mode: string; risk_pct: number };
type Instance = { running: boolean } | null;

function labBot(session: Session) {
  return { id: "bot-1", symbol: session.symbol, state: "running", market_status: "LIVE",
           last_decision: { decision: "ENTER LONG", state: "ORDER_PENDING", reason: "LONG | 1H BULL_TREND 72% | quality 78/100 | RR 2.40" },
           metrics: { balance: 10_042.5, realized_pnl: 42.5, trades: 3, win_rate: 66.7 } };
}

function viewFor(source: string, session: Session, instance: Instance) {
  if (instance && source === INSTANCE_ID) return {
    source: INSTANCE_ID, kind: "instance", bot: INSTANCE_BOT, bot_id: INSTANCE_ID, symbol: "XRPUSDT", timeframe: "5m",
    running: instance.running, armed: instance.running, state_label: instance.running ? "Running · entries armed" : "Stopped",
    risk_pct: 0.5, capital_allocation: 1000, controlled_from: "Trading Instances" };
  return { source: "lab", kind: "lab", bot: labBot(session), bot_id: "bot-1", symbol: session.symbol, timeframe: "5m",
    running: session.mode !== "off", armed: session.mode === "automatic", state_label: session.mode,
    risk_pct: session.risk_pct, capital_allocation: 10000, controlled_from: "this lab" };
}

function statusFor(session: Session, source = "lab", instance: Instance = null) {
  const sources = [{ id: "lab", kind: "lab", symbol: session.symbol, timeframe: "5m", running: session.mode !== "off", label: `Lab bot · ${session.symbol} 5m` },
    ...(instance ? [{ id: INSTANCE_ID, kind: "instance", symbol: "XRPUSDT", timeframe: "5m", running: instance.running,
                      label: `Trading Instance · XRPUSDT 5m · ${instance.running ? "running" : "stopped"}` }] : [])];
  return {
    sources, view: viewFor(source, session, instance),
    lab: "ADAPTIVE_MTF_TREND_PULLBACK",
    strategy: { key: "adaptive_trend_pullback", label: "Adaptive MTF Trend Pullback", version: "1.0.0", timeframe: "5m" },
    symbol: session.symbol, mode: session.mode, risk_pct: session.risk_pct, max_risk_pct: 1,
    modes: [{ id: "automatic", label: "Automatic paper" }, { id: "signals_only", label: "Signals only" }, { id: "off", label: "Off" }],
    supported_symbols: ["XRPUSDT", "ADAUSDT", "BTCUSDT"],
    bots: [{ symbol: session.symbol, id: "bot-1", mode: session.mode }], bot_id: "bot-1",
    bot: labBot(session),
    paper_only: true, real_execution_allowed: false,
  };
}

async function labServer(page: Page, { refuse = "", mode = "automatic", instance = null as Instance } = {}) {
  let session = { symbol: "XRPUSDT", mode, risk_pct: 0.5 };
  const saves: any[] = [];
  const feedCalls: string[] = [];
  const reads: { path: string; source: string }[] = [];
  await mockApi(page);
  await page.route("**/research/adaptive-lab/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/.*\/research\/adaptive-lab/, "");
    const source = url.searchParams.get("source") ?? "lab";
    if (route.request().method() === "GET") reads.push({ path, source });
    if (instance && source === INSTANCE_ID) {
      // The mirrored instance's own account: a different trade from the lab bot's.
      if (path === "/status") return route.fulfill({ json: statusFor(session, source, instance) });
      if (path === "/paper") return route.fulfill({ json: {
        bot_id: INSTANCE_ID, symbol: "XRPUSDT",
        positions: [{ id: "ip1", side: "short", size: 1500, entry: 0.548, stop: 0.553, target: 0.533, opened_at: CANDLES[115].timestamp }],
        orders: { forward_paper_intents: {}, strategy_limit_intents: {}, quarantined_intents: {} },
        trades: [{ id: "it1", side: "short", entry: 0.5602, exit: 0.548, realized_pnl: 18.3, status: "closed", opened_at: CANDLES[60].timestamp }],
        logs: [], paper_only: true, real_execution_allowed: false } });
      if (path === "/live-chart") {
        feedCalls.push(`instance:${instance.running}`);
        if (!instance.running) return route.fulfill({ status: 503, json: { detail: "the XRPUSDT Trading Instance is off, so it has no live feed to show" } });
        return route.fulfill({ json: { ...LIVE_CHART, bot_id: INSTANCE_ID, source: INSTANCE_ID,
          trade_plan: { entry: 0.548, stop: 0.553, target_1: 0.533, target_2: 0.533 } } });
      }
      if (path === "/journal") return route.fulfill({ json: { bot_id: INSTANCE_ID, symbol: "XRPUSDT",
        state_counts: { POSITION_OPEN: 1, ENGINE_REPORT_ONLY: 1 },
        entries: [
          { id: 9, candle_time: CANDLES[115].timestamp, engine_decision: "SELL", price: 0.548, strategy_state: "ORDER_PENDING",
            strategy_decision: "ENTER SHORT", direction: "short", reason: "SHORT | 1H BEAR_TREND 68% | quality 74/100 | RR 2.10",
            quality: 74, rr: 2.1, entry: 0.548, stop: 0.553, target: 0.533, engine_reasons: [], evidence: "strategy_journal" },
          { id: "report-41", candle_time: CANDLES[60].timestamp, engine_decision: "SELL", price: 0.5602, strategy_state: null,
            strategy_decision: null, direction: "short", reason: "SHORT | 1H BEAR_TREND 71% | quality 80/100 | RR 2.60",
            quality: null, rr: null, entry: null, stop: null, target: null, engine_reasons: [], evidence: "engine_report" },
        ] } });
    }
    if (path === "/configuration") {
      const body = route.request().postDataJSON();
      saves.push(body);
      if (refuse) return route.fulfill({ status: 400, json: { detail: refuse } });
      session = { ...session, ...Object.fromEntries(Object.entries(body).filter(([, v]) => v !== null && v !== undefined)) } as any;
      return route.fulfill({ json: statusFor(session, "lab", instance) });
    }
    if (path === "/status") return route.fulfill({ json: statusFor(session, "lab", instance) });
    if (path === "/paper") return route.fulfill({ json: {
      bot_id: "bot-1", symbol: session.symbol,
      positions: [{ id: "p1", side: "long", size: 1900, entry: 0.5321, stop: 0.5268, target: 0.5448, opened_at: CANDLES[110].timestamp }],
      orders: { forward_paper_intents: {}, strategy_limit_intents: { o1: { side: "long", entry: 0.5301, stop: 0.5260, target: 0.5400 } }, quarantined_intents: {} },
      trades: [{ id: "t1", side: "long", entry: 0.5102, exit: 0.5225, realized_pnl: 31.4, status: "closed", opened_at: CANDLES[20].timestamp }],
      logs: [{ id: 1, ts: CANDLES[119].timestamp, level: "info", message: "candle_processed" }],
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
    if (path === "/live-chart") {
      feedCalls.push(session.mode);
      // The real server has no feed for an off bot (503 NO_LIVE_FEED).
      if (session.mode === "off") return route.fulfill({ status: 503, json: { detail: `the ${session.symbol} bot is off, so it has no live feed to show` } });
      return route.fulfill({ json: { ...LIVE_CHART, symbol: session.symbol } });
    }
    if (path === "/journal") return route.fulfill({ json: JOURNAL });
    return route.fallback();
  });
  return { saves, feedCalls, reads };
}

test("Adaptive MTF Lab shows its bot, orders and journal, and saves each change at once", async ({ page }) => {
  const server = await labServer(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/#/adaptive-mtf-lab");
  const saved = page.getByTestId("adaptive-saved-configuration");
  await expect(saved).toContainText("Adaptive MTF Trend Pullback");
  await expect(saved).toContainText("XRPUSDT 5m · Automatic paper · risk 0.5%");
  await expect(page.getByTestId("adaptive-waiting")).toContainText("Trend resumption confirmed");
  // The chart is the SMC lab's live chart on the bot's own feed: closed
  // candles, the forming candle (display only) and bid/ask/mark.
  await expect(page.locator(".smc-chart-canvas canvas").first()).toBeVisible();
  const readout = page.locator(".pa-market-readout");
  await expect(readout).toContainText("Forming candle · display only");
  await expect(readout).toContainText("C 0.5463");
  await expect(readout).toContainText("0.5462 / 0.5464");
  await expect(readout).toContainText("0.54635");
  await expect(readout).toContainText("120 closed candles loaded");
  await expect(page.locator(".pa-stream-truth")).toContainText("LIVE");
  await expect(page.locator(".pa-table")).toContainText("0.5321");           // the open position
  await page.locator(".pa-bottom nav").getByRole("button", { name: /orders/ }).click();
  await expect(page.locator(".pa-table")).toContainText("0.5301");           // the working order
  await page.locator(".pa-bottom nav").getByRole("button", { name: /trades/ }).click();
  await expect(page.locator(".pa-table")).toContainText("31.4");
  await page.locator(".pa-bottom nav").getByRole("button", { name: /journal/ }).click();
  const journal = page.getByTestId("adaptive-journal");
  await expect(journal.locator("tbody tr")).toHaveCount(2);
  await expect(journal.locator("tr.is-signal")).toContainText("ENTER LONG");       // the BUY candle
  await expect(journal).toContainText("quality 78/100");
  await expect(page.locator(".adaptive-journal-head")).toContainText("WAITING FOR PULLBACK 108");
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

test("Adaptive MTF Lab that is off says so plainly, polls no feed, and turns on from the chart", async ({ page }) => {
  const server = await labServer(page, { mode: "off" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/#/adaptive-mtf-lab");
  const off = page.getByTestId("adaptive-off");
  await expect(off).toContainText("The bot is off");
  await expect(page.locator(".pa-health-scope")).toContainText("BOT OFF");
  await expect(page.locator(".pa-health-scope")).not.toContainText("ERROR");
  await expect(page.locator(".pa-error")).toHaveCount(0);            // off is not a fault
  await page.waitForTimeout(3_000);                                  // longer than one feed poll
  expect(server.feedCalls).toEqual([]);                              // no feed asked of an off bot
  await page.screenshot({ path: "test-results/adaptive-lab-off.png", fullPage: false });
  await page.locator(".pa-bottom nav").getByRole("button", { name: /journal/ }).click();
  await expect(page.getByTestId("adaptive-journal")).toContainText("ENTER LONG");   // history stays

  await off.getByRole("button", { name: "Turn on · Automatic paper" }).click();
  await expect.poll(() => server.saves.length).toBe(1);
  expect(server.saves[0]).toEqual({ mode: "automatic" });
  await expect(page.getByTestId("adaptive-off")).toHaveCount(0);
  await expect(page.locator(".smc-chart-canvas canvas").first()).toBeVisible();
  await expect(page.locator(".pa-market-readout")).toContainText("0.5462 / 0.5464");
  await expect(page.getByTestId("adaptive-saved-configuration")).toContainText("Automatic paper");
});

test("Adaptive MTF Lab shows what the Trading Instance does, view only, and can switch to its own bot", async ({ page }) => {
  const server = await labServer(page, { instance: { running: true } });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/#/adaptive-mtf-lab");
  // With an instance running this strategy, the lab shows it by default.
  const mirror = page.getByTestId("adaptive-mirror");
  await expect(mirror).toContainText("view only");
  await expect(mirror).toContainText("XRPUSDT 5m · Running · entries armed · risk 0.5% · allocation 1,000 USDT");
  await expect(page.getByLabel("Adaptive lab source")).toHaveValue(INSTANCE_ID);
  await expect(page.locator(".pa-safety")).toContainText("MIRROR · RUNNING · ENTRIES ARMED");
  // Nothing on the page can change the instance.
  for (const control of ["Adaptive lab mode", "Adaptive lab risk per trade", "Adaptive lab symbol"]) {
    await expect(page.getByLabel(control)).toHaveCount(0);
  }
  await expect(page.getByTestId("adaptive-saved-configuration")).toHaveCount(0);

  // Its trade, drawn and listed from its own account.
  await expect(page.locator(".smc-chart-canvas canvas").first()).toBeVisible();
  await expect(page.locator(".pa-market-readout")).toContainText("0.5462 / 0.5464");
  await expect(page.locator(".pa-table")).toContainText("0.548");
  await expect(page.locator(".pa-table")).toContainText("short");
  await page.locator(".pa-bottom nav").getByRole("button", { name: /trades/ }).click();
  await expect(page.locator(".pa-table")).toContainText("18.3");
  await page.locator(".pa-bottom nav").getByRole("button", { name: /journal/ }).click();
  const journal = page.getByTestId("adaptive-journal");
  await expect(journal.locator("tr.is-signal")).toHaveCount(2);
  await expect(journal).toContainText("ENTER SHORT");
  await expect(journal.locator(".adaptive-evidence")).toHaveText(/engine report/i);   // older row, labelled
  await expect(journal).toContainText("quality 80/100 | RR 2.60");
  await page.screenshot({ path: "test-results/adaptive-lab-instance.png", fullPage: false });
  expect(server.saves).toEqual([]);                                             // no write, ever
  for (const path of ["/paper", "/live-chart", "/journal", "/state"]) {
    expect(server.reads.filter((row) => row.path === path).at(-1)?.source).toBe(INSTANCE_ID);
  }

  // The lab's own bot is one choice away, and the choice is remembered.
  await page.getByLabel("Adaptive lab source").selectOption("lab");
  await expect(page.getByTestId("adaptive-saved-configuration")).toContainText("XRPUSDT 5m · Automatic paper");
  await page.locator(".pa-bottom nav").getByRole("button", { name: /positions/ }).click();
  await expect(page.locator(".pa-table")).toContainText("0.5321");
  await page.reload();
  await expect(page.getByLabel("Adaptive lab source")).toHaveValue("lab");
  await expect(page.getByTestId("adaptive-saved-configuration")).toBeVisible();
  expect(server.saves).toEqual([]);
});

test("Adaptive MTF Lab mirroring a stopped Trading Instance says so and offers no way to start it", async ({ page }) => {
  const server = await labServer(page, { instance: { running: false } });
  await page.goto("/#/adaptive-mtf-lab");
  const off = page.getByTestId("adaptive-off");
  await expect(off).toContainText("The Trading Instance is stopped");
  await expect(off).toContainText("Start it from Trading Instances");
  await expect(off.getByRole("button")).toHaveCount(0);
  await expect(page.locator(".pa-health-scope")).toContainText("INSTANCE STOPPED");
  await page.waitForTimeout(3_000);
  expect(server.feedCalls.filter((row) => row.startsWith("instance"))).toEqual([]);
  await page.locator(".pa-bottom nav").getByRole("button", { name: /journal/ }).click();
  await expect(page.getByTestId("adaptive-journal")).toContainText("ENTER SHORT");   // history stays
  expect(server.saves).toEqual([]);
});
