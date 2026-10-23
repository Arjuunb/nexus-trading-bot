# Strategy Intelligence metrics contract v2

`services/strategy_intelligence_v2.py` is a pure observation module. It cannot
read or mutate the paper ledger, obtain market data, submit orders or start a
strategy. Its producer is responsible for obtaining consistent authoritative
snapshots and a fresh Sprint 1.5 completeness assessment. Existing v1 endpoints,
financial formulas and dashboard calculations remain available unchanged.

The public contract is `strategy_intelligence.v2`. Financial fields inside each
group identify `calculation_version=strategy_intelligence.v1` because their
accounting formulas reuse that calculator. Verification metadata identifies
`verification_contract_version=strategy_context_evidence.v1`, and applies to
the exact context subgroup rather than borrowing a parent's verification flag.

## Exact cohort and partition rules

Every calculation requires all `EvidenceCohort` dimensions: strategy ID,
observed version, immutable configuration fingerprint, owner, account, instance,
lab, simulation session, execution mode and source kind. Explicit NULL scope
matches only NULL scope. Symbol=NULL aggregates assets within that exact cohort;
it does not relax ownership, version or execution isolation. Public services
resolve access scope on the server, never from a client completeness assertion.

Only executed, fully closed position episodes contribute to trade statistics.
Rejected decisions, counterfactuals, shadow outcomes, cancelled/expired intents
and open episodes never contribute to completed trading performance. Identical
repeated episodes count once; conflicting rows with one episode ID raise an
error. All fills, partial exits and explicitly linked scale-ins remain part of
one episode. Original observed risk includes explicit scale-in risk receipts.

An episode's contextual performance uses its **original entry**. Scale-ins
retain separate immutable entry context, but cannot replace the root entry's
session/regime or become additional statistical trades. When a root trade ID
exists, only its exact matching context is eligible. Missing root context never
falls back to a convenient scale-in or reduction context.

Supported groupings are total, asset, session, trend regime, volatility regime,
direction, entry timeframe, asset × session, asset × trend, asset × volatility,
asset × session × trend, and timeframe × direction. Strategy identity is fixed
by the exact cohort, so these implement strategy × each requested grouping.
Unknown values remain explicit NULL/UNKNOWN partitions. LONG/SHORT are derived
only from an observed BUY/SELL or LONG/SHORT direction; missing direction never
defaults to long.

All groups additionally partition by immutable classifier ID, version and
parameter hash, and by fee/funding/slippage cost-model identities. Different
classifiers or cost models never pool silently. Unnamed cost models remain
UNKNOWN and block verification even when their booked amount is known. Entry
classifications are selected by default; versioned research reclassifications
require an explicit internal opt-in. Across different classifier versions or
research classifications, the same episode may appear in multiple separately
labeled views, so summing those views is invalid. Within one classifier/cost
partition, canonical sessions count each episode once, including overlap trades.

## Authoritative metrics and missingness

Financial arithmetic uses Decimal precision 50 and JSON decimal strings. REAL
precision already lost in a source database cannot be recovered. No source
amount is rewritten. Full totals stay NULL when necessary inputs are missing;
`known_*` totals and sample sizes disclose partial coverage.

| Metric | Meaning |
|---|---|
| Completed episodes / trade count | Fully closed position episodes, never number of financial legs |
| Wins / losses / breakevens | Strictly positive / negative / zero authoritative net episode P&L |
| Win / loss rate | Corresponding count divided by all completed episodes; NULL if any net P&L is unknown |
| Gross profit / gross loss | Sum of positive gross episode P&L / absolute sum of negative gross episode P&L |
| Gross P&L | Authoritative gross; derive net + fees + funding only when both cost coverages are confirmed |
| Fees | Explicit confirmed costs, nonnegative |
| Funding | Explicit confirmed signed cost: positive paid, negative received |
| Slippage | Sum of separately observed or modeled signed slippage costs; otherwise NULL |
| Net P&L | Authoritative booked net; never charge fees, funding or slippage a second time |
| Average winner / loser | Mean positive / mean negative net episode P&L; loser retains its negative sign |
| Average winning / losing R | Mean known net P&L / original positive episode risk among winners / losers, with separate sample sizes |
| Expectancy R / average realised RR | Mean known net episode R; includes partial-exit economics and explicit scale-in risk; never a final price-only RR |
| Profit factor | v1 net and gross PF remain separately named; no losses yields NULL plus NO_LOSSES, never 99/Infinity |
| Planned RR | Mean explicitly observed episode planned RR; full average NULL if any is missing; known average/sample size separately disclosed |
| Drawdown / streaks | v1 realised net P&L sorted by aware actual closure time, episode ID tie-break; breakeven resets streaks |
| Trade duration | v1 mean of known, nonnegative actual open-to-final-close durations with sample size |
| MAE / MFE | Mean explicitly observed `mae_r` / `mfe_r`; full mean NULL if any is missing, known mean/sample sizes separate |
| Long / short summaries | Descriptive counts/net/wins only; each direction requires its own context group for verification/confidence |

