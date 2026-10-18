"""Intelligence metadata delegate using the existing JournalStore transaction.

Only immutable observation/research facts and disposable calculation reports
are stored here. This class cannot place orders or write a financial ledger.
Omitted internal filters mean all matching journal records; supplied None is
exact unknown scope. Symbol=None retains v1's explicit aggregate-assets meaning.
Externally exposed reads must resolve owner/account scope before calling here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from services.strategy_evidence import SCOPE_FIELDS, evidence_json
from services.strategy_identity import canonical_configuration_json, configuration_fingerprint

if TYPE_CHECKING:
    from data.journal_store import JournalStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value, field: str, *, optional=False) -> str | None:
    if value is None and optional:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} requires an aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} requires an aware timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _positive_limit(value, maximum=100_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}")
    return value


class JournalContextStore:
    """Additive metadata operations sharing the parent lock and transaction."""

    COHORT_FIELDS = (*SCOPE_FIELDS[:2], "config_fingerprint", *SCOPE_FIELDS[3:])
    _CONTEXT_FIELDS = (*SCOPE_FIELDS, "snapshot_id", "episode_id", "trade_id", "classification_kind",
                       "classifier_id", "classifier_version", "parameter_hash", "entry_timeframe",
                       "higher_timeframe", "direction", "session", "trend_regime", "volatility_regime",
                       "structure_regime", "context_quality", "evidence_quality")
    _ALIASES = {"configuration_hash": "strategy_config_hash", "config_fingerprint": "strategy_config_hash",
                "configuration_fingerprint": "strategy_config_hash"}
    _TIMESTAMP_FIELDS = ("signal_timestamp", "entry_timestamp", "classification_timestamp",
                         "market_data_timestamp", "last_closed_candle_timestamp",
                         "higher_timeframe_last_closed_candle_timestamp", "higher_timeframe_market_data_timestamp")
    _MARKET_TIMESTAMPS = _TIMESTAMP_FIELDS[3:]

    def __init__(self, journal: JournalStore):
        self.journal = journal

    @staticmethod
    def _required_text(payload: dict, field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        return value

    def save_classifier_version(self, definition: dict) -> dict:
        frozen = dict(definition)
        classifier_id = self._required_text(frozen, "classifier_id")
        version = self._required_text(frozen, "classifier_version")
        fingerprint = self._required_text(frozen, "parameter_hash")
        parameters = frozen.get("parameters")
        if not isinstance(parameters, dict) or configuration_fingerprint(parameters) != fingerprint:
            raise ValueError("classifier parameter hash mismatch")
        canonical = canonical_configuration_json(parameters)
        frozen["parameters"] = json.loads(canonical)
        # Observation time is storage metadata, not part of the mathematical definition.
        frozen.pop("calculation_timestamp", None)
        frozen.pop("captured_at", None)
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            row = self.journal._c.execute("""SELECT definition_json FROM regime_classifier_versions
                WHERE classifier_id=? AND classifier_version=?""", (classifier_id, version)).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("immutable classifier version conflict")
                return json.loads(row[0])
            self.journal._c.execute("""INSERT INTO regime_classifier_versions
                (classifier_id,classifier_version,parameter_hash,parameters_json,definition_json,captured_at)
                VALUES (?,?,?,?,?,?)""", (classifier_id, version, fingerprint, canonical, encoded, _now()))
        return json.loads(encoded)

    def classifier_versions(self, *, filters: dict | None = None, limit=1000) -> list[dict]:
        where, args = self._filters(filters, allowed={"classifier_id", "classifier_version", "parameter_hash"})
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT definition_json FROM regime_classifier_versions" + where
                + " ORDER BY classifier_id,classifier_version LIMIT ?", (*args, _positive_limit(limit)))
            return [json.loads(row[0]) for row in rows]

    def record_market_context(self, snapshot: dict) -> bool:
        frozen = dict(snapshot)
        for alias, canonical in self._ALIASES.items():
            if alias in frozen:
                if canonical in frozen and frozen[canonical] != frozen[alias]:
                    raise ValueError("context configuration alias conflict")
                frozen[canonical] = frozen[alias]
        for key in SCOPE_FIELDS:
            frozen.setdefault(key, None)
        frozen["configuration_hash"] = frozen["strategy_config_hash"]
        frozen["config_fingerprint"] = frozen["strategy_config_hash"]
        kind = frozen.setdefault("classification_kind", "ENTRY")
        if kind not in {"ENTRY", "RESEARCH"}:
            raise ValueError("classification_kind must be ENTRY or RESEARCH")
        key_fields = ("episode_id", "trade_id", "classifier_id", "classifier_version", "parameter_hash")
        for field in key_fields:
            self._required_text(frozen, field)
        for field in self._TIMESTAMP_FIELDS:
            if field in frozen or field in ("signal_timestamp", "entry_timestamp", "classification_timestamp"):
                frozen[field] = _timestamp(frozen.get(field), field, optional=field != "classification_timestamp")
        cutoff = frozen.get("signal_timestamp")
        if cutoff is None:
            if (frozen.get("context_quality") not in {"UNKNOWN", "INSUFFICIENT_HISTORY"}
                    or any(frozen.get(field) is not None for field in self._MARKET_TIMESTAMPS)
                    or any(frozen.get(field) not in (None, "UNKNOWN") for field in
                           ("session", "trend_regime", "volatility_regime", "structure_regime"))
                    or any(frozen.get(field) is not None for field in
                           ("trend_strength", "atr_value", "atr_percentile"))):
                raise ValueError("unknown signal timestamp cannot support classified market data")
        else:
            cutoff_at = datetime.fromisoformat(cutoff)
            for field in self._MARKET_TIMESTAMPS:
                if frozen.get(field) and datetime.fromisoformat(frozen[field]) > cutoff_at:
                    raise ValueError(f"future market data in {field}")
        semantic_key = [frozen.get(field) for field in (*key_fields, "classification_kind")]
        expected_id = "context:" + hashlib.sha256(evidence_json(semantic_key).encode()).hexdigest()
        if frozen.get("snapshot_id") not in (None, expected_id):
            raise ValueError("context snapshot_id does not match immutable identity")
        frozen["snapshot_id"] = expected_id
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            episode = self.journal._c.execute("SELECT * FROM strategy_position_episodes WHERE episode_id=?",
                (frozen["episode_id"],)).fetchone()
            if not episode:
                raise ValueError("context episode reference missing")
            if any(episode[field] != frozen.get(field) for field in SCOPE_FIELDS):
                raise ValueError("context episode scope conflict")
            entry = self.journal._c.execute("""SELECT e.payload_json FROM strategy_evidence_events e
                WHERE e.episode_id=? AND e.trade_id=? AND e.kind='execution_fill' ORDER BY e.rowid""",
                (frozen["episode_id"], frozen["trade_id"])).fetchall()
            if not any(json.loads(row[0]).get("action") in {"opened", "recovered"} for row in entry):
                raise ValueError("context trade is not an actual episode entry")
            definition = self.journal._c.execute("""SELECT 1 FROM regime_classifier_versions
                WHERE classifier_id=? AND classifier_version=? AND parameter_hash=?""",
                (frozen["classifier_id"], frozen["classifier_version"], frozen["parameter_hash"])).fetchone()
            if not definition:
                raise ValueError("context classifier version reference missing")
            row = self.journal._c.execute("SELECT payload_json FROM market_context_snapshots WHERE snapshot_id=?",
                (expected_id,)).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("immutable market context conflict")
                return False
            columns = (*self._CONTEXT_FIELDS, "signal_timestamp", "entry_timestamp", "classification_timestamp",
                       "payload_json", "captured_at")
            self.journal._c.execute(f"INSERT INTO market_context_snapshots ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                (*(frozen.get(field) for field in columns[:-2]), encoded, _now()))
        return True

    def _filters(self, filters, *, allowed=None, prefix=""):
        allowed = set(self._CONTEXT_FIELDS) if allowed is None else allowed
        clauses, args = [], []
        for key, value in (filters or {}).items():
            key = self._ALIASES.get(key, key)
            if key not in allowed:
                raise ValueError(f"unsupported intelligence filter: {key}")
            if key == "symbol" and value is None:
                continue  # Same explicit aggregate-assets contract as v1.
            if value is None:
                clauses.append(f"{prefix}{key} IS NULL")
            else:
                clauses.append(f"{prefix}{key}=?")
                args.append(value)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    def get_market_context(self, episode_id: str, classifier_id: str, classifier_version: str,
                           parameter_hash: str, classification_kind="ENTRY", trade_id=None) -> dict | None:
        with self.journal._lock:
            if trade_id is None:
                episode = self.journal._c.execute("SELECT root_trade_id FROM strategy_position_episodes WHERE episode_id=?",
                    (episode_id,)).fetchone()
                if not episode:
                    return None
                trade_id = episode[0]
            rows = self.market_contexts(filters={"episode_id": episode_id, "trade_id": trade_id,
                "classifier_id": classifier_id, "classifier_version": classifier_version,
                "parameter_hash": parameter_hash, "classification_kind": classification_kind}, limit=1)
            return rows[0] if rows else None

    def market_contexts(self, *, filters: dict | None = None, limit=1000, after_snapshot_id=None) -> list[dict]:
        where, args = self._filters(filters)
        if after_snapshot_id is not None:
            where += (" AND " if where else " WHERE ") + "snapshot_id>?"
            args.append(after_snapshot_id)
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT payload_json FROM market_context_snapshots" + where
                + " ORDER BY snapshot_id LIMIT ?", (*args, _positive_limit(limit)))
            return [json.loads(row[0]) for row in rows]

    def context_snapshot(self, *, max_records=100_000, filters: dict | None = None) -> dict:
        bound = _positive_limit(max_records)
        where, args = self._filters(filters)
        with self.journal.transaction():
            count = self.journal._c.execute("SELECT COUNT(*) FROM market_context_snapshots" + where, args).fetchone()[0]
            rows = self.journal._c.execute("SELECT payload_json FROM market_context_snapshots" + where
                + " ORDER BY snapshot_id LIMIT ?", (*args, bound)).fetchall()
            contexts = [json.loads(row[0]) for row in rows]
            watermark = hashlib.sha256(evidence_json({"contexts": contexts, "source_count": count}).encode()).hexdigest()
            return {"contexts": contexts, "watermark": watermark, "source_count": count,
                    "source_complete": count <= bound, "bound": bound}

    def context_identity_snapshot(self, *, filters: dict | None = None, max_records=100_000) -> dict:
        """One bounded metadata SELECT for incremental worker membership checks.

        The window count reports overflow without loading candle payloads or
        issuing one context query for every historical episode.
        """
        bound = _positive_limit(max_records)
        where, args = self._filters(filters)
        columns = ("episode_id", "trade_id", "classifier_id", "classifier_version",
                   "parameter_hash", "classification_kind")
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT " + ",".join(columns)
                + ",COUNT(*) OVER () AS source_count FROM market_context_snapshots" + where
                + " ORDER BY " + ",".join(columns) + " LIMIT ?", (*args, bound + 1)).fetchall()
            count = rows[0]["source_count"] if rows else 0
            return {"keys": [tuple(row[field] for field in columns) for row in rows[:bound]],
                    "source_count": count, "source_complete": count <= bound, "bound": bound}

    def unclassified_episodes(self, *, classifier_id, classifier_version, parameter_hash,
                              classification_kind="ENTRY", limit=500, filters: dict | None = None) -> list[dict]:
        where, args = self._filters(filters, allowed={"episode_id", *SCOPE_FIELDS}, prefix="e.")
        condition = """NOT EXISTS (SELECT 1 FROM market_context_snapshots c
            WHERE c.episode_id=e.episode_id AND c.trade_id=e.root_trade_id AND c.classifier_id=?
              AND c.classifier_version=? AND c.parameter_hash=? AND c.classification_kind=?)"""
        where += (" AND " if where else " WHERE ") + condition
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT e.* FROM strategy_position_episodes e" + where
                + " ORDER BY e.rowid LIMIT ?", (*args, classifier_id, classifier_version, parameter_hash,
                                             classification_kind, _positive_limit(limit)))
            return [dict(row) for row in rows]

    def cached_cohorts(self, *, filters: dict | None = None, limit=500) -> list[dict]:
        where, args = self._filters(filters)
        columns = ",".join("strategy_config_hash AS config_fingerprint" if field == "config_fingerprint" else field
                           for field in self.COHORT_FIELDS)
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT DISTINCT " + columns + " FROM market_context_snapshots" + where
                + " ORDER BY " + columns.replace("strategy_config_hash AS config_fingerprint", "strategy_config_hash")
                + " LIMIT ?", (*args, _positive_limit(limit)))
            return [dict(row) for row in rows]

    def record_intelligence_run(self, report: dict) -> bool:
        frozen = dict(report)
        for field in ("run_id", "cache_key", "input_watermark", "contract_version"):
            self._required_text(frozen, field)
        frozen["calculated_at"] = _timestamp(frozen.get("calculated_at"), "calculated_at")
        if not isinstance(frozen.get("scope"), dict) or not isinstance(frozen.get("cohort"), dict):
            raise ValueError("intelligence run requires explicit scope and cohort")
        encoded = evidence_json(frozen)
        scope, cohort = frozen["scope"], frozen["cohort"]
        for field in ("owner_id", "account_id", "instance_id"):
            if field in scope and field in cohort and scope[field] != cohort[field]:
                raise ValueError("intelligence run scope conflict")
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT report_json FROM intelligence_calculation_runs WHERE run_id=?",
                (frozen["run_id"],)).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("immutable intelligence run conflict")
                return False
            self.journal._c.execute("""INSERT INTO intelligence_calculation_runs
                (run_id,cache_key,input_watermark,contract_version,calculated_at,owner_id,account_id,instance_id,
                 scope_json,cohort_json,report_json,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (frozen["run_id"], frozen["cache_key"], frozen["input_watermark"], frozen["contract_version"],
                 frozen["calculated_at"], *(scope.get(field, cohort.get(field)) for field in
                    ("owner_id", "account_id", "instance_id")), evidence_json(scope), evidence_json(cohort), encoded, _now()))
        return True

    def get_latest_intelligence_run(self, cache_key: str, *, filters: dict | None = None) -> dict | None:
        return self._get_run(cache_key, filters=filters)

    def get_intelligence_run(self, cache_key: str, input_watermark: str, *, filters: dict | None = None) -> dict | None:
        return self._get_run(cache_key, input_watermark=input_watermark, filters=filters)

    def _get_run(self, cache_key, *, input_watermark=None, filters=None):
        where, args = self._filters({"cache_key": cache_key, **(filters or {})},
            allowed={"cache_key", "contract_version", "owner_id", "account_id", "instance_id", "run_id"})
        if input_watermark is not None:
            where += " AND input_watermark=?"
            args.append(input_watermark)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT report_json FROM intelligence_calculation_runs" + where
                + " ORDER BY calculated_at DESC,rowid DESC LIMIT 1", args).fetchone()
            return json.loads(row[0]) if row else None

    def record_validation_run(self, report: dict) -> bool:
        """Persist one immutable research result; retries are logical no-ops."""
        frozen = dict(report)
        for field in ("run_id", "strategy_id", "dataset_identity", "execution_mode", "method", "status"):
            self._required_text(frozen, field)
        frozen.setdefault("created_at", _now())
        frozen.setdefault("completed_at", frozen.get("created_at"))
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT report_json FROM strategy_validation_runs WHERE run_id=?",
                                          (frozen["run_id"],)).fetchone()
            if row:
                prior_payload = json.loads(row[0])
                for field in ("created_at", "completed_at", "holdout_access_count"):
                    if field in prior_payload:
                        frozen[field] = prior_payload[field]
                encoded = evidence_json(frozen)
                if row[0] != encoded:
                    raise ValueError("immutable strategy validation run conflict")
                return False
            if frozen.get("method") in {"out_of_sample", "walk_forward"}:
                prior = self.journal._c.execute(
                    "SELECT COUNT(*) FROM strategy_validation_runs WHERE strategy_id=? AND strategy_version IS ? "
                    "AND config_fingerprint IS ? AND dataset_identity=? AND method=? AND owner_id IS ?",
                    (frozen["strategy_id"], frozen.get("strategy_version"), frozen.get("config_fingerprint"),
                     frozen["dataset_identity"], frozen["method"], frozen.get("owner_id"))).fetchone()[0]
                frozen["holdout_access_count"] = int(prior) + 1
            else:
                frozen.setdefault("holdout_access_count", 0)
            encoded = evidence_json(frozen)
            columns = ("run_id", "strategy_id", "strategy_version", "config_fingerprint", "dataset_identity",
                       "data_revision", "execution_mode", "method", "period_start", "period_end", "policy_version",
                       "cost_model_version", "classifier_version", "software_revision", "random_seed", "status",
                       "failure_reason", "holdout_access_count", "report_json", "created_at", "completed_at",
                       "owner_id", "account_id", "instance_id", "simulation_session_id")
            self.journal._c.execute(f"INSERT INTO strategy_validation_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(encoded if column == "report_json" else frozen.get(column)
                      for column in columns))
        return True

    def validation_runs(self, *, filters: dict | None = None, limit=100) -> list[dict]:
        filters = filters or {}
        allowed = {"run_id", "strategy_id", "strategy_version", "config_fingerprint", "dataset_identity",
                   "execution_mode", "method", "status", "owner_id", "account_id", "instance_id", "simulation_session_id"}
        clauses, args = [], []
        for key, value in filters.items():
            if key not in allowed:
                raise ValueError(f"unsupported validation filter: {key}")
            column = key
            if value is None:
                clauses.append(f"{column} IS NULL")
            else:
                clauses.append(f"{column}=?")
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT report_json FROM strategy_validation_runs" + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, _positive_limit(limit, 1000))).fetchall()
            return [json.loads(row[0]) for row in rows]

    def get_validation_run(self, run_id: str) -> dict | None:
        with self.journal._lock:
            row = self.journal._c.execute("SELECT report_json FROM strategy_validation_runs WHERE run_id=?", (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def create_research_job(self, job: dict) -> dict:
        frozen = dict(job)
        for field in ("job_id", "idempotency_key", "owner_id", "job_type", "strategy_id", "status"):
            self._required_text(frozen, field)
        frozen.setdefault("progress", 0)
        frozen.setdefault("requested_at", _now())
        frozen.setdefault("frozen_config", {})
        frozen.setdefault("request", {})
        frozen.setdefault("max_attempts", 3)
        frozen.setdefault("logical_result_key", "validation:" + frozen["job_id"])
        with self.journal.transaction():
            existing = self.journal._c.execute(
                "SELECT request_json FROM research_jobs WHERE owner_id=? AND idempotency_key=?",
                (frozen["owner_id"], frozen["idempotency_key"])).fetchone()
            if existing:
                return json.loads(existing[0])
            self.journal._c.execute("""INSERT INTO research_jobs
                (job_id,idempotency_key,owner_id,account_id,instance_id,simulation_session_id,
                 job_type,strategy_id,strategy_version,config_fingerprint,frozen_config_json,request_json,
                 status,progress,requested_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (frozen["job_id"], frozen["idempotency_key"], frozen["owner_id"], frozen.get("account_id"),
                 frozen.get("instance_id"), frozen.get("simulation_session_id"), frozen["job_type"],
                 frozen["strategy_id"], frozen.get("strategy_version"), frozen.get("config_fingerprint"),
                 evidence_json(frozen.get("frozen_config", {})), evidence_json(frozen),
                 frozen["status"], int(frozen.get("progress", 0)), frozen["requested_at"]))
            self.journal._c.execute("UPDATE research_jobs SET max_attempts=?,logical_result_key=? WHERE job_id=?",
                                    (int(frozen["max_attempts"]), frozen["logical_result_key"], frozen["job_id"]))
        return frozen

    def get_research_job(self, job_id: str, *, owner_id: str | None = None) -> dict | None:
        clauses, args = ["job_id=?"], [job_id]
        if owner_id is not None:
            clauses.append("owner_id=?"); args.append(owner_id)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT * FROM research_jobs WHERE " + " AND ".join(clauses), args).fetchone()
            if not row:
                return None
            result = dict(row)
            result["frozen_config"] = json.loads(result.pop("frozen_config_json"))
            raw_request = json.loads(result.pop("request_json"))
            result["request"] = raw_request.get("request", raw_request)
            if result.get("result_json"):
                result["result"] = json.loads(result.pop("result_json"))
            else:
                result.pop("result_json", None)
            return result

    def research_jobs(self, *, filters: dict | None = None, limit=100) -> list[dict]:
        filters = filters or {}
        allowed = {"owner_id", "account_id", "instance_id", "simulation_session_id", "status", "job_type", "strategy_id"}
        clauses, args = [], []
        for key, value in filters.items():
            if key not in allowed:
                raise ValueError(f"unsupported research job filter: {key}")
            clauses.append(f"{key}=?"); args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.journal._lock:
            ids = self.journal._c.execute("SELECT job_id FROM research_jobs" + where
                + " ORDER BY requested_at DESC LIMIT ?", (*args, _positive_limit(limit, 1000))).fetchall()
        return [self.get_research_job(row[0]) for row in ids]

    def update_research_job(self, job_id: str, *, owner_id: str, **changes) -> dict:
        allowed = {"status", "progress", "error_class", "error_message", "result", "started_at", "completed_at", "lease_until"}
        if set(changes) - allowed:
            raise ValueError("unsupported research job update")
        assignments, args = [], []
        for key, value in changes.items():
            column = "result_json" if key == "result" else key
            assignments.append(column + "=?")
            args.append(evidence_json(value) if key == "result" else value)
        if not assignments:
            return self.get_research_job(job_id, owner_id=owner_id)
        args.extend((job_id, owner_id))
        with self.journal.transaction():
            self.journal._c.execute("UPDATE research_jobs SET " + ",".join(assignments) + " WHERE job_id=? AND owner_id=?", args)
        return self.get_research_job(job_id, owner_id=owner_id)

    # ------------------------------------------------------- Sprint 4.6 queue
    JOB_TRANSITIONS = {
        "QUEUED": {"CLAIMED", "CANCELLED"},
        "CLAIMED": {"RUNNING", "CANCEL_REQUESTED", "INTERRUPTED", "FAILED"},
        "RUNNING": {"COMPLETED", "FAILED", "CANCEL_REQUESTED", "INTERRUPTED"},
        "CANCEL_REQUESTED": {"CANCELLED"},
        "INTERRUPTED": {"RETRY_PENDING", "FAILED"},
        "RETRY_PENDING": {"CLAIMED", "CANCELLED"},
        "FAILED": set(), "COMPLETED": set(), "CANCELLED": set(),
    }

    @staticmethod
    def _job_stamp(value=None) -> str:
        return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    @staticmethod
    def _job_dt(value: str | None):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _job_event_locked(self, job_id, from_status, to_status, *, attempt=0,
                          worker_id=None, reason=None, payload=None):
        self.journal._c.execute(
            """INSERT INTO research_job_events
               (event_id,job_id,from_status,to_status,attempt_number,worker_id,reason,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            ("research-event:" + uuid.uuid4().hex, job_id, from_status, to_status,
             int(attempt or 0), worker_id, reason, evidence_json(payload or {}), _now()))

    def _transition_job_locked(self, row, to_status, *, worker_id=None, lease_token=None,
                               reason=None, changes=None, force=False):
        current = row["status"]
        if not force and to_status not in self.JOB_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid research job transition {current}->{to_status}")
        if lease_token is not None and row["lease_token"] != lease_token:
            raise ValueError("research job lease fencing token mismatch")
        changes = dict(changes or {})
        changes["status"] = to_status
        assignments, args = [], []
        for key, value in changes.items():
            column = "result_json" if key == "result" else key
            assignments.append(column + "=?")
            args.append(evidence_json(value) if key == "result" else value)
        args.append(row["job_id"])
        self.journal._c.execute("UPDATE research_jobs SET " + ",".join(assignments) + " WHERE job_id=?", args)
        self._job_event_locked(row["job_id"], current, to_status,
                               attempt=row["attempt_number"], worker_id=worker_id,
                               reason=reason, payload=changes)

    def claim_research_job(self, *, worker_id: str, lease_seconds: int = 60,
                           max_active: int = 1) -> dict | None:
        """Atomically claim one eligible job on the journal's SQLite writer lock."""
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        now = datetime.now(timezone.utc)
        stamp = self._job_stamp(now)
        expiry = self._job_stamp(now + timedelta(seconds=max(5, int(lease_seconds))))
        with self.journal.transaction():
            active = self.journal._c.execute(
                """SELECT COUNT(*) FROM research_jobs
                   WHERE status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED')
                     AND (lease_expires_at IS NULL OR lease_expires_at>?)""", (stamp,)
            ).fetchone()[0]
            if active >= max(1, int(max_active)):
                return None
            row = self.journal._c.execute(
                """SELECT * FROM research_jobs
                   WHERE (status IN ('QUEUED','RETRY_PENDING') AND attempt_number<max_attempts AND (retry_at IS NULL OR retry_at<=?))
                      OR (status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?)
                   ORDER BY requested_at ASC LIMIT 1""", (stamp, stamp)).fetchone()
            if not row:
                return None
            current = dict(row)
            old_status = current["status"]
            if old_status in {"CLAIMED", "RUNNING", "CANCEL_REQUESTED"}:
                self._transition_job_locked(current, "INTERRUPTED", reason="lease_expired", force=True)
                current["status"] = "INTERRUPTED"
                old_status = "INTERRUPTED"
            if old_status == "INTERRUPTED":
                self._transition_job_locked(current, "RETRY_PENDING", reason="recovered_after_lease_expiry")
                current["status"] = "RETRY_PENDING"
            attempt = int(current.get("attempt_number") or 0) + 1
            token = uuid.uuid4().hex
            attempt_id = "research-attempt:" + uuid.uuid4().hex
            self._transition_job_locked(current, "CLAIMED", worker_id=worker_id,
                                        reason="worker_claimed", changes={
                                            "attempt_number": attempt, "worker_id": worker_id,
                                            "lease_token": token, "heartbeat_at": stamp,
                                            "lease_expires_at": expiry, "retry_at": None,
                                            "cancel_requested_at": None,
                                        })
            self.journal._c.execute(
                """INSERT INTO research_job_attempts
                   (attempt_id,job_id,attempt_number,worker_id,lease_token,status,claimed_at,heartbeat_at,lease_expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (attempt_id, current["job_id"], attempt, worker_id, token, "CLAIMED", stamp, stamp, expiry))
            result = dict(current)
            result.update(status="CLAIMED", attempt_number=attempt, worker_id=worker_id,
                          lease_token=token, heartbeat_at=stamp, lease_expires_at=expiry)
            return result

    def transition_research_job(self, job_id: str, *, to_status: str, worker_id: str | None = None,
                                lease_token: str | None = None, owner_id: str | None = None,
                                reason: str | None = None, changes: dict | None = None) -> dict:
        with self.journal.transaction():
            clauses = ["job_id=?"]; args = [job_id]
            if owner_id is not None:
                clauses.append("owner_id=?"); args.append(owner_id)
            row = self.journal._c.execute("SELECT * FROM research_jobs WHERE " + " AND ".join(clauses), args).fetchone()
            if not row:
                raise KeyError(job_id)
            current = dict(row)
            self._transition_job_locked(current, to_status, worker_id=worker_id,
                                        lease_token=lease_token, reason=reason, changes=changes)
        return self.get_research_job(job_id, owner_id=owner_id)

    def heartbeat_research_job(self, job_id: str, *, worker_id: str, lease_token: str,
                               lease_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc); stamp = self._job_stamp(now)
        expiry = self._job_stamp(now + timedelta(seconds=max(5, int(lease_seconds))))
        with self.journal.transaction():
            changed = self.journal._c.execute(
                """UPDATE research_jobs SET heartbeat_at=?,lease_expires_at=?
                   WHERE job_id=? AND worker_id=? AND lease_token=?
                     AND status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED')""",
                (stamp, expiry, job_id, worker_id, lease_token)).rowcount
            if changed:
                self.journal._c.execute(
                    "UPDATE research_job_attempts SET heartbeat_at=?,lease_expires_at=? WHERE job_id=? AND lease_token=?",
                    (stamp, expiry, job_id, lease_token))
        return bool(changed)

    def commit_research_result(self, job_id: str, *, worker_id: str, lease_token: str,
                               report: dict, validation: dict) -> dict:
        """Commit validation evidence and terminal job state under one lock.

        The fencing check happens before the immutable insert, so a cancelled
        or superseded worker cannot leave evidence that later appears as a
        successful qualification input.
        """
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT * FROM research_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            current = dict(row)
            if current.get("worker_id") != worker_id or current.get("lease_token") != lease_token:
                raise ValueError("research job lease fencing token mismatch")
            if current["status"] in {"CANCEL_REQUESTED", "CANCELLED", "FAILED", "INTERRUPTED", "COMPLETED"}:
                if current["status"] == "COMPLETED" and current.get("result_json"):
                    return self.get_research_job(job_id)
                raise ValueError("research job is no longer commit-eligible")
            self.record_validation_run(validation)
            self._transition_job_locked(current, "COMPLETED", worker_id=worker_id,
                                        lease_token=lease_token, reason="result_committed",
                                        changes={"progress": 100, "result": report,
                                                 "result_reference": validation["run_id"],
                                                 "completed_at": _now()})
            self.journal._c.execute("UPDATE research_job_attempts SET status='COMPLETED',completed_at=? WHERE job_id=? AND lease_token=?",
                                    (_now(), job_id, lease_token))
        return self.get_research_job(job_id)

    def request_research_cancellation(self, job_id: str, *, owner_id: str) -> dict:
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT * FROM research_jobs WHERE job_id=? AND owner_id=?", (job_id, owner_id)).fetchone()
            if not row:
                raise KeyError(job_id)
            current = dict(row); status = current["status"]
            stamp = _now()
            if status in {"QUEUED", "RETRY_PENDING"}:
                self._transition_job_locked(current, "CANCELLED", reason="cancelled_by_user",
                                            changes={"cancel_requested_at": stamp, "completed_at": stamp})
            elif status in {"CLAIMED", "RUNNING"}:
                self._transition_job_locked(current, "CANCEL_REQUESTED", worker_id=current.get("worker_id"),
                                            lease_token=current.get("lease_token"), reason="cancel_requested",
                                            changes={"cancel_requested_at": stamp})
            elif status == "CANCEL_REQUESTED":
                pass
        return self.get_research_job(job_id, owner_id=owner_id)

    def recover_expired_research_jobs(self, *, max_attempts: int = 3) -> int:
        stamp = _now(); count = 0
        with self.journal.transaction():
            rows = self.journal._c.execute(
                "SELECT * FROM research_jobs WHERE status IN ('CLAIMED','RUNNING','CANCEL_REQUESTED') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?", (stamp,)).fetchall()
            for row in rows:
                current = dict(row)
                self._transition_job_locked(current, "INTERRUPTED", reason="lease_expired", force=True)
                current["status"] = "INTERRUPTED"
                self.journal._c.execute("UPDATE research_job_attempts SET status='INTERRUPTED',completed_at=? WHERE job_id=? AND lease_token=?", (stamp, current["job_id"], current.get("lease_token")))
                if int(current.get("attempt_number") or 0) < max_attempts:
                    self._transition_job_locked(current, "RETRY_PENDING", reason="recovery_retry")
                    self.journal._c.execute("UPDATE research_jobs SET retry_at=? WHERE job_id=?", (stamp, current["job_id"]))
                else:
                    self._transition_job_locked(current, "FAILED", reason="retry_exhausted", changes={"error_class": "WORKER_INTERRUPTED", "completed_at": stamp})
                count += 1
        return count

    # ------------------------------------------------- Sprint 5 comparisons
    def create_research_candidate(self, candidate: dict) -> dict:
        frozen = dict(candidate)
        for field in ("candidate_id", "owner_id", "strategy_id", "dataset_identity", "execution_mode"):
            self._required_text(frozen, field)
        frozen.setdefault("frozen_config", {})
        frozen.setdefault("validation_run_ids", [])
        frozen.setdefault("scope", {})
        frozen.setdefault("evidence_quality", "UNKNOWN")
        frozen.setdefault("created_at", _now())
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT payload_json FROM strategy_research_candidates WHERE candidate_id=?", (frozen["candidate_id"],)).fetchone()
            if row:
                if row[0] != encoded:
                    raise ValueError("immutable research candidate conflict")
                return json.loads(row[0])
            self.journal._c.execute("""INSERT INTO strategy_research_candidates
              (candidate_id,owner_id,strategy_id,strategy_version,config_fingerprint,frozen_config_json,
               dataset_identity,data_revision,symbol,asset_portfolio_json,venue,market_type,timeframe,
               execution_mode,cost_model_version,classifier_version,software_revision,policy_version,
               validation_run_ids_json,evidence_quality,scope_json,created_at,payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["candidate_id"], frozen["owner_id"], frozen["strategy_id"], frozen.get("strategy_version"),
               frozen.get("config_fingerprint"), evidence_json(frozen["frozen_config"]), frozen["dataset_identity"],
               frozen.get("data_revision"), frozen.get("symbol"), evidence_json(frozen.get("asset_portfolio")) if frozen.get("asset_portfolio") is not None else None,
               frozen.get("venue"), frozen.get("market_type"), frozen.get("timeframe"), frozen["execution_mode"],
               frozen.get("cost_model_version"), frozen.get("classifier_version"), frozen.get("software_revision"),
               frozen.get("policy_version"), evidence_json(frozen["validation_run_ids"]), frozen["evidence_quality"],
               evidence_json(frozen["scope"]), frozen["created_at"], encoded))
        return frozen

    def get_research_candidate(self, candidate_id: str, *, owner_id: str | None = None) -> dict | None:
        clauses, args = ["candidate_id=?"], [candidate_id]
        if owner_id is not None:
            clauses.append("owner_id=?"); args.append(owner_id)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT * FROM strategy_research_candidates WHERE " + " AND ".join(clauses), args).fetchone()
            if not row:
                return None
            result = dict(row)
            result["frozen_config"] = json.loads(result.pop("frozen_config_json"))
            result["validation_run_ids"] = json.loads(result.pop("validation_run_ids_json"))
            result["scope"] = json.loads(result.pop("scope_json"))
            if result.get("asset_portfolio_json"):
                result["asset_portfolio"] = json.loads(result.pop("asset_portfolio_json"))
            else:
                result.pop("asset_portfolio_json", None); result["asset_portfolio"] = None
            if "_payload" in result["scope"]:
                result["scope"].pop("_payload", None)
            return result

    def research_candidates(self, *, owner_id: str, limit: int = 100) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT candidate_id FROM strategy_research_candidates WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                                           (owner_id, _positive_limit(limit, 1000))).fetchall()
        return [self.get_research_candidate(row[0], owner_id=owner_id) for row in rows]

    def designate_research_champion(self, designation: dict) -> dict:
        frozen = dict(designation)
        for field in ("designation_id", "owner_id", "scope_key", "candidate_id", "selection_policy_version", "reviewer_id", "reason"):
            self._required_text(frozen, field)
        frozen.setdefault("review_status", "PENDING")
        frozen.setdefault("selected_at", _now())
        frozen.setdefault("evidence_reference", None)
        with self.journal.transaction():
            if not self.journal._c.execute("SELECT 1 FROM strategy_research_candidates WHERE candidate_id=? AND owner_id=?",
                                           (frozen["candidate_id"], frozen["owner_id"])).fetchone():
                raise ValueError("candidate is not owned by the reviewer")
            self.journal._c.execute("""INSERT INTO research_champion_registry
              (designation_id,owner_id,scope_key,candidate_id,selection_policy_version,reviewer_id,
               review_status,reason,evidence_reference,selected_at,payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["designation_id"], frozen["owner_id"], frozen["scope_key"], frozen["candidate_id"],
               frozen["selection_policy_version"], frozen["reviewer_id"], frozen["review_status"], frozen["reason"],
               frozen.get("evidence_reference"), frozen["selected_at"], evidence_json(frozen)))
        return frozen

    def research_champion(self, *, owner_id: str, scope_key: str) -> dict | None:
        with self.journal._lock:
            row = self.journal._c.execute("SELECT payload_json FROM research_champion_registry WHERE owner_id=? AND scope_key=? ORDER BY selected_at DESC LIMIT 1",
                                          (owner_id, scope_key)).fetchone()
            return json.loads(row[0]) if row else None

    def record_strategy_comparison(self, comparison: dict) -> dict:
        frozen = dict(comparison)
        for field in ("comparison_id", "idempotency_key", "owner_id", "champion_candidate_id", "challenger_candidate_id", "policy_version", "status", "recommendation"):
            self._required_text(frozen, field)
        frozen.setdefault("scope", {}); frozen.setdefault("statistical_method", "UNKNOWN")
        frozen.setdefault("blocking_reasons", []); frozen.setdefault("validation_run_ids", [])
        frozen.setdefault("report", {}); frozen.setdefault("created_at", _now()); frozen.setdefault("completed_at", frozen["created_at"])
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            existing = self.journal._c.execute("SELECT payload_json FROM strategy_comparisons WHERE owner_id=? AND idempotency_key=?",
                                               (frozen["owner_id"], frozen["idempotency_key"])).fetchone()
            if existing:
                prior = json.loads(existing[0])
                def stable(value):
                    value = dict(value)
                    for key in ("comparison_id", "created_at", "completed_at"):
                        value.pop(key, None)
                    report = dict(value.get("report") or {})
                    report.pop("calculated_at", None)
                    value["report"] = report
                    return evidence_json(value)
                if stable(prior) != stable(frozen):
                    raise ValueError("comparison idempotency conflict")
                return prior
            self.journal._c.execute("""INSERT INTO strategy_comparisons
              (comparison_id,idempotency_key,owner_id,champion_candidate_id,challenger_candidate_id,scope_json,
               policy_version,statistical_method,random_seed,status,recommendation,blocking_reasons_json,report_json,
               validation_run_ids_json,created_at,completed_at,payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["comparison_id"], frozen["idempotency_key"], frozen["owner_id"], frozen["champion_candidate_id"],
               frozen["challenger_candidate_id"], evidence_json(frozen["scope"]), frozen["policy_version"],
               frozen["statistical_method"], frozen.get("random_seed"), frozen["status"], frozen["recommendation"],
               evidence_json(frozen["blocking_reasons"]), evidence_json(frozen["report"]), evidence_json(frozen["validation_run_ids"]),
               frozen["created_at"], frozen.get("completed_at"), encoded))
        return frozen

    def get_strategy_comparison(self, comparison_id: str, *, owner_id: str | None = None) -> dict | None:
        clauses, args = ["comparison_id=?"], [comparison_id]
        if owner_id is not None:
            clauses.append("owner_id=?"); args.append(owner_id)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT * FROM strategy_comparisons WHERE " + " AND ".join(clauses), args).fetchone()
            if not row: return None
            result = dict(row)
            for column, key in (("scope_json", "scope"), ("blocking_reasons_json", "blocking_reasons"), ("report_json", "report"), ("validation_run_ids_json", "validation_run_ids")):
                result[key] = json.loads(result.pop(column))
            return result

    def strategy_comparisons(self, *, owner_id: str, limit: int = 100) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT comparison_id FROM strategy_comparisons WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                                           (owner_id, _positive_limit(limit, 1000))).fetchall()
        return [self.get_strategy_comparison(row[0], owner_id=owner_id) for row in rows]

    # ------------------------------------------ Sprint 5.5 matched research
    EXPERIMENT_TRANSITIONS = {
        "DRAFT": {"VALIDATING", "QUEUED", "BLOCKED", "CANCEL_REQUESTED"},
        "VALIDATING": {"QUEUED", "BLOCKED", "FAILED", "CANCEL_REQUESTED"},
        "QUEUED": {"RUNNING", "CANCEL_REQUESTED", "BLOCKED", "FAILED"},
        "RUNNING": {"PARTIALLY_COMPLETED", "COMPLETED", "FAILED", "CANCEL_REQUESTED"},
        "PARTIALLY_COMPLETED": {"RUNNING", "COMPLETED", "FAILED", "CANCEL_REQUESTED"},
        "CANCEL_REQUESTED": {"CANCELLED", "PARTIALLY_COMPLETED", "FAILED"},
        "COMPLETED": set(), "FAILED": set(), "CANCELLED": set(), "BLOCKED": set(),
    }

    def create_research_campaign(self, campaign: dict) -> dict:
        frozen = dict(campaign)
        for field in ("campaign_id", "idempotency_key", "owner_id", "status"):
            self._required_text(frozen, field)
        frozen.setdefault("selection_criteria", {}); frozen.setdefault("policy_versions", {})
        frozen.setdefault("created_at", _now()); frozen.setdefault("candidate_count", 0)
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            existing = self.journal._c.execute("SELECT payload_json FROM research_campaigns WHERE owner_id=? AND idempotency_key=?",
                                               (frozen["owner_id"], frozen["idempotency_key"])).fetchone()
            if existing:
                prior = json.loads(existing[0])
                if evidence_json({k: v for k, v in prior.items() if k not in {"campaign_id", "created_at", "completed_at"}}) != evidence_json({k: v for k, v in frozen.items() if k not in {"campaign_id", "created_at", "completed_at"}}):
                    raise ValueError("campaign idempotency conflict")
                return prior
            self.journal._c.execute("""INSERT INTO research_campaigns
              (campaign_id,idempotency_key,owner_id,strategy_family,selection_criteria_json,policy_versions_json,
               status,candidate_count,comparison_attempts,payload_json,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["campaign_id"], frozen["idempotency_key"], frozen["owner_id"], frozen.get("strategy_family"),
               evidence_json(frozen["selection_criteria"]), evidence_json(frozen["policy_versions"]), frozen["status"],
               int(frozen["candidate_count"]), int(frozen.get("comparison_attempts", 0)), encoded, frozen["created_at"]))
        return frozen

    def get_research_campaign(self, campaign_id: str, *, owner_id: str | None = None) -> dict | None:
        clauses, args = ["campaign_id=?"], [campaign_id]
        if owner_id is not None: clauses.append("owner_id=?"); args.append(owner_id)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT * FROM research_campaigns WHERE " + " AND ".join(clauses), args).fetchone()
            if not row: return None
            result = dict(row); payload = json.loads(result.pop("payload_json"))
            result["selection_criteria"] = json.loads(result.pop("selection_criteria_json"))
            result["policy_versions"] = json.loads(result.pop("policy_versions_json"))
            result.update({key: value for key, value in payload.items() if key not in result})
            return result

    def research_campaigns(self, *, owner_id: str, limit=100) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT campaign_id FROM research_campaigns WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                                           (owner_id, _positive_limit(limit, 1000))).fetchall()
        return [self.get_research_campaign(row[0], owner_id=owner_id) for row in rows]

    def create_matched_experiment(self, experiment: dict, candidates: list[dict]) -> dict:
        frozen = dict(experiment)
        for field in ("experiment_id", "idempotency_key", "owner_id", "champion_candidate_id", "status"):
            self._required_text(frozen, field)
        frozen.setdefault("challenger_candidate_ids", []); frozen.setdefault("scope", {})
        frozen.setdefault("frozen_spec", {}); frozen.setdefault("candidate_job_ids", {})
        frozen.setdefault("validation_run_ids", {}); frozen.setdefault("holdout_status", "UNKNOWN")
        frozen.setdefault("recommendation", "INSUFFICIENT_EVIDENCE"); frozen.setdefault("warnings", [])
        frozen.setdefault("report", {}); frozen.setdefault("created_at", _now())
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            existing = self.journal._c.execute("SELECT payload_json FROM matched_experiments WHERE owner_id=? AND idempotency_key=?",
                                               (frozen["owner_id"], frozen["idempotency_key"])).fetchone()
            if existing:
                return json.loads(existing[0])
            self.journal._c.execute("""INSERT INTO matched_experiments
              (experiment_id,idempotency_key,owner_id,campaign_id,champion_candidate_id,challenger_candidate_ids_json,
               scope_json,frozen_spec_json,candidate_job_ids_json,validation_run_ids_json,holdout_status,status,
               recommendation,warnings_json,report_json,created_at,payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["experiment_id"], frozen["idempotency_key"], frozen["owner_id"], frozen.get("campaign_id"),
               frozen["champion_candidate_id"], evidence_json(frozen["challenger_candidate_ids"]), evidence_json(frozen["scope"]),
               evidence_json(frozen["frozen_spec"]), evidence_json(frozen["candidate_job_ids"]), evidence_json(frozen["validation_run_ids"]),
               frozen["holdout_status"], frozen["status"], frozen["recommendation"], evidence_json(frozen["warnings"]),
               evidence_json(frozen["report"]), frozen["created_at"], encoded))
            for candidate in candidates:
                self.journal._c.execute("""INSERT INTO matched_experiment_candidates
                  (experiment_id,candidate_id,role,status,updated_at) VALUES (?,?,?,?,?)""",
                  (frozen["experiment_id"], candidate["candidate_id"],
                   "CHAMPION" if candidate["candidate_id"] == frozen["champion_candidate_id"] else "CHALLENGER",
                   "PENDING", frozen["created_at"]))
            self._experiment_event_locked(frozen["experiment_id"], None, frozen["status"], "created", frozen)
        return frozen

    def _experiment_event_locked(self, experiment_id, from_status, to_status, reason, payload=None):
        self.journal._c.execute("""INSERT INTO matched_experiment_events
          (event_id,experiment_id,from_status,to_status,reason,payload_json,created_at)
          VALUES (?,?,?,?,?,?,?)""",
          ("experiment-event:" + uuid.uuid4().hex, experiment_id, from_status, to_status, reason,
           evidence_json(payload or {}), _now()))

    def get_matched_experiment(self, experiment_id: str, *, owner_id: str | None = None) -> dict | None:
        clauses, args = ["experiment_id=?"], [experiment_id]
        if owner_id is not None: clauses.append("owner_id=?"); args.append(owner_id)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT * FROM matched_experiments WHERE " + " AND ".join(clauses), args).fetchone()
            if not row: return None
            result = dict(row); payload = json.loads(result.pop("payload_json"))
            for column, key in (("challenger_candidate_ids_json", "challenger_candidate_ids"), ("scope_json", "scope"), ("frozen_spec_json", "frozen_spec"), ("candidate_job_ids_json", "candidate_job_ids"), ("validation_run_ids_json", "validation_run_ids"), ("warnings_json", "warnings"), ("report_json", "report")):
                result[key] = json.loads(result.pop(column))
            result.update({key: value for key, value in payload.items() if key not in result})
            result["candidates"] = [dict(item) for item in self.journal._c.execute("SELECT * FROM matched_experiment_candidates WHERE experiment_id=?", (experiment_id,)).fetchall()]
            return result

    def matched_experiments(self, *, owner_id: str, limit=100) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT experiment_id FROM matched_experiments WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                                           (owner_id, _positive_limit(limit, 1000))).fetchall()
        return [self.get_matched_experiment(row[0], owner_id=owner_id) for row in rows]

    def update_matched_experiment(self, experiment_id: str, *, owner_id: str, to_status: str | None = None, reason: str = "update", **changes) -> dict:
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT * FROM matched_experiments WHERE experiment_id=? AND owner_id=?", (experiment_id, owner_id)).fetchone()
            if not row: raise KeyError(experiment_id)
            current = dict(row); old = current["status"]
            if to_status and to_status != old and to_status not in self.EXPERIMENT_TRANSITIONS.get(old, set()):
                raise ValueError(f"invalid experiment transition {old}->{to_status}")
            if to_status: changes["status"] = to_status
            assignments, args = [], []
            mapping = {"challenger_candidate_ids": "challenger_candidate_ids_json", "scope": "scope_json", "frozen_spec": "frozen_spec_json", "candidate_job_ids": "candidate_job_ids_json", "validation_run_ids": "validation_run_ids_json", "warnings": "warnings_json", "report": "report_json"}
            for key, value in changes.items():
                column = mapping.get(key, key); assignments.append(column + "=?"); args.append(evidence_json(value) if key in mapping else value)
            if assignments:
                args.append(experiment_id); self.journal._c.execute("UPDATE matched_experiments SET " + ",".join(assignments) + " WHERE experiment_id=?", args)
            if to_status and to_status != old: self._experiment_event_locked(experiment_id, old, to_status, reason, changes)
        return self.get_matched_experiment(experiment_id, owner_id=owner_id)

    def update_experiment_candidate(self, experiment_id: str, candidate_id: str, **changes) -> None:
        allowed = {"job_id", "status", "validation_run_id", "error_message"}
        if set(changes) - allowed: raise ValueError("unsupported experiment candidate update")
        columns = []; args = []
        for key, value in changes.items(): columns.append(key + "=?"); args.append(value)
        columns.append("updated_at=?"); args.append(_now()); args.extend((experiment_id, candidate_id))
        with self.journal.transaction():
            self.journal._c.execute("UPDATE matched_experiment_candidates SET " + ",".join(columns) + " WHERE experiment_id=? AND candidate_id=?", args)

    def record_holdout_exposure(self, exposure: dict) -> dict:
        frozen = dict(exposure)
        for field in ("exposure_id", "owner_id", "experiment_id", "dataset_identity", "exposure_kind", "selection_influence"):
            self._required_text(frozen, field)
        frozen.setdefault("created_at", _now())
        with self.journal.transaction():
            prior = self.journal._c.execute(
                "SELECT COUNT(*) FROM holdout_exposure_events WHERE owner_id=? AND dataset_identity=? "
                "AND (period_start IS NULL OR period_end IS NULL OR ? IS NULL OR ? IS NULL OR "
                "(period_start < ? AND period_end > ?))",
                (frozen["owner_id"], frozen["dataset_identity"], frozen.get("period_end"), frozen.get("period_start"),
                 frozen.get("period_end"), frozen.get("period_start"))).fetchone()[0]
            frozen["holdout_status"] = "UNTOUCHED" if prior == 0 else "REUSED" if prior > 1 else "EXPOSED"
            self.journal._c.execute("""INSERT INTO holdout_exposure_events
              (exposure_id,owner_id,campaign_id,experiment_id,dataset_identity,period_start,period_end,candidate_id,
               exposure_kind,selection_influence,created_at,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["exposure_id"], frozen["owner_id"], frozen.get("campaign_id"), frozen["experiment_id"], frozen["dataset_identity"],
               frozen.get("period_start"), frozen.get("period_end"), frozen.get("candidate_id"), frozen["exposure_kind"],
               frozen["selection_influence"], frozen["created_at"], evidence_json(frozen)))
        return frozen

    def holdout_exposures(self, *, owner_id: str, dataset_identity: str, period_start=None, period_end=None) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT payload_json FROM holdout_exposure_events WHERE owner_id=? AND dataset_identity=? AND period_start IS ? AND period_end IS ? ORDER BY created_at",
                                           (owner_id, dataset_identity, period_start, period_end)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def overlapping_holdout_exposures(self, *, owner_id: str, dataset_identity: str,
                                      period_start=None, period_end=None) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute(
                "SELECT payload_json FROM holdout_exposure_events WHERE owner_id=? AND dataset_identity=? "
                "AND (period_start IS NULL OR period_end IS NULL OR ? IS NULL OR ? IS NULL OR "
                "(period_start < ? AND period_end > ?)) ORDER BY created_at",
                (owner_id, dataset_identity, period_end, period_start, period_end, period_start)).fetchall()
        return [json.loads(row[0]) for row in rows]

    # ------------------------------------------------ research-agent metadata
    def create_agent_hypothesis(self, hypothesis: dict) -> dict:
        frozen = dict(hypothesis)
        for field in ("hypothesis_id", "owner_id", "strategy_id", "question", "status"):
            self._required_text(frozen, field)
        frozen.setdefault("created_at", _now()); frozen.setdefault("updated_at", frozen["created_at"])
        frozen.setdefault("evidence_refs", []); frozen.setdefault("uncertainties", [])
        frozen.setdefault("methodology", {}); frozen.setdefault("dataset_scope", {})
        frozen.setdefault("cost_assumptions", {}); frozen.setdefault("plan", {})
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT payload_json FROM research_agent_hypotheses WHERE hypothesis_id=? AND owner_id=?",
                                          (frozen["hypothesis_id"], frozen["owner_id"])).fetchone()
            if row:
                if row[0] != encoded: raise ValueError("immutable research hypothesis conflict")
                return json.loads(row[0])
            self.journal._c.execute("""INSERT INTO research_agent_hypotheses
              (hypothesis_id,campaign_id,owner_id,strategy_id,strategy_version,baseline_fingerprint,proposed_fingerprint,
               question,expected_effect,evidence_refs_json,uncertainties_json,methodology_json,dataset_scope_json,
               cost_assumptions_json,agent_version,status,plan_json,created_at,updated_at,payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (frozen["hypothesis_id"], frozen.get("campaign_id"), frozen["owner_id"], frozen["strategy_id"],
               frozen.get("strategy_version"), frozen.get("baseline_fingerprint"), frozen.get("proposed_fingerprint"),
               frozen["question"], frozen.get("expected_effect"), evidence_json(frozen["evidence_refs"]),
               evidence_json(frozen["uncertainties"]), evidence_json(frozen["methodology"]), evidence_json(frozen["dataset_scope"]),
               evidence_json(frozen["cost_assumptions"]), frozen.get("agent_version"), frozen["status"], evidence_json(frozen["plan"]),
               frozen["created_at"], frozen["updated_at"], encoded))
        return frozen

    def get_agent_hypothesis(self, hypothesis_id: str, *, owner_id: str) -> dict | None:
        with self.journal._lock:
            row = self.journal._c.execute("SELECT payload_json FROM research_agent_hypotheses WHERE hypothesis_id=? AND owner_id=?",
                                          (hypothesis_id, owner_id)).fetchone()
        return json.loads(row[0]) if row else None

    def agent_hypotheses(self, *, owner_id: str, limit=100) -> list[dict]:
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT payload_json FROM research_agent_hypotheses WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                                           (owner_id, _positive_limit(limit, 1000))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_agent_hypothesis(self, hypothesis_id: str, *, owner_id: str, **changes) -> dict:
        allowed = {"status", "plan", "updated_at"}
        if set(changes) - allowed: raise ValueError("unsupported hypothesis update")
        current = self.get_agent_hypothesis(hypothesis_id, owner_id=owner_id)
        if not current: raise KeyError(hypothesis_id)
        current.update(changes); current["updated_at"] = _now()
        encoded = evidence_json(current)
        with self.journal.transaction():
            self.journal._c.execute("UPDATE research_agent_hypotheses SET status=?,plan_json=?,updated_at=?,payload_json=? WHERE hypothesis_id=? AND owner_id=?",
                (current["status"], evidence_json(current.get("plan", {})), current["updated_at"], encoded, hypothesis_id, owner_id))
        return current

    def create_agent_approval(self, approval: dict) -> dict:
        frozen = dict(approval)
        for field in ("approval_id", "hypothesis_id", "owner_id", "plan_fingerprint", "reviewer_id", "decision"):
            self._required_text(frozen, field)
        frozen.setdefault("approved_at", _now()); frozen.setdefault("max_jobs", 1); frozen.setdefault("resource_budget", {})
        encoded = evidence_json(frozen)
        with self.journal.transaction():
            row = self.journal._c.execute("SELECT payload_json FROM research_agent_approvals WHERE owner_id=? AND hypothesis_id=? AND plan_fingerprint=?",
                                          (frozen["owner_id"], frozen["hypothesis_id"], frozen["plan_fingerprint"])).fetchone()
            if row:
                if row[0] != encoded: raise ValueError("immutable research approval conflict")
                return json.loads(row[0])
            self.journal._c.execute("""INSERT INTO research_agent_approvals
              (approval_id,hypothesis_id,owner_id,plan_fingerprint,reviewer_id,decision,approved_at,max_jobs,resource_budget_json,payload_json)
              VALUES (?,?,?,?,?,?,?,?,?,?)""", (frozen["approval_id"], frozen["hypothesis_id"], frozen["owner_id"], frozen["plan_fingerprint"],
                frozen["reviewer_id"], frozen["decision"], frozen["approved_at"], frozen["max_jobs"], evidence_json(frozen["resource_budget"]), encoded))
        return frozen

    def agent_approval(self, hypothesis_id: str, *, owner_id: str, plan_fingerprint: str | None = None) -> dict | None:
        clauses = ["hypothesis_id=?", "owner_id=?"]; args = [hypothesis_id, owner_id]
        if plan_fingerprint: clauses.append("plan_fingerprint=?"); args.append(plan_fingerprint)
        with self.journal._lock:
            row = self.journal._c.execute("SELECT payload_json FROM research_agent_approvals WHERE " + " AND ".join(clauses) + " ORDER BY approved_at DESC LIMIT 1", args).fetchone()
        return json.loads(row[0]) if row else None

    def record_agent_event(self, event: dict) -> dict:
        frozen = dict(event)
        for field in ("event_id", "owner_id", "event_type", "actor_id"):
            self._required_text(frozen, field)
        frozen.setdefault("created_at", _now()); frozen.setdefault("payload", {})
        with self.journal.transaction():
            self.journal._c.execute("INSERT INTO research_agent_events(event_id,owner_id,hypothesis_id,event_type,actor_id,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (frozen["event_id"], frozen["owner_id"], frozen.get("hypothesis_id"), frozen["event_type"], frozen["actor_id"], evidence_json(frozen["payload"]), frozen["created_at"]))
        return frozen

    def agent_events(self, *, owner_id: str, hypothesis_id: str | None = None, limit=200) -> list[dict]:
        clauses = ["owner_id=?"]; args = [owner_id]
        if hypothesis_id: clauses.append("hypothesis_id=?"); args.append(hypothesis_id)
        with self.journal._lock:
            rows = self.journal._c.execute("SELECT * FROM research_agent_events WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT ?", (*args, _positive_limit(limit, 1000))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try: item["payload"] = json.loads(item.pop("payload_json"))
            except (TypeError, ValueError): item["payload"] = {}
            result.append(item)
        return result
