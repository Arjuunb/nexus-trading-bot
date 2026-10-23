"""Bounded, explicitly started read-only observation of the EXISTING feed hub.

Callers supply an existing hub. This opens no new provider or trading client and
does not evaluate strategies or create fills. A transport fixture is always
labelled FIXTURE; live certification requires separately authorized duration.
"""
from datetime import datetime,timezone
import queue
import threading
import time
import uuid

from services.strategy_identity import configuration_fingerprint as digest


def observe_existing_feed(hub,*,symbol,timeframe,duration_seconds,origin,stop=None):
    if not 1<=duration_seconds<=300 or origin not in {'AUTHORIZED_PUBLIC_LIVE','DETERMINISTIC_FIXTURE'}:raise ValueError('INVALID_SOAK_SCOPE')
    stop=stop or threading.Event();notices=queue.Queue(maxsize=256);overflow=[0]
    def notice(record):
        try:notices.put_nowait(dict(record))
        except queue.Full:overflow[0]+=1
    subscription=hub.subscription('read-only-soak:'+uuid.uuid4().hex)
    subscription.consumer.observation_notice=notice
    start=datetime.now(timezone.utc).isoformat();deadline=time.monotonic()+duration_seconds
    records=[];probes=[]
    try:
        if not subscription.start(symbol,timeframe,quotes=False):raise ValueError('SOAK_FEED_START_FAILED')
        while not stop.is_set() and time.monotonic()<deadline:
            try:
                rows=hub.channel_report()
                probes.append({'observed_at':datetime.now(timezone.utc).isoformat(),
                    'channels':rows,'measurement_basis':'LOCAL_SAMPLED_TRANSPORT_STATE'})
            except Exception:probes.append({'observed_at':datetime.now(timezone.utc).isoformat(),'state':'UNKNOWN'})
            while not notices.empty() and len(records)<10000:records.append(notices.get_nowait())
            stop.wait(min(.5,max(0,deadline-time.monotonic())))
        while not notices.empty() and len(records)<10000:records.append(notices.get_nowait())
    finally:subscription.stop()
    end=datetime.now(timezone.utc).isoformat()
    return {'schema_version':'public-feed-soak.v1','source_kind':origin,'symbol':symbol,'timeframe':timeframe,
        'start':start,'end':end,'bounded_duration_seconds':duration_seconds,'cancelled':stop.is_set(),
        'observed_receipts':records,'transport_probes':probes,'overflow_count':overflow[0],
        'status':'PARTIAL' if overflow[0] else 'OBSERVED',
        'evidence_hash':digest([records,probes]),'continuous_uptime_certified':False,
        'htf_freshness':'UNKNOWN_UNLESS_SEPARATELY_MONITORED','decision_completeness':'NOT_EVALUATED',
        'profitability':'NOT_ASSESSED','execution_authorization':False}
