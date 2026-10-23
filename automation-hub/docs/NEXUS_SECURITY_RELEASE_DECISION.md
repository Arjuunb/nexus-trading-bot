# NEXUS final container security release decision

**Recommendation: EVALUATE VERIFIED ALTERNATIVE.**

**Current release: NO-GO.** Authorize a separate, local, worker-first evaluation
of the official Python 3.12 Alpine 3.24 runtime. Consider the corresponding
Python 3.11 API runtime only after that trial succeeds and receives further
approval. This recommendation approves neither implementation nor release,
publication, staging, trading or a vulnerability exception.

Assessment: **14 October 2026**, Europe/London. Base scans occurred
11:34–11:35 BST (10:34–10:35 UTC). No application alternative was built,
installed, integrated or deployed. Only public base images were pulled by digest,
inspected in isolated read-only containers and scanned. Existing code, locks,
images, release reports and financial records were preserved.

## Current Debian blocker

All eight official Debian CVE source entries were refreshed on 14 October and
compared with the preceding verified assessment: **unchanged**. Fresh
trixie/security/updates/backports InRelease signatures and validity were checked
with Debian's archive keyring. Changed security/backports indexes were retrieved
and verified by authenticated SHA-256 identity. None provides a newer candidate
for the 17 flagged binary packages. Both current official same-minor
slim-trixie Python indexes still resolve to the existing pins.

