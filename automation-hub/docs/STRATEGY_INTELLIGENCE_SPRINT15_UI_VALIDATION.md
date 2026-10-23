# Sprint 1.5 UI failure reconciliation

All five Sprint 1 failures were **test-contract mismatches**. With landing and dashboard bundles present, the intentional public front door is `/`, the authenticated React dashboard is `/app`, and its assets are `/app/assets`. Without landing assets the dashboard remains at `/`. A React document is a boot shell: it does not contain the legacy renderer's server-generated KPI, navigation and inline SSE markup.

The unchanged source with its existing assets reproduced **5 failed, 47 passed** across `test_auth_accounts.py`, `test_auth_endpoints.py` and `test_hub_app.py`. The failures were the three tests requesting the public root as if it were an authenticated dashboard, and two Phase 1 renderer tests requesting the public/React document as if it were legacy HTML. Rebuilding did not require any runtime, authentication, frontend, risk or deployment configuration change.

The correction keeps every original Phase 1 KPI/navigation/SSE assertion. An explicit legacy-renderer fixture binds those two historical renderer tests to their stated subject. Authentication and pending-2FA-cookie assertions now request the actual protected dashboard path; the pending cookie must also receive 401 from `/settings`. New bundle-contract tests prove the public-root/authenticated-dashboard distinction, reject a control header as a substitute for a UI session, check dashboard subroutes, test the retained standalone React root, check private response caching/robots policy, prohibit control/session secrets in rendered documents and fetch the actual compiled script/style assets from the correct mounted base.

## Clean-build validation

Both builds used the committed lockfiles with `npm ci`. Dashboard validation ran `DASHBOARD_BASE=/app/ npm run build:check` (TypeScript plus Vite). Landing validation ran `VITE_APP_URL=/app npm run build` (TypeScript, browser build, SSR and prerendering of 20 public routes). Builds succeeded. Existing nonfatal chunk-size and mixed dynamic/static import warnings remain; no package manifest or lockfile changed. Generated bundles were copied into the ignored local `automation-hub/webui` and `automation-hub/landing` directories; prior generated assets were preserved in `/tmp/nexus-sprint15-ui-bundle-backup`.

Against these clean bundles, the affected auth/Hub tests plus the new bundle tests, landing route/SEO tests and Phase 0 security tests passed: **67 passed, 0 failed, 0 skipped**. Log: `/tmp/nexus-sprint15-ui-clean-build-tests.log`.

Chrome DevTools MCP was unavailable in this environment. The existing Playwright Chromium test exercised the compiled dashboard DOM and checked console/page errors instead: **1 passed**, with no application console/page errors (`/tmp/nexus-sprint15-ui-browser.log`). A second real-Chromium smoke used an isolated localhost backend, temporary data directory and autonomous engine disabled. It verified landing 200, anonymous `/app` 303 to `/login`, authenticated `/app` 200, visible dashboard/sidebar and zero page exceptions. Screenshots: `/tmp/nexus-sprint15-landing-browser.png` and `/tmp/nexus-sprint15-dashboard-browser.png`. External browser requests were blocked in this isolated smoke; it was not a validation of external providers or live trading.

## Files changed

- `tests/test_auth_accounts.py`
- `tests/test_auth_endpoints.py`
- `tests/test_hub_app.py`
- `tests/test_ui_bundle_contract.py` (new)
- This validation report (new)

No meaningful test was deleted or skipped. The five failures are resolved against the clean build and do not remain deployment blockers. Full-suite integration results belong in the overall Sprint 1.5 report.
