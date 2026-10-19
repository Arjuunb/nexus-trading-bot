# Task 5: integrated Strategy Intelligence acceptance

Base: Task 4 commit `567157c9d820847e94c1a854049e141e4e63d48e` on
`finish/strategy-intelligence-staging`. Task 5 validates the research workflow
and closes the gaps found during acceptance. It includes no deployment or Task 6.

## Acceptance coverage

| Workflow | Verification and boundary |
| --- | --- |
| Frozen identity | Historical, OOS, walk-forward, robustness and stress references use the exact observed strategy/version/configuration; paper provenance remains a separate partition of that identity |
| Financial evidence | Original samples reconcile booked gross, fees, signed funding, net P&L and original risk; explicit cost coverage and textual model identities are required; missing evidence never becomes zero |
| Actual forward paper | Real local paper-engine entry, partial close and final close produce one persisted position episode, read through the authenticated router and dashboard |
| Qualification | Bound historical stages pass against synthetic fixture finances; the real paper episode remains NOT_QUALIFIED because its sample and cost evidence are incomplete |
| Champion/Challenger | Positive comparisons require owned committed raw evidence, comparable original cost models, independent OOS and walk-forward evidence, adequate holdouts, known drawdowns and seeded uncertainty |
| Regime selection | Exact account/session/asset/context, qualified frozen candidates and adequate complete context evidence; incomplete actual paper evidence abstains |
| EdgeScore | Versioned nine-component descriptive calculation; unknown inputs remain UNSCORED; qualification and selection remain independent |
| Decay | Actual final closed episodes; 20/50/100 windows, disjoint persistence blocks, unknown evidence and approval-only proposals |
| Weekly research | Completed UTC weeks, own scoped samples, append-only reports, restart/source-change checks and fair bounded cohort processing |
| Tenant isolation | Unauthenticated requests fail; foreign instance, candidate and comparison reads are denied; supplied ownership fields are rejected |
| Accounting and rules | Authoritative ledger snapshots, original evidence events and historical validation records remain identical after the integrated workflow |
| Restart | Persisted paper caches require reconciliation before current eligibility; old results grant no score or qualification |

Synthetic fixture results do not establish a real profitable strategy. The
actual paper-engine fixture has incomplete funding/slippage evidence; acceptance
asserts that it stays blocked rather than changing its receipts or assuming zero.

## Gaps fixed

The legacy comparison used summary-only metrics and candidate quality assertions,
allowed missing drawdown, ignored sample/OOS/walk-forward requirements, and read
run references without first checking their owner and scope. Task 5 introduces
`challenger.v2`: each candidate's owned, committed references are revalidated
with Task 1's financial/identity/chronology binder. Comparison outcomes come from
the independent holdout financial samples. Worker payload overrides and claimed
episode outcomes are not financial authority. Valid committed worker references
can establish evidence for a candidate registered before execution, without
rewriting that candidate or its validation records.

Both holdouts require at least 30 episodes, positive net expectancy and known R
drawdowns. Original fees/funding/slippage model sets must match across the
validated financial samples. The seeded bootstrap uses the requested valid
confidence level; its lower bound must support the requested improvement before
the challenger can be preferred. This remains a descriptive, uncalibrated
research procedure, with dependent-return limitations disclosed. A comparison
never grants qualification, execution or promotion rights. Nonpositive OOS does
not recommend retaining an unqualified champion.

Unknown policy fields, non-finite thresholds, invalid sample/bootstrap/confidence
parameters and attempts to disable required evidence gates are rejected. New
matched experiments and agent submissions freeze `challenger.v2`. Stored v1
artifacts remain historical records; explicitly frozen v1 pending experiments
need a new v2 experiment rather than silently executing under another policy.
The legacy candidate degradation endpoint now points to exact-cohort forward
paper `/decay`; repeated historical runs cannot establish independent decay.

Malformed cost coverage and non-text/blank cost models now return incomplete
financial evidence instead of producing complete metrics or an unhashable-value
exception.

The existing weekly refresh loop now rotates its bounded cohort batch. An
incomplete first cohort cannot permanently starve later cohorts. Cursors are
bounded, isolated by scope/week/classifier/cohort set, held only in process,
and reset on restart. Persisted report idempotency still prevents duplicates.
The loop still reports COHORT_BOUND_EXCEEDED for each partial batch and does
not claim full backfill or start another worker.

## Dashboard and continuous validation

New EdgeScore and Strategy Selection tabs expose the existing Task 3 and Task 2
APIs. They require explicit observed scope and asset, show missing evidence as
unavailable, check returned identity/context and clear old results when controls
change. They show component explanations, sample counts and source references.
There are no execution, promotion or apply controls.

The real HTTP browser acceptance server is an explicit test-only loopback
process using a temporary SQLite directory and the registered intelligence
router. It imports no production app, starts no provider/engine worker and clears
notification/execution destinations. Its test identity cookie is supplied by the
test harness; production authentication is not replaced or modified. Other
dashboard navigation endpoints use existing UI fixtures. The browser verifies
actual paper evidence, score/selection abstention, blocked weekly publication
and an independently valid historical comparison, then checks unchanged finances.

CI now typechecks/builds the dashboard and runs the 20 focused browser checks,
including this real HTTP path, alongside the existing Python 3.10/3.11/3.12
backend matrix. Chromium uses a configured/local executable when present or the
Playwright-installed browser otherwise. No release or deployment job is invoked.

## Results

Actual commands, failing regression reproductions, JUnit results and the final
receipt are under `validation/task5/`. `results.json` records pre-publication
results. The final acceptance receipt records CI against its exact published SHA.
Skipped opt-in tests are disclosed separately and are not counted as passes.
Container, PostgreSQL and external-provider drills require their own environments;
this task does not substitute synthetic fixtures for those deployment checks.

Strategy implementations, entry/exit/SL/TP/RR/risk/leverage settings, financial
ledger code and existing financial records are unchanged. Task 6 and all
deployment/activation work remain separate.
