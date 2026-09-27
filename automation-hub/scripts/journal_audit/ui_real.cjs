// AUDIT: the Journal UI on the real backend serving the audit data dir.
// Reads what the page SHOWS and prints it next to what the API returns for
// the same view, so the two can be compared. Screenshots go to argv[2].
const path = require("path");
const { chromium } = require(path.join(__dirname, "../../../automation-hub-dashboard/node_modules/@playwright/test"));
const BASE = process.env.AUDIT_BASE || "http://127.0.0.1:8777";
const OUT = process.argv[2];
(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  const apiCalls = new Set();
  page.on("console", (m) => { if (m.type() === "error") problems.push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => problems.push("pageerror " + String(e).slice(0, 200)));
  page.on("response", (r) => {
    if (r.url().includes("/journal/")) apiCalls.add(r.url().replace(BASE, "").split("?")[0]);
    if (r.status() >= 400) problems.push(`${r.status()} ${r.url()}`);
  });
  await page.goto(BASE + "/login");
  await page.fill('input[name="username"]', "admin");
  await page.fill('input[name="password"]', "admin");
  await Promise.all([page.waitForNavigation(), page.click('button[type="submit"]')]);
  const api = async (p) => page.evaluate(async (u) => (await fetch(u, { credentials: "include" })).json(), p);
  const res = {};

  // 1. Trades tab: KPIs and rows
  await page.goto(BASE + "/#/journal");
  await page.locator("table.jr-table tbody tr").first().waitFor();
  await page.waitForTimeout(1500);
  res.kpis_ui = await page.locator(".stat-row.six").innerText();
  res.rows_ui = await page.locator("table.jr-table tbody tr").count();
  res.subtitle_ui = await page.locator(".card").filter({ hasText: "Trade records" }).first().locator("p, .subtitle, .card-sub").first().innerText().catch(() => "");
  const a = await api("/journal/records?limit=200");
  res.kpis_api = a.kpis; res.total_api = a.total; res.rows_api = a.records.length;
  await page.screenshot({ path: OUT + "/1-trades-forward.png" });

  // 2. origin chips: legacy, simulation
  for (const [chip, origin, shot] of [["Legacy / unverified", "LEGACY_MIGRATION", "2-legacy"], ["Simulation", "SIMULATION", "3-simulation"]]) {
    await page.getByRole("button", { name: new RegExp("^" + chip.replace("/", "\\/")) }).click();
    await page.waitForTimeout(1500);
    const rowsText = await page.locator("table.jr-table tbody").innerText();
    const kp = await page.locator(".stat-row.six").innerText();
    const r = await api(`/journal/records?origin=${origin}&limit=200`);
    res[origin] = { rows_ui: await page.locator("table.jr-table tbody tr").count(), rows_api: r.records.length,
                    kpis_ui: kp.replace(/\n+/g, " | "), kpis_api: r.kpis, sample: rowsText.slice(0, 400) };
    await page.screenshot({ path: `${OUT}/${shot}.png` });
  }
  await page.getByRole("button", { name: /^Forward paper/ }).click();
  await page.waitForTimeout(1200);

  // 3. the real-strategy trade record: detail sections and timeline
  await page.getByRole("button", { name: /Open BTCUSDT long trade record/ }).first().click();
  await page.locator(".jr-stage").first().waitFor();
  await page.waitForTimeout(1000);
  res.detail_sections = await page.locator(".jr-section-head h3").allInnerTexts();
  res.detail_stages = await page.locator(".jr-stage").allInnerTexts();
  res.detail_text = (await page.locator(".jr-sections").first().innerText()).slice(0, 2500);
  const rid = page.url().split("/trade/")[1];
  res.detail_id = rid;
  await page.screenshot({ path: OUT + "/4-detail.png", fullPage: true });

  // 4. Decisions tab
  await page.goto(BASE + "/#/journal?tab=decisions");
  await page.locator("table.jr-table tbody tr").first().waitFor();
  await page.waitForTimeout(1500);
  res.decision_chips = await page.locator(".chips.jr-origins").nth(1).innerText();
  res.decision_rows_ui = await page.locator("table.jr-table tbody tr").count();
  const d = await api("/journal/decision-records?limit=200");
  res.decision_total_api = d.total; res.decision_by_type_api = d.by_type;
  await page.screenshot({ path: OUT + "/5-decisions.png" });
  const brain = page.locator("table.jr-table tbody tr").filter({ hasText: "GATE_REJECTED: BRAIN" }).first();
  res.brain_row = await brain.innerText();
  await brain.click();
  await page.waitForTimeout(1200);
  res.brain_detail = (await page.locator(".jr-why").innerText().catch(() => "")).slice(0, 400);
  await page.screenshot({ path: OUT + "/6-brain-decision.png" });

  // 5. Memory tab
  await page.goto(BASE + "/#/journal?tab=memory");
  await page.waitForTimeout(2000);
  res.memory_tables = (await page.locator("table.jr-table").allInnerTexts()).map((t) => t.slice(0, 700));
  res.memory_api = await api("/journal/memory");
  await page.screenshot({ path: OUT + "/7-memory.png", fullPage: true });

  // 6. Weekly tab
  await page.goto(BASE + "/#/journal?tab=weekly");
  await page.waitForTimeout(2000);
  res.weekly_ui = (await page.locator(".card").filter({ hasText: /eekly/ }).first().innerText()).slice(0, 1500);
  await page.screenshot({ path: OUT + "/8-weekly.png", fullPage: true });

  // 7. Notes tab
  await page.goto(BASE + "/#/journal?tab=notes");
  await page.waitForTimeout(1500);
  res.notes_ui = (await page.locator(".jr-notes").innerText().catch(() => "")).slice(0, 300);
  await page.screenshot({ path: OUT + "/9-notes.png" });

  res.api_calls = [...apiCalls].sort();
  res.problems = problems;
  console.log(JSON.stringify(res, null, 1));
  await browser.close();
})();
