# Postgres / Supabase migrations

Forward-only SQL migrations for the **production multi-tenant identity layer**
(DSP Sprint 1). These target **Postgres/Supabase** — they are deliberately
separate from the local SQLite bot-store migrations in
[`database/migrations/`](../database/migrations), which keep the legacy
TEXT-id / REAL-money conventions and run only against SQLite.

## What's here

Sprint 8 routing audit: `0002_shadow_research.sql` is a legacy **SQLite shadow**
schema, not a tenant-isolated PostgreSQL migration. `pg_migrations.discover`
now explicitly excludes it; nested `shadow/*.sql` are also never discovered.
An installation that already applied it to PostgreSQL needs a separately
authorized isolation/data-preservation review. This change does not drop or
rewrite those tables. Local research/observation workers remain SQLite-only;
PostgreSQL identity or financial-outbox tests do not certify their claims.

The SQLite-only worker statement above describes the Sprint 8 routing audit.
Sprint 10 added an explicit PostgreSQL research/shadow metadata backend; Sprint
11 adds operational resilience tables. These use the separate operator migration
entrypoint in `services/postgres_workers.py`, not identity migration discovery.
See [NEXUS_DEPLOYMENT_READINESS.md](../docs/NEXUS_DEPLOYMENT_READINESS.md) for
the current database boundaries, migration order and deployment blockers.

| File | Creates |
|------|---------|
| `0001_identity.sql` | `tenants`, `users`, `profiles`, `sessions`, `user_settings` — uuid PKs, `timestamptz`, `jsonb`, FKs, and **RLS `ENABLE`+`FORCE`** with a tenant-isolation policy on every table. |

## How isolation works

After authenticating a request, the app sets a Postgres session variable:

```sql
SET app.current_tenant = '<tenant-uuid>';
```

Every RLS policy filters on `current_setting('app.current_tenant', true)`. When
the variable is unset the function returns `NULL` (the `true` = *missing_ok*),
so an unscoped connection sees **nothing** — default-deny, never an error.

### ⚠️ Supabase `service_role` bypasses RLS

The Supabase `service_role` key has `BYPASSRLS`, so RLS will **not** isolate
tenants on a connection using it. To get real isolation:

1. Connect on a role the policies apply to (not a `BYPASSRLS`/superuser role), **and**
2. `SET app.current_tenant` per request.

`FORCE ROW LEVEL SECURITY` additionally applies the policies to the table
*owner* (they are skipped by default for the owner). The `rls_coverage()` check
(below) guarantees both `ENABLE` and `FORCE` ship for every tenant table.

## Applying

- **Supabase SQL editor:** paste each file in order (once).
- **Programmatically:** `data.pg_migrations.apply(conn)` with a DB-API
  (psycopg) connection — forward-only, records applied versions in
  `schema_migrations`, idempotent.

## CI gate

`tests/test_pg_migrations.py` calls `data.pg_migrations.rls_coverage()` and
fails the build if any tenant-scoped table ships without `ENABLE`+`FORCE` and an
isolation policy — the standing mitigation for *"RLS misconfig leaks tenant
data"* (DSP risk R4). It runs statically, so it needs **no live Postgres**.
