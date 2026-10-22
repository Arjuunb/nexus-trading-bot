# Nexus Proposal 01 — Performance Audit, Feature Comparison & Safe Retirement Assessment

**Date:** 2026-10-22 · **Phase:** 01 Investigation and Planning · **Mode:** read-only, no deployment
**Status:** findings complete; **awaiting owner approval before any implementation.**

Nothing was disabled, deleted, migrated, deployed or reconfigured. No production or
staging system was contacted. Evidence files are in [`validation/p01-audit/`](validation/p01-audit/).

Evidence labels used throughout:

| Label | Meaning |
|---|---|
| **[CODE]** | Confirmed by reading the audited source |
| **[LOCAL]** | Measured in a controlled sandbox run (repeatable, but not the VPS) |
| **[VPS-OP]** | Operator-supplied VPS output already recorded in this repository |
| **[INFER]** | Inference from code + evidence, high confidence, not directly measured |
| **[HYPO]** | Hypothesis that needs a VPS measurement before acting on it |

---

## 0. Executive summary

1. **The retirement work is built on the wrong code line for production.** [CODE]
   The sidebar in this proposal (Guardian, Instance Visual Lab, SMC Agent, Adaptive MTF
   Lab, Calendar) exists only on the Guardian lineage (`93f5f2d` → `215c96c`,
   branch `codex/guardian-vps-integration`). The Price Action retirement commits
   `f9ebd32` / `c0c0cf0` sit on the main/Task 6 lineage. The two lines diverged on
   Sept 14: the retirement branch lacks **229 Guardian-lineage commits** (and carries 42
   strategy-intelligence/Task 6 commits production does not), so it has none of those
   modules. Deploying that retirement image to
   production as-is would remove Guardian, SMC Agent, Adaptive MTF Lab, Instance Visual
   Lab, Calendar, the Journal Recorder and the PA journal fixes. It must be ported, not
   deployed. The exact production source revision is still unrecorded (blocker B1).

2. **The 17.16 GB staging PA database is explained by missing fixes, not by PA itself.** [INFER]
   Staging's API runs `f8778ed` (main lineage). It lacks five PA journal fixes that the
   production lineage has (`42de6d0`, `264e96b`, `60df5fd`, `b80d9b8`, `36388b7`). Those
   commits record that `pa_journal_revisions` was writing a ~9.5 KB duplicate snapshot
   whenever a drifting bar counter changed: 690 MB of an 888 MB production file on
   Sept 15. Production has been at ~914 MB since — growth effectively stopped. Staging
   never received the fix.

3. **The shared VPS disk is the most urgent operational risk.** [VPS-OP] [HYPO]
   Production and staging run on one host. Free disk: **32 GiB (Oct 19) → 28 GiB (Oct 20)
   → ~19 GiB (Oct 22)**, about 4 GiB/day across three point readings. Candidate causes:
   nightly backups (7 full copies of every database, on the same disk), retained Task 6
   SQLite snapshot copies, and preloaded release images. Not yet attributed. The
   production app also has **no memory limit** and the host has **no swap**.

4. **Most continuous CPU is spent re-reading history, and it grows with time.** [CODE] [LOCAL]
   The Journal Recorder re-reads the full PA and SMC history every 30 s; Guardian scans
   unindexed or full tables every 15–60 s; PA `bot-status` rebuilds heavy state on every
   call. Measured on synthetic data with the exact schema: Guardian's PA reader costs
   **13 → 80 → 135 ms** per cycle at 10k/50k/100k rows; the Journal Recorder pass costs
   **0.18 → 0.93 → 2.0 s** every 30 s, partly while holding the PA account lock.

5. **The browser polls far more than it shows.** [CODE] [LOCAL]
   Every open tab sends **~89 requests/min from the page frame alone**, 30 of them to
   the PA and SMC `bot-status` endpoints that are only displayed on the two lab pages.
   An open Dashboard tab sends **~237 requests/min (~4/s)**, all into one Python process
   shared with the trading workers. The lab pages' own pollers keep running in hidden tabs.

6. **Retirement verdicts.**
   - **Price Action Lab — retire** (highest value, needs a proper port to the deployed line).
   - **Instance Visual Lab — keep.** Zero idle cost; it is the only place that draws a
     Trading Instance's chart with its strategy overlays, including your
     3-Candle Rejection · EMA 9/33 and PA S/R strategies.
   - **SMC Strategy Lab — consolidate the page, keep the engine.** SMC Agent is built on
     its runtime; retiring it would kill SMC Agent.

---

## 1. Scope, sources and method

### 1.1 Which code is deployed where

