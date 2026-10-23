# Task 3: versioned Edge Score

Task 3 extends `finish/strategy-intelligence-staging` from Task 2 commit
`d2c2ef4511a9ec221b195bb6ce776d0f931368aa`.

The PRD identified composite score components but did not prescribe a formula,
weights or calibrated thresholds. This implementation makes those choices an
explicit **uncalibrated research policy**, `edge_score.v1`. A score of 80 is
not an 80% probability of profit. No optimization or calibration to observed
strategy returns was performed.

## Contract and policy

The score ranges from 0 to 100. `strategy_edge_score.v1` returns the policy,
fingerprint, raw component inputs, formulas, normalized component scores,
weighted points, blocking reasons and original evidence references. Decimal
arithmetic uses 50-digit precision, six decimal places for displayed component
scores/points, and a total equal to the sum of the displayed weighted points.
It does not sum historical and paper episode counts into an independent sample.

Let `clip(x)` mean `max(0, min(1, x))`:

| Component | Weight | Normalized score, 0–100 |
| --- | ---: | --- |
| Historical net expectancy | 15 | `100 × clip(expectancy R / 0.5)` |
| Historical monetary profit factor | 10 | `100 × clip((PF − 1) / (2 − 1))` |
| R drawdown | 10 | `100 × 10 / (10 + max(historical DD, actual paper DD))` |
| Financial sample volume | 10 | `100 × min(historical episodes / 300, actual paper episodes / 100, 1)` |
| Walk-forward consistency | 10 | `100 × positive evaluation windows / all bound evaluation windows` |
| Out-of-sample retention | 10 | `100 × clip(holdout expectancy / positive training expectancy)` |
| Actual forward-paper performance | 15 | `100 × clip(paper net expectancy R / 0.5)` |
| Observed regime robustness | 10 | `100 × clip(worst observed regime expectancy R / 0.25)` |
| Adverse cost resilience | 10 | `100 × clip(stressed net R / positive base net R)` |

All-nine-component weights sum to 100. A missing required component leaves
`score=null`, `band=null`, `status=UNSCORED`; its weight is never redistributed.
Known nonpositive economic outcomes score zero in their relevant components.
Nonpositive training/base outcomes cannot produce a favorable ratio by dividing
two negative numbers. Missing, boolean or nonfinite values remain unknown.

An observed no-loss historical sample can receive the maximum profit-factor
component only when original complete financial samples prove positive P&L
and no negative outcomes. Its monetary ratio remains null with the explicit
state `NO_LOSSES_OBSERVED`; no numeric infinity is invented. Explicit all-zero
booked net outcomes score zero for that component. Existing qualification
criteria are retained, including their treatment of undefined profit factors.

Display bands are `LOW_SCORE` below 40, `MID_SCORE` from 40 to below 70,
and `HIGH_SCORE` from 70 through 100. They are descriptive score bands, not
qualification states or trading recommendations.

The immutable policy fingerprint is:

`503fa1c6e14181bd00b8fca86b695330f4bfa02144376a89b929176706b1bd88`

Policy parameters and weights are exposed in
`validation/task3/edge-score-v1-policy.json`. Changing v1 parameters requires
a new policy version. Caller-owned mutable lists are copied into immutable
tuples. A fixed fingerprint regression protects the published v1 definition.
Alternative policies can be introduced deliberately in code under new versions;
the public API currently supports v1 and accepts no weight overrides.

## Required source evidence

The score consumes Task 1's exact frozen strategy/version/configuration,
historical dataset/revision/timeframe, independent OOS, walk-forward,
robustness, cost-stress and actual forward-paper chain. It rechecks the selected
validation run references and original historical financial samples. Summaries
or caller-supplied completeness flags cannot replace missing financial facts.
Decision-only shadow records and persisted assertions of paper profitability
cannot replace actual closed paper position episodes.

Minimum scoring evidence is:

- 100 original closed historical episodes and 30 actual closed forward-paper
  episodes with complete costs and original-risk R observations.
