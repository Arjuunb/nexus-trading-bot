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
  await expect(page.getByRole("tab")).toHaveText(["Command Center", "Incidents", "System Map", "Strategies",
    "Risk & Integrity", "Research", "Ask Guardian", "Reports & Recovery", "Activity"]);
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

// Phase 4. The fixture's reports come from the real integrity monitor over the
// scene: the strategy's open trade, an SMC-lab paper position and the real
// journal -- and, for the findings view, a copy of the ledger with its
// position row deleted.
test("exposure is added up across instance and lab, and live stays apart", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=integrity");
  await expect(page.getByTestId("guardian-integrity-clean")).toContainText("Every stage reconciles across 2 account(s)");
  const exposure = page.getByTestId("guardian-exposure");
  await expect(exposure).toContainText("BTCUSDT");
  await expect(exposure).toContainText("MAIN, SMC_LAB");
  await expect(page.getByText("one bet, not 2")).toBeVisible();
  await expect(page.getByTestId("guardian-live-exposure")).toContainText("LIVE ROUTING LOCKED");
  await expect(page.getByTestId("guardian-live-exposure")).toContainText("No live positions.");
});

test("a fill without its position is shown as a HIGH finding", async ({ page }) => {
  await mockApi(page);
  await page.route((url) => url.pathname === "/guardian/integrity",
    (route) => route.fulfill({ json: GUARDIAN.integrity_with_findings }));
  await page.goto("/#/guardian?tab=integrity");
  const findings = page.getByTestId("guardian-integrity-findings");
  await expect(findings).toContainText("a fill whose position or trade record does not exist");
  await expect(findings).toContainText("HIGH");
});

test("research shows each idea's stages in order, and only a recommended idea can be approved", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=research");
  await expect(page.getByTestId("guardian-research-boundary")).toContainText("An observed correlation is not a proven improvement");
  const cards = page.getByTestId("guardian-hypothesis");
  await expect(cards).toHaveCount(3);
  const recommended = cards.filter({ hasText: "three_candle v1" });
  await expect(recommended).toContainText("RECOMMENDED");
  await expect(recommended).toContainText("may improve");
  // Observation through recommendation passed, in order; the owner's stage waits on the owner.
  await expect(recommended.getByText("PASS", { exact: true })).toHaveCount(9);
  await expect(recommended.locator(".gd-stage-here")).toContainText("owner approval");
  await expect(recommended.locator(".gd-stage-here")).toContainText("WAITING");
  const waiting = cards.filter({ hasText: "pin_bar v2" });
  await expect(waiting).toContainText("WAITING");
  await expect(waiting.getByRole("button", { name: "Approve for development" })).toBeDisabled();
  // A rejected idea is kept as history with no controls: it is never rediscovered.
  const rejected = cards.filter({ hasText: "ema_pullback v1" });
  await expect(rejected).toContainText("REJECTED BY EVIDENCE");
  await expect(rejected.getByTestId("guardian-owner-controls")).toHaveCount(0);
  // Approval is an explicit owner action with its note; nothing else is sent.
  await recommended.getByLabel("Owner note").fill("build as a candidate");
  const [request] = await Promise.all([
    page.waitForRequest((r) => r.method() === "POST" && r.url().includes("/guardian/research/1/action")),
    recommended.getByRole("button", { name: "Approve for development" }).click(),
  ]);
  expect(request.postDataJSON()).toEqual({ action: "APPROVE_FOR_DEVELOPMENT", note: "build as a candidate" });
  await expect(page.getByRole("button", { name: /optimi[sz]e/i })).toHaveCount(0);
  await expect(page.getByTestId("guardian-analyst")).toContainText("pin_bar");
});

test("Ask Guardian shows the answer, its checked citations and what it ignored", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=ask");
  await page.getByLabel("Question for Guardian").fill("Why did every feed stop at once?");
  await page.getByRole("button", { name: "Ask" }).click();
  const answer = page.getByTestId("guardian-answer");
  await expect(answer).toContainText(GUARDIAN.reasoning_answer.answer);
  await expect(answer).toContainText("HIGH CONFIDENCE");
  await expect(answer).toContainText(GUARDIAN.reasoning_answer.citations[0]);
  await expect(answer).toContainText("Not in the evidence (ignored): incident:9999");
  await expect(answer).toContainText("evidence pack sha256");
});

test("with no API key the reasoning layer says it is off and sends nothing", async ({ page }) => {
  await mockApi(page);
  await page.route((url) => url.pathname === "/guardian/reasoning", (route) => route.fulfill({ json: GUARDIAN.reasoning }));
  await page.goto("/#/guardian?tab=ask");
  await expect(page.getByTestId("guardian-reasoning-off")).toContainText("Nothing is sent anywhere");
  await page.getByLabel("Question for Guardian").fill("anything");
  await expect(page.getByRole("button", { name: "Ask" })).toBeDisabled();
});

test("reports say how much Guardian observed, and recovery is diagnostics only by default", async ({ page }) => {
  await mockApi(page);
  await page.goto("/#/guardian?tab=reports");
  const reports = page.getByTestId("guardian-reports");
  await expect(reports).toContainText("Guardian observed 75.0% of it");
  await expect(reports).toContainText("of the time Guardian observed");
  await expect(reports).toContainText("Trades: not measured");
  const policies = page.getByTestId("guardian-recovery-policies");
  await expect(policies.locator("li").filter({ hasText: "restart instance worker" })).toContainText("OFF");
  await expect(policies.locator("li").filter({ hasText: "gather diagnostics" })).toContainText("AUTOMATIC");
  await expect(page.getByTestId("guardian-recovery-history")).toContainText("gather diagnostics");
  await expect(page.getByText("NO_CHANNEL").first()).toBeVisible();          // no Telegram: said, not hidden
});