| Environment | Running source | Evidence |
|---|---|---|
| Production app | **Unverified.** Sidebar matches Guardian lineage `93f5f2d`…`215c96c`. Image `sha256:d4c51634…be523`, started 2026-10-17, volume `nexus-migration-tradexa-data-cutover-20260913`, memory limit **0** (unlimited) | [VPS-OP] `validation/task6-approved-4ece/vps-operator-baseline-20261020.json` |
| Staging API | `f8778ed` (main lineage), image `sha256:f2343e76…5397`, 1536 MiB memory + swap limit | [VPS-OP] `validation/task6-approved-0ded/vps-operator-image-identity-20261019.txt` |
| Staging worker | `28acdbc` | same |
| PA retirement | `f9ebd32`, `c0c0cf0`, `c13a439` — branches off main lineage, not deployed | [CODE] |

Primary audit target: **`215c96c`** (Guardian lineage). Differences on `f8778ed` are noted
where they matter.

### 1.2 Host baseline (operator readings, not load tests) [VPS-OP]

| Reading | Oct 19 | Oct 20 | Oct 22 |
|---|---|---|---|
| Host RAM total / available | 11,671 / 6,633 MiB | 11,671 / 6,180 MiB | — |
| Swap | 0 | 0 | — |
| Root disk (96 GiB) free | 32 GiB (67% used) | 28 GiB (72% used) | 20,395,585,536 B ≈ 19 GiB |
| Running containers | prod app, nginx, certbot; staging api, ingress, postgres, research-worker; `pa-replay-2025` exited | same | — |

Staging memory limits: api 1536 MiB, postgres 512 MiB (+512 swap), research-worker
512 MiB (+512), ingress 96 MiB. Production app and nginx: unlimited.

The Oct 22 free-disk figure comes from `docs/price-action-lab-retirement.md` on the
retirement branch; it is assumed to be the same root filesystem. Mixed units in the
sources (`df -h` GiB vs bytes) make the trend approximate.

### 1.3 What was measured locally [LOCAL]

Sandbox: 4 vCPU Intel Xeon 2.80 GHz, Python 3.12, throwaway data directory, dev auth.
**Outbound Binance was blocked**, so no websocket feed or candle processing ran. Each
configuration was booted 3 times; medians below.

| Configuration | Ready (s) | RSS (MB) | Threads | Idle CPU (% of 1 core) | Open data files |
|---|---|---|---|---|---|
| Deployed lineage `215c96c` | 8.6 (8.3–10.9) | 164.6 (164.6–172.6) | 26 | 0.38 | 39 |
| Retirement branch, PA enabled | 3.5 (3.4–4.5) | 142.1 (142.1–143.8) | 15 | 0.29 | 24 |
| Retirement branch, PA disabled | 3.2 (3.2–3.5) | 136.6 (136.6–136.8) | 14 | 0.25 | 18 |

Endpoint latency on **empty** databases was 3–10 ms for every route tested
(`/instances` 5.4, PA `bot-status` 9.8, SMC `bot-status` 9.3, `/guardian/status` 7.6 ms).

**What this shows:** the code's fixed footprint is small. Disabling the PA Lab saves
~5–7 MB, one thread and six file handles at idle. The real cost on the VPS comes from
**accumulated data** and **live feed processing**, which grow over time and could not be
reproduced here. Per-module CPU/RAM on the VPS cannot be separated from outside the
process: everything runs inside one uvicorn worker.

### 1.4 Limitations

- No VPS access: no production/staging CPU, RAM, I/O, query-latency or API-log samples.
- Production source revision and `compose.override.yaml` contents unknown.
- PA/SMC lab symbols, timeframes and modes on the VPS unknown; this decides how many
  market-data channels each lab holds.
- Synthetic SQLite benchmarks use a 3 KB payload estimate and a warm OS cache; real
  payload sizes and cold-cache I/O on the VPS will differ.
- Whether production uses the Supabase ledger or local SQLite is unknown; this changes
  the cost of `/strategy/performance`, the ledger mirror and the Journal Recorder.

---

## 2. Resource Usage Report

### 2.1 Always-on background work (runs with nobody viewing) [CODE]

All in one process. Intervals are code defaults; environment overrides on the VPS are
unknown.

