# Staging PA database: verified backup and re-check (2026-10-23)

**Verdict: SAFE FOR SEPARATE DELETION APPROVAL** (run #14, 16:06 UTC). Nothing was changed.
- This is not permission to delete.
- `delete` is still disabled in the deployed script.
- Deleting needs the owner's explicit approval and a separately reviewed release that enables it.

## Backup ([run #10](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39977455336), started 15:28:58, finished 15:45:52 UTC)

| Step | Result |
|---|---|
| Source checksums | `price_action_paper.db` `7a38c7701849252c…`, `-wal` `9802286b99d0be7c…`, `-shm` `b2eef071c18cbdfe…` |
| Archive | `/var/lib/nexus-ops/pa-backup/price_action_paper.tar.zst`, **2.19 GB**, SHA-256 `76df669799158575…` (zstd -3) |
| Source unchanged while packing | yes: inode, size and nanosecond timestamps compared |
| Restore test: files | all 3 restored files byte-identical to the originals |
| Restore test: WAL | SQLite applied **341** committed WAL frames, exactly the number the read-only check found |
| Restore test: integrity | `PRAGMA integrity_check` **ok**, 0 foreign-key violations, 26 tables, **10,451,986 rows** |
| Cleanup | temporary restored copy deleted; free space **36.01 GB** |

The status checks were runs [#11](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39978692180) and [#12](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39979712038) (both "still running") and [#13](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39983586405) (RESULT: OK).
The backup is on the VPS disk only. An off-server copy of the 2.19 GB archive is optional.

## Re-check ([run #14](https://github.com/Arjuunb/nexus-trading-bot/actions/runs/39983692709), 16:06 UTC, commit `99340a0`)

All gates passed:
- Staging API stopped, and every user of the volume stopped. The research worker, postgres and
  ingress run but do not mount the volume.
- 219 processes scanned, 0 unreadable, no open files in the volume.
- 3 target files: regular, one link each, inside the staging volume.
- Schema confirmed: 14 `pa_*` tables, 8 `v2_*` tables, no SMC tables.
- WAL: 341 committed frames exist only in the WAL, and the verified backup's restore test applied
  exactly 341.
- Production isolation: separate volume. Production's `price_action_paper.db` (0.93 GB, inode
  1254490) is not a target. Production is healthy: workers 3 of 3, candles current, database
  recording.
- Backup verified: the archive SHA-256 was recomputed and matches, the source files are unchanged
  since the backup, and the restore test passed.

**Scope if deletion is approved later:**
- Files:
  - `/var/lib/docker/volumes/nexus-remote-stage-rc3-28acdbc_api-state/_data/price_action_paper.db`
    (17.97 GB, inode 2342064)
  - `…/price_action_paper.db-wal` (0.07 GB, inode 2342065)
  - `…/price_action_paper.db-shm` (inode 2342066)
- It would free **18.05 GB**, about **15.86 GB net** after keeping the 2.19 GB backup.
- Excluded and kept: `smc_strategy_paper.db` (15.67 GB) and its sidecars, all journal, account,
  decision, broker and market files, and the Task 6 recovery folder.
