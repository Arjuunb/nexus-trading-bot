# Replica test for `p1b_stop_staging_api.sh`

**Disposable Docker hosts only. Never run this on the VPS.** It removes and recreates containers
that carry the production and staging names. `setup.sh` refuses unless `P1B_REPLICA_TEST=disposable`
is set, and it also refuses to touch any container or volume with those names that it did not create.

The replicas copy what P0 measured on the VPS (2026-10-23):

| | Production replica | Staging replica |
|---|---|---|
| Name | `nexus-trading-bot-app-1` | `nexus-remote-stage-rc3-28acdbc-api-1` |
| Compose labels | project `nexus-trading-bot` | project `nexus-remote-stage-rc3-28acdbc`, service `api`, revision `f8778ed…` |
| Data volume at `/var/lib/tradexa` | `nexus-migration-tradexa-data-cutover-20260913` | `nexus-remote-stage-rc3-28acdbc_api-state` |
| Restart policy | `unless-stopped` | `on-failure:2` |
| Start command | — | `sh -c "uvicorn app:app …"` on Alpine as user `tradexa` (same as `ops/api.alpine.Dockerfile`) |
| Behaviour | answers `/health` and `/status/public` | a background thread writes to two WAL-mode SQLite files; ASGI shutdown stops it |

Build and run:

```bash
cd stage && pip download --no-deps --only-binary=:all: -d wheels uvicorn h11 click \
  && docker build -t p1b-stage-api:test . && cd ..
docker build -t p1b-prod-app:test prod
P1B_REPLICA_TEST=disposable sh run_tests.sh
# optional reboot simulation: P1B_RESTART_DOCKER_CMD='<command that restarts dockerd>'
```

What `test-output.txt` shows (57 passed, 0 failed, both reboot simulations included):

- On Alpine, `sh -c` hands over to uvicorn, so uvicorn receives Docker's stop signal itself. It shut
  down cleanly in about 1 s with exit code 0. The WAL files were merged and removed, the database
  passed `integrity_check`, and every row was kept.
- Production received no Docker actions apart from its own health checks, and was not restarted.
- After a Docker daemon restart (what a server reboot does), a cleanly stopped staging stays stopped.
- If shutdown hangs and Docker kills the process (exit 137), an `on-failure` container **does** start
  again after a daemon restart; the control container without the change did. The script sets that
  one container's restart policy to `no` in that case, and staging stayed stopped.
- Every refusal case exits 2 and leaves staging running with nothing written:
  - the same Supabase project (even when the case or the path differs)
  - a different staging build
  - no `SUPABASE_URL`
  - production missing
  - staging on another volume
  - not run as root
- No secret or Supabase address appears in any output.
