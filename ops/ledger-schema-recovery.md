# Logical column positions and retained ledger recovery

PostgreSQL preserves physical column-number gaps after `DROP COLUMN`. A logical
dump restores visible columns in the same relative order with dense physical
numbers. The catalog comparison canonicalizes only these numbers. Column order,
names, definitions, constraints, policies, ownership, grants and all other recorded
schema fields must still match. Original catalog JSON remains unchanged.

`verify-retained-supabase-ledger.py` is a local continuation for a backup stopped
at `RESTORED_SCHEMA_DEFINITION_MISMATCH` when column numbering is the only
difference. It rejects additional schema differences, non-private input files,
symlinks, running clusters and remaining source credentials before starting.

It starts only the backup helper's retained, stopped PostgreSQL 17 test cluster
with TCP listening and preload libraries disabled, then restores the archive
again into a new randomly named local database. The previous restored database
is kept. Full recorded schema equality and financial row fingerprints are checked.
The test server is stopped on success or failure. No password, source connection,
VPS command, production update or source deletion is involved.

Keep the original backup directory and the separate verification directory.
The original archive, source catalog and source fingerprints are required,
along with the verification receipt and recorded local role/bootstrap metadata.
All retained input files are hashed before and after verification. These seals
record their state at the start of reverification; they do not retroactively
claim a separately recorded hash from the interrupted original backup.

The result proves recovery of the recorded snapshot's selected application,
Auth and reviewed extension schemas. It does not prove recovery of current live
state, all Supabase services, Storage object payloads, original role passwords,
login attributes or role memberships. It does not complete deployment acceptance.
