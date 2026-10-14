"""Bounded research-job orchestration over the existing historical simulator.

Jobs are metadata/research operations only.  The runner uses real candles and
the existing ``strategy_presets`` simulator; it never opens a paper position or
writes a financial journal.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
import sys
import threading
import time
import uuid

from services.strategy_qualification import (
    chronological_partition, concentration_analysis, out_of_sample_report,
    seeded_monte_carlo, stress_costs, walk_forward_report, walk_forward_windows,
)

JOB_TYPES = {"HISTORICAL_BACKTEST", "OUT_OF_SAMPLE_VALIDATION", "ANCHORED_WALK_FORWARD",
             "ROLLING_WALK_FORWARD", "COST_STRESS", "ROBUSTNESS_EVALUATION", "QUALIFICATION_REFRESH"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _manifest(rows, *, symbol, timeframe, request, source):
    from data.historical import canonical_candle_manifest
    return {**canonical_candle_manifest(rows, symbol=symbol, timeframe=timeframe,
            provider=source, venue=request.get("venue"), market_type=request.get("market_type"),
            requested_start=_parse_time(request.get("period_start")),
            requested_end=_parse_time(request.get("period_end"))), "data_source": source}


def _parse_time(value):
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("INVALID_CONFIGURATION: timestamps must be ISO-8601") from None
    if parsed.tzinfo is None:
        raise ValueError("INVALID_CONFIGURATION: timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def _tf_ms(timeframe):
    from data.historical import _TF_MS
    if timeframe not in _TF_MS:
        raise ValueError(f"INVALID_CONFIGURATION: unsupported timeframe {timeframe}")
    return _TF_MS[timeframe]


def _metrics(result):
    from services.strategy_presets import _metrics as preset_metrics
    return preset_metrics(result or {})


class ResearchJobService:
    def __init__(self, context_store):
        self.context = context_store

    def create(self, request: dict, *, owner_id: str, scope: dict) -> dict:
        job_type = str(request.get("job_type", ""))
        if job_type not in JOB_TYPES:
            raise ValueError("unsupported research job type")
        strategy_id = str(request.get("strategy_id", "")).strip()
        symbol = str(request.get("symbol", "")).strip()
        timeframe = str(request.get("timeframe", "")).strip()
        if not strategy_id or not symbol or not timeframe:
            raise ValueError("strategy_id, symbol and timeframe are required")
        if not request.get("idempotency_key"):
            raise ValueError("idempotency_key is required")
        frozen = dict(request.get("frozen_config") or {})
        request_copy = {key: value for key, value in request.items() if key not in {"frozen_config"}}
        job = {"job_id": uuid.uuid4().hex, "idempotency_key": str(request["idempotency_key"]),
               "owner_id": owner_id, **{key: scope.get(key) for key in ("account_id", "instance_id", "simulation_session_id")},
               "job_type": job_type, "strategy_id": strategy_id,
               "strategy_version": request.get("strategy_version"), "config_fingerprint": request.get("config_fingerprint"),
               "frozen_config": frozen, "request": request_copy, "status": "QUEUED", "progress": 0,
               "requested_at": _now()}
        return self.context.create_research_job(job)

    def get(self, job_id: str, *, owner_id: str):
        return self.context.get_research_job(job_id, owner_id=owner_id)

    def list(self, *, owner_id: str, scope: dict, limit=100):
        return self.context.research_jobs(filters={"owner_id": owner_id, **{key: value for key, value in scope.items() if value is not None}}, limit=limit)

    def cancel(self, job_id: str, *, owner_id: str):
        job = self.get(job_id, owner_id=owner_id)
        if not job:
            return None
        return self.context.request_research_cancellation(job_id, owner_id=owner_id)

    def run(self, job_id: str, *, owner_id: str) -> dict:
        """Compatibility helper: execute one job through the durable worker."""
        worker = ResearchJobWorker(self.context, service=self, worker_id="compat-" + uuid.uuid4().hex[:8])
        return worker.process_job(job_id, owner_id=owner_id)

    def _execute(self, job, *, checkpoint=None):
        from data.market_data import get_bars
        from services.strategy_presets import _run_on
        from services.research_dataset_retention import ResearchDatasetRetention
        retention = ResearchDatasetRetention(self.context)
        request, strategy, symbol, timeframe = job["request"], job["strategy_id"], job["request"]["symbol"], job["request"]["timeframe"]
        # The registry uses stable IDs while the existing simulator resolves
        # its long-lived display-name presets.  This is an adapter only; the
        # strategy object and rules remain the existing implementation.
        from services.strategy_presets import PRESETS
        strategy_for_engine = next((name for name, spec in PRESETS.items()
                                    if spec.get("key") == strategy), strategy)
        if strategy == "adaptive_trend_pullback":
            strategy_for_engine = "Adaptive MTF Trend Pullback"
        limit = max(600, min(int(request.get("bars", 4000)), 10000))
        period_start = _parse_time(request.get("period_start"))
        period_end = _parse_time(request.get("period_end"))
        if (period_start is None) != (period_end is None):
            raise ValueError("INVALID_CONFIGURATION: period_start and period_end are both required")
        warmup = max(0, int(request.get("warmup", 0)))
        fetch_start = period_start - timedelta(milliseconds=_tf_ms(timeframe) * warmup) if period_start else None
        if checkpoint:
            checkpoint()
        retained = retention.load(job["job_id"], owner_id=job["owner_id"])
        if retained:
            rows, source = retained["bars"], retained["manifest"]["provider"]
        else:
            rows, source = get_bars(symbol, n=limit + warmup, timeframe=timeframe,
                                    since_ms=int(fetch_start.timestamp() * 1000) if fetch_start else None,
                                    end_ms=int(period_end.timestamp() * 1000) if period_end else None,
                                    require_real=True)
        if period_start:
            from data.historical import slice_candles
            try:
                sliced = slice_candles(rows, timeframe=timeframe, start=period_start, end=period_end, warmup=warmup)
            except ValueError as exc:
                raise RuntimeError(f"INSUFFICIENT_DATA: {exc}") from exc
            if len(sliced["warmup"]) < warmup:
                raise RuntimeError("INSUFFICIENT_DATA: required warm-up history is unavailable")
            evaluation_rows = sliced["evaluation"]
            coverage = sliced["coverage"]
        else:
            evaluation_rows, coverage = rows, {"status": "UNKNOWN", "warmup_candles": 0,
                                                "evaluation_candles": len(rows), "missing_candles": None,
                                                "duplicate_candles": None}
        manifest = _manifest(evaluation_rows, symbol=symbol, timeframe=timeframe, request=request, source=source)
        manifest["coverage"] = coverage
        if not rows:
            raise RuntimeError("INSUFFICIENT_DATA: real historical candles are unavailable")
        expected_dataset = request.get("dataset_identity")
        expected_revision = request.get("data_revision")
        if expected_dataset and expected_dataset != manifest["dataset_id"]:
            raise RuntimeError("DATASET_REVISION_MISMATCH: requested immutable dataset is unavailable")
        if expected_revision and expected_revision != manifest["content_hash"]:
            raise RuntimeError("DATASET_REVISION_MISMATCH: requested data revision is unavailable")
        manifest["retention"] = retention.retain(job, manifest, rows)
        tuning = job.get("frozen_config") or {}
        def evaluate(segment, **_):
            if checkpoint:
                checkpoint()
            return _metrics(_run_on(strategy_for_engine, symbol, timeframe, tuning, None, segment))
        method = job["job_type"]
        if method == "HISTORICAL_BACKTEST":
            report = {"status": "COMPLETE", "method": "historical_backtest", "metrics": evaluate(rows), "dataset": manifest}
        elif method == "OUT_OF_SAMPLE_VALIDATION":
            parts = chronological_partition([{"bar": row, "timestamp": row.timestamp.isoformat()} for row in rows],
                                            train=float(request.get("train_fraction", .6)), validation=float(request.get("validation_fraction", .2)),
                                            holdout=float(request.get("holdout_fraction", .2)), warmup=int(request.get("warmup", 0)))
            report = out_of_sample_report({key: [item["bar"] for item in value] for key, value in parts.items() if key in {"train", "validation", "holdout"}}, evaluate)
            report["dataset"] = manifest; report["partitions"] = parts["boundaries"]
        elif method in {"ANCHORED_WALK_FORWARD", "ROLLING_WALK_FORWARD"}:
            windows = walk_forward_windows([{"bar": row, "timestamp": row.timestamp.isoformat()} for row in rows],
                development_size=int(request.get("development_size", max(1, len(rows) // 3))),
                evaluation_size=int(request.get("evaluation_size", max(1, len(rows) // 6))), anchored=method == "ANCHORED_WALK_FORWARD")
            report = walk_forward_report([{**window, "development": [item["bar"] for item in window["development"]],
                                           "evaluation": [item["bar"] for item in window["evaluation"]]} for window in windows], evaluate)
            report["dataset"] = manifest
        else:
            base = evaluate(rows); outcomes = [trade.get("r") for trade in (_run_on(strategy_for_engine, symbol, timeframe, tuning, None, rows).get("trades") or [])]
            if method == "COST_STRESS":
                report = {"method": "cost_stress", "base": base,
                          "stress": stress_costs(outcomes, fee=request.get("fee", "0"), slippage=request.get("slippage", "0"), funding=request.get("funding"))}
            elif method == "ROBUSTNESS_EVALUATION":
                report = {"method": "robustness", "base": base, "concentration": concentration_analysis(outcomes),
                          "monte_carlo": seeded_monte_carlo(outcomes, runs=int(request.get("runs", 1000)), seed=int(request.get("seed", 1))),
                          "status": "COMPLETE"}
            else:
                report = {"method": "qualification_refresh", "status": "COMPLETE", "note": "Qualification refresh reads persisted validation evidence only."}
            report["dataset"] = manifest
        report.setdefault("status", "COMPLETE")
        validation_method = report.get("method", method.lower())
        return report

    @staticmethod
    def validation_record(job: dict, report: dict) -> dict:
        dataset = report.get("dataset") or {}
        request = job.get("request") or {}
        return {"run_id": "validation:" + job["job_id"], "strategy_id": job["strategy_id"],
            "strategy_version": job.get("strategy_version"), "config_fingerprint": job.get("config_fingerprint"),
            "dataset_identity": dataset.get("dataset_id", "unknown:" + job["job_id"]),
            "data_revision": dataset.get("content_hash"), "execution_mode": "backtest",
            "method": report.get("method", job["job_type"].lower()), "status": report.get("status", "COMPLETE"),
            "period_start": dataset.get("period_start"), "period_end": dataset.get("period_end"),
            "policy_version": request.get("policy_version"), "cost_model_version": request.get("cost_model_version"),
            "classifier_version": request.get("classifier_version"), "software_revision": request.get("software_revision"),
            "random_seed": request.get("seed"), "owner_id": job["owner_id"], "account_id": job.get("account_id"),
            "instance_id": job.get("instance_id"), "simulation_session_id": job.get("simulation_session_id"), "report": report}


class ResearchCancelled(Exception):
    """Cooperative cancellation/timeout signal inside a research attempt."""


class ResearchInterrupted(Exception):
    """Cooperative worker shutdown; incomplete work cannot commit."""


class ResearchJobWorker:
    """Small database-backed worker; no new queue or trading authority."""
    def __init__(self, context_store, *, service=None, worker_id=None, lease_seconds=60,
                 poll_seconds=1.0, max_active=1):
        self.context = context_store
        self.service = service or ResearchJobService(context_store)
        self.worker_id = worker_id or ("research-worker-" + uuid.uuid4().hex[:10])
        self.lease_seconds = max(5, int(lease_seconds))
        self.poll_seconds = max(.05, float(poll_seconds))
        self.max_active = max(1, int(max_active))
        self._stop = threading.Event()
        self._thread = None
        self.telemetry=None
        if getattr(context_store,'backend',None)=='postgresql':
            from services.operational_evidence import OperationalEvidenceStore,ShadowOperationalRecorder
            from services.postgres_workers import PgShadowObservationStore
            from services.operational_spool import worker_operational_store
            self.telemetry=ShadowOperationalRecorder(worker_operational_store(OperationalEvidenceStore(PgShadowObservationStore(context_store.journal._dsn))),
                self.worker_id,clock=lambda:datetime.now(timezone.utc))

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self):
        if getattr(self.context,'backend',None)=='postgresql' and any(name in sys.modules for name in ('app','webhook_api','execution.paper_engine')):
            return False  # durable PG work belongs to the credentials-free external worker
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="research-worker", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s=5):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout_s)
        if not self._thread or not self._thread.is_alive():self._thread = None

    def _run(self):
        while not self._stop.is_set():
            try:
                self.context.recover_expired_research_jobs()
                if not self.process_once():
                    self._stop.wait(self.poll_seconds)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).error('research_worker_iteration_failed',extra={'worker_id':self.worker_id,'error_category':type(exc).__name__})
                self._stop.wait(self.poll_seconds)

    def process_once(self):
        claimed = self.context.claim_research_job(worker_id=self.worker_id, lease_seconds=self.lease_seconds,
                                                   max_active=self.max_active)
        if not claimed:
            return False
        self.process_claimed(claimed)
        return True

    def process_claimed(self, claimed):
        job_id, token = claimed["job_id"], claimed["lease_token"]
        observed={**claimed,'registration_id':job_id,'timeframe':(claimed.get('request') or {}).get('timeframe'),
            'symbol':(claimed.get('request') or {}).get('symbol')}
        committing=False
        if self.telemetry:self.telemetry.record(observed,'WORKER_STARTED',{'attempt':claimed['attempt_number'],'lease_token':token})
        try:
            self.context.transition_research_job(job_id, to_status="RUNNING", worker_id=self.worker_id,
                                                 lease_token=token, reason="worker_started",
                                                 changes={"started_at": _now(), "progress": 1})
            if self.telemetry:self.telemetry.record(observed,'EVALUATION_STARTED',{'job_id':job_id,'attempt':claimed['attempt_number']})
            started = time.monotonic()
            def checkpoint():
                if self._stop.is_set():
                    raise ResearchInterrupted('WORKER_SHUTDOWN')
                if not self.context.heartbeat_research_job(job_id, worker_id=self.worker_id,
                                                           lease_token=token, lease_seconds=self.lease_seconds):
                    raise ResearchCancelled("lease lost or cancellation accepted")
                if self.context.get_research_job(job_id).get("status") == "CANCEL_REQUESTED":
                    raise ResearchCancelled("cancelled by user")
                if time.monotonic() - started > float((claimed.get("request") or {}).get("max_runtime_seconds", 3600)):
                    raise TimeoutError("research job runtime limit exceeded")
                if self.telemetry:self.telemetry.probe(observed,{})
            job = self.context.get_research_job(job_id)
            result = self.service._execute(job, checkpoint=checkpoint)
            checkpoint()
            if self.telemetry:self.telemetry.record(observed,'EVALUATION_COMPLETED',{'job_id':job_id,'attempt':claimed['attempt_number']})
            committing=True
            self.context.commit_research_result(job_id, worker_id=self.worker_id, lease_token=token,
                                                report=result, validation=self.service.validation_record(job, result))
            if self.telemetry:self.telemetry.record(observed,'COMMIT_SUCCEEDED',{'job_id':job_id,'validation_run_id':'validation:'+job_id},key=token+':result-commit')
        except ResearchInterrupted:
            current=self.context.get_research_job(job_id)
            if current and current.get('lease_token')==token:
                if current.get('status')=='CANCEL_REQUESTED':
                    self.context.transition_research_job(job_id,to_status='CANCELLED',worker_id=self.worker_id,
                        lease_token=token,reason='cancelled_during_shutdown',changes={'completed_at':_now()})
                else:
                    self.context.transition_research_job(job_id,to_status='INTERRUPTED',worker_id=self.worker_id,
                        lease_token=token,reason='WORKER_SHUTDOWN',changes={'error_class':'WORKER_INTERRUPTED'})
                    exhausted=current.get('attempt_number',0)>=current.get('max_attempts',3)
                    self.context.transition_research_job(job_id,to_status='FAILED' if exhausted else 'RETRY_PENDING',
                        reason='shutdown_retry_exhausted' if exhausted else 'graceful_shutdown_retry',
                        changes={'retry_at':_now()})
        except ResearchCancelled as exc:
            current = self.context.get_research_job(job_id)
            if current and current.get("status") == "CANCEL_REQUESTED":
                self.context.transition_research_job(job_id, to_status="CANCELLED", worker_id=self.worker_id,
                                                     lease_token=token, reason="cancelled_by_user",
                                                     changes={"completed_at": _now()})
        except Exception as exc:  # research failures never touch trading state
            if self.telemetry:self.telemetry.record(observed,'COMMIT_FAILED' if committing else 'EVALUATION_ERROR',{'error_category':type(exc).__name__,'attempt':claimed['attempt_number']})
            current = self.context.get_research_job(job_id)
            if not current or current.get("lease_token") != token:
                return
            if current.get('status')=='COMPLETED':
                # A lost COMMIT acknowledgement cannot turn durable completed
                # evidence into an interrupted retry. Read the atomic journal.
                if committing and current.get('result')==result and current.get('result_reference'):
                    if self.telemetry:
                        self.telemetry.record(observed,'RECOVERY_COMPLETED',{'reason':'RESULT_ACKNOWLEDGEMENT_RECOVERED','validation_run_id':current['result_reference']})
                        self.telemetry.record(observed,'COMMIT_SUCCEEDED',{'job_id':job_id,'validation_run_id':current['result_reference']},key=token+':result-commit')
                return
            error_class = "JOB_TIMEOUT" if isinstance(exc, TimeoutError) else type(exc).__name__
            transient = isinstance(exc, (TimeoutError, ConnectionError, OSError))
            attempts = int(current.get("attempt_number") or 0)
            if transient and attempts < int(current.get("max_attempts") or 3):
                self.context.transition_research_job(job_id, to_status="INTERRUPTED", worker_id=self.worker_id,
                                                     lease_token=token, reason=error_class,
                                                     changes={"error_class": error_class, "error_message": str(exc)[:500]})
                self.context.transition_research_job(job_id, to_status="RETRY_PENDING", reason="bounded_retry",
                                                     changes={"retry_at": _now()})
            else:
                self.context.transition_research_job(job_id, to_status="FAILED", worker_id=self.worker_id,
                                                     lease_token=token, reason=error_class,
                                                     changes={"error_class": error_class, "error_message": str(exc)[:500], "completed_at": _now()})
        finally:
            if self.telemetry:
                self.telemetry.record(observed,'MONITORING_STOPPED',{'attempt':claimed['attempt_number'],'reason':'ATTEMPT_EXITED'})
                self.telemetry.started.discard(job_id);self.telemetry.last_probe.pop(job_id,None)

    def process_job(self, job_id: str, *, owner_id: str):
        job = self.context.get_research_job(job_id, owner_id=owner_id)
        if not job:
            raise KeyError(job_id)
        if job.get("status") in TERMINAL:
            return job
        claimed = self.context.claim_research_job(worker_id=self.worker_id, lease_seconds=self.lease_seconds,
                                                   max_active=self.max_active)
        if not claimed or claimed["job_id"] != job_id:
            return self.context.get_research_job(job_id, owner_id=owner_id)
        self.process_claimed(claimed)
        return self.context.get_research_job(job_id, owner_id=owner_id)
