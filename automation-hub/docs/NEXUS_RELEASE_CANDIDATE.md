# NEXUS release candidate — Sprint 14

**LOCAL RELEASE CANDIDATE: VERIFIED. REMOTE AND PRODUCTION: NOT READY.**
No Git mutation, VPS access, remote transfer/deployment or Binance feed test is
authorized by this report. Artifact identity includes the complete dirty tree;
HEAD `7dd5e9ab8ad9e300d5d99e72a27bb8aeabc27ae0` on `work` alone is insufficient.

## Inventory and provenance

Initial state: 179 dirty paths, 177 regular-file hashes and two dependency links.
All 20 Sprint 13 contribution hashes matched before Sprint 14 edits. The prior
review `release-review:c20dce6c0b9542911125d21537ab354a387ee5f745c12c4c9a7fb486e98ba75f`
verified all 1,479 retained artifacts, zero mismatches. Earlier changes remain
owned by their prior contributors; they are not relabelled Sprint 14 work.

The trusted source freezer includes tracked and required untracked inputs,
locks, migrations and build definitions, excluding dotenv, databases, Git,
dependency links and generated caches. Verified public wheel/npm-cache manifests
plus pip hashes/npm SRI preserve package integrity. No secret values were read
or copied. No lock, strategy/risk/financial code or production Compose change.

The production Dockerfile uses pinned previously verified Node 20 and Python
3.11 manifest references. Builds execute with `--no-cache --network none`,
retained public inputs and complete source. Temporary npm trees are removed
inside the build step, and redundant full-image WORKDIR layers are avoided.
The code-only worker keeps Python 3.12 and its explicit source allowlist with a
pinned prior base. No credential or writable financial input is provided.
Runtime image/config identity and operator source-archive identity are recorded
separately. Final documentation/test evidence may postdate build-time source;
runtime source equality must be independently verified, not assumed.
The helper's legacy `image_digest` field is a **local Docker config ID**, not a
published registry manifest digest. Both built images target Linux amd64.
Transfer/load exact approved exports and verify config IDs, or separately
authorize publication and record real registry digests. Never append a config
ID to a registry repository and assume it names the registry manifest.

## Release gates (strict statuses)

Local gates below use actual final validation; target gates remain unverified.

| Gate | Meaning | Status / evidence |
| --- | --- | --- |
| G1 | Complete reviewable release | PASS — complete source/images/input/test inventories and retained review archive |
| G2 | Clean pinned Dockerfile build | PASS — no-cache, network-none build, retained packages, pinned bases |
| G3 | Python PostgreSQL driver | PASS — Python 3.11.17, binary psycopg 3.3.6, actual startup |
| G4 | Both frontend bundles | PASS — Node 20.20.2, 266 bundle hashes, actual HTTPS/Chromium smoke |
| G5 | Real PG metadata/claim/fencing | PASS — 33 real PostgreSQL cases in final root/Hub/database suites |
| G6 | Independent local supervision | PASS — standalone processes, SIGKILL/restart/new generations and graceful stop |
| G7 | Separate staging volumes | PASS — scoped fixtures; 194 preexisting volumes retained |
| G8 | Separate networks/private PG | PASS — private PG, scoped networks, loopback HTTPS; no production mounts |
| G9 | Secret exclusion/scoping | PASS — image checks and fixture separation; production bindings uninspected |
| G10 | Authentication/tenant isolation | PASS — real local auth and tenant regressions; customer Supabase UNKNOWN |
| G11 | Local backup/restore | PASS — 16 SQLite backups in new namespace; PG original metadata authority restored |
| G12 | Actual VPS capacity | UNKNOWN — no authorized target measurement |
| G13 | Actual VPS conflicts | UNKNOWN — no authorized inventory |
| G14 | Remote TLS/auth plan | PASS — separate reviewed design; remote implementation UNKNOWN |
| G15 | Current official Binance endpoints | PASS — current official public/market/Connect docs and schema retrieved over verified HTTPS; no feed test |
| G16 | Public-soak approval/status | BLOCKED — separate permission absent; NOT RUN |
| G17 | External monitoring owner | UNKNOWN — no assigned remote owner/integration |
| G18 | Remote rollback/restore | BLOCKED — prepared procedure, no authorized remote test |

Local PASS is not remote or production certification. Mandatory UNKNOWN/BLOCKED
gates prohibit a ready-for-VPS claim. Live Binance endpoint validation is
separate from transport fixture correctness and security/authentication tests.

## Final retained identities

API config ID: `sha256:2a06435876742ee690521ed21828d6ae093c92f0474834d71ccc68e7a7b531c3`.
Worker config ID: `sha256:6ef0a094f2aa599bc5411d7f151512df28f3974912b51996d90611c1216728a5`.
API export SHA-256: `1fc50ee9cc4b20e4d2b4ebcb856ec6ddc8d3bcb1def1291d379cbd11e2a4a50b`
(184,365,655 bytes). Worker export SHA-256:
`830bed3827079e6237c73afa2ec490aa50544cf8e4be54e670d18da87022c277`
(61,115,591 bytes).

API build source: `e7de53f84034262433699c91588f1b60f8915e82861cbd0fd02de6b73cd44dc1`.
Independent exported-layer verification matched all **1,628** frozen API inputs/
bundle files and **315** worker inputs, with every layer diff ID verified.
Worker code matches final API source despite its earlier context freeze; the
later helper/test/docs changes are outside its explicit allowlist. Final operator
documentation/results postdate construction. Their complete source archive and
separate identity are in the review manifest; no byte-identical rebuild claim.

Originals: `/workspace/nexus-audit/sprint14-local-validation/` (bytes retained
under `/tmp/nexus-retained-reviews`). `composite-release-manifest.json` records
the computed `release-review:` ID, exact source archive, exports, locks,
migrations, original reports and G1–G18. `artifact-integrity.json` enumerates
retained bytes. Temporary local storage is not durable off-host backup.

## Proposed commit plan — not executed

Human review must inventory the entire preexisting tree and decide ownership.
Review prior sprint implementation/tests/migrations as their own changes; then
review Sprint 14 packaging/helper/tests; then operator documentation/artifact
references. Verify included untracked files and excluded local state. Agree the
intended release branch and commit boundaries before any staging/commit/push.
Do not deploy from HEAD while the required implementation is uncommitted.
There is no automatic Git action in this plan.
