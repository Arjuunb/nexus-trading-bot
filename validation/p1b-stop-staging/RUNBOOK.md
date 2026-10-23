# P1-B — Stop staging's disk growth (operator runbook)

**Approved 2026-10-23.** This stops one container, `nexus-remote-stage-rc3-28acdbc-api-1`: the staging
API, build `f8778ed`. It writes an `EQUITY_OBSERVED` row on every market quote and fills the disk by
~2 GiB/day, which leaves about 8 days (see `NEXUS_P0_VPS_EVIDENCE_REPORT.md` §11).

## What changes and what does not

| Changes | Not touched |
|---|---|
| The staging API container stops. | All production containers: app, nginx, certbot. |
| Only if Docker would start it again after a reboot: that container's restart policy becomes `no`. The script prints which case applies. | The three paper-trading instances, Guardian, SMC Agent, Adaptive MTF, journals. |
| | Every database and file. Nothing is deleted. The 31 GiB of staging data stays on disk. |
| | The other staging containers (postgres, research worker, ingress) and every configuration file. |

While staging is stopped:

- The staging website returns an error.
- The staging labs and Task 6 staging observation pause.
- Free disk space stops falling by ~2 GiB/day. Production still adds ~30 MiB/day.

## Why it is safe

- **Production is separate.** It runs as a different Compose project and container, on its own data
  volume. The script only reads its status.
- **Staging's shutdown only stops itself** (`app.py` 616–648 at `f8778ed`):
  - Its entry gates close in memory only.
  - Its labs and workers stop.
  - Trading instances keep their restart intent (`trading_instances.py` 2275–2277).
  - Its ledger and instance tables are in its own Supabase project. The script checks that this
    differs from production's before doing anything, without printing either value.
- **The databases are protected.** Their WAL files are small (70 MiB and 12 MiB), and a clean stop
  merges them. SQLite's WAL mode also survives a forced stop.
- **Nothing restarts staging on its own.** It is deployed only by hand: `staging-release.yml` has no
  push trigger and no deploy job, and no cron job or timer on the host touches it. Its restart policy
  (`on-failure:2`) is handled as described above.
- **Tested against copies of the containers**, including server-reboot simulations: 57 passed,
  0 failed (`replica-test/`).

## Steps (on the VPS)

Connect with `ssh -o ServerAliveInterval=30 ubuntu@<vps-address>`.

**1. Get the exact script.** The last line must print `OK`.

```bash
sudo install -d -m 700 /root/p1b-staging
sudo curl -fsSL -o /root/p1b-staging/p1b_stop_staging_api.sh \
  https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/33f7e68187297af08ad7c66c8e671aade7f3eadf/validation/p1b-stop-staging/p1b_stop_staging_api.sh
echo "eccb9c461ef15782d408b3cf38111ab71eb9684ff64ce1cbc2ab1a41d86b549f  /root/p1b-staging/p1b_stop_staging_api.sh" | sudo sha256sum -c -
```

**2. Preflight (read-only).** The last line must start with `GO`. On `NO-GO`, stop and send the output.

```bash
sudo sh /root/p1b-staging/p1b_stop_staging_api.sh check
```

**3. Stop.** It runs in the background, so a dropped SSH connection cannot interrupt it.

```bash
sudo sh -c 'setsid nohup sh /root/p1b-staging/p1b_stop_staging_api.sh stop > /root/p1b-staging/stop.log 2>&1 < /dev/null &'
sleep 45; sudo cat /root/p1b-staging/stop.log
```

It is finished when the last line starts with `DONE`. That usually takes under a minute, and at most
about 4 minutes if production answers slowly or staging needs the full 120 s to stop. If it has not
finished yet, run `sudo cat /root/p1b-staging/stop.log` again.

**4. Verify (read-only), 10 or more minutes later.**

```bash
sudo sh /root/p1b-staging/p1b_stop_staging_api.sh verify
```

Expect `ok: staging API is still stopped` and `ok: staging lab files unchanged since the stop`.

**5. Send the output of steps 2, 3 and 4** as pasted text. It contains no secrets.

## Undo (resumes staging and its disk growth)

```bash
# only if stop.log says "restart policy changed to 'no'":
sudo docker update --restart=on-failure:2 nexus-remote-stage-rc3-28acdbc-api-1
sudo docker start nexus-remote-stage-rc3-28acdbc-api-1
```

The staging labs resume on their own, as they did after staging's restart on Oct 18. Do not run
`docker compose up` in the staging folder unless you mean to start staging again.
