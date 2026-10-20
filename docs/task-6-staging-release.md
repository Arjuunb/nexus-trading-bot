# Task 6: existing RC3 staging deployment and acceptance

**INCOMPLETE — approved Task 5 images have not been deployed or accepted.**

The user approved replacing original Task 5 source `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`
with the concrete validated replacement:

- Commit: `4eceb973e98e1350dd625153753637629453c401`
- Tree: `1af45a4993d83919c5007373ba8b9a27e9de5a71`
- Source SHA-256: `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8`
- Verified release ref: `release/strategy-intelligence-task6-4eceb97`

All 1468 frozen files rehashed with zero mismatches. Exact-source release
validation passed 4553 tests on each Python version, 31 PostgreSQL tests and
165 browser tests. These are CI results, not image scans or VPS acceptance.
The source approval is resolved. All existing target/preservation boundaries
below remain binding; later documentation commits are not deployment sources.

The previous 0ded manual run failed in the reviewed browser launcher. Its
immutable wrapper could not consume Task 5's server array. The approved
replacement repairs that integration while preserving reviewed assertions.
No fresh image build/publication has completed. A new manual first attempt is
required; retrying the previous cancelled workflow would violate source guards.

## Existing target and preservation requirements

- Compose project: `nexus-remote-stage-rc3-28acdbc`.
- Release directory: `/home/ubuntu/nexus-rc3-staging-28acdbc/release`.
- Retain the existing customer workspace, authentication/credentials, TLS,
  configuration overlays, data mounts and volumes.
- Preserve API memory and memory-plus-swap limits at **1536 MiB / 1610612736 bytes**.
- Include `intelligence-recovery-20261018/compose.memory.yaml` last when rendering
  or recreating this project's Compose configuration. The base declares 768m;
  the currently listed overlays do not include the retained memory override.
- Preserve prior image identities, configurations and recovery data for rollback.

Do not create another staging project, pull main over RC3, regenerate workspace
credentials, discard data/volumes, change production databases/services, activate
public feeds, start real-money/new-paper execution or promote strategies
automatically. The current request authorizes the bounded staging deployment;
no additional blanket deployment confirmation is required. Existing image
publication and acceptance guards still apply.

## Actual operator-supplied VPS evidence, 2026-10-19

Existing API, ingress, PostgreSQL and research worker are running and healthy.
They report zero restarts and no OOM. Available host RAM is 6633 MiB; root disk
has 32G available at 67% used. This is a point-in-time baseline, not load testing.

| Existing component | Source revision | Image config ID |
| --- | --- | --- |
| API | `f8778ed42039d415f3829e08a05225976fe0d0a5` | `sha256:f2343e76a896ec3718328f3318adc3c2a40c93774194602858de4bdd4dce5397` |
| Worker | `28acdbccc4f9b737c683d94e15bb16a09e50aa85` | `sha256:25daa1ca6ea70c493053ab60635844d82ea5b147daf8ae64da7b58a508277fb1` |

Both existing initializers exited 0. The API's actual memory/swap limits are
1610612736 bytes, and the retained override contains 1536m for both settings.
These earlier initializations do not establish migration success for the
undeployed approved release. Research-worker health does not establish actual
job execution, heartbeat/recovery, restricted-role behavior or PRD acceptance.

The production app and nginx are healthy. No production mutation was performed
by this attempt. A full container ID/image/start-time/mount before-and-after
comparison remains pending; health alone does not prove production isolation.

## Exact-source build and security scan

The approved source freeze verified all 1468 retained files with zero mismatches.
The new release ref was created and independently verified at exactly 4eceb97.
Start a fresh manual build using the GitHub Actions UI or an authenticated CLI:

```bash
gh workflow run staging-release.yml --repo Arjuunb/nexus-trading-bot \
  --ref release/strategy-intelligence-task6-4eceb97 \
  --field reviewed_commit=4eceb973e98e1350dd625153753637629453c401 \
  --field expected_source_sha256=82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8
```

Connected tools cannot dispatch a fresh run. Do not use Re-run jobs, main or
the development head. Source guards require manual attempt 1 on a release ref.

The workflow performs release checks, source/image scans and guarded publication;
it has no SSH deployment job. Its protected environment/reviewer checks must
remain enforced. Retain any actual failure and stop the affected step. Do not
change the frozen approved source, select a later commit or weaken checks to
obtain a successful result. No fresh API/worker images or scans have been
verified yet. Ordinary CI success does not establish image publication.

## Remaining target and acceptance gates

Before updating services, capture full production and staging container/image
baselines, the actual Compose overlay order, mounts/volume ownership, existing
credential bindings, migration state and restorable backups. Verify exact-source
image labels, content and immutable digests against the build/scan packet. Use
only the existing staging project and preserve the memory overlay last.

After a bounded staging update, verify real authenticated dashboard/API behavior,
tenant isolation, frozen historical/forward evidence identity, EdgeScore,
Champion/Challenger, regime selection, decay and weekly research. Missing or
incomplete real financial evidence must produce NOT_QUALIFIED, UNSCORED or
abstention. Do not manufacture evidence or start new execution for acceptance.

Verify research-only worker behavior, actual PostgreSQL permissions/concurrency,
persistence, container/resource/restart behavior and backup/restore recovery.
Compare production identities and mounts after staging changes. A rollback must
use retained prior images/configuration and preserve volumes and the 1536 MiB
limit; never downgrade/reset financial data or run volume removal/pruning.

## Evidence and blockers

Current approval, source proof, status and ready-to-run inputs are in
`validation/task6-approved-4ece/`. The previous failed 0ded attempt and real
operator VPS/image baseline are retained in `validation/task6-approved-0ded/`.
Their prior source approval is historical. Neither historical candidate
runbooks nor documentation changes authorize a new target or another source.

The assistant workspace cannot reach VPS SSH and lacks the existing SSH identity,
Docker/scanner and workflow-dispatch capability. Operator-supplied evidence has
allowed the baseline checks above; remaining remote execution must use the
existing trusted SSH session. Keep private keys/tokens/credential values there.

Task 6 completes only when actual approved-image deployment and all required VPS
acceptance/recovery/isolation evidence pass. No new feature sprint is defined by
this final task of the six-task PRD.