MAE/MFE are never inferred from the final result or current candles. Missing
planned RR is never synthesized from current strategy defaults. Confirmed-zero
cost status must contain an observed numeric zero; a nonzero amount raises a
conflict rather than being accepted as free execution.

## Evidence binding and subgroup reliability

`build_subgroup_report` validates the current original v1 projection's exact
episode digest against the completeness report, exact cohort, combined source
watermark, supported report version and calculation clock. A future assessment,
assessment older than 300 seconds, changed source or changed episode input
makes binding UNKNOWN. Optional episode enrichment may add original observed
direction/planned RR/excursions/cost metadata; it cannot change any authoritative
episode field or membership. `evidence_episodes` carries the unmodified
authority projection so enrichment cannot invalidate or forge the original
receipt digest.

The deterministic context partition is then checked independently. Every
eligible cohort episode, including open episodes, must have an attributable
original-entry context before any subgroup can certify complete membership.
Context identity must match every exact cohort field and episode ID. A signal
timestamp after the authoritative entry raises an error. Missing contexts,
unreliable quality, unknown core session/trend/volatility, and unknown group
dimensions prevent context profitability verification. An experimental UNKNOWN
structural regime is allowed because this contract does not certify structure.

Each subgroup has its own selector, completed count, exact episode IDs,
subgroup evidence digest and full context digest. Parent counts, confidence and
profitability are never copied. The helper proves that the selected subgroup is
an exact partition of the freshly assessed authoritative cohort, rather than
repeating an unrelated parent flag. PARTIAL, UNKNOWN, CONFLICTED and RECOVERING
authority statuses propagate conservatively; a convenient small subgroup never
upgrades its parent. A missing context elsewhere in the cohort prevents complete
subgroup membership certification because its true subgroup is not known.

Reports expose `missing_context_count`, `unreliable_context_count`, their reason
codes and affected episode-ID samples. Samples are capped at 100 IDs, with an
explicit truncation flag; counts and digests still cover the whole assessed
input. Authoritative financial completeness details remain in the original
Sprint 1.5 report. Shared full-source validation happens once per grouping,
followed by indexed subgroup checks, avoiding a full source scan per group.

The pure module cannot authenticate a caller's source watermark. Public
consumers use producer-generated reports; client-submitted proofs are forbidden.
The assessment describes one source view, not a permanent guarantee about
future trades. Cache freshness/invalidation is enforced by the producer service.

## Independent evidence, costs, confidence and performance

These concepts are intentionally separate:

- `evidence_quality.status`: COMPLETE, PARTIAL, UNKNOWN, CONFLICTED or RECOVERING
  for the subgroup's bound view.
- `context_quality`: VALID only for reliable observed core classifications.
- `cost_coverage`: separate COMPLETE/PARTIAL/UNKNOWN fee, funding and slippage
  coverage, source-status counts and named cost-model provenance.
- `sample_confidence`: descriptive evidence volume, nominal win-frequency
  interval and independence/concentration warnings.
- `profitability.observed_direction`: POSITIVE, NEGATIVE, BREAKEVEN or UNKNOWN
  from the authoritative booked result.
- `profitability_verified`: evidence/cost/context verification of that observed
  result. It does not establish positive expectancy, statistical proof or
  suitability for strategy selection.

