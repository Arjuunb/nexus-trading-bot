# NEXUS container security audit and bounded remediation

**SECURITY RELEASE: NO-GO. Bounded Python remediation: IMPLEMENTED AND TESTED.**
Both rebuilt images have **44 HIGH, zero CRITICAL and zero secret findings**
under the unchanged release scan policy. This reduces the original 47 HIGH
findings per image by three, removing both Python vulnerability IDs. Eight
shared Debian vulnerability IDs remain. They were not suppressed, waived or
declared unexploitable. No publication, push, VPS access or deployment occurred.

## Audit scope and retained evidence

Scanner: **Trivy 0.69.3**. Original reports were created on 13 October 2026 at
21:56:50 UTC (API) and 21:58:15 UTC (worker), or 22:56:50 / 22:58:15 London.
The original retained local image config digests were:

* API: `sha256:2a06435876742ee690521ed21828d6ae093c92f0474834d71ccc68e7a7b531c3`
* Worker: `sha256:6ef0a094f2aa599bc5411d7f151512df28f3974912b51996d90611c1216728a5`

These are immutable local Docker config identities, **not GHCR manifest
digests**. Neither original image has a recorded registry digest. Their deployed
VPS identities are UNKNOWN; this audit did not connect to the VPS.

Original raw reports remain unchanged under
`/workspace/nexus-audit/github-staging-readiness/`. New evidence is retained at
`/workspace/nexus-audit/container-security-remediation/`, including:

* `original-inventory.csv` and `{api,worker}-full-original-inventory.json`:
  all 94 original package findings, image identities, CVE/GHSA aliases, installed
  and fixed versions, severity, source/type, file path and image layer.
* `deduplicated-original-inventory.json` and `reachability-assessment.json`.
* `{api,worker}-final-scan.raw.json`, `final-before-after.json`, scan logs and
  per-image remaining inventories. Raw scanner output is private local evidence.
* `api-final-build/`, `worker-final-build.log`, `worker-wheels.json`, dependency
  download logs and frozen public build-input manifest.
* `final-image-source-runtime-proof.json`, `final-image-export-proof.json`,
  exact exported image archives, JUnit/logs, smoke and PostgreSQL reports.

Rescans deliberately reused the retained database schema 2 snapshot, updated
13 October 2026 19:11:46 UTC, downloaded 21:56:43 UTC, with `--skip-db-update`.
Its SHA-256 is
`d808e14eb4c683ed715f112b0dce435219f3a0b6806df5227edd8419230a7238`.
This permits comparison without an intervening advisory update; it is not a
claim that later databases will have identical findings. Scan configuration
remained `vuln,secret`, `HIGH,CRITICAL`, exit code 1. No ignore file, VEX exemption,
ignore-unfixed flag or severity downgrade was added.

## Original inventory and deduplication

Each image has 47 findings: 44 OS package findings and three Python findings.
The combined 94 records contain **10 unique CVE IDs, all shared by API and
worker**. GHSA aliases identify the same vulnerabilities and are not additional
independent findings. Repeated packages from one Debian source package and the
direct/vendored wheel installations do not create new CVEs.

| Vulnerability | Original package/component and installed version | Findings per image | Fixed version reported | Result |
| --- | --- | ---: | --- | --- |
| CVE-2026-23949 / GHSA-58pv-8j8x-9vj2 | setuptools-vendored jaraco.context 5.3.0 | 1 | 6.1.0 | Removed from final scans by patched vendor dependency |
| CVE-2026-24049 / GHSA-8rrh-rw8j-w5fx | Direct and setuptools-vendored wheel 0.45.1 | 2 | 0.46.2 | Removed from final scans |
| CVE-2025-69720 | ncurses source: libncursesw6, libtinfo6, ncurses-base/bin 6.5+20250216-2 | 4 | Not reported | Remains HIGH |
| CVE-2026-16742 | systemd source: libsystemd0/libudev1 257.13-1~deb13u1 | 2 | Not reported | Remains HIGH |
| CVE-2026-54369 | libacl1 2.3.2-2+b1 | 1 | Not reported | Remains HIGH |
| CVE-2026-76642 | util-linux source 2.41.5-0+deb13u1 | 9 | Not reported | Remains HIGH |
| CVE-2026-78408 | util-linux source 2.41.5-0+deb13u1 | 9 | Not reported | Remains HIGH |
| CVE-2026-78409 | util-linux source 2.41.5-0+deb13u1 | 9 | Not reported | Remains HIGH |
| CVE-2026-78410 | util-linux source 2.41.5-0+deb13u1 | 9 | Not reported | Remains HIGH |
| CVE-2026-9538 | perl-base 5.40.1-6+deb13u1; Archive::Tar advisory | 1 | Not reported | Remains HIGH |

The nine util-linux package records per CVE are bsdutils, libblkid1,
liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount and util-linux.
The CSV/JSON preserves each exact binary package version, including login's
`1:4.16.0-2+really2.41.5-0+deb13u1` and epoch prefixes on utilities. Grouped
source versions in this table do not replace those individual records.

