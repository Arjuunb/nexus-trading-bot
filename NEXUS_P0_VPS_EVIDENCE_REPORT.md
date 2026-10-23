# Nexus Proposal 01 — P0 VPS Operational Evidence Report

**Date:** 2026-10-23 · **Phase:** P0 (read-only) · **Baseline:** `NEXUS_P01_PERFORMANCE_AND_RETIREMENT_AUDIT.md`
**Evidence:** collector run on `vps-46469a44`, 2026-10-23 09:44:18 → 09:54:57 UTC, every step succeeded.
Collector `f9f51d9` (SHA-256 `ef243cdf…94097`); evidence file 562,564 bytes, SHA-256
`c8d99d147644889b373f2992006d55f6e44eac1c2cd7b6dc9dc8061b3a9f0309`. The raw evidence file is **not**
committed (public repository); this report summarizes it.

**Nothing was changed on the VPS.** No deployment, restart, configuration change, deletion or database
write. Free disk was 18 GiB before and after the run.

Labels: **[MEASURED]** from the evidence file · **[CODE]** read in the running commit's source ·
**[ESTIMATE]** derived, with method stated · **[HYPO]** needs further measurement.

---

## 0. Summary

1. **Production runs exactly `215c96c`** (Guardian lineage), confirmed three independent ways; staging runs
   exactly `f8778ed` (main lineage). 23 earlier production images are retained for rollback. [MEASURED]
2. **Staging is the dominant load and the disk-growth source.** The staging API sits at its 1-CPU and
   1.5 GiB limits, reads **~220 MiB/s** from disk continuously (51.2 TB read since Oct 18), and its two lab
   databases grew to **31 GiB in 6.5 days**. 93% of each file is one append-only table,
   `v2_source_evidence`, which the staging code writes an `EQUITY_OBSERVED` row into **on every market
   quote**. Production does not have that code. [MEASURED] [CODE]
3. **Production's Python process is saturated.** It uses ~1.07 CPU cores continuously (a single Python
   process cannot do much more), with 62 threads; one thread takes 41.5% of it. The **Journal Recorder's last
   pass took 341 s** (interval 30 s) and **Guardian's last cycle took 21.5 s** (interval 15 s) — both run
   back-to-back. Two requests hit nginx's 90 s timeout (504) in the sampled hour. [MEASURED]
4. **Production has no recent backup.** In-app nightly backups are disabled (`HUB_DAILY_REPORT_HOUR=-1`
   also disables retention pruning); the newest in-app snapshot is **Sept 25**, the newest manual archive
   **Oct 7**. Every copy is on the same disk; nothing is copied off the server. [MEASURED] [CODE]
5. **Production memory is high and unbounded:** 4.16 GiB process memory, 7.88 GiB peak since Oct 17, no
   container limit, no swap, 5.2 GiB host memory available. [MEASURED]
6. **Disk:** 17.50 GiB free (82% used). Reclaimable without touching any database: ~12.4 GiB of unused
   Docker images plus an 11.1 GiB build cache. [MEASURED]
7. **All protected assets are running:** 3 of 3 trading instances, Adaptive MTF XRPUSDT bot, SMC Agent
   (session in `manual_approval`), Guardian, Journal. [MEASURED]
8. **Collector defects found and fixed** (§9): one query type scanned indexes (bounded to 2 s each, no
   locks); sparse-key tables were overcounted. My earlier statement that the first run "left no file" was
   wrong — the run completed; the copy command's `*` was expanded without root permission.

## 1. Blocker status

| ID | Blocker | Status | Evidence |
|---|---|---|---|
| B1 | Production source revision | **RESOLVED: `215c96c`** | Image tag `nexus-trading-bot-app:guardian-215c96c…`; `/health` commit `215c96c`, branch `codex/guardian-vps-integration`, deployed 2026-10-09T00:59Z; 17 source-file fingerprints match exactly; `/opt/nexus-trading-bot` HEAD `215c96c`, 0 modified files |
| B2 | Rollback images retained | **RESOLVED: retained** | 24 production app images; previous release `93f5f2d` (image `7aa91b705676`, Oct 7) and 22 older ones present |
| B3 | Per-container CPU/RAM/I/O | **RESOLVED (one 10-min window)** | §3 |
| B4 | 17.16 GB staging PA file by table | **RESOLVED (by subtraction)** | `v2_source_evidence` 15.48 GiB of 16.57 GiB (93%); §5 |
| B5 | Disk-space drop | **RESOLVED** | Staging lab databases (+31 GiB since Oct 16), Task 6 artifacts, images and build cache; §4 |
| B6 | Lab symbols/timeframes/modes | **RESOLVED** | §8 |
| B7 | Production ledger backend | **RESOLVED: Supabase** | `[startup] ledger backend = SupabaseLedger (Supabase active: True)` |
| B8 | Off-host backup and restore test | **CONFIRMED GAP** | No off-host copy or schedule; newest backup Oct 7; §7 |

