# P1-C result: Docker images and build cache cleaned up (2026-10-23)

**Done at 14:27:56 UTC.** Disk `/`: **18.6 GB → 38.2 GB free** of 102.9 GB. Production was not
restarted: `nexus-trading-bot-app-1` stayed "Up 5 days (healthy)". No container, volume or data was
touched.

Run through the approval-gated **VPS operations** workflow from branch
`claude/beautiful-faraday-0nbitn` (commit `ac040c0`, script `ops/vps/operations/docker-cleanup.sh`).
The owner approved each run.

| Step | GitHub run | Result |
|---|---|---|
| `plan` (read-only) | [#6](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39969062790) | 20 old production app images to remove (at least 10.55 GB); build cache 11.96 GB |
| `apply` | [#7](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39969283144) | 20 removed, 0 refused; build cache 11.96 GB removed; 19.6 GB freed |

**Kept**
- `guardian-215c96c…`: production, running
- `…journal-gate-93f5f2d…` (Oct 7) and `merged-354d572…` (Oct 4): rollback
- `…q5nse7-724927c…`: used by the stopped `pa-replay-2025` container
- every staging, CI and base image

**Removed:** 20 production app images built Sep 24 – Oct 3, none used by any container.

The freed space (19.6 GB) is a little below the estimate (10.55 + 11.96 GB). Docker's containerd
image store may release some remaining layer data later, during its own garbage collection.

**Effect:** the next build of the app on the server starts without a build cache, so it takes a few
minutes longer. There is no other effect.
