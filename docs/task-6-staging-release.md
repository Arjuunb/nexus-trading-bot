# Task 6: isolated staging release

Task 5 is published as `0ded1cc4cb77268cd4120224fa1f0d629589bd6b` on
`finish/strategy-intelligence-staging`, with successful CI run `39155905417`.
Task 6 prepares the staging release and records the actual deployment blockers.
**It is not deployed and Task 6 is not complete.**

## Authorized scope and current access

Use the existing separate remote staging Compose profile and fresh immutable
API/worker image digests from the guarded publication workflow. Begin with
PostgreSQL, sequential state/metadata initializers, API and loopback HTTPS
ingress. Add the research worker only after the target's Stage A acceptance,
worker/database-role checks and capacity checks pass. Shadow observation,
public-feed producers and all live execution remain disabled. Production
containers, Compose configuration, volumes and financial records are outside
this deployment.

Prepared target namespace: `nexus-remote-stage-intelligence-t6`, with loopback
HTTPS port `18443`. The public configuration packet belongs in a new
`/home/ubuntu/nexus-staging/intelligence-task6/<exact-commit>/` directory;
private bindings belong separately under
`/home/ubuntu/nexus-staging-private/intelligence-task6/<exact-commit>/`.
These are planned paths, not created or remotely verified paths. Actual image
UIDs and registry digests must come from the fresh publication proof.

The configured target from prior work is `ubuntu@57.129.155.230:22`. The
read-only SSH probe in this workspace returned:

```
ssh: connect to host 57.129.155.230 port 22: Network is unreachable
```

This workspace also has no SSH agent or previous `~/.ssh/id_ed25519`, and no
Docker CLI. No remote command ran, no remote file was transferred and no
production/staging service was inspected, restarted or changed. Restore access
from an SSH-capable workspace using the existing private key locally; do not
paste or commit it. Retain strict host-key verification against the independently
verified provider record documented in the existing staging runbook.

The connected GitHub tools support source publication and CI inspection, but
do not expose workflow dispatch. GitHub returned 404 for
`.github/workflows/staging-release.yml` on `main`: this workflow has not landed
on the default branch, which GitHub requires for manual dispatch. The development
branch and `main` required integration. The development branch now preserves
`main`'s accepted RC3 controller, sanitizer tests and API isolation regressions.
One test file required conflict resolution: both scalar ownership guards and
both sets of added tests are retained. The historical manual RC3 controller and
its fixed source identity remain unchanged. No default/production-branch update
was performed here. Draft PR #41 makes the development integration reviewable.
Protected environment configuration could not be verified through the connector.
This does not establish that protection is absent and does not waive it.
Any integration or conflict resolution that changes the candidate requires a new
exact-source freeze and validation. This receipt cannot certify a later merge.

## Release validation correction

Task 5 introduced a multi-server Playwright configuration. The unchanged RC3
clipboard wrapper extends a single server object, so spreading that array loses
its command and fails to start the dashboard. The new configuration preserves
the single-server form for that wrapper. The ordinary Task 5 focused browser
job still starts both its dashboard and isolated API itself.

`ops/run-release-browser-checks.py` verifies the exact accepted RC3 harness
hashes, starts only the temporary loopback paper/API fixture, waits for its
unchanged-financial-state readiness result and runs the full original browser
suite. It enables the actual-paper acceptance case rather than skipping it.
Its `finally` block stops only its own fixture child. The harness, strategy and
financial logic, original browser assertions, minimum test count and no-skip release gate
are unchanged. The release workflow invokes this runner using its locked test
Python environment. No deployment operation is added to CI.

The first local full-browser attempt found the browser cache missing after
workspace inactivity. That failed run is retained. Installing the pinned
Playwright Chromium restores the test prerequisite; the subsequent actual
result is recorded in `validation/task6/` without hiding or skipping failures.

The first run with Chromium available passed 160 cases and failed five. It
exposed header controls overlapping the menu/title and account controls, and an
analytics grid whose minimum content width escaped the phone viewport. The
header now reserves space for its outside controls and lets the middle controls
wrap on desktop; phone controls retain their horizontal scroller. Grid tracks
can shrink while their tables retain their own scroll containers. These are
layout changes only. The original failing assertions are retained for the final
full-suite rerun. A temporary measurement spec is removed before freezing source.