## Root causes and runtime reachability

**Python:** build-time packaging tools were explicitly installed and retained
in the runtime filesystem by the existing API/worker builds. Setuptools 80.9.0
included vulnerable jaraco.context 5.3.0 and wheel 0.45.1; direct wheel 0.45.1
accounts for the third finding. Some unchanged vendored bytes were attributed
to Python base layers, so layer origin alone does not identify the active
installed setuptools version. Inspecting distribution and vendor metadata does.
These are packaging/tooling dependencies, not trading-rule dependencies.

Static AST inspection of frozen non-test Nexus Python source found no direct
imports of setuptools, pkg_resources, wheel, jaraco, curses or readline. This
does not prove absence of dynamic or third-party use. Packaging tools have an
actual build-time use, and their unsafe archive functions should still be patched.
No source call-path audit proved every reported component exploitable.

**OS:** both Python images share Debian 13.7 base packages and the same eight
remaining advisory IDs. The current official Python 3.11-slim-trixie and
3.12-slim-trixie registry indexes resolved to the existing pins:

* API base index: `sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`
* Worker base index: `sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`

The supported Python families and base digests therefore remained unchanged.
No newer same-flavour official base or verified vendor fix was found through
these checks. Direct Debian security-tracker retrieval was unavailable; the
retained scanner reports contain no fixed version for these OS findings. Do
not generalize that to a claim that no vendor patch can ever exist.

Image inspection found systemd-homed absent at both inspected standard paths
and Archive/Tar.pm absent at the inspected Perl version path. Their source
packages still supply flagged libraries/core packages. These may be broad
source-package attributions; they are not independently reviewed exemptions.
Mount/nsenter tools and ACL/terminal libraries are present. Their exploitability
depends on privileged operations, input paths and runtime capabilities; it was
not demonstrated. The existing proposed runtime drops capabilities, and the
bounded state initializer has only its existing specific capabilities. None
were changed to make this audit pass.

Several flagged packages are Debian Essential or Protected (bsdutils, login,
ncurses-base/bin, perl-base and util-linux). Forcing their removal, changing OS
family or installing unverified packages would exceed this bounded fix and could
break initialization, filesystem tools or Python. Those actions were not taken.

**JavaScript:** neither retained/final image scan reported a HIGH/CRITICAL Node
finding. No JS dependency or lock changed. Lower severities were outside this
release gate's severity selection; this is not a claim of zero vulnerabilities
at every severity.

## Minimal remediation

| Package | Before | Pinned after | Relevant vendor contents |
| --- | --- | --- | --- |
| setuptools | 80.9.0 | 80.10.2 | jaraco.context 6.1.0, wheel 0.46.3 |
| wheel | 0.45.1 | 0.46.2 | Direct patched wheel |

Official PyPI metadata and downloaded wheel hashes were verified over HTTPS.
Both updates stay within their current major version and support Python >=3.9.
Only these two package selections changed; all other API and worker package pins
remain unchanged. The API lock still has 71 packages; worker wheel input still
has 12 packages. Build tools were patched rather than removed, preserving the
existing packaging and complete installed-lock verification contract.

Files changed:

* `ops/api-build-requirements.in`: two patched packaging pins.
* `ops/api-requirements.lock`: only those two versions and verified hashes.
* `ops/prepare-shadow-worker-wheels.py`: matching two worker packaging pins.
* `ops/github-release.py`: bounded stdin for large image source-hash inventories.
* `automation-hub/tests/test_github_release.py`: two provenance regression tests.
* `automation-hub/tests/test_container_security_runtime.py`: four actual-image
  direct/vendor version and dependency-graph checks.
* This report.

Dockerfiles, strategy code, authentication rules, financial schemas, risk rules,
execution flags, active settings and all 22 SQL migration files were unchanged.
No new migrations or distributed infrastructure were introduced.

The integrity helper's original 158,096-byte single argument exceeded Linux's
131,072-byte single-argument limit and raised E2BIG. Two failing regressions
reproduced the problem before the fix. Fixed receiver code now receives bounded
public JSON over stdin (4 MiB maximum), preserving every source-hash assertion
and existing read-only/no-network probes. This is a release-integrity repair,
not arbitrary code or execution functionality.

## Rebuild and immutable identities

Both final builds used the existing Dockerfiles, `--no-cache --network=none`,
pinned bases and verified public package contexts. The API rebuilt both SPAs
with the existing Node 20 build stage. Worker copies remain code-only, excluding
the app, financial execution package, UI, credentials and runtime databases.
No new image was built by modifying a retained reviewed image in place.

Final build-source SHA-256:
`28af78bf23a5ba4afed9a390b661a7e177bd798c32db1651029928a729c60a19`.
The frozen build has 1,375 source inputs. The final operator report is added
after validation and has a separate source inventory; it does not silently
change the frozen image inputs. The Git base remains
`7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`; the changes are uncommitted and
have no human release acceptance.

