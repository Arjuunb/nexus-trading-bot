import {expect,test} from '@playwright/test';
import {mockApi} from './mock';

// API contract fixtures only. Real database/financial proofs are backend tests.
const base='/api/v2/strategy-intelligence';

test('operational reliability preserves unavailable monitoring and exposes outage evidence',async({page})=>{
  await mockApi(page);const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/v2/strategy-intelligence/operational-reliability/**',async route=>{
    const path=decodeURIComponent(new URL(route.request().url()).pathname);
    const body=path.endsWith('/sources')?{rows:[{source_id:'shadow:owned',scope:{timeframe:'5m'}}]}:
      path.endsWith('/events')?{rows:[{event_type:'FEED_DISCONNECTED',occurred_at:'2026-10-12T08:00:00Z'}]}:
      {monitoring_status:'UNKNOWN',worker_availability:null,feed_availability:null,worker_denominator:60,feed_denominator:60,
        commit_success_rate:null,evidence_commitment_failures:1,feed_disconnects:1,warning_codes:['UNMONITORED_PERIOD'],
        availability_basis:'OBSERVED_PROBE_OPPORTUNITIES; NOT CONTINUOUS CERTIFICATION'};
    await route.fulfill({json:body});
  });
  await page.goto('/#/strategy-intelligence?tab=operations');
  await page.getByLabel('Integrity evidence source').selectOption('shadow:owned');
  await page.getByRole('button',{name:'Inspect reliability',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('Monitoring: UNKNOWN');
  await expect(page.getByText('UNMONITORED_PERIOD',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Inspect operational evidence'}).click();
  await expect(page.getByText(/"event_type": "FEED_DISCONNECTED"/)).toBeVisible();
  expect(errors).toEqual([]);
});

test('paper reconciliation preserves missing funding, commits reports and opens original evidence',async({page})=>{
  await mockApi(page);const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  const report={report_id:'paper-reconciliation:fixture',status:'PARTIAL',performance_eligible:false,
    performance_type:'PARTIALLY_RECONCILED_PAPER_PERFORMANCE',metrics:{net_pnl:null,average_realized_r:null},
    cost_coverage:{fees:'RECORDED_FEE',funding:'UNKNOWN',spread:'UNKNOWN',slippage:'UNKNOWN'},
    checks:[{name:'funding_coverage',status:'UNKNOWN',reason:'Original rate/exposure history is unavailable'}],
    warning_codes:['ORIGINAL_RISK_UNAVAILABLE'],scope:{account_id:'fixture-paper',strategy_id:'adaptive_trend_pullback',strategy_version:'1.0.0'}};
  await page.route('**/api/v2/strategy-intelligence/paper-reconciliation/**',async route=>{
    const path=decodeURIComponent(new URL(route.request().url()).pathname);const method=route.request().method();
    let body:any={rows:[]};
    if(path.endsWith('/sources'))body={rows:[{source_id:'paper:owned',kind:'pa-lab'}]};
    else if(path.endsWith('/availability'))body={fields:{funding:{status:'UNKNOWN',reason:'Missing original rate history'}}};
    else if(method==='POST')body={...report,commitment_status:path.endsWith('/preview')?'NOT_COMMITTED':'COMMITTED'};
    else if(path.includes('/reports/'))body={...report,commitment_status:'COMMITTED',original_snapshot:{fixture:true,financial_mutations:0}};
    await route.fulfill({status:method==='POST'&&path.endsWith('/reports')?201:200,json:body});
  });
  await page.goto('/#/strategy-intelligence?tab=paper-reconciliation');
  await page.getByLabel('Integrity evidence source').selectOption('paper:owned');
  await page.getByRole('button',{name:'Inspect paper evidence availability'}).click();
  await expect(page.getByRole('table',{name:'Paper source evidence availability'})).toContainText('UNKNOWN');
  await page.getByRole('button',{name:'Preview reconciliation',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('NOT_COMMITTED');
  await expect(page.getByText('Performance eligibility: BLOCKED',{exact:true})).toBeVisible();
  await expect(page.getByRole('table',{name:'Reconciled paper metrics'})).toHaveCount(0);
  await page.getByRole('button',{name:'Create immutable reconciliation report'}).click();
  await expect(page.getByRole('status')).toContainText('COMMITTED');
  await page.getByRole('button',{name:'Inspect original financial evidence'}).click();
  await expect(page.getByText(/"financial_mutations": 0/)).toBeVisible();
  await expect(page.getByRole('button',{name:/activate.*paper|enable.*live|place.*order/i})).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('unauthorized integrity source access blocks research controls',async({page})=>{
  await mockApi(page);
  await page.route('**/api/v2/strategy-intelligence/paper-reconciliation/**',route=>route.fulfill({status:401,json:{detail:'Authentication required'}}));
  await page.goto('/#/strategy-intelligence?tab=paper-reconciliation');
  await expect(page.getByRole('button',{name:'Create immutable reconciliation report'})).toBeDisabled();
  await expect(page.getByRole('alert').first()).toBeVisible();
});
