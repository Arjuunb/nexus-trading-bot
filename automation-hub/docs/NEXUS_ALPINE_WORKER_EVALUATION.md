# NEXUS authorized Alpine research-worker evaluation

**Worker candidate: GO for the bounded local evaluation and baseline-supported
research paths. Overall NEXUS release: NO-GO.** The complete Alpine worker has
zero HIGH/CRITICAL vulnerabilities and zero detected secrets. Tested strategy,
numerical and accounting outputs match Debian exactly, apart from two explicitly
measured processing-time fields. Existing dependency-support limitations remain.

**A separate API evaluation is justified, but is not authorized or started.**
This result does not approve deployment, publication, replacement of active
workers or production use. Assessment date: 14 October 2026, Europe/London.
Raw evidence timestamps are UTC.

## Authorization and preserved baseline

The user authorized one LOCAL-ONLY research-worker evaluation. The existing
Debian Dockerfile remains the default; the API Dockerfile, staging profiles,
strategy rules, dependency locks and financial evidence were not changed.

The directly compared Debian worker is
`sha256:f2b343acbd9ebfd57f224215a7f17e6734fc1938e26ad921e68f16da2325772e`.
Its existing source identity is
`bd744baa0421547ad9266ec152998e443480400312cf336fadc90454707b253b`.
Previous reviewed images/archives and reports remain available and unchanged.
Git HEAD remains `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0`.

Final preservation checks verified six reviewed image config IDs and three
existing archive checksums. All 194 pre-existing Docker volume names remain
present. Of 1,384 baseline source inventory entries, only the wheel helper
changed; the three added files are listed below. No baseline file was removed,
and `git diff --check` plus added-file whitespace checks passed.

## Exact Alpine candidate identity

| Item | Verified identity |
| --- | --- |
| Reviewed official base | python:3.12-alpine3.24@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 |
| Resolved platform | Linux amd64; Alpine 3.24.2; musl 1.2.6 |
| Python | 3.12.15 |
| Separate local tag | nexus-workers:sprint12-alpine-evaluation-v1 |
| Final local Docker config ID | sha256:d25a0cec6a427fc348f25e3467a22033c44274f94e8ae1bd459cacadfb28f636 |
| Frozen build-source SHA-256 | ff09b1a3ba01249683bd3b88d16735b090d341558198859de5e7c621dff5a149 |
| Exact Docker export SHA-256 | c051aa2e03dbc28c8e5c804d612ead755dd4529e53cffdb5f9f149e2ef8bb506 |

The base registry index, local config ID and export checksum are different
identities. No candidate registry publication/manifest digest exists. The build
source manifest contains 1,380 eligible inputs; this later report does not alter
that frozen source identity. Export verification checked the config and all six
uncompressed layers, with no private application inputs detected.

## Bounded build and dependency changes

Files changed for this evaluation:

* **Added** `ops/research-worker.alpine.Dockerfile`: separate opt-in candidate,
  reviewed base pin, curated code-only copies and existing research entrypoint.
* **Extended** `ops/prepare-shadow-worker-wheels.py`: explicit musllinux selection;
  the default manylinux path remains supported and unchanged in selection.
* **Added** `automation-hub/tests/test_worker_wheel_platforms.py`: 11 regressions
  covering platform/ABI rejection, mixed wheelhouses, ambiguity, integrity and
  preservation of conflicting existing bytes.
* **Added** this report.

The helper rejects unsupported platforms and mixed libc wheelhouses. Native
musl inputs must be CPython 3.12 / musllinux 1.2 / x86_64. Target ABI, libc,
architecture and actual wheel platform are recorded. Official PyPI HTTPS,
size limits, SHA-256 verification, require-hashes installation and ambiguity
rejection remain enforced. No TLS bypass or source-build fallback was used.

All **12 pinned dependencies** retain their versions: setuptools 80.10.2,
wheel 0.46.2, packaging 25.0, websockets 15.0.1, psycopg/psycopg-binary 3.3.6,
typing-extensions 4.16.0, requests 2.34.2, certifi 2026.6.17,
charset-normalizer 3.4.7, idna 3.18 and urllib3 2.8.0. Installed distribution
inventories also match for pip 25.0.1 and tradexa-trading-bot 0.3.0.

Only two wheel byte identities differ from the retained glibc inputs:

| Wheel | Verified SHA-256 |
| --- | --- |
| websockets-15.0.1-cp312-cp312-musllinux_1_2_x86_64.whl | 3be571a8b5afed347da347bfcf27ba12b069d9d7f42cb8c7028b5e98bbb12597 |
| psycopg_binary-3.3.6-cp312-cp312-musllinux_1_2_x86_64.whl | f21d057f3e5f5491067e5b292498073b73847d48799b099803fef100775fcc52 |

