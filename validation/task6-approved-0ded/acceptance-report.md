# Task 6 — existing RC3 staging deployment and acceptance

**BLOCKED — exact-source release validation failed; deployment not attempted. Task 6 is NOT COMPLETE.**

Recorded: 2026-10-18T23:33:02.072994+00:00.

This report follows the current approval: exact Task 5 commit `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`, tree `9a8b1fcc22e8647452047971d950f6dda53f780a`, and the existing OVHcloud Compose project `nexus-remote-stage-rc3-28acdbc`. Previous preparation receipts for later commits or a new staging namespace are historical and are not deployment approval for this request. No GitHub main pull/merge or approved-source edit occurred in this attempt.

## Verified source preparation

A clean detached checkout of the approved commit was frozen with its own `ops/github-release.py`. All 1461 retained files were verified against their sizes and SHA-256 values. RC3 `28acdbccc4f9b737c683d94e15bb16a09e50aa85` and the customer-workspace base `f8778ed42039d415f3829e08a05225976fe0d0a5` are ancestors of the approved source.

- Approved commit: `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`
- Approved tree: `9a8b1fcc22e8647452047971d950f6dda53f780a`
- Frozen source SHA-256: `b58399c346abb3416579cade8dd2df159a7bb197cfb0e1c36ef1dad7d0045d9c`
- Clean checkout: `/workspace/scratch/734bba8326aa/nexus-task6-approved-0ded1cc`
- Retained manifest: `/workspace/scratch/734bba8326aa/task6-approved-source-0ded1cc/source-manifest.json`
- Task 5 exact-source CI: [39155905417](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39155905417), verified successful for this commit. Its artifacts endpoint returned zero artifacts; it supplies no fresh deployment-image packet.

This is a source freeze, not an image build, security scan or VPS acceptance result. Later-commit CI/browser results are not used as evidence for this approved deployment. No new paper fills were generated in this attempt.

## Required deployment and acceptance report

| Required item | Actual result |
| --- | --- |
| Exact deployed source | Operator source labels: API `f8778ed42039d415f3829e08a05225976fe0d0a5`, worker RC3 `28acdbccc4f9b737c683d94e15bb16a09e50aa85`. Approved `0ded1cc` is not running |
| Exact deployed API/worker image digests | Existing config IDs and locally reported RepoDigests recorded below. Fresh approved-source image digests and scans remain unavailable |
| Deployment success/failure | BLOCKED BEFORE UPDATE; no staging service was updated |
| VPS container and application health | Operator snapshot: existing API, ingress, PostgreSQL and research worker all running/healthy; no OOM or restarts reported. New-release application acceptance NOT RUN |
| Real authenticated dashboard/API results | NOT RUN; no live authenticated session or credential was accessed |
| Research-worker status | Existing worker operator-reported running/healthy; source identity, heartbeat, role and actual research-job acceptance pending. No worker started/replaced by this attempt |
| Database migrations, persistent data and recovery | Existing state-init and metadata-init exited 0. Approved-release migrations, persistent-data validation and backup/restore remain NOT RUN; no database/volume change occurred |
| EdgeScore, Champion/Challenger, regime selection, decay and weekly research | NOT RUN on the VPS; full PRD acceptance is unverified |
| Insufficient financial evidence abstention | NOT RUN on staging; Task 5 code tests do not replace real staging evidence |
| Environment-specific PostgreSQL, container, restart and resource tests | Existing Docker/resource baseline supplied by operator; PostgreSQL integration, restart, load and recovery tests NOT RUN |
| Production isolation | Existing app and nginx operator-reported running/healthy; app restarts 0, OOM false. No production mutation performed by this attempt; full ID/start/mount before/after comparison still unverified |

## Current precise blockers — 2026-10-20

1. The operator dispatched the correct exact-source release workflow. [Run 39499761284](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39499761284) failed in the browser launcher before browser tests began. Both backend regression jobs completed their Python suites, and disposable PostgreSQL tests passed. The image build and publication jobs were skipped; no fresh image digest or security scan exists.
2. The concrete fix is already present in `4eceb973e98e1350dd625153753637629453c401`, with successful release validation. This is a proposed replacement source, not permission to deploy it. The user's exact `0ded1cc` source approval remains binding until explicitly replaced. The original release ref is unchanged.
3. Read-only SSH remains blocked before any remote command runs: exit 255, `ssh: connect to host 57.129.155.230 port 22: Network is unreachable`. No existing SSH key or agent is configured here. No credential should be pasted into chat.
4. This workspace has no Docker engine/socket or image scanner. GitHub's existing guarded builder can build and scan once release validation passes for the approved source.
5. Actual staging configuration, credential bindings, mounts, migration/backup/restore state, authenticated PRD acceptance and the production before/after comparison remain pending.

The former missing-workflow and manual-dispatch steps are resolved. Repeating the unchanged failed source will not correct this launcher error. No release gate was waived, no image was substituted and no VPS service was changed.