| Final local image | Config digest | Export SHA-256 |
| --- | --- | --- |
| nexus-api:sprint12-security-remediation-v2 | sha256:3e29eb32d34759c6570cb2100c2eac0753906ac22c57ba016332fcb5fe8de17b | ee53c6bfa0e2c8f82a001fee40db6c5a82b086aadc5d17597ad7c7d7ad77ffb4 |
| nexus-workers:sprint12-security-remediation-v2 | sha256:188ff08e71ee4b41507c7a34caab6fa0559fe74dd998949bdfe4c3bbb05ae952 | e86a92357417ce440d0e7d3d39d7febad478a7ced1b8cccfa25a53461a701e11 |

These are local config digests and exact Docker export checksums. GHCR registry
manifest digests remain UNKNOWN because publication is prohibited/unperformed.
Each export's config and all six uncompressed layer hashes were verified.
No private application inputs were found in those archive layers. Runtime
source checks verified all 1,375 API inputs and 315 worker inputs, nonroot users,
Python 3.11.17 / 3.12.15, binary PostgreSQL driver and the worker code boundary.
All 266 generated frontend file hashes match across the two remediation builds.

## Actual validation results

| Executed suite | Passed | Failed/errors | Skipped | Warnings |
| --- | ---: | ---: | ---: | ---: |
| Full repository after final code fix | 3,958 | 0 | 76 | 85 |
| Focused release/prerequisite tests | 60 | 0 | 0 | 0 |
| Release guard tests | 33 | 0 | 0 | 0 |
| Final actual-image tooling/dependency checks | 4 | 0 | 0 | 0 |
| Actual disposable PostgreSQL migration/recovery/restore | 32 | 0 | 2 | 0 |

Suites overlap; do not sum them. Full-suite time was 482.978 seconds. Its 76
skips include the prior opt-in/parameter-scenario skips and four actual-image
cases, which ran separately against the final image IDs. Two PG container-
supervision cases remain opt-in/unrun in that suite; the separate existing
rehearsal exercised both worker crash recoveries. The 85 warnings are existing
FastAPI/aiohttp deprecations, including one more occurrence of the existing
aiohttp cleanup warning than the prior 84-warning run. No warning suppression
or assertion weakening was introduced.

The first rebuilt-image disposable rehearsal passed API HTTPS/local fixture
auth, two real Chromium smoke scenarios, both actual UI bundles, restricted
PG authority, worker crash recovery, database/API restart, backup/restore and
unchanged financial-fixture hashes. Shadow had zero registrations and no
observations. Final images additionally passed tmpfs-only API startup and
research-worker READY/AVAILABLE checks, execution runtime not loaded, no jobs
submitted, no published ports, fresh PG migrations and idempotent rerun.
That final check created/deleted no Docker volumes. Early tmpfs smoke attempts
used an unsupported `local` auth-mode value; the test configuration was corrected
to the existing `legacy` development fixture contract, with emergency admin
disabled. Application authentication rules were not changed. Failure evidence
was retained. Fixtures do not certify customer Supabase identity or trusted
staging TLS. The earlier 154-case browser suite is historical; it was not claimed
as rerun here.

Original reviewed image tags/IDs and release archive remain intact. The prior
archive still hashes to
`b589b2c00f768651bc501569aa8106e2e87a2c5cfe1fce5138a9c2de8b4a369e`.
All 194 preexisting Docker volumes remain. The reused disposable rehearsal
cleans up only resources it created; final tmpfs tests use no Docker volumes.
No production financial record was read or altered. Workflow lint and whitespace
checks passed. No tests were deleted or assertions weakened.

## Before/after and release decision

| Image | Original HIGH | Final HIGH | Final CRITICAL | Final secrets | Gate exit |
| --- | ---: | ---: | ---: | ---: | ---: |
| API | 47 | 44 | 0 | 0 | 1 (FAIL) |
| Worker | 47 | 44 | 0 | 0 | 1 (FAIL) |

Unique vulnerability IDs: **10 → 8**. Remaining IDs: CVE-2025-69720,
CVE-2026-16742, CVE-2026-54369, CVE-2026-76642, CVE-2026-78408,
CVE-2026-78409, CVE-2026-78410 and CVE-2026-9538. Every remaining finding
has no fixed version reported in the retained scanner database. Assertions
about reachability are limited observations, not permission to bypass the gate.

**Stop point: the verified safe bounded fixes are complete; security release
remains NO-GO.** The exact blocker is the unresolved Debian advisory set under
the existing HIGH/CRITICAL gate. The next safe step is obtain and verify vendor
fixes/a patched supported base, or obtain independently reviewed component-
specific vendor applicability evidence. Any future exemption requires a separate
documented review; none was implemented here. Do not force-remove essential
packages, switch OS families merely to change counts or silently relax policy.
Rescan/rebuild/retest against accepted inputs before publication can be considered.
Git/GHCR publication, human release acceptance, customer staging auth/TLS and
VPS transfer/startup approvals remain separate and are not granted by this audit.