- Three bound, disjoint, causal walk-forward evaluation windows for consistency.
- Two observed trend regimes, each with at least 30 complete actual paper
  episodes. Every parent paper episode must belong to exactly one regime group.
  Unknown regimes, duplicate memberships, small groups or omitted losing groups
  leave this component unscored.
- Explicit positive incremental fee and slippage stress, nonnegative incremental
  funding stress, and reconciled base/stressed net R. Merely completing a cost
  calculation with zero adverse stress is insufficient.

Paper and regime views must be current authorized cache reads of the same
owner/account/instance/session/lab, asset and frozen identity, source watermark,
classifier ID/version/parameter hash and cost models. Parent counts and metrics
are not copied into subgroups. Only original point-in-time entry classifications
are used. Unseen regimes remain unknown, even when observed regimes score well.

The total may describe financially complete but unqualified evidence; the
response preserves its separate `qualification_status`. Every response has
`advisory_only=true`, `orders_allowed=false`, `recommendation_eligible=false`.
A total never grants qualification, selection, promotion or execution rights.
Task 2's `regime_selection.v1` ranking remains unchanged.

## API and lineage

Authenticated, read-only routes under `/api/v2/strategy-intelligence`:

- `GET /edge-score/policies`: the supported policy definitions and fingerprints.
- `GET /edge-score`: an exact frozen cohort's score and component explanations.

The score request requires `instance_id`, `strategy_id`, `strategy_version`,
`config_fingerprint` and `symbol`. Its only execution/source partition is
`paper` / `forward_paper`. Optional simulation sessions must belong to the
authenticated owner's instance. Classifier ID/version/hash must be supplied
together, or the current server definition is selected and returned explicitly.
The `score_policy_version` defaults to `edge_score.v1`.

The existing owner/session authorization and `Cache-Control: no-store` contract
are reused. Unknown query fields, client weights, forged owner/account/evidence
facts, partial classifier identities and unsupported policies are rejected.
Cache errors return a null score. Exact or broader matching scope invalidation
during assembly revokes both the total and component values; another tenant's
invalidation cannot revoke this owner's result.

A deterministic `score_id` binds the policy, exact identity, selected reports,
paper/regime cache snapshots, component results and qualification policy. It
ignores the time at which the read was requested. Lineage includes historical
dataset identity/revision/period, validation run IDs, paper/regime cache run IDs,
source watermark, classifier, cost models and completed paper episode IDs.
Original source reports and cache snapshots remain immutable in their existing
stores. GET requests do not persist new score/history records or run research.

## Scope and validation

This task adds the backend calculator and API; it does not change the dashboard.
No strategy rules, frozen identity, risk/leverage setting, execution code,
financial ledger, migration, worker activation, market-data path, deployment
configuration or existing immutable record is changed. Task 4 is not started.

Regression tests cover explicit fixture arithmetic, policy immutability,
normalization bounds, known losses versus unknown evidence, no-loss samples,
small-sample gates, consistency, cost stress, lineage and tenant isolation,
source invalidation, authenticated API reads from real persisted derived caches,
and actual partial/final paper closure preserving the authoritative financial
snapshot. Fixture-generated results do not establish a profitable strategy.

Actual local results and test logs are retained in `validation/task3/`.
External/live execution, automatic workers and notification destinations remain
disabled during local tests. The previously observed sandbox networking and
`/proc` limitations prevent treating a local broad run as acceptance; complete
engine/hub regression validation uses the existing branch CI workflow. Existing
opt-in PostgreSQL/container/provider drills are reported as skipped, not passed.

Final local validation: **382 focused/service/API/integration tests passed,
0 failed, 0 skipped, 4 warnings**; **530 engine/client tests passed**. The
warnings are existing FastAPI lifecycle deprecations. The illustrative fixture
report in `validation/task3/fixture-example.json` scores `80.749576`; it is
explicitly marked as fixture-only and makes no real-market profitability claim.
`validation/task3/results.json` records this pre-publication checkpoint; the
subsequent branch CI result is a separate acceptance receipt.
