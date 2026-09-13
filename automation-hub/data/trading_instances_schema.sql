-- Trading Instance migration for Supabase/Postgres (additive; safe to rerun).
ALTER TABLE positions ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE positions ADD COLUMN IF NOT EXISTS target DOUBLE PRECISION;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS management_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS simulation_session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS target DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS strategy_id TEXT NOT NULL DEFAULT '';
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS sizing_mode TEXT;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS sizing_engine_version TEXT;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS risk_basis_at_entry DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS risk_pct_at_entry DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS risk_amount_at_entry DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS equity_before_trade DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS equity_after_close DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS fees DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS realized_pnl DOUBLE PRECISION;
ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS simulation_session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS paper_executions (
 execution_id TEXT PRIMARY KEY,
 action TEXT NOT NULL CHECK (action IN ('OPEN','REDUCE','CLOSE')),
 position_id TEXT NOT NULL,
 trade_id TEXT NOT NULL,
 instance_id TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_paper_executions_instance
 ON paper_executions(instance_id, created_at DESC);

-- Position and accounting-trade rows are one logical unit. PostgREST calls are
-- individually transactional, so expose one RPC for each compound mutation.
CREATE OR REPLACE FUNCTION public.paper_open_atomic(p_payload JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
 p JSONB := p_payload->'position'; t JSONB := p_payload->'trade';
 v_pid TEXT := p->>'id'; v_tid TEXT := t->>'id'; v_now TIMESTAMPTZ := NOW();
BEGIN
 INSERT INTO positions
  (id,symbol,side,size,entry,stop,target,management_json,status,pnl,opened_at,instance_id,simulation_session_id)
 VALUES
  (v_pid,p->>'symbol',p->>'side',(p->>'size')::DOUBLE PRECISION,
   (p->>'entry')::DOUBLE PRECISION,(p->>'stop')::DOUBLE PRECISION,
   (p->>'target')::DOUBLE PRECISION,COALESCE(p->'management','{}'::JSONB),
   'open',0,v_now,COALESCE(p->>'instance_id',''),COALESCE(p->>'simulation_session_id',''));
 INSERT INTO paper_trades
  (id,alert_id,symbol,side,size,entry,stop,target,status,opened_at,strategy_id,
   instance_id,simulation_session_id,sizing_mode,sizing_engine_version,
   risk_basis_at_entry,risk_pct_at_entry,risk_amount_at_entry,equity_before_trade,fees)
 VALUES
 (v_tid,t->>'alert_id',t->>'symbol',t->>'side',(t->>'size')::DOUBLE PRECISION,
   (t->>'entry')::DOUBLE PRECISION,(t->>'stop')::DOUBLE PRECISION,
   (t->>'target')::DOUBLE PRECISION,'open',v_now,COALESCE(t->>'strategy_id',''),
   COALESCE(t->>'instance_id',p->>'instance_id',''),
   COALESCE(t->>'simulation_session_id',p->>'simulation_session_id',''),
   t->>'sizing_mode',t->>'sizing_engine_version',(t->>'risk_basis_at_entry')::DOUBLE PRECISION,
   (t->>'risk_pct_at_entry')::DOUBLE PRECISION,(t->>'risk_amount_at_entry')::DOUBLE PRECISION,
   (t->>'equity_before_trade')::DOUBLE PRECISION,0);
 INSERT INTO paper_executions(execution_id,action,position_id,trade_id,instance_id,created_at)
 VALUES (p_payload->>'execution_id','OPEN',v_pid,v_tid,
         COALESCE(t->>'instance_id',p->>'instance_id',''),v_now);
 RETURN jsonb_build_object('position_id',v_pid,'trade_id',v_tid);
END $$;

CREATE OR REPLACE FUNCTION public.paper_close_atomic(p_payload JSONB)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE p_count INTEGER; t_count INTEGER; v_now TIMESTAMPTZ := NOW();
BEGIN
 UPDATE positions SET status='closed',pnl=(p_payload->>'pnl')::DOUBLE PRECISION,closed_at=v_now
  WHERE id=p_payload->>'position_id' AND status='open'
    AND (COALESCE(p_payload->>'instance_id','')='' OR instance_id=p_payload->>'instance_id');
 GET DIAGNOSTICS p_count = ROW_COUNT;
 UPDATE paper_trades SET status='closed',exit=(p_payload->>'exit_price')::DOUBLE PRECISION,
  pnl=(p_payload->>'pnl')::DOUBLE PRECISION,rr=(p_payload->>'rr')::DOUBLE PRECISION,
  fees=COALESCE((p_payload->>'fees')::DOUBLE PRECISION,0),
  realized_pnl=COALESCE((p_payload->>'realized_pnl')::DOUBLE PRECISION,(p_payload->>'pnl')::DOUBLE PRECISION),
  equity_after_close=(p_payload->>'equity_after_close')::DOUBLE PRECISION,closed_at=v_now
  WHERE id=p_payload->>'trade_id' AND status='open'
    AND (COALESCE(p_payload->>'instance_id','')='' OR instance_id=p_payload->>'instance_id');
 GET DIAGNOSTICS t_count = ROW_COUNT;
 IF p_count <> 1 OR t_count <> 1 THEN
  RAISE EXCEPTION 'paper close invariant failed: position %, trade %', p_count, t_count;
 END IF;
 INSERT INTO paper_executions(execution_id,action,position_id,trade_id,instance_id,created_at)
 VALUES (p_payload->>'execution_id','CLOSE',p_payload->>'position_id',p_payload->>'trade_id',
         COALESCE(p_payload->>'instance_id',''),v_now);
END $$;

CREATE OR REPLACE FUNCTION public.paper_reduce_atomic(p_payload JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
 p JSONB := p_payload->'position'; rp JSONB := p_payload->'remainder_position';
 rt JSONB := p_payload->'remainder_trade'; p_count INTEGER; t_count INTEGER;
 v_now TIMESTAMPTZ := NOW();
BEGIN
 UPDATE positions SET status='closed',pnl=(p_payload->>'pnl')::DOUBLE PRECISION,closed_at=v_now
  WHERE id=p->>'id' AND status='open'
    AND (COALESCE(p_payload->>'instance_id','')='' OR instance_id=p_payload->>'instance_id');
 GET DIAGNOSTICS p_count = ROW_COUNT;
 UPDATE paper_trades SET status='closed',exit=(p_payload->>'exit_price')::DOUBLE PRECISION,
  pnl=(p_payload->>'pnl')::DOUBLE PRECISION,rr=(p_payload->>'rr')::DOUBLE PRECISION,
  size=(p_payload->>'closed_size')::DOUBLE PRECISION,
  fees=(p_payload->>'fees')::DOUBLE PRECISION,realized_pnl=(p_payload->>'pnl')::DOUBLE PRECISION,
  equity_after_close=(p_payload->>'equity_after_close')::DOUBLE PRECISION,closed_at=v_now
  WHERE id=p_payload->>'trade_id' AND status='open'
    AND (COALESCE(p_payload->>'instance_id','')='' OR instance_id=p_payload->>'instance_id');
 GET DIAGNOSTICS t_count = ROW_COUNT;
 IF p_count <> 1 OR t_count <> 1 THEN
  RAISE EXCEPTION 'paper reduce invariant failed: position %, trade %', p_count, t_count;
 END IF;
 INSERT INTO positions
  (id,symbol,side,size,entry,stop,target,management_json,status,pnl,opened_at,instance_id,simulation_session_id)
 VALUES
  (rp->>'id',rp->>'symbol',rp->>'side',(rp->>'size')::DOUBLE PRECISION,
   (rp->>'entry')::DOUBLE PRECISION,(rp->>'stop')::DOUBLE PRECISION,
   (rp->>'target')::DOUBLE PRECISION,COALESCE(rp->'management','{}'::JSONB),
   'open',0,v_now,COALESCE(rp->>'instance_id',''),COALESCE(rp->>'simulation_session_id',''));
 INSERT INTO paper_trades
  (id,alert_id,symbol,side,size,entry,stop,target,status,opened_at,strategy_id,
   instance_id,simulation_session_id,sizing_mode,sizing_engine_version,
   risk_basis_at_entry,risk_pct_at_entry,risk_amount_at_entry,equity_before_trade,fees)
 VALUES
 (rt->>'id',rt->>'alert_id',rt->>'symbol',rt->>'side',(rt->>'size')::DOUBLE PRECISION,
   (rt->>'entry')::DOUBLE PRECISION,(rt->>'stop')::DOUBLE PRECISION,
   (rt->>'target')::DOUBLE PRECISION,'open',v_now,COALESCE(rt->>'strategy_id',''),
   COALESCE(rt->>'instance_id',''),COALESCE(rt->>'simulation_session_id',''),
   rt->>'sizing_mode',rt->>'sizing_engine_version',(rt->>'risk_basis_at_entry')::DOUBLE PRECISION,
   (rt->>'risk_pct_at_entry')::DOUBLE PRECISION,(rt->>'risk_amount_at_entry')::DOUBLE PRECISION,
   (rt->>'equity_before_trade')::DOUBLE PRECISION,0);
 INSERT INTO paper_executions(execution_id,action,position_id,trade_id,instance_id,created_at)
 VALUES (p_payload->>'execution_id','REDUCE',rp->>'id',rt->>'id',
         COALESCE(p_payload->>'instance_id',rp->>'instance_id',''),v_now);
 RETURN jsonb_build_object('position_id',rp->>'id','trade_id',rt->>'id');
END $$;

REVOKE ALL ON FUNCTION public.paper_open_atomic(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.paper_close_atomic(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.paper_reduce_atomic(JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.paper_open_atomic(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.paper_close_atomic(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.paper_reduce_atomic(JSONB) TO service_role;

CREATE TABLE IF NOT EXISTS trading_instances (
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy_key TEXT NOT NULL,
 strategy_label TEXT NOT NULL, strategy_version TEXT NOT NULL, timeframe TEXT NOT NULL,
 risk_per_trade_pct DOUBLE PRECISION NOT NULL, capital_allocation DOUBLE PRECISION NOT NULL,
 mode TEXT NOT NULL, state TEXT NOT NULL, desired_running BOOLEAN NOT NULL DEFAULT FALSE,
 created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, last_error TEXT NOT NULL DEFAULT ''
);
-- CREATE TABLE IF NOT EXISTS does not repair a table from a partially applied
-- migration.  Keep every runtime column additive as well, so this file is the
-- one canonical recovery migration for existing production databases.
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS symbol TEXT NOT NULL DEFAULT '';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS strategy_key TEXT NOT NULL DEFAULT '';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS strategy_label TEXT NOT NULL DEFAULT '';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS strategy_version TEXT NOT NULL DEFAULT 'builtin-1';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS timeframe TEXT NOT NULL DEFAULT '5m';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS risk_per_trade_pct DOUBLE PRECISION NOT NULL DEFAULT 0.005;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS capital_allocation DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS exchange TEXT NOT NULL DEFAULT 'inherit';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS instrument_type TEXT NOT NULL DEFAULT 'spot';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'trading';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'stopped';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS desired_running BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS market_data_mode TEXT NOT NULL DEFAULT 'paper_forward';
-- Instance-owned execution configuration.  Existing rows retain the prior
-- paper defaults; no legacy global setting is copied or guessed.
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS sizing_mode TEXT NOT NULL DEFAULT 'fixed_starting_equity_percent';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS fixed_position_size DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS fixed_quantity DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS profit_reinvestment BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS maximum_risk_amount DOUBLE PRECISION;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS minimum_equity DOUBLE PRECISION;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS starting_equity DOUBLE PRECISION;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS current_realized_equity DOUBLE PRECISION;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS risk_basis DOUBLE PRECISION;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS sizing_engine_version TEXT NOT NULL DEFAULT 'v2';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS entry_mode TEXT NOT NULL DEFAULT 'limit';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS fill_model TEXT NOT NULL DEFAULT 'RealisticFill';
-- Existing rows deliberately retain their recorded model. Only future inserts
-- that omit the explicit application value inherit realistic paper execution.
ALTER TABLE trading_instances ALTER COLUMN fill_model SET DEFAULT 'RealisticFill';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'paper';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS simulation_session_id TEXT NOT NULL DEFAULT '';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS simulation_session_number INTEGER NOT NULL DEFAULT 0;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS stopped_at TIMESTAMPTZ;
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS max_open_positions INTEGER NOT NULL DEFAULT 3;
-- Legacy "auto" used a fixed constructor-time equity snapshot. Preserve that
-- behaviour explicitly; operators must opt into dynamic compounding.
UPDATE trading_instances
SET sizing_mode = CASE
  WHEN sizing_mode IN ('auto', 'fixed_starting_equity_pct') THEN 'fixed_starting_equity_percent'
  WHEN sizing_mode IN ('fixed', 'fixed_position') THEN 'fixed_quantity'
  ELSE sizing_mode
END;
UPDATE trading_instances SET fixed_quantity = fixed_position_size
 WHERE fixed_quantity = 0 AND fixed_position_size > 0;
UPDATE trading_instances SET starting_equity = capital_allocation
 WHERE starting_equity IS NULL OR starting_equity <= 0;
UPDATE trading_instances SET current_realized_equity = capital_allocation
 WHERE current_realized_equity IS NULL;
UPDATE trading_instances SET risk_basis = capital_allocation
 WHERE risk_basis IS NULL;
ALTER TABLE trading_instances ALTER COLUMN sizing_mode SET DEFAULT 'fixed_starting_equity_percent';
CREATE TABLE IF NOT EXISTS instance_metrics (
 instance_id TEXT PRIMARY KEY REFERENCES trading_instances(id) ON DELETE CASCADE,
 data_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE instance_metrics ADD COLUMN IF NOT EXISTS data_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE instance_metrics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE TABLE IF NOT EXISTS instance_engine_logs (
 id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES trading_instances(id) ON DELETE CASCADE,
 ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
ALTER TABLE instance_engine_logs ADD COLUMN IF NOT EXISTS instance_id TEXT;
ALTER TABLE instance_engine_logs ADD COLUMN IF NOT EXISTS ts TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE instance_engine_logs ADD COLUMN IF NOT EXISTS level TEXT NOT NULL DEFAULT 'info';
ALTER TABLE instance_engine_logs ADD COLUMN IF NOT EXISTS message TEXT NOT NULL DEFAULT '';
-- Durable forward-paper cursor. Each active Paper Trading instance stores the
-- last closed candle it actually processed, so a restart can request only
-- newer candles and cannot replay historical opportunities as new trades.
CREATE TABLE IF NOT EXISTS instance_market_state (
 instance_id TEXT PRIMARY KEY REFERENCES trading_instances(id) ON DELETE CASCADE,
 last_processed_candle_timestamp TIMESTAMPTZ,
 market_data_mode TEXT NOT NULL DEFAULT 'paper_forward',
 market_data_status TEXT NOT NULL DEFAULT 'stopped',
 last_market_data_timestamp TIMESTAMPTZ,
 data_source TEXT,
 warmup_bars INTEGER NOT NULL DEFAULT 0,
 duplicate_candles INTEGER NOT NULL DEFAULT 0,
 missing_candles INTEGER NOT NULL DEFAULT 0,
 out_of_order_candles INTEGER NOT NULL DEFAULT 0,
 last_blocker TEXT,
 last_blocker_timestamp TIMESTAMPTZ,
 pending_orders_json JSONB NOT NULL DEFAULT '{}'::jsonb,
 worker_heartbeat TIMESTAMPTZ,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS last_processed_candle_timestamp TIMESTAMPTZ;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS market_data_mode TEXT NOT NULL DEFAULT 'paper_forward';
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS market_data_status TEXT NOT NULL DEFAULT 'stopped';
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS last_market_data_timestamp TIMESTAMPTZ;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS data_source TEXT;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS warmup_bars INTEGER NOT NULL DEFAULT 0;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS duplicate_candles INTEGER NOT NULL DEFAULT 0;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS missing_candles INTEGER NOT NULL DEFAULT 0;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS out_of_order_candles INTEGER NOT NULL DEFAULT 0;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS last_blocker TEXT;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS last_blocker_timestamp TIMESTAMPTZ;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS pending_orders_json JSONB NOT NULL DEFAULT '{}'::jsonb;
-- The worker's last proof of life. It lived only in the engine object, so
-- after a crash or a container restart nothing could say whether an instance
-- had been working an hour ago or had been dark since the previous day.
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS worker_heartbeat TIMESTAMPTZ;
ALTER TABLE instance_market_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.instance_market_state'::regclass AND contype = 'p'
  ) THEN
    ALTER TABLE public.instance_market_state
      ADD CONSTRAINT instance_market_state_pkey PRIMARY KEY (instance_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.instance_market_state'::regclass
      AND contype = 'f' AND confrelid = 'public.trading_instances'::regclass
  ) THEN
    ALTER TABLE public.instance_market_state
      ADD CONSTRAINT instance_market_state_instance_id_fkey
      FOREIGN KEY (instance_id) REFERENCES public.trading_instances(id) ON DELETE CASCADE;
  END IF;
END $$;
-- ---------------------------------------------------------------------------
-- Exactly one execution owner per Trading Instance.
--
-- Nothing previously stopped two containers pointed at this database from each
-- running a worker for the same instance, and two workers on one paper account
-- duplicate every order, fill, position and journal entry it produces. The
-- primary key is the instance, so a second claim cannot insert: it either
-- takes over a lease that has genuinely expired, or it is refused and the
-- caller fails closed. A crashed process's lease ages out on its own, which is
-- why the expiry exists rather than a plain "is running" flag.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instance_worker_leases (
 instance_id TEXT PRIMARY KEY REFERENCES trading_instances(id) ON DELETE CASCADE,
 worker_id TEXT NOT NULL,
 process_id INTEGER NOT NULL,
 host TEXT NOT NULL,
 started_at TIMESTAMPTZ NOT NULL,
 heartbeat_at TIMESTAMPTZ NOT NULL,
 lease_expires_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE instance_worker_leases ADD COLUMN IF NOT EXISTS worker_id TEXT NOT NULL DEFAULT '';
ALTER TABLE instance_worker_leases ADD COLUMN IF NOT EXISTS process_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE instance_worker_leases ADD COLUMN IF NOT EXISTS host TEXT NOT NULL DEFAULT '';
ALTER TABLE instance_worker_leases ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE instance_worker_leases ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE instance_worker_leases ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE INDEX IF NOT EXISTS idx_worker_lease_expiry
 ON instance_worker_leases(lease_expires_at);

-- Ownership and configuration revision on the instance itself. owner_id
-- defaults to the single-owner tenant, so every existing row keeps working and
-- nothing is re-homed; it is the column every instance-scoped check keys on
-- once this deployment stops being single-owner. config_revision lets the API
-- say plainly when a running worker has not yet adopted an edit.
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT '__owner__';
ALTER TABLE trading_instances ADD COLUMN IF NOT EXISTS config_revision INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS idx_instance_owner ON trading_instances(owner_id, created_at);

-- Order idempotency as a constraint, not only as application code. An
-- autonomous alert_id is deterministic per instance and candle, so a replayed
-- candle after a reconnect, a retry, or two threads racing DuplicateGuard's
-- read-then-insert all produce the same key. status is part of the key because
-- one order legitimately moves through pending and accepted; what must never
-- happen twice is the same stage for the same key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_alert_instance_unique
 ON webhook_events(alert_id, instance_id, status);
CREATE INDEX IF NOT EXISTS idx_webhook_instance ON webhook_events(instance_id, received_at);
CREATE INDEX IF NOT EXISTS idx_paper_trades_instance
 ON paper_trades(instance_id, simulation_session_id);

CREATE TABLE IF NOT EXISTS trading_instance_platform_settings (
 id TEXT PRIMARY KEY, max_active_slots INTEGER NOT NULL DEFAULT 3,
 max_global_risk_pct DOUBLE PRECISION NOT NULL DEFAULT 0.02,
 max_global_daily_loss_pct DOUBLE PRECISION NOT NULL DEFAULT 0.05,
 updated_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS max_global_daily_loss_pct DOUBLE PRECISION NOT NULL DEFAULT 0.05;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS paper_account_capital DOUBLE PRECISION NOT NULL DEFAULT 10000;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS max_active_slots INTEGER NOT NULL DEFAULT 3;
-- One-time capacity migration, and genuinely once. The shipped default was a
-- single active slot and the manager capped the configured value at three, so
-- a persisted 1 was never an operator risk decision: it was the only value the
-- platform ever wrote, and it meant only one Trading Instance could run.
--
-- It is marked applied rather than left conditional, because one IS a legal
-- deliberate choice now (a single-core host) and an unguarded UPDATE would
-- revert that operator's decision on every migration run.
CREATE TABLE IF NOT EXISTS instance_schema_migrations (
 name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM instance_schema_migrations
                 WHERE name = '2026-09-13-active-slots-default-3') THEN
    UPDATE trading_instance_platform_settings SET max_active_slots = 3
     WHERE max_active_slots <= 1;
    INSERT INTO instance_schema_migrations(name) VALUES ('2026-09-13-active-slots-default-3');
  END IF;
END $$;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS max_global_risk_pct DOUBLE PRECISION NOT NULL DEFAULT 0.02;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS max_instance_risk_per_trade_pct DOUBLE PRECISION NOT NULL DEFAULT 0.05;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_symbol TEXT NOT NULL DEFAULT 'BTCUSDT';
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_timeframe TEXT NOT NULL DEFAULT '5m';
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_strategy TEXT NOT NULL DEFAULT 'brain';
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_capital DOUBLE PRECISION NOT NULL DEFAULT 1000;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_risk_per_trade_pct DOUBLE PRECISION NOT NULL DEFAULT 0.005;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_max_open_positions INTEGER NOT NULL DEFAULT 3;
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_entry_mode TEXT NOT NULL DEFAULT 'limit';
ALTER TABLE trading_instance_platform_settings
 ADD COLUMN IF NOT EXISTS default_fill_model TEXT NOT NULL DEFAULT 'RealisticFill';
CREATE INDEX IF NOT EXISTS idx_instance_pair_strategy ON trading_instances(symbol, strategy_key, strategy_version);
CREATE UNIQUE INDEX IF NOT EXISTS idx_instance_market_state_owner ON instance_market_state(instance_id);
CREATE INDEX IF NOT EXISTS idx_instance_trade ON paper_trades(instance_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_instance_position ON positions(instance_id, status);
CREATE INDEX IF NOT EXISTS idx_instance_logs ON bot_logs(instance_id, ts DESC);

CREATE TABLE IF NOT EXISTS simulation_sessions (
 id TEXT PRIMARY KEY,
 instance_id TEXT NOT NULL,
 session_number INTEGER NOT NULL,
 starting_balance DOUBLE PRECISION NOT NULL,
 ending_balance DOUBLE PRECISION,
 realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
 trades_count INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL,
 started_at TIMESTAMPTZ NOT NULL,
 ended_at TIMESTAMPTZ,
 end_reason TEXT,
 UNIQUE(instance_id, session_number)
);
CREATE TABLE IF NOT EXISTS simulation_account_audit (
 id TEXT PRIMARY KEY,
 action TEXT NOT NULL,
 instance_id TEXT NOT NULL,
 previous_session_id TEXT,
 new_session_id TEXT NOT NULL,
 previous_balance DOUBLE PRECISION NOT NULL,
 new_balance DOUBLE PRECISION NOT NULL,
 open_positions_cleared INTEGER NOT NULL,
 pending_orders_cleared INTEGER NOT NULL,
 timestamp TIMESTAMPTZ NOT NULL,
 initiated_by TEXT NOT NULL,
 result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simulation_sessions_instance
 ON simulation_sessions(instance_id, session_number DESC);
CREATE INDEX IF NOT EXISTS idx_simulation_restart_audit
 ON simulation_account_audit(instance_id, timestamp DESC);

-- Existing Trading Instances keep their full financial history in Session #1.
INSERT INTO simulation_sessions
 (id,instance_id,session_number,starting_balance,ending_balance,realized_pnl,
  trades_count,status,started_at)
SELECT 'session_' || md5(t.id || ':1'), t.id, 1,
       COALESCE(t.starting_equity,t.capital_allocation), NULL,
       COALESCE(t.current_realized_equity,t.capital_allocation) - COALESCE(t.starting_equity,t.capital_allocation),
       (SELECT COUNT(*) FROM paper_trades p WHERE p.instance_id=t.id AND p.status='closed'),
       'active', t.created_at
FROM trading_instances t
WHERE COALESCE(t.simulation_session_id,'')=''
ON CONFLICT (instance_id,session_number) DO NOTHING;
UPDATE trading_instances t
SET simulation_session_id=s.id, simulation_session_number=s.session_number
FROM simulation_sessions s
WHERE s.instance_id=t.id AND s.status='active' AND COALESCE(t.simulation_session_id,'')='';
UPDATE paper_trades p SET simulation_session_id=t.simulation_session_id
FROM trading_instances t
WHERE p.instance_id=t.id AND COALESCE(p.simulation_session_id,'')='';
UPDATE positions p SET simulation_session_id=t.simulation_session_id
FROM trading_instances t
WHERE p.instance_id=t.id AND COALESCE(p.simulation_session_id,'')='';

CREATE OR REPLACE FUNCTION public.restart_simulation_account(
 p_instance_id TEXT,
 p_new_session_id TEXT,
 p_previous_balance DOUBLE PRECISION,
 p_initiated_by TEXT,
 p_timestamp TIMESTAMPTZ
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
 v_instance trading_instances%ROWTYPE;
 v_previous_session_id TEXT;
 v_next_session_number INTEGER;
 v_open_positions INTEGER;
 v_pending_orders INTEGER;
 v_closed_trades INTEGER;
 v_audit_id TEXT;
BEGIN
 SELECT * INTO v_instance FROM trading_instances WHERE id=p_instance_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Trading instance not found'; END IF;
 IF LOWER(v_instance.execution_mode) <> 'paper' OR v_instance.mode <> 'trading' THEN
   RAISE EXCEPTION 'Simulation account restart is allowed only for Paper Trading instances';
 END IF;
 v_previous_session_id := v_instance.simulation_session_id;
 v_next_session_number := GREATEST(1, v_instance.simulation_session_number + 1);
 SELECT COUNT(*) INTO v_open_positions FROM positions
  WHERE instance_id=p_instance_id AND status='open';
 SELECT COUNT(*) INTO v_pending_orders
  FROM instance_market_state AS market_state
  CROSS JOIN LATERAL jsonb_object_keys(
    COALESCE(market_state.pending_orders_json,'{}'::jsonb)
  ) AS pending_order(key)
  WHERE market_state.instance_id=p_instance_id;
 v_pending_orders := COALESCE(v_pending_orders,0);
 SELECT COUNT(*) INTO v_closed_trades FROM paper_trades
  WHERE instance_id=p_instance_id AND simulation_session_id=v_previous_session_id AND status='closed';

 UPDATE positions SET status='reset', pnl=0, closed_at=p_timestamp
  WHERE instance_id=p_instance_id AND status='open';
 UPDATE paper_trades SET status='cancelled', pnl=0, realized_pnl=0, closed_at=p_timestamp
  WHERE instance_id=p_instance_id AND status='open';
 UPDATE simulation_sessions
  SET status='ended', ending_balance=p_previous_balance,
      realized_pnl=p_previous_balance-v_instance.starting_equity,
      trades_count=v_closed_trades, ended_at=p_timestamp, end_reason='account restart'
  WHERE id=v_previous_session_id AND instance_id=p_instance_id;
 INSERT INTO simulation_sessions
  (id,instance_id,session_number,starting_balance,realized_pnl,trades_count,status,started_at)
 VALUES
  (p_new_session_id,p_instance_id,v_next_session_number,v_instance.starting_equity,0,0,'active',p_timestamp);
 UPDATE trading_instances
  SET current_realized_equity=starting_equity, risk_basis=starting_equity,
      simulation_session_id=p_new_session_id,
      simulation_session_number=v_next_session_number, updated_at=p_timestamp
  WHERE id=p_instance_id;
 UPDATE instance_market_state SET pending_orders_json='{}'::jsonb, updated_at=p_timestamp
  WHERE instance_id=p_instance_id;
 DELETE FROM instance_metrics WHERE instance_id=p_instance_id;
 v_audit_id := md5(p_instance_id || p_new_session_id || p_timestamp::text);
 INSERT INTO simulation_account_audit
  (id,action,instance_id,previous_session_id,new_session_id,previous_balance,new_balance,
   open_positions_cleared,pending_orders_cleared,timestamp,initiated_by,result)
 VALUES
  (v_audit_id,'simulation_account_restart',p_instance_id,v_previous_session_id,p_new_session_id,
   p_previous_balance,v_instance.starting_equity,v_open_positions,v_pending_orders,p_timestamp,
   p_initiated_by,'success');
 RETURN jsonb_build_object(
  'action','simulation_account_restart','instance_id',p_instance_id,
  'previous_session_id',v_previous_session_id,'new_session_id',p_new_session_id,
  'previous_balance',p_previous_balance,'new_balance',v_instance.starting_equity,
  'open_positions_cleared',v_open_positions,'pending_orders_cleared',v_pending_orders,
  'timestamp',p_timestamp,'initiated_by',p_initiated_by,'result','success',
  'session_number',v_next_session_number);
END;
$$;
REVOKE ALL ON FUNCTION public.restart_simulation_account(TEXT,TEXT,DOUBLE PRECISION,TEXT,TIMESTAMPTZ)
 FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.restart_simulation_account(TEXT,TEXT,DOUBLE PRECISION,TEXT,TIMESTAMPTZ)
 TO service_role;

-- Factory Reset audit is deliberately outside the operational deletion set.
CREATE TABLE IF NOT EXISTS public.factory_reset_audit (
 id TEXT PRIMARY KEY, requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 completed_at TIMESTAMPTZ, initiated_by TEXT NOT NULL, reset_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK (status IN ('requested','succeeded','failed')),
 duration_ms BIGINT, preserved_scope JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
);
ALTER TABLE public.factory_reset_audit ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.factory_reset_audit FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON TABLE public.factory_reset_audit TO service_role;

CREATE OR REPLACE FUNCTION public.factory_reset_application_data(
 p_reset_id TEXT, p_confirmation TEXT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
 IF p_confirmation IS DISTINCT FROM 'FACTORY RESET' THEN
   RAISE EXCEPTION 'exact factory-reset confirmation is required';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM public.factory_reset_audit
                WHERE id=p_reset_id AND status='requested') THEN
   RAISE EXCEPTION 'factory-reset audit request does not exist';
 END IF;
 -- Supabase safe-update requires a WHERE clause for intentional all-row deletes.
 DELETE FROM public.instance_engine_logs WHERE TRUE;
 DELETE FROM public.instance_metrics WHERE TRUE;
 DELETE FROM public.instance_market_state WHERE TRUE;
 DELETE FROM public.simulation_account_audit WHERE TRUE;
 DELETE FROM public.positions WHERE TRUE;
 DELETE FROM public.paper_trades WHERE TRUE;
 DELETE FROM public.simulation_sessions WHERE TRUE;
 DELETE FROM public.webhook_events WHERE TRUE;
 DELETE FROM public.bot_logs WHERE TRUE;
 DELETE FROM public.alerts WHERE TRUE;
 DELETE FROM public.trade_memories WHERE TRUE;
 DELETE FROM public.memory_reviews WHERE TRUE;
 DELETE FROM public.trading_instances WHERE TRUE;
 DELETE FROM public.trading_instance_platform_settings WHERE TRUE;
 DELETE FROM public.user_settings WHERE TRUE;
 RETURN jsonb_build_object('ok',TRUE,'reset_id',p_reset_id);
END;
$$;
REVOKE ALL ON FUNCTION public.factory_reset_application_data(TEXT,TEXT)
 FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.factory_reset_application_data(TEXT,TEXT)
 TO service_role;

-- Make newly created/altered relations visible to the REST API immediately.
NOTIFY pgrst, 'reload schema';
