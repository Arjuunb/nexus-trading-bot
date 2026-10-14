import {expect,test} from '@playwright/test';
import {mockApi} from './mock';

// UI contract fixtures, not actual strategy performance or operational proof.
const base='/api/v2/strategy-intelligence/forward-qualification';
const report={report_id:'forward-report:fixture',window_id:'forward-window:fixture',source_id:'shadow:r',
  source_kind:'FORWARD_SHADOW_DECISION',start:'2026-10-01T00:00:00Z',end:'2026-10-08T00:00:00Z',
  identity:{strategy_id:'adaptive_trend_pullback',strategy_version:'1.0.0',config_fingerprint:'a'.repeat(64)},
  policy:{policy_version:'forward-qualification.v1'},outcome:'BEHAVIOUR_ONLY',profitability:'NOT_ASSESSED',
  coverage:{expected_opportunities:2016,eligible_decisions:2016,decision_evaluation_coverage:1,missing_decisions:0},
  reliability:{evaluation_error_rate:0,missing_data_rate:0,worker_availability:null},
  behaviour:{signal_count:35,signal_frequency:35/2016,long_count:20,short_count:15},
  checks:[{name:'observation_coverage',status:'PASS',purpose:'BEHAVIOUR',reason:'2016/2016'},
    {name:'outcome_cost_coverage',status:'UNKNOWN',purpose:'OUTCOMES',reason:'Funding is unknown'}],
  warning_codes:['SIGNAL_BEHAVIOUR_IS_NOT_PROFITABILITY','HOLDOUT_PROVENANCE_UNKNOWN'],
  blocking_reasons:['Funding is unknown'],holdout_provenance:{status:'UNKNOWN'},
  paper_evidence:{cost_coverage:{fees:'UNKNOWN',funding:'UNKNOWN'},evidence_references:[]},
  matched_comparison:null,created_at:'2026-10-08T01:00:00Z',workflow_state:'COMPLETED',commitment_status:'COMMITTED'};

