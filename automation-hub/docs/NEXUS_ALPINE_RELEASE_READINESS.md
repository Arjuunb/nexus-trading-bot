# NEXUS Alpine release integration and human acceptance

**LOCAL PREPARATION: VERIFIED. HUMAN RELEASE ACCEPTANCE: PENDING.**
**GitHub/GHCR publication: NO-GO. Stage A deployment: NO-GO.**
This report prepares a Stage A-only release for explicit review; it does not
approve Git mutation, publication, VPS access, transfer, deployment or trading.
Assessment: 14 October 2026, UTC.

## Retained evaluated candidates

| Identity | API | Research worker |
| --- | --- | --- |
| Local image config ID | sha256:11e9509803bd2b41f594b3e5c6f85a16cdccc3d047cb990550c54674bc4429dd | sha256:d25a0cec6a427fc348f25e3467a22033c44274f94e8ae1bd459cacadfb28f636 |
| Export SHA-256 | c1607ab225c75e40fcc6dde97a6d62bcabe889e1d5811a823123cffbefa7c93c | c051aa2e03dbc28c8e5c804d612ead755dd4529e53cffdb5f9f149e2ef8bb506 |
| Original frozen source SHA-256 | 1ac6401d9ed158d0f0afbbda1e94f45154e18b4f30c7f9f4cca14f4cfb7d7e25 | ff09b1a3ba01249683bd3b88d16735b090d341558198859de5e7c621dff5a149 |
| Original source inputs | 1,382 | 1,380; 315 curated image inputs |
| Reviewed recipe | ops/api.alpine.Dockerfile | ops/research-worker.alpine.Dockerfile |
| Recipe SHA-256 | d7c61ac3ae598e6f669a42d5ecc81aabf74a200c9b867cd05b952e97a7dc8aa3 | e3e5edb2f78921b6c42989de3a1560bf3cad71adf7155d9f3f0aa808f697c64d |
| Official Python base index | python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929 | python:3.12-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 |
| Python / default UID | 3.11.17 / 100 | 3.12.15 / 1000 |
| Pinned Python inputs | 71 packages; ops/api-requirements.lock | 12 packages; ops/prepare-shadow-worker-wheels.py |

The API lock SHA-256 remains
`7ac510e8d1dd86f6c56c1f8f4e1fac491585bb39d3ba1a540e34d29dddc52d15`.
The worker helper SHA-256 remains
`430c4854a4f43ec543406fd8d60c6afddec60f2840c2f668bdf21ba72ca08d6b`.
Both evaluated recipes, locks, exports, Debian images and previous reports remain
unchanged. Registry base digests, local config IDs and tar checksums are distinct;
**no new published GHCR references exist**.

These candidates were built from different dirty-tree manifests with the same
Git HEAD, `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`. They are not relabelled as
one clean release build. Local probes verified each original source, native
runtime/dependencies and shared runtime hashes: 1,382 API and 315 worker inputs.
The verification records `SEPARATE_EVALUATED_SOURCE_MANIFESTS`, which publication
guards explicitly reject as a substitute for a single accepted release source.

## Local review material and Git proposal

Review material is retained outside the checkout at
`/workspace/nexus-audit/alpine-release-integration/`. Its final `review-source`
manifest records the integration preview's exact full source fingerprint,
tracked/untracked inventory, locks, migrations and exclusions. This is a new,
**unaccepted preview**, not either candidate's original build identity.
`integration-review.diff` distinguishes this task from pre-existing work;
the separate source inventory accounts for every required Git input.

Proposed branch: `release/nexus-alpine-stage-a-20261014`.
Proposed commit subject: `release: prepare reviewed Alpine images for isolated Stage A`.
Proposed tag: `nexus-staging-alpine-20261014-rc1`.
No branch, commit or tag was created. The future accepted commit ID is UNKNOWN.
Review the existing application changes as well as this task's operator diff;
do not infer that all uncommitted files are integration changes or already accepted.
Do not stage `.venv`, node_modules, credentials, keys, runtime/financial databases,
state, generated test secrets or image exports. Source-freeze guards deny them;
only the CI artifact policy permits the two named, tested image exports.

## Prepared CI and publication controls

`staging-release.yml` now selects the two evaluated Alpine Dockerfiles. It uses:

* Clean accepted commit/ref and exact full-source fingerprint verification.
* SHA-pinned actions, original Python/npm locks and immutable base references.
* CPython 3.11 musllinux/abi3 API wheels checked against approved lock hashes;
  CPython 3.12 worker musllinux inputs through the existing helper.
* Retained verified frontend cache; network-disabled Docker build instructions.
  Online preparation fetches only public locked dependencies over verified TLS.
* Actual native-image imports, package-version/UID/TLS/disabled-flag checks,
  common source/math checks and the fixed native API regression matrix.
* Original vuln+secret HIGH/CRITICAL, exit-code-1 gates, including unfixed issues;
  additional all-severity secret gates for both images.
