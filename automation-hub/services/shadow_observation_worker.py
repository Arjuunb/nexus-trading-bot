"""Bounded observation worker on the existing shared public market-data hub.

No HTTP request performs evaluation. Candle callbacks merely wake this thread.
SQLite leases fence journal commits; this is NOT a process security sandbox.
"""
from datetime import datetime, timezone
import logging
import sys
import queue
import threading
import uuid

from services.shadow_router import utc

logger = logging.getLogger(__name__)


class ShadowObservationWorker:
    def __init__(self, router, market_hub, *, interval=2):
        self.router = router; self.hub = market_hub; self.interval = interval
        self.worker_id = "shadow-worker:" + uuid.uuid4().hex
        self._wake = threading.Event(); self._stop = threading.Event(); self._lock = threading.Lock()
        self._thread = None; self._owned = {}
        self._last_sweep = None; self._last_error = None
        self._market_evidence=queue.Queue(maxsize=256);self._market_evidence_dropped=0
        from services.operational_evidence import OperationalEvidenceStore,ShadowOperationalRecorder
        from services.operational_spool import worker_operational_store
        self.telemetry=ShadowOperationalRecorder(worker_operational_store(OperationalEvidenceStore(router.store,clock=router.clock)),self.worker_id,clock=router.clock)
        router.worker = self

    def wake(self):
        self._wake.set()

    def _notice_market_evidence(self,reg,metadata):
        # Called on the shared feed thread: bounded enqueue only, no SQL/network.
        scope={k:reg.get(k) for k in ('registration_id','owner_id','strategy_id','strategy_version','config_fingerprint','venue','market_type','symbol','timeframe')}
        try:self._market_evidence.put_nowait((scope,metadata))
        except queue.Full:self._market_evidence_dropped+=1
        self.wake()

    def _drain_market_evidence(self):
        from services.shadow_router import candle_material
        from services.strategy_identity import configuration_fingerprint
        from bot.data.resample import TF_SECONDS
        for _ in range(100):
            try:reg,metadata=self._market_evidence.get_nowait()
            except queue.Empty:break
            tf=metadata.get('timeframe')
            if tf!=reg['timeframe']:continue
            bar=metadata.get('bar')
            if bar is None or tf not in TF_SECONDS:continue
            from datetime import timedelta
            try:
                self.telemetry.record(reg,'CANDLE_RECEIVED',{'origin':metadata.get('origin','UNKNOWN'),
                    'candle_close':(bar.timestamp+timedelta(seconds=TF_SECONDS[tf])).isoformat(),
                    'candle_hash':configuration_fingerprint(candle_material(bar)),'finalized':metadata.get('finalized'),
                    'delivery_observed_at':metadata.get('delivery_observed_at'),'timeframe':tf},occurred_at=metadata.get('delivery_observed_at'))
            except (ValueError,TypeError,AttributeError):self.telemetry.record(reg,'INVALID_CANDLE',{'timeframe':tf})

    def health(self):
        age = (self.router.clock()-utc(self._last_sweep)).total_seconds() if self._last_sweep else None
        readiness = "STOPPED" if self._stop.is_set() else "DEGRADED" if self._last_error else "UNKNOWN" if age is None else "READY" if 0 <= age <= max(10,self.interval*3) else "STALE"
        return {"worker_id":self.worker_id,"readiness":readiness,"last_successful_sweep":self._last_sweep,
                "telemetry_persistence_failures":self.telemetry.failures,
                "telemetry_buffer_dropped":self._market_evidence_dropped,
                "sweep_age_seconds":age,"error_category":self._last_error,"owned_registrations":len(self._owned),
                "concurrency":1,"database_backend":getattr(self.router.store,'backend','sqlite'),"resource_boundary":"COOPERATIVE_NOT_HARD_SANDBOX"}

    def start(self):
        if getattr(self.router.store,'backend',None)=='postgresql' and any(name in sys.modules for name in ('app','webhook_api','execution.paper_engine')):
            return  # PostgreSQL observation is supervised independently of financial execution
        with self._lock:
            if self._thread and self._thread.is_alive(): return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="shadow-observation", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set(); self.wake()
        thread = self._thread
        if thread: thread.join(timeout=5)
        if thread and thread.is_alive():
            # Fencing still protects commits; never claim a hard-killed worker.
            return {"errors": ["Shadow worker shutdown acknowledgement timed out"]}
        self._detach_all()
        return {"errors": []}

    def _detach(self, rid):
        owned = self._owned.pop(rid, None)
        if owned:
            reg=owned.get('registration')
            if reg:self.telemetry.record(reg,'MONITORING_STOPPED',{'reason':'WORKER_DETACHED'})
            self.telemetry.started.discard(rid);self.telemetry.last_probe.pop(rid,None)
            for sub in owned["subscriptions"].values(): sub.stop()
            self.router.store.release(rid, owned["token"])

    def _detach_all(self):
        for rid in list(self._owned): self._detach(rid)

    def _loop(self):
        try:
            while not self._stop.is_set():
                try: self.tick()
                except Exception as exc:
                    self._last_error = type(exc).__name__
                    logger.error("shadow_worker_iteration_failed", extra={"worker_id": self.worker_id, "error_category": type(exc).__name__})
                self._wake.wait(self.interval); self._wake.clear()
        finally:
            try: self._detach_all()
            except Exception as exc: logger.error("shadow_worker_release_failed", extra={"error_category": type(exc).__name__})

    def tick(self):
        self._drain_market_evidence()
        self._last_error = None
        now = self.router.clock(); epoch = now.timestamp()
        registrations = self.router.store.registrations(active_only=True, limit=self.router.policy["max_global"])
        desired = {r["registration_id"] for r in registrations}
        for rid in set(self._owned) - desired: self._detach(rid)
        for reg in registrations:
            if self._stop.is_set(): break
            rid = reg["registration_id"]; owner = reg["owner_id"]
            try:
                local = self._owned.get(rid)
                if local and not self.router.store.heartbeat(rid, local["token"], now=epoch): self._detach(rid); local = None
                if not local:
                    claimed = self.router.store.claim(rid, self.worker_id, now=epoch)
                    if not claimed: continue
                    local = {"token": claimed["lease_token"], "registration":reg,"subscriptions": {}, "attached_at": now, "retry_at": epoch + 5, "retry_seconds": 5}
                    self._owned[rid] = local
                    self.router.store.event(rid, owner_id=owner, action="WORKER_ATTACHED", payload={"worker_id": self.worker_id, "at": now.isoformat(), "backfill": False})
                    for tf in dict.fromkeys([*reg["lookback_spec"].get("required_candles", {}), *reg["lookback_spec"].get("optional_timeframes", [])]):
                        sub = self.hub.subscription(consumer_id=f"{self.worker_id}:{rid}:{tf}", candle_notice=lambda bar: self.wake())
                        if hasattr(sub,'consumer'):sub.consumer.observation_notice=lambda metadata,registered=reg:self._notice_market_evidence(registered,metadata)
                        if tf==reg['timeframe']:self.telemetry.capture_supported[rid]=hasattr(sub,'consumer')
                        local["subscriptions"][tf] = sub
                        sub.start(reg["symbol"], tf, quotes=False)
                if epoch >= local["retry_at"]:
                    failed = False
                    for tf in dict.fromkeys([*reg["lookback_spec"].get("required_candles", {}), *reg["lookback_spec"].get("optional_timeframes", [])]):
                        sub = local["subscriptions"].get(tf)
                        if sub is None:
                            sub = self.hub.subscription(consumer_id=f"{self.worker_id}:{rid}:{tf}", candle_notice=lambda bar: self.wake())
                            if hasattr(sub,'consumer'):sub.consumer.observation_notice=lambda metadata,registered=reg:self._notice_market_evidence(registered,metadata)
                            local["subscriptions"][tf] = sub
                        if not getattr(sub, "running", True):
                            failed = not sub.start(reg["symbol"], tf, quotes=False) or failed
                    local["retry_seconds"] = min(30, local["retry_seconds"] * 2) if failed else 5
                    local["retry_at"] = epoch + local["retry_seconds"]
                previous_probe=self.telemetry.last_probe.get(rid)
                if self._market_evidence_dropped:
                    lost=self._market_evidence_dropped
                    if self.telemetry.record(reg,'TELEMETRY_LOSS',{'dropped_market_receipts':lost,'scope':'WORKER_PROCESS','not_reconstructed':True}):self._market_evidence_dropped=0
                if previous_probe is None or (now-previous_probe).total_seconds()>=60:
                    entry_sub=local['subscriptions'].get('5m')
                    probe=entry_sub.status() if entry_sub and hasattr(entry_sub,'status') else entry_sub.snapshot().get('connection',{}) if entry_sub else {}
                    self.telemetry.probe(reg,probe)
                # Process only opportunities occurring AFTER this worker attached.
                # Cached REST/bootstrap candles are warm-up, never live evidence.
                boundary_epoch = int(epoch // 300) * 300
                cutoff = datetime.fromtimestamp(boundary_epoch, timezone.utc)
                if cutoff < local["attached_at"] or cutoff < utc(reg["resume_after"]): continue
                if reg["last_decision_time"] and cutoff <= utc(reg["last_decision_time"]): continue
                snapshots = {tf: sub.snapshot() for tf, sub in local["subscriptions"].items()}
                entry = snapshots.get("5m", {})
                connection = entry.get("connection", {})
                self.telemetry.probe(reg,connection)
                # Brief close-delivery grace while transport is healthy. A dead
                # feed still records the scheduled blocked opportunity promptly.
                bars = entry.get("closed_bars", [])
                latest_closed = utc(bars[-1].timestamp).timestamp() + 300 if bars else None
                if latest_closed != boundary_epoch and epoch - boundary_epoch < 5 and connection.get("transport_state") == "CONNECTED": continue
                frame = {"venue": reg["venue"], "market_type": reg["market_type"], "symbol": reg["symbol"], "timeframe": reg["timeframe"],
                         "source": "BINANCE_USDM_PUBLIC", "finalized": True, "decision_time": cutoff.isoformat(),
                         "connected": connection.get("transport_state") == "CONNECTED" and connection.get("reliable") is True,
                         "available_at": now.isoformat(), "stream_observed_at": connection.get("last_candle_update"),
                         "context": {tf: snap.get("closed_bars", []) for tf, snap in snapshots.items()},
                         "htf_health": {tf: snap.get("connection", {}).get("reliable") is True for tf, snap in snapshots.items()}}
                if reg["last_decision_time"] and cutoff.timestamp() - utc(reg["last_decision_time"]).timestamp() > 300:
                    self.router.store.event(rid, owner_id=owner, action="OBSERVATION_GAP", payload={"reason": "MISSED_SCHEDULED_OPPORTUNITIES", "after": reg["last_decision_time"], "until": cutoff.isoformat(), "backfilled": False})
                result = self.router.evaluate(rid, frame, owner_id=owner, lease_token=local["token"])
                if result:
                    self.telemetry.decision(reg,result)
                    logger.info("shadow_decision_committed", extra={"registration_id": rid, "decision_id": result["decision_id"], "status": result["status"]})
            except Exception as exc:
                self._last_error = type(exc).__name__
                self.telemetry.record(reg,'COMMIT_FAILED',{'error_category':type(exc).__name__})
                # No secrets/dataset bodies/exception messages in logs.
                logger.error("shadow_observation_failed", extra={"registration_id": rid, "error_category": type(exc).__name__})
                try: self.router.store.event(rid, owner_id=owner, action="OBSERVATION_FAILURE", payload={"error_category": type(exc).__name__, "at": now.isoformat()})
                except Exception: pass  # expired lease records the unavailable interval on recovery
        if not self._last_error: self._last_sweep = self.router.clock().isoformat()