| Thread / worker | Interval | What it does each time | Grows with history? | Module |
|---|---|---|---|---|
| Trading Instance workers (1 per running instance) + supervisor | per candle; supervisor 20 s | Strategy on closed candle, decision + cycle report writes | Bounded (cycles keep 5,000) | Trading Instances |
| **PA Lab runtime** (`autostart=True`) | poll 5 s + quote worker ~1 msg/s + per closed candle | Quote fills under account lock, per-tick journal capture, one `pa_evaluations` row + `pa_activity` row per candle | **Yes — no retention on any `pa_*` table** | **Price Action Lab** |
| **SMC Lab runtime** (`autostart=True`) | poll 5 s + quotes + per candle | Same pattern; agent decision rows in `smc_agent_journal.db` | **Yes** | SMC Strategy Lab + SMC Agent |
| Adaptive MTF Lab bot + supervisor | per candle; 20 s | Private paper bot (autostarts on XRPUSDT unless `HUB_ADAPTIVE_LAB_AUTOSTART=0`) | Ledger/decisions grow | Adaptive MTF Lab |
| Market-data hub channels | continuous | Per (symbol, timeframe): 2 Binance websockets, 1 reader thread, 1,500-bar REST reconcile, plus higher-timeframe child channels | No | Shared |
| **Journal Recorder** | **30 s** + wake on every execution | Re-reads **all** PA and SMC fills/orders/candidates, **all** `pa_evaluations` rows with JSON parse, all `agent_decisions`; projects into `trade_records.db` | **Yes — O(total history) per pass** | Journal |
| **Guardian** cycle | **15 s** | Telemetry reads of PA/SMC decision tables; rewrites all components; heartbeat meta writes; incidents/anomalies | Yes | Guardian |
| Guardian integrity | every 4th cycle (60 s) | `SELECT *` over `paper_trades`, `paper_executions`, `trade_records`, positions of 4 accounts | **Yes — full scans** | Guardian |
| Guardian event bus | 0.5 s flush | Appends to `guardian_events` (5 secondary indexes, delete-blocked, no retention) | **Yes** | Guardian |
| Candle sync | 300 s | Refetch stale candles: 8 symbols × 5 timeframes | No | Shared |
| Status monitor | 60 s | Probes, writes `status.db` | Slowly | Risk & Health |
| Outbound webhooks | 15 s | Delivery queue | No | Settings |
| Watchdog | 60 s | Engine/feed liveness | No | Shared |
| Monitor agent | 900 s | Backtest-vs-live deviation for legacy engine | No | Legacy |
| Weekly review scheduler | 900 s | Weekly journal reviews | Slowly | Journal |
| Daily tasks | poll 300 s; nightly | **Backup of every `.db`, keep 7, same disk**; retention prune; retune check | **Yes (backup size)** | Shared |
| Econ feed | 6 h | Calendar fetch | No | Shared |
| Audit exporter | 900 s | Only if `HUB_AUDIT_EXPORT_URL` set | No | Settings |

Not running by default in production compose: shadow research observer
(`HUB_RESEARCH_AUTOSTART=0`), legacy autonomous engine (only when no instance restored).

### 2.2 Continuous costs that scale with data [CODE] [LOCAL]

Synthetic benchmark using the exact `pa_evaluations` DDL from `215c96c`
(`validation/p01-audit/sqlite-benchmark-output.txt`):

| `pa_evaluations` rows | File size | Guardian PA reader (every 15 s) | Same with `updated_at` index | Row-value seek | Journal Recorder full pass (every 30 s) |
|---|---|---|---|---|---|
| 10,000 | 42 MB | 13.2 ms — full scan + temp sort | 1.8 ms | — | 176 ms |
| 50,000 | 212 MB | 80.4 ms | 5.2 ms | — | 932 ms |
| 100,000 | 424 MB | 134.6 ms | 11.4 ms | **0.03 ms** | 2,032 ms |

At 5-minute candles a single PA session writes 288 rows/day (~105k/year); at 1-minute,
1,440/day. The Journal Recorder figure is CPU-bound Python work repeated every 30 s, and
part of it (`project_decisions`) runs **inside the PA account's own lock** — the same
lock quote fills take. On the VPS's cold cache these numbers will be higher.

Other unbounded or full-history reads found:

- PA `bot-status`: `account.state()` (up to 500 activity + 500 evaluation payloads),
  three journal listings, performance summary, and a higher-timeframe context build —
  **every 4 s per open tab** because the page frame polls it (§2.4). Commit `b2ab220`
  records this route previously exceeding nginx's 90 s limit on a large journal (since
  reduced, but the per-call work still grows with history).
- `/strategy/performance`: summarises the full paper history on every call, uncached.
- Guardian integrity: `SELECT * FROM paper_trades / paper_executions / trade_records`.
- Calendar: re-collects all four ledgers including the full PA and SMC broker histories,
  cached 15 s.

### 2.3 Storage and database inventory [CODE] [VPS-OP]

About 30 SQLite files share one data directory (`/var/lib/tradexa`). Nightly retention
prunes only `ledger` logs/alerts/events, main `decisions` and `skipped` (20,000 rows).

