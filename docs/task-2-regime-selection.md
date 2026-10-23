# Task 2: advisory regime-based strategy selection

Task 1 was published on `finish/strategy-intelligence-staging` as
`85c862bcd49b21d40ed4d12f5eb1efc3b8a74f92`. Its tree exactly matches the tested
local Task 1 commit. The user subsequently authorized proceeding to Task 2
after the two broader regression failures were disclosed. That authorization
does not establish a green full suite or deployment readiness.

Task 2 implements the agreed advisory recommendation scope. It does not add
automatic strategy switching or execution.

## Public read

`GET /api/v2/strategy-intelligence/selection`

Required query fields:

| Field | Meaning |
| --- | --- |
| `instance_id` | An instance authorized for the authenticated owner |
| `symbol` | Exact asset, for example `XRPUSDT` |
| `session` | Canonical session, including overlap or off-session |
| `trend_regime` | BULL, BEAR, STRONG_BULL, STRONG_BEAR or RANGE |
| `volatility_regime` | LOW, NORMAL, HIGH or EXTREME |
| `entry_timeframe` | Exact original entry timeframe, for example `5m` |

An optional `simulation_session_id` must belong to that instance; omission
selects its current simulation session. `lab_id` is an exact nullable partition.
Classifier ID, version and parameter hash may be supplied together. If omitted,
the current server classifier definition is used and returned explicitly.
The route does not classify current market conditions; its input identifies
the context to inspect. It uses the existing authenticated API and `no-store`
response policy. There is no dashboard change in this task.

## Evidence and policy

`regime_selection.v1` discovers at most 100 candidate frozen identities in the
same owner/account/instance/session/lab and asset partition. Only actual paper
cohorts (`execution_mode=paper`, `source_kind=forward_paper`) are considered.
Missing identities and foreign scopes cannot become wildcard matches. A
truncated discovery abstains rather than selecting a possibly incomplete winner.

Every eligible candidate must pass the real Task 1 qualification chain. Its
context cache must be ready and fresh under the existing five-minute cache
contract. The exact asset/session/trend/volatility/timeframe combination gets
its own independently bound subgroup; parent sample sizes or results are not
inherited. Classifier and cost model partitions remain distinct.

The subgroup additionally requires:

- At least 30 unique completed position episodes and an original-risk R sample
  for every episode.
- Complete bound context/financial evidence, fees, funding and slippage.
- Positive net expectancy, a finite net profit factor of at least 1.30, and a
  known nonnegative R drawdown.
- Exact subgroup membership within the qualification chain's actual forward
  episode IDs, with the same source watermark and frozen strategy identity.
- A valid observed opening/closing period. Eligible alternatives must have
  overlapping observation periods and identical cost models.

The added v2 `max_drawdown_r` is calculated from booked episode net P&L divided
by each episode's original risk, ordered by actual close time and episode ID.
It remains unknown if risk or closure facts are missing. Existing monetary
drawdown and v1 financial calculations are retained.

Eligible candidates rank by higher observed net R expectancy, lower R drawdown,
then larger context sample. An exact top-rank tie produces `TIED` and a null
recommendation. Other abstentions are `INSUFFICIENT_EVIDENCE`, `NOT_COMPARABLE`,
`BOUND_EXCEEDED` or API `UNAVAILABLE`. Ranking is descriptive; overlapping
periods do not establish matched independent samples or statistical superiority.

A recommendation includes its frozen cohort, metrics, episode IDs, cache run,
source watermark, validation run references and policy version. Rejected
candidates include blocking reasons. A scope invalidation during assembly
revokes the result. Cache failures cannot return an executable recommendation.

## Preserved boundaries

`advisory_only=true` and `orders_allowed=false` are explicit. Selection reads
metadata and precomputed caches; it does not download market data, run research,
submit orders, designate champions, edit strategy rules, change risk/leverage,
alter financial records or enable workers. Existing immutable research and
financial history are retained. No Edge Score, decay lifecycle or weekly
research work (Tasks 3 and 4) is included. No deployment is performed.

## Validation

Tests cover context and tenant boundaries, source/membership binding, missing
costs and risk, stale evidence, classifier/model conflicts, small samples,
ties, deterministic decimal ranking, query bounds, scope invalidation, actual
partial/final paper closure, and authenticated API reads over calculated,
persisted context caches plus the real qualification chain. Test evidence is
fixture-generated; it does not demonstrate a profitable real market strategy.

The persisted-cache integration test first exposed the absence of R drawdown
in the old v2 projection. The original failure and the targeted drawdown
regression are retained. The final selector suite passes all 44 tests.

The final focused suite passes **308 tests, 0 failures, 0 skips, 4 warnings**.
The local broad run was interrupted by the workspace network approval control
after reaching 29%; it has no valid final count or completion result. Its
partial log is retained as `validation/task2/full.txt`. An earlier broad run was
superseded during the drawdown fix and also has no acceptance result.
The interrupted run had process-health assertion failures: retained health
records show `process_start_ticks=null` even with the candidate/database stores
available and execution unloaded. This workspace's namespaced process IDs do
not match its mounted `/proc` view. Health checks correctly fail closed; their
assertions and production implementation are retained.

Machine-readable results are in `validation/task2/results.json`. External/live
execution, automatic workers and notification destinations are disabled during
tests. Full-suite acceptance will use the branch CI result for the published
Task 2 source; this local interruption is not a passing full-suite claim.

Task 1's published commit subsequently passed GitHub CI run `39138064963`:
engine and automation-hub tests succeeded on Python 3.10, 3.11 and 3.12;
the landing build also succeeded. The job receipt is retained in
`validation/task2/task1-github-ci.json`. This is separate from the earlier local
regression result and is not a deployment or real-market validation claim.
