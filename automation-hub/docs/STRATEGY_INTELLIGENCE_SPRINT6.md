# NEXUS Strategy Intelligence — Sprint 6

## Executive summary

Sprint 6 adds a controlled, research-only agent layer. It turns persisted evidence into structured hypotheses, validates only allowlisted configuration changes, requires an explicit human approval for the frozen plan, and delegates approved work to the existing matched-experiment and durable research-worker services. The agent has no trading, account, risk-setting, secret, or arbitrary-code capability.

## Repository audit

The audit confirmed that strategy identity/fingerprints, research candidates, matched experiments, validation runs, holdout tracking and the leased research worker already exist and are reused. `services/strategy_agent.py` is a plain-language custom-strategy interpreter and is deliberately not used as the research agent. The supported Sprint 6 strategy allowlist currently contains `adaptive_trend_pullback`; a strategy-specific reviewed lookback specification is not yet present in the repository, so the agent requires one to be supplied and marked reviewed. Unknown or unspecified lookbacks remain `LOOKBACK_UNVERIFIED` and are blocked.

Dataset manifests and historical integrity checks remain the source of truth. A manifest hash alone does not prove that raw candles can be reconstructed after retention expiry; this remains an operational limitation and is surfaced as an evidence limitation rather than guessed around.

## Architecture and lifecycle

`READ_EVIDENCE → PROPOSE_HYPOTHESIS → deterministic policy gates → human approval → existing matched experiment → existing research jobs → committed validation evidence → advisory review`.

Hypotheses and approvals are tenant-scoped, plan-fingerprinted and persisted. A material plan change invalidates the approval. Failed, cancelled and incomplete jobs cannot be reviewed as completed evidence. The agent never writes financial records or active strategy configuration.

## Files and migration

Implemented files:

- `services/research_agent.py` — policy, allowlist validation, lookback gate, hypothesis/approval/submission/review orchestration.
- `data/journal_context_store.py` — tenant-scoped hypothesis, approval and append-only audit-event persistence.
- `data/journal_context_migrations.py` — additive `research_agent_hypotheses`, `research_agent_approvals`, `research_agent_events` tables and indexes.
- `routers/strategy_intelligence.py` — authenticated policy, hypothesis, validation, approval, submission, review and audit-event endpoints.
- `automation-hub-dashboard/src/pages/StrategyIntelligenceHub.tsx` — Research Agent workflow using real API actions.
- `tests/test_research_agent.py` — policy, evidence, approval invalidation and forbidden-action regression coverage.

The migration is additive and backward-compatible. Existing financial and trading tables are not changed. Rollback is a forward migration procedure (retain the additive tables until dependent records are exported/expired); no historical financial evidence is deleted.

## Hypothesis and policy model

Hypotheses store strategy/version, baseline and proposed fingerprints, the question, expected effect, evidence references, uncertainties, dataset scope, cost assumptions, methodology, lookback specification, policy version and lifecycle status. Evidence references are checked against committed `COMPLETED` validation runs before a hypothesis is created. Current lifecycle states are `DRAFT`, `APPROVED`, `REJECTED` and `SUBMITTED`; completed experiment state remains owned by the existing matched-experiment system.

The versioned `research-agent.v1` policy limits strategies, parameters, bars, walk-forward windows, Monte Carlo iterations, challengers, simultaneous jobs and runtime. Only `min_score`, `rr`, trend/volume/regime/session filters are research-tunable. Leverage, execution and risk parameters are rejected. Campaign hypothesis limits, allowlisted market types/methodologies, contaminated-holdout blocking and unknown funding blocking are enforced server-side.

## Approval and security

Approval records reviewer, timestamp, plan fingerprint, decision and resource budget. Submission requires the exact approved fingerprint and is owner/tenant scoped. All agent capabilities are an explicit allowlist; forbidden actions (orders, live/paper changes, leverage, champion promotion and code execution) are rejected. Model-generated text, if later added, remains untrusted input and cannot bypass deterministic validation.

## APIs and dashboard

Added under the existing Strategy Intelligence router:

- `GET /research-agent/policy`
- `GET/POST /research-agent/hypotheses`
- `GET /research-agent/hypotheses/{id}`
- `POST .../{id}/validate`, `/approve`, `/submit`
- `GET .../{id}/review`
- `GET /research-agent/events`

The dashboard includes a Research Agent tab for selecting an evidence candidate, proposing an allowlisted variation, reviewing validation, approving and submitting. It displays unavailable/blocked states and does not display fabricated metrics.

## Research memory and limitations

Append-only agent events provide research memory for proposals, validation, approval and submission, while existing campaign/experiment lineage retains successful and unsuccessful research. No autonomous model retraining is claimed. Raw dataset reconstruction after retention expiry, independently supervised workers, PostgreSQL multi-worker validation, hard OS sandboxing and production-shaped migration validation remain deployment prerequisites.

## Tests

Sprint 6 focused tests: **4 passed** (`tests/test_research_agent.py`); final focused agent/research regression rerun: **28 passed**. Automation Hub full suite: **2,923 passed, 25 skipped, 83 warnings**. Repository-root full suite: **3,453 passed, 25 skipped, 85 warnings**. Dashboard typecheck and production build: **passed**. Chromium browser suite, including the Research Agent flow: **137 passed**. The full suites and browser suite were run before the final lookback-spec tightening; the final focused rerun covers that change. The warnings are existing FastAPI lifecycle and aiohttp cleanup deprecations plus the dashboard chunk-size warning.

## Status

**IMPLEMENTED:** policy/allowlist, immutable plan fingerprinting, approval invalidation, lookback gate for the supported strategy, tenant-scoped persistence, authenticated APIs, dashboard workflow and focused tests.

**TESTED:** focused agent and reused research tests; dashboard typecheck/build.

**PARTIALLY IMPLEMENTED:** dataset retention/reconstruction proof, strategy-specific lookback derivation (reviewed specifications are accepted explicitly but not inferred), and independently supervised worker deployment.

**BLOCKED:** production/staging readiness until authorized PostgreSQL, migration, worker-restart, resource-isolation and dataset-retention validation is performed.

**NOT IMPLEMENTED:** autonomous champion promotion, live/paper execution changes, arbitrary code execution, LLM self-training or automatic deployment.

## Sprint 7 prerequisites

Authorize staging-shaped migration and PostgreSQL fencing tests, establish retained/reconstructable dataset snapshots, supervise the worker as an independently started service, and extend reviewed lookback specifications for each newly allowlisted strategy before adding it to the agent policy.