The final unchanged reviewed suite passed **165 tests, zero failures/errors/skips**
in 358.41 seconds. Both real clipboard cases and the actual-paper API acceptance
case ran. The owned API fixture was unreachable after the runner exited, as
expected. The six original focused layout regressions also passed, along with
one temporary measurement diagnostic. Release guards passed 82 tests; the
historical/actual-paper evidence integration checks passed 32 tests. Dashboard
typechecking and the production build passed. See `validation/task6/results.json`
for the pre-publication result. These browser results were collected for
`1188aac`; the subsequent main integration changes only controller/test inputs.
All 284 focused main-integration, ownership, workflow-guard and evidence tests
passed after resolving the conflict. All 141 API tests passed again after the
final test arrangement, with every original assertion preserved. The final receipt adds the new published
candidate, its exact source hash, the candidate's full browser rerun and actual
CI state.

## Exact-source preparation and publication

The final receipt records the new candidate commit and source SHA-256. Freeze
from a clean detached worktree of that exact commit, not the development
checkout containing validation receipts. The existing source freezer excludes
runtime state, private files and generated research journals. Source manifests
and retained source are preparation evidence, not built/scanned images.

After the workflow is available on the default branch and the exact reviewed
candidate is selected on an allowed `release/*` branch or `nexus-staging-*` tag,
the existing manual publication command is:

```bash
gh workflow run staging-release.yml --repo Arjuunb/nexus-trading-bot \
  --ref "$RELEASE_REF" --field reviewed_commit="$RELEASE_COMMIT" \
  --field expected_source_sha256="$SOURCE_SHA256"
```

Use the exact commit and source hash in the final receipt, with the separately
reviewed allowed release ref. No release ref has been created by this preparation.
The workflow must complete locked
regressions, disposable PostgreSQL tests, the unchanged RC3 browser suite, fresh
API/worker builds and all required source/image security scans. Publication
retains the existing protected `ghcr-staging-release` environment and actual
distinct-reviewer approval checks. Neither this task request nor a local status
file can stand in for that server-side approval. No approval guard was changed.

No fresh image digests, scan success or publication is claimed until that run
produces its accepted `publication.json` and sealed configuration packet.

## Target execution and acceptance

Continue with the existing exact-packet procedures in
`automation-hub/docs/NEXUS_GITHUB_STAGING_RELEASE.md`, using the new candidate
receipt rather than the historical commit/source hashes in that document.
Recheck target identity, production container IDs/restarts, project/volume
ownership, free loopback port 18443 and capacity before transferring anything.
Use a new independent staging directory, credentials and project. Do not copy
production dotenv or financial databases.

Provision separate customer auth, trusted staging TLS and encrypted backup/
restore evidence. Validate the actual target Compose profile and immutable
image identities. Run the initializers sequentially and start only the explicit
Stage A services. Record initializer exits, container health, target resource
use and original production-container snapshots. Authenticate as real staging
users and verify tenant isolation, frozen identity, missing-cost abstention,
qualification, EdgeScore, selection, decay and weekly-report behavior.

Only after that acceptance, start the research worker explicitly and validate
its health/heartbeat, restricted metadata role, restart recovery and dataset
boundaries. An empty staging database is not evidence that historical or actual
forward-paper episodes have been ingested. Do not import production financial
records or manufacture complete evidence to make a dashboard appear populated.

On failure, stop only the owned staging research worker, ingress and API;
retain PostgreSQL and every volume/evidence artifact. Never run `down -v`, prune,
production restarts, database downgrades, financial resets or automatic strategy
changes. A first deployment has no prior accepted release to invent for rollback.

## Completion gate

Task 6 stays **BLOCKED / NOT DEPLOYED** until the exact-source release has been
built/scanned/published and actual target access, isolated startup, authentication,
TLS, backup/restore, health and research-service verification are recorded.
Local test success is release preparation evidence, not VPS deployment success.