The other ten pure-Python wheel identities are unchanged. Inputs were prepared
in a new owned directory; no reviewed glibc wheelhouse was reused or overwritten.
One complete candidate was built with --no-cache and --network=none.

## Complete-image security result

**Trivy 0.69.3 completed with exit code 0:**

| HIGH | CRITICAL | Detected secrets |
| ---: | ---: | ---: |
| 0 | 0 | 0 |

The scan used the existing vuln+secret, HIGH+CRITICAL, exit-code-1 policy. Alpine
was recognized as 3.24.2. No ignore file/configuration, ignore-unfixed flag,
VEX exemption, severity reduction or scanner environment override was used.
The result applies to the selected severities and this database snapshot.

The current official database was downloaded for this evaluation, updated
14 October 07:11:52 UTC and downloaded 11:44:58 UTC. Database SHA-256:
`88daf0f4f0789698811bec5ebf95ef9610cd0f4d0571394f754a5cdb8cf081c9`.
Raw report SHA-256:
`c5f3b082dd94b6a88832ac3e7eaf410deedbc08b95c220ce22418b0c1e711f90`.

This is the complete research-worker image, including installed dependencies.
Validation-only pytest tooling and generated TLS/database fixtures were mounted
separately and were not added to the image. No claim of permanent freedom from
new vulnerabilities or lower-severity findings follows.

## Functional and financial isolation validation

Both images contain the same **315 runtime source files**, verified individually
against the frozen manifest. Both lack the API app and execution package. The
new default entrypoint is `services.research_worker_main`; the Debian baseline
was invoked through that same existing module for comparison. No strategy or
worker execution algorithm was modified.

Actual local PostgreSQL 16.15 checks passed with a generated fixture CA and
`sslmode=verify-full`: native psycopg binary imports/connectivity, encrypted
connections, invalid CA rejection, invalid hostname rejection and restricted
database-role checks. CA selection used the supported `PGSSLROOTCERT` variable;
the application DSN-option allowlist was preserved.

The native matrix additionally verified concurrent claims, duplicate submission,
owner isolation, connection-loss recovery, replacement-lease fencing, cancelled
result exclusion, exactly one logical fixture result, graceful SIGTERM and
restart with the same metadata authority. Operational spool storage persisted
in new owned bind directories. SQLite spool reopen/idempotency and its financial-
database rejection guard passed under each runtime's native SQLite library.

Fresh metadata migrations and reruns passed through both images. Injected
migration failures rolled back; clean reruns preserved authority and record
counts. A populated test-metadata backup restored into a separate disposable
database: both clients observed the same authority, two completed fixture jobs
and two validation runs. These records are explicitly test fixtures, not actual
market research evidence.

Financial sentinels were placed in a **separate disposable test database**.
The worker metadata guard rejected that database. Its synthetic balances,
positions, orders and journals remained unchanged before/after the workflow;
snapshot hash is
`4330174db789b15000985f5a0dd2b33f45f798b22b28fa1bbd5f3835541f9611`.
No production records, account objects, trading credentials or existing financial
volumes were supplied. Forbidden execution credentials were rejected before a
database connection. Worker readiness reported execution_runtime_loaded=false.

The running worker's limits were verified in kernel cgroup files: **512 MiB,
1 CPU and 128 PIDs**. UID was 1000, CapEff/CapBnd were zero and NoNewPrivs was 1.
The filesystem was read-only, the network was internal, no ports were published
and no Docker volumes were created. Trading, automatic research startup,
shadow supervision and public-feed flags remained disabled. This verifies the
local test boundary, not independently supervised VPS operation.

## Numerical and strategy equivalence

A single immutable **synthetic test input** was prepared once and supplied to
both images; inputs were not regenerated with each runtime's math library.
Input SHA-256:
`5e27e62483ccb768a91a310a1a70f3c03754da45aff13b18d11f9d8cb20758e4`.
It contains 600 chronological bars and 720 multi-timeframe context bars.

The comparison covers EMA/RSI/ATR/ADX across four periods; four adaptive
configuration/direction cases including genuine evaluator-produced long/short
fixture signals; six streamed built-in strategies; 600 PA and 600 native SMC
snapshots; three existing simulator presets; chronological partitions and
anchored/rolling windows; seeded Monte Carlo; unknown funding; portfolio
accounting/unknown marks; and 32 London/New York DST/session cases.

All **domain outputs match exactly**: values, decision states, signal directions,
rejection data, simulated outcomes, accounting values and unknown states.
No numerical tolerance or rounding was introduced to obtain equivalence.
Only `/portfolio/known/computed_ms` and `/portfolio/unknown_mark/computed_ms`
are excluded from domain equality: these are actual wall-clock timings measured
by the existing portfolio engine, retained separately in both raw outputs.
Domain-output SHA-256:
`df927250b297cbde858b10c0acaed4eb16262cd13061135291b94af1393842c8`.