## 2. Identity and rollback [MEASURED]

| Container | Image / revision | Started | Restarts | OOM | CPU limit | Memory limit |
|---|---|---|---|---|---|---|
| nexus-trading-bot-app-1 (production) | `215c96c` | Oct 17 17:56 | 0 | no | none | none |
| nexus-trading-bot-nginx-1 | nginx 1.27-alpine | Sept 25 | 0 | no | none | none |
| staging api | `f8778ed` (`customer-workspace-candidate`) | Oct 18 07:45 | 0 | no | 1 CPU | 1.5 GiB |
| staging research-worker / postgres / ingress | RC3 images | Oct 17–21 | 0 | no | 1 / 1 / 0.25 CPU | 512 / 512 / 96 MiB |
| `pa-replay-2025` (exited) | `724927c` (Sept 16) | — | — | — | — | **mounts the production data volume read-write**; restart policy `no` |

Production's container restarted on Oct 17 with the same image deployed on Oct 9 (not a redeploy).
Configuration seen (non-secret values): `HUB_PA_POLL=600`, `HUB_SMC_POLL=5`, `HUB_DAILY_REPORT_HOUR=-1`,
`HUB_AUTH_MODE=supabase`; master key and previous master key are set.

## 3. Resources — 10 minutes, 41 samples [MEASURED]

Host: 6 CPUs, busy **39%** average (≈2.3 cores; p95 40%), iowait 1.75%, steal 0.02%. CPU pressure (PSI
"some") 16–19%. Memory available **5.2 GiB** of 11.4 GiB; swap 0. Disk `sda`: **read 220.6 MiB/s**,
write 1.2 MiB/s, utilisation 26–32%. Load average 3.1–3.3.

| Container | CPU (% of one core) avg / p95 | Memory avg / peak | Process memory | Disk read / write | Since start |
|---|---|---|---|---|---|
| **production app** | **107.5 / 112.1** | 4.77 GiB / **7.88 GiB** | **4.16 GiB** | 0.14 / 0.81 MiB/s | 9.6 GB read, **502 GB written** (5.7 days) |
| **staging api** | **95.5 / 100.1 (capped)** | **1.50 / 1.50 GiB (at limit)** | 1.07 GiB | **220.5** / 0.36 MiB/s | **51.2 TB read**, 536 GB written (5.1 days) |
| staging research-worker | 14.8 / 16.3 | 41 MiB | 30 MiB | 0 / 0 | — |
| staging postgres | 2.8 / 3.1 | 112 MiB | 12 MiB | 0 / 0 | — |
| nginx, ingress, certbot | < 3 | < 40 MiB | — | 0 | — |

Inside production: 62 threads; busiest thread **41.5%** of the process's CPU, next four 7–8% each.
Application-recorded timings: **Journal Recorder last pass 341.3 s** (configured every 30 s, 0 errors);
**Guardian last cycle 21,450 ms** (configured every 15 s; 14,359 cycles since restart). Thread names are not
visible at OS level, so which component owns the 41.5% thread is [HYPO] — the Journal Recorder is the
strongest candidate.

## 4. Disk [MEASURED]

`/` 95.85 GiB: **78.33 GiB used, 17.50 GiB free**.

| Location | GiB | Contents |
|---|---|---|
| Docker volumes | 40.80 | **staging api-state 32.90** (PA db 16.57, SMC db 14.46, Task 6 recovery snapshot 1.79); production 5.60; **pre-cutover volume `nexus-migration-tradexa-data` 2.18 (not used by any container)**; staging postgres 0.10 |
| Docker images | 15.82 | 36 images; **12.44 GiB reclaimable** (not used by any container), incl. 22 production app images |
| Docker build cache | 11.14 | 159 entries (Docker reports 0 B reclaimable; actual savings from pruning are uncertain) |
| `/home/ubuntu` | 5.36 | manual backups 3.64; staging release 0.91; migration tarballs 0.43; Task 6 image zips 0.32 |
| `/var/log` | 0.51 | journald 0.40 |
| `/root` | 0.22 | Oct 6 manual backup |
| Not walked (OS, packages) | ≈4.4 | — |

**Growth** [ESTIMATE from row timestamps]:

