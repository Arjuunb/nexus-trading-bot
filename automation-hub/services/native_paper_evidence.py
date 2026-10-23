"""Original source facts captured inside existing V2 accounting transactions.

There is no balance calculation authority here. Cash deltas and allocations
describe the mutation the broker actually performed. Replay reads immutable
receipts; it never creates orders or repairs financial rows.
"""
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import json
import math
from pathlib import Path
import sqlite3
import uuid

from services.strategy_identity import canonical_configuration_json as encoded, configuration_fingerprint as digest

SCHEMA = 'paper_execution_evidence.v1'


def financial_transaction(method):
    """Keep original commit boundaries; roll back on any pre-commit failure.

    Existing public broker methods already commit after accounting. Evidence is
    appended before that commit, never over a network. The outer lock prevents
    a failed transaction leaking into another caller's subsequent commit.
    """
    @wraps(method)
    def run(broker, *args, **kwargs):
        with broker._lock:
            prior = getattr(broker, '_source_transaction_id', None)
            broker._source_transaction_id = prior or 'paper-tx:' + uuid.uuid4().hex
            try:
                return method(broker, *args, **kwargs)
            except BaseException:
                broker._c.rollback()
                raise
            finally:
                broker._source_transaction_id = prior
    return run


def verify_owner(db, owner):
    if not owner:
        return
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_source_evidence'").fetchone():
        return
    prior = db.execute('SELECT DISTINCT tenant_id FROM v2_source_evidence WHERE tenant_id IS NOT NULL').fetchall()
    if any(row[0] != owner for row in prior):
        raise ValueError('PAPER_EVIDENCE_OWNER_CONFLICT')


def install(broker, *, new_account):
    broker._c.executescript((Path(__file__).parents[1] / 'migrations/financial/0001_native_paper_evidence.sql').read_text())
    if not broker._c.execute('SELECT 1 FROM v2_source_evidence LIMIT 1').fetchone():
        account = dict(broker._account_row())
        emit(broker, 'ACCOUNT_OPENED' if new_account else 'CAPTURE_STARTED',
            {'account': account, 'historical_completeness': 'COMPLETE' if new_account else 'UNKNOWN',
             'balance_basis': 'ORIGINAL_ACCOUNT_CREATION' if new_account else 'CONTEMPORANEOUS_CAPTURE_CHECKPOINT'},
            event_id='paper-capture:' + account['account_id'])
        broker._c.commit()


def validate_context(context, *, owner, account_id, strategy, version):
    result = deepcopy(context or {})
    if result.get('tenant_id') and owner and result['tenant_id'] != owner:
        raise ValueError('PAPER_EVIDENCE_OWNER_CONFLICT')
    if result.get('paper_account_id') and result['paper_account_id'] != account_id:
        raise ValueError('PAPER_EVIDENCE_ACCOUNT_CONFLICT')
    snapshot = result.get('configuration_snapshot')
    verified = bool(snapshot and result.get('configuration_status') == 'VERIFIED'
        and result.get('configuration_reference') and digest(snapshot) == result.get('effective_config_fingerprint')
        and snapshot.get('strategy_version') == version
        and snapshot.get('strategy_id') == result.get('strategy_id') and result.get('strategy_version') == version
        and (result.get('strategy_id') == strategy or snapshot.get('execution_config', {}).get('model_id') == strategy))
    if not verified:
        result.update(configuration_status='UNKNOWN', effective_config_fingerprint=None)
    result.update(tenant_id=owner, paper_account_id=account_id)
    if len(encoded(result).encode()) > 512 * 1024:
        raise ValueError('PAPER_EVIDENCE_CONTEXT_LIMIT')
    return result


