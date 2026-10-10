import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./mock";

// Synthetic fixtures exercise presentation; these are not historical results.
const cohort = {
  strategy_id: "fixture_strategy", strategy_version: "1.0.0", config_fingerprint: "a".repeat(64),
  instance_id: "fixture-instance", lab_id: null, simulation_session_id: "fixture-session",
  execution_mode: "forward_paper", source_kind: "executed", owner_id: "fixture-owner", account_id: "fixture-account",
};
const envelope = {
  contract_version: "strategy_intelligence.v2", calculation_timestamp: "2026-01-15T10:00:00Z",
  evidence_quality: "PARTIAL", cache_status: "READY",
};
const group = {
  group_key: "fixture-group", group: { symbol: "TESTUSDT", session: "NEW_YORK", trend_regime: "BULL" },
  classifier: { classifier_id: "fixture_classifier", classifier_version: "1.0.0", parameter_hash: "b".repeat(64) },
  metrics: { completed_episode_count: 17, win_rate_pct: "47.0588235294", net_profit_factor: "1.20", net_pnl: "2.125", fees: "0.75", funding: null },
  evidence_quality: { status: "PARTIAL", binding_status: "MATCHING", reasons: ["Funding coverage is unknown."] },
  context_quality: { status: "VALID", counts: { VALID: 17 } },
  cost_coverage: { status: "PARTIAL", fees: { status: "COMPLETE" }, funding: { status: "UNKNOWN" }, slippage: { status: "COMPLETE" }, verification_blockers: ["Missing funding costs"] },
  sample_confidence: { label: "INSUFFICIENT", completed_episodes: 17, warnings: ["Small subgroup; parent sample confidence does not apply."] },
  profitability: { status: "UNVERIFIED", observed_direction: "POSITIVE", verified: false, blockers: ["Missing funding costs"] },
};

async function intelligenceApi(page: Page, mode: "ready" | "pending" | "unauthorized" = "ready") {
  await mockApi(page);
  await page.route("**/api/v2/strategy-intelligence/**", async (route) => {
    const url = new URL(route.request().url());
    if (mode === "unauthorized") return route.fulfill({ status: 401, json: { detail: "Sign in required" } });
    if (url.pathname.endsWith("/cohorts")) return route.fulfill({ json: { ...envelope, cohorts: [cohort] } });
    if (mode === "pending") return route.fulfill({ json: { ...envelope, calculation_timestamp: null, cache_status: "PENDING", evidence_quality: "UNKNOWN", groups: [], contexts: [] } });
    if (url.pathname.endsWith("/performance")) return route.fulfill({ json: { ...envelope, cohort, groups: [group] } });
    return route.fulfill({ json: { ...envelope, contexts: [{
      snapshot_id: "fixture-snapshot", episode_id: "fixture-episode", trade_id: "fixture-trade",
      symbol: "TESTUSDT", entry_timeframe: "15m", higher_timeframe: "1h", session: "NEW_YORK",
      trend_regime: "BULL", volatility_regime: "HIGH", structure_regime: "UNKNOWN",
      signal_timestamp: "2026-01-15T09:30:00Z", entry_timestamp: "2026-01-15T09:30:15Z",
      last_closed_candle_timestamp: "2026-01-15T09:29:59Z", context_quality: "VALID", evidence_quality: "PARTIAL",
      classifier_id: "fixture_classifier", classifier_version: "1.0.0", parameter_hash: "b".repeat(64),
    }] } });
  });
}

