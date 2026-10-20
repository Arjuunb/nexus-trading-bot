# Task 6 current acceptance report — approved replacement 4eceb97

**INCOMPLETE: no approved-image VPS deployment or acceptance has occurred.**

The user explicitly approved the prepared replacement source after reviewing the failed 0ded release and successful 4ece release validation. This replaces the source pin only. It does not alter the existing staging target, customer workspace, credentials, financial records, tenant isolation, API memory requirement or execution boundaries.

## Exact approved source and preparation

- Repository: `Arjuunb/nexus-trading-bot`
- Commit: `4eceb973e98e1350dd625153753637629453c401`
- Tree: `1af45a4993d83919c5007373ba8b9a27e9de5a71`
- Frozen source SHA-256: `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8`
- Release ref: `release/strategy-intelligence-task6-4eceb97`, independently verified at that exact commit.
- Clean detached source and all **1468** retained files reverified, **zero** hash mismatches.
- Local request-guard check passed with explicit simulated workflow metadata; this is not an image build or remote run.
- Exact-source [release validation 39173887337](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39173887337): **4553 passed, 76 skipped** on each Python version; disposable PostgreSQL **31 passed, 2 skipped**; original reviewed browser suite **165 passed, zero failures/skips**.

No trading, strategy/financial service or migration source differs from Task 5. The replacement repairs release/browser tooling and dashboard layout and preserves the original reviewed harness assertions. The earlier failed 0ded run and original VPS baseline remain in `../task6-approved-0ded/`; their approval state is historical, superseded by this explicit replacement approval.

## Actual required deployment results

| Required item | Actual result |
| --- | --- |
| Deployed approved source and image digests | **Not deployed.** No fresh built/scanned/published 4ece API or worker digest is available |
| Existing API source/image config ID | Operator baseline: `f8778ed42039d415f3829e08a05225976fe0d0a5`, `sha256:f2343e76a896ec3718328f3318adc3c2a40c93774194602858de4bdd4dce5397` |
| Existing worker source/image config ID | Operator baseline: `28acdbccc4f9b737c683d94e15bb16a09e50aa85`, `sha256:25daa1ca6ea70c493053ab60635844d82ea5b147daf8ae64da7b58a508277fb1` |
| Deployment success/failure | **Blocked before update**; no VPS mutation performed |
| VPS container/application health | Existing API, ingress, PostgreSQL and worker operator-reported healthy on 2026-10-19, zero restarts/OOM; no fresh approved-release acceptance |
| Authenticated dashboard/API | **Not run**; no staging session/credential accessed |
| Research worker | Existing old worker healthy in baseline; approved-release heartbeat, job, recovery and research-only behavior unverified |
| Database migrations, persistence and recovery | Prior initializers exited 0. Approved-release PostgreSQL/migration, persistent-data and backup/restore acceptance **not run** |
| Full PRD: EdgeScore, Champion/Challenger, regime selection, decay, weekly research | **Not run on VPS**; passing CI does not replace actual authenticated acceptance |
| Insufficient real financial evidence | Staging NOT_QUALIFIED/UNSCORED/abstention behavior **not yet verified**; no evidence or profit was fabricated |
| Restart, resources and recovery | Existing API memory and memory-plus-swap both 1610612736 bytes; 6633 MiB RAM and 32G disk available in baseline. New-release restart/load/recovery checks **not run** |
| Production isolation | No production mutation performed. Baseline production app healthy at `sha256:d4c51634a70e88c026f4b4d6e84c1e4bf6604603bb1307217ec2f88938bbe523`; full before/after container/start/mount comparison pending |

## Precise current blockers

1. A **fresh manual** `staging-release.yml` run is required. Connected GitHub tools support retries but expose no workflow dispatch. Source guards explicitly require attempt 1 and a `release/` ref, so the prior cancelled run 39331267323 cannot be reused. That run has no image packet. `next-commands.md` contains the ready-to-run inputs.
2. SSH still fails before a remote command runs: exit 255, `ssh: connect to host 57.129.155.230 port 22: Network is unreachable`. No configured SSH identity/agent is available here. Target operations must use the existing trusted operator session until access is available.
3. No fresh built/scanned/published approved-source API/worker packet exists. No local Docker engine or scanner is present. The guarded GitHub builder must supply actual image proof first.
4. Effective live staging overlays, credential bindings, persistent mounts, migration state and restorable backups still need verification before a service update. Authenticated PRD, recovery and production-isolation checks remain pending.

The source approval is resolved; another generic deployment confirmation is unnecessary. The separate protected publication review must be completed by its configured distinct reviewer when actual scanned bytes are ready. No guard was weakened or bypassed.

## Binding target and remaining execution

Deploy only `nexus-remote-stage-rc3-28acdbc` under `/home/ubuntu/nexus-rc3-staging-28acdbc/release`. Preserve the existing customer workspace/authentication, credentials, TLS, overlays, named volumes, mounts and financial data. Include `intelligence-recovery-20261018/compose.memory.yaml` last; verify memory and memory-plus-swap each **1536 MiB / 1610612736 bytes** after rendering and recreation. Retain existing image/configuration identities and recovery backups for rollback.

The candidate's historical runbook proposed a separate namespace. The current explicit user target supersedes it: do not create a project or replace production. Do not pull main, discard volumes, downgrade/reset financial databases, enable feeds, start real-money/new-paper execution or automatically promote a strategy. Research-worker acceptance must stay research-only. Use existing real evidence and require abstention for incomplete financial evidence.

After actual source/image/scan and preservation checks, update only authorized staging services, complete the full authenticated workflow and environment-specific checks, and compare production identities/mounts before and after. Report actual results. Task 6 is not complete until those VPS deployment and acceptance gates pass; no feature sprint is authorized.
