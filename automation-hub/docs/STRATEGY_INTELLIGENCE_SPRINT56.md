# NEXUS Strategy Intelligence — Sprint 5.6

## Executive summary

Sprint 5.6 hardens Sprint 5.5 matched research around historical evidence integrity. The existing Binance/local-cache research path now has a canonical UTC candle contract, deterministic normalized manifests, finalized-candle filtering, half-open period verification, missing-candle failure, duplicate/conflict rejection and explicit evaluation coverage. Matched jobs propagate frozen period boundaries and comparisons consume coverage status when committed worker evidence is available.

No strategy rules, execution modes, risk settings, financial ledgers, balances, positions, orders or active champion state were changed. No production or staging environment was accessed.

## Repository audit findings

### Supported data paths

- `data.historical`: Binance public kline REST (`data-api.binance.vision`, then `api.binance.com`) cached in SQLite. Supported symbols are the configured USDT set and supported intervals are `5m` through `1w`.
- `data.market_data`: strict research calls use the real local cache or optional real ccxt/Yahoo paths. Bundled samples and deterministic synthetic data remain available to non-research/demo consumers, but `require_real=True` prevents those fallbacks for research.
- Higher-timeframe context uses the existing causal `mtf_engine` lookup and closed higher-timeframe indices. Legacy resampling is explicitly opt-in; the research path does not add future context.

The Binance fetcher supports bounded pagination and open-time ordering. Local-cache selection now accepts an end boundary. ccxt can filter returned bars by end boundary, but provider-specific historical pagination is not independently verified for every optional exchange; those paths remain a limitation and must fail closed when exact coverage cannot be established.

### Timestamp and period contract

Canonical candles use timezone-aware UTC timestamps at millisecond precision and candle-open semantics. Research periods are `[period_start, period_end)`: the start candle is eligible and the end candle is excluded. A period must be positive and aligned to the requested timeframe. Provider rows with an explicit future close time are not finalized and are excluded before caching.

### Warm-up and HTF findings

The existing simulator computes indicators from the bars supplied to it, while the causal HTF lookup uses the last closed higher-timeframe candle. Sprint 5.6 records a requested warm-up count and verifies warm-up availability before evaluation. Complete strategy-specific lookback partitioning for every strategy/provider remains a follow-up limitation; arbitrary period slicing is now explicit rather than silently ignored.

## Implemented changes

- Added `canonical_candle_manifest`, `slice_candles` and finalized-row validation in `automation-hub/data/historical.py`.
- Added exact `end_ms` propagation to the local market-data path and period fields to research-job API requests.
- Research workers now parse timezone-aware boundaries, fetch the requested range, split warm-up/evaluation candles, reject missing critical candles and persist coverage in the dataset manifest.
- Historical cache writes reject invalid OHLCV and conflicting duplicate candles instead of silently replacing evidence.
- Matched experiment job requests now carry common period boundaries and comparison overrides propagate dataset revision and coverage status into comparability checks.
- Holdout exposure queries detect overlapping intervals across experiment/campaign IDs; a new campaign cannot reset prior exposure.
- Dashboard matched experiments show dataset, period, warm-up, holdout and comparison eligibility indicators, using `UNKNOWN` when backend evidence is unavailable.

## Dataset identity and hashing

Canonical serialization sorts candles by timestamp and serializes normalized numeric OHLCV values with stable JSON separators. SHA-256 over this canonical content is the `content_hash`; the dataset identity includes symbol, timeframe and the hash prefix. Changing a candle changes the hash. Manifests retain provider, venue, market type, requested range, actual range, timestamp semantics, finality and quality status.

The original dataset revision is not reconstructed from a different source. A requested `dataset_identity` or `data_revision` mismatch remains a hard research failure.

## Evaluation coverage and warm-up

`slice_candles` verifies every expected interval in `[start,end)`, reports actual first/last timestamps, expected/evaluated counts, missing and duplicate counts, and returns separate `warmup` and `evaluation` collections. Missing critical candles raise `INSUFFICIENT_DATA`; no interpolation is performed. Warm-up-only candles are not part of the evaluation collection or manifest metrics.

Different strategies may produce different trade counts, but they receive the same verified eligible period when a matched experiment supplies the same scope. A candidate that cannot meet the required coverage is incomplete/not comparable rather than silently shortened.

## HTF and point-in-time safeguards

