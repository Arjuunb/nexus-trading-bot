# Task 4: rolling decay and weekly strategy research

Base: Task 3 commit `d306f4a789754ee0b5f2fece08ab7dd952f11085` on
`finish/strategy-intelligence-staging`. This task adds descriptive monitoring,
weekly persisted research reports, and dashboard inspection. It does not deploy
or begin Task 5.

## Frozen evidence and financial boundaries

The monitor reuses the owner-authorized Task 1 chain: frozen strategy ID,
version and configuration fingerprint; original historical financial samples;
OOS, walk-forward, robustness and cost-stress references; and actual completed
forward-paper position episodes. It rebuilds that chain against the saved
strategy identity, then independently validates original paper amounts, signed
funding, fees, explicit slippage coverage, original entry risk, episode membership,
parent financial summaries, close chronology, classifier and source watermarks.
Missing costs, incomplete history, conflicting summaries, stale caches, bounded
source reads, mixed sources and foreign identities produce UNKNOWN. Missing
amounts are never filled with zero. Slippage is disclosed and not charged twice.

Only the exact owner/account/instance/session/lab/asset/frozen identity and
paper/forward-paper partition are analyzed. All partial exits and remainders
remain one position episode. The monitor sorts by actual UTC final close time,
then episode ID. Reads inspect bounded journal metadata, never the financial
ledger or market providers. Original financial and strategy records are unchanged.

The service serializes calculations with refresh and journal capture, rechecks
journal/context source watermarks, and checks matching scope invalidations.
Invalidation during monitoring clears the conclusion and windows. Invalidation
or a changed source during report creation prevents the metadata write. Another
tenant's invalidation does not revoke an owner's independent evidence.

## Versioned descriptive decay policy

The PRD specifies 20/50/100-trade windows but no numerical thresholds. The
implementation exposes these explicit **uncalibrated research rules** as
`edge_decay.v1`; they are not a calibrated probability of decay or profitability:

| Rule | v1 definition |
| --- | --- |
| Historical baseline | At least 100 complete historical episodes; positive net expectancy and finite monetary PF above 1 |
| Windows | Last 20, 50 and 100 completed position episodes, separately reported |
| Expectancy deterioration | Window mean booked net R is below 50% of the historical mean |
| Monetary PF deterioration | Window monetary PF is below 1; no positive outcomes is separately disclosed |
| Drawdown deterioration | Window R drawdown exceeds `2 × max(historical R drawdown, 1R)` |
| Continued deterioration | Two consecutive, disjoint 20-episode blocks both deteriorate |

A window below its required count remains INSUFFICIENT_EVIDENCE. Missing or
unusable baselines do not imply stability. Monetary PF remains null for no-loss
samples with an explicit ratio state; no infinity is manufactured.

The report states are STABLE, EDGE_DEGRADING, PAUSE_REVIEW and
INSUFFICIENT_EVIDENCE; invalid/unavailable evidence returns UNKNOWN. A degrading
report suggests DEGRADED; continued decay suggests PAUSED. Both are
PENDING_APPROVAL review proposals. No champion is assumed or modified. Repeated
GETs, overlapping 20/50/100 windows or repeated weekly ticks cannot count as new
persistence observations. Recovery is recalculated from evidence and never
resumes a worker automatically. Historical/forward drawdowns cover different
observation horizons, and all comparisons are descriptive.

The policy definition and fingerprint are exposed by the API. Numerical policy
changes require deliberately introducing and reviewing a new policy version.

## Weekly research

`weekly_strategy_research.v1` reviews completed Monday-to-Monday UTC calendar
weeks using half-open close-time ranges, and compares the preceding, separate
calendar week. Future and unfinished weeks are rejected. Later trade outcomes
are excluded from that week's decay calculation.

