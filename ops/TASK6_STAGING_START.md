# Start Strategy Intelligence for staging acceptance

This operation starts only the existing, stopped `nexus-remote-stage-rc3-28acdbc`
API using its already scanned application commit
`f9ebd32a3eedf6740c14a3daafb32e981d1d7ce8` and exact manifest
`sha256:9b7087da9cd66ff51f2120e84444705e1ae02464855e63b25267d3f0a2fbf492`.
It adds only `NEXUS_ACCEPTANCE_READ_ONLY=1`. No application source, image,
production service, financial history or resource limit changes.

The intent is an authenticated Strategy Intelligence research workspace.
Staging paper/live controls, live feeds and saved execution sessions remain
paused. Production continues using its current application and strategy lineage.
The operation does not authorize production replacement or strategy promotion.

## Owner action

On the owner's Mac, run the existing Python installation with
`ops/run-task6-staging-from-mac.py --start`. The three adjacent operation files
must match the SHA-256 pins in that entry point. It uses the existing SSH identity
and known-host record, with no database password or new key setup.

The entry point first verifies the retained local ledger receipt, all five
recorded input hashes and the presence of the recovery bootstrap record.
It copies only a non-secret result to a new private VPS operation directory.
The original local backup and its verification directory are both retained.
This is proof for the recorded selected application snapshot, not full Supabase
platform recovery, current live state or cross-database atomicity.

## Server gates

1. Acquire the same one-operation lock used by the VPS runner.
2. Require the stopped API, exact scanned image and staging Supabase project.
   Refuse shared production state or another API-volume consumer, apart from the
   existing stopped staging initializer. Require the worker, metadata database
   and ingress to already be healthy.
3. Render the recorded Compose files. Require their API config hash and rendered
   environment to reproduce the stopped API. The proposed render may differ only
   in the one acceptance flag. Existing 1536 MiB memory/swap must already match.
   Refuse an already existing backup-test container instead of allowing the
   backup tool to replace an unknown container with its reserved name.
4. Create a new stopped-volume backup and detached SQLite/Postgres restore test
   using the existing backup implementation copied from operations commit
   `f43a274ff0851f5e74c2818271fa5f2f74d4ced5`. Only a new output directory is selected;
   its reserve, consistency and restore gates are unchanged. Prior backups remain.
   The tool checks actual free capacity before starting; no upgrade or pruning.
5. Recheck all containers and config-file hashes after the backup. Record the
   original Compose file list and fresh backup for rollback.
6. Recreate only `api`, with no dependency starts, image pulls or builds. Verify
   actual image, mounts, networks, full environment, security settings and limits.
7. Require healthy configured primary, PA disabled, acceptance mode active,
   unauthenticated research rejected, execution POSTs blocked and dashboard HTML
   served. Check financial main/WAL/journal file identities do not change.
8. Restart only this staging API; recheck health and financial persistence.
   Test and reload the existing staging ingress, without restarting it.
9. Measure one minute of idle CPU, app/container RAM and disk I/O. Refuse average
   idle API CPU >=40% of one core or anonymous memory >=90% of its existing cap.
   These are bounded smoke gates, not a benchmark or full-load acceptance.
10. Require no PA file descriptors and unchanged other container identities.

Any failure after the API start attempts to stop that staging API. A failed stop
is explicitly reported. No unsafe normal-mode or old-image restart is automatic.
No source financial database is deleted, migrated or restored by this helper.
The backup implementation removes only its own temporary restore-test copies.

## Result and remaining acceptance

The private remote `startup-receipt.json` records actual outcomes and resource
samples. Success is `STRATEGY_INTELLIGENCE_STAGING_RESEARCH_MODE_STARTED`.
It deliberately retains `task6_complete=false`: authenticated owner dashboard,
PRD evidence/abstention and full worker workflow acceptance remain required.
No synthetic records are injected into customer financial or research data.

After a successful receipt, sign in through the existing staging workspace and
open `/app/strategy-intelligence`. Browser results must be recorded before
claiming end-to-end completion. For rollback, use the recorded original Compose
list with `--no-start api`; preserve the stopped state instead of resuming normal
execution automatically. Current production uses a separate Guardian lineage and
must not be replaced with this staging image as a shortcut.

## Tests

The standalone unit suite covers single-flag scope, image/resource/consumer gates,
current backup integrity/foreign keys, retained ledger input hashes, health
policy, financial/WAL preservation and stop-on-failure behavior. It uses synthetic
fixtures and does not access the VPS. Application acceptance tests separately
exercise the existing release's real boot, HTTP policy and PRD abstention.