test('forward qualification previews committed evidence, generates a report and records human review',async({page})=>{
  await mockApi(page);
  const errors:string[]=[];page.on('pageerror',e=>{errors.push(e.message);console.error('forward-ui-runtime-error',e.message);});
  page.on('console',message=>{if(message.type()==='error')console.error('forward-browser-console',message.text());});
  let completed=false,reviewed=false;
  await page.route('**/api/v2/strategy-intelligence/forward-qualification/**',async route=>{
    const url=new URL(route.request().url()),path=decodeURIComponent(url.pathname);
    if(path===base+'/sources')return route.fulfill({json:{rows:[{source_id:'shadow:r',source_kind:'FORWARD_SHADOW_DECISION',strategy_id:'adaptive_trend_pullback',strategy_version:'1.0.0',config_fingerprint:'a'.repeat(64)}]}});
    if(path===base+'/policies')return route.fulfill({json:{rows:[{policy_version:'forward-qualification.v1',minimum_duration_seconds:604800,minimum_opportunities:1000,minimum_observation_coverage:.95}]}});
    if(path===base+'/operations')return route.fulfill({json:{production_ready:false,gates:{postgresql_worker_concurrency:{status:'BLOCKED',reason:'No PostgreSQL worker backend'},worker_supervision:{status:'PARTIAL',reason:'Local service only'}}}});
    if(path===base+'/eligibility'){
      const body=route.request().postDataJSON();
      return route.fulfill({json:{...report,start:body.start,end:body.end,workflow_state:'DRAFT',commitment_status:'NOT_COMMITTED'}});
    }
    if(path===base+'/reports'&&route.request().method()==='POST'){
      const body=route.request().postDataJSON();
      expect(body).not.toHaveProperty('enable_paper');completed=true;
      return route.fulfill({json:{...report,start:body.start,end:body.end}});
    }
    if(path===base+'/reports')return route.fulfill({json:{rows:completed?[report]:[],independent_samples_claimed:false}});
    if(path.endsWith('/review')){
      expect(route.request().postDataJSON().decision).toBe('ACKNOWLEDGED');reviewed=true;
      return route.fulfill({json:{reviewer_id:'fixture-owner',execution_authorization:false}});
    }
    if(path===base+'/reports/'+report.report_id)return route.fulfill({json:{report,reviews:reviewed?[{decision:'ACKNOWLEDGED',reviewer_id:'fixture-owner',reason:'Need verified cost evidence'}]:[],workflow_state:reviewed?'REVIEWED':'COMPLETED'}});
    if(path.includes('/windows/'))return route.fulfill({json:{window_id:report.window_id,evidence_hash:'b'.repeat(64),inputs:{decisions:[{decision_id:'shadow:fixture',status:'NO_SIGNAL',evidence_kind:'FORWARD_SHADOW_DECISION'}],active:[]}}});
    return route.fulfill({json:{rows:[]}});
  });
  await page.goto('/#/strategy-intelligence?tab=forward-qualification');
  await expect(page.getByRole('heading',{name:'Forward Qualification',exact:true})).toBeVisible().catch(async reason=>{
    await page.screenshot({path:'/tmp/sprint9-ui-diagnostic.png',fullPage:true});throw reason;
  });
  await page.getByLabel('Forward evidence source').selectOption('shadow:r');
  await page.getByLabel('Observation start (UTC)').fill(report.start);
  await page.getByLabel('Observation end (UTC)').fill(report.end);
  await page.getByRole('button',{name:'Check evidence eligibility'}).click();
  await expect(page.getByText('Eligibility preview: BEHAVIOUR_ONLY',{exact:true})).toBeVisible();
  await expect(page.getByText('NOT_COMMITTED',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Generate immutable report'}).click();
  await expect(page.getByText('Qualification outcome: BEHAVIOUR_ONLY',{exact:true})).toBeVisible();
  await expect(page.getByText('Profitability: NOT_ASSESSED',{exact:true})).toBeVisible();
  await expect(page.getByRole('table',{name:'Forward qualification checks'}).getByText('Funding is unknown')).toBeVisible();
  await page.getByLabel('Qualification review reason').fill('Need verified cost evidence');
  await page.getByRole('button',{name:'Record human review'}).click();
  await expect(page.getByText('Human review state: REVIEWED',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Inspect frozen decision evidence'}).click();
  await expect(page.getByText('shadow:fixture',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:/activate|enable.*trading|promote/i})).toHaveCount(0);
  await page.screenshot({path:'/tmp/sprint9-forward-qualification.png',fullPage:true});
  expect(errors).toEqual([]);
});

test('forward qualification shows unavailable evidence and blocks unauthorized report creation',async({page})=>{
  await mockApi(page);
  await page.route('**/api/v2/strategy-intelligence/forward-qualification/**',route=>{
    const path=decodeURIComponent(new URL(route.request().url()).pathname);
    if(path.endsWith('/sources'))return route.fulfill({status:401,json:{detail:'Sign in required'}});
    return route.fulfill({json:{rows:[],gates:{},production_ready:false}});
  });
  await page.goto('/#/strategy-intelligence?tab=forward-qualification');
  await expect(page.getByText(/Sign in required/)).toBeVisible();
  await expect(page.getByRole('button',{name:'Generate immutable report'})).toBeDisabled();
  await expect(page.getByText('Qualification outcome: FORWARD_EVIDENCE_SUFFICIENT',{exact:true})).toHaveCount(0);
});

test('edited observation window cannot display stale eligibility as current',async({page})=>{
  await mockApi(page);
  await page.route('**/api/v2/strategy-intelligence/forward-qualification/**',async route=>{
    const path=decodeURIComponent(new URL(route.request().url()).pathname);
    if(path.endsWith('/sources'))return route.fulfill({json:{rows:[{source_id:'shadow:r',source_kind:'FORWARD_SHADOW_DECISION'}]}});
    if(path.endsWith('/policies'))return route.fulfill({json:{rows:[{policy_version:'forward-qualification.v1'}]}});
    if(path.endsWith('/eligibility')){
      const body=route.request().postDataJSON();
      return route.fulfill({json:{...report,start:body.start,end:body.end,commitment_status:'NOT_COMMITTED'}});
    }
    return route.fulfill({json:{rows:[],gates:{},production_ready:false}});
  });
  await page.goto('/#/strategy-intelligence?tab=forward-qualification');
  await page.getByLabel('Forward evidence source').selectOption('shadow:r');
  await page.getByRole('button',{name:'Check evidence eligibility'}).click();
  await expect(page.getByText('Eligibility preview: BEHAVIOUR_ONLY',{exact:true})).toBeVisible();
  await page.getByLabel('Observation start (UTC)').fill('2026-09-30T00:00:00Z');
  await expect(page.getByText('Eligibility preview: BEHAVIOUR_ONLY',{exact:true})).toHaveCount(0);
});