| Store | Owner | Retention | Notes |
|---|---|---|---|
| `price_action_paper.db` | PA Lab | **None** (journal revisions are delete-blocked by triggers) | Prod **914.2 MB**, staging **17.16 GB** [VPS-OP]. Read by Guardian, Journal Recorder, Calendar |
| `price_action_research.db` | PA Lab | None | On-demand experiments |
| `smc_strategy_paper.db`, `smc_agent_journal.db` | SMC Lab / Agent | None | Read by Guardian, Journal Recorder, Calendar |
| `adaptive_lab.db`, `_decisions.db`, `_journal.db` | Adaptive MTF Lab | None (separate decision store is not in nightly prune) | **Protected evidence** |
| `guardian.db` | Guardian | **None; delete-blocked** | Copies every lab/instance decision trace; 5 indexes |
| `trade_records.db` | Journal | Records kept by design | Holds journal records projected from PA and SMC labs — **shared, must be kept** |
| `ledger.db` / Supabase | Trading Instances | Logs/alerts pruned nightly | Source of truth for instances |
| `cycles.db` | Instances/engine | Keeps 5,000 | Bounded |
| `market_data.db`, `market_data/` | Shared | None | Candle cache |
| `status.db`, `webhooks.db`, `audit.db`, `key_vault.db` | Ops/Settings | Varies | Small |
| `backups/<stamp>` | Daily tasks | **7 full snapshots, same volume** | Production: up to ~7 × (all DBs) incl. ~6.4 GB of PA copies alone [INFER] |

The stores inspected (runtime helper, Guardian, trade records) open in WAL mode. No
periodic `VACUUM` exists, so deleted rows leave free pages inside the file.

### 2.4 Web performance [LOCAL] [CODE]

**Bundle** (production build of `215c96c`):

| Asset | Size | Gzip | When loaded |
|---|---|---|---|
| App shell JS | 250 kB | 78 kB | Always |
| App CSS | 197 kB | 37 kB | Always |
| ECharts chunk | 681 kB | 232 kB | First page with a chart (Dashboard included). Already tree-shaken |
| PA Lab page | 64 kB | 19 kB | Only on that page |
| SMC Strategy Lab page | 34 kB | 10 kB | Only on that page |
| Instance Visual Lab page | 24 kB | 8 kB | Only on that page |

Pages are lazy-loaded, so retiring any of the three candidates **does not change initial
load time**. Bundle size is not the main cause of slowness.

**Polling per open tab** (`validation/p01-audit/poll-rates.txt`):

| View | Requests/min | Of which PA+SMC `bot-status` |
|---|---|---|
| Page frame only (header, footer ticker, profile menu, bell) | **88.5** | 30 |
| Dashboard | **236.5** (~4/s) | 30 |
| Price Action Lab (live) | 175.0 | 30 |
| SMC Strategy Lab (live) | 172.5 | 30 |
| Instance Visual Lab (excl. candle refresh) | 122.5 | 30 |

Specific problems:

- **Footer ticker polls both lab `bot-status` endpoints every 4 s on every page**
  (`components/layout/TickerBar.tsx`) but only displays them on the two lab pages.
- `/instances` is polled at 2–2.5 s by the header and Dashboard cards.
- PA Lab, Instance Visual Lab, Replay and the Paper Trading terminal use raw
  `setInterval` pollers that **keep firing in hidden tabs**; the shared `useLive` hook
  correctly pauses and backs off, these do not.
- Backtests, Replay, Strategy League and research runs execute **inside the API process**
  (one uvicorn worker), sharing Python's GIL with live instance workers.

### 2.5 Shared services (cannot be attributed to one module)

Market-data hub and channels; `strategy_visual_registry` (used by Instance Visual Lab,
`instance_status.py`, `strategy_trace.py`); `native_price_action` engine (PA Lab and the
PA-family Trading Instance strategies); `PaperBrokerV2`; lab event guard; Journal
Recorder; Guardian; nightly backup; Supabase ledger mirror (if Supabase is used).

### 2.6 Items that could not be attributed

- The ~13 GiB disk drop between Oct 19 and Oct 22.
- Production RSS and CPU (no limit set, never sampled).
- Which tables make up the 17.16 GB staging file (no per-table measurement exists yet).
- Whether the exited `pa-replay-2025` container holds a volume or data copy.

---

## 3. Feature Comparison Matrix

### 3.1 Requested comparison

CPU/RAM on the VPS could not be measured per module (single process). Values below are
the measured local footprint where available plus the code-derived workload class.

