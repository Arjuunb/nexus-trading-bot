# XRPUSDT Adaptive MTF evidence required for verification

The reported **22 completed trades, 40.9% win rate, 1.13 profit factor and +$0.84 net realised P&L remain UNVERIFIED**. The Sprint 1.5 inspection found no XRP-prefixed symbols in direct `symbol`, `ticker` or `pair` columns across 149 local development databases and backups (435 tables). It did not filter on strategy names or IDs, so an instance/version attribution alias cannot hide matching XRP rows from this search. Both current primary ledgers have zero `paper_trades`, `positions` and `paper_executions`; both current decision stores and journals contain no trading evidence for the claim.

These observations describe development data, not production performance. Zero local records do not establish zero production trades. The machine-readable result is [XRP_ADAPTIVE_MTF_RECONCILIATION_SPRINT15.json](XRP_ADAPTIVE_MTF_RECONCILIATION_SPRINT15.json). No production endpoint was contacted, no financial row was changed, no migration was run against these sources, and no historical configuration was inferred from current defaults.

## Missing claim scope

Verification needs the exact source behind the reported numbers, its capture/cutoff time and the following cohort values:

| Field | Required information |
| --- | --- |
| Strategy | Historical canonical strategy ID, observed version and any ledger `strategy_id` attribution alias mapping. Current `adaptive_trend_pullback` version `1.0.0` is not proof of a historical version. |
| Configuration | Original immutable effective configuration JSON and SHA-256 fingerprint, plus source identity if it was recorded. An absent snapshot remains unknown. |
| Ownership | Instance ID, owner/tenant ID, paper account ID, simulation session ID and lab ID. Unknown/null fields must be supplied as unknown, not replaced with current values. |
| Execution | Persisted execution mode and source kind: forward paper, historical backtest, simulation/replay or other. A UI label alone does not establish forward execution. |
| Period | UTC beginning, end and original inclusion rule. State whether trades are counted by opening or final closure and identify the reporting currency. |
| Profit factor | Whether the original 1.13 used gross or net episode P&L, handling of zero-loss cohorts, fees and funding. |
| Count | Whether the original 22 were closed ledger legs or completed position episodes. Partial exits are not separate completed statistical trades. |

## Safe data package

The preferred input is an operator-generated, consistent **read-only production export** uploaded for inspection. Explicitly authorized read-only access to the relevant production tables is an alternative; it has not been authorized or attempted in this sprint. No credentials, API keys, session cookies or service-role tokens belong in the package.

For SQLite, use an online backup or a quiesced consistent snapshot that includes committed WAL state. Copying a live `.db` file alone can miss committed events. For a remote database, export from a consistent snapshot where available, with schema/version, extraction time, cutoff watermark, table counts, ordering, every page and checksums. Since journal and ledger do not share a transaction, record each cutoff and reconcile receipts through an explicitly stated common watermark. Stable pseudonymous owner/account IDs are sufficient if the mapping remains consistent across all stores.

Export full constituent lineage for every included episode, including entries before the reporting window and the final closure. Include open episodes at cutoff separately. Keep historical rows unchanged and retain null values. Export representative raw financial values with their original precision/type; rounding to the displayed cents is inadequate for independent reconciliation.

## Tables and fields

The following names describe the current paper ledger and Sprint 1 journal schema. Include equivalent fields where the actual historical/remote schema differs, document the mapping and include its schema definition. Do not invent missing columns.

| Source | Required fields/data |
| --- | --- |
| `paper_executions` | `execution_id`, `action`, `position_id`, `trade_id`, `instance_id`, `created_at`; every committed OPEN, REDUCE and CLOSE receipt, including duplicates/conflicts if detected. |
| `paper_trades` | `id`, `alert_id`, `symbol`, `side`, `size`, `entry`, `stop`, `target`, `exit`, `pnl`, `realized_pnl`, `fees`, `rr`, `status`, `source`, `opened_at`, `closed_at`, `strategy_id`, `instance_id`, `tenant_id`, `simulation_session_id`, `risk_amount_at_entry`, `risk_basis_at_entry`, `risk_pct_at_entry`, `equity_before_trade`, `equity_after_close`, `sizing_mode`, `sizing_engine_version`. |
| `positions` | `id`, `symbol`, `side`, `size`, `entry`, `stop`, `target`, `management_json`, `status`, `pnl`, `opened_at`, `closed_at`, `instance_id`, `tenant_id`, `simulation_session_id`. Management/parent/remainder IDs are needed to separate a partial-exit continuation from a fresh entry or reversal. |
| `trading_instances`, `simulation_sessions` | Stable IDs and historical session/owner/strategy/version/mode assignments, session boundaries and authoritative realised equity totals. Current mutable instance settings do not recover historical configuration. |
| `decisions` | `id`, `decision_identity`, `ts`, `decided_at`, `symbol`, `strategy`, `strategy_id`, `strategy_version`, `strategy_config_hash`, `source_hash`, `decision`, `executed`, `final_state`, `instance_id`, `simulation_session_id`, `tenant_id`, `owner_id`, `account_id`, `lab_id`, `execution_mode`, `source_kind` and captured components where present. |
| `trade_decision_journal` | Entry/exit economics and sections plus `trade_id`, `position_id`, `execution_id`, `signal_id`, `decision_id`, `order_id`, `episode_id`, `parent_trade_id`, `strategy_id`, `strategy_version`, `strategy_config_hash`, `source_hash`, `identity_status`, `initial_risk_amount_text`, `evidence_schema_version`, distinct `signal_at`, `decision_at`, `executed_at`, `created_at`, `closed_at` and all ownership/mode/source fields. |
| `strategy_evidence_versions` | `strategy_id`, `strategy_version`, `strategy_config_hash`, `configuration_json`, `source_hash`, `identity_json`, `captured_at`. |
| `strategy_evidence_events` | Stable `event_id`, `kind`, immutable `payload_json` and `envelope_json`, capture/observation timestamps, all cohort fields and signal/decision/order/execution/trade/position/episode references. Include close preparation and actual committed fill records. |
| `strategy_position_episodes`, `strategy_episode_legs` | Episode/root/parent/leg IDs, entry event references, metadata, original risk and full cohort fields. Include scale-ins, every partial exit and final close. |
| Cost and recovery records | Authoritative funding bookings or an explicit persisted funding-model/coverage policy; fees split/booked consistently; any original execution context, reconciliation delivery records, completeness reports and conflicts available after Sprint 1.5. |

Legacy `paper_executions` supplies stable IDs and commit timing, but not all original configuration, economics or parent lineage by itself. A current version assignment or a nearby journal row cannot fill these gaps safely. Existing historical rows may support financial reconciliation while their strategy configuration or funding coverage still prevents verified analytics.

## Independent checks after data is available

Match committed receipts to fills and legs using stable IDs and exact ownership scope. Detect orphan references, duplicate logical events and conflicts before calculating. Reconstruct each flat-to-flat episode, reconcile partial quantities and constituent realised P&L, and compare episode sums with authoritative closed rows and account/session totals. Attribute booked fees once and funding according to its recorded coverage and sign convention; unknown or unmodeled funding is not verified zero.

Calculate completed episode count, wins/count and both gross/net profit factors without mixing replay, historical backtest, rejection or counterfactual outcomes into forward-paper realised P&L. Preserve decimal precision, record calculation time and completeness status, and compare with the four reported values using their declared rounding rules. If the original historical identity, cohort, lineage, costs or complete authoritative dataset are still unavailable, keep the affected metrics UNVERIFIED and report the specific missing evidence.
