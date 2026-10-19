"""Actual disposable PostgreSQL DDL, NOT SQLite-worker PG certification.

Opt in with NEXUS_PG_OUTBOX_TEST=1. The imported fixture creates a local,
network-isolated Docker server; it accepts no deployment URL or credentials.
"""
from data.pg_migrations import discover
from test_paper_evidence_outbox import isolated_postgres, postgres_outbox


def test_real_postgres_identity_upgrade_routes_only_scoped_sql(postgres_outbox):
    sql,value=postgres_outbox.sql,postgres_outbox.value
    sql("CREATE TABLE representative_history(id text primary key, value numeric); INSERT INTO representative_history VALUES ('old',0.123456789);")
    before=value("SELECT jsonb_agg(to_jsonb(h)) FROM representative_history h;")
    sql("CREATE TABLE schema_migrations(version text primary key);")
    for migration in discover():
        sql("BEGIN;"+migration.sql+"INSERT INTO schema_migrations VALUES ('"+migration.version+"'); COMMIT;")
    assert value("SELECT jsonb_agg(to_jsonb(h)) FROM representative_history h;")==before
    assert value("SELECT to_jsonb(COUNT(*)) FROM pg_tables WHERE tablename LIKE 'shadow_%';")==0
    # Forward runner skips completed versions; no duplicate policies or rows.
    done=value("SELECT jsonb_agg(version) FROM schema_migrations;")
    assert done==[m.version for m in discover()]
    assert all(value("SELECT to_jsonb(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE relname='"+table+"';") for table in ("tenants","users","profiles","sessions","user_settings"))


def test_real_postgres_interrupted_ddl_transaction_rolls_back(postgres_outbox):
    sql,value=postgres_outbox.sql,postgres_outbox.value
    migration=discover()[0]
    failed=sql("BEGIN;"+migration.sql+"SELECT missing_sprint8_relation; COMMIT;",check=False)
    assert failed.returncode!=0
    assert value("SELECT to_jsonb(COUNT(*)) FROM pg_tables WHERE tablename='tenants';")==0
    sql("BEGIN;"+migration.sql+"COMMIT;")
    assert value("SELECT to_jsonb(COUNT(*)) FROM tenants;")==1