Verification requires a nonempty sample, a matching COMPLETE subgroup report,
observed strategy identity, a recognized executed partition, complete financial
inputs, actual close times, reliable context, known requested grouping values,
and complete coverage with named compatible models for all three cost components.
Every failed gate appears as a structured reason/blocker. A verified negative
result remains NEGATIVE. A high sample label with incomplete evidence remains
UNVERIFIED.

Paper funding remains UNMODELED, and separate actual slippage is currently not
persisted. These values stay NULL/UNKNOWN and block verification. Slippage is
not inferred merely because actual fill prices already contain its economic
effect. Measured slippage is reported separately without subtracting it from
already booked net P&L. This contract does not repair funding coverage or silently
merge book, execution-price and research cost models.

## Sample confidence

| Completed subgroup episodes | Descriptive classification |
|---|---|
| 0–29 | INSUFFICIENT |
| 30–74 | EARLY_EVIDENCE |
| 75–149 | DEVELOPING |
| 150–299 | STRONG_SAMPLE |
| 300+ | MATURE_SAMPLE |

Every subgroup computes these labels from its own count. An asset with 150
episodes cannot lend its STRONG_SAMPLE label to a 17-episode session/regime
subset. None of the labels implies a validated trading edge.

When every subgroup net result is known, the nominal 95% Wilson interval
describes the frequency of net-positive episodes. Breakevens are non-wins,
consistent with the existing win-rate denominator. With n episodes, k wins,
p=k/n, z=1.959963984540054 and d=1+z²/n:

`center = (p + z²/(2n))/d`

`half = z * sqrt(p(1-p)/n + z²/(4n²))/d`

The interval is `[max(0,center-half), min(1,center+half)]`, reported as
percentages. Zero/all-win boundary endpoints are exactly 0/100. An empty
sample or unknown net result yields NULL. Nominal binomial coverage assumes
independent comparable observations; trading dependence may invalidate it.

`TRADES_NOT_ASSUMED_INDEPENDENT` is always reported. Additional warnings cover
overlapping actual episode intervals, unavailable interval timestamps, shared
signal clusters and subgroups below 30 episodes. Return concentration warns
when the largest positive episode contributes at least 50% of positive net
returns and at least two winning episodes exist. This fixed descriptive
threshold is disclosed, not optimized or presented as a hypothesis test.

## Validation and measured aggregation cost

Tests first reproduced missing module behavior, then covered all scope/mode
dimensions, nonexecuted exclusion, partial exits, version/model isolation,
original-entry scale-in anchoring, exact subgroup binding, stale/future/mismatched
reports, independent subgroup labels, Wilson boundaries, costs, unknown context,
source enrichment integrity and Decimal reconciliation. The current focused
run including v1 and service regressions passed 107 tests on 9 October 2026.
Full-suite totals are recorded in `STRATEGY_INTELLIGENCE_SPRINT2.md`.

An isolated synthetic benchmark used 60 asset/session/regime partitions, all
Decimal-string economics, zero ledger access/writes, and `tracemalloc` enabled:

| Completed episodes | Groups | Measured seconds | Peak traced memory |
|---|---:|---:|---:|
| 1,000 | 60 | 0.7145 | 6.25 MiB |
| 10,000 | 60 | 6.9384 | 53.46 MiB |

Reproduce from `automation-hub`:

```bash
../.venv/bin/python scripts/benchmark_context_metrics.py \
  --episodes 1000 10000 --tracemalloc
```

The reproducible script uses no test fixtures, database or network access.
Raw host metadata, timings and reconciliation checks are committed in
`docs/STRATEGY_INTELLIGENCE_SPRINT2_METRICS_BENCHMARK.json`. Its explicitly
synthetic evidence remains UNKNOWN and cannot verify profitability.

The benchmark verified exact counts and net sums. Measurements are local,
synthetic, instrumentation-inclusive observations, not a production SLA or
end-to-end worker benchmark. Requests read derived caches rather than performing
these historical calculations. Source bounds and dirty/expired cache gates
remain producer responsibilities; load testing on an approved staging dataset
is still required before production deployment.
