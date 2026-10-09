# Guardian / deployed-runtime integration

## Pinned inputs and scope

This branch combines the reported VPS revision
`93f5f2dcca18db82ce564fd10e56c7a5212589dc` with the validated standalone
Guardian foundation `1b01d06a4cd4343803ead9ac1bfcf26190c1a2c7`. Their common
ancestor is `804a7c0d004b55e7a8c95e4c7caf0fc558e0f5e2`.

The newer remote journal commit `f0c64ae` is deliberately **not** included:
the approved integration target was the exact reported running revision.
If production advances again, recheck ancestry and review that delta before
deployment. Never bypass the running-revision ancestry guard.

No main-branch update, VPS deployment, restart, environment change, historical
reset, or live-routing activation is part of this local integration.

## Two distinct observers, not two execution authorities

| Component | Preserved behavior | Authority / evidence boundary |
| --- | --- | --- |
| Embedded `services.guardian` | Existing app startup/shutdown, dashboard, queued per-candle traces, journal/integrity readers, reports | Existing independent `guardian.db`; existing owner-scoped research/reasoning endpoints and opt-in recovery policy unchanged |
| Standalone `tradexa.guardian` | Optional isolated observer, immutable evidence, bounded imports and analysis | Separate `/var/lib/guardian/evidence.db`; optional `compose.guardian.yaml`, separate credentials, no trading volume or execution endpoint |
| Main app `/guardian/*` exports | Bounded GET projections of committed source evidence | `HUB_GUARDIAN_OBSERVER_KEY` cannot access embedded private routes, owner controls, or trading mutations; control key alone cannot read the exports |

The existing embedded Guardian's owner-authorized research/reasoning POST
routes are preserved. They are not granted to the standalone observer key.
No new recovery action is configured or enabled. This integration does not
replace the embedded dashboard with the standalone Command Center and does
not activate a second background collector by default.

Source-side provenance tables and fill columns are additive and transactional;
they preserve original decision/execution IDs. They do not rewrite historical
evidence, synthesize missing legacy provenance, or decide a trading action.
The full production coverage and soak gates in `GUARDIAN_REMAINING_WORK.md`
remain open for the standalone implementation. Existing embedded capabilities
are not, by themselves, certification of the standalone roadmap.

## Merge review and integration-only changes

Only `automation-hub/app.py` and `automation-hub/services/auto_engine.py` were
changed on both parents. Git merged them without textual conflicts:

- App: preserve embedded startup/shutdown while adding independently
  authenticated read exports.
- Engine: preserve native-feed checks, replay execution identities, pending
  fill/expiry tracing, frozen reports and blocker telemetry while recording
  bounded applied-settings provenance. No alpha or execution gate is changed
  by the integration resolution.

The combined collection initially rejected two duplicate pytest module names.
The standalone incident and research tests were renamed to
`test_guardian_standalone_incidents.py` and
`test_guardian_standalone_research.py`; their assertions are retained. The
standalone service test import and focused documentation command were updated.
No tests were skipped or assertions relaxed to resolve the collision.

New `automation-hub/tests/test_guardian_integration_contract.py` checks:

1. Distinct Guardian route operations, including existing API-prefix aliases,
   with only the existing owner research/reasoning mutation routes.
2. Observer/control credential isolation across both APIs and execution controls.
3. The real existing strategy/engine/pipeline/paper broker executes exactly one
   fill with embedded observation enabled or disabled; both evidence views use
   the original decision identity, and replaying the standalone import creates
   no extra event or position.

## Strategy protection

All four files in `automation-hub/data/smc_decision_path_freeze.json` are
byte-identical to both parents and match their recorded SHA-256 values:

- `services/native_smc.py`
- `services/smc_strategy_ladder.py`
- `services/smc_strategy_v1.py`
- `services/native_smc_live_visual.py`

The existing source freeze, behavior freeze, agent protection and crash-boundary
tests are rerun unchanged. The freeze baseline is not regenerated.

## Validation record

- Focused integration / SMC locks / agent crash boundaries: **38 passed**.
- Original Guardian parent GitHub CI run `37863493027`: all five jobs passed,
  including Python 3.10, 3.11, and 3.12. This is **parent CI**, not integrated CI.
- The archive-only preliminary run had **5 failures, 5,801 passes and 15 skips**:
  all five failures were unchanged `test_pa_status_script.py` tests requiring
  `git rev-parse` in a checkout, not a Git archive. No status-script assertion
  or implementation was changed. A temporary Git checkout of the same staged
  tree was prepared and all six status-script tests then passed.
- Focused checks in the temporary Git checkout, including status-script tests:
  **44 passed**.
- Complete clean-Git suite on Python **3.12.14**: **5,806 passed, 15 skipped,
  0 failed** in 474.55 seconds (95 dependency/deprecation warnings).
  No skip marker or production assertion was changed by the integration.
  JUnit evidence: `/private/tmp/guardian-vps-integration-git-full.xml`;
  focused evidence: `/private/tmp/guardian-vps-integration-git-focused.xml`.
  Reproduce from a Git checkout with dependencies installed:
  `PYTHONPATH="$PWD/automation-hub:$PWD:$PWD/sdks/python" python -m pytest -q`.
- Frontend source is byte-identical to the reported deployed parent. No new UI
  feature or dashboard build is introduced by this integration.

Final tests use a clean temporary Git checkout of index tree
`32f4ab36d5fae5214a43b6f6f2552d52551960aa`, not unrelated untracked duplicate
files that appeared in the working directory. Those files are preserved outside
the release and are not automatically deleted. Final documentation-only
validation results are recorded after the run; runtime/test source is unchanged.

Push, integrated CI, review and production deployment are separate gates. Before
any deployment: re-read the actual VPS version, require it to be an ancestor of
the approved integrated revision, retain the existing persistent volume, verify
live routing remains explicitly disabled, and preserve paper state throughout
the controlled restart/reconciliation check.