These are synthetic functional tests, **not historical or forward profitability
evidence**. The generic SMC stream produced no signal in this particular input;
its positive-entry branch is not certified by that comparison. Other streams
and the adaptive long/short fixtures are non-neutral. No strategy is promoted,
qualified or declared profitable.

## Actual tests and scope limits

| Final executed suite | Passed | Failed/errors | Skipped | Warnings |
| --- | ---: | ---: | ---: | ---: |
| Full repository after implementation | 3,969 | 0 | 76 | 84 |
| Wheel-selection regressions | 11 | 0 | 0 | 0 |
| Release/prerequisite/wheel guard suite | 71 | 0 | 0 | 0 |
| Existing worker-compatible tests inside Debian image | 208 | 0 | 0 | 0 |
| Same unchanged tests inside Alpine image | 208 | 0 | 0 | 0 |
| Native TLS/PG/claims/cancellation/restart/spool matrix | 13 | 0 | 0 | 0 |
| Native migration rollback and populated restore | 3 | 0 | 0 | 0 |

Suites overlap; do not sum them. Full regression took 512.97 seconds. Its 84
warnings are the existing FastAPI on_event and aiohttp cleanup-option
deprecations; they were not suppressed. Existing default opt-in skips remain
visible. Native tools were checksum-verified read-only mounts, not new worker
dependencies; existing assertions were unchanged.

Earlier verification attempts and logs are retained. Harness corrections
addressed an external-probe import path, a forbidden sslrootcert DSN option,
financial sentinels initially placed in the forbidden metadata namespace,
the heartbeat API's False rejection contract, and a nested test-string escape.
Those corrections changed test setup, not application guards or strategy code.
The final scoped results above are the corrected completed runs.

One API-ledger-dependent qualification test cannot run in a code-only worker;
it passed in the complete repository suite. A broader simulator-parity module
also failed collection in the Debian worker because its Decision Brain import
requires **pandas**, absent from the existing 12-dependency worker contract.
The same unsupported Decision Brain import was verified in Alpine. That native
module attempt had one collection error and is **not counted as passed**.
No API dependencies were injected to disguise the limitation. This existing
support gap must remain explicit in any future worker strategy catalog.

## Runtime differences and remaining limitations

| Component | Debian baseline | Alpine candidate |
| --- | --- | --- |
| Python | 3.12.15 | 3.12.15 |
| Installed Python distribution versions | Baseline 14 distributions | Identical versions |
| C runtime | glibc | musl 1.2.6 |
| Native libpq | 18.6 | 18.6 |
| Python OpenSSL | 3.5.7 | 3.5.9 |
| SQLite | 3.46.1 | 3.53.4 |
| Default SSL CA count | 150 | 121 |

The native library and trust-store differences were recorded rather than
silently treated as identical. Local certificate/hostname validation and spool
compatibility passed. Actual provider/proxy certificate chains, external market
data, production data volumes, every timezone/date, every strategy configuration,
high-load behavior and staging restarts were not tested or authorized.
End-to-end jobs against authoritative retained market datasets were not run;
the commitment/recovery tests use clearly labelled fixtures, while the numerical
probe uses the real existing strategy/simulation code on synthetic input.

Independent supervision, target-VPS capacity, authorized staging migration tests
and production credential separation remain separate operational gates. No
production-readiness claim is made. Alpine main support and Python support
windows still require the maintenance/monitoring procedure in
[the security decision](NEXUS_SECURITY_RELEASE_DECISION.md).

## Evidence, rollback and next action

Evidence is retained under `/workspace/nexus-audit/alpine-worker-evaluation/`:
entry inventories; frozen build context/manifest; verified wheel manifests;
build/scan logs and exact image export; source/runtime/kernel proofs; raw
cross-runtime outputs and domain comparison; JUnit/logs; TLS fixtures and
metadata restore/isolation proofs. Raw logs/keys remain private local evidence,
excluded from the build and publication. Owned containers/networks were stopped
and removed; no existing volume, image or archive was deleted.

Rollback is simply to leave the default Debian build paths unchanged and stop
selecting the opt-in candidate. Existing reviewed images remain available but
retain their previously reported security blockers. No financial rollback or
production service action is required or authorized.

**Separate API evaluation: justified.** This trial provides evidence that the
same Python minor/patch on a maintained Alpine runtime can remove the worker's
Debian blocker while preserving tested behavior. The API has a larger native
dependency set and separate ownership/startup/TLS contracts; its compatibility
and complete-image scan must be independently evaluated after new authorization.
The API was not converted, rebuilt or rescanned here; its last verified scan
contained 44 HIGH findings. The overall release remains NO-GO.

Stop at this worker result. Any API conversion, publication, staging selection,
VPS access or deployment requires a separately authorized action.
