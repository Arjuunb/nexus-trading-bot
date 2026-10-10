# Strategy Intelligence calculation contract v1

Sprint 1 adds `services/strategy_intelligence_metrics.py` and a read-only
projection/inspection CLI. Existing dashboards and their response formulas are
unchanged. No calculation can submit an order, start an instance, mutate a
paper account, migrate a ledger, or enable live trading.

Sprint 1.5 adds authoritative completeness assessments and gates evidence
verification. The financial formulas and `calculation_version` stay v1;
verification metadata is additive. A caller-supplied `history_complete=True`
alone no longer verifies history or profitability. Its value is retained as
`history_complete_claimed` for compatibility diagnostics.

## Cohort and completed trade

Every calculation requires an explicit `EvidenceCohort`: strategy ID, observed
version, configuration fingerprint, owner, account, instance, lab, simulation
session, execution mode, source kind, and optional symbol. Required scope keys
must exist on every included episode. None is an explicit unknown value, never
a wildcard. Symbol=None aggregates symbols only within this exact cohort.
Missing version/configuration remain unknown; current configuration is never
attached to historical records. This internal calculator is not an access
authorization interface; future APIs must supply server-resolved owner/account
scope.

One completed statistical trade is one position episode from its first entry
until final closure. All explicitly linked entries, partial exits, remainder
legs, fees and funding belong to that episode. An open episode's realised legs
remain excluded from completed-trade statistics. A reversal closes the old
episode and starts a new one, following the existing execution receipts.
No coincident-symbol/time heuristic reconstructs historical episodes.

Only `evidence_kind=executed` and `status=closed` are eligible. Rejected,
cancelled, expired, shadow and counterfactual records never contribute. Forward
paper, historical backtest and replay/simulation remain separate execution
partitions. Generic `paper` mode requires explicit `source_kind=forward_paper`
to be classified as executed forward paper; an ambiguous source stays
unverified. Version, hash, account, instance, lab, session and source never pool.

## Formulas and missingness

`calculation_version=strategy_intelligence.v1` uses Decimal sums and exposes
financial amounts and ratios as decimal strings. Existing SQLite REAL/float
source precision cannot be recovered; conversion through source text does not
rewrite stored values or promise additional accounting precision.

| Result | Definition |
|---|---|
| Trade count / episode count | Number of eligible completed episodes, never number of ledger legs |
| Win rate | Episodes with positive authoritative net P&L / all completed episodes; unavailable if any net P&L is missing |
| Gross P&L | Sum of authoritative gross episode P&L; derive net + fees + funding only with confirmed cost coverage |
| Net P&L | Sum of authoritative persisted net episode P&L; costs are never subtracted again |
| Fees | Sum of confirmed episode costs exactly once |
| Funding | Signed cost: positive paid, negative received; UNKNOWN/UNMODELED never becomes verified zero |
| Net / gross PF | Sum of positive per-episode net / gross P&L divided by absolute sum of its negative per-episode values |
| PF without losses | NULL plus `NO_LOSSES`, not invented finite 99 or serialized Infinity |
| Episode net R | Authoritative net episode P&L / immutable captured episode entry risk, including explicit scale-in risk receipts |
| R expectation | Mean of known episode net R, with eligible R sample size and unknown/nonpositive-risk count |
| Drawdown | Peak-to-trough cumulative realised net P&L ordered by timezone-aware actual closure time, with episode ID tie-break |
| Streaks | Consecutive positive/negative completed episodes in the same closure order; breakeven resets both |
| Wins / losses / breakevens | Counts of strictly positive / strictly negative / zero net completed episodes; all NULL if any net amount is missing |
| Mean duration | Mean seconds from aware actual entry to aware final close for known nonnegative durations; disclose `duration_sample_size` |
| First / last close time | Earliest / latest aware actual close in this exact cohort; NULL unless all selected close times are known |
| Net R | Sum of known per-episode net R; NULL if no R sample exists; missing risks reduce sample size rather than inventing risk |

Missing financial amounts remain NULL. `known_net_pnl` and
`known_gross_pnl` are explicitly partial totals. Full totals/PF/win rate/DD
remain unavailable when their necessary inputs are missing. Original risk is
never inferred from a current or trailed stop. Cost reconciliation tolerates
at most 1e-10 to disclose pre-existing REAL arithmetic differences; values are
never adjusted. Conflicting repeated episode rows fail rather than selecting
a convenient outcome.