Each report includes current/prior counts, wins/losses/breakevens, win rate,
gross/net P&L, total/mean net R, monetary PF and R drawdown; its own scoped
symbol/session/trend/volatility/timeframe/direction/setup/exit-reason breakdowns;
planned-versus-achieved RR observations; observed MAE/MFE coverage; consecutive
booked-loss clusters; financial execution costs; findings; and pending proposals.
Each subgroup uses its own episodes and sample size. Pattern comparisons need
at least 20 episodes in each compared week or subgroup. A run of at least three
booked losses is an observed cluster, not a causal or statistical edge claim.

Unknown MAE/MFE, planned targets, context, setup and exit observations remain
unknown, with known sample counts disclosed. Recorded entry setup and final
exit reasons are copied from immutable journal sections without changing them.
Violations, latency and missed-opportunity profits remain UNKNOWN when a
complete independent observation dataset is not bound; executed trades cannot
establish the outcomes of blocked or missed entries.

Reports retain reviewed episode IDs, all observed constituent trade IDs and
per-episode trade-reference coverage; historical run/dataset references;
classifier/cost models; paper cache run; and journal/context/source watermarks.
Proposals contain an evidence-linked research question and validation requirements,
not an applied configuration. Every proposal is PENDING_APPROVAL and auto_apply
is false. No order, optimization job, model call, promotion, notification or
configuration mutation is submitted by this workflow.

The existing evidence refresh/recovery tick generates the latest completed
week once when complete bound evidence is available. It starts no new thread
and remains dependent on that existing worker. After a missed tick/restart it
retries the latest completed week. It does not claim to backfill all missed
weeks: authorized users can explicitly generate older completed weeks.
Automatic processing is bounded to 50 exact asset cohorts per refresh (or the
configured smaller service batch size), and reports COHORT_BOUND_EXCEEDED rather
than claiming full processing when that limit is exceeded.

Identical snapshot retries return the same report. Explicit regenerations against
a changed assessed source append a new report with supersedes_review_id; they
never replace previous reports. The existing append-only intelligence metadata
store holds these reports under a separate cache-key/contract namespace. There
is no schema migration. Weekly failures are isolated from evidence refresh and
never affect execution or accounting. Saved reports remain historical snapshots
after restart, explicitly NOT_REVALIDATED against current sources.

## API and dashboard

Authenticated routes under `/api/v2/strategy-intelligence`:

- `GET /monitoring/policies`: both versioned policy definitions and fingerprints.
- `GET /decay`: current exact frozen paper cohort's rolling monitoring report.
- `GET /weekly-research`: bounded saved report revisions for an exact cohort/week.
- `POST /weekly-research`: create an immutable metadata-only report from server
  evidence; optional week_start defaults to the latest completed UTC week.

Strategy ID/version/fingerprint, instance and asset are required. Owner, account
and authorized session are resolved on the server. Client ownership, evidence,
threshold and execution assertions are rejected. Unsupported policy versions,
partial classifier identities, repeated query fields and unknown fields are
rejected. Classifier ID/version/hash must be supplied together or use the current
server definition. Responses use no-store; analytics never grant execution rights.

Strategy Intelligence now has an Edge Monitoring tab and a working Research
Findings view for weekly reports. Both require an explicitly selected frozen
paper cohort and exact asset. Weekly creation is an explicit metadata-only button;
there are no apply, pause, resume or promotion controls. Unknown evidence is not
rendered as zero performance. Changing cohort, asset or week remounts the scoped
results and checks returned identities so an old/foreign report cannot remain
visible. Tables scroll inside their cards on narrow screens.

## Validation and limits

Actual test output, reproduction failures, policy definitions, fixture-only
examples and the final acceptance receipt are retained under `validation/task4/`.
Browser tests use synthetic fixtures. No illustrative score/report establishes a
real profitable strategy. PostgreSQL/container/provider drills are separate opt-in
acceptance; skipped tests are not passes. Existing sandbox process-health and
missing Docker limitations are reported explicitly if encountered in broad local
validation. Full branch CI, when run, is recorded separately against its exact SHA.

No strategy rules, entry/exit/SL/TP/RR/risk/leverage settings, financial records,
execution path, notification destinations, deployment configuration or live
activation changed. Task 5 and deployment remain separate work.
