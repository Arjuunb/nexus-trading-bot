# NEXUS authorized Alpine API evaluation

**API candidate: GO for a separate human release review within the tested scope.**
The complete Python 3.11 Alpine API passes the unchanged security gate and all
six candidate-specific actual-server checks. This is a local compatibility
assessment, not publication, deployment or production approval.

**Overall release/deployment remains NO-GO pending separate review and existing
operational gates.** Both Alpine candidates can be presented for that review;
their default build paths have not been switched. The worker's previously
documented dependency/support limitations remain. No combined production
certification follows from these individual evaluations.

Assessment: 14 October 2026. Evidence timestamps are UTC. Authorized scope:
one bounded LOCAL-ONLY API evaluation, with disposable fixtures. No Git push,
GHCR publication, VPS connection, staging deployment or trading activation.

## Baseline and bounded changes

Read the [security decision](NEXUS_SECURITY_RELEASE_DECISION.md),
[worker evaluation](NEXUS_ALPINE_WORKER_EVALUATION.md) and
[Debian resolution](NEXUS_DEBIAN_VULNERABILITY_RESOLUTION.md).
The compared Debian API is
`sha256:f1e91d92d2bffd3f51e1cb56960537ce9b4d0aea26492ebb07051865976cb914`.
The retained Alpine worker is
`sha256:d25a0cec6a427fc348f25e3467a22033c44274f94e8ae1bd459cacadfb28f636`.

Git HEAD remains `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`.
All 1,387 baseline regular-file/symlink inventory entries were preserved.
Existing uncommitted work, Dockerfiles, strategy/risk rules, dependency locks,
migrations, workflows, reports, images and release archives were not rewritten.
All pre-existing Docker volumes remain present and were never mounted as fixtures.

Only two repository files were added:

* `ops/api.alpine.Dockerfile`: opt-in candidate; default `Dockerfile` unchanged.
* This evaluation report.

Evaluation scripts, manifests, scans, generated test credentials/certificates,
test databases and logs are retained outside the checkout under
`/workspace/nexus-audit/alpine-api-evaluation/`. Private fixture material is
excluded from image/release inputs. No dependency declarations or strategy
implementations were changed.

## Exact image and input identities

| Item | Verified identity |
| --- | --- |
| Reviewed official base index | python:3.11-alpine3.24@sha256:d9368b3a5ac59afea7b5d4f2e2aea0941dbf9fdee9c369c5bec00b98244bc929 |
| Inspected base config | sha256:607dc744833974b365cc338d565e01bb76eaebe25993fe328ce185170bcd8f05 |
| Platform/runtime | Linux amd64; Alpine 3.24.2; Python 3.11.17; musl |
| New local tag | nexus-api:sprint12-alpine-evaluation-v1 |
| Complete API local config ID | sha256:11e9509803bd2b41f594b3e5c6f85a16cdccc3d047cb990550c54674bc4429dd |
| Frozen build-source SHA-256 | 1ac6401d9ed158d0f0afbbda1e94f45154e18b4f30c7f9f4cca14f4cfb7d7e25 |
| Docker export SHA-256 | c1607ab225c75e40fcc6dde97a6d62bcabe889e1d5811a823123cffbefa7c93c |
| Unchanged API lock SHA-256 | 7ac510e8d1dd86f6c56c1f8f4e1fac491585bb39d3ba1a540e34d29dddc52d15 |
| Verified package-input manifest SHA-256 | 4cc6b387fd078e45f547c8c217c7a849a876b77eb92682eccf9754acb420d8e3 |

The frozen source contains 1,382 eligible inputs, including required untracked
source. This later report does not alter the frozen identity. The registry base
index, local image config and export checksum are distinct identities. No
published GHCR manifest digest exists. The export's config and all six
uncompressed layers passed the existing archive-integrity verifier, with no
private application inputs detected.

## Dependencies, frontend and runtime changes