The projection reads journal tables in a SQLite read-only transaction and
shares `build_episode` with JournalStore. The source watermark hashes the
immutable scoped headers, legs and fill receipts. A new receipt changes it;
repeating a rebuild does not. More than the requested episode bound raises an
error instead of reporting a truncated performance result. Projections are
disposable output: no extra accounting store or cache is created.

## Evidence reliability and calculation metadata

`history_complete` is false for a journal-only rebuild. Successful capture does
not prove that an external ledger has no earlier history or missing receipts.
`calculation_timestamp` (also `calculated_at`) is the actual aware UTC time of
calculation. The two-store reader assesses bounded consistent snapshots of
each SQLite file, then rereads the financial source. A change during that
assessment makes the report UNKNOWN and requires another assessment. The
stores do not share an atomic transaction; a COMPLETE report describes its
specific assessed source view, not a permanent guarantee about future events.

The calculator accepts `completeness_report` only when its report version,
entire cohort, combined source watermark and exact episode-input digest match.
The input digest includes open episodes, so a new partial exit or closure
invalidates an old report. The supplied calculation time must be no earlier
than the report and no more than 300 seconds later. An unmatched, expired or
future report produces UNKNOWN with `completeness_binding_status` explaining
the mismatch. A pure calculator cannot independently authenticate a caller's
watermark; consumers must obtain it from the authoritative two-store assessor,
not accept a client-submitted assertion of completeness.

| Metadata | Meaning |
|---|---|
| `evidence_completeness_status` | COMPLETE, PARTIAL, UNKNOWN, CONFLICTED or RECOVERING from a matching current report; otherwise UNKNOWN |
| `profitability_verified` | True only for COMPLETE evidence, at least one completed episode, valid observed identity and recognized execution partition, complete net/gross/cost evidence, complete scope and actual close times |
| `ready_for_evidence_review` | Same evidence gate as `profitability_verified`; carries no strategy qualification, selection or execution authority |
| `history_complete` | True only when the exact supplied source/cohort report is COMPLETE; an empty fully assessed source can be complete without verifying profitability |
| `identity_verified` | Cohort has observed version, a valid SHA-256 fingerprint, owner/account and instance or lab; invalid explicit identity statuses fail this flag. This flag alone does not establish stored-history completeness |
| `financial_evidence_complete` | A nonempty completed sample has all net/gross amounts and confirmed fees/funding coverage |
| `cohort_evidence_complete` | No missing scope keys or missing episode IDs were discarded; different known cohorts are intentionally excluded |
| `source_watermark` | Digest of the assessed authoritative and journal facts when ledger-aware, or captured episode facts for a journal-only rebuild; the latter never verifies history |
| `last_successful_reconciliation` | Timestamp of the most recent persisted COMPLETE assessment matching supplied scope/cohort filters. With no cohort, the read-only CLI reports the journal-wide latest COMPLETE run, which can be from another scope and does not establish whole-ledger completeness. Recording PARTIAL successfully does not advance this value |
| `financial_precision` / `source_precision_notes` | Decimal calculations from source text; pre-existing float precision is disclosed and cannot be restored |
| `excluded` | Counts of missing scope, known other cohorts, nonexecuted records, open episodes and missing episode IDs |
| `duplicate_episode_rows` | Identical repeated inputs counted once. Differing inputs under the same episode ID raise a conflict |

`coverage` reports known net/gross episodes, unknown or nonpositive original
risk, valid actual close timestamps, duration sample size, confirmed fee and
funding status counts, derived gross samples, and exact versus tolerated cost
reconciliations. Financial outputs use NULL for unavailable full amounts and
explicit `known_*` names for partial sums. UNKNOWN and UNMODELED funding never
become verified zero. The current paper ledger does not model funding, so an
otherwise fully delivered completed paper cohort is PARTIAL for total cost
coverage. Its persisted net P&L remains the unchanged booked amount.

`strategy_evidence_completeness.v1` compares committed `paper_executions`
against immutable fill events using exact scoped execution IDs. Atomic SQLite
outbox rows additionally preserve original context, receipts and REDUCE
parent/remainder IDs. Delivery is at least once with idempotent logical
effects, not exactly-once delivery across stores. Missing exit observer
context can inherit only from an exact committed OPEN → REDUCE lineage;
current configuration, matching symbol or coincident time are never joins.
Recovery snapshots masked during an outage become usable only after canonical
configuration and source identity validate against immutable stored snapshots.