test("context intelligence uses an explicit exact cohort and separates sample, evidence and costs", async ({ page }) => {
  const errors: string[] = [];
  const reads: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error" || message.type() === "warning") errors.push(message.text()); });
  page.on("request", (request) => { if (request.url().includes("/api/v2/strategy-intelligence/")) reads.push(request.url()); });
  await intelligenceApi(page);
  await page.goto("/#/analytics?tab=context");
  await expect(page.getByRole("heading", { name: "Context Intelligence", exact: true })).toBeVisible();
  await expect(page.getByLabel("Evidence cohort")).toBeVisible();
  expect(reads.some((url) => url.includes("/performance"))).toBe(false);
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  const performance = page.getByRole("table", { name: "Performance by context" });
  await expect(performance.getByText("TESTUSDT", { exact: true })).toBeVisible();
  await expect(performance.getByText("INSUFFICIENT", { exact: true })).toBeVisible();
  await expect(performance.getByText("UNVERIFIED", { exact: true })).toBeVisible();
  await performance.getByText("Explain status", { exact: true }).click();
  await expect(page.getByText("Funding: UNKNOWN", { exact: true })).toBeVisible();
  await expect(page.getByText("Small subgroup; parent sample confidence does not apply.")).toBeVisible();
  await expect(page.getByRole("table", { name: "Market context snapshots" }).getByText("HIGH", { exact: true })).toBeVisible();
  const request = new URL(reads.find((url) => url.includes("/performance"))!);
  for (const key of ["strategy_id", "strategy_version", "config_fingerprint", "instance_id", "simulation_session_id", "execution_mode", "source_kind"]) expect(request.searchParams.get(key)).toBe(cohort[key as keyof typeof cohort]);
  expect(request.searchParams.has("owner_id")).toBe(false);
  expect(request.searchParams.has("account_id")).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("pending intelligence never presents empty results as zero performance", async ({ page }) => {
  await intelligenceApi(page, "pending");
  await page.goto("/#/analytics?tab=context");
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  await expect(page.getByText("Intelligence calculation pending.", { exact: true })).toBeVisible();
  await expect(page.getByText("No completed episode groups available.", { exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Performance by context" })).toHaveCount(0);
});

test("unauthorized intelligence requests show a recoverable error without fabricated metrics", async ({ page }) => {
  await intelligenceApi(page, "unauthorized");
  await page.goto("/#/analytics?tab=context");
  await expect(page.getByRole("alert").filter({ hasText: "HTTP 401" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry cohort discovery" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Performance by context" })).toHaveCount(0);
});

test("incomplete costs cannot display verified profitability even if the payload flag is inconsistent", async ({ page }) => {
  await intelligenceApi(page);
  await page.route("**/api/v2/strategy-intelligence/performance?**", (route) => route.fulfill({ json: {
    ...envelope, groups: [{ ...group, profitability: { status: "VERIFIED", observed_direction: "POSITIVE", verified: true, blockers: [] } }],
  } }));
  await page.goto("/#/analytics?tab=context");
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  const performance = page.getByRole("table", { name: "Performance by context" });
  await expect(performance.getByText("UNVERIFIED", { exact: true })).toBeVisible();
  await expect(performance.getByText("VERIFIED", { exact: true })).toHaveCount(0);
});

test("unknown metrics stay unavailable and stale cache cannot verify profitability", async ({ page }) => {
  await intelligenceApi(page);
  await page.route("**/api/v2/strategy-intelligence/performance?**", (route) => route.fulfill({ json: {
    ...envelope, cache_status: "STALE", groups: [{ ...group,
      metrics: { completed_episode_count: 1, win_rate_pct: null, net_profit_factor: null, net_pnl: null },
      profitability: { status: "VERIFIED", verified: true, blockers: [] },
    }],
  } }));
  await page.goto("/#/analytics?tab=context");
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  await expect(page.getByText("Cached intelligence is stale. Profitability verification is unavailable.")).toBeVisible();
  const performance = page.getByRole("table", { name: "Performance by context" });
  await expect(performance.getByText("Unavailable", { exact: true })).toHaveCount(3);
  await expect(performance.getByText("UNVERIFIED", { exact: true })).toBeVisible();
});

test("a cache failure is an explicit evidence error rather than a valid empty result", async ({ page }) => {
  await intelligenceApi(page);
  await page.route("**/api/v2/strategy-intelligence/performance?**", (route) => route.fulfill({ json: {
    ...envelope, cache_status: "ERROR", evidence_quality: "UNKNOWN", groups: [], error_reason: "Intelligence cache unavailable",
  } }));
  await page.goto("/#/analytics?tab=context");
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  await expect(page.getByRole("alert").filter({ hasText: "Intelligence cache unavailable" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry performance" })).toBeVisible();
});

test("the versioned profitability flag requires independent complete evidence and cost gates", async ({ page }) => {
  await intelligenceApi(page);
  await page.route("**/api/v2/strategy-intelligence/performance?**", (route) => route.fulfill({ json: {
    ...envelope, groups: [{ ...group, profitability_verified: true,
      evidence_quality: { status: "COMPLETE", reasons: [] },
      cost_coverage: { status: "COMPLETE", fees: "COMPLETE", funding: "COMPLETE", slippage: "NOT_APPLICABLE" },
      profitability: { status: "VERIFIED", observed_direction: "POSITIVE", blockers: [] },
    }],
  } }));
  await page.goto("/#/analytics?tab=context");
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  await expect(page.getByRole("table", { name: "Performance by context" }).getByText("VERIFIED", { exact: true })).toBeVisible();
});

test("an identity binding conflict is an explicit blocked cache result", async ({ page }) => {
  await intelligenceApi(page);
  await page.route("**/api/v2/strategy-intelligence/performance?**", (route) => route.fulfill({ json: {
    ...envelope, cache_status: "CONFLICTED", evidence_quality: "CONFLICTED", groups: [],
    quality_reasons: ["CACHE_IDENTITY_BINDING_MISMATCH"],
  } }));
  await page.goto("/#/analytics?tab=context");
  await page.getByLabel("Evidence cohort").selectOption({ index: 1 });
  await expect(page.getByRole("alert").filter({ hasText: "Evidence cache identity conflict" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Performance by context" })).toHaveCount(0);
});