## Existing staging state to preserve when access resumes

Use only project `nexus-remote-stage-rc3-28acdbc` and its existing release directory `/home/ubuntu/nexus-rc3-staging-28acdbc/release`. Inspect the actual running Compose project/service labels and complete configuration-file list before any recreation. Retain customer-workspace overlays, the existing credentials, authentication settings, TLS bindings, data mounts and named volumes.

The required API ceiling is **1536 MiB / 1,610,612,736 bytes**. The approved base Compose file still declares `768m`, so it must not be used alone. Preserve the existing `compose.memory.yaml` override last in the observed overlay order and verify the effective Docker memory/swap settings. The historical checkpoint records the memory recovery and rollback material under `release/intelligence-recovery-20261018/`; its current presence and contents have not been remotely verified here.

Capture production container IDs, image IDs, start/restart data and mounts before and after staging changes, without reading production financial databases. Capture existing staging image identities and retain them for rollback. Validate the approved source labels and actual API/worker image digests after fresh builds/scans, then verify migration state, recovery backups and persistent-data mounts before updating only the named staging services.

Do not create a replacement project, regenerate existing workspace credentials, discard volumes, downgrade financial databases, pull main over RC3, activate public feeds, start real-money/new-paper execution or promote a strategy automatically. Worker acceptance must exercise research-only behavior with execution and feed flags disabled. Use actual existing staging users and evidence; incomplete financial evidence must remain NOT_QUALIFIED, UNSCORED or abstention.

Rollback must preserve the existing data and workspace, use captured prior image/configuration identities and retain the 1536 MiB API override. No rollback or service replacement was attempted in this run.

## Resume condition

Continue from a workspace that can reach the OVHcloud SSH endpoint and already has the authorized SSH identity, plus an approved Docker/scanner-capable builder. The deployment authorization is already explicit; another generic deployment confirmation is not needed. Recheck the real target first and carry out the pending acceptance steps above. Task 6 remains incomplete until actual VPS deployment and acceptance evidence exist.

Machine-readable evidence is in `status.json`; the actual connectivity error is retained in `ssh-probe.txt` beside this report.

## Continuation check — 2026-10-19

Last rechecked: 2026-10-19T22:16:30.817400+00:00. The approved checkout still matches the exact commit/tree, and all 1461 frozen files were rehashed with zero mismatches. SSH again returned exit 255 (`Network is unreachable`); the SSH identity, engine and scanner are still unavailable. No remote command ran.

The release workflow is now available on GitHub. Created `release/strategy-intelligence-task6-0ded1cc` and independently read its ref back at the approved `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`. Main and the development worktree were not updated. This source-ref preparation does not build images or deploy services. The exact-source run query before ref creation returned the existing successful CI run and no release-image artifacts.

`next-commands.md` contains the exact pending workflow dispatch and a short read-only VPS inventory. `vps-readonly-preflight.sh` provides a fuller sanitized container/image/mount/resource baseline; local syntax validation is not a real VPS test. The existing project, customer workspace, volumes, execution boundaries and 1536 MiB memory requirement remain binding. Task 6 remains incomplete.

Creating the release ref triggered ordinary [CI run 39333631513](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39333631513) at the exact approved commit. It was in progress at the last check; no new passing result is claimed. This is not the manual image-build/publication workflow and does not deploy to the VPS.

## Operator-supplied VPS baseline — 2026-10-19

The user supplied real SSH terminal output for the existing projects. This establishes a point-in-time baseline of the existing release, not direct assistant SSH access or acceptance of the approved Task 5 release. The exact normalized output is retained in `vps-operator-snapshot-20261019.txt` and parsed fields in `status.json`.

- Staging API, ingress, PostgreSQL and research worker: running and healthy; all report zero restarts and no OOM.
- Existing API image **config ID**: `sha256:f2343e76a896ec3718328f3318adc3c2a40c93774194602858de4bdd4dce5397` (matches the historical pre-Task-5 staging API ID).
- Existing worker image **config ID**: `sha256:25daa1ca6ea70c493053ab60635844d82ea5b147daf8ae64da7b58a508277fb1`.
- API effective memory and memory-plus-swap ceilings: both 1610612736 bytes (1536 MiB); host swap total 0 MiB.
- Host available RAM 6633 MiB; root disk 32G available and 67% used. This is not a peak-load/resource stability test.
- Initializers are exited; their exit codes were not supplied, so successful migrations cannot be inferred.
- Production app and nginx are healthy, certbot is running, and the prior replay container is exited. The production app image baseline is `sha256:d4c51634a70e88c026f4b4d6e84c1e4bf6604603bb1307217ec2f88938bbe523`. Full container IDs/start times/mounts still need a before/after capture.

The staging Compose file list contains the Alpine base and customer-workspace overlay, **without** the memory overlay. The effective API limit is correct now, but recreation must explicitly preserve `intelligence-recovery-20261018/compose.memory.yaml` last. Its current contents and effective merged configuration remain to be verified. No recreation/update/restart/reboot command was issued.

