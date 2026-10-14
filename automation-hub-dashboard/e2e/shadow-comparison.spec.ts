import { expect,test } from "@playwright/test";
import { mockApi } from "./mock";

// Synthetic API fixtures are UI contracts, not market observations or profits.
test("matched shadow comparison exposes exact denominators, exclusions and evidence",async({page})=>{
  await mockApi(page);
  const errors:string[]=[];
  page.on("pageerror",e=>errors.push(e.message));
  page.on("console",m=>{if(m.type()==="error"||m.type()==="warning")errors.push(m.text());});
  const artifact={comparison_id:"fixture-comparison",registration_id:"fixture-reg",active_source_id:"fixture-active",eligibility:"PARTIALLY_COMPARABLE",evidence_completeness:"PARTIAL",calculation_version:"shadow_comparison.v2",start:"2026-10-11T12:00:00Z",end:"2026-10-11T12:30:00Z",metrics:{matched_opportunities:5,all_eligible_agreement:.8,all_eligible_denominator:5,signal_relevant_agreement:.5,signal_relevant_denominator:2,both_no_signal:3,active_only_signals:0,shadow_only_signals:0,opposite_signals:1,excluded_opportunities:1,missing_decision_rate:1/6},coverage:{expected_scheduled_opportunities:6,market_data_coverage:1,shadow_evaluation_coverage:1,active_decision_coverage:5/6,matched_comparison_coverage:5/6},timeline:[{candle_close_timestamp:"2026-10-11T12:25:00Z",active_status:"NO_DECISION_RECORDED",shadow_status:"SIGNAL_LONG",eligible:false,exclusion_reasons:["NO_DECISION_RECORDED"]}],breakdowns:{session:{LONDON:{matched_opportunities:5,signal_relevant_agreement:.5}}},reliability:{gap_events:1,duplicate_events:0,continuous_feed_uptime:null},warnings:["Missing active evidence is not NO_SIGNAL."],input_watermark:"b".repeat(64),inputs:{active:[],shadow:[]}};
  await page.route("**/api/v2/strategy-intelligence/**",async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith("/shadow/registrations")) return route.fulfill({json:{rows:[{registration_id:"fixture-reg",strategy_id:"fixture-shadow",status:"OBSERVING",config_fingerprint:"a".repeat(64)}]}});
    if(path.endsWith("/shadow/active-sources"))return route.fulfill({json:{rows:[{source_id:"fixture-active",coverage_class:"PARTIAL_DECISION_EVIDENCE",limitations:["Effective configuration fingerprint not captured"]}]}});
    if(path.endsWith("/shadow/comparisons")&&route.request().method()==="POST"){
      const body=route.request().postDataJSON();expect(body.registration_id).toBe("fixture-reg");expect(body.active_source_id).toBe("fixture-active");expect(body).not.toHaveProperty("owner_id");
      return route.fulfill({status:201,json:artifact});
    }
    if(path.endsWith("/shadow/comparisons"))return route.fulfill({json:{rows:[artifact]}});
    if(path.endsWith("/fixture-comparison"))return route.fulfill({json:artifact});
    if(path.endsWith("/fixture-reg"))return route.fulfill({json:{registration_id:"fixture-reg",status:"OBSERVING"}});
    return route.fulfill({json:{rows:[]}});
  });
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await page.getByLabel("Shadow registration").selectOption("fixture-reg");
  await page.getByLabel("Active decision source").selectOption("fixture-active");
  await expect(page.getByText("Effective configuration fingerprint not captured",{exact:true})).toBeVisible();
  await page.getByLabel("Comparison start (UTC)").fill(artifact.start);
  await page.getByLabel("Comparison end (UTC)").fill(artifact.end);
  await page.getByRole("button",{name:"Calculate matched comparison"}).click();
  await expect(page.getByText("Comparison eligibility: PARTIALLY_COMPARABLE",{exact:true})).toBeVisible();
  await expect(page.getByText("All eligible agreement: 80.0% · denominator 5",{exact:true})).toBeVisible();
  await expect(page.getByText("Signal-relevant agreement: 50.0% · denominator 2",{exact:true})).toBeVisible();
  await expect(page.getByRole("table",{name:"Matched decision timeline"}).getByText("NO_DECISION_RECORDED",{exact:true}).first()).toBeVisible();
  await expect(page.getByText("LONDON",{exact:true})).toBeVisible();
  await page.getByText("Comparison evidence details",{exact:true}).click();
  await expect(page.getByText("Input watermark: "+"b".repeat(64),{exact:true})).toBeVisible();
  await page.screenshot({path:"/tmp/sprint8-shadow-comparison.png",fullPage:true});
  await page.setViewportSize({width:390,height:844});
  // Resize triggers the existing off-canvas transition. Verify its settled
  // state rather than capturing a sidebar halfway across the content.
  await expect.poll(()=>page.locator("aside.sidebar").evaluate(e=>e.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await page.getByLabel("Active decision source").scrollIntoViewIfNeeded();
  await expect(page.getByLabel("Active decision source")).toBeVisible();
  await page.screenshot({path:"/tmp/sprint8-shadow-mobile.png",fullPage:true});
  expect(errors).toEqual([]);
});

test("unavailable active source explains blockers without invented agreement",async({page})=>{
  await mockApi(page);
  await page.route("**/api/v2/strategy-intelligence/**",async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith("/shadow/active-sources"))return route.fulfill({json:{rows:[{source_id:"smc-lab",coverage_class:"UNAVAILABLE",limitations:["Persisted decision database unavailable or incompatible"]}]}});
    if(path.endsWith("/shadow/registrations"))return route.fulfill({json:{rows:[{registration_id:"r",status:"BLOCKED"}]}});
    return route.fulfill({json:{rows:[]}});
  });
  await page.goto("/#/strategy-intelligence?tab=shadow-lab");
  await page.getByLabel("Shadow registration").selectOption("r");
  await page.getByLabel("Active decision source").selectOption("smc-lab");
  await expect(page.getByText("Persisted decision database unavailable or incompatible",{exact:true})).toBeVisible();
  await expect(page.getByText("No committed matched comparison selected.",{exact:true})).toBeVisible();
  await expect(page.getByText("All eligible agreement: 100.0%",{exact:false})).toHaveCount(0);
});