* A sealed source/image/test/scan packet; publisher verifies the exact artifact ID,
  source mode and loaded config IDs before login. Published references are actual
  GHCR @sha256 digests, never tags or fabricated values.
* Existing `ghcr-staging-release` environment approval by a configured distinct
  reviewer, prevention of self-review, protected release refs and first-attempt
  request checks. Publication grants no deployment or trading authorization.

The reusable validation workflow still requires full Python 3.11/3.12 regressions,
frontend builds, local PostgreSQL tests and browser checks. Local actionlint syntax
validation passed; ShellCheck was unavailable and was not claimed executed.
No GitHub Actions run, environment-policy query, registry login or publication
was performed. **A future CI rebuild creates new image identities** because its
source/commit metadata changes. It must pass fresh tests/scans and receive fresh
acceptance; prior candidate results do not automatically certify those bytes.

## Stage A configuration

New `ops/staging.alpine.compose.yaml` extends the existing remote/local profiles.
Guarded binding preparation selects it with `--compose-profile alpine`. Profiles
are allowlisted; old bindings retain their previous default. Config packets now
include every referenced public Compose file and verify their accepted hashes.

Default Stage A services are **postgres, state-init, metadata-init, api, ingress**.
Research worker requires profile `research`; shadow requires `shadow`. The
public-feed producer is absent. Do not use the legacy remote profile as an
Alpine Stage A default: it leaves the research worker active by default.

Local Compose **2.40.3** parsed both the default and all-profile configurations.
Verified: independent project/network/volume names; internal metadata network;
separate PostgreSQL metadata database/roles; loopback-only 18443 ingress; no
published API/DB port; original health checks, read-only application filesystems,
capability restrictions, bounded retries and CPU/RAM/PID limits. All execution,
market-feed, automatic research and shadow flags are zero. Customer Supabase
auth remains required; emergency admin is disabled. Generated bindings are new,
private and outside Git; customer credentials and trusted TLS are not configured.

API image UID 100 and inactive worker override UID 100:100 are nonroot. The
evaluated worker's default UID is 1000; the inherited volume-ownership mapping
is therefore a separate Stage B rehearsal requirement. Initializer root/limited
capability exceptions, PostgreSQL bootstrap and the existing privileged nginx
master/capability allowances are preserved and exposed for operator review;
this report does not claim every infrastructure process is nonroot.

| Limit | Stage A steady state | Stage B adds research worker |
| --- | ---: | ---: |
| CPU ceilings | 2.25 CPUs | 3.25 CPUs total |
| RAM ceilings | 1,376 MiB | 1,888 MiB total |
| Initializers | Sequential; each 0.5 CPU / 256 MiB / 64 PIDs | Same |

The supplied 13 October resource series gives only conditional observed margins:
approximately 2.43 CPUs/4.46 GiB for Stage A, 1.43 CPUs/3.96 GiB for Stage B.
These are historical short-window estimates, not production peak/reserve approval.
Approximately 22 GiB free disk does not establish growth/restore headroom; cache
reported reclaimable is not available space. No cleanup or new VPS inspection
was performed. Target Compose compatibility, current port availability and exact
project ownership remain UNKNOWN. **No staging services were started.**

## Security and compatibility evidence

Both complete evaluated images have retained Trivy 0.69.3 results:
**0 HIGH / 0 CRITICAL / 0 detected secrets**, database SHA-256
`88daf0f4f0789698811bec5ebf95ef9610cd0f4d0571394f754a5cdb8cf081c9`.
API raw scan SHA-256:
`59e4aaf0ea96766d1ce28115813e17d24596f23f3937881b126ce7e653234531`;
worker raw scan:
`c5f3b082dd94b6a88832ac3e7eaf410deedbc08b95c220ce22418b0c1e711f90`.
The API all-severity secret scan remains zero; an additional worker all-severity
scan executed during this preparation also found zero. No exceptions or ignores.
These do not certify host/upstream infrastructure or future rebuilt images.

The new API input preparer reproduced **all 71 evaluated wheel hashes**, including
21 native wheels. Its complete public-input manifest is the same
`4cc6b387fd078e45f547c8c217c7a849a876b77eb92682eccf9754acb420d8e3`.
Existing TLS/authentication, PostgreSQL migration/recovery, populated restore,
financial invariants and frontend evidence remains traceable in the
[API evaluation](NEXUS_ALPINE_API_EVALUATION.md) and
[worker evaluation](NEXUS_ALPINE_WORKER_EVALUATION.md).
The numerical/strategy domain fixtures match exactly; native API analytic results
had two ~1.11e-16 differences within the predeclared tolerance. System CA counts
changed from 150 to 121; actual target/provider chains remain unverified.

The worker's Decision Brain/pandas dependency gap remains. Read-only imports do
not certify job execution; the CI proof keeps support UNKNOWN or dependency-blocked
and `worker_activation_authorized=false`. Stage A uses the image for initialization
only. Research activation requires a separate supported-job/catalogue evaluation
and approval; generic unsupported shadow/SMC paths remain blocked.

