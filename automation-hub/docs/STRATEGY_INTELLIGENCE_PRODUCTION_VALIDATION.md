# Sprint 1.5 isolated production-shaped validation

These checks run against temporary local SQLite databases and isolated local
PostgreSQL/PostgREST servers. They do not open a production connection, read
production secrets, change deployment settings, or deploy an application.
Representative values are test fixtures, not historical XRP evidence and not a
claim about production performance.

## Validation performed

`tests/test_evidence_production_validation.py` validates the following boundaries
through the actual journal, paper engine, scoped ledger, and evidence capture
interfaces:

| Boundary | Evidence checked |
| --- | --- |
| Early pre-evidence schema | Closed, open and cancelled rows, historical timeline and evolution data keep every original column value after upgrade. |
| Instance-aware pre-evidence schema | Existing instance/session/version columns survive; missing configuration, causal IDs and timestamps remain unknown. |
| Repeated migration | Reopening three times preserves both original and added column values. SQLite integrity and foreign keys pass. |
| Interrupted migration | A database authorizer interrupts creation partway through additive DDL. Reapplying the migration completes the schema and preserves all original data. |
| Primary outbox upgrade | A restored pre-outbox primary schema contains historical position, trade and execution rows. Three upgrades preserve every original value, add no invented receipt and pass integrity/foreign-key checks. |
| Old explicit SQL compatibility | An application using the previous named insert/update columns can still write and close an unfingerprinted row against the expanded schema. |
| Entry journal write outage | Timeline persistence fails inside the journal transaction after financial commit. The journal row rolls back; replay reconstructs all five entry events once. |
| Exit journal write outage | Closing timeline persistence fails. Journal close and evolution increment roll back together; replay closes once, with one evolution increment and eight total timeline events. |
| Actual process termination | A worker receives `SIGKILL` from the postcommit callback. A separate process opens the same stores and recovers the exact committed execution ID and timestamp. |
| Full evidence-store outage and partial-commit termination | The journal connection is closed before any order context is persisted. The existing accounting transaction commits OPEN and REDUCE plus their immutable outbox metadata. `SIGKILL` after REDUCE leaves no producer journal; restart recovers the parent/continuation as one open episode, and a subsequent normal close completes that episode once. |
| Prolonged full outage | Sixteen January accounting commits spanning four entries, two partial exits per entry and four final closures are reconstructed months later. Original configuration, initial risk, scope and commit clocks come from the immutable primary receipts and explicit parent references. Distinct episode economics reconcile individually and in total; retries do not change the evidence watermark. |
| Partial exits and reversal | Two reductions plus final closure produce one completed episode. A subsequent opposite entry produces a second episode; four financial legs remain four authoritative ledger rows. |
| Account and session isolation | A shared journal and primary ledger preserve separate account/owner/instance/session attribution during replay. |
| Financial preservation | Primary positions, trades, execution receipts and relevant webhook values are compared before and after replay; no financial values change. |
| Repeated delivery | Two explicit deliveries of every exit plus two reconciliation passes leave episode and journal results unchanged. |
| Original risk | Moving the stop after entry does not replace the captured initial risk used by the episode. |
| Consistent journal snapshot | One bounded transaction includes fills, episodes, legs, configuration, journal headers and timeline. A competing writer cannot become visible partway through the view. Stable watermarks exclude reconciliation-run bookkeeping. |
| Explicit snapshot overflow | Invalid bounds are rejected; an exceeded bound reports source counts and affected collections instead of silently certifying completeness. |
| Reconciliation-run persistence | Actual aware calculation timestamps and supported statuses are required. Reports are append-only, exact delivery retries have one logical effect, and conflicting payloads under the same run ID fail. The last COMPLETE run matches exact supplied scope/cohort. |

At the independent validation checkpoint, **29 tests passed, 0 failed and 0
skipped**. The process-termination test is skipped only on non-POSIX systems.
The local execution log is `/tmp/nexus-sprint15-production-validation-current.log`.
The final combined suite results are recorded in the Sprint 1.5 implementation
report, because subsequent integration work may extend these checks.

## Isolated PostgreSQL and RPC transport validation

The opt-in checks in `tests/test_paper_evidence_outbox.py` also exercise actual
PostgreSQL 16, PostgREST 12.2.12 and the installed `supabase-py` client. The latest
outbox-file checkpoint passed **26 tests**, including **10 isolated server and
gateway cases**. Log: `/tmp/nexus-sprint15-outbox.log`. The preceding nine-case
server/gateway checkpoint was extended by a second actual-gateway test for the
existing PostgreSQL `REAL` storage precision boundary.

The standalone PostgreSQL container publishes no port. Gateway checks use a
dedicated temporary Docker network, a random loopback-only HTTP port and a
generated short-lived local token. Containers, network and local proxy are
removed by fixture cleanup; no production credentials or endpoint are used.

| Boundary | Evidence checked |
| --- | --- |
| PostgreSQL migration | A historical row survives unchanged, no historical outbox receipt is fabricated, and the original OPEN/REDUCE/CLOSE accounting function bodies are retained verbatim under private names. Reapplying the migration is safe. |
| Atomic outbox failure | An injected outbox insert failure rolls back each complete OPEN, REDUCE and CLOSE accounting RPC. No position, trade or execution is partially committed. |
| IDs, costs and scope | Real RPC results preserve exact root, parent and continuation IDs; committed net P&L and fees reconcile with outbox receipts, and session-filtered snapshots exclude another session. |
| Permissions and immutability | Service-role RPC execution succeeds; anonymous access, direct private-function execution, metadata updates/deletes and duplicate execution fail without changing authoritative rows. |
| Complete scalar snapshots | A PostgreSQL snapshot returns 1,001 representative historical rows without an ordinary table-result row cap and preserves their unknown metadata. |
| Base RPC overwrite and forward recovery | Reapplying the older base RPC schema makes the capability check fail. Reapplying the outbox migration restores wrappers. Legacy fallback remains explicitly incomplete. |
| Actual client transport | The real `supabase-py` client executes OPEN, REDUCE and CLOSE through PostgREST. A legacy fallback returns all 1,003 readable rows across HTTP pages, while keeping consistency/completeness false. |
| Existing financial precision | Public fill results retain producer arithmetic. Detached evidence observers use the authoritative booked values when PostgreSQL `REAL` rounds them. Live journal replay reconciles net P&L and fees exactly, remains idempotent and leaves the primary snapshot unchanged. Unmodeled funding keeps the report `PARTIAL`. |

