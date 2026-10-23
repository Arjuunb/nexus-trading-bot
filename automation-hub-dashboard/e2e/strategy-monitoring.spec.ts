import { test, expect, type Page } from "@playwright/test";
import { mockApi } from "./mock";

const cohort = {strategy_id:"fixture_strategy",strategy_version:"1",config_fingerprint:"a".repeat(64),
  instance_id:"fixture-instance",simulation_session_id:"fixture-session",lab_id:null,execution_mode:"paper",source_kind:"forward_paper"};
const metrics = {completed_episode_count:20,expectancy_net_r:"-0.2",net_profit_factor:"0.7",max_drawdown_r:"4",net_pnl:"-20",net_r:"-4"};
const decay = {contract_version:"strategy_intelligence.v2",identity:{...cohort,symbol:"TESTUSDT"},cache_status:"READY",status:"PAUSE_REVIEW",suggested_lifecycle_state:"PAUSED",
  action_status:"PENDING_APPROVAL",policy:{version:"edge_decay.v1"},windows:[20,50,100].map(window_size=>({window_size,status:"DEGRADING",metrics:{...metrics,completed_episode_count:window_size},reasons:["EXPECTANCY_BELOW_BASELINE_RETENTION"]}))};
const review = {review_id:"weekly-fixture",identity:{...cohort,symbol:"TESTUSDT"},period:{start:"2025-03-24",end:"2025-03-31"},
  status:"COMPLETE",current:{metrics,episode_ids:["episode-a"]},previous:{metrics:{...metrics,expectancy_net_r:"0.3"},episode_ids:["episode-b"]},
  comparison:{status:"COMPARABLE",expectancy_change_r:"-0.5"},observations:{mae_r:{status:"UNKNOWN",mean:null,sample_size:0}},
  findings:[{kind:"WEEKLY_EXPECTANCY_DECLINE",episode_ids:["episode-a","episode-b"]}],
  proposals:[{proposal_id:"proposal-a",status:"PENDING_APPROVAL",question:"Investigate observed deterioration",episode_ids:["episode-a"]}],
  groups:{trend_regime:[{value:"BEAR",metrics,episode_ids:["episode-a"],pattern_sample_sufficient:true}]},
  missed_opportunities:{status:"UNKNOWN"},violations:{status:"UNKNOWN"}};

async function setup(page:Page, unknown=false) {
  await mockApi(page);
  await page.route("**/api/v2/strategy-intelligence/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith("/cohorts")) return route.fulfill({json:{cohorts:[cohort]}});
    if(path.endsWith("/decay")) return route.fulfill({json:unknown?{cache_status:"PENDING",status:"UNKNOWN",windows:[],blocking_reasons:["FINANCIAL_EVIDENCE_INCOMPLETE"]}:decay});
    return route.fulfill({json:{cache_status:"HISTORICAL_SNAPSHOT",rows:[review]}});
  });
}
async function select(page:Page) {
  await page.getByLabel("Monitoring cohort").selectOption({index:1});
  await page.getByLabel("Monitoring asset").fill("TESTUSDT");
}

test("decay windows require an exact cohort and never offer execution actions",async({page})=>{
  const errors:string[]=[];const requests:string[]=[];
  page.on("pageerror",error=>errors.push(error.message));
  page.on("console",message=>{if(["error","warning"].includes(message.type()))errors.push(message.text());});
  page.on("request",r=>{if(r.url().includes("/decay"))requests.push(r.url());});
  await setup(page);await page.goto("/#/strategy-intelligence?tab=edge-monitoring");
  await expect(page.getByRole("heading",{name:"Edge Monitoring",exact:true})).toBeVisible();
  expect(requests).toHaveLength(0);await select(page);
  await expect(page.getByRole("table",{name:"Rolling edge windows"})).toBeVisible();
  await expect(page.getByText("PAUSE_REVIEW",{exact:true})).toBeVisible();
  await expect(page.getByText("Suggested state: PAUSED · PENDING_APPROVAL")).toBeVisible();
  await expect(page.getByRole("button",{name:/pause|resume|apply|promote/i})).toHaveCount(0);
  const params=new URL(requests[0]).searchParams;
  for(const key of ["instance_id","simulation_session_id","strategy_id","strategy_version","config_fingerprint","execution_mode","source_kind"] as const)expect(params.get(key)).toBe(cohort[key]);
  expect(params.has("owner_id")).toBe(false);expect(params.has("account_id")).toBe(false);
  await page.setViewportSize({width:390,height:844});
  await expect.poll(()=>page.locator(".sidebar").evaluate(el=>el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:"../validation/task4/monitoring-mobile.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("incomplete monitoring never renders fabricated zero performance",async({page})=>{
  await setup(page,true);await page.goto("/#/strategy-intelligence?tab=edge-monitoring");await select(page);
  await expect(page.getByText("FINANCIAL_EVIDENCE_INCOMPLETE")).toBeVisible();
  await expect(page.getByRole("table",{name:"Rolling edge windows"})).toHaveCount(0);
});

test("weekly report creation is explicit, scoped and has no proposal apply control",async({page})=>{
  await setup(page);const writes:unknown[]=[];
  await page.route("**/api/v2/strategy-intelligence/weekly-research**",route=>{
    if(route.request().method()==="POST"){writes.push(route.request().postDataJSON());return route.fulfill({status:201,json:review});}
    return route.fulfill({json:{cache_status:"HISTORICAL_SNAPSHOT",rows:[review]}});
  });
  await page.goto("/#/strategy-intelligence?tab=findings");await select(page);
  await expect(page.getByText("weekly-fixture",{exact:true})).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.getByRole("button",{name:"Generate weekly report"}).click();
  await expect(page.getByRole("status")).toContainText("Report saved");
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({...cohort,symbol:"TESTUSDT"});
  expect(writes[0]).not.toHaveProperty("owner_id");expect(writes[0]).not.toHaveProperty("account_id");
  await expect(page.getByText("PENDING_APPROVAL",{exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:/apply|approve|promote/i})).toHaveCount(0);
  await page.getByRole("heading",{name:"Findings and proposals"}).scrollIntoViewIfNeeded();
  await page.screenshot({path:"../validation/task4/weekly-research.png",fullPage:true});
});

test("changing asset cannot leave another asset's weekly report visible",async({page})=>{
  await setup(page);await page.goto("/#/strategy-intelligence?tab=findings");await select(page);
  await expect(page.getByText("weekly-fixture",{exact:true})).toBeVisible();
  await page.getByLabel("Monitoring asset").fill("ETHUSDT");
  await expect(page.getByText("weekly-fixture",{exact:true})).toHaveCount(0);
});
