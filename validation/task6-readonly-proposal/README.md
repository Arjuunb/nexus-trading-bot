# Proposed Task 6 API acceptance boundary

Update: the user approved this proposal by replying **continue**. Current source
freeze handling and validation results are in
[`../task6-readonly-approved/README.md`](../task6-readonly-approved/README.md).
The original proposal and pre-approval results below are retained as history.

Status: **REQUIRES SOURCE REVIEW. NOT DEPLOYED. RELEASE FREEZE CHECK STILL FAILS.**

The existing research-worker rollout completed on the VPS with restart and
research-state verification. That receipt does not establish recovery of current
API financial data or authenticated PRD acceptance.

The earlier interrupted API patch was not found in the available checkout or
published branch. This is a reconstructed proposal against approved application
source `4eceb973e98e1350dd625153753637629453c401`. The proposal is layered on the
operator-evidence branch at `ea9b419cc52bf8428e27ef7728fff1a095ee0e1a`; application
sources are identical between those two commits.

## Behavior

`HUB_ACCEPTANCE_READ_ONLY=1` suppresses import-time timers and native-lab,
instance, grid, legacy-engine and shadow startup. Shutdown does not checkpoint
or stop saved sessions that this process never started. Saved running intent is
retained. Invalid flag values reject startup. The default is off.

Existing account, journal, legacy local ledger and native paper databases are
opened with SQLite `mode=ro` and `query_only=ON`. An authorizer also rejects
writes, schema changes, attaching databases and disabling query-only mode.
Committed WAL records remain visible. Missing files/accounts reject startup;
there is no financial seeding, schema repair, migration or reconciliation.
Separate research metadata keeps its existing connection policy.

The acceptance HTTP surface permits health/version, authentication, UI shells
and existing Strategy Intelligence reads. Execution endpoints and new research
submissions return 409. Legacy GET endpoints are restricted too because some
can initialize native runtimes. Supabase session and refresh routes retain
their existing authentication and CSRF checks. Health reports the active flag.
This mode is for inspection, not ordinary trading operation.

## Validation

Local targeted result: **215 passed, 4 skipped, 1 failed** across 17 modules.
The new focused module has 20 passing cases, including complete boot with all
legacy autostart flags enabled and saved PA/SMC, instance and grid intent.
It compares complete logical dumps of six financial databases and saved JSON
before and after startup, HTTP checks and shutdown. It covers both legacy auth
and Supabase auth with isolated provider responses, verifies anonymous/admin/
ordinary-user boundaries, and rejects writes and missing stores.

The remaining failure is
`test_authorized_source_delta_pins_all_unchanged_methods_and_native_strategy_rules`.
It correctly detects proposed constructor changes against approved method
hashes. The historical freeze manifest and test were not weakened or rewritten.
`source-review.json` records original/proposed file and callable hashes, and
confirms native strategy rules, MTF policy and migration files are unchanged.
`test-results.json` records the scoped result. No full repository pass is claimed.

## Before any API rollout

1. Review and explicitly approve the exact replacement source. Record the
   approved delta without changing historical evidence; rerun the release gate.
2. Build and scan images from that exact approved commit. Existing 4ece image
   scan results do not cover this proposal.
3. Prove backup and detached restore of **current** API financial state. The
   earlier preserved point-in-time SQLite snapshot and worker/Postgres recovery
   receipts do not close this gate.
4. Preserve the deployed worker overlay, 1536 MiB API memory override last,
   credentials, TLS, users and staging/production data boundaries. Verify the
   resulting runtime configuration before any container replacement.
5. Run authenticated PRD/abstention and restart/persistence acceptance on the
   existing staging target. Demonstrate unchanged production identities and
   financial records.

No API deployment command is supplied by this proposal. No new image was built,
no VPS operation was performed, and Task 6 remains incomplete.