def emit(broker, kind, payload, *, event_id, context=None, timestamp=None):
    account = broker._account_row()
    context = context or {}
    event = {'schema_version': SCHEMA, 'event_id': event_id, 'event_type': kind,
        'tenant_id': context.get('tenant_id') or broker.evidence_owner,
        'paper_account_id': account['account_id'], 'strategy_id': context.get('strategy_id'),
        'strategy_version': context.get('strategy_version'),
        'effective_config_fingerprint': context.get('effective_config_fingerprint'),
        'execution_session_id': context.get('execution_session_id'), 'execution_mode': context.get('execution_mode'),
        'venue': context.get('venue'), 'market_type': context.get('market_type'),
        'symbol': payload.get('symbol') or (payload.get('order') or {}).get('symbol'),
        'timeframe': context.get('timeframe'),
        'event_timestamp_utc': timestamp or datetime.now(timezone.utc).isoformat(),
        'source_transaction_id': getattr(broker, '_source_transaction_id', None) or 'paper-tx:' + uuid.uuid4().hex,
        'source_authority': 'ORIGINAL_NATIVE_PAPER_TRANSACTION', 'payload': deepcopy(payload)}
    old = broker._c.execute('SELECT payload_json FROM v2_source_evidence WHERE event_id=?', (event_id,)).fetchone()
    if old:
        original = json.loads(old[0])
        if original['event_type'] != kind or encoded(original['payload']) != encoded(payload):
            raise ValueError('PAPER_EVIDENCE_IDENTITY_CONFLICT')
        return original
    text = encoded(event)
    broker._c.execute('INSERT INTO v2_source_evidence(event_id,account_id,tenant_id,event_type,event_timestamp,source_transaction_id,evidence_hash,payload_json) VALUES (?,?,?,?,?,?,?,?)',
        (event_id, account['account_id'], event['tenant_id'], kind, event['event_timestamp_utc'], event['source_transaction_id'], digest(event), text))
    return event


def order_context(broker, order_id):
    row = broker._c.execute("SELECT payload_json FROM v2_source_evidence WHERE event_id=?", ('paper-order:' + order_id,)).fetchone()
    return json.loads(row[0])['payload']['context'] if row else {}


def position_context(broker, position_id):
    # Original position ID allocated by the broker, never symbol/direction inference.
    row=broker._c.execute('SELECT context_json FROM v2_episode_origins WHERE episode_id=?',(position_id,)).fetchone()
    return json.loads(row[0]) if row else {}


def capture_order(broker, order, context=None):
    context = validate_context(context, owner=broker.evidence_owner, account_id=broker._account_row()['account_id'],
        strategy=order.get('strategy') or (context or {}).get('strategy_id'),
        version=order.get('strategy_version') or (context or {}).get('strategy_version'))
    emit(broker, 'ORDER_ACCEPTED', {'order': order, 'context': context,
         'intent_reference': context.get('order_intent_reference'), 'decision_reference': context.get('decision_reference')},
         event_id='paper-order:' + order['id'], context=context, timestamp=order['created_at'])


