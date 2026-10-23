# P1-B result: staging API stopped (2026-10-23)

**Done at 13:45:23 UTC.** `nexus-remote-stage-rc3-28acdbc-api-1` is stopped. Production was not
touched and stayed healthy throughout. Nothing was deleted.

Run through the approval-gated **VPS operations** workflow. The owner approved each run; the script is
`p1b_stop_staging_api.sh`, SHA-256 `eccb9c46…549f`, from commit `8838c85`.

| Step | GitHub run | Result |
|---|---|---|
| Connection test (`hello`) | [#2](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39963082508) | VPS reachable: 18 GB free, 5.1 GB memory available, all containers up |
| `check` (read-only) | [#3](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39963463780) | **GO**: right target, separate volume, **different Supabase projects** |
| `stop` | [#4](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39963636416) | Clean shutdown in 25 s, exit code 0; restart policy left `on-failure:2` (stays stopped after a reboot) |
| `verify` (read-only) | [#5](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39963936029) | Still stopped; lab files unchanged since the stop |

Run #1 failed before reaching the VPS: the `VPS_SSH_KEY` secret held an invalid key and was re-saved.

## State before and after

| | 13:44:57 (before) | 13:45:23 (after) | 13:46:43 (verify) |
|---|---|---|---|
| Staging API | running | exited (0) | exited (0) |
| Production | healthy, `215c96c` | healthy, `215c96c` | healthy, `215c96c` |
| Production workers | 3 of 3 running | 3 of 3 running | 3 of 3 running |
| `price_action_paper.db` | 17,973,682,176 B | unchanged | unchanged |
| `smc_strategy_paper.db` | 15,674,056,704 B | unchanged | unchanged |
| Disk `/` free | 17 GB | 17 GB | 17 GB |

Staging's Price Action file grew from 16.66 GiB (11:43) to 16.74 GiB (13:43), about 1 GiB/day for
that file alone. That growth has now stopped.

## Notes

- **WAL files remain:** `price_action_paper.db-wal` (70 MiB) and `smc_strategy_paper.db-wal`
  (40 MiB). The app did not checkpoint them on shutdown. They hold committed data, which SQLite
  applies the next time the database is opened. Anyone copying or backing up these databases must
  copy the `-wal` files with them.
- **The verify ran about 80 s after the stop.** That is conclusive, because the staging container
  is stopped and nothing else writes those files.
- **The 31 GiB of staging data is still on disk.** Freeing it is a separate decision. So are
  reclaiming Docker images and build cache (P1-C, about 23 GiB) and an off-host backup (P1-A).
- **The other staging containers are still running:** postgres, research worker and ingress. The
  staging website returns an error while the API is stopped.

## Undo (resumes staging and its disk growth)

```bash
sudo docker start nexus-remote-stage-rc3-28acdbc-api-1
```
