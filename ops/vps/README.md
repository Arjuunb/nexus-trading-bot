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
| `staging-pa-off-stop` | `check`, `stop` or `verify` | The P1-B stop logic for the PA-off staging API (build `f9ebd32`): confirms the target and that production has its own volume and Supabase project, then stops only the staging API and keeps it stopped after a reboot. The original P1-B script stays pinned to the earlier build |
| `docker-cleanup` | `plan`, then `apply` | P1-C: remove unused old production app images, keeping the two most recent and every image a container uses, plus the build cache. `apply` removes exactly the list from the last `plan` and refuses if anything changed |

## Read-only checks without approval (environment `vps-check`)

The checks listed in `check-operations.txt` (status, inventory, health and measurement checks that
change nothing) can run **without** a per-run approval. That happens only when all of these hold:

1. the run is started from `main`, and the operation and argument are on that list
2. `check-env-guard.py` confirms the GitHub environment `vps-check` may be used **only from `main`**
3. the run uses the separate **check key**, and the VPS runner then works in check mode: it refuses
   anything that is not on its own built-in copy of the list (changing that copy means installing a
   new runner on the server)

Everything else (deploy, stop, start, delete, backup start, cleanup), and any run from another
branch, still uses environment `vps` and waits for the owner's approval. The workflow summary says
which of the two applies.

### One-time setup of the check key

On the VPS (one command per box). Connect: `ssh -o ServerAliveInterval=30 ubuntu@<vps-address>`

**1. Download the new runner**

```bash
sudo curl -fsSL -o /root/nexus-ops-setup/nexus-ops-runner https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/5f28275abff067013178e50d160535df4292c438/ops/vps/nexus-ops-runner
```

**2. Download the new setup script**

```bash
sudo curl -fsSL -o /root/nexus-ops-setup/setup-vps-ops.sh https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/5f28275abff067013178e50d160535df4292c438/ops/vps/setup-vps-ops.sh
```

**3. Check the runner.** It must say `OK`.

```bash
echo "42959c9d2ad563c1eed6b573e44c762f63aa572a06def0128b3633a71864d7df  /root/nexus-ops-setup/nexus-ops-runner" | sudo sha256sum -c -
```

**4. Check the setup script.** It must say `OK`.

```bash
echo "f8a1907c1eed44274e20b9cdc6c74b9588c9364c79e12ebcdb423303ffacf967  /root/nexus-ops-setup/setup-vps-ops.sh" | sudo sha256sum -c -
```

**5. Install the runner and add the check key.** The main key keeps working.

```bash
sudo sh /root/nexus-ops-setup/setup-vps-ops.sh --add-check-key /root/nexus-ops-setup/nexus-ops-runner
```

It must show `ok: check mode runs a read-only check and refuses a change`, then prints a private
key. That key goes **only into GitHub, never into a chat**.

In GitHub: **Settings → Environments → New environment**, name `vps-check`, **Configure environment**.

1. Leave **Required reviewers** unticked.
2. **Deployment branches and tags** → **Selected branches and tags** → **Add deployment branch or
   tag rule** → `main`. Add nothing else (not `claude/*`).
3. **Environment secrets**: `VPS_SSH_KEY` = the private key from step 5 (with the BEGIN and END
   lines); `VPS_HOST` = the same address as in `vps`.
4. **Environment variables**: `VPS_HOST_KEY` = the `ssh-ed25519 …` line from step 5;
   `VPS_SSH_PORT` = `22`.

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
runner, against a local OpenSSH server and the P1-B replica containers. Result with both keys:
48 passed, 0 failed (`test/test-output.txt`). `test/check_mode_test.sh` covers the read-only list,
the runner's built-in copy, check-mode refusals and the environment guard: 51 passed. The tests cover:

- P1-B through the pipeline
- tampered scripts, shell characters, arbitrary commands, plain login and port forwarding (all refused)
- the off switch, one-at-a-time locking, a dropped connection, the time limit and the audit trail
- removing and re-installing (the old key stops working)
- the check key: read-only checks run; stop, deploy, delete, cleanup and backup start are refused
  before any script is read; re-installing the main key keeps the check key