Next collect image revision/source labels and registry digests, initializer exit codes, execution-disable flags, mounts/volume identities and rollback configuration without exposing credentials. Authenticated dashboard/API, real PRD workflow, PostgreSQL migrations/data/recovery and production isolation acceptance remain pending. Task 6 is not complete.

## Operator-supplied image identities and initializer exits — 2026-10-19

The next actual VPS command output confirms the API source label is `f8778ed42039d415f3829e08a05225976fe0d0a5` and its source hash is `940df9d11aee70f2c4a8b7fc3960f23376f1537adac3d1273da10007f5f5813e`. Its locally reported RepoDigest is `nexus-rc3-api@sha256:f2343e76a896ec3718328f3318adc3c2a40c93774194602858de4bdd4dce5397`; this is not a verified fresh GHCR image for Task 5.

The existing worker source label is RC3 `28acdbccc4f9b737c683d94e15bb16a09e50aa85`, source hash `3ba76e44d9181ee1a8910667be645bfa477a7b3e4055c30538a29030e83caa4d`, with reported RepoDigest `ghcr.io/arjuunb/nexus-trading-bot-worker@sha256:25daa1ca6ea70c493053ab60635844d82ea5b147daf8ae64da7b58a508277fb1`. These prior image identities must be retained for rollback. Neither image is the approved `0ded1cc` release.

Both existing initializers are exited with code **0**. This confirms their previous completion, not successful migrations for the still-undeployed approved source. The memory overlay was read on the VPS and contains exactly `mem_limit: 1536m` and `memswap_limit: 1536m` under the API service. Effective combined Compose configuration and persistence/credentials still need verification before updating services.

Receipt: `vps-operator-image-identity-20261019.txt`. The next affected step is fresh build/security scan/publication for the exact approved source using the prepared release ref and existing protected workflow. The connected tools cannot dispatch that workflow. No service update or reboot is requested while image proof and remaining preservation checks are pending.

## Actual manual release result — 2026-10-20

The user dispatched run **39499761284**, attempt 1, on `release/strategy-intelligence-task6-0ded1cc`, at exactly approved commit `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`. The run completed with **failure**.

| Actual gate | Result |
| --- | --- |
| Python 3.11 root regression | **4448 passed, 76 skipped**, 516.20 seconds |
| Python 3.12 root regression | **4448 passed, 76 skipped**, 498.81 seconds |
| Disposable PostgreSQL integration | **31 passed, 2 skipped**, 38.10 seconds; not VPS database acceptance |
| Frontend typechecks and builds | Passed |
| Reviewed browser gate | Launcher failed before tests with `TypeError: The "file" argument must be of type string. Received undefined` |
| Image build / scan / publish | Build and publication skipped; image scan not run |
| VPS deployment / acceptance | Not attempted |

The reviewed wrapper from immutable harness commit `24b67bf43e1712b5566fa33b457814ceac5ec30b` assumes `webServer` is one object. Approved Task 5 supplies an array. The wrapper spreads that array into an object without a `command`, so Playwright's process launcher receives undefined. The reviewed wrapper and clipboard assertions were not altered or bypassed.

### Concrete replacement proposed for source approval

- Commit: `4eceb973e98e1350dd625153753637629453c401`
- Tree: `1af45a4993d83919c5007373ba8b9a27e9de5a71`
- Frozen source SHA-256: `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8`
- **1468 files rehashed, zero mismatches**, clean detached checkout.
- Exact-source [release validation 39173887337](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39173887337): success. Python 3.11 and 3.12 each **4553 passed, 76 skipped**; PostgreSQL **31 passed, 2 skipped**; reviewed browser suite **165 passed, zero failures/skips**.

The replacement keeps a single dashboard server compatible with the immutable reviewed wrapper, manages the isolated research API fixture through the verified launcher, and includes responsive dashboard layout repairs, release-controller/test integration and documentation/evidence. The 0ded-to-4ece diff has no changes to trading, strategy/financial service or migration source. It changes 11 files; the complete comparison is reviewable on GitHub. Original harness file hashes and original browser assertions remain enforced.

This candidate's older runbook proposed a separate staging namespace. Those historical target instructions are superseded by the current explicit request: **only existing `nexus-remote-stage-rc3-28acdbc`, with its customer workspace and 1536 MiB memory override preserved**. Approval of a new source would not authorize a new project or loosen any execution/production boundary.

The passing replacement-source CI is not evidence that 0ded passed, that images were built/scanned or that the VPS was deployed. The replacement has not been authorized or deployed in this continuation. The development branch evidence update does not advance the frozen release ref.

Machine-readable failure details and filtered primary log excerpts: `release-run-39499761284.json`. Remaining required acceptance items in the report above are unchanged. Task 6 remains incomplete.
