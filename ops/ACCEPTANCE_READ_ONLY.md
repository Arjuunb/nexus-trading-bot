# Strategy Intelligence acceptance without paper execution

`NEXUS_ACCEPTANCE_READ_ONLY=1` is an operator setting for staging acceptance.
It defaults to `0`; only `0`, `1`, and an empty value are accepted. It is not
an HTTP setting or an authentication mechanism.

This mode retains saved execution intent without restoring the engine,
instances, grid, PA, SMC, shadow observations, live feeds, watchdog, monitor,
or daily execution tasks. It skips account seeding, financial reconciliation,
trade-memory backfill and runtime override loading. Saved override files stay
intact. Shutdown does not flush finances or rewrite desired execution state.

Local financial stores open with SQLite `mode=ro` and `query_only=ON`, reading
committed WAL state. Missing databases, accounts, required columns and ownership
or revision fields fail closed. Existing evidence owner checks still apply.
The financial Supabase client allows table reads and the two existing SQL STABLE
evidence-reader RPCs, and denies mutation builders and unknown RPCs. An
unavailable configured primary fails startup instead of creating a fallback.

The request policy permits existing authentication, dashboard assets, Strategy
Intelligence reads and explicit research metadata operations. Financial controls,
shadow activation, live-data routes, unknown mutations and WebSockets are blocked
before handlers. Authentication, authorization and tenant filters still apply.
Research metadata and offline historical jobs can run; this does not authorize
paper execution or automatic strategy promotion.

## Deployment and recovery boundary

This change requires separate approval of its exact source and newly built,
scanned image. Setting the flag cannot make a previous image safe. Local tests
do not establish actual VPS acceptance or replace current financial backups.

An older API's shutdown hooks may change financial records and desired state.
Verify the existing process before stopping it. Prepare a staging-only cutover
and safe recovery procedure; do not assume normal Compose replacement or
rollback to an old image preserves the no-new-execution boundary. Preserve
customer workspace configuration, all existing volumes, the current worker
overlay and the final 1536 MiB API memory/swap override.

The original approved `pr6_real_paper_freeze.json` remains unchanged. The separate
`task6_acceptance_read_only_delta.json` records proposed successor hashes only
for the explicit startup/access methods, while retaining all other method and
strategy hashes. It is not operator deployment approval.

## Verification

`test_acceptance*.py` covers synthetic existing books, saved sessions, incomplete
and missing evidence, ownership, committed WAL preservation, remote mutation
denial, HTTP restrictions, normal behavior with the flag off and PRD abstention.
The real app boot test forbids outbound connections with legacy autostart flags
enabled, and checks authenticated HTML when a built dashboard is bundled.

Task 6 still requires actual deployed image identities, authenticated staging
dashboard/API results, complete current financial recovery, worker/PostgreSQL,
restart/resource checks and before/after production isolation evidence.
