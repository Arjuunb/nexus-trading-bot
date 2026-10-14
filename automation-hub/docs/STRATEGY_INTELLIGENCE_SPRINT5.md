# NEXUS Strategy Intelligence — Sprint 5

## Executive summary

Sprint 5 adds a separate advisory Champion/Challenger research layer. Frozen
strategy candidates are identified by strategy version, configuration
fingerprint, dataset revision, scope and execution mode. Candidates are
compared only when their evidence is compatible and complete enough to support
the requested calculation. Results are immutable research artifacts and never
alter active strategies, paper accounts, ledgers, orders or risk settings.

## Architecture changes

The existing journal remains the authority for research metadata. New
candidate, champion-designation and comparison tables are additive. The
comparison service consumes committed `strategy_validation_runs`; it does not
duplicate the backtester or worker queue. The existing Sprint 4.6 worker and
lease fencing remain unchanged.

## Candidate identity and champion registry

Candidate IDs are deterministic SHA-256 identities over strategy/version,
configuration, dataset/revision, scope and software/cost metadata. Different
fingerprints or versions cannot be merged. Champion designations are
append-only, owner-scoped and include reviewer, reason, policy, scope and
evidence reference. No initial champion is invented; an empty scope returns
`NO_RESEARCH_CHAMPION_SELECTED`.

## Comparability rules

The gate checks dataset identity and revision, period, venue, market type,
symbol, timeframe, execution mode, cost model, funding coverage, slippage,
method, OOS status and classifier version. Configuration fingerprints are
deliberately excluded from equality. Any mismatch produces
`NOT_COMPARABLE` with explicit reasons. Unknown or incomplete evidence is never
treated as zero.

## Matched comparison and uncertainty

The current implementation compares already-committed validation artifacts and
stores all validation references. It calculates expectancy and drawdown
deltas, uses seeded bootstrap intervals when episode outcomes are supplied in
the candidate scope, and records limitations around temporal dependence. It
does not force one-to-one trade pairing. A comparison with unknown evidence,
cost coverage or metrics returns `INSUFFICIENT_EVIDENCE`; positive improvement
under all configured checks returns `CHALLENGER_PREFERRED`; otherwise the
result is `CHAMPION_RETAINED` or `INCONCLUSIVE`.

## Challenger policy and degradation

`ChallengerEvaluationPolicy` is versioned and includes minimum sample,
expectancy improvement, drawdown regression, cost, bootstrap and confidence
settings. Checks are explicit PASS/FAIL/UNKNOWN. Degradation is advisory only:
with insufficient independent results it returns `UNKNOWN`; otherwise it
reports `STABLE`, `WATCH` or `DEGRADED` from observed expectancy movement.

## API and dashboard

Added owner-scoped endpoints under the existing Strategy Intelligence router:

* `POST/GET /research-candidates`
* `POST/GET /research-champions`
* `POST/GET /comparisons`
* `GET /research-candidates/{id}/degradation`

The dashboard now exposes a Champion / Challenger tab with candidate selection,
comparison submission, recommendation display and immutable history. It is
explicitly advisory and has no execution hooks.

## Database migrations

`journal_context_migrations.py` adds immutable candidate, champion registry and
comparison tables plus owner/scope indexes. Payloads and references are stored
with normalized identity columns for auditability and idempotency. Financial
schema is untouched. Production-shaped migration execution remains pending
authorization.

## Security and authorization

All routes use the existing authenticated owner resolver and tenant-scoped
reads. Candidate designation requires an authenticated reviewer, an explicit
scope and reason, and a supporting candidate. Unknown candidate IDs and
cross-tenant reads fail closed. No arbitrary code, path, order, account or
execution-mode mutation is exposed.

## Tests and validation

Added candidate identity, incompatible-period, deterministic/idempotent
comparison, champion designation, degradation, API authorization and tenant
scope tests. Current focused comparison/intelligence run: **85 passed, 4
warnings**. Dashboard typecheck and production build passed.

Final validation on the completed Sprint 5 tree:

* Automation Hub: **2,907 passed, 25 skipped, 86 warnings**.
* Repository root: **3,437 passed, 25 skipped, 84 warnings**.
* Chromium browser suite using `/usr/bin/chromium`: **10 passed**.
* Dashboard typecheck and production build: passed.

The earlier audit subprocess briefly observed two failures while the additive
migration was being edited; the clean final root run is fully green.

## Partially implemented

* Matched research execution is not automatically submitted by comparison
  creation; callers must provide committed validation references. This avoids
  silently selecting datasets or running unbounded work.
* Bootstrap support uses candidate-provided outcome blocks; a full block
  bootstrap over simulator episodes is planned once the research artifact
  contract exposes those blocks consistently.
* Multiple-challenger orchestration and explicit holdout-search lineage are
  represented by immutable artifacts but not yet surfaced as a dedicated
  experiment manager.

## Blocked and not implemented

* No PostgreSQL production migration or multi-process worker validation was
  authorized.
* No automatic promotion, strategy switching, live trading, or paper-mode
  change exists.
* No production deployment occurred.
* Production-shaped migration validation remains to be authorized.

## Staging prerequisites

Authorize a production-shaped migration, rerun the full root suite after the
final tree is stable, add authenticated browser fixtures for candidate and
comparison flows, and validate multi-tenant designation policy in staging.

## Sprint 6 prerequisites

Define the approved human-review workflow, matched-period research-job
orchestration, multiple-testing/holdout lineage policy, and a supported
PostgreSQL worker topology before adding any operational strategy-selection
consumer.
