import {expect,test} from "@playwright/test";
import {mockApi} from "./mock";

// UI contract fixtures only. Backend tests separately prove these attestations.
test("active evidence health exposes frozen identity, optional unknowns and timeline",async({page})=>{
  await mockApi(page);
  const errors:string[]=[];page.on("pageerror",e=>errors.push(e.message));
  const start="2026-10-11T12:00:00Z",end="2026-10-11T12:05:00Z";
  await page.route("**/api/v2/strategy-intelligence/**",async route=>{
    const url=new URL(route.request().url()),path=url.pathname;
    if(path.endsWith("/shadow/registrations"))return route.fulfill({json:{rows:[{registration_id:"r",status:"OBSERVING"}]}});
    if(path.endsWith("/shadow/active-sources"))return route.fulfill({json:{rows:[{source_id:"pa-lab:s",coverage_class:"PARTIAL_DECISION_EVIDENCE"}]}});
    if(path.endsWith("/health"))return route.fulfill({json:{source:{source_id:"pa-lab:s"},start:url.searchParams.get("start"),end:url.searchParams.get("end"),status:"VERIFIED",comparison_readiness:"ACTIVE_EVIDENCE_READY",expected_opportunities:1,recorded_opportunities:1,verified_opportunities:1,configuration_completeness:1,market_context_completeness:1,active_decision_coverage:1,last_verified_observation:start,reconciliation_backlog:0,reconciliation_status:"COMPLETE",field_matrix:{config_fingerprint:"PRESENT_VERIFIED",canonical_candle:"PRESENT_VERIFIED",session:"UNKNOWN"},optional_fields:["session"],blocking_reasons:{}}});
    if(path.includes("/active-sources/")&&path.endsWith("/decisions"))return route.fulfill({json:{rows:[{decision_id:"active-1",status:"NO_SIGNAL",config_fingerprint:"a".repeat(64),configuration_reference:"active-config:"+"a".repeat(64),persistence_status:"COMMITTED",candle_close_timestamp:start}]}});
    return route.fulfill({json:{rows:[]}});
  });
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await page.getByLabel("Shadow registration").selectOption("r");
  await page.getByLabel("Active decision source").selectOption("pa-lab:s");
  await page.getByLabel("Comparison start (UTC)").fill(start);
  await page.getByLabel("Comparison end (UTC)").fill(end);
  await expect(page.getByText("Active evidence status: VERIFIED",{exact:true})).toBeVisible();
  await expect(page.getByText("Configuration completeness: 100.0%",{exact:true})).toBeVisible();
  await expect(page.getByRole("table",{name:"Active evidence field verification"}).getByText("UNKNOWN",{exact:true})).toBeVisible();
  await page.getByRole("button",{name:"View active decision evidence"}).click();
  await page.getByText(start+" · NO_SIGNAL",{exact:true}).click();
  await expect(page.getByText("active-config:"+"a".repeat(64),{exact:true})).toBeVisible();
  await page.screenshot({path:"/tmp/sprint85-active-health.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("blocked active evidence shows precise missing fields and unauthorized errors",async({page})=>{
  await mockApi(page);
  await page.route("**/api/v2/strategy-intelligence/**",async route=>{
    const url=new URL(route.request().url()),path=url.pathname;
    if(path.endsWith("/shadow/registrations"))return route.fulfill({json:{rows:[{registration_id:"r",status:"BLOCKED"}]}});
    if(path.endsWith("/shadow/active-sources"))return route.fulfill({json:{rows:[{source_id:"instance:i:s",coverage_class:"SIGNALS_ONLY"}]}});
    if(path.endsWith("/health"))return route.fulfill({json:{source:{source_id:"instance:i:s"},start:url.searchParams.get("start"),end:url.searchParams.get("end"),status:"BLOCKED",comparison_readiness:"INCOMPLETE_ACTIVE_EVIDENCE",blocking_reasons:{ACTIVE_CONFIGURATION_UNVERIFIED:2,NO_DECISION_RECORDED:1},field_matrix:{config_fingerprint:"MISSING"}}});
    if(path.includes("/active-sources/")&&path.endsWith("/decisions"))return route.fulfill({status:401,json:{detail:"Authentication required"}});
    return route.fulfill({json:{rows:[]}});
  });
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await page.getByLabel("Shadow registration").selectOption("r");
  await page.getByLabel("Active decision source").selectOption("instance:i:s");
  await expect(page.getByText("Active evidence status: BLOCKED",{exact:true})).toBeVisible();
  await expect(page.getByText("ACTIVE_CONFIGURATION_UNVERIFIED: 2",{exact:true})).toBeVisible();
  await expect(page.getByText("Configuration completeness: 100.0%",{exact:true})).toHaveCount(0);
  await page.getByRole("button",{name:"View active decision evidence"}).click();
  await expect(page.getByText(/Authentication required/)).toBeVisible();
});
