"""Idle and busy worker probes; no claim of continuous availability."""
from datetime import datetime,timezone,timedelta


class WorkerOperationalMonitor:
    def __init__(self,recorder,*,worker_kind,backend,owner_id='__owner__'):
        self.recorder=recorder;self.last=None
        self.scope={'registration_id':'operator-worker:'+worker_kind,'owner_id':owner_id,
            'timeframe':'5m','worker_kind':worker_kind,'worker_backend':backend}
        recorder.record(self.scope,'WORKER_STARTED',{'generation':recorder.worker_id,'scope':'OPERATOR_WORKER; NOT TENANT_TRADING_EVIDENCE'})

    def sample(self,*,database_available,ready,queue_backlog=None,stopped=False):
        now=self.recorder.clock()
        if self.last and (now-self.last).total_seconds()<60 and not stopped:return
        payload={'worker_ready':ready,'feed_connected':None,'database_available':database_available,
            'queue_backlog':queue_backlog,'measurement_basis':'LOCAL_PROCESS_PROBE; NO_UNOBSERVED_TIME_INFERRED'}
        self.recorder.record(self.scope,'HEALTH_PROBE',payload)
        if not database_available:self.recorder.record(self.scope,'DATABASE_UNAVAILABLE',{'scope':'METADATA_DATABASE'})
        store=self.recorder.store;spool=getattr(store,'spool',None)
        if spool:
            spool.replay(store.store,limit=100)
            spool.expire_delivered(before=now-timedelta(days=7))
        self.last=now
        if stopped:
            self.recorder.record(self.scope,'WORKER_STOPPED',{'reason':'GRACEFUL_SHUTDOWN'})
            self.recorder.record(self.scope,'MONITORING_STOPPED',{'reason':'GRACEFUL_SHUTDOWN'})

    def health(self):
        spool=getattr(self.recorder.store,'spool',None)
        return spool.health(owner_id=self.scope['owner_id']) if spool else {'status':'UNKNOWN','reason':'DURABLE_SPOOL_NOT_CONFIGURED'}
