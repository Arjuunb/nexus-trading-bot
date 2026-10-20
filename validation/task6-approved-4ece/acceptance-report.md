# Task 6 current acceptance report — approved replacement 4eceb97

**INCOMPLETE: fresh build/tests/scans PASSED; publication awaits a distinct reviewer; no VPS deployment or acceptance has occurred.**

The user explicitly approved the prepared replacement source after reviewing the failed 0ded release and successful 4ece release validation. This replaces the source pin only. It does not alter the existing staging target, customer workspace, credentials, financial records, tenant isolation, API memory requirement or execution boundaries.

## Exact approved source and preparation

- Repository: `Arjuunb/nexus-trading-bot`
- Commit: `4eceb973e98e1350dd625153753637629453c401`
- Tree: `1af45a4993d83919c5007373ba8b9a27e9de5a71`
- Frozen source SHA-256: `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8`
- Release ref: `release/strategy-intelligence-task6-4eceb97`, independently verified at that exact commit.
- Clean detached source and all **1468** retained files reverified, **zero** hash mismatches.
- Local request-guard check passed with explicit simulated workflow metadata; this is not an image build or remote run.
- Fresh exact-source manual [release run 39503485769](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39503485769), attempt 1: Python 3.11 **4553 passed, 76 skipped**, 523.51 s; Python 3.12 **4553 passed, 76 skipped**, 382.50 s; disposable PostgreSQL **31 passed, 2 skipped**, 36.69 s; reviewed browser suite **165 passed, zero failures/skips**, 5.5 min.
- Actual unpublished API-image regression: **700 passed, 25 skipped**, 40.46 s, zero failures/errors. These fixtures did not start application services or access production.
- Image build, exact-source/label proof, HIGH/CRITICAL vulnerability scans and all-severity secret scans: **PASSED**. The protected publish job is **waiting**, not successful.

No trading, strategy/financial service or migration source differs from Task 5. The replacement repairs release/browser tooling and dashboard layout and preserves the original reviewed harness assertions. The earlier failed 0ded run and original VPS baseline remain in `../task6-approved-0ded/`; their approval state is historical, superseded by this explicit replacement approval.

## Actual required deployment results

| Required item | Actual result |
| --- | --- |
| Deployed approved source and image digests | **Not deployed.** Exact 4ece images are built/scanned and sealed below; registry publication/digests await protected review |
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

## Verified built images, security and retained packet

| Role | Actual built image config ID | Export SHA-256 |
| --- | --- | --- |
| API | `sha256:09b524395a9706115b53c8c19e8cc4239b873dcb5450bd3d43700d3bff5f0321` | `150450e714afbd9bdda913b038a519cd7f95474639b0136247ce181c4552fa66` |
| Worker | `sha256:b16074411ac65ed2cc8c0d6981b0608eb25bebdf8445abdad1a04aa8913cda41` | `b9d529cb248617ff402ec16edf1f9b4026774d211adca791aa3a37eed34e49de` |

Both actual image scan reports contain **zero HIGH/CRITICAL vulnerabilities**. Both all-severity image secret reports and the source secret report contain **zero secrets**. Their image IDs match the built config IDs. These results are scoped to the configured scanner/database and severities; they are not a guarantee of zero possible vulnerabilities. The API is Python 3.11.17, worker Python 3.12.15; both verify binary psycopg and disabled default execution flags. No generated journals are embedded; the worker boundary is code-only.

Artifact **12303500964**, `unpublished-staging-images-39503485769-1`, is **176365085 bytes**, expires **2026-10-27T22:07:45Z**, SHA-256 **`bcdcdcab3b3870cad1dbc6913e941979b2ec0b22e69e6726117d35e5bf0bb1da`**. Its downloaded bytes match GitHub's digest. The approved `verify-packet` command rehashed all **16 sealed files** successfully. Independent archive verification matched each config ID and revision/source label, each stored blob hash, and each decompressed diff hash for all **12** image layers.

An extra legacy `recover-staging-release.py` archive check initially reported `LAYER_HASH_MISMATCH`: it compares compressed Buildx blobs directly with uncompressed diff IDs. The format-aware check verifies both identities correctly and all match. The frozen source/helper was not modified and no gate was waived. This legacy helper must not be used as the verifier for these compressed exports. Official publication uses Docker load and actual-image/source verification; actual VPS recovery remains untested.

Machine-readable scan hashes, config/export identities and layer checks: `built-images-and-scans.json`. Fresh jobs, test artifacts, actual counts and review gate: `release-run-39503485769.json`. These are **built** identities; no published registry digest or deployed approved-image digest is claimed.

## Precise current blockers

1. **Publication reviewer conflict.** The actual pending environment API lists only **Arjuunb**, ID **190428336**, as required reviewer. The same ID is both actor and triggering actor of this run. The approved release guard requires actual approval by a configured **different** reviewer, `prevent_self_review` enabled and an enabled deployment branch policy. The eligible listed distinct-reviewer set is empty. Full environment settings cannot be read through the connected fetch endpoint. Keep this existing successfully built run; configure the required distinct reviewer and preserve/verify the protection and intended release-branch policy. Do not self-approve, weaken guards or restart the build as a workaround.
2. **VPS access.** SSH fails before a remote command runs: exit 255, `ssh: connect to host 57.129.155.230 port 22: Network is unreachable`. No configured existing SSH identity/agent is available here. Actual target operations require the trusted operator session until access is available.
3. **Live acceptance and preservation.** Existing overlays, credential bindings, mounts, migration state, restorable backups, authenticated PRD/abstention behavior, environment-specific PostgreSQL/restart/resource/recovery and production before/after isolation still require actual target evidence.

Source approval, fresh dispatch, release tests, image construction and security-scan steps are resolved. Another generic source/deployment approval is unnecessary. No publication review has been performed by the assistant, no image published or VPS service changed. No other person was messaged. `next-commands.md` gives the concrete existing-run review action and remaining target gates.

## Binding target and remaining execution

Deploy only `nexus-remote-stage-rc3-28acdbc` under `/home/ubuntu/nexus-rc3-staging-28acdbc/release`. Preserve the existing customer workspace/authentication, credentials, TLS, overlays, named volumes, mounts and financial data. Include `intelligence-recovery-20261018/compose.memory.yaml` last; verify memory and memory-plus-swap each **1536 MiB / 1610612736 bytes** after rendering and recreation. Retain existing image/configuration identities and recovery backups for rollback.

The candidate's historical runbook proposed a separate namespace. The current explicit user target supersedes it: do not create a project or replace production. Do not pull main, discard volumes, downgrade/reset financial databases, enable feeds, start real-money/new-paper execution or automatically promote a strategy. Research-worker acceptance must stay research-only. Use existing real evidence and require abstention for incomplete financial evidence.

After actual source/image/scan and preservation checks, update only authorized staging services, complete the full authenticated workflow and environment-specific checks, and compare production identities/mounts before and after. Report actual results. Task 6 is not complete until those VPS deployment and acceptance gates pass; no feature sprint is authorized.
