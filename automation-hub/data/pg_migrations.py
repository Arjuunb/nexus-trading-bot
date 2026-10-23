"""Postgres/Supabase migration discovery + RLS-coverage lint.

These migrations (../migrations/*.sql) create the multi-tenant identity
foundation — tenants / users / profiles / sessions / user_settings — with
Row-Level Security ENABLE+FORCE. They target Postgres/Supabase, NOT the local
SQLite ledger (which keeps its own forward-only runner in database/store.py).

Nothing here runs at import and the app's runtime path is unchanged. The value
in CI is ``rls_coverage()``: it fails the build if any tenant-scoped table ships
without RLS ENABLE+FORCE and an isolation policy — the standing mitigation for
"RLS misconfig leaks tenant data" (DSP risk R4). It is a STATIC check over the
SQL, so it needs no live Postgres.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")

# Tenant-scoped tables that MUST carry RLS. ``tenants`` is scoped by its own id;
# the rest by ``tenant_id``.
TENANT_TABLES: tuple[str, ...] = ("tenants", "users", "profiles", "sessions", "user_settings")
# Legacy shadow DDL is applied by ShadowResearchStore to its isolated SQLite
# file. Discovery previously routed this unscoped schema into PostgreSQL.
# Preserve the file/version for old local consumers; never apply it here.
SQLITE_ONLY_MIGRATIONS = {"0002_shadow_research.sql"}


@dataclass(frozen=True)
class Migration:
    version: str   # filename without .sql (e.g. "0001_identity")
    path: str
    sql: str


def discover(directory: str = MIGRATIONS_DIR) -> list[Migration]:
    """Return PostgreSQL migrations only, sorted by apply order."""
    out: list[Migration] = []
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".sql") or fn in SQLITE_ONLY_MIGRATIONS:
            continue
        path = os.path.join(directory, fn)
        with open(path, encoding="utf-8") as f:
            out.append(Migration(version=fn[:-4], path=path, sql=f.read()))
    return out


def combined_sql(directory: str = MIGRATIONS_DIR) -> str:
    return "\n".join(m.sql for m in discover(directory))


def _norm(sql: str) -> str:
    """Lower-case and collapse whitespace so multi-line DDL matches simply."""
    return re.sub(r"\s+", " ", sql.lower())


def rls_coverage(directory: str = MIGRATIONS_DIR) -> dict[str, dict]:
    """Per tenant table: is it created, is RLS ENABLEd + FORCEd, has a policy.

    Word boundaries keep ``users`` from matching ``user_settings`` (the ``_`` is
    a word char, so ``users\\b`` does not match inside ``user_settings``)."""
    sql = _norm(combined_sql(directory))
    cov: dict[str, dict] = {}
    for t in TENANT_TABLES:
        cov[t] = {
            "created": bool(re.search(rf"create table if not exists {t}\b", sql)),
            "enabled": bool(re.search(rf"alter table {t} enable row level security", sql)),
            "forced": bool(re.search(rf"alter table {t} force +row level security", sql)),
            "policy": bool(re.search(rf"create policy \w+ on {t}\b", sql)),
        }
    return cov


def apply(conn, directory: str = MIGRATIONS_DIR) -> list[str]:  # pragma: no cover - needs live Postgres
    """Forward-only apply of pending migrations through a DB-API (psycopg)
    connection. Records applied versions in ``schema_migrations``; idempotent.
    Returns the versions applied this call."""
    cur = conn.cursor()
    cur.execute(
        "create table if not exists schema_migrations ("
        " version text primary key,"
        " applied_at timestamptz not null default now())")
    conn.commit()
    cur.execute("select version from schema_migrations")
    done = {r[0] for r in cur.fetchall()}
    applied: list[str] = []
    for m in discover(directory):
        if m.version in done:
            continue
        cur.execute(m.sql)
        cur.execute("insert into schema_migrations(version) values (%s)", (m.version,))
        conn.commit()
        applied.append(m.version)
    return applied
