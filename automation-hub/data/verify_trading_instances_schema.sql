-- Read-only verification for the Trading Instance paper-forward runtime.
-- Expected result: zero rows. Any returned row names a missing table/column.
WITH required(table_name, column_name) AS (
  VALUES
    ('trading_instances', 'id'),
    ('trading_instances', 'symbol'),
    ('trading_instances', 'strategy_key'),
    ('trading_instances', 'strategy_label'),
    ('trading_instances', 'strategy_version'),
    ('trading_instances', 'timeframe'),
    ('trading_instances', 'risk_per_trade_pct'),
    ('trading_instances', 'capital_allocation'),
    ('trading_instances', 'max_open_positions'),
    ('trading_instances', 'sizing_mode'),
    ('trading_instances', 'fixed_position_size'),
    ('trading_instances', 'entry_mode'),
    ('trading_instances', 'fill_model'),
    ('trading_instances', 'execution_mode'),
    ('trading_instances', 'simulation_session_id'),
    ('trading_instances', 'simulation_session_number'),
    ('trading_instances', 'mode'),
    ('trading_instances', 'market_data_mode'),
    ('trading_instances', 'state'),
    ('trading_instances', 'desired_running'),
    ('trading_instances', 'created_at'),
    ('trading_instances', 'started_at'),
    ('trading_instances', 'stopped_at'),
    ('trading_instances', 'updated_at'),
    ('trading_instances', 'last_error'),
    ('instance_market_state', 'instance_id'),
    ('instance_market_state', 'last_processed_candle_timestamp'),
    ('instance_market_state', 'market_data_mode'),
    ('instance_market_state', 'market_data_status'),
    ('instance_market_state', 'last_market_data_timestamp'),
    ('instance_market_state', 'data_source'),
    ('instance_market_state', 'warmup_bars'),
    ('instance_market_state', 'duplicate_candles'),
    ('instance_market_state', 'missing_candles'),
    ('instance_market_state', 'out_of_order_candles'),
    ('instance_market_state', 'pending_orders_json'),
    ('instance_market_state', 'worker_heartbeat'),
    ('instance_market_state', 'updated_at'),
    ('instance_metrics', 'instance_id'),
    ('instance_metrics', 'data_json'),
    ('instance_metrics', 'updated_at'),
    ('instance_engine_logs', 'id'),
    ('instance_engine_logs', 'instance_id'),
    ('instance_engine_logs', 'ts'),
    ('instance_engine_logs', 'level'),
    ('instance_engine_logs', 'message'),
    ('positions', 'simulation_session_id'),
    ('paper_trades', 'simulation_session_id'),
    ('simulation_sessions', 'id'),
    ('simulation_sessions', 'instance_id'),
    ('simulation_sessions', 'session_number'),
    ('simulation_sessions', 'starting_balance'),
    ('simulation_sessions', 'ending_balance'),
    ('simulation_sessions', 'status'),
    ('simulation_sessions', 'started_at'),
    ('simulation_sessions', 'ended_at'),
    ('simulation_account_audit', 'action'),
    ('simulation_account_audit', 'instance_id'),
    ('simulation_account_audit', 'previous_session_id'),
    ('simulation_account_audit', 'new_session_id'),
    ('simulation_account_audit', 'previous_balance'),
    ('simulation_account_audit', 'new_balance'),
    ('simulation_account_audit', 'open_positions_cleared'),
    ('simulation_account_audit', 'pending_orders_cleared'),
    ('simulation_account_audit', 'timestamp'),
    ('simulation_account_audit', 'initiated_by'),
    ('simulation_account_audit', 'result'),
    ('trading_instances', 'owner_id'),
    ('trading_instances', 'config_revision'),
    ('instance_worker_leases', 'instance_id'),
    ('instance_worker_leases', 'worker_id'),
    ('instance_worker_leases', 'process_id'),
    ('instance_worker_leases', 'host'),
    ('instance_worker_leases', 'started_at'),
    ('instance_worker_leases', 'heartbeat_at'),
    ('instance_worker_leases', 'lease_expires_at'),
    ('trading_instance_platform_settings', 'id'),
    ('trading_instance_platform_settings', 'max_active_slots'),
    ('trading_instance_platform_settings', 'max_global_risk_pct'),
    ('trading_instance_platform_settings', 'max_global_daily_loss_pct'),
    ('trading_instance_platform_settings', 'paper_account_capital'),
    ('trading_instance_platform_settings', 'updated_at')
)
SELECT required.table_name, required.column_name
FROM required
LEFT JOIN information_schema.columns actual
  ON actual.table_schema = 'public'
 AND actual.table_name = required.table_name
 AND actual.column_name = required.column_name
WHERE actual.column_name IS NULL
ORDER BY required.table_name, required.column_name;

-- Expected result: exactly one row with is_primary_key=true and
-- has_owner_foreign_key=true. This proves one durable cursor row per instance.
SELECT
  EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.instance_market_state'::regclass
      AND contype = 'p'
  ) AS is_primary_key,
  EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.instance_market_state'::regclass
      AND contype = 'f'
      AND confrelid = 'public.trading_instances'::regclass
  ) AS has_owner_foreign_key;
