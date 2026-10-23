# Controlled staging deployment preparation — Sprint 14

**PREPARED; NOT RUN REMOTELY. REMOTE STAGING AND PRODUCTION: NOT READY.**
This procedure requires separate authorization for target access, transfer,
resource creation, migrations and startup. Public Binance tests are a distinct
approval. Do not run root `scripts/deploy.sh` or merge production Compose.

## Review before approval

1. Review the complete source archive, dirty-tree ownership, lock/input hashes,
   immutable API/worker IDs, PG/ingress digests and original test reports.
2. Complete the [read-only VPS preflight](NEXUS_VPS_PREFLIGHT.md), incremental
   capacity assessment and conflict review. Assign monitoring/incident owners.
3. Choose a unique project, separate volumes/network and unused loopback port.
   Set both `NEXUS_REMOTE_STAGING_PROJECT` and `NEXUS_STAGING_PROJECT` to the same
   reviewed `nexus-remote-stage-*` name. Never reuse an existing authority.
4. Supply separate staging Supabase identity/session/control/PG bindings through
   the approved secret mechanism. No production dotenv or exchange credentials.
   Review separate TLS files and customer login/RBAC; local fixture auth is not
   customer-identity certification. Emergency admin must remain disabled remotely.
5. Approve backup destination, encryption/access, retention, restore procedure,
   maintenance window and rollback owner. Actual remote RPO/RTO remain UNKNOWN.

## Exact proposed order after authorization

Use only `ops/staging.remote.compose.yaml`, its existing isolated definitions and
`ops/staging.remote.nginx.conf`. Pin every image by reviewed digest/config ID;
record the resolved private configuration in a restricted operator directory.
Validate with the existing `validate_remote_profile` before starting resources.
Never publish its credentials in reports. Shadow profile and feed producer stay
off; all execution/autostart flags remain zero. PostgreSQL has no host port.

1. Transfer only approved images, source/manifest and scoped configuration over
   an authorized channel. Reverify transferred hashes and loaded image config
   IDs before use. A mutable tag is not release identity.
2. Start only the new metadata PostgreSQL service and wait for its health.
3. Run the one-shot state initializer, then the metadata initializer, sequentially
   and remove each completed **owned** initializer container. Install worker
   metadata migrations 0001 then 0002; validate original metadata marker and
   restricted runtime role. Do not execute SQLite financial DDL on PG.
4. Start API, research worker and separate loopback ingress with `--no-deps`
   after successful initialization. Validate build/source IDs, both bundles,
   trusted TLS, actual customer sessions/RBAC, common PG authority and readiness.
5. Verify worker health, heartbeat, leases, bounded restarts, cancellation and
   immutable logical result commitment using only approved isolated fixtures.
6. Verify original financial authorities/volume mounts through metadata and
   approved read-only integrity procedures. No financial import or state reset.
7. Back up and restore the approved metadata/artifact set into a **different**
   isolated namespace; verify schema/authority/references/hashes. Preserve the
   original source and failure history. Obtain human go/no-go sign-off.

API root health is liveness only. Require authenticated diagnostics and worker
database verification. The remote ingress only allows authentication and
read-only Strategy Intelligence diagnostics; trading/research mutation paths
remain outside the allowlist. No observation/paper activation follows startup.

After all approval/preflight/transfer/configuration checks, the operator can
use this sequence from the approved source root. Variables must come from
reviewed operator setup; no credentials are printed. **NOT RUN ON A VPS.**

```sh
staging_compose() {
  docker compose --env-file "$REVIEWED_STAGING_BINDINGS" \
    -p "$NEXUS_REMOTE_STAGING_PROJECT" -f ops/staging.remote.compose.yaml "$@"
}
staging_compose config --quiet
# Resolve to protected operator-only JSON and validate using the existing
# services.remote_staging.validate_remote_profile before resource creation.
staging_compose up -d --wait --wait-timeout 100 postgres
staging_compose run --rm --no-deps state-init
staging_compose run --rm --no-deps metadata-init
staging_compose up --no-deps -d --wait --wait-timeout 100 api
staging_compose up --no-deps -d --wait --wait-timeout 100 research-worker
staging_compose up --no-deps -d --wait --wait-timeout 100 ingress
staging_compose ps
```

Record exit statuses and authenticated/TLS/authority/health checks described
above. No shadow profile, feed producer or automatic research submission. Local
zero-registration observer tests do not authorize enabling a remote observer.

## Failure and rollback

On any failed gate stop submissions, capture redacted diagnostics and quiesce
only the approved staging project. Preserve volumes, attempts, fencing tokens,
spools, dataset bytes, holdout history and committed IDs. Never silently fall
back to SQLite or another dataset. Restore into a new DB and compare original
authority, schema and row/reference hashes before reviewer acceptance.

Application rollback retains additive evidence tables and compatible images.
Dropping added tables loses evidence and is not rollback. Production financial
state and services are never rollback targets for this separate project.
Interrupted `.partial` recovery bundles are not completed backups. Cleanup is
limited to individually identified, approved staging resources; no broad prune.

## Public-feed approval, separate from deployment

Configured endpoint structure is verified against current official Binance
public/market/Connect documentation and its downloaded schema. No transport
correction is required. This is documentation verification, not a live feed test.
The existing finite helper defaults to NOT_RUN and does not authorize itself:
`ops/public-feed-distribution-soak.py`. Proposed tests cover XRP/BTC/ETH USDⓈ-M,
1m/5m/15m/1h opportunities, finalized candles, continuity, duplicate/conflicting
data, gaps, reconnects, REST-origin labels, HTF, cursors, independent processes,
disconnect and limits. Current helper exercises 5m/15m/1h/4h and two readers in
one process for at most 300 seconds; it cannot certify the whole proposed soak.
Plan the missing 1m/independent-process/sustained coverage explicitly rather
than claiming it occurred. No live soak or credential use occurred in Sprint 14.
