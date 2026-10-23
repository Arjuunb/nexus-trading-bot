# Sprint 14 — release finalization and controlled staging preparation

## Executive status

**SPRINT 14: PARTIAL. LOCAL RELEASE CANDIDATE/STAGING: VERIFIED.** Remote staging
and production remain NOT READY. VPS inventory, public-feed soak and remote
backup/restore remain external gates; official endpoint documentation is verified.
No staging/production access, Git mutation, deployment, order or paper activation.

## Audit and corrections

Read Sprint 11/12/13, readiness/runbooks/remote plan/market reliability and actual
Dockerfiles, Compose, rehearsal, deployment script, source freezer, metadata
authorities, feed/journal/provider and test code. Prior review verified 1,479
files and all 20 contribution hashes. Starting dirty tree has 179 paths; no
unrelated contribution was discarded or overwritten. Dependency links retained.

The remaining clean-build blocker was VFS physical disk amplification from
frontend dependency and backend layers. Packaging now uses read-only source and
public-package mounts, one temporary frontend stage, pinned prior base manifests,
hash-locked Python 3.11/Node 20 and code-only Python 3.12 worker. The existing build
helper adds optional `--no-cache`; existing image namespace/API remain compatible.
No new dependency or database migration. Runtime entrypoint, user, strategies,
financial rules, auth, production Compose and execution restrictions retained.

Two actual image checks first failed because Sprint 14 images did not exist;
the first clean candidate then passed both. Verified intermediate image exports
were retained before packaging's redundant WORKDIR copies were eliminated.
Only individually inventoried, owned, unshared build cache is reclaimed; no
volume, unknown image or original release archive is deleted.

## Market-data certification

Actual `price_action_stream.py` routes are
`wss://fstream.binance.com/market/stream?streams=...` for kline/optional markPrice
and `wss://fstream.binance.com/public/stream?streams=...@bookTicker` for quotes.
Symbols are lowercase combined streams. The URL-selected subscription has no
SUBSCRIBE acknowledgement path; do not claim one was verified. The library
uses ping/timeout 20 seconds, close timeout 5, queue 2,048, bounded exponential
reconnect jitter and existing closed-candle/event identity checks.

Initial retrieval over verified HTTPS failed with proxy 403. The existing
allowlist lacked `developers.binance.com`; the additive draft preserves all
prior domains and adds this documentation host. Subsequent current-runtime
HTTPS retrieval succeeded (200), including the routed public/market docs,
Connect page and versioned schema. The draft remains unpublished; successful
access is evidence for this instance, not publication/new-instance validation.

**Official endpoint structure: VERIFIED. No transport correction required.**
Official docs confirm query-mode combined envelopes, lowercase symbols,
`/market/stream` for kline/markPrice and `/public/stream` for bookTicker; 24-hour
connections; server ping every 3 minutes and pong deadline 10 minutes; 10
incoming messages/second and 1,024 streams/connection. Existing library-managed
pong and bounded reconnect are retained. URL-selected subscriptions do not
send SUBSCRIBE control messages, so control acknowledgements are not applicable
to this client. Neither a handshake nor actual frame delivery was tested live.
Current docs mention merged UM/CM universes for some all-symbol streams; Nexus
uses explicitly matched single USDⓈ-M symbols and does not change market scope.

Official sources retrieved on 2026-10-13, with original bytes/UTC timestamps/SHA-256
in the review archive:

* [Public streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public)
* [Market streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)
* [Connection rules](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)
* [Versioned schema](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/1.0.0/schema.yaml)

Existing deterministic stream-URL/closed-candle tests are retained and executed
in the full suites. **Public soak: NOT RUN**; separate network authorization is
required. Its existing helper's limited coverage is documented in the staging plan.

## Deliverables and ownership