| Feature | CPU | RAM | Storage | API impact | Dependency risk | Recommendation |
|---|---|---|---|---|---|---|
| **Price Action Lab** | **High, always on**: runtime + 1/s quotes + per-candle writes; drives Journal Recorder (0.2–2 s/30 s) and Guardian reads (13–135 ms/15 s) [LOCAL synthetic] | ~5–7 MB fixed [LOCAL]; more with live state | **914 MB prod / 17.16 GB staging**, × up to 7 backup copies | `bot-status` every 4 s per tab on **every** page; lab page 175 req/min | Medium: Guardian, Journal, Calendar, backups, PA-family instance strategies share code | **Retire** (port first) |
| **Instance Visual Lab** | **None when closed** (GET-only router, no thread) | ~0 fixed | **None** (no own store) | Only while open: 122 req/min + candle reads (may hit Binance REST) | High if removed wholesale: Adaptive MTF Lab router imports it; registry feeds instance status | **Keep** (fix pollers) |
| **SMC Strategy Lab** | High, always on (same pattern as PA) | Not separable | `smc_strategy_paper.db` (size unknown) | `bot-status` every 4 s per tab on every page | **Critical: SMC Agent is a subclass of its runtime** | **Consolidate UI, keep engine** |
| **SMC Agent** | Rides on SMC runtime; agent journal writes | Not separable | `smc_agent_journal.db` | 5 s polls while open | Guardian, Journal, Calendar read it | **Keep; merge page with SMC Lab** |
| **Adaptive MTF Lab** | Always on (private bot, autostart) + supervisor 20 s | Not separable | 3 DBs | 2.5–5 s polls while open | Protected strategy evidence | **Keep (protected)** |
| **Backtesting** | **On-demand, in-process** (GIL contention with live workers) | Spikes per run | Small | 3–8 s polls while open | Low | **Optimise** (move heavy runs off the API process) |
| **Analytics** | On-demand; full-history `/strategy/performance` uncached | Small | None | 3 s polls while open | Low | **Optimise** (cache) |

### 3.2 All sidebar modules

| Module | Unique purpose | Overlap | Idle background work | Data to preserve | Business value | Recommendation |
|---|---|---|---|---|---|---|
| Dashboard | Platform overview | Aggregates others | None of its own | — | High | Keep; cut polling (237 req/min) |
| Guardian | Independent read-only observer, incidents, integrity | Partial with Risk & Health | **15 s cycle, 60 s full scans, unbounded event table** | `guardian.db` | High | Keep; optimise queries + retention policy |
| Trading Instances | Core paper execution | — | Workers + 20 s supervisor | Ledger, instance state, 3 paper instances | Core | Keep — do not touch |
| Instance Visual Lab | Instance chart with strategy overlays, gates, decision timeline | Partly Guardian Strategies / Journal Decisions (text only) | None | None | High for V2 | **Keep** |
| Strategy Studio | Build/edit strategies | — | None | Custom strategies | High | Keep |
| Live Trading | Live readiness + safety (locked) | — | None | Safety state | High (safety) | Keep |
| Price Action Lab | Isolated PA forward-paper session | PA strategies already run as Trading Instances; Instance Visual Lab has PA adapters | **Runtime, quotes, writes** | PA ledger, journal, evaluations (archive) | Low now | **Retire** |
| SMC Strategy Lab | SMC session control + chart | Same engine as SMC Agent; SMC Visual Lab (hidden) | Runtime, quotes, writes | SMC ledger | Medium | **Consolidate** |
| SMC Agent | Autonomous approval layer + decision audit | Same engine | Rides on SMC runtime | Agent journal | Medium | Keep (merged page) |
| Adaptive MTF Lab | Private Adaptive MTF bot + instance mirror | Strategy also runnable as instance | Bot + supervisor | **Protected** | High | Keep |
| Replay | Visual no-lookahead replay | Backtesting | None (in-process when used) | — | Medium | Keep; offload compute |
| Backtesting | Historical tests | Replay, Optimisation | None (in-process when used) | Research results | High | Optimise |
| Forward Validation | Forward vs backtest | — | None found | Validation records | High | Keep |
| Portfolio | Positions, allocation | Risk & Health | None | — | Medium | Keep; reduce 2.5 s polls |
| Analytics | Performance, AI insights | Portfolio | None | — | Medium | Optimise (cache) |
| Calendar | Realised P&L by day across sources | Analytics | None (15 s cache) | — | Medium | Keep; freeze PA history source on retirement |
| Journal | Canonical trade records | — | Journal Recorder 30 s | `trade_records.db` | High | Keep; make recorder incremental |
| Risk & Health | Risk, worker health, logs | Guardian | Status monitor 60 s | `status.db` | High | Keep |

### 3.3 Rankings

