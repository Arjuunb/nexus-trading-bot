# Tradexa Guardian: evidence foundation

Status: local foundation, independently runnable service, read-only Command Center shell, and initial incident correlation. This is **not** a deployed Guardian service or a completed Phase 1.

## Boundaries

- `tradexa.guardian` contains no trading commands and imports no trading runtime.
- Guardian evidence belongs in its own owner-only SQLite database, never in a PA, SMC, instance, order, or journal database. WAL permits short writes beside readers. Replayed events with an identical ID and identical content are ignored; a conflicting replay fails. SQL triggers reject updates and deletes to raw events.
- Event `timestamp` is the source's aware clock; `received_at` is assigned by the store. This distinction is necessary for clock-skew and transport-lag investigations.
- Heartbeat state is current-state data, not immutable evidence. Missing, invalid, stale, or future heartbeats produce `UNKNOWN`, not `HEALTHY`. A heartbeat is **not** proof that strategy, feed, broker, and ledger are all healthy.
- Field validation rejects common secret-bearing names and values, oversized payloads, and non-JSON evidence. This is defense in depth, **not** a guarantee against every secret pattern. Producers must emit allowlisted, redacted evidence. Never send credentials, raw HTTP headers, or unrestricted exceptions.
- The database is operational evidence, not a tamper-proof external audit log. Its host owner can still alter SQLite or remove triggers. A production deployment needs restricted filesystem access, backups, and external integrity anchors.
- The standalone WSGI service runs with `python -m tradexa.guardian.service`, binds to `127.0.0.1:8765` by default, and imports no trading workers. Its process and database are separate from the trading app. No application or Compose startup path has been changed.
- The optional `GuardianEmitter` keeps a bounded in-memory queue and sends on a daemon thread. `emit()` does no network or database I/O. Queue overflow and delivery failures have counters; delivery is **best effort**, not durable. It must never replace order, position, risk, or journal persistence.
- The standalone Command Center at `/` is a shell, not the existing trading dashboard. It reads only Guardian's authenticated `/v1/health`, `/v1/events`, and `/v1/incidents` endpoints. No trading mutation endpoint or order control is present. It shows `UNKNOWN` until evidence is loaded, and still shows `UNKNOWN` if a read fails.
- The incident analyzer consumes the append-only event stream with a transactional cursor. Its incident/timeline tables are Guardian-owned derived views; the raw event is never edited or deleted. It groups a shared feed outage separately from worker, journal, and execution uncertainty. Routine strategy `condition_failed` and no-setup decisions are **not** incidents. Recovery requires an explicit source verification flag; a reconnect alone does not prove closed-candle continuity.

## Local service contract

Configuration requires `GUARDIAN_DB_PATH`, `GUARDIAN_SOURCE_KEYS_JSON` (a JSON object mapping each source service to its own long random key), and a distinct `GUARDIAN_READ_KEY`. Optional settings: `GUARDIAN_REQUIRED_COMPONENTS`, `GUARDIAN_BIND_HOST`, `GUARDIAN_PORT`. The service rejects reuse of `HUB_CONTROL_KEY` when it is present in its environment. Keep the database in a dedicated owner-only directory and supply secrets through a protected environment mechanism, not source control or shell history.

| Method | Path | Authority | Result |
| --- | --- | --- | --- |
| GET | `/` and `/assets/command-center.*` | local shell only | static read-only Command Center; no evidence or key embedded |
| GET | `/healthz` | local liveness probe | `self_state`, deliberately no claim that trading is healthy |
| POST | `/v1/events` | source-specific `X-Guardian-Key` | 201 appended, 200 identical replay, 403 source mismatch, 422 invalid evidence, 503 persistence unavailable |
| POST | `/v1/heartbeats` | source-specific `X-Guardian-Key` | current heartbeat for that source or a source-prefixed component |
| GET | `/v1/events?limit=50&source_service=smc_lab` | separate read key | paged recent immutable evidence |
| GET | `/v1/health` | separate read key | evidence-backed component health, with missing/stale components `UNKNOWN` |
| GET | `/v1/incidents?limit=50&state=OPEN` | separate read key | derived incident summaries, filtered by state when requested |
| GET | `/v1/incidents/<incident_id>/timeline` | separate read key | ordered source event references and state transitions |

For the authenticated endpoints, the header is `X-Guardian-Key: <the appropriate key>`. The event body is the JSON produced by `GuardianEvent.canonical_json()`. A producer must reuse its original `event_id` on retry. Its `source_service` must match the identity bound to the presented key. Raw `received_at` is store-owned and cannot be supplied by a producer.

The local WSGI server is deliberately simple and single-process. It is not yet rate-limited or hardened for public ingress; keep it on loopback. If remote ingestion is needed later, add a TLS gateway, network policy, request limits, and load tests before exposing it.

The Command Center asks for the separate read key when opened. It holds that key in the current page's memory only; disconnect clears it. Do not serve this page over plain HTTP except on loopback. The page uses a restrictive Content Security Policy and writes event strings as text rather than HTML. It shows the newest 50 Guardian events and incidents, **not** lifetime counts or proof of complete telemetry. A timeline can be opened for a visible incident; this is still local Guardian evidence, not a production root-cause guarantee.

## Required next work before production use

1. Package and operate Guardian as a separate process/container with its own persistent volume and credentials. Prove trading-worker and Guardian failures are isolated in a deployment environment.
2. Decide which producer facts need a durable outbox rather than the current best-effort queue. Never call the Guardian SQLite store synchronously from trading hot paths. Critical order and journal truth must remain in their existing durable ledgers.
3. Add external-ingress hardening: TLS gateway, rate limits, version negotiation, source rotation, and load tests. Do not expose the local evidence store or service publicly as-is.
4. Add evidence adapters for market feeds, workers, strategy decisions, risk gates, execution, and persistence. Instrument without altering decision rules or introducing a Guardian-dependent gate.
5. Connect the Command Center to validated evidence adapters and add source-specific observation age. Its current shell shows required component heartbeat age, event IDs, reasons, and derived incident timelines, but no trading-source adapters exist yet. A service health response alone must not claim the trading system is healthy.
6. Test worker death, Guardian death, transport backpressure, database lock/full conditions, secret redaction, replay, stale evidence, and permissions in the actual deployment configuration. Only then consider incident and recovery phases.

No live-routing, risk, strategy, deployment, or automatic-recovery change is part of this foundation.
