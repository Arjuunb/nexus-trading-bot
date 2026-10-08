# Guardian: remaining work and completion gates

Updated 2026-10-08 from the current `codex/guardian-foundation` code and the
Guardian v1.0 PRD. This is a delivery checklist, not a production certification.

## Honest status

There is still **substantial work**: eight major remaining workstreams, each
potentially requiring several reviewable implementation/validation slices.
These are not "eight more commits" or an estimate of elapsed time. There is no
defensible percentage-complete estimate without agreeing the production
coverage/acceptance targets and the external integrations below.

The independent local service, immutable evidence store, credential separation,
bounded decision/lifecycle observers, incident grouping/investigation, several
SMC paper-history links, read-only standalone Command Center, evidence reports,
research registry, backup/recovery rehearsal and scoped self/queue/transport
diagnostics exist. The new HTTP admission phase limits producer write floods.
They are **locally tested**, not proof that every production evaluation/order
was captured. No Guardian deployment or producer activation was performed in
these local phases. Do not infer the current VPS state from this checklist.

## Remaining workstreams

| Workstream | Existing local evidence | Still required to close it |
| --- | --- | --- |
| 1. Complete source coverage and provenance | Post-install persisted PA/SMC/instance decision outboxes; bounded/restart-safe imports; optional emitter transport contract | Capture every required evaluation and failed-persistence outcome without altering alpha; exact strategy/version/commit/config identity; approved source/process inventory; production producer wiring; outage/replay completeness proof. |
| 2. Execution, journal and isolated risk integrity | PA/SMC retained fills, several SMC intent/fill/position/exit/journal/stop links, bounded current instance ledger pairing | Complete PA and all-instance lifecycle history; source-authoritative reconciliation of unresolved/racing links; currency/venue/account identity and correlated exposure; missing/legacy evidence remains unknown; never combine paper/live or unrelated lab accounts. |
| 3. Infrastructure, agents and causal evidence | Coarse public probe, lab-feed observations, declared graph, bounded incident timelines and typed latency read models | Actual host/container resource and agent telemetry; runtime-verified dependencies and recovery chains; required frequency/distribution/latency baselines with sufficient attributable samples; no causal claims from coincidence alone. |
| 4. Causal research runners | Owner-governed hypothesis/result/review registry; active strategy protected | Real isolated backtest/OOS/walk-forward/stress/forward-paper runners, causal datasets, statistical comparison and reproducibility; no automatic production rule application. |
| 5. Bounded reasoning integration | Structured evidence/read contracts; no model provider selected | Owner chooses provider/model/data policy/budget; bounded redacted evidence context, authorization, prompt-injection and cost/failure tests; reasoning remains advisory and cannot trade or deploy. |
| 6. Approved operational recovery | Diagnostic/recommendation boundaries; no automatic recovery authority inferred | Owner approves exact non-trading targets/actions/policy; audited commands, idempotency, verification, failure handling and rollback; no discretionary entries/exits, strategy/risk edits or live enablement. |
| 7. App integration and remote awareness | Separate read-only Command Center; local daily/weekly reports and deduplicated in-app notice records | One authenticated Guardian app sidebar entry with role/secret-safe read integration; approved remote notification destination, delivery/deduplication/outage tests; reports disclose incomplete evidence. |
| 8. Production security and acceptance | Optional isolated image/Compose contract; local consistent SQLite backup/recovery and overload tests | Actual isolated staging deployment; ingress/TLS/request timeout/key rotation, retention and storage sizing, workload/load/latency targets; persistence/restart/worker-death/Guardian-death/disk-full/restore/soak evidence against the real deployment configuration. |

## Mapping to the PRD's eight phases

1. Foundation: substantial local implementation; main-app integration and
   production isolation/availability acceptance remain.
2. Deep strategy telemetry: partial source coverage; every-evaluation and
   version/failure completeness are not yet proven.
3. Incident intelligence: bounded local analysis; complete source baselines
   and runtime-proven causal/dependency evidence remain.
4. Execution/risk intelligence: partial paper record associations; full
   reconciliation and verified isolated/global-currency risk remain.
5. Research intelligence: registry exists; actual isolated causal runners and
   statistical validation remain.
6. Guardian intelligence: provider-backed bounded reasoning is not built.
7. Controlled recovery: operational actuation is not built/authorized.
8. Reporting/remote awareness: local received-evidence reports/notices exist;
   remote delivery, complete report inputs and deployment proof remain.

Thus **none of the eight PRD phases is production-certified complete**. A green
local suite, successful HTTP response or "HEALTHY" diagnostic scoped to one
component cannot substitute for the above acceptance evidence.

## Suggested order after HTTP admission safety

1. Agree an explicit source coverage/identity matrix, then close gaps in
   evaluation/failure/version capture without changing protected strategies.
2. Complete paper execution/journal/exit lifecycle and currency/risk provenance.
3. Validate infrastructure/agent telemetry and causal/dependency baselines.
4. Add the single read-only app integration and an approved notification channel.
5. Run isolated staging acceptance: permissions, ingress, load, persistence,
   outage, disk-full, restore and paper-only soak. No automatic data deletion.
6. Build isolated research runners; introduce model reasoning and narrowly
   approved recovery only after their external choices/authority are supplied.

Independent workstreams may overlap after interfaces are stable, but never
substitute one workstream's tests for another's acceptance. Updates from other
branches/VPS must be ancestry-checked before integration or deployment.

## Choices/authority not inferred

No real credentials, model/provider, notification destination, recovery target,
source retention/deletion policy, workload SLA or production cutover is selected
by this checklist. Research strategy proposals are not permission to modify
protected SMC/PA alpha. Any needed choice is reported at its boundary; safe
local work can continue meanwhile.

Detailed existing contracts and validation records are in
[GUARDIAN_FOUNDATION.md](GUARDIAN_FOUNDATION.md).