| Ranking | Order |
|---|---|
| Highest measured/derived resource use | 1 Journal Recorder full passes · 2 PA Lab runtime + its data · 3 SMC runtime · 4 Guardian scans · 5 Nightly backups (disk/I/O) |
| Greatest contribution to website slowness | 1 Page-frame polling of lab `bot-status` · 2 Dashboard 2–2.5 s polling · 3 Heavy uncached endpoints (`bot-status`, `/strategy/performance`) · 4 GIL contention from background scans and in-process backtests · 5 Hidden-tab pollers |
| Lowest value / highest duplication | 1 PA Lab · 2 SMC Strategy Lab page (duplicates SMC Agent page shell) · 3 SMC Visual Lab (hidden third SMC page) |
| Safest retirement opportunities | 1 Stop page-frame lab polling (UI only) · 2 PA Lab via flag (after port) · 3 Merge SMC pages (UI only) |
| Gain vs risk | Highest: ticker polling fix, Guardian PA query fix, PA retirement. Lowest: deleting PA data (high risk, disk-only gain) |

---

## 4. Retirement Recommendations

### 4.1 Price Action Lab — **Retire (after porting to the deployed lineage)**

| Question | Answer |
|---|---|
| 1. Unique purpose | An isolated PA forward-paper session with its own account, journal and governance. [CODE] |
| 2. Already done elsewhere? | **Yes.** `three_candle_rejection` (3-Candle Rejection · EMA 9/33), `price_action_rejection`, `price_action_flip_retest` and `pa_rulebook` are registered Trading Instance strategies, and Instance Visual Lab has visual adapters for all four. [CODE] |
| 3. Background work when unused? | **Yes**: autostart runtime, stream subscription, 1/s quotes, per-candle writes; also drives Guardian, Journal Recorder and Calendar reads. [CODE] |
| 4. What depends on it | Guardian (lab row, `PAEvaluationReader`, integrity source `PA_LAB`), Journal Recorder (`PALabProjector`), Calendar (`pa_lab` collector), page-frame ticker + Dashboard card, factory reset, shutdown, nightly backup, lab event guard, `/research/price-action/*` and `/research/pa-rulebook/*` routes. [CODE] |
| 5. Data to preserve | Whole `price_action_paper.db` (v2_* ledger, `pa_*` tables, journal entries + revisions) and `price_action_research.db`; PA rows already in `trade_records.db` and `guardian.db`; old backups containing PA files. |
| 6. Resource eliminated | Fixed: ~5–7 MB, 1 thread, 6 file handles [LOCAL]. Continuous: one runtime + its quote worker, per-candle writes, PA share of the Journal Recorder pass (0.2–2 s every 30 s at 10k–100k rows), Guardian PA reads (13–135 ms every 15 s), 30 req/min per open tab (with the ticker fix), possibly 1–3 market-data channels if its symbol/timeframe is unique. Disk: none until data is archived. [LOCAL synthetic + INFER] |
| 7. Effect on instances/strategies | None if done as specified: the shared `native_price_action` engine, PA-family instance strategies, `PaperBrokerV2` and market hub stay. Must be verified by test. |
| 8. Flag-controlled and reversible? | On the retirement branch yes (`PRICE_ACTION_LAB_ENABLED`). On the deployed lineage **no flag exists yet**. |

**Review of `f9ebd32` / `c0c0cf0` / `c13a439`:**

| Finding | Severity |
|---|---|
| Built on main lineage; deploying it replaces the Guardian lineage in production | **Blocker** |
| Does not handle Guardian's PA reader/integrity source, Journal Recorder's `PALabProjector`, Calendar's `pa_lab` collector, `pa_rulebook` router — none exist on main | **Blocker for a port** |
| Backup change only understands plain snapshot folders; the deployed lineage writes encrypted `.tlxb` archives when `HUB_MASTER_KEY` is set | High |
| Old snapshots containing PA files are retained indefinitely: disk use **rises** after retirement (up to 7 old + 7 new snapshots) | High on a 19 GiB-free shared disk |
| Audit scripts are correctly read-only, but their protected-store list omits `trade_records.db`, `guardian.db`, `adaptive_lab*.db`, `smc_agent_journal.db` | Medium |
| Audit scripts do not measure per-table size, so the 17 GB cannot be attributed | Medium |
| Retired routes return HTTP 410 cleanly; feature flag defaults to disabled; invalid values fail boot | Good |
| Retirement doc's own status: broader Mac run had 35 failures; Linux CI and browser runs unconfirmed | Open |

### 4.2 Instance Visual Lab — **Keep**

1. **Unique purpose:** the only view that draws a running instance's candles with the
   strategy's own zones, swings, structure breaks, FVGs and EMAs, its gate pipeline and
   a per-candle decision timeline. [CODE]