All **71 pinned API dependency versions** are unchanged. A separate wheelhouse
contains 21 compatible CPython 3.11/abi3 musllinux wheels and 50 pure-Python wheels.
Each artifact was checked against official HTTPS PyPI metadata, its byte size,
SHA-256 and an already approved hash in `ops/api-requirements.lock`.
No manylinux wheel, source-build fallback, compatibility shim, edge repository,
TLS bypass or unverified package was introduced. The observed distribution
metadata maps are identical in both runtimes, including vendored metadata.

The existing Node 20 frontend stage, dependency locks, npm integrity checks and
both production build commands remain unchanged. npm used the retained verified
offline cache. Both API images contain **266 byte-identical frontend files**.
All **465 application/runtime source inputs** checked individually are identical
between Debian, Alpine and the reviewed checkout baseline.

The candidate uses BusyBox-compatible account creation with the measured Debian
API identity, **UID 100 / GID 101**, preserving the nonroot account. The first
attempt used an incorrect reserved ID and failed before package installation;
its frozen inputs/log remain retained. Only account-creation configuration was
corrected, followed by a fresh frozen source and successful no-cache build.
The complete build used `--network=none` and hash-required offline installation.

| Runtime property | Debian | Alpine |
| --- | --- | --- |
| Python | 3.11.17 | 3.11.17 |
| C runtime | glibc | musl |
| Python OpenSSL | 3.5.7 | 3.5.9 |
| SQLite | 3.46.1 | 3.53.4 |
| Binary psycopg libpq | 18.6 | 18.6 |
| Default system CA count | 150 | 121 |
| API UID/GID | 100/101 | 100/101 |

Native imports passed for NumPy, pandas, cryptography/Rust bindings, coincurve,
psycopg binary, pydantic-core, uvloop, httptools, aiohttp, jiter, orjson,
compression modules and the remaining inspected application imports. Different
CA inventories and native library/compiler builds are recorded, not assumed
equivalent for every possible provider or workload.

## Complete-image vulnerability result

**Trivy 0.69.3: exit 0; zero HIGH, zero CRITICAL, zero detected secrets.**

The scan covered the exported complete image with vuln+secret scanners,
HIGH+CRITICAL selection and exit-code 1. Alpine was recognized as 3.24.2.
No ignore file, severity reduction, ignore-unfixed option, VEX exception or
Trivy environment override was used. Test-only tooling and private fixtures
were mounted separately, not installed into the scanned image.

An additional secret-only scan across **all severities**, including LOW/MEDIUM
and UNKNOWN, also returned zero detections and exit 0. This extends secret
verification without reducing the primary HIGH/CRITICAL release policy.

The official database was downloaded 14 October 12:53:15 UTC; its UpdatedAt was
07:11:52 UTC. DB SHA-256:
`88daf0f4f0789698811bec5ebf95ef9610cd0f4d0571394f754a5cdb8cf081c9`.
Raw scan SHA-256:
`59e4aaf0ea96766d1ce28115813e17d24596f23f3937881b126ce7e653234531`.

The Debian API's last retained result remains 44 HIGH findings. No Debian
finding was suppressed or reclassified to approve this candidate. This scan is
bounded to its database snapshot and selected severities; it is not a guarantee
against future vulnerabilities or a scan of the build-stage toolchain.

## Functional, authentication and TLS validation

The same unchanged selected tests ran inside both actual images using verified,
read-only test tooling: **611 passed / 25 skipped in each**. Coverage includes
password/reset/verification/2FA flows, signed JWTs and session tampering, OAuth
contracts, Supabase principal/role boundaries, tenant authorization, API routes,
atomic ledger rollback, partial-exit lineage, unknown costs, persistence,
research/qualification safeguards, PA/SMC parity and historical-data integrity.

The actual servers used newly generated fixture credentials, isolated SQLite
state, restricted PostgreSQL metadata roles, internal API networking, no exchange
credentials and no active trading instances or shadow registrations. API root
filesystems were read-only, capabilities dropped and no-new-privileges enabled.
Kernel measurements verified **768 MiB, 1 CPU, 128 PIDs and UID/GID 100/101**.
Test runners used separately documented limits, not a claim that pytest runs
within the production API ceiling.