| Store | Growth |
|---|---|
| Staging PA + SMC databases | 31.0 GiB since sessions started Oct 16 22:19 → **4.8 GiB/day average**; current rate [HYPO] (see §10) |
| Production live databases | **≈30 MiB/day** total (Guardian ≈12, PA ≈8, SMC ≈6, Adaptive MTF ≈2, SMC Agent ≈1) |
| Production stale stores | `decisions.db` Core V2 shadow table 1.09 GiB — no rows after Sept 18; `shadow_research.db` 0.97 GiB — no rows after Sept 10 |

Free-disk readings: 32 GiB (Oct 19) → 28 (Oct 20) → ~19 (Oct 22) → 17.5 (Oct 23 09:44). At 2.5–4.8
GiB/day the remaining space lasts roughly **4–7 days**. The login banners at 09:44, 10:00 and 10:11
(81.7% → 81.5% → 81.5%) suggest growth may have slowed; §10 measures it.

## 5. Databases [MEASURED] [ESTIMATE]

**Staging** (lab sessions PA and SMC, both BTCUSDT 5m automatic, started Oct 16 22:19):

| File | Size | `v2_source_evidence` (by subtraction) | Other large tables |
|---|---|---|---|
| `price_action_paper.db` | 16.57 GiB | **15.48 GiB (93%)** | `pa_evaluations` 1,775 rows × **260 KiB** = 452 MiB; `active_decision_receipts` 416 MiB; `pa_journal_revisions` 22,090 rows (210 MiB, 83% of sample `MATERIAL_EVIDENCE_CHANGED` — the duplicate-revision bug, unfixed on staging) |
| `smc_strategy_paper.db` | 14.46 GiB | **13.48 GiB (93%)** | `smc_evaluations` 1,780 × **394 KiB** = 687 MiB; receipts 310 MiB |

Cause [CODE, `f8778ed`]: `PaperBrokerV2.process_quote` (`execution/paper_broker_v2.py:651`) calls
`observe_equity`, which inserts an `EQUITY_OBSERVED` row into `v2_source_evidence` for every quote; the table
is append-only with delete-blocking triggers. Staging evaluation rows are ~150–260× larger than production's
(260–394 KiB vs 1.8–15 KiB). **This is the code line the PA-retirement branch is built on.**

**Production** (data volume 5.60 GiB; ≈4.0 GiB live databases):

| File | Size | Main content |
|---|---|---|
| `decisions.db` | 1.15 GiB | Core V2 shadow decisions 227,450 rows (1.09 GiB), stale since Sept 18 |
| `shadow_research.db` | 0.97 GiB | 19,152 shadow decisions, stale since Sept 10 |
| `price_action_paper.db` | 0.86 GiB | `pa_journal_revisions` 534 MiB (64%), `pa_journal_entries` 176 MiB, `pa_activity` 163k rows |
| `smc_strategy_paper.db` | 0.34 GiB | `smc_evaluations` 15,584 rows (240 MiB), `smc_activity` 181k rows (92% stream events) |
| `guardian.db` | 0.31 GiB | `guardian_events` 85,668 rows (217 MiB), growing ≈3,500 rows/day |
| others | < 0.1 GiB each | ledger (pre-Supabase, last trade Aug 5), journals, adaptive lab, trade records |

All database headers are consistent with their file sizes (no truncation).

## 6. Application performance [MEASURED]

- nginx, last 60 min: 360 requests (6/min — at most one dashboard tab open); **5 × 504 timeouts**:
  `/markets/watchlist` ×4, `/research/price-action/bot-status` ×1. Most 4xx were unauthenticated requests,
  including scanner probes (`/.git/config`).
- `/instances` responses average ~170 KB; `/strategy/performance` ~34 KB.
- `/health`: production 15, 21, 15, **422**, 136 ms; staging first call 3,783 ms then 9–45 ms.
- Public status page (90 days): API 99.985%, **workers 87.2%**, database 99.94%, market data 100%.
  Notable incidents: **all 3 workers down 48.7 h from Oct 15 17:18** (started with a 5-minute ledger
  outage; recovered with the Oct 17 restart); all workers down 22.6 h from Sept 25; API not answering
  3 min twice on Oct 18 without a restart.
- Production log keywords (24 h): 2 tracebacks; 0 locked, disk-full or persistence-blocked messages.

## 7. Backups and restore readiness [MEASURED] [CODE]

| Location | Contents | Newest |
|---|---|---|
| Production volume `backups/` | 3 sealed (encrypted) snapshots Sept 24–25, ~260 MiB each; archive of pruned PA revisions (514 MiB) | **Sept 25** |
| `/home/ubuntu/tradexa-backups` | manual `tar.gz` archives Sept 13, Sept 28, Oct 7 (×2), ~1.15 GiB each | **Oct 7** |
| `/root/tradexa-backups` | manual archive | Oct 6 |
| Staging | none (Task 6 recovery snapshot 1.79 GiB, Oct 21) | — |
| Off-host | **none**; only standard OS cron/timers scheduled | — |

