# P1-A: encrypted off-server backups (Backblaze B2)

Every night at 03:17 UTC, and whenever it is started from GitHub, the VPS backs up production to a
private Backblaze B2 bucket. The data is encrypted on the server with **your backup password**
before it leaves, so Backblaze cannot read it. Without that password the backups cannot be opened,
so keep it in your password manager.

**What is backed up**
- every SQLite database in the production data volume, copied safely while the app keeps running
- the rest of that volume
- `/opt/nexus-trading-bot/.env`, which holds `HUB_MASTER_KEY` and the other secrets
- the compose and nginx files
- the TLS certificates

**Not included**
- staging, which is stopped
- Supabase, which is Supabase's own service with its own backups

**How long backups are kept:** 7 daily, 4 weekly and 6 monthly. That's about 1–2 GB, within B2's
free 10 GB.

## Part 1: Backblaze (in the browser)

1. Sign up at backblaze.com for **B2 Cloud Storage**. If asked for a region, choose **EU Central**.
2. Go to **Buckets** and click **Create a Bucket**.
   - Name: something unique, for example `nexus-backup-` followed by 6 random digits.
   - Files in bucket: **Private**.
   - Leave encryption and Object Lock as they are, then create the bucket.
3. On the new bucket, click **Lifecycle Settings**, choose **Keep only the last version of the
   file**, and save.
4. On the bucket's card, note the **Endpoint**, which looks like `s3.eu-central-003.backblazeb2.com`.
5. Go to **Application Keys** and click **Add a New Application Key**.
   - Name: `nexus-vps-backup`.
   - Access to: your bucket only.
   - Type: **Read and Write**.
   - Click Create. Backblaze shows `keyID` and `applicationKey` **only once**, so keep that page
     open until Part 2 is finished.
6. In your password manager, create an entry called "Nexus VPS backup" with a generated strong
   password, and save it.

## Part 2: on the VPS (one command per box)

```bash
sudo curl -fsSL -o /root/nexus-ops-setup/nexus-backup https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/bd69d0add54cd491f53fb403159935d3d9e6e0d3/ops/vps/backup/nexus-backup
```

```bash
sudo curl -fsSL -o /root/nexus-ops-setup/setup-backup.sh https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/bd69d0add54cd491f53fb403159935d3d9e6e0d3/ops/vps/backup/setup-backup.sh
```

```bash
echo "3dcc934da260b87988ebb391fb80ff493fd1d2c696469a76c2abc5667308e158  /root/nexus-ops-setup/nexus-backup" | sudo sha256sum -c -
```

```bash
echo "9e1859f51fd816e5f609b5535edefc9a9cc8487446f2690910e1b114a8fbbdac  /root/nexus-ops-setup/setup-backup.sh" | sudo sha256sum -c -
```

```bash
sudo sh /root/nexus-ops-setup/setup-backup.sh /root/nexus-ops-setup/nexus-backup
```

The setup asks for five things. Copy each one from the browser or your password manager and paste
it into the terminal.
- Bucket name and Endpoint are shown as you paste them.
- keyID, applicationKey and the password are **hidden**: nothing appears when you paste. That is
  normal; just press Enter.

The setup does not back anything up yet. Its output contains no secrets.

## Part 3: first backup (through GitHub)

Claude starts the **VPS operations** workflow with operation `backup` and argument `run`, and you
approve it. Then the same again with `restore-test`, which proves the backup can be restored and
read.

| Argument | What it does |
|---|---|
| `run` | Back up now |
| `status` | Last run, backups stored, storage used, next scheduled run (read-only) |
| `check` | Verify the stored backups by reading 10% of the data back (read-only) |
| `restore-test` | Restore the latest backup into a temporary folder, integrity-check every database and the `.env`, then delete the folder |

## Safety

- **No locks on the app's databases:**
  - WAL databases go through SQLite's backup API, which never blocks writers.
  - The other databases are opened by the app with Python's 5-second lock wait (`decisions.db`,
    `journal.db`, `ledger.db` and others). They are copied without any lock, and a copy is kept
    only if the file provably did not change during it: no journal file, and the same change
    counter, size and timestamp. Otherwise it retries.
  - Live database files are never uploaded directly.
- **Short, safe output:** it never shows keys, the password or the storage address, because it can
  appear in public GitHub logs.
- **Undo:** `sudo sh /root/nexus-ops-setup/setup-backup.sh --remove` removes the schedule, the
  program and the keys from the server. Backups already in Backblaze stay there.
- **Tested:** `test/backup_test.sh` (43 passed, 0 failed) ran while live writers kept going, one of
  them every 3 s with the app's 5-second lock wait. No writer saw a lock error, and the slowest
  commit took 21 ms.