| CVE | Debian 13 source status | Fixed version recorded for unstable, unavailable in checked Debian 13 repositories |
| --- | --- | --- |
| [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720) | ncurses: vulnerable; no-dsa, minor issue; upstream-status TODO remains | 6.6+20251231-1 |
| [CVE-2026-16742](https://security-tracker.debian.org/tracker/CVE-2026-16742) | systemd: vulnerable; no-dsa, minor issue | 261.2-1 |
| [CVE-2026-54369](https://security-tracker.debian.org/tracker/CVE-2026-54369) | acl: vulnerable; intended unstable then point-release fix; individual patch backport unsuitable because of ABI changes | 2.4.0-1 |
| [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642) | util-linux: vulnerable; no-dsa, minor issue | 2.42.3-1 |
| [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408) | util-linux: vulnerable; no-dsa, minor issue | 2.42.4-1 |
| [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409) | util-linux: vulnerable; no-dsa, minor issue | 2.42.3-1 |
| [CVE-2026-78410](https://security-tracker.debian.org/tracker/CVE-2026-78410) | util-linux: vulnerable; no-dsa, minor issue | 2.42.3-1 |
| [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538) | perl: vulnerable; postponed pending upstream regressions | 5.42.3-1 |

The existing complete application images have **44 HIGH, zero CRITICAL and
zero detected secrets each**. Some findings are broad Debian source-package
attribution: the existing images lack systemd-homed and Archive::Tar, while
infocmp, ACL libraries and GNU mount/nsenter components are present. That does
not authorize suppressions. Debian's minor/no-dsa assessment is not a change to
the HIGH/CRITICAL release policy. Detailed package and applicability evidence
remains in [the preceding assessment](NEXUS_DEBIAN_VULNERABILITY_RESOLUTION.md).

## Supported alternatives and actual measurements

Trivy **0.69.3**, vuln+secret scanners, HIGH+CRITICAL selection and exit-code 1
were retained. All five alternative scans completed. They used the same retained
schema-2 database, updated 13 October 19:11 UTC, SHA-256
`d808e14eb4c683ed715f112b0dce435219f3a0b6806df5227edd8419230a7238`.
Alpine was recognized as **Alpine 3.24.2**, not an unknown/unsupported OS.

These are **base-image** counts, not full NEXUS candidate counts. App dependencies
and bundled native libraries can introduce additional findings. The existing
complete images' 44 findings cannot be compared directly as identical inventories.

| Option | Actual base HIGH / CRITICAL / secrets | Maintenance and compatibility | Decision |
| --- | --- | --- | --- |
| Current official Python 3.11/3.12 slim-trixie | Complete existing images: 44 / 0 / 0 each; not rescanned in this proposal task | Current Python versions and glibc contracts already tested; official bases unchanged; Debian fixes unavailable | Keep as preserved baseline; cannot release |
| Official Python 3.11 slim-bookworm | 55 / 2 / 0 | Python 3.11.17, glibc 2.36; older Debian/LTS coverage rather than a new fix; all eight original CVEs remain | Reject for this bounded remediation |
| Official Python 3.12 slim-bookworm | 53 / 2 / 0 | Python 3.12.15, glibc 2.36; retains setuid GNU mount and vulnerable source packages | Reject for this bounded remediation |
| Official Python 3.11 Alpine 3.24 | 2 / 0 / 0; neither finding is one of the eight Debian IDs | Python 3.11.17, musl 1.2.6; maintained branch; native-wheel/runtime compatibility requires testing | Evaluate after worker success and separate approval |
| Official Python 3.12 Alpine 3.24 | 0 / 0 / 0 | Python 3.12.15, musl 1.2.6; maintained branch; preserves Python minor/patch version | Preferred first local evaluation |
| Distroless Python Debian 13 nonroot | 31 / 0 / 0; five original IDs remain | Python 3.13.5, glibc 2.41; lacks shell/pip; differs from both required Python minors and existing build/startup contracts | Reject for this bounded remediation |

Bookworm's remaining findings include actual infocmp and setuid GNU mount
components, plus broad source attribution. Larger counts do not establish every
finding exploitable, but both CRITICAL findings and all eight original IDs still
fail the existing gate. Choosing it would not resolve this blocker.

Distroless genuinely removes several utilities, but retains vulnerable Debian
libraries and adds a Python-minor migration. Copying another interpreter into
it would create a custom maintenance/ABI responsibility beyond this proposal.
The current distroless support policy also leaves Python-specific details TBD.
It is not a drop-in solution for these images.

## Why Alpine is a genuine risk-reduction candidate

The evidence is component-level, not merely a different distribution name:

* The inspected Alpine bases do **not** contain GNU util-linux/libmount,
  libacl, systemd libraries/daemon or Perl. Their mount/nsenter commands resolve
  to **BusyBox**, have no setuid bit, and do not contain the identified GNU
  libmount implementation. Those four GNU util-linux mechanisms are not supplied
  by these inspected binaries. BusyBox can have its own vulnerabilities; it is
  not presumed universally safe.
* infocmp is absent. The remaining terminal library is
  `libncursesw=6.6_p20260516-r0`, newer than the advisory's affected upstream
  range; Python curses/readline remain importable. This removes the described
  vulnerable utility and uses newer library code rather than hiding its metadata.
* Alpine's official security database records util-linux fixes at
  `2.42.3-r0` for CVE-2026-76642/78409/78410 and `2.42.3-r1` for
  CVE-2026-78408. The preferred base does not need util-linux installed. Do not
  add it simply to imitate Debian tooling.
* Alpine's [release policy](https://alpinelinux.org/releases/) lists v3.24
  main support through **1 June 2028**. Community support ends at the next stable
  release; any required community package would need a separately reviewed
  support plan. Restrict proposed runtime additions to supported main packages;
  do not use edge/testing repositories or unmaintained compatibility shims.
* Official Python [3.11](https://peps.python.org/pep-0664/) security support
  runs through October 2027 and [3.12](https://peps.python.org/pep-0693/) through
  October 2028. Python 3.11 support therefore limits the API's maintenance window
  before Alpine's main EOL. A later Python-minor upgrade is a separate task.

Both official base configurations default to root. The proposal must retain
NEXUS's explicit nonroot users, capability drops, no-new-privileges, read-only
filesystem, quotas and credential separation. The inspection containers
explicitly used nonroot UID 65534, no network, no capabilities and bounded
CPU/memory. Their restrictions are not evidence that an application image has
already been configured or deployed safely.

The two Python 3.11 Alpine base findings are **CVE-2026-23949** in vendored
jaraco.context 5.3.0 and **CVE-2026-24049** in vendored wheel 0.45.1. NEXUS already
pins setuptools 80.10.2/wheel 0.46.2, with previously verified patched vendor
contents. Retain those pins; require the final Alpine image scan to confirm
removal. They must not be waived on the assumption that an upgrade will work.

## Dependency compatibility and operational risks

Public PyPI metadata was checked at **all 71 existing API versions** and **all
12 worker versions**, separately preserving API packaging 26.3 and worker
packaging 25.0. Every version has an appropriate pure-Python or x86_64 musllinux
wheel for the existing Python patch version and musl compatibility level.
Requires-Python constraints were checked. API-compatible wheel hashes are already
in its current hash lock. This is availability evidence, **not** an installation,
ELF-linkage, application-equivalence or complete-image security test.

The worker's two platform-specific downloads, websockets and psycopg-binary,
need new musllinux wheel bytes and independently verified public hashes at the
same versions. The other ten worker wheel identities can remain unchanged.
Its current preparation helper explicitly selects manylinux, so changing only
the Docker FROM line is insufficient. Never reuse the existing glibc wheelhouse
as an Alpine input or weaken its TLS/hash checks.

Risks requiring explicit tests include musl versus glibc DNS/resolver and thread
behaviour, OpenSSL/CA trust and TLS behaviour, native PostgreSQL wheel dependencies,
NumPy/Pandas and other native API wheels, BusyBox account/file utilities, file
ownership, process shutdown and numerical behaviour near strategy thresholds.
Do not relax existing assertions, adjust strategy rules or add glibc shims to
make a candidate pass. Reject the candidate if frozen decisions or accounting
semantics change outside existing documented tolerances.

## Smallest bounded implementation proposal — not executed

**First approval scope: one local worker-only candidate and its tests.**

1. Preserve current source/image/archive identities. Use a separate owned source
   snapshot, wheel directory, local candidate tag and digest references. Do not
   overwrite existing contexts, tags or manifests.
2. Change only the worker base pin in `ops/shadow-worker.Dockerfile` to the
   reviewed same-patch Python 3.12 Alpine 3.24 identity below. Preserve code-only
   copies, USER 1000:1000, Python entrypoint and all runtime safeguards.
3. Extend `ops/prepare-shadow-worker-wheels.py` with an explicit reviewed
   musllinux selection alongside the existing glibc path. Preserve package versions,
   ambiguity rejection, limits, official PyPI origin and checksum verification.
   Record libc/platform/ABI in the separate input manifest. Add focused regressions
   for platform isolation and rejection of incompatible/missing wheels.
4. Build once from verified inputs without network; inspect native linkage and
   dependency consistency, scan the complete worker image and run the required
   local worker/migration/recovery tests. Start only owned disposable fixtures;
   no feed, orders, production credentials or production state.
5. Return the worker result for human review. Only after success and **further
   explicit approval**, evaluate the API: its reviewed Python 3.11 Alpine pin,
   equivalent BusyBox user/group creation preserving ownership, and a separate
   musl package context at unchanged lock versions. Update the API base-reference
   inventory in `ops/build-staging-release.py`, whose current regex recognizes
   `python:3.11-slim` but would omit the proposed Alpine reference. Do not silently lose
   base provenance. Keep the existing frontend build, trading code and settings.

No package removal from the current Debian images, OS-package sid backport,
Python-minor upgrade, dependency-version sweep, new worker framework or trading
feature is proposed. This is an alternative build experiment with a failure exit.

## Required tests and acceptance before any release decision

* Same frozen strategy/configuration/dataset inputs must produce equivalent
  decisions, episodes, costs and accounting under existing recorded tolerances.
  Include HTF alignment, lookback boundaries, future-data invariants and native
  numerical edge cases. Changed trading decisions block the candidate.
* All pinned native imports, ELF dependencies, pip check, binary psycopg,
  TLS verification and local API/worker startup/readiness must succeed.
* Run existing full backend/root regression, authentication, financial isolation,
  PostgreSQL migration/rerun/partial-failure/restore, lease/fencing/idempotency,
  cancellation and graceful restart tests. Include both actual candidate images'
  source/UID/entrypoint boundaries and existing browser smoke workflows.
* Verify immutable source/config/layer identities, dependency/public-artifact
  hashes, source metadata and SBOM against the full filesystem, including bundled
  wheel libraries. Ensure Alpine is recognized by the scanner and has active
  vendor coverage; a missing package database must not create an artificial pass.
* Both **complete application images** must pass unchanged Trivy vuln+secret
  HIGH/CRITICAL gates with zero HIGH/CRITICAL and no detected secrets. A clean
  base alone is insufficient. Record all exit codes, skips and limitations.
* Runtime users/capabilities, financial non-mutation, disabled execution/feed/
  shadow flags, storage boundaries and resource ceilings must remain intact.
  No staging readiness or deployment claim follows from local success.

None of these application-alternative tests ran in this proposal task. The prior
Debian baseline results remain 3,958 passed / 76 skipped / 84 warnings, focused
64 passed, and PostgreSQL 32 passed / 2 skipped; those are preserved prior results,
not proof of Alpine compatibility.

## Rollback and vendor-patch monitoring

Rollback means discard the alternative's local selection and retain the existing
reviewed Debian image/archive references; do not overwrite them or migrate real
databases. The trial uses isolated disposable state, so no financial rollback or
production service change is necessary. Cleanup must affect only owned trial
resources, with no pruning or removal of existing volumes. The preserved Debian
images remain **NO-GO** after rollback; rollback is not permission to release them.

If the trial fails or approval is withheld, keep publication/deployment blocked:

1. A named release/security owner checks the eight Debian tracker entries and
   official Python same-minor slim-trixie digests each working day and before any
   release. Record timestamp, source hashes/commit, vendor dispositions and diffs.
2. On a reported stable fix or new base digest, verify signed Debian 13 repository
   candidates and security advisories. No-dsa/postponed status or an unstable
   version alone does not count as a supported stable fix.
3. Prepare a new bounded pin/package proposal; preserve old artifacts. After its
   authorization, rebuild both complete images, refresh the scanner database,
   rescan with the unchanged gate and run the relevant regression/integrity tests.
4. Record the new CVE inventory and human release decision. No automatic exception,
   Git publication, GHCR publication, image rollout or VPS action follows.

## Immutable references and retained decision evidence

Reviewed alternative index digests, resolved to linux/amd64 for this assessment:

* Worker Python 3.12 Alpine 3.24:
  `sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4`.
  Inspected local config: `sha256:979e5a8a56846771598d47ebf1f8a75b46155104e168a0b237c0783c1dedff8a`.
* API Python 3.11 Alpine 3.24:
  `sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929`.
  Inspected local config: `sha256:607dc744833974b365cc338d565e01bb76eaebe25993fe328ce185170bcd8f05`.

Registry indexes and local config IDs are distinct identities. Retained Debian
application identities remain API
`sha256:f1e91d92d2bffd3f51e1cb56960537ce9b4d0aea26492ebb07051865976cb914`
and worker `sha256:f2b343acbd9ebfd57f224215a7f17e6734fc1938e26ad921e68f16da2325772e`.
Earlier reviewed images and archive checksums remain in the preceding reports.

New decision evidence is under `/workspace/nexus-audit/security-release-decision/`:
official Python tag definitions and registry indexes; current Debian CVE/repository
checks; Alpine release/security metadata and package recipes; five base runtime
probes/scans; and validated wheel metadata. Primary references include
[Alpine security data](https://secdb.alpinelinux.org/v3.24/main.json),
[official Python image definitions](https://github.com/docker-library/official-images/blob/master/library/python),
and [distroless maintenance policy](https://github.com/GoogleContainerTools/distroless/blob/main/SUPPORT_POLICY.md).

**Smallest safe next action:** separately approve the worker-only local evaluation
defined above. Until that action is authorized and its evidence reviewed, keep
the current release blocked and preserve the existing HIGH/CRITICAL gate.
