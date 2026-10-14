import {expect,test} from '@playwright/test';
import {mockApi} from './mock';

const base='/api/v2/strategy-intelligence';
// Backend contract fixtures; never represented as live financial evidence.
test('native evidence lineage preserves unknown funding and no execution controls',async({page})=>{
  await mockApi(page);
  await page.route('**'+base+'/paper-reconciliation/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    const body=path.endsWith('/sources')?{rows:[{source_id:'fixture-owned-account'}]}:
      path.endsWith('/availability')?{native_capture:{original_fill_count:3,captured_fill_count:3,transaction_capture:'COMPLETE'},
        fields:{configuration_fingerprint:{status:'VERIFIED',reason:'Original frozen decision'},funding:{status:'UNKNOWN',reason:'Missing original schedule'},financial_journal:{status:'COMPLETE',reason:'Original cash receipts'},equity_series:{status:'PARTIAL',reason:'Incomplete valuation history'}}}:
      path.endsWith('/lineage')?{schema_version:'paper-lineage.v1',total_episodes:1,episodes:[{episode_id:'original-episode',funding_coverage:'UNKNOWN'}],cash_journal_complete:true,equity_status:'PARTIAL',execution_authorization:false}:{rows:[]};
    await route.fulfill({json:body});
  });
  await page.goto('/#/strategy-intelligence?tab=paper-reconciliation');
  await page.getByLabel('Integrity evidence source').selectOption('fixture-owned-account');
  await page.getByRole('button',{name:'Inspect paper evidence availability'}).click();
  await expect(page.getByText('Original / captured fills: 3 / 3')).toBeVisible();
  await expect(page.getByRole('table',{name:'Paper source evidence availability'})).toContainText('Missing original schedule');
  await page.getByRole('button',{name:'Inspect native configuration and episode lineage'}).click();
  await expect(page.getByText(/"episode_id": "original-episode"/)).toBeVisible();
  await expect(page.getByRole('button',{name:/activate|place order|enable live/i})).toHaveCount(0);
});

test('operational resilience displays spool backlog and blocked retention honestly',async({page})=>{
  await mockApi(page);
  await page.route('**'+base+'/operational-resilience/health',route=>route.fulfill({json:{spool:{status:'RECOVERING',pending:3,corrupt:0,conflicting:0,overflow_count:1},raw_retention:{owner_payload_bytes:1024,owner_byte_limit:4096,raw_reclamation:'BLOCKED_UNTIL_REVIEWED_REPORT_PIN_POLICY'},feed_soak:{status:'BLOCKED',reason:'No real soak evidence'}}}));
  await page.goto('/#/strategy-intelligence?tab=operations');
  await expect(page.getByText('Durable spool: RECOVERING · pending: 3')).toBeVisible();
  await expect(page.getByText('Live-feed soak: BLOCKED — No real soak evidence')).toBeVisible();
  await expect(page.getByText(/Durable buffered observations cannot prove uptime/)).toBeVisible();
});

test('local readiness opens immutable evidence without production certification',async({page})=>{
  await mockApi(page);
  const assessment={assessment_id:'fixture-local-one',environment_id:'local:fixture-rehearsal',status:'PARTIAL',worker_backend:'postgresql',software_revision:'fixture',created_at:'2026-10-12T08:00:00Z',production_certification:false,gates:{postgresql:{status:'LOCAL_VERIFIED',evidence_reference:'fixture-test-output'},feed_soak:{status:'BLOCKED',reason:'No sustained provider test'}}};
  await page.route(url=>url.pathname.startsWith(base+'/local-readiness'),route=>route.fulfill({json:new URL(route.request().url()).pathname.endsWith('/local-readiness')?{rows:[assessment]}:assessment}));
  await page.goto('/#/strategy-intelligence?tab=local-readiness');
  await page.getByRole('button',{name:'local:fixture-rehearsal'}).click();
  await expect(page.getByRole('table',{name:'Local readiness gates'})).toContainText('No sustained provider test');
  await expect(page.getByText(/"production_certification": false/)).toBeVisible();
});

test('human review records a request for evidence without paper activation',async({page})=>{
  await mockApi(page);
  const report={report_id:'fixture-immutable-paper-report',status:'PARTIAL',commitment_status:'COMMITTED',performance_eligible:false,
    performance_type:'UNVERIFIED_PAPER_PERFORMANCE',metrics:{},cost_coverage:{funding:'UNKNOWN'},checks:[{name:'funding',status:'UNKNOWN',reason:'Original schedule missing'}],warning_codes:[]};
  await page.route('**'+base+'/paper-reconciliation/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/review')){
      const body=route.request().postDataJSON();expect(body.reason).toBe('Obtain complete original funding evidence');expect(body.activate).toBeUndefined();
      await route.fulfill({json:{review_id:'fixture-review',decision:'REQUEST_MORE_EVIDENCE',execution_authorization:false}});return;
    }
    await route.fulfill({json:path.endsWith('/sources')?{rows:[{source_id:'fixture-owned'}]}:route.request().method()==='POST'?report:{rows:[]}});
  });
  await page.goto('/#/strategy-intelligence?tab=paper-reconciliation');
  await page.getByLabel('Integrity evidence source').selectOption('fixture-owned');
  await page.getByRole('button',{name:'Create immutable reconciliation report'}).click();
  await page.getByLabel('Paper evidence review reason').fill('Obtain complete original funding evidence');
  await page.getByRole('button',{name:'Record request for more evidence'}).click();
  await expect(page.getByText('Human review: REQUEST_MORE_EVIDENCE · fixture-review · No execution authorization')).toBeVisible();
});