- Nightly backups stopped because `HUB_DAILY_REPORT_HOUR=-1` stops `DailyTasks` entirely, which also stops
  retention pruning and the retune check. [CODE]
- **Do not simply re-enable it:** each nightly run copies every database (≈4 GiB now) and keeps 7, all on
  this disk — about 28 GiB, more than is free.
- Restore code in the production image: `restore_to` and `restore_check` present, sealed archives
  supported. Restoring a sealed archive requires the master key — its off-host safekeeping is unverified.
- Supabase holds the instance ledger; the provider's backup plan was not checked.
- Manual archives were not opened or verified (that would mean reading GBs).

## 8. Protected dependencies [MEASURED]

| Asset | State |
|---|---|
| Trading Instances | **3 of 3 running** (status page); stored in Supabase |
| Adaptive MTF Trend Pullback | XRPUSDT 5m **running**; BTCUSDT 5m stopped (since Sept 24); journal growing |
| SMC Strategy Lab + SMC Agent | session `8babc86251`, BTCUSDT 5m, **`manual_approval` — SMC Agent is the approver**; latest agent decision Oct 23 01:00 |
| Price Action Lab | session `e2838f758c`, BTCUSDT 5m automatic, active since Sept 13; latest fill Oct 23 09:27 |
| Guardian | running; latest event 09:35 |
| Journal | `trade_records` updated 09:33; records: PA_LAB 174, INSTANCE 192, ADAPTIVE_LAB 49, SMC_LAB 2, legacy 114 |

The production app holds 21 open handles to the Price Action database files and 12 to the SMC ones.

## 9. Corrections and collector defects

- **First run:** completed at 09:54:57. Its file was missed because `sudo cp /root/…/*.json` expands `*`
  as the unprivileged user. The current runbook uses `sudo sh -c '…'`.
- **Row-range query:** `SELECT MIN(rowid), MAX(rowid)` is not optimised by SQLite and scanned an index
  per table, stopped by the 2 s per-query cap (the whole database step took 14 s, lowest I/O priority, no
  locks). This contradicted the "no full-table scans" guarantee. Fixed in `2f07629`: two single-seek
  statements, and `rowid >= ?` seeks for samples (test: a 300,000-row table inspects within 50 ms per query).
- **Sparse keys:** a 1.5 MiB file was estimated at 1.8 billion rows; fixed (spacing from seek gaps;
  exact page counts used for files ≤ 64 MiB). Fixed collector SHA-256 `aaa0457d…41a9`; 18/18 tests pass.

## 10. Recommended next actions

**Immediate (P0, read-only, ~1 minute):** run the fixed collector once with `--skip-resources` (runbook §5)
to measure `v2_source_evidence` row count, row size, event types and its growth curve — that tells us whether
staging is still adding GiBs per day — and fetch the 10:12 run's file.

**P1 proposals — each needs your explicit approval; none started:**

| # | Action | Gain | Risk | Rollback |
|---|---|---|---|---|
| P1-A | **Off-host copy of production data now** (consistent SQLite backups or the app's sealed backup, copied to your computer; confirm the master key is stored off-host) | The newest backup is 16 days old and none exists on another machine | Low (reads, one temporary file) | n/a |
| P1-B | **Stop staging growth and load:** pause the two staging lab sessions, or stop the staging API container | Frees ~1 CPU core, 1.5 GiB RAM, ~220 MiB/s disk reads; halts staging growth | Pauses Task 6 staging acceptance; production untouched | Resume sessions / start container; data retained |
| P1-C | **Reclaim Docker space:** remove unused images except the rollback set (`215c96c`, `93f5f2d`, plus one older); prune build cache | Up to ~12 GiB (+ build cache, uncertain) | Slower next build; removed images rebuildable from git | Rebuild from commit |
| P1-D | Keep nightly in-app backups **off** until off-host copies and retention fit the disk | Avoids filling the disk | — | — |

**Deferred to later phases (evidence-backed):** raise `HUB_SMC_POLL` from 5 s (SMC decides on candle close
anyway); make the Journal Recorder incremental; fix Guardian's scans; investigate production's 4.2 GiB
process memory; decide on the stale 2.1 GiB of Core V2 and shadow-research data; remove or keep the
`pa-replay-2025` container (it mounts production data); port PA Lab retirement to the `215c96c` line — and
never deploy the `f8778ed` line to production until its per-quote evidence writes are bounded.
