import { expect, test } from "@playwright/test";
import { GUARDIAN, mockApi } from "./mock";

// The fixture is the real Guardian service's output over a fixed scene: an
// instance whose candles went stale (its feed FAILED, the instance BLOCKED by
// it), the SMC lab synchronised, the PA lab idle, and a journal that could not
// read a Supabase ledger.
const INSTANCE = "a3f9c2d1e8b74c0f";

test("Guardian is one sidebar entry and opens on the Command Center", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/dashboard");
  await page.locator("nav.nav").getByRole("button", { name: "Guardian" }).click();
  await expect(page).toHaveURL(/#\/guardian/);
  const headline = page.getByTestId("guardian-headline");
  await expect(headline).toContainText(GUARDIAN.status.summary.state);
  // The verdict names what failed, from the components alone.
  await expect(headline).toContainText("has failed");
  await expect(page.getByTestId("guardian-group-instance")).toContainText("BLOCKED");
  await expect(page.getByTestId("guardian-group-journal")).toContainText("DEGRADED");
  await expect(page.getByText("READ-ONLY", { exact: true })).toBeVisible();
  // Only built tabs are offered.
  await expect(page.getByRole("tab")).toHaveText(["Command Center", "Incidents", "System Map", "Strategies", "Activity"]);
  // The open incident is on the Command Center; anomalies say when there is no baseline yet.
  await expect(page.getByTestId("guardian-open-incidents")).toContainText(GUARDIAN.incidents.incidents[0].title);
  await expect(page.getByText("not enough history yet to judge")).toBeVisible();
});

test("the system map says an instance is blocked by its feed, not broken", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=map");
  const node = page.getByTestId(`guardian-node-instance:${INSTANCE}`);
  await expect(node).toContainText("BLOCKED");
  await expect(node).toContainText(`Blocked by feed:instance:${INSTANCE}`);
  await expect(page.getByTestId(`guardian-node-feed:instance:${INSTANCE}`)).toContainText("FAILED");
  await expect(page.getByTestId("guardian-node-journal")).toContainText("ledger is not a local SQLite ledger");
  await node.click();
  await expect(page).toHaveURL(new RegExp(`tab=activity&component=instance%3A${INSTANCE}`));
  await expect(page.getByLabel("Component")).toHaveValue(`instance:${INSTANCE}`);
});

test("activity lists the recorded events with their state changes", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=activity");
  const table = page.getByTestId("guardian-events");
  await expect(table.locator("tbody tr")).toHaveCount(GUARDIAN.events.events.length);
  await expect(table).toContainText("health changed");
  await expect(table).toContainText("HEALTHY → BLOCKED");
  await expect(table).toContainText(`instance:${INSTANCE}`);
});

// Phase 2. The strategy fixture is real strategy output: the 3-Candle Rejection
// strategy through the engine, the frozen SMC strategy through its agent and
// the frozen Price Action engine through its lab (pin-bar experiment on).
test("the Strategies tab shows what each strategy attempted and why it did not trade", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=strategies");
  const tiles = page.getByTestId("guardian-strategies");
  for (const card of GUARDIAN.strategies.strategies) await expect(tiles).toContainText(card.strategy_id);
  const instance = page.getByTestId(`guardian-strategy-instance:${INSTANCE}`);
  await expect(instance).toContainText("EMA_TREND_NOT_ALIGNED");
  await expect(instance).toContainText("1 entries");
  await expect(page.getByTestId("guardian-strategy-lab:pa")).toContainText("MISSING_PIN_BAR_ONLY");
  // Near-valid setups are never presented as a verdict on a rule.
  await expect(page.getByTestId("guardian-almost-note")).toContainText("does not mean the rule was wrong");
  const almost = page.getByTestId("guardian-almost-trades");
  await expect(almost).toContainText("Bullish rejection");
  await expect(almost).toContainText("6/7");
  // Research is Phase 5 and says so.
  await expect(page.getByText(GUARDIAN.strategies.research.note)).toBeVisible();
});

test("a decision trace shows every condition, the one that stopped it, and what was never reached", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=strategies");
  await page.getByTestId(`guardian-strategy-instance:${INSTANCE}`).click();       // filter to the instance
  const almost = page.getByTestId("guardian-almost-trades");
  await expect(almost.locator("li")).toHaveCount(1);
  await almost.getByRole("button", { name: "Trace" }).click();
  const trace = page.getByTestId("guardian-trace");
  const row = (label: string) => trace.locator("tr", { hasText: label });
  await expect(row("Candle 3 closed beyond the rejection candle")).toContainText("PASS");
  await expect(row("EMA 9 on the trade's side of EMA 33")).toContainText("FAIL");
  await expect(row("EMA 9 on the trade's side of EMA 33")).toContainText("EMA_TREND_NOT_ALIGNED");
  await expect(row("Decision Brain quality gate")).toContainText("NOT REACHED");
  await expect(trace).toContainText("NO_SETUP");
});

// Phase 3. The fixture's incidents come from the real incident engine over the
// scene: a Binance outage that stalled both instances and the SMC lab (one
// incident, closed after verification), then one instance's own feed failing,
// recovering and failing again before its recovery was verified.
test("one outage is one incident, and a closed incident is kept with its diagnosis and timeline", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=incidents");
  const list = page.getByTestId("guardian-incidents");
  await expect(list.locator("li")).toHaveCount(1);                       // open only
  await expect(list).toContainText("HIGH CONFIDENCE");
  await page.getByRole("button", { name: "All" }).click();
  await expect(list.locator("li")).toHaveCount(2);
  const outage = list.locator("li", { hasText: "Binance USD-M market data not reaching the platform" });
  await expect(outage).toContainText("CLOSED");
  await expect(outage).toContainText("7 affected");
  await outage.getByRole("button", { name: "Details" }).click();
  const diagnosis = page.getByTestId("guardian-diagnosis");
  await expect(diagnosis).toContainText("independent consumers failed together");
  await expect(diagnosis).toContainText("advice only — Guardian takes no action");
  const timeline = page.getByTestId("guardian-timeline");
  for (const entry of ["incident opened", "incident recovered", "incident verified", "incident closed"]) {
    await expect(timeline).toContainText(entry);
  }
  await expect(timeline).toContainText("feed:lab:smc");
});

test("a fault that returns before recovery is verified reopens the same incident", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=incidents");
  const open = page.getByTestId("guardian-incidents").locator("li").first();
  await expect(open).toContainText("OPEN");
  await open.getByRole("button", { name: "Details" }).click();
  await expect(page.getByTestId("guardian-timeline")).toContainText("incident reopened");
});