## Financial precision boundary

Completed episode net P&L and booked fee totals reconcile exactly with the
decimal representation of the authoritative persisted REAL values. This does
not restore precision lost in the existing float accounting implementation.

The full-outage fixture independently compares every reconstructed episode
with its constituent authoritative trade IDs, then compares aggregate net P&L
and fees with the primary store. Reconciliation reports require exact zero net
and fee deltas. Primary positions, trades, execution receipts and immutable
outbox rows remain value-for-value unchanged. Original stop risk survives a
later stop update. Unknown funding coverage deliberately keeps these recovered
paper results `PARTIAL`; successful delivery alone does not certify complete
profitability evidence.

Gross P&L is a captured receipt value, not a primary `paper_trades` column. Deriving
gross from separately persisted net and fees can differ in the final binary
floating-point bit. The representative fixture produces a difference of
`0.000000000000000011` between these two representations. The test bounds this
derived identity by the sum of the source values' binary ULP resolutions; it
does not use a cent-level tolerance or alter stored values. Net and fees still
must reconcile exactly. Funding remains explicitly unmodeled, so a reconciled
paper net total does not prove complete real-world cost coverage.

## Migration and rollback procedure

1. Stop instrumented producers before changing deployed evidence schemas.
2. Make consistent backups of the authoritative ledger, journal and decision
   databases. Include the primary outbox with its accounting receipts. For SQLite,
   use the backup API or another transactionally consistent snapshot; copying
   only a database file while a WAL writer is active is insufficient.
3. Apply the additive journal and applicable SQLite/PostgreSQL primary-outbox
   migrations to isolated restored copies first. Preserve
   original column values, run integrity/foreign-key checks, and reconcile the
   copy against its matching authoritative ledger export.
4. If an additive migration is interrupted, reapply the same migration. Its
   `IF NOT EXISTS` objects and checked column additions provide forward recovery.
   Do not delete evidence tables, immutable receipts or financial history to
   restart an upgrade.
5. Application rollback means stopping the instrumented producer and running
   the previous application against the compatible expanded schemas.
   Existing explicit column reads and writes remain compatible. Keep evidence
   tables and triggers. Previous producers cannot capture the new evidence
   guarantees, so any interval they produce must remain visibly incomplete or
   unknown until supported recovery proves it.
6. A physical database rollback is restoration of the consistent backup while
   writers are stopped. Later financial commits require reconciliation against
   the current authority; a backup restoration is not proof that later evidence
   was never needed.

The tests prove the named SQL compatibility and forward reapplication paths.
They do not certify every historical binary/application version or a production
database backup/restore operation.

## Limits before production deployment

The isolated fixtures exercise real SQLite transactions, process termination,
PostgreSQL migration/RPC execution, local permissions, HTTP client transport and
pagination beyond the ordinary PostgREST row cap. They do not certify a hosted
production Supabase project's RLS, gateway/runtime versions, timeout or response
size limits, backups, retention/reset rules, or a real restored production
database. Those require an explicitly authorized isolated staging copy with
the deployed schema, representative export and matching application
configuration. No production connection was requested or used in this sprint.

Existing paper and factory-reset operations still delete their original
operational position/trade rows while retaining committed execution and outbox
metadata. Recovery does not recreate financial rows after such a reset. If the
matching authoritative financial history was not preserved separately, affected
historical cohorts remain incomplete or conflicted. A production retention
procedure must preserve a consistent ledger/outbox/journal export before the
existing destructive reset path, and validate recovery against that export.
This sprint does not change reset semantics or certify production archives.

The XRP claim remains separate from these fixtures. Verifying it requires the
authoritative ledger execution/trade/position export and original immutable
configuration, decisions and evidence for the exact owner/account/instance,
session, execution mode and date range, with partial-exit lineage and cost
coverage. Local synthetic validation rows cannot satisfy that requirement.

## Sprint 2 staging handoff

Before an authorized staging deployment, run the additive journal/context tests
and the existing primary outbox validation from `automation-hub`:

```bash
../.venv/bin/python -m pytest -q \
  tests/test_journal_market_context.py \
  tests/test_market_context_classifier.py \
  tests/test_market_context_observer.py \
  tests/test_strategy_intelligence_v2.py \
  tests/test_strategy_intelligence_service.py
NEXUS_PG_OUTBOX_TEST=1 ../.venv/bin/python -m pytest -q \
  tests/test_evidence_production_validation.py \
  tests/test_paper_evidence_outbox.py
```

Record the restored schema identifier, migration start/end, row counts,
integrity/foreign-key results, reconciliation watermark, cache status and
rollback decision in the staging change record. Do not copy production secrets
into test fixtures. Preserve a consistent ledger, outbox and journal backup
before any reset or rollback exercise. The retention owner must specify how long
immutable context, classifier definitions and calculation runs are retained;
the application deliberately performs no automatic deletion.
