// Trusted local operator smoke: real production bundles, no trading actions.
// Private fixture credentials arrive over stdin, never argv or logs.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(path.resolve(__dirname, '../automation-hub-dashboard/node_modules/playwright'));
(async () => {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.match(input.certificateSpki, /^[A-Za-z0-9+/=]+$/);
  assert.equal(new URL(input.url).hostname, '127.0.0.1');
  const browser = await chromium.launch({executablePath:'/usr/bin/chromium', headless:true,
    args:['--no-sandbox', '--ignore-certificate-errors-spki-list='+input.certificateSpki]});
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => new URL(route.request().url()).origin === new URL(input.url).origin
      ? route.continue() : route.abort());
    const page = await context.newPage();
    await page.goto(input.url+'/', {waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => document.querySelector('#root')?.children.length > 0);
    assert.ok(!(await page.content()).includes(input.password));
    assert.equal(await page.evaluate(async () => (await fetch('/api/v2/strategy-intelligence/deployment-readiness')).status),401);
    await page.goto(input.url+'/login');
    await page.locator('input[name="username"]').fill(input.username);
    await page.locator('input[name="password"]').fill(input.password);
    await Promise.all([page.waitForURL(url => url.pathname === '/'),page.locator('button[type="submit"]').click()]);
    await page.goto(input.url+'/app', {waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => document.querySelector('#root')?.children.length > 0);
    const readiness = await page.evaluate(async () => {
      const r=await fetch('/api/v2/strategy-intelligence/deployment-readiness');
      return {status:r.status,body:await r.json()};
    });
    assert.equal(readiness.status,200);
    assert.equal(readiness.body.checks.POSTGRES_READY.status,'READY');
    assert.equal(readiness.body.checks.FINANCIAL_EVIDENCE_READY.status,'UNKNOWN');
    assert.equal(readiness.body.execution_authorization,false);
    console.log(JSON.stringify({scope:'ACTUAL_PRODUCTION_BUNDLES_OVER_LOCAL_HTTPS',
      passed_scenarios:2,failed_scenarios:0,skipped_scenarios:0,
      scenarios:['anonymous_public_bundle_and_401','secure_login_dashboard_and_read_only_readiness'],
      certificate_trust:'GENERATED_FIXTURE_SPKI_ONLY; NO_GLOBAL_HTTPS_BYPASS',trading_controls_invoked:false}));
  } finally { await browser.close(); }
})().catch(error => { console.error(error.name+': '+error.message);process.exitCode=1; });