The Debian default-command shutdown control failure remains **exit 137, not OOM**.
Alpine's evaluated API default command passed shutdown with exit 0. No assertion,
Debian image or historical result was rewritten to remove that finding.

## Actual tests for this integration

| Executed check | Passed | Failed/errors | Skipped | Warnings |
| --- | ---: | ---: | ---: | ---: |
| Final full repository | 4,011 | 0 | 76 | 85 |
| Focused release/Alpine/binding guards | 102 | 0 | 0 | 0 |
| Prepared CI native API runner, evaluated API image | 611 | 0 | 25 | 4 |
| Local Compose syntax/policy | PASS | 0 | — | — |
| actionlint workflow syntax | PASS | 0 | — | — |
| Read-only original-source/native pair checks | PASS | 0 | — | — |
| Worker all-severity secret scan | 0 detections | 0 | — | — |

Suites overlap. Root regression took 479.70 seconds; the prepared native runner
45.50 seconds. Root warnings are existing FastAPI/aiohttp deprecations (frequency
varies); native warnings are four existing FastAPI on_event deprecations. No global
warning suppression. Conditional/opt-in skips remain visible. The native runner
uses hash-locked, separate pure-Python test tools, not new image dependencies.

New regressions cover incompatible/unapproved/ambiguous wheels, exact locks,
profile injection, unsafe runtime/TLS/package states, path traversal, nonroot
tool access, archive exclusion and refusal to publish mixed evaluation manifests.
Initial failing tests and setup attempts remain in the audit directory. A native
runner attempt failed because a cloud umask made its tooling root unreadable;
the permissions regression preceded the public-tool mount fix. Compose initially
omitted inactive profiles as designed; its validator now parses both views.

No new combined service startup, TLS login, browser suite, DB migration/recovery
or backup/restore was run in this task. Previous component evidence is retained,
not counted again as new combined-release testing. GitHub CI and target-VPS
validation remain unexecuted.

## Human acceptance checklist and remaining gates

| Gate | Current decision | Required acceptance/evidence |
| --- | --- | --- |
| Evaluated image/export/recipe/lock identities | PASS | Review exact references and retained integrity proofs |
| Native compatibility/reference source correspondence | PASS within tested scope | Accept limits, CA/numeric differences and visible Debian control failure |
| Single release source/commit/tag | BLOCKED | Review complete tracked/untracked inventory; explicitly authorize commit/tag/push; accept resulting clean fingerprint |
| Stage A scope | PENDING | Named reviewer, UTC time, accepted source/config hashes and initializer-only worker scope |
| GitHub environment/ref protections | UNKNOWN | Verify configured distinct reviewer/self-review prevention, access and package permissions |
| Fresh CI build/tests/scans and image acceptance | NOT RUN | Accepted commit; successful required jobs; review exact artifact/scanner identities before publication |
| Actual GHCR references | NOT CREATED | Separate publication authorization and reviewed environment approval |
| Stage A local Compose | PASS syntax only | Select guarded Alpine profile and accepted immutable refs |
| VPS compatibility/names/ports/reserves/storage | UNKNOWN / CONDITIONAL historical capacity | Separately authorized current inventory/Compose probe, ownership and reserve/growth decisions |
| Host security maintenance | PENDING | Approved patch/reboot disposition; no update/reboot here |
| Separate customer auth/trusted TLS | BLOCKED | New staging authority/credentials, reviewed TLS and functional read-only checks |
| Monitoring, rollback, backup/restore ownership | PENDING | Named owners, isolation boundaries, retention, restore/incident runbook acceptance |
| Combined staging/restart drill | NOT RUN | Separate staging authorization, never production state |
| Stage B / research worker activation | BLOCKED | Resolve/review supported job paths, UID mapping and independent supervision tests |
| Deployment/trading authorization | NOT GRANTED | Separate explicit deployment authorization; trading remains disabled |

Record reviewer identity, UTC timestamp, exact source/config/image/scan references,
accepted scope, reason and unresolved limitations in a new acceptance record.
Do not convert this local-preparation authorization into publication acceptance.
Do not delete failed tests/experiments or silently replace old artifacts.

Rollback preparation retains all Debian/Alpine references and old packets. No
active system was replaced. Any later rollback is confined to the new staging
project and requires a reviewed compatible backup; no production volumes,
networks, services or financial databases are rollback targets. Vulnerable Debian
images are historical references, not automatically approved release fallbacks.

**Smallest next safe action:** review the local integration diff, complete source
inventory, candidate evidence and Stage A-only acceptance scope. Then grant a
separate explicit authorization for the approved Git commit/tag/push. A later
manual CI run must build and scan the accepted source and obtain protected
environment approval before GHCR publication. VPS staging remains a separate
authorization after its remaining operational gates. Stop before external action.