2. **Duplicated?** No. Trading Instances shows status, feed health and equity, with no
   price chart or overlays. Guardian Strategies and Journal Decisions show decision text,
   not the chart.
3. **Background work?** **None.** GET-only router, no thread, no store. [CODE]
4. **Dependents:** Adaptive MTF Lab's router imports its functions;
   `strategy_visual_registry` feeds `instance_status.py` and `strategy_trace.py`.
5. **Data:** none of its own.
6. **Saving if removed:** ~zero at idle; only while someone has it open.
7. **Instance impact if removed carelessly:** breaks Adaptive MTF Lab endpoints and
   instance status explanations.
8. **Fix instead:** make its pollers pause in hidden tabs; optionally move it into Trading
   Instances as a "Chart" tab. It is the right tool for checking your S/R rejection and
   EMA 9/33 instances visually, and it will matter for Trading Instances V2.

### 4.3 SMC Strategy Lab — **Consolidate the UI; keep the engine and data**

1. **Unique purpose:** control surface (market, timeframe, mode) and chart for the SMC
   forward-paper engine.
2. **Duplicated?** Its **page** overlaps with SMC Agent and the hidden SMC Visual Lab. Its
   **engine** is not duplicated: Strategy Studio builds strategies, Backtesting is
   historical, Forward Validation does not read it, Adaptive MTF is a different strategy.
3. **Background work?** Yes: autostart runtime, quotes, per-candle writes.
4. **Dependents:** SMC Agent (`AgentSMCStrategyLabRuntime` subclasses it), Guardian,
   Journal Recorder, Calendar.
5. **Data:** `smc_strategy_paper.db`, `smc_agent_journal.db`, policies file.
6. **Saving from merging pages:** UI only — removes one duplicated page and one
   page-frame poller once the ticker fix lands. Backend unchanged.
7. **Instance impact:** none.
8. **Plan:** one "SMC Lab" page with tabs: Control & Chart · Agent Decisions · Rules ·
   Pine Reference/Parity (absorbs hidden SMC Visual Lab). Keep `/research/smc/*` routes.
   If you later decide SMC research is finished, the engine can be paused via its session
   mode (signals-only / stopped) — no deletion needed.

---

## 5. Risk & Recovery Assessment

### 5.1 Backups

| Item | Finding |
|---|---|
| Mechanism | Nightly `sqlite3` backup API of every `.db` + `.json`, keep 7 [CODE] |
| Location | `<data dir>/backups` — **same volume, same disk** as live data. Not an off-host backup |
| Encryption | Deployed lineage seals snapshots when `HUB_MASTER_KEY` is set; unknown on VPS |
| Restore | `restore_to` writes into a chosen directory, never the live one; `restore_check` opens each DB. No recorded production restore test |
| Staging | Same job tries to copy the 17 GB file nightly; retirement doc records a **9.58 GB shortfall** for raw-snapshot recovery |
| Risk | A full disk on the shared host stops SQLite writes for **production and staging together**, including the three paper instances |

**Required before any PA data removal:** an off-host or separate-volume copy of
`price_action_paper.db` (WAL checkpointed into the copy), integrity check, row counts per
table, SHA-256, and a successful test restore into a scratch directory.

### 5.2 Rollback

- Production rollback image: `sha256:d4c51634…` is the running image; confirm it is
  retained and tagged before any deploy (blocker B2).
- Any PA retirement must be a **flag on the deployed lineage**, so rollback = set
  `PRICE_ACTION_LAB_ENABLED=true` and restart; no data is touched.
- Never roll production onto the main lineage: schema written by the Guardian lineage
  (`trade_records.db`, `guardian.db`, `adaptive_lab*.db`) is unknown to that code.

### 5.3 Impact on active instances and protected assets

| Asset | Risk from proposed plan | Guard |
|---|---|---|
| 3 paper-trading instances | None from UI/flag work; restart during deploy pauses workers briefly | Supervisor restores; verify `desired_running` before/after |
| Adaptive MTF Trend Pullback config/evidence | None if Adaptive lab DBs and strategy files untouched | Add to protected-store list |
| Journals, trade history, portfolio, analytics | PA history disappears from Calendar if its collector is simply removed | Replace with a read-only historical collector |
| Market data | Unaffected unless PA held a unique channel | Compare channel list before/after |

---

## 6. Prioritised Implementation Plan (for approval)

Each step needs separate approval. "Gain" is expected, not measured, unless stated.

