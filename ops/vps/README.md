# VPS operations through GitHub

Runs one reviewed script on the VPS from GitHub, and **only after you approve that run**.
Nothing runs on push.

## How a run works

1. A run of the **VPS operations** workflow is started, either by Claude or by you from the Actions
   tab. It names an operation: one file in `ops/vps/operations/`.
2. Job **What will run** shows the exact script, the commit and its SHA-256 on the run's summary
   page. No secrets are available to this job.
3. Job **Run on the VPS** waits for your approval in the `vps` environment. Only after you approve
   does GitHub give it the SSH key. It then sends the script to the VPS.
4. On the VPS, that key can only start `/usr/local/sbin/nexus-ops-runner`. The runner:
   - checks that the script's SHA-256 matches the approved one
   - runs one script at a time, as root, with a 30-minute limit
   - keeps running if the connection drops
   - keeps a copy of every script (`/var/lib/nexus-ops/state/scripts/`) and its full output
     (`/var/log/nexus-ops/`)

The workflow log is public, because this repository is public. Operations print short status only:
never secrets, balances or detailed evidence.

| Operation | Argument | What it does |
|---|---|---|
| `hello` | — | Read-only: host, disk, memory, container status, last runs |
| `ops-log` | GitHub run id | Read-only: end of an earlier run's log, including one whose connection dropped |
| `p1b-stop-staging` | `check`, `stop` or `verify` | The reviewed P1-B script (`validation/p1b-stop-staging/`) |
| `backup` | `run`, `status`, `check` or `restore-test` | Encrypted off-server backups to Backblaze B2 (`backup/README.md`). Not set up: the owner chose to skip backups on 2026-10-23 |
| `staging-pa-delete` | `check`, then `delete` | Safety and recovery check for the stopped staging PA lab database (`price_action_paper.db` and its `-wal` and `-shm`); ends with SAFE FOR SEPARATE DELETION APPROVAL or BLOCKED. `delete` (owner-approved 2026-10-23) deletes only those 3 files, and only if every gate passes again, the last `check` is SAFE, under 24 h old and for identical files. It keeps the verified backup and writes a receipt |
| `pa-backup` | `start`, then `status` | Verified backup of the stopped staging PA database. It packs the `.db`, `-wal` and `-shm` into one compressed archive in `/var/lib/nexus-ops/pa-backup`, then restore-tests it in a temporary folder: byte-identical files, WAL frames applied, full integrity_check, row counts. It writes `manifest.json` only on success and runs in the background; originals are only read |
| `staging-inventory` | `show` | Read-only: how the staging project is set up before the Price Action Lab retirement deploy (containers, images, limits, mounts, Compose files rendered with values hidden, volumes and SQLite files, the uploaded image package, production identity, free disk) |
| `staging-config-check` | `show` | Read-only: which Compose files, env file and config hash each staging container was created with; staging YAML files (memory/image overlays shown in full when secret-free); env variable names; what Compose would create with and without each memory overlay; local nexus images |
| `staging-backup` | `start`, then `status` | Verified backup of what the PA retirement deploy could change: the stopped staging API's data volume (all files except the Task 6 recovery folder, which is already a compressed copy) and the staging Postgres metadata (pg_dumpall roles + pg_dump per database). Restore-tested: byte-identical files, WAL applied, integrity_check on every SQLite database, and the Postgres dump loaded into a throwaway container with no network. Writes `manifest.json` only on success; runs in the background; originals are only read |
| `staging-pa-off-images` | `load` | Checks the uploaded PA-retirement image package (`~ubuntu/nexus-pa-retirement/…zip`) against the SHA-256 GitHub recorded, verifies its sealed file list, security reports (0 HIGH/CRITICAL, 0 secrets) and the API image archive, loads the API image only, probes it without starting the app (PA lab default off, no dashboard PA calls, non-root) and records the image ID for the deploy step. Starts or changes no container |
| `staging-pa-off-deploy` | `plan`, then `apply`; `rollback` if needed | PA retirement on the STAGING API only. `plan` (read-only) requires the verified staging backup with unchanged API data, the verified PA-off image, and the exact reviewed Compose files, and proves the API would change only its image while keeping 1536 MiB, volumes, networks, user and environment. `apply` (needs a plan under 2 h old with nothing changed) recreates only the API, waits for healthy, checks `/health` (PA off) and that every PA route is the retired one, reloads the staging web proxy and confirms production did not change; an unhealthy API is stopped again. `rollback` puts the previous image back, stopped |
| `staging-pa-off-check` | `measure` | Read-only, 10 minutes, after the deploy: identity (verified image, healthy, no restarts/OOM, 1536 MiB); PA gone at runtime (`/health` off, all PA routes retired, PA database not re-created, leftover PA file untouched, no PA file open, no PA runtime log lines); shared features (SMC database written, research worker and web proxy healthy); API/production/host CPU, memory and disk from cgroup counters vs the P0 baseline; production unchanged. Ends with GO or NO-GO |
| `docker-cleanup` | `plan`, then `apply` | P1-C: remove unused old production app images, keeping the two most recent and every image a container uses, plus the build cache. `apply` removes exactly the list from the last `plan` and refuses if anything changed |

