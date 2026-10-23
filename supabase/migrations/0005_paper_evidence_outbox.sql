-- Sprint 1.5: metadata in the existing authoritative accounting transaction.
-- Apply after automation-hub/data/trading_instances_schema.sql. The original
-- RPC bodies are retained verbatim under private names; wrappers add no
-- financial writes. Do not rerun the base RPC schema after this migration.
-- Forward recovery: rerun this migration in one transaction. Application
-- downgrade may retain these wrappers/table. Do not remove the outbox while
-- replay is pending. A ledger backup must include this immutable metadata.
BEGIN;

CREATE TABLE IF NOT EXISTS public.paper_evidence_outbox (
 sequence_id BIGSERIAL UNIQUE NOT NULL,
 execution_id TEXT PRIMARY KEY,
 action TEXT NOT NULL CHECK(action IN ('OPEN','REDUCE','CLOSE')),
 instance_id TEXT NOT NULL DEFAULT '', simulation_session_id TEXT NOT NULL DEFAULT '',
 trade_id TEXT NOT NULL, position_id TEXT NOT NULL,
 parent_trade_id TEXT, parent_position_id TEXT,
 remainder_trade_id TEXT, remainder_position_id TEXT,
 observed_at TEXT, context_json JSONB, receipt_json JSONB NOT NULL,
 created_at TIMESTAMPTZ NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_paper_evidence_outbox_scope
 ON public.paper_evidence_outbox(instance_id,simulation_session_id,sequence_id);
ALTER TABLE public.paper_evidence_outbox ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.paper_evidence_outbox FROM PUBLIC,anon,authenticated;
GRANT SELECT ON public.paper_evidence_outbox TO service_role;

CREATE OR REPLACE FUNCTION public.reject_paper_evidence_mutation()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path=public AS $$
BEGIN RAISE EXCEPTION 'immutable paper evidence outbox'; END $$;
DROP TRIGGER IF EXISTS immutable_paper_evidence_outbox ON public.paper_evidence_outbox;
CREATE TRIGGER immutable_paper_evidence_outbox BEFORE UPDATE OR DELETE
 ON public.paper_evidence_outbox FOR EACH ROW EXECUTE FUNCTION public.reject_paper_evidence_mutation();

DO $$
BEGIN
 IF to_regprocedure('public.paper_open_accounting_v1(jsonb)') IS NULL THEN
  ALTER FUNCTION public.paper_open_atomic(JSONB) RENAME TO paper_open_accounting_v1;
 END IF;
 IF to_regprocedure('public.paper_close_accounting_v1(jsonb)') IS NULL THEN
  ALTER FUNCTION public.paper_close_atomic(JSONB) RENAME TO paper_close_accounting_v1;
 END IF;
 IF to_regprocedure('public.paper_reduce_accounting_v1(jsonb)') IS NULL THEN
  ALTER FUNCTION public.paper_reduce_atomic(JSONB) RENAME TO paper_reduce_accounting_v1;
 END IF;
END $$;
REVOKE ALL ON FUNCTION public.paper_open_accounting_v1(JSONB) FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.paper_close_accounting_v1(JSONB) FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.paper_reduce_accounting_v1(JSONB) FROM PUBLIC,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION public.paper_open_atomic(p_payload JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE
 v_result JSONB; v_trade public.paper_trades%ROWTYPE;
 v_execution public.paper_executions%ROWTYPE;
 v_evidence JSONB := p_payload->'trade'->'_evidence';
BEGIN
 v_result := public.paper_open_accounting_v1(p_payload);
 SELECT * INTO STRICT v_trade FROM public.paper_trades WHERE id=v_result->>'trade_id';
 SELECT * INTO STRICT v_execution FROM public.paper_executions
  WHERE execution_id=p_payload->>'execution_id';
 INSERT INTO public.paper_evidence_outbox
  (execution_id,action,instance_id,simulation_session_id,trade_id,position_id,
   observed_at,context_json,receipt_json,created_at)
 VALUES (v_execution.execution_id,'OPEN',v_trade.instance_id,v_trade.simulation_session_id,
  v_trade.id,v_result->>'position_id',v_evidence->>'observed_at',v_evidence->'context',
  COALESCE(v_evidence->'receipt','{}'::JSONB) || jsonb_build_object(
   'symbol',v_trade.symbol,'side',v_trade.side,'entry',v_trade.entry,'price',v_trade.entry,
   'size',v_trade.size,'stop',v_trade.stop,'target',v_trade.target,
   'initial_risk_amount',v_trade.risk_amount_at_entry,
   'risk_amount_at_entry',v_trade.risk_amount_at_entry,'booked_fees',0),v_execution.created_at::TIMESTAMPTZ);
 RETURN v_result;
END $$;

CREATE OR REPLACE FUNCTION public.paper_close_atomic(p_payload JSONB)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE
 v_trade public.paper_trades%ROWTYPE; v_execution public.paper_executions%ROWTYPE;
 v_evidence JSONB := p_payload->'evidence'; v_receipt JSONB;
BEGIN
 PERFORM public.paper_close_accounting_v1(p_payload);
 SELECT * INTO STRICT v_trade FROM public.paper_trades WHERE id=p_payload->>'trade_id';
 SELECT * INTO STRICT v_execution FROM public.paper_executions
  WHERE execution_id=p_payload->>'execution_id';
 v_receipt := COALESCE(v_evidence->'receipt','{}'::JSONB) || jsonb_build_object(
  'symbol',v_trade.symbol,'side',v_trade.side,'entry',v_trade.entry,'price',v_trade.exit,
  'size',v_trade.size,'stop',v_trade.stop,'target',v_trade.target,
  'initial_risk_amount',v_trade.risk_amount_at_entry,
  'risk_amount_at_entry',v_trade.risk_amount_at_entry,
  'net_pnl',v_trade.pnl,'booked_fees',v_trade.fees,
  'gross_pnl',COALESCE(NULLIF(v_evidence->'receipt'->'gross_pnl','null'::JSONB),
                      to_jsonb(v_trade.pnl + v_trade.fees)));
 INSERT INTO public.paper_evidence_outbox
  (execution_id,action,instance_id,simulation_session_id,trade_id,position_id,
   observed_at,context_json,receipt_json,created_at)
 VALUES (v_execution.execution_id,'CLOSE',v_trade.instance_id,v_trade.simulation_session_id,
  v_trade.id,p_payload->>'position_id',v_evidence->>'observed_at',v_evidence->'context',
  v_receipt,v_execution.created_at::TIMESTAMPTZ);
END $$;

CREATE OR REPLACE FUNCTION public.paper_reduce_atomic(p_payload JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE
 v_result JSONB; v_trade public.paper_trades%ROWTYPE;
 v_remainder public.paper_trades%ROWTYPE; v_execution public.paper_executions%ROWTYPE;
 v_evidence JSONB := p_payload->'evidence'; v_receipt JSONB;
BEGIN
 v_result := public.paper_reduce_accounting_v1(p_payload);
 SELECT * INTO STRICT v_trade FROM public.paper_trades WHERE id=p_payload->>'trade_id';
 SELECT * INTO STRICT v_remainder FROM public.paper_trades WHERE id=v_result->>'trade_id';
 SELECT * INTO STRICT v_execution FROM public.paper_executions
  WHERE execution_id=p_payload->>'execution_id';
 v_receipt := COALESCE(v_evidence->'receipt','{}'::JSONB) || jsonb_build_object(
  'symbol',v_trade.symbol,'side',v_trade.side,'entry',v_trade.entry,'price',v_trade.exit,
  'size',v_trade.size,'stop',v_trade.stop,'target',v_trade.target,
  'initial_risk_amount',v_trade.risk_amount_at_entry,
  'risk_amount_at_entry',v_trade.risk_amount_at_entry,'remainder_size',v_remainder.size,
  'net_pnl',v_trade.pnl,'booked_fees',v_trade.fees,
  'gross_pnl',COALESCE(NULLIF(v_evidence->'receipt'->'gross_pnl','null'::JSONB),
                      to_jsonb(v_trade.pnl + v_trade.fees)));
 INSERT INTO public.paper_evidence_outbox
  (execution_id,action,instance_id,simulation_session_id,trade_id,position_id,
   parent_trade_id,parent_position_id,remainder_trade_id,remainder_position_id,
   observed_at,context_json,receipt_json,created_at)
 VALUES (v_execution.execution_id,'REDUCE',v_remainder.instance_id,v_remainder.simulation_session_id,
  v_trade.id,p_payload->'position'->>'id',v_trade.id,p_payload->'position'->>'id',
  v_result->>'trade_id',v_result->>'position_id',v_evidence->>'observed_at',v_evidence->'context',
  v_receipt,v_execution.created_at::TIMESTAMPTZ);
 RETURN v_result;
END $$;

CREATE OR REPLACE FUNCTION public.paper_evidence_capabilities()
RETURNS JSONB LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=public AS $$
 SELECT jsonb_build_object('schema_version',1,'consistent_snapshot',true,
  'atomic_outbox',
  position('paper_open_accounting_v1' in pg_get_functiondef('public.paper_open_atomic(jsonb)'::regprocedure))>0
  AND position('paper_close_accounting_v1' in pg_get_functiondef('public.paper_close_atomic(jsonb)'::regprocedure))>0
  AND position('paper_reduce_accounting_v1' in pg_get_functiondef('public.paper_reduce_atomic(jsonb)'::regprocedure))>0);
$$;

-- A single SQL statement has one MVCC snapshot. A scalar JSON RPC response is
-- not a table result that PostgREST's ordinary row-limit can silently truncate.
CREATE OR REPLACE FUNCTION public.paper_evidence_snapshot(
 p_instance_id TEXT DEFAULT '',p_simulation_session_id TEXT DEFAULT '')
RETURNS JSONB LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=public AS $$
 WITH scoped_trades AS (
  SELECT * FROM public.paper_trades
   WHERE (p_instance_id='' OR instance_id=p_instance_id)
     AND (p_simulation_session_id='' OR simulation_session_id=p_simulation_session_id)
 ), scoped_positions AS (
  SELECT * FROM public.positions
   WHERE (p_instance_id='' OR instance_id=p_instance_id)
     AND (p_simulation_session_id='' OR simulation_session_id=p_simulation_session_id)
 ), scoped_outbox AS (
  SELECT * FROM public.paper_evidence_outbox
   WHERE (p_instance_id='' OR instance_id=p_instance_id)
     AND (p_simulation_session_id='' OR simulation_session_id=p_simulation_session_id)
 ), instance_executions AS (
  SELECT * FROM public.paper_executions e
   WHERE (p_instance_id='' OR instance_id=p_instance_id)
    OR EXISTS(SELECT 1 FROM scoped_trades t WHERE t.id=e.trade_id)
    OR EXISTS(SELECT 1 FROM scoped_outbox o WHERE o.execution_id=e.execution_id)
 ), scoped_executions AS (
  SELECT * FROM instance_executions e WHERE p_simulation_session_id=''
   OR EXISTS(SELECT 1 FROM scoped_trades t WHERE t.id=e.trade_id)
   OR EXISTS(SELECT 1 FROM scoped_outbox o WHERE o.execution_id=e.execution_id)
 ), unscoped_executions AS (
  SELECT * FROM instance_executions e WHERE p_simulation_session_id<>''
   AND NOT EXISTS(SELECT 1 FROM public.paper_trades t WHERE t.id=e.trade_id)
   AND NOT EXISTS(SELECT 1 FROM public.paper_evidence_outbox o WHERE o.execution_id=e.execution_id)
 )
 SELECT jsonb_build_object(
  'trades',COALESCE((SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM scoped_trades t),'[]'::JSONB),
  'positions',COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY id) FROM scoped_positions p),'[]'::JSONB),
  'executions',COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY created_at,execution_id) FROM scoped_executions e),'[]'::JSONB),
  'outbox',COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY sequence_id) FROM scoped_outbox o),'[]'::JSONB),
  'unscoped_executions',COALESCE((SELECT jsonb_agg(to_jsonb(u) ORDER BY created_at,execution_id) FROM unscoped_executions u),'[]'::JSONB),
  'source_complete',NOT EXISTS(SELECT 1 FROM unscoped_executions),
  'consistent_snapshot',true,'outbox_supported',true,'source_kind','postgres');
$$;

REVOKE ALL ON FUNCTION public.paper_open_atomic(JSONB) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.paper_close_atomic(JSONB) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.paper_reduce_atomic(JSONB) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.paper_evidence_capabilities() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.paper_evidence_snapshot(TEXT,TEXT) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.paper_open_atomic(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.paper_close_atomic(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.paper_reduce_atomic(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.paper_evidence_capabilities() TO service_role;
GRANT EXECUTE ON FUNCTION public.paper_evidence_snapshot(TEXT,TEXT) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