A separate local proxy exposed only an ephemeral 127.0.0.1 HTTPS port. Verified
fixture certificates, secure/HttpOnly cookies, anonymous 401 responses, login,
real public/dashboard assets and read-only readiness succeeded. Untrusted HTTPS
certificates were rejected. Actual `/usr/bin/chromium` smoke passed two scenarios
per image; trust was limited to the generated certificate SPKI, with no global
certificate bypass and external browser requests blocked.

Native PostgreSQL TLS used `sslmode=verify-full` and `PGSSLROOTCERT` without
changing the DSN-option allowlist. Invalid trust and hostname were rejected.
Restricted-role checks, migration/rerun, rollback, disconnect recovery, claims,
fencing, cancellation, immutable fixture commitment and populated metadata
backup/restore passed through both native clients. Existing local financial
RPC/outbox and migration regressions also passed against disposable PostgreSQL.
The restored metadata checks verified authority and committed fixture counts;
the existing restore suite additionally verifies retained dataset/result
references. None of these fixture results is actual market research evidence.

The existing SQLite backup/restore helper preserved generated application row
hashes and rejected corrupted fixture snapshots. Financial row hashes remained
unchanged through authentication, read-only reporting, browser use and restart.
Separate generated financial database sentinels also remained unchanged and
were rejected as worker metadata targets. No existing financial record or
Docker volume was supplied to a fixture.

## Shutdown finding and safety boundary

**Alpine passed all six final actual-server checks.** Its unchanged default
shell/uvicorn command produced PID-1 uvicorn, exited 0 on shutdown, restarted,
retained application state and again reported execution_authorization=false.

**Debian passed five; its default-command graceful-shutdown check failed.**
The retained shell remained PID 1 while uvicorn was a child. After the 15-second
stop timeout it exited **137**, with OOMKilled=false. Restart and financial row
consistency still passed. The zero-exit assertion was retained: this control
failure is visible in the final XML and was not turned into a passing test.
No Debian Dockerfile, image or shutdown implementation was changed here.

Trading/feed/automatic research/shadow flags were zero in the tested API.
No engine-start, trade, champion-designation or trading-setting action was
performed against an actual fixture server. The complete API still contains
its existing authorized trading routes and execution code; this is configuration,
credential, state and network isolation, **not** a capability-free API or a new
permanent execution lock. Never mount production state into this test profile.

## Numerical and strategy equivalence

Both images consumed the same retained synthetic input, SHA-256
`5e27e62483ccb768a91a310a1a70f3c03754da45aff13b18d11f9d8cb20758e4`.
EMA/RSI/ATR/ADX, adaptive long/short fixtures, streamed strategies, native PA/SMC
snapshots, existing simulator presets, chronological/walk-forward windows,
seeded Monte Carlo, unknown funding, portfolio accounting and DST/session
fixtures produced **exactly matching domain outputs**. Only the two actual
`computed_ms` processing-time fields were excluded; raw timings were retained.
Domain SHA-256:
`df927250b297cbde858b10c0acaed4eb16262cd13061135291b94af1393842c8`.

A separate NumPy/pandas/native-crypto fixture compared 134 floating values.
Two sine results differed by **1.1102230246251565e-16**. They passed the
predeclared analytic tolerances, relative 1e-12 / absolute 1e-14, which were
not changed after execution. Trading decisions/accounting and crypto outputs
remained exact; ECDSA verification/signatures and AES-GCM round-trip/output
identities matched. Native floating-point calculations are not universally
bit-identical. Threshold-sensitive untested configurations remain a limitation.

All data is explicitly synthetic functional-test input, not profitability
evidence. The generic SMC stream was neutral in this fixture; its positive-entry
branch is not certified by this probe. Existing parity assertions were preserved.

## Actual final test results

