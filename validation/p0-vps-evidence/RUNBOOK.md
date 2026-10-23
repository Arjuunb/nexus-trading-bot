# P0 VPS evidence collection — operator runbook

Read-only. Takes about 15–20 minutes, and keeps running if your SSH session
drops. It does not deploy, restart, exec into containers, change configuration,
delete, prune, vacuum, checkpoint, or write anywhere except its own files in
`/root/p0-evidence`.

Tip: connect with `ssh -o ServerAliveInterval=30 …` so an idle session is not
dropped by the network. (The first attempt on 2026-10-23 was cut off by a
connection reset while the collector was silently sampling.)

## 0. Make sure an earlier attempt is not still running

```bash
ps -eo pid,etime,args | grep "[c]ollect_p0_evidence" || echo "nothing running"
```

If a process is listed, wait for it to finish instead of starting another.

## 1. Get the exact script onto the VPS

Do **not** `git pull` inside `/opt/nexus-trading-bot`: that is the production
checkout and must not change. Fetch the single file instead.

Expected SHA-256: `4a2a13828ce3cad08fa07d9a7836f17688b98fa566ca5c97cc1f4b3d56180c74`
(pinned to commit `27bba32075909e8d0d203881a45729a81c9e0cd2`).

**Option A — download on the VPS (simplest):**

```bash
sudo install -d -m 700 /root/p0-evidence
sudo curl -fsSL -o /root/p0-evidence/collect_p0_evidence.py \
  https://raw.githubusercontent.com/Arjuunb/nexus-trading-bot/27bba32075909e8d0d203881a45729a81c9e0cd2/validation/p0-vps-evidence/collect_p0_evidence.py
echo "4a2a13828ce3cad08fa07d9a7836f17688b98fa566ca5c97cc1f4b3d56180c74  /root/p0-evidence/collect_p0_evidence.py" | sudo sha256sum -c -
df -h /                                                 # note free space before
```

The `sha256sum -c` line must print `OK`. If it does not, stop.

**Option B — copy from your computer.** In any clone of the repository:

```bash
git fetch origin claude/beautiful-faraday-0nbitn
git show 27bba32075909e8d0d203881a45729a81c9e0cd2:validation/p0-vps-evidence/collect_p0_evidence.py > collect_p0_evidence.py
sha256sum collect_p0_evidence.py                        # must equal the value above
scp collect_p0_evidence.py ubuntu@<vps-address>:/tmp/
```

Then on the VPS:

```bash
sudo install -d -m 700 /root/p0-evidence
sudo install -m 600 /tmp/collect_p0_evidence.py /root/p0-evidence/
echo "4a2a13828ce3cad08fa07d9a7836f17688b98fa566ca5c97cc1f4b3d56180c74  /root/p0-evidence/collect_p0_evidence.py" | sudo sha256sum -c -
df -h /                                                 # note free space before
```

## 2. Start it in the background

```bash
sudo sh -c 'setsid nohup python3 -I /root/p0-evidence/collect_p0_evidence.py --out /root/p0-evidence --sample-seconds 600 > /root/p0-evidence/run.log 2>&1 < /dev/null &'
sleep 10; sudo sh -c 'tail -n 5 /root/p0-evidence/*.progress.log'
```

You should see timestamped `step …: started` / `done` lines. You can now log out;
the run continues on its own. To check on it later:

```bash
sudo sh -c 'tail -n 8 /root/p0-evidence/*.progress.log'
```

It is finished when the last line reads `finished: /root/p0-evidence/nexus-p0-evidence-<stamp>.json`.

- Runs at the lowest CPU and I/O priority (`nice 19`, `ionice` idle).
- `--sample-seconds 600` samples CPU/RAM/disk I/O for 10 minutes. Use `300` for a quicker run.
- Results are saved after every step in `<stamp>.partial.json`, so even an
  interrupted run leaves usable evidence. The complete `<stamp>.json` appears only at the end.
- Stopping it early is safe: `sudo pkill -f "[c]ollect_p0_evidence.py"`. Nothing needs cleaning up.

What it touches, so you can judge it:

| Activity | Load |
|---|---|
| `docker ps/inspect/image ls/volume ls/system df/logs/stats --no-stream` | Metadata only; `system df` makes Docker sum image, volume and build-cache sizes |
| Metadata walk of data volumes and `/opt/nexus-trading-bot`, `/home/ubuntu`, `/root`, `/var/log`, `/tmp`, `/var/lib/docker/containers` | File names and sizes only, capped at 200,000 entries / 60 s each, 8 min total |
| SQLite: header read + about 100–300 single-row lookups per table | Opened `mode=ro&immutable=1`: no locks, no `-wal`/`-shm` access, no writes; never a full-table scan |
| HTTP to each API container | 5 × `/health`, 1 × `/status/public` (the app's own cached public status) |
| Logs | Last 60 min of nginx access logs (counts per path only) and last 24 h of app logs (keyword counts and `[startup]` lines) |

What it never outputs: environment values other than an allow-list of
non-secret settings (`GIT_COMMIT`, feature flags, intervals), secret values (only
"set / not set"), balances, P&L, client IP addresses, or log lines other than
`[startup]` lines.

## 3. Bring the result back

```bash
sudo sh -c 'cp /root/p0-evidence/nexus-p0-evidence-*Z.json /root/p0-evidence/*.progress.log /tmp/' \
  && sudo chown ubuntu /tmp/nexus-p0-evidence-*
df -h /                                                 # free space after (should be unchanged)
```

If the run was interrupted, copy `/root/p0-evidence/*.partial.json` instead; it
holds everything collected up to that point.

On your computer: `scp ubuntu@<vps-address>:/tmp/nexus-p0-evidence-* .` and
upload the files to the Claude session. I then run:

```bash
python3 validation/p0-vps-evidence/analyze_p0_evidence.py nexus-p0-evidence-<stamp>.json --repo . > report.md
```

The JSON contains container/image IDs, file paths, instance IDs, symbols and
strategies, table row estimates and request counts per endpoint. Review it
before sharing if you prefer; nothing in it is a credential.

## 4. Optional: growth measurement

Run step 2 again about 24 hours later with `--sample-seconds 60`. Comparing the
two files gives measured daily growth per database, backup folder and volume.
