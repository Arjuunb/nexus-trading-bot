import { test, expect, type Page } from "@playwright/test";
import { mockApi } from "./mock";

const cohort = { instance_id: "one", simulation_session_id: "session", lab_id: null,
  strategy_id: "fixture-strategy", strategy_version: "1", config_fingerprint: "a".repeat(64),
  execution_mode: "paper", source_kind: "forward_paper" };
const context = { symbol: "TESTUSDT", session: "LONDON", trend_regime: "BULL", volatility_regime: "NORMAL", entry_timeframe: "5m" };
const candidate = { cohort: { ...cohort, symbol: "TESTUSDT" }, eligible: true, qualification_status: "QUALIFIED",
  metrics: { completed_episode_count: 40, expectancy_net_r: "0.3", net_profit_factor: "1.5", max_drawdown_r: "2" },
  evidence: { cache_run_id: "source-cache", episode_ids: ["source-episode"], validation_run_ids: { out_of_sample: "source-oos" } } };

async function setup(page: Page, response: object) {
  await mockApi(page);
  await page.route("**/api/v2/strategy-intelligence/**", route => route.fulfill({ json:
    new URL(route.request().url()).pathname.endsWith("/cohorts") ? { cohorts: [cohort] } : response }));
}
async function select(page: Page) {
  await page.getByLabel("Research cohort").selectOption({ index: 1 });
  await page.getByLabel("Research asset").fill("TESTUSDT");
}

test("EdgeScore explains a versioned descriptive score without execution controls", async ({ page }) => {
  const writes: string[] = []; const errors: string[] = [];
  page.on("request", req => { if (req.url().includes("/strategy-intelligence/") && req.method() !== "GET") writes.push(req.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await setup(page, { status: "SCORED", cache_status: "READY", identity: { ...cohort, symbol: "TESTUSDT" },
    score: "75", qualification_status: "QUALIFIED", policy: { policy_version: "edge_score.v1" },
    components: [{ name: "expectancy", weight: "15", status: "KNOWN", normalized_score: "50", weighted_points: "7.5", formula: "Observed net R / target", reason: "COMPLETE_BOUND_EVIDENCE" }] });
  await page.goto("/#/strategy-intelligence?tab=edge-score"); await select(page);
  await expect(page.getByText("SCORED", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "EdgeScore components" })).toContainText("7.5");
  await expect(page.getByText("75", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /apply|promote|execute|trade/i })).toHaveCount(0);
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

test("unknown score stays unavailable and changing asset clears old evidence", async ({ page }) => {
  await setup(page, { status: "UNSCORED", cache_status: "READY", identity: { ...cohort, symbol: "TESTUSDT" }, score: null,
    blocking_reasons: ["FINANCIAL_EVIDENCE_INCOMPLETE"] });
  await page.goto("/#/strategy-intelligence?tab=edge-score"); await select(page);
  await expect(page.getByText("FINANCIAL_EVIDENCE_INCOMPLETE")).toBeVisible();
  await expect(page.getByText("UNSCORED", { exact: true })).toBeVisible();
  await page.getByLabel("Research asset").fill("FOREIGNUSDT");
  await expect(page.getByRole("alert")).toHaveText("Evidence does not match this request.");
  await expect(page.getByText("FINANCIAL_EVIDENCE_INCOMPLETE")).toHaveCount(0);
});

test("selection binds exact context, displays sample and lineage, and clears changed contexts", async ({ page }) => {
  const requests: URL[] = [];
  page.on("request", req => { if (new URL(req.url()).pathname.endsWith("/selection")) requests.push(new URL(req.url())); });
  await setup(page, { status: "RECOMMENDED", cache_status: "READY", context, candidates: [candidate], recommendation: candidate });
  await page.goto("/#/strategy-intelligence?tab=selection"); await select(page);
  await expect(page.getByText("RECOMMENDED", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Context selection evidence" })).toContainText("40");
  await page.getByText("Source references").click();
  await expect(page.getByText(/Cache: source-cache/)).toBeVisible();
  expect(requests[0].searchParams.get("trend_regime")).toBe("BULL");
  for (const field of ["owner_id", "account_id", "strategy_id", "config_fingerprint"]) expect(requests[0].searchParams.has(field)).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.locator(".sidebar").evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "../validation/task5/selection-mobile.png", fullPage: true });
  await page.getByLabel("trend_regime", { exact: true }).selectOption("BEAR");
  await expect(page.getByText("RECOMMENDED", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveText("Evidence does not match this request.");
});

test("stale or foreign selection cannot show a recommendation", async ({ page }) => {
  await setup(page, { status: "RECOMMENDED", cache_status: "RECONCILIATION_REQUIRED", context,
    candidates: [candidate], recommendation: candidate });
  await page.goto("/#/strategy-intelligence?tab=selection"); await select(page);
  await expect(page.getByText("Recommendation: None")).toBeVisible();
  await expect(page.getByRole("table", { name: "Context selection evidence" })).toHaveCount(0);
  await setup(page, { status: "RECOMMENDED", cache_status: "READY", context,
    candidates: [{ ...candidate, cohort: { ...candidate.cohort, instance_id: "foreign" } }], recommendation: candidate });
  await page.reload(); await select(page);
  await expect(page.getByRole("alert")).toHaveText("Evidence does not match this request.");
  await expect(page.getByText("Recommendation: None")).toBeVisible();
});