| Executed suite | Passed | Failed | Errors | Skipped | Warnings |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full repository after final candidate configuration | 3,969 | 0 | 0 | 76 | 86 |
| Selected tests in actual Debian API | 611 | 0 | 0 | 25 | 5 |
| Same tests in actual Alpine API | 611 | 0 | 0 | 25 | 5 |
| Native API-image PG/TLS/recovery/migration/restore matrix | 16 | 0 | 0 | 0 | 0 |
| Existing PostgreSQL/financial outbox/migration/recovery suite | 65 | 0 | 0 | 3 | 4 |
| Final actual-server matrix, both images | 11 | 1 | 0 | 0 | 0 |
| Actual-server Alpine subset | 6 | 0 | 0 | 0 | 0 |
| Actual-server Debian subset | 5 | 1 | 0 | 0 | 0 |
| Chromium scenarios, both actual images | 4 | 0 | 0 | 0 | Not separately counted |

Suites/subsets/scenarios overlap; do not sum them. Full regression took
432.91 seconds; native selected runs took 34.60/43.65 seconds; native PG matrix
374.83 seconds; existing PG regressions 88.06 seconds; final server matrix
156.32 seconds. The original mocked dashboard browser suite was not rerun;
the actual-image HTTPS smoke and identical built assets provide this trial's
frontend evidence.

The 86 root warnings are four existing FastAPI on_event deprecations and 82
existing aiohttp cleanup-option deprecations. Compared with the prior count of
84, frequency changed but no new warning source appeared. Each native selected
run has four FastAPI warnings and one pytest-cache warning from its read-only
checkout; application state uses the separate writable fixture. PG warnings
are existing Supabase/PostgREST client API deprecations. None was globally hidden.

The 25 native skips comprise ten Docker-dependent PG/PostgREST checks and fifteen
existing conditional parity scenarios. The former were exercised separately
in the local PG suite. That suite's three opt-in initializer/supervision checks
remain skipped; its container targets are older profiles. The 76 default root
skips remain reported, not silently claimed as executed.

Earlier attempts are retained: account-ID build failure; external probe file
permissions; an inventory expectation for excluded logs/.gitkeep; internal-network
port publication; wrong fixture backup-marker format; and an invalid fixture
AES key size. Corrections affected candidate account configuration or external
test setup, not application rules, guards or dependency pins. The initial
server attempt had 12 setup errors; the next had 9 passes/3 failures; the final
has 11 passes/1 genuine Debian control failure, as recorded above.

## Remaining limits and release-review decision

**GO: present the immutable API candidate and retained Alpine worker for a
separately approved release review.** The HIGH/CRITICAL/secret gate is enforced,
not waived. Do not change default build/Compose selections or publish artifacts
as a side effect of this report.

Review must retain these limitations:

* The worker's pre-existing Decision Brain/pandas support gap remains; no worker
  dependency or supported-strategy expansion was authorized here.
* The combined two-image release packet/profile has not received human acceptance.
  Workflows/default builds still select the preserved Debian paths.
* Actual provider/Supabase/JWKS/OAuth/TLS chains, production accounts, production
  peaks, every numerical boundary/configuration and authoritative market datasets
  were not tested. System CA changes require target-chain verification.
* The Debian control's shutdown defect remains. Retained vulnerable Debian images
  are evidence/rollback references, not automatically approved release fallbacks.
* Security refresh, Python 3.11 support through October 2027 and Alpine-main
  maintenance must follow the existing monitoring procedure. The retained Node
  build-stage toolchain was not upgraded or certified by the final-image scan.
* Target-VPS capacity, staging credentials/TLS/ownership, independent supervision,
  monitoring, backup/recovery ownership, authorized staging migration/restart
  drills, release acceptance and deployment authorization remain separate gates.

Rollback requires no service action: keep default Debian build paths selected;
the candidate is an additional local image/export. No active system was replaced.
All evaluated server/proxy/database containers and networks are owned local
fixtures and are removed after validation; pre-existing volumes/images/archives
are preserved. No staging or production readiness claim is made.

**Smallest next action:** a separate human review of the two exact candidate
identities, frozen inputs, scan evidence, supported worker scope, numeric/CA
differences and the visible control failure. Publication, any release integration
or staging deployment needs its own explicit authorization. Stop at this local
API evaluation.
