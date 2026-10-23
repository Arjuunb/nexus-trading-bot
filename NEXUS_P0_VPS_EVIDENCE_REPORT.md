# Nexus Proposal 01 — P0 Read-Only VPS Operational Evidence

**Date:** 2026-10-22 · **Phase:** P0 only · **Baseline:** `NEXUS_P01_PERFORMANCE_AND_RETIREMENT_AUDIT.md`, `validation/p01-audit/`
**Status:** evidence collector built, tested and rehearsed against a live app; **the VPS run itself is pending**,
because the VPS cannot be reached from this environment. Nothing on the VPS was touched.

No deployment, source-code change, configuration change, restart, deletion or database scan was made.
New files are evidence tooling and documentation only, under `validation/p0-vps-evidence/`.

Labels: **[REPO]** recorded in this repository's history or docs · **[CODE]** read in the deployed-lineage
source (`215c96c`) · **[LOCAL]** measured in the sandbox · **[PENDING]** needs the VPS run.

---

## 0. Summary

1. **VPS access from here is blocked.** The environment's network policy denies `trade-logx.com`
   (HTTPS CONNECT refused) and `57.129.155.230:22`, and no SSH identity exists in this session.
   Earlier Task 6 work hit the same limit. The host-level evidence therefore has to be gathered from
   your trusted SSH terminal, as all previous VPS evidence was.
2. **I built a read-only collector for that run** (`validation/p0-vps-evidence/collect_p0_evidence.py`)
   plus an analyzer that turns its output into the evidence report and settles B1–B8 automatically.
   It is validated: 15/15 tests, size estimates within 3.5% of SQLite's exact page counts, and a
   rehearsal against a live app (24 databases, 1.0 s, zero files created or changed, app healthy).
3. **Repository history already answers part of P0** — five earlier on-the-box measurements show the
   **PA and SMC lab runtimes were the main CPU and memory consumers** on the VPS (97–157% CPU, memory
   exhaustion after five hours). One root cause they named is **still present** on the deployed
   lineage: the SMC runtime rebuilds its full 800-bar analysis every 5 s regardless of whether a new
   candle has closed. [REPO] [CODE]
4. **B1 is one command away**, because `scripts/deploy.sh` tags every production image as
   `nexus-trading-bot-app:<branch>-<commit>` and bakes `GIT_COMMIT` into it. **B2 is at risk**: the
   deployment guide tells the operator to run `docker image prune -a` after deploys, which deletes every
   unused image, including the previous app image needed for rollback. [REPO]
5. **B8 is confirmed at the documentation level**: the in-app nightly backup writes to the same volume,
   and the only manual procedure in the guide writes to `/root/tradexa-backups` on the same host.
   No off-host copy exists in code or docs. Runtime confirmation is [PENDING].

**Recommended next approved action (approved 2026-10-23):** run the collector on the VPS, detached,
per `validation/p0-vps-evidence/RUNBOOK.md`, and send me the JSON. The first attempt (Oct 23 09:44 UTC)
was cut off by an SSH connection reset before it wrote output; see §1.1. Free disk was 18 GiB on
Oct 23, down from 32 GiB on Oct 19.

---

## 1. Access attempt (facts)

| Attempt | Result |
|---|---|
| TCP `57.129.155.230:22` from this sandbox | Unreachable |
| `GET https://trade-logx.com/health` (and `www.`, `api.`) | Denied by the environment's outbound network policy |
| SSH identity in this session | None |
| GitHub (repository, PRs, Actions) | Reachable — used below |

No request reached the VPS.

### 1.1 Operator run log

| When (UTC) | What happened | Observed by the operator |
|---|---|---|
| 2026-10-23 09:44 | Collector downloaded from the pinned commit `f9f51d9`; checksum `OK`; started in the foreground | Login banner: Ubuntu **26.04.1 LTS**, kernel **7.0.0-34**, load **4.08**, memory **58%**, swap 0%, `/` **81.7%** of 95.85 GB, "System restart required". `df -h /`: **96G size, 79G used, 18G free (82%)** |
| 09:44–10:00 | SSH session ended with `Connection reset by peer` during the run | The collector printed nothing while sampling and wrote output only at the end, so it stopped with the session and left no file |
| 10:00 | Reconnected; no output file present | Load **2.96**, memory **59%**, `/` 81.5%; `df -h /` still **18G free** — the interrupted run consumed no disk |