Extended existing files: `Dockerfile`, `ops/shadow-worker.Dockerfile`,
`ops/build-staging-release.py`, `ops/rehearse-staging.py` and
`automation-hub/tests/test_sprint13_staging_integration.py` (all already dirty).
New test: `automation-hub/tests/test_sprint14_release.py`.
New docs: this report, `NEXUS_RELEASE_CANDIDATE.md`, `NEXUS_VPS_PREFLIGHT.md`,
`NEXUS_CONTROLLED_STAGING_DEPLOYMENT.md`, `ADAPTIVE_MTF_XRP_RESEARCH_PLAN.md`.
Migrations: **NONE**. Locks/configuration/strategy/financial sources: unchanged.

The release gate matrix and human-reviewed commit plan are in
[NEXUS_RELEASE_CANDIDATE.md](NEXUS_RELEASE_CANDIDATE.md). Read-only proposed VPS
commands and incremental budgets are in [NEXUS_VPS_PREFLIGHT.md](NEXUS_VPS_PREFLIGHT.md).
Separate credential/network/volume/authority and rollback/startup order are in
[NEXUS_CONTROLLED_STAGING_DEPLOYMENT.md](NEXUS_CONTROLLED_STAGING_DEPLOYMENT.md).
XRP research is prepared separately; reported profitability remains UNVERIFIED.

## Validation and limitations

Actual initial focused baseline: **86 passed, 0 failed, 0 skipped, 0 warnings**.
Actual first-candidate image checks: **2 passed, 0 failed, 1 deselected**.
Final suites ran after the last code change; only reporting/source sealing
followed. Intermediate failures are retained separately from final regressions.

The compact candidate's actual HTTPS/auth/PG/recovery/financial-isolation test
passed in 114.07 seconds after resolving host storage. Earlier runs reached
ENOSPC; no assertion was removed. Runtime startup is now sequential in the local
helper. The verified Sprint 12/13 archives were rehomed to `/tmp/nexus-retained-reviews`
with original-path links and complete byte-for-byte inventories. Only the unused
Sprint 13 API image was removed after verifying its archive checksum and config
ID, plus its one newly unshared cache record. All 194 preexisting volumes and
unknown images remain intact. The Sprint 13 regression supports explicit
`NEXUS_STAGING_TEST_API_IMAGE` / `NEXUS_STAGING_TEST_WORKER_IMAGE` overrides;
defaults/assertions remain unchanged. Final runs select the current Sprint 14
candidate, and do not claim the archived Sprint 13 image was freshly retested.

## Exact final validation

| Check | Passed | Failed | Skipped | Warnings | Reported duration |
| --- | ---: | ---: | ---: | ---: | --- |
| Focused release/readiness/recovery/API | 89 | 0 | 0 | 0 | 149.04 s |
| Repository root | 3,953 | 0 | 15 | 90 | 938.76 s |
| Automation Hub | 3,423 | 0 | 15 | 89 | 904.03 s |
| Database/recovery, 43 files | 587 | 0 | 0 | 4 | 523.35 s |
| Chromium regressions | 154 | 0 | 0 | 4 | 6.1 min (runner rounded) |

Suites overlap; do not sum pass counts. The 33 real PostgreSQL claim/lease/
fencing/cancellation/cutover cases are included in root/Hub/database results
(24 `test_postgres_workers`, 6 Sprint 11, 3 Sprint 13), not another independent
run. No PG case skipped. Full-suite skips are unchanged: 8 "not a validity
scenario", 7 "only comparable when both approve".

Dashboard typecheck/build **PASS**; build 8.12 s, one existing Vite chunk-size
advisory plus npm upgrade notices. No dependency update. Actual Python 3.11
candidate HTTPS/auth/PG/financial recovery **PASS**, including two production
bundle browser scenarios. The 154 browser regressions use fixture API responses
and are reported separately. `git diff --check` **PASS**. Original logs/JUnits,
build configuration/inputs and intermediate failures remain available.