**Off switch** (blocks every run at once): `sudo touch /etc/nexus-ops/disabled`. To switch it back
on: `sudo rm /etc/nexus-ops/disabled`. **Remove everything** except the logs:
`sudo sh /root/nexus-ops-setup/setup-vps-ops.sh --remove`

## One-time setup

### On the VPS (one command per box)

Connect: `ssh -o ServerAliveInterval=30 ubuntu@<vps-address>`

**1. Make a folder**

```bash
sudo install -d -m 700 /root/nexus-ops-setup
```

**2. Download the runner**

```bash
sudo curl -fsSL -o /root/nexus-ops-setup/nexus-ops-runner https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/96fab3d805d820c1315f5f996b957becc609de21/ops/vps/nexus-ops-runner
```

**3. Download the setup script**

```bash
sudo curl -fsSL -o /root/nexus-ops-setup/setup-vps-ops.sh https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/96fab3d805d820c1315f5f996b957becc609de21/ops/vps/setup-vps-ops.sh
```

**4. Check the runner.** It must say `OK`.

```bash
echo "63280e8ab99a982130b536230514068ccb2e39ca14bee73073274a1cc3d926ff  /root/nexus-ops-setup/nexus-ops-runner" | sudo sha256sum -c -
```

**5. Check the setup script.** It must say `OK`.

```bash
echo "46cdf02fa344942ccb055e8f82a929360581eade167eb35b8ba49cac3f7f9ad4  /root/nexus-ops-setup/setup-vps-ops.sh" | sudo sha256sum -c -
```

**6. Run the setup.** Keep the window open; you need what it prints for the GitHub part.

```bash
sudo sh /root/nexus-ops-setup/setup-vps-ops.sh /root/nexus-ops-setup/nexus-ops-runner
```

It ends by printing `VPS_HOST_KEY`, `VPS_SSH_PORT` and a private key (`VPS_SSH_KEY`). The private
key goes **only into GitHub, never into a chat**. If anything above `6/6` says `WARNING` or
`setup stopped`, send that part (not the key).

### On GitHub (web browser)

Open the repository, then **Settings → Environments → New environment**. Name it `vps` and click
**Configure environment**.

1. Tick **Required reviewers**, add yourself (`Arjuunb`) and click **Save protection rules**.
   Leave **Prevent self-review** unticked: runs Claude starts appear under your name.
2. Under **Deployment branches and tags**, choose **Selected branches and tags**. Add `main`,
   then add `claude/*`.
3. Under **Environment secrets**, click **Add environment secret** twice:
   - `VPS_SSH_KEY`: the private key from step 6, including the BEGIN and END lines.
   - `VPS_HOST`: the address you use after `ubuntu@` when you connect.
4. Under **Environment variables**, click **Add environment variable** twice:
   - `VPS_HOST_KEY`: the `ssh-ed25519 …` line from step 6.
   - `VPS_SSH_PORT`: the number from step 6, usually `22`.

### Switching it on

GitHub only lets a workflow be started once its file is on the default branch (`main`). That
needs a pull request that adds `.github/workflows/vps-ops.yml` and `ops/vps/` to `main`. After it is
merged, the first run should be `hello`, to prove the connection works.

## Testing

`test/pipeline_test.sh` drives the real path: workflow script → SSH → forced command → sudo →
runner, against a local OpenSSH server and the P1-B replica containers. Result: 32 passed,
0 failed (`test/test-output.txt`). The tests cover:

- P1-B through the pipeline
- tampered scripts, shell characters, arbitrary commands, plain login and port forwarding (all refused)
- the off switch, one-at-a-time locking, a dropped connection, the time limit and the audit trail
- removing and re-installing (the old key stops working)
