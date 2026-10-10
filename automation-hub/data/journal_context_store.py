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