Warnings: root/Hub FastAPI lifespan deprecations 4/4; aiohttp ignored
`enable_cleanup_closed` on Python 3.12.14, 82/81; Supabase timeout/verify
deprecations 4/4 (also database's 4). Browser NO_COLOR/FORCE_COLOR: 4. No new
category or unclosed-resource warning. Connector-instantiation counts vary in
unchanged instance tests; root adds two instances of the same ignored-flag
warning compared with Sprint 13. No warnings suppressed. Build root-pip notices
do not describe runtime ownership: API is tradexa and worker non-root.

## Integrity, recovery and measurements

Final API: Python 3.11.17, Node 20.20.2, binary psycopg 3.3.6, both SPAs and 22
discovered migration hashes. No new migrations. Exact image/export/build-source
IDs are in [NEXUS_RELEASE_CANDIDATE.md](NEXUS_RELEASE_CANDIDATE.md). Independent
archived-layer verification matched 1,628 API frozen source/bundle files, 315
worker inputs and every layer diff ID. Private dotenv/database inputs excluded;
worker app/execution entrypoints absent.

Unchanged hashes of 26 fixture financial tables after read-only flows, both
worker failures, PG restart/restore and API restart. All original financial/
strategy source hashes unchanged. This proves local fixture non-mutation,
not production evidence completeness or XRP returns. Sixteen consistent SQLite
authority backups restore into a new namespace; PG restore keeps the original
metadata authority. No production financial records were accessed.

Latest database-suite rehearsal:

| Measurement | Seconds | Scope |
| --- | ---: | --- |
| Research SIGKILL to ready/new generation | 6.490 | Local fixture worker |
| Shadow SIGKILL to ready/new generation | 7.216 | Zero registrations; no feed |
| PG restart to original authority | 3.382 | Local metadata |
| Consistent PG dump | 0.202 | Tiny fixture |
| PG restore into different database | 0.754 | Original authority verified |
| API restart to HTTP health | 18.743 | Local fixture |

171 resource snapshots: peak summed owned-container memory about 249.4 MiB;
minimum sampled free root space 442.0 MiB and 991,019 inodes. Samples exclude
non-container host processes; these are not continuous peaks or VPS capacity.
Earlier ENOSPC proves transient clone headroom matters. Remote RPO/RTO UNKNOWN.

## Final inventory and review

185 dirty paths: 179 prior plus 6 new. Five prior files extended (listed above),
172 prior regular-file hashes and both dependency links preserved. HEAD/branch/
index unchanged; no stage/commit/push. No production Compose, deploy script,
lock, strategy/risk rule, financial write or migration change. All 194 prior
Docker volumes remain; disposable test containers removed.

Complete final operator source, runtime build source, hashed public inputs,
actual exports/layer/source proofs, original reports, official documentation,
redacted rehearsal/restore records and ownership diff are retained at
`/workspace/nexus-audit/sprint14-local-validation/`. Its composite manifest records
separate build/final-source identities and computed `release-review:` ID; the
artifact manifest enables independent byte verification. Final report updates
postdate image construction; runtime source remains tested source.
Reusable startup instructions and the documentation-domain addition are saved
in the environment draft, **requires publication**, preserving prior settings.

## Status and next authorization

IMPLEMENTED / VERIFIED locally: full pinned clean release, drivers/bundles,
source/input/layer integrity, authenticated isolated startup, PG concurrency/
fencing, local supervision/recovery, migration/restore compatibility, financial
non-mutation, official endpoint configuration audit and all final suites.

PARTIAL: production operational readiness/credential review, off-host retention,
sustained market-data certification. BLOCKED/UNKNOWN: actual VPS inventory/
capacity/conflicts, customer TLS/auth, monitoring owner, authorized public soak,
representative remote restore/restart/migrations. NOT RUN: VPS access/transfer/
deployment, public feed soak, production backup, remote migrations, paper/live
activation and XRP research. XRP remains UNVERIFIED. No promotion/trading change.

The smallest next step is separately authorized read-only inventory on a named
VPS using the prepared preflight, with no transfers or service changes. Then
review target capacity, isolated bindings/ports, monitoring and rollback before
separate deployment approval. Public-only soak authorization is separate.

Remaining remote blockers: authorized read-only VPS inventory/capacity/conflicts,
separately authorized live public soak,
customer identity/TLS, external monitoring owner, encrypted off-host backup,
target-shaped migrations and remote restart/recovery/capacity assessment.
Local PG, fixture security and zero-registration workers do not prove these.
No full production readiness or historical XRP verification claim is made.
