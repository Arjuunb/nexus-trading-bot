import { test, expect } from "@playwright/test";
import { mockApi } from "./mock";

test("real paper ledger flows through authenticated API and dashboard without granting eligibility", async ({ page, context, request }) => {
  test.skip(process.env.NEXUS_RESEARCH_E2E !== "1", "Requires the isolated loopback acceptance API");
  const api = "http://127.0.0.1:18005";
  const state = await (await request.get(`${api}/__acceptance__/state`)).json();
  const errors: string[] = []; const mutations: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", req => { if (req.url().includes("/strategy-intelligence/") && req.method() !== "GET") mutations.push(new URL(req.url()).pathname); });
  await page.addInitScript(() => { (window as any).__HUB_CONFIG__ = { apiBase: "http://127.0.0.1:18005" }; });
  await context.addCookies([{ name: "fixture_owner", value: "owner", domain: "127.0.0.1", path: "/", sameSite: "Lax" }]);
  await mockApi(page);
  // The intelligence routes use real HTTP, FastAPI authorization, services,
  // persisted SQLite evidence, and the actual test paper ledger. Only unrelated
  // application navigation endpoints use the common dashboard fixtures.
  await page.route("**/api/v2/strategy-intelligence/**", route => route.continue());
  // Supply the fixture's test identity to actual loopback HTTP requests. The
  // registered router still resolves and checks ownership on every request.
  await page.route("http://127.0.0.1:18005/**", async route => {
    const response = await route.fetch({ headers: { ...route.request().headers(), cookie: "fixture_owner=owner" } });
    await route.fulfill({ response, headers: { ...response.headers(), "access-control-allow-origin": "http://localhost:4173", "access-control-allow-credentials": "true" } });
  });
  await page.goto("/#/strategy-intelligence?tab=edge-score");
  await page.getByLabel("Research cohort").selectOption({ index: 1 });
  await page.getByLabel("Research asset").fill("XRPUSDT");
  await expect(page.getByText("UNSCORED", { exact: true })).toBeVisible();
  await expect(page.getByText("Qualification: NOT_QUALIFIED")).toBeVisible();
  await page.goto("/#/strategy-intelligence?tab=selection");
  await page.getByLabel("Research cohort").selectOption({ index: 1 });
  await page.getByLabel("Research asset").fill("XRPUSDT");
  await expect(page.getByText("INSUFFICIENT_EVIDENCE", { exact: true })).toBeVisible();
  await expect(page.getByText("Recommendation: None")).toBeVisible();
  await page.goto("/#/strategy-intelligence?tab=edge-monitoring");
  await page.getByLabel("Monitoring cohort").selectOption({ index: 1 });
  await page.getByLabel("Monitoring asset").fill("XRPUSDT");
  await expect(page.getByText("UNKNOWN", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Rolling edge windows" })).toHaveCount(0);
  await page.goto("/#/strategy-intelligence?tab=findings");
  await page.getByLabel("Monitoring cohort").selectOption({ index: 1 });
  await page.getByLabel("Monitoring asset").fill("XRPUSDT");
  await page.getByRole("button", { name: "Generate weekly report" }).click();
  await expect(page.getByRole("alert")).toContainText("HTTP 409");
  await page.goto("/#/strategy-intelligence?tab=champion-challenger");
  await page.getByLabel("Research champion", { exact: true }).selectOption(state.champion);
  await page.getByLabel("Research challenger", { exact: true }).selectOption(state.challenger);
  await page.getByRole("button", { name: "Run advisory comparison" }).click();
  await expect(page.getByRole("status")).toContainText("CHALLENGER_PREFERRED");
  const after = await (await request.get(`${api}/__acceptance__/state`)).json();
  expect(after).toMatchObject({ financial_records_unchanged: true, original_events_unchanged: true,
    validation_records_unchanged: true, actual_episode_count: 1 });
  expect(mutations).toEqual(["/api/v2/strategy-intelligence/weekly-research", "/api/v2/strategy-intelligence/comparisons"]);
  expect(errors).toEqual([]);
});