| # | Step | Expected gain | Confidence | Risk | Rollback | Validation |
|---|---|---|---|---|---|---|
| **P0** | **Read-only VPS evidence**: production image revision label; `du` of both data volumes, `backups/`, Docker images; per-table sizes of both PA files (read-only `dbstat` or row counts); nightly backup results in logs; `docker stats` 10-min idle samples for prod + staging; `docker inspect pa-replay-2025` mounts | Turns hypotheses into facts | High | Very low | n/a | Evidence recorded in repo |
| **P1** | **Disk safety on the shared host**: stop staging copying its 17 GB PA file nightly (or disable PA in staging), delete only job-owned scratch snapshots already verified, move backups to a separate volume/off-host, set a production memory limit with headroom | Stops the ~4 GiB/day trend [HYPO]; protects production | Medium until P0 | Low–Medium | Re-enable job / restore config | Free bytes measured daily for 3 days |
| **P2** | **Frontend polling** (UI only): ticker polls lab status only on lab pages; `/instances` to ≥5 s; PA/IVL/Replay/terminal pollers pause when hidden; review Dashboard card intervals (2–3 s today) | Page frame **88.5 → 46.5 req/min (−47%) on every page**; Dashboard only 236.5 → 218.5 from these two changes — its cards need their own interval review [LOCAL calc, `poll-rates.txt`] | High | Low | Revert UI commit | Browser network count before/after per page |
| **P3** | **Price Action Lab retirement on the deployed lineage**: port flag; skip PA runtime/account/stream construction; Guardian, Journal Recorder and Calendar read PA history from a read-only frozen source; keep PA strategies, engine, rulebook code; 410 for lab routes; backup handles archives | Removes always-on PA work and its scans | Medium–High | Medium | Flag `true` + restart | Boot with flag off: no PA threads, no PA DB write descriptors, Guardian healthy, Journal/Calendar still show PA history, 3 instances running; 24 h CPU/RSS comparison |
| **P4** | **Backend query fixes**: Guardian PA reader row-value seek + `updated_at` index; Journal Recorder incremental watermarks for lab projectors; cache `/strategy/performance` and PA/SMC status for a few seconds | Guardian PA read 135 ms → <1 ms at 100k rows [LOCAL]; recorder pass from O(history) to O(new rows) | High | Medium (journal correctness) | Revert commit | Recorder output equality on a copy of production data |
| **P5** | **SMC page consolidation** | Simpler navigation; one less page frame poller | High | Low | Revert UI | E2E navigation tests |
| **P6** | **Retention policy decisions (owner)**: Guardian events, PA journal duplicates (existing `pa_journal_prune.py` archives first), staging PA data | Disk reclaim | Medium | **High (evidence tables)** | Restore from verified archive | Archive row counts = removed rows; triggers restored |
| **P7** | **Move CPU-heavy research off the API process**: backtests, replay, league to the research worker | Removes GIL contention spikes | Medium | Medium | Route back | Instance candle latency during a backtest |

Measure before and after each step: idle CPU% and RSS (`docker stats`), p95 latency of
`/instances`, `/research/smc/bot-status`, `/strategy/performance` from nginx logs, free
disk bytes, requests/min per page.

---

## 7. Unresolved blockers

| ID | Blocker | Who can resolve |
|---|---|---|
| B1 | Production source revision unrecorded (image `d4c51634…` has no revision captured) | Operator: `docker inspect` image labels |
| B2 | Production rollback image retention not confirmed | Operator |
| B3 | No VPS CPU/RAM/I/O samples per container | Operator: read-only `docker stats`, `du` |
| B4 | 17.16 GB staging file not broken down by table | Operator: read-only per-table measurement |
| B5 | Cause of ~13 GiB disk drop Oct 19–22 unknown | Operator: `du` on volumes, `backups/`, Docker image store, Task 6 job directories |
| B6 | PA/SMC lab symbols, timeframes and modes on VPS unknown | Read `/research/*/bot-status` on VPS |
| B7 | Ledger backend (Supabase vs SQLite) in production unknown | Startup log line `ledger backend = …` |
| B8 | No off-host backup or recorded restore test | Owner decision + operator |

---

## Appendix A — Commits referenced

| Commit | Lineage | Meaning |
|---|---|---|
| `215c96c` | Guardian | Audited head; "Merge Guardian foundation with deployed 93f5f2d runtime" |
| `93f5f2d` | Guardian | "deployed runtime"; already has Guardian sidebar and PA fixes |
| `f8778ed` | Main | Staging API revision |
| `42de6d0`, `264e96b`, `60df5fd`, `b80d9b8`, `36388b7` | Guardian only | PA journal revision-storm fixes and audited prune tool |
| `f9ebd32`, `c0c0cf0`, `c13a439` | Main | PA retirement implementation and read-only audits |

## Appendix B — How the local measurements were produced

See [`validation/p01-audit/README.md`](validation/p01-audit/README.md). All runs used
synthetic data in a throwaway directory; no production or staging data was read.