The existing `make_trend_lookup` uses `((i + 1) // factor) - 1`, selecting only the last closed aligned higher-timeframe candle. Sprint 5.6 preserves this causal behavior and does not enable legacy future resampling. Provider availability timestamps are not available on all paths; the conservative closed-candle/finality rule is used. The mandatory future-data invariant remains covered by the causal lookup tests and should be extended to every provider-backed research fixture.

## OOS and walk-forward integrity

Chronological partitioning and walk-forward window generation remain the existing qualification implementation. Research now supplies the verified bounded input before those partitions are calculated. Exact per-window coverage persistence and provider-level warm-up optimization are not fully implemented for every walk-forward variant; no optimizer is claimed when the frozen configuration path is used.

## Holdout exposure and multiple testing

Exposure records remain append-only and are now matched by overlapping dataset/interval ranges. Status definitions are:

- `UNTOUCHED`: first recorded exposure event before result selection influence.
- `EXPOSED`: a subsequent overlapping exposure exists.
- `REUSED`: repeated exposure beyond the first repeat.
- `CONTAMINATED`: reserved for policy-level determination after selection influence or repeated search.
- `UNKNOWN`: historical lineage is unavailable.

Candidate, campaign, failed-experiment and rejected-job counts remain in the existing research records. Formal statistical correction for dependent windows and broad parameter searches is not invented; comparison policies remain conservative and return inconclusive/unknown when evidence is insufficient.

## Recovery and immutable evidence

Sprint 4.6 worker leases, fencing, retries, cancellation and idempotent validation-run commitment remain the recovery mechanism. Sprint 5.5 candidate-to-job mappings remain idempotent. A completed matched experiment still requires committed candidate results; coverage and dataset references are carried in those immutable reports. No completed artifact is rewritten in place.

## API and dashboard

Research-job requests accept optional `period_start`, `period_end`, `dataset_identity` and `data_revision`. Existing matched experiment, lineage and holdout endpoints remain owner-scoped. The dashboard now exposes evidence-integrity fields and displays `UNKNOWN` when no verified backend evidence exists; it does not manufacture green verification indicators.

## Files modified

- `automation-hub/data/historical.py`
- `automation-hub/data/market_data.py`
- `automation-hub/data/journal_context_store.py`
- `automation-hub/services/research_jobs.py`
- `automation-hub/services/matched_experiments.py`
- `automation-hub/services/strategy_comparison.py`
- `automation-hub/routers/strategy_intelligence.py`
- `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx`
- `automation-hub/tests/test_historical_integrity.py`
- `automation-hub/tests/test_matched_experiments.py`

Existing Sprint 3–5 dirty changes were preserved. No new migration was required; coverage is stored in immutable validation report JSON and holdout overlap uses the existing append-only exposure table.

## Tests

Focused Sprint 5.6/backend validation: **97 passed, 4 warnings**. This includes canonical hashing, half-open slicing, missing-candle failure, finality filtering, conflicting duplicate rejection, overlapping holdout detection, research jobs, matched experiments, API, comparison and qualification regressions. The focused historical/research compatibility set subsequently passed **35 tests**.

Python compilation passed for all modified backend modules. Final full validation completed with Automation Hub **2,919 passed, 25 skipped, 85 warnings**, repository root **3,449 passed, 25 skipped, 84 warnings**, dashboard typecheck/build passed, and Chromium browser suite **136 passed**. The build emitted the existing large-chunk advisory warning.

## Status

- **IMPLEMENTED:** canonical candle normalization, deterministic hashing, exact local period boundaries, finalized-row filtering, critical-gap failure, duplicate/conflict rejection, coverage propagation, overlap-aware holdout history and dashboard evidence fields.
- **TESTED:** focused backend tests and Python compilation.
- **PARTIALLY IMPLEMENTED:** exact pagination/period guarantees for optional ccxt/Yahoo providers, strategy-specific lookback derivation, per-window coverage artifacts and formal multiple-testing adjustment.
- **BLOCKED:** production/staging provider verification, PostgreSQL multi-worker validation, retention/reconstruction testing and supervised worker deployment without authorization.
- **NOT IMPLEMENTED:** live trading, automatic promotion, strategy switching, financial-ledger changes or synthetic evidence repair.

## Operational readiness and Sprint 6 prerequisites

Remain blocked on independently supervised workers, PostgreSQL claim/fencing validation, hard OS resource isolation, production-shaped migrations, dataset retention/reconstruction, staging restart tests and provider-specific production data verification. Sprint 6 should begin only after full post-change Hub/root/browser validation and an authorized staging evidence-recovery exercise.