Free-disk trend from all readings so far: 32 GiB (Oct 19) → 28 GiB (Oct 20) → ~19 GiB (Oct 22) →
**18 GiB (Oct 23 09:44)**. The fall slowed to about 1 GiB over the most recent day.

Fix (commit `27bba32`, SHA-256 `4a2a1382…80c74`): the collector now ignores hang-up signals, survives a
vanished terminal, logs timestamped progress to `<stamp>.progress.log`, saves partial results after every
step, and is started detached (`setsid nohup`) per the updated runbook. It also no longer asks Docker for
per-container root-filesystem sizes, which made the daemon walk every image layer. A new test reproduces
the failure (hang-up plus closed terminal mid-run) and the run completes with a full file.

## 2. Blocker status

| ID | Blocker | Status now | What is already known | What settles it in the VPS run |
|---|---|---|---|---|
| B1 | Production source revision | **PENDING** (method ready) | `deploy.sh` tags images `<branch>-<commit>` and sets `GIT_COMMIT`; `/health` returns it. Commit `215c96c` (Oct 9) says the deployed runtime was then `93f5f2d`. The production container started Oct 17. [REPO] | Image tags + `GIT_COMMIT`, `/health` deploy info, and **git blob fingerprints of 17 source files read from the running container**, matched against every commit. Locally this identified `215c96c` and `f8778ed` exactly. [LOCAL] |
| B2 | Rollback images retained | **AT RISK** | `DEPLOYMENT_VPS.md` instructs `docker image prune -a` once new containers are healthy. [REPO] | Full image list with tags, creation dates and `GIT_COMMIT` per image |
| B3 | Per-container CPU/RAM/I/O | **PENDING** | Earlier on-the-box measurements in §3 | cgroup v2 sampling of every container for 5–10 min, host CPU busy/iowait/**steal**, PSI, disk I/O, top app threads |
| B4 | 17.16 GB staging PA file by table | **PENDING** (method validated) | Staging lacks the five PA journal fixes; production's file was 78% `pa_journal_revisions` on Sept 15. [REPO] | Per-table row and size estimates, plus the sampled share of duplicate-revision reason codes and a rowid→date growth curve |
| B5 | Cause of the ~13 GiB disk drop | **PENDING** | Candidates: nightly backups (7 full copies, same disk), Task 6 snapshot copies, preloaded images, Docker build cache. [REPO] | `docker system df` (images, build cache, volumes), volume and host-directory walks, **bytes by last-modified day**, 25 largest files per location |
| B6 | PA/SMC symbols, timeframes, modes | **PENDING** | — | Latest `pa_sessions` / `smc_sessions` rows (no balances) |
| B7 | Ledger backend in production | **PENDING** | Production volume name `nexus-migration-tradexa-data-cutover-20260913`; the guide documents a SQLite→Supabase migration. [REPO] | `[startup] ledger backend = …` log line; presence of `SUPABASE_URL` (yes/no only); `ledger_mirror.db` existence |
| B8 | Off-host backup and restore test | **CONFIRMED GAP in code/docs**; runtime [PENDING] | In-app backup: same volume, keep 7. Manual guide: same host. [CODE] [REPO] | Backup listings, manifest errors, truncated-copy detection by header, cron/systemd schedules classified by keyword |

## 3. Earlier production measurements recovered from history [REPO]

These were measured on the VPS by previous sessions and recorded in commit messages.

| Date | Measured on the VPS | Cause found | Fix | In production lineage | In staging (`f8778ed`) |
|---|---|---|---|---|---|
| Sept 11 (`cbf47ae`) | Host **97% CPU**, SSH locked out; app container **157% CPU** hours earlier. Commit describes a **two-core** VPS | PA and SMC lab supervisors tick every 5 s; each SMC tick rebuilds an 800-bar structure engine and nine-candidate ladder — 59 of 60 ticks recompute an unchanged 5m result | Made intervals tunable (`HUB_SMC_POLL`, `HUB_PA_POLL`, default 5). Commit states the real fix (cache against the last closed candle) was left for later | Yes | Yes |
| Sept 13 (`e824bfa`) | Thread dump: app "burning six cores", stream threads queued on one lock | Quote hand-off took the market hub's global lock | Lock removed from quote path | Yes | Yes |
| Sept 14 (`2bbcc2f`) | Memory ceiling hit after **5 h**; **~500 MB read and ~500 MB written every 30 s**, a core at **104%**, memory **+30 MB/s**. Ending two open lab sessions → flat memory, **1–7% CPU** | Lab `state()` re-read every candidate/order/funding row on each tick and each `bot-status` poll | Bounded reads | Yes | Yes |
| Sept 15 (`42de6d0`…) | `pa_journal_revisions`: **72,443 rows, 690 MB of an 888 MB** file; lab refused orders on write timeouts | Duplicate journal revisions from drifting counters | Five fixes + audited prune tool | Yes | **No** |
| Oct 1 (`536b3bf`) | Thread sample: ~10 request threads re-reading a year of candles; Journal Recorder **~1 min per pass** waiting for the interpreter | Full-series reads for last-n candles (99.8 ms → 1.3 ms) | Bounded read | Yes | **No** |

Confirmed in the deployed source [CODE]: `SMCStrategyLabRuntime._run` still calls `tick()` every
`poll_seconds` while a live session exists, and `tick()` always calls `live_visual_state(limit=800)`;
there is no check for "no new closed candle since the last tick". The closed-candle callback already
triggers a tick on each close, so the timer path is redundant most of the time.

The CPU-count discrepancy (two cores on Sept 11, "six cores" on Sept 13) suggests the VPS was resized;
the collector records `nproc` and host memory.

## 4. The collector

`validation/p0-vps-evidence/collect_p0_evidence.py` — Python 3 standard library only, run as root on the
host. Runbook: `validation/p0-vps-evidence/RUNBOOK.md`. Expected SHA-256 is printed there.

**What it collects**

| Area | Items |
|---|---|
| Identity (B1, B2) | Containers (state, start time, restarts, OOM, memory limits, mounts, compose labels, allow-listed env values), all images with tags and `GIT_COMMIT`, production checkout HEAD and modified-file count (no fetch, no lock), git blob fingerprints of running source via `/proc/<pid>/root` |
| Disk (B5) | `df` bytes and inodes, `docker info`, `docker system df`, bounded metadata walks of every Docker volume and of `/opt/nexus-trading-bot`, `/home/ubuntu`, `/root`, `/var/log`, `/tmp`, container log directories; bytes by modified day; largest files |
| Resources (B3) | `/proc` + cgroup v2 samples: per-container CPU, memory current/peak, anon, I/O bytes, pids, OOM kills; host busy/iowait/steal, MemAvailable, swap, PSI, disk throughput/utilisation; production app's busiest threads |
| App performance | nginx access logs (last 60 min): requests/min, top paths, error paths incl. 499/504; 5× `/health` timing; `/status/public` (component states, 90-day uptime, incidents); `[startup]` lines; 24 h log keyword counts (tracebacks, locked, disk full) |
| Databases (B4, B6, B7) | Every SQLite file: header (pages, free pages, truncation), WAL/SHM sizes, open file descriptors; per-table row and size estimates incl. indexes; latest row times; instances, lab sessions, Guardian heartbeat, Journal Recorder last pass (no balances) |
| Backups (B8) | In-app `backups/` and `/root/tradexa-backups`: snapshots, sizes, manifests, errors, encryption flag, header-consistency of each copied database; restore functions present in the running image; cron/systemd schedules classified by keyword only |

**Safety design**

- Docker access goes through an allow-list: `ps`, `inspect`, `logs`, `info`, `version`, `image ls/inspect`,
  `volume ls/inspect`, `system df`, `stats --no-stream`. `exec`, `run`, `rm`, `prune`, `cp`, `restart`,
  `compose`, etc. are refused before anything runs (tested).
- SQLite files are opened `mode=ro&immutable=1`. Local testing showed this is the only read mode that
  leaves a WAL database untouched: with the app connected, `readonly_shm=1` modified the `-shm` file;
  with the app's connections closed, plain `mode=ro` **created** both `-wal` and `-shm`, and
  `readonly_shm=1` created `-wal`. Run as root on the VPS, a created sidecar would be root-owned and
  could stop the app writing that database. Cost: readings reflect the last checkpoint (the WAL size
  is reported with every file). Evidence: `validation/p0-vps-evidence/validation-runs/sqlite-open-mode-check.txt`.
- No full-table scans: row counts come from `MIN/MAX(rowid)` plus 48–256 single-row lookups per table,
  each query capped at 2 s, each database at 30 s.
- Lowest CPU and I/O priority; metadata walks capped (200,000 entries / 60 s each, 8 min total).
- Output never contains secret values, balances, P&L, client IPs or log text other than `[startup]` lines.
- Writes only its output JSON, refuses to write inside a data volume or Docker storage, and checks free
  space first.

**Validation [LOCAL]**

| Check | Result |
|---|---|
| Unit and safety tests (`test_p0_evidence.py`) | **15/15 pass** (incl. dropped-SSH-session reproduction) |
| Live WAL database: bytes of `.db`, `-wal`, `-shm` before/after inspection | Identical; no file created |
| Concurrent writer during inspection | 0 lock/busy errors |
| Size estimates vs exact `dbstat` (150 MB synthetic PA file, real schema) | `pa_journal_revisions` 0.999×, `pa_evaluations` 0.998×, `pa_activity` 1.035×, all tables + indexes 1.021× |
| Duplicate-revision share from sample | 0.78 sampled vs 0.80 true |
| Source fingerprint → commit | `215c96c` and `f8778ed` matched exactly, lineage identified correctly |
| Rehearsal against the running Guardian-lineage app | 24 databases in 1.0 s, 0 errors, 0 files created, 0 ownership changes, app healthy after; instances read from `ledger.db` and `adaptive_lab.db` |
| Full run without Docker | Completes in 13 s, failed steps recorded, nothing crashes |

**Known limits:** Docker's own size calculations (`inspect --size`, `system df`) run inside `dockerd`
at normal priority (metadata only); values in young WAL-only tables (e.g. a fresh Guardian heartbeat)
can be behind by up to one checkpoint; `/status/public` exists only on the Guardian lineage.

## 5. Protected dependencies (item 6)

Confirmed in the deployed source [CODE]; the collector confirms presence, size, open handles and latest
activity of each store at runtime [PENDING].

| Asset | Stores that must be preserved | Who reads them |
|---|---|---|
| Trading Instances (3 paper instances) | `ledger.db` (or Supabase + `ledger_mirror.db`), `decisions.db`, `cycles.db`, `skipped.db`, `account.db` | Instance manager, Guardian (integrity `MAIN`), Journal Recorder, Calendar |
| Adaptive MTF Trend Pullback | `adaptive_lab.db`, `adaptive_lab_decisions.db`, `adaptive_lab_journal.db`; strategy `adaptive_trend_pullback` | Adaptive lab manager, Guardian (`ADAPTIVE_LAB`), Journal Recorder, Calendar |
| SMC Agent | `smc_strategy_paper.db`, `smc_agent_journal.db`, `smc_agent_policy.json` | SMC runtime (Agent subclasses it), Guardian, Journal Recorder, Calendar |
| Guardian | `guardian.db` (append-only, delete-blocked) | Guardian only |
| Historical journals | `trade_records.db`, `journal.db`, JSON memory/learning stores | Journal, Guardian, Calendar |
| Price Action history (retain) | `price_action_paper.db`, `price_action_research.db` | PA runtime, Guardian, Journal Recorder, Calendar |

The P01 retirement audit script's protected list still omits `trade_records.db`, `guardian.db`,
`adaptive_lab*.db` and `smc_agent_journal.db` (P01 finding, unchanged).

## 6. Hypotheses the VPS run will confirm or refute

| # | Hypothesis | Deciding evidence |
|---|---|---|
| H1 | Production runs a Guardian-lineage commit between `93f5f2d` and `215c96c` or a later local commit | Fingerprint match + image tag |
| H2 | `pa_journal_revisions` is most of the 17.16 GB staging file, mostly `MATERIAL_EVIDENCE_CHANGED` | Table estimates + sampled reason codes |
| H3 | The disk drop since Oct 19 is mainly Task 6 snapshot copies, staging backups and images/build cache, not production growth | Bytes by day per location; `docker system df` |
| H4 | The SMC and PA lab runtimes remain the largest steady CPU consumers in the production app | Container CPU + app thread shares + lab session modes |
| H5 | No earlier production image is retained | Image list |
| H6 | Production backups exist only on the VPS | Backup listings + schedules |

## 7. Recommended next approved action

1. **Run the collector on the VPS now** (P0, read-only) following `RUNBOOK.md`; send me the JSON.
   I will analyze it, commit the factual evidence report, and update B1–B8.
2. While it runs, avoid deploys and image pruning, so B1/B2 reflect the current state.
3. After the evidence is in, I will propose the P1 disk-safety step with exact items and byte counts
   for your approval. Nothing beyond P0 will start without explicit approval.