def capture_fill(broker, *, fill, order, before, after, raw_price, persisted):
    context = order_context(broker, order['id'])
    if not context and before:
        context = position_context(broker, before['position_id'])
    qty = fill['quantity']; close_qty = 0
    if before and (before['side'] == 'long') != (fill['side'] == 'buy'):
        close_qty = min(qty, before['size'])
    legs = []
    if close_qty:
        legs.append({'episode_id': before['position_id'], 'effect': 'EXIT', 'quantity': close_qty,
            'episode_closed': not after or after['position_id'] != before['position_id'],
            'direction': before['side'].upper(), 'gross_pnl': fill['realized_pnl'],
            'fee': fill['fee'] * close_qty / qty})
    if qty > close_qty and after:
        legs.append({'episode_id': after['position_id'], 'effect': 'ENTRY', 'quantity': qty - close_qty,
            'episode_closed': False, 'direction': after['side'].upper(), 'gross_pnl': 0,
            'fee': fill['fee'] - sum(leg['fee'] for leg in legs)})
    for leg in legs:
        leg['initial_risk'] = (abs(fill['price']-fill['stop_loss'])*leg['quantity']
            if leg['effect']=='ENTRY' and fill.get('stop_loss') is not None else None)
    cost = {'version': 'native-paper-price.v1', 'basis': 'EMBEDDED_IN_PRICE',
        'fee_rate': broker.fee_rate, 'spread_bps': broker.spread_bps,
        'slippage_bps': broker.slippage_bps, 'reference_price': raw_price,
        'spread_price_component': raw_price * broker.spread_bps / 20000,
        'slippage_price_component': raw_price * broker.slippage_bps / 10000,
        'commission_basis': 'SEPARATELY_DEDUCTED', 'funding_basis': 'UNKNOWN_UNTIL_ORIGINAL_COVERAGE_PROOF'}
    emit(broker, 'FILL_COMMITTED', {'symbol': fill['symbol'], 'fill': fill, 'order': order,
        'context': context, 'allocations': legs, 'position_before': before, 'position_after': after,
        'cash_delta': fill['realized_pnl'] - fill['fee'], 'cash_currency': 'USDT',
        'balance_after': broker._account_row()['balance'], 'cost_model': cost,
        'order_origin': 'PERSISTED_BROKER_ORDER' if persisted else 'EXISTING_INTERNAL_PROTECTION',
        'historical_episode_completeness': 'UNKNOWN' if before and not position_context(broker, before['position_id']) else 'CAPTURED'},
        event_id='paper-fill:' + fill['id'], context=context, timestamp=fill['timestamp'])
    for leg in legs:
        if leg['effect']=='ENTRY' and not broker._c.execute('SELECT 1 FROM v2_episode_origins WHERE episode_id=?',(leg['episode_id'],)).fetchone():
            broker._c.execute('INSERT INTO v2_episode_origins VALUES (?,?,?,?)',
                (leg['episode_id'],'paper-fill:'+fill['id'],encoded(context),fill['timestamp']))


def original_lab_context(db, prefix, *, correlation_id, owner, session, execution_config, strategy, version):
    """Use contemporaneous source capture only; never hash current settings."""
    result = {'tenant_id': owner, 'execution_session_id': session['id'],
        'execution_mode': 'paper' if session.get('mode') == 'LIVE_PAPER' else 'historical',
        'strategy_id': strategy, 'strategy_version': version,
        'venue': 'binance_usdm', 'market_type': 'perpetual', 'timeframe': session.get('timeframe'),
        'decision_reference': prefix + ':decision:' + str(correlation_id) if correlation_id else None,
        'order_intent_reference': prefix + ':intent:' + str(correlation_id) if correlation_id else None}
    try:
        row = db.execute(f'SELECT payload_json FROM {prefix}_evaluations WHERE correlation_id=? AND session_id=?', (correlation_id, session['id'])).fetchone()
        capture = json.loads(row[0])['active_evidence'] if row else {}
        frozen = capture.get('configuration_snapshot') or {}
        if capture.get('tenant_id') == owner and capture.get('configuration_status') == 'VERIFIED' and frozen.get('execution_config') == execution_config and capture.get('strategy_id') == strategy and capture.get('strategy_version') == version:
            result.update(configuration_snapshot=frozen, effective_config_fingerprint=capture['config_fingerprint'],
                configuration_reference=prefix + ':original-decision:' + correlation_id, configuration_status='VERIFIED')
    except (sqlite3.Error, KeyError, ValueError, TypeError):
        pass  # Original unavailable stays UNKNOWN; execution behaviour is unchanged.
    return result