| Completeness status | Meaning |
|---|---|
| COMPLETE | Sources read completely; expected committed executions, fill receipts, journals, episode links, identity, original risk and costs agree |
| PARTIAL | Sources are readable but required captured facts, links, original risk or cost coverage are missing |
| UNKNOWN | Source reads fail or exceed a bound; historical transactions/positions lack authoritative receipts or durable attribution; completeness cannot be proved |
| CONFLICTED | Duplicate logical execution facts, mismatched committed IDs/scope/economics, conflicting stored configurations or unbacked fill events were found |
| RECOVERING | Recovery is in progress and recoverable gaps remain; conflicts and unknown source coverage retain precedence |

Machine-readable `counts` expose `expected_authoritative_events` and
`persisted_evidence_events` (committed executions and captured fill events),
missing/duplicate/conflicting events, unresolved episode references, missing
fingerprints, unknown modes, missing journal/close events and cost components.
`details` contains the actual IDs and reasons, including unreceipted history.
`financial_totals` compares authoritative closed financial legs with captured
closing fill receipts for net P&L and booked fees. Deltas and full totals are
NULL when amounts are unknown; `known_authoritative_*` and `known_evidence_*`
disclose partial amounts. A zero delta with incomplete delivery or cost
coverage does not verify profitability. Partial exits reconcile as constituent
legs and still contribute only one completed episode after final closure.

## Discrepancies from existing calculations and migration

`services/performance.py` counts supplied ledger rows with non-null P&L, rounds
financial results, returns 99 for winning samples without losses, and uses
persisted ledger `rr`. Its `gross_win`/`gross_loss` use `pnl`, which the primary
paper engine books net of commission. Reductions can make several rows for
one episode. Its caller supplies scope, so the calculator itself does not
enforce version/configuration/source isolation.

`services/research_analytics.py` groups shadow measurements by strategy ID
alone and includes any measurement with `net_r`, including rejected setup
counterfactual outcomes. Its drawdown follows input order rather than sorting
actual closure timestamps. These research responses retain their SHADOW label
and existing formulas for compatibility; they must not be consumed as v1
realised trading performance.

New consumers must explicitly select the v1 contract, display the exact cohort,
cost/R/history coverage and PF basis, and compare the old and new calculations
before changing a dashboard. Do not relabel legacy rows or write corrected
numbers into financial records. Unknown historical lineage/configuration is
quarantined. Rollback means reverting consumers while retaining additive
immutable journal metadata; the projection itself has no persisted state to
roll back.

## CLI and XRP evidence inspected

From `automation-hub`, inspect source coverage without importing the app:

```bash
../.venv/bin/python scripts/recompute_strategy_intelligence.py reconcile \
  --ledger-db /workspace/nexus-dev-data/ledger.db \
  --journal-db /workspace/nexus-dev-data/journal.db \
  --source-classification development
```

Both the local `/workspace/nexus-dev-data` stores and the checkout's
`automation-hub/logs` stores were inspected read-only on 9 October 2026. Each
had zero paper trades, zero trading instances, zero decision-journal records,
and zero decisions. They are development evidence and cannot verify production
performance. The reported XRPUSDT trade count, win rate, PF and positive net
P&L remain **UNVERIFIED**. Gross P&L, fees, funding, episode count, historical
version, fingerprint, execution mode and time period are also **UNVERIFIED**.
No historical values were populated and no primary records were rewritten.

To recompute one captured cohort, provide every `EvidenceCohort` dimension in
a JSON file and run:

```bash
../.venv/bin/python scripts/recompute_strategy_intelligence.py recompute \
  --ledger-db /path/to/ledger.db --journal-db /path/to/journal.db \
  --cohort-json /path/to/cohort.json \
  --source-classification production_export
```

Omitting `--ledger-db` preserves journal-only inspection with UNKNOWN history.
For a standalone machine-readable completeness report, use `completeness`
with both paths, optional `--cohort-json`, and an explicit `--max-records`
bound. These commands perform SELECTs through read-only SQLite connections;
they do not migrate an old export or replay financial transactions.

`production_export` is an explicit caller declaration, not independent proof
of provenance. No credentials, settings, secret values, or network connections
are required by these commands. An authoritative production export and an
explicit configuration/account/instance/mode/period cohort are prerequisites
to reconciling the XRP claim. Do not treat fixture or onboarding data as that
export.

## Intended Sprint 2 consumers

Future read-only strategy profiles, cohort comparisons, research validation
and episode drilldowns should consume this contract with explicit scope,
calculation time, completeness status, cost coverage and metric PF basis.
Any future strategy eligibility or market-regime system needs a separately
reviewed policy for incomplete history, sample requirements and version
boundaries. Evidence flags do not authorize orders or promotion. No Sprint 2
consumer, market-regime logic or dashboard formula migration is implemented
in Sprint 1.5.
