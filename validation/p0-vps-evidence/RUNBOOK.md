# P0 VPS evidence collection — operator runbook

Read-only. Takes about 15 minutes. Run it from your existing trusted SSH session.
It does not deploy, restart, exec into containers, change configuration, delete,
prune, vacuum, checkpoint or write anywhere except one output file you choose.

## 1. Get the exact script onto the VPS

Do **not** `git pull` inside `/opt/nexus-trading-bot`: that is the production
checkout and must not change. Copy the single file instead.

On your own computer, in any clone of the repository:

```bash
git fetch origin claude/beautiful-faraday-0nbitn
git show origin/claude/beautiful-faraday-0nbitn:validation/p0-vps-evidence/collect_p0_evidence.py > collect_p0_evidence.py
sha256sum collect_p0_evidence.py
#   expected: ef243cdfcf65091a7ae67ee9b12f9af10fa0e60fd8086f7b1d7c4fd650494097
scp collect_p0_evidence.py ubuntu@<vps-address>:/tmp/
```

On the VPS:

```bash
sudo install -d -m 700 /root/p0-evidence
sudo install -m 600 /tmp/collect_p0_evidence.py /root/p0-evidence/
sha256sum /root/p0-evidence/collect_p0_evidence.py     # must match the value above
df -h /                                                 # note free space before
```

## 2. Run it

```bash
cd /root/p0-evidence
sudo python3 collect_p0_evidence.py --out /root/p0-evidence --sample-seconds 600
```

- Runs at the lowest CPU and I/O priority (`nice 19`, `ionice` idle).
- `--sample-seconds 600` samples CPU/RAM/disk I/O for 10 minutes. Use `300` for a quicker run.
- Prints the path of one JSON file when finished (typically under 3 MB).
- Ctrl-C at any time is safe; nothing needs cleaning up.

What it touches, so you can judge it:

| Activity | Load |
|---|---|
| `docker ps/inspect/image ls/volume ls/system df/logs/stats --no-stream` | Docker computes container and volume sizes from metadata |
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
sudo cp /root/p0-evidence/nexus-p0-evidence-*.json /tmp/ && sudo chown ubuntu /tmp/nexus-p0-evidence-*.json
df -h /                                                 # free space after (should be unchanged)
```

On your computer: `scp ubuntu@<vps-address>:/tmp/nexus-p0-evidence-*.json .` and
upload it to the Claude session. I then run:

```bash
python3 validation/p0-vps-evidence/analyze_p0_evidence.py nexus-p0-evidence-<stamp>.json --repo . > report.md
```

The JSON contains container/image IDs, file paths, instance IDs, symbols and
strategies, table row estimates and request counts per endpoint. Review it
before sharing if you prefer; nothing in it is a credential.

## 4. Optional: growth measurement

Run step 2 again about 24 hours later with `--sample-seconds 60`. Comparing the
two files gives measured daily growth per database, backup folder and volume.