def record_funding_schedule(broker, *, symbol, start, end, settlements, source_reference):
    """Retain an ORIGINAL provider response; no funding charge is calculated.

    This trusted producer hook is not exposed via HTTP. An empty schedule only
    proves zero applicability within its explicitly complete original range.
    Existing lab polling cannot yet supply this complete history.
    """
    from services.shadow_router import utc
    lo, hi = utc(start), utc(end)
    if not source_reference or hi <= lo or (hi-lo).total_seconds() > 31*86400 or len(settlements) > 1000:
        raise ValueError('INVALID_ORIGINAL_FUNDING_SCHEDULE')
    stamps = [utc(row['timestamp']) for row in settlements]
    if stamps != sorted(set(stamps)) or any(not lo <= stamp < hi for stamp in stamps):
        raise ValueError('INVALID_ORIGINAL_FUNDING_SCHEDULE')
    with broker._lock:
        result = emit(broker, 'FUNDING_SCHEDULE_RECORDED', {'symbol': symbol,
            'start':lo.isoformat(),'end':hi.isoformat(),'settlements':settlements,
            'source_reference':source_reference, 'basis':'ORIGINAL_PROVIDER_COMPLETE_RANGE'},
            event_id='paper-funding-schedule:' + digest([symbol, start, end, settlements, source_reference]))
        broker._c.commit()
        return result


#: Position fields whose change makes a new valuation worth recording at once.
#: The mark-driven ones (unrealized P&L, a trailing stop's peak) are not listed.
_MATERIAL_POSITION_FIELDS = ('symbol', 'side', 'size', 'entry_price', 'stop_loss', 'take_profit')


def observe_equity(broker, marks, *, observed_at, maximum_age_seconds=30, commit=True,
                   minimum_interval_seconds=0):
    """Explicit source-timed valuation observation, never persist broker metrics.

    ``minimum_interval_seconds`` is for the per-quote caller. An observation
    whose cash, positions and completeness match the last one this broker
    recorded less than that many seconds earlier is skipped and None is
    returned. So a valuation is still recorded at once whenever anything
    material changes, and otherwise at most once per interval instead of on
    every quote (one row per quote grew a lab database by about 4.7 GB/day).
    The default 0 records every call, as before."""
    at = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
    if at.tzinfo is None or not 1 <= maximum_age_seconds <= 300 or minimum_interval_seconds < 0:
        raise ValueError('INVALID_EQUITY_OBSERVATION')
    with broker._lock:
        positions = [dict(row) for row in broker._c.execute('SELECT * FROM v2_positions')]
        cash = broker._account_row()['balance']; unrealized = 0.; complete = True
        for position in positions:
            mark = marks.get(position['symbol']) or {}
            try:
                stamp = datetime.fromisoformat(str(mark['timestamp']).replace('Z', '+00:00'))
                price = float(mark['price'])
                if not stamp.tzinfo or not mark.get('source') or not math.isfinite(price) or price <= 0 or not 0 <= (at-stamp).total_seconds() <= maximum_age_seconds:
                    raise ValueError('UNVERIFIED_MARK')
                unrealized += (price-position['entry_price'])*position['size']*(1 if position['side']=='long' else -1)
            except (KeyError,ValueError,TypeError):
                complete = False
        state = digest([cash, complete, [{key: position.get(key) for key in _MATERIAL_POSITION_FIELDS}
                                         for position in positions]])
        last = getattr(broker, '_last_equity_observation', None)
        if (minimum_interval_seconds and last and last[1] == state
                and 0 <= (at - last[0]).total_seconds() < minimum_interval_seconds):
            return None
        result = emit(broker, 'EQUITY_OBSERVED', {'cash_balance': cash, 'positions': positions, 'marks': marks,
            'unrealized_pnl': unrealized if complete else None, 'equity': cash+unrealized if complete else None,
            'completeness': 'COMPLETE' if complete else 'UNKNOWN', 'valuation_model': 'native-paper-mark.v1'},
            event_id='paper-equity:' + digest([broker._account_row()['account_id'], observed_at, marks, positions, cash]), timestamp=observed_at)
        broker._last_equity_observation = (at, state)
        if commit:broker._c.commit()
        return result
