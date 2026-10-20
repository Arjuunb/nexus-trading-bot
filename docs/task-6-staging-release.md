# Task 6: existing RC3 staging deployment and acceptance

**INCOMPLETE — approved images built/scanned; direct verified-archive staging preparation ready; VPS deployment and acceptance remain pending. Registry publication remains failed.**

The user approved replacing original Task 5 source `0ded1cc4cb77268cd4120224fa1f0d629589bd6b`
with the concrete validated replacement:

- Commit: `4eceb973e98e1350dd625153753637629453c401`
- Tree: `1af45a4993d83919c5007373ba8b9a27e9de5a71`
- Source SHA-256: `82a41d03d5c049051d740d154a6c941efae01b4370f4b6d167b7167b54582ea8`
- Verified release ref: `release/strategy-intelligence-task6-4eceb97`

All 1468 frozen files rehashed with zero mismatches. Exact-source release
validation in fresh manual run 39503485769 passed 4553 tests on each Python
version, 31 PostgreSQL tests and 165 browser tests. Actual API-image tests passed
700 tests with 25 skips. Both image vulnerability/secret scan gates also passed.
These are CI/build results; actual VPS acceptance remains pending.
The source approval is resolved. All existing target/preservation boundaries
below remain binding; later documentation commits are not deployment sources.

The previous 0ded manual run failed in the reviewed browser launcher. Its
immutable wrapper could not consume Task 5's server array. The approved
replacement repairs that integration while preserving reviewed assertions.
Fresh image build and security scans have completed successfully. Publication
failed with `PROTECTED_ENVIRONMENT_WITH_DISTINCT_USER_REVIEWER_REQUIRED`.
GitHub records initiator Arjuunb approving the run; only that user was listed.
The guard requires a different configured reviewer, self-review prevention and
an enabled intended release-branch policy. Those settings require correction and a fresh manual run if registry publication
is pursued. The selected direct-archive staging route performs no registry write.

## Selected delivery route: existing verified image archives

No second reviewer is available. The original task authorizes staging deployment
separately from registry publication; the publication workflow explicitly makes
that separation, and the project supports immutable local image config IDs.
Use the already tested/scanned exact 4ece image archives for the existing RC3
project. Keep registry publication failed/protected; do not forge an approval,
change the guard or claim registry publication. This route changes no source or
image bytes and needs no new build. The existing explicit staging/source
approval applies; current target preservation and acceptance gates remain.

The prepared image-only overlay, zip/export checksums, exact exported manifest
chains and ordered steps are in `validation/task6-approved-4ece/direct-archive/`.
Offline checks passed; actual VPS Compose rendering/loading/deployment have not
run. `next-commands.md` now starts with read-only discovery of the existing
Compose environment-file bindings. No new credentials or project are created.

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
Fresh manual [run 39503485769](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39503485769)
built and scanned the exact approved source. All 16 sealed files and 12 stored/
decompressed image layer hash pairs were independently verified. No registry
publication or actual VPS deployment has occurred.

Publication failed its configured-protection guard in `ghcr-staging-release`:
`PROTECTED_ENVIRONMENT_WITH_DISTINCT_USER_REVIEWER_REQUIRED`. Registry login and
push were skipped. History confirms initiator Arjuunb approved their own run.
The approved guard requires a distinct configured individual reviewer,
self-review prevention and an enabled intended deployment-branch policy.
Full environment configuration is unavailable through the connected endpoint;
If publication is pursued later, inspect/correct those settings before a fresh
manual first attempt; job retries are rejected. Direct archive staging uses the
separate existing deployment authorization and keeps this protection unchanged.

Artifact ID 12303500964, SHA-256:
`bcdcdcab3b3870cad1dbc6913e941979b2ec0b22e69e6726117d35e5bf0bb1da`.
It expires 2026-10-27T22:07:45Z. Built config/export IDs, scan hashes, actual
fresh test counts and exact review state are recorded in
`validation/task6-approved-4ece/`. Built config IDs are not registry digests.

The workflow performs release checks, source/image scans and guarded publication;
it has no SSH deployment job. Its protected environment/reviewer checks must
remain enforced. Retain any actual failure and stop the affected step. Do not
change the frozen approved source, select a later commit or weaken checks to
obtain a successful result. Fresh API/worker images and scans have been verified. Registry publication
and approved-image VPS deployment remain pending.

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
