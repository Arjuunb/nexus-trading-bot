"""Audited research governance; no strategy execution, code writes or deployment.

Artifact hashes and reported results are provenance claims, not certification.
Only a separate owner authority permits research or development. Actual causal
backtest runners and statistical methods must be independently validated before
their results can be considered proven; this registry never invents them.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import closing
from datetime import datetime, timezone

from .events import GuardianEvent, _safe_json
from .store import GuardianStore

STAGES = ("HISTORICAL_BACKTEST", "OUT_OF_SAMPLE", "WALK_FORWARD", "STRESS_TEST",
          "FORWARD_PAPER", "STATISTICAL_COMPARISON", "RECOMMENDATION")
_HASH = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _identity(value, pattern=_HASH) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("research requires a complete content hash or commit identity")
    return value


def _text(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2048:
        raise ValueError("research text is missing or oversized")
    return _safe_json(value.strip())


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("research period requires aware timestamps")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("research period requires aware timestamps")
    return parsed.astimezone(timezone.utc)


def _period(value: dict, now: datetime) -> dict:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ValueError("research period requires only start and end")
    start, end = _time(value["start"]), _time(value["end"])
    if not start < end <= now:
        raise ValueError("research result must describe a completed nonempty period")
    return {"start": start.isoformat(), "end": end.isoformat()}


class GuardianResearch:
    def __init__(self, store: GuardianStore):
        self.store = store
        with closing(store._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guardian_research_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guardian_research_results (
                    result_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL,
                    stage TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(hypothesis_id,stage)
                );
                CREATE TABLE IF NOT EXISTS guardian_research_reviews (
                    review_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL, decision TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(hypothesis_id,evidence_digest,decision)
                );
                CREATE TRIGGER IF NOT EXISTS guardian_research_identity_no_update
                  BEFORE UPDATE OF payload_json,hypothesis_id,created_at ON guardian_research_hypotheses
                  BEGIN SELECT RAISE(ABORT,'Guardian hypothesis evidence is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_research_no_delete
                  BEFORE DELETE ON guardian_research_hypotheses
                  BEGIN SELECT RAISE(ABORT,'Guardian hypothesis is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_research_results_no_update
                  BEFORE UPDATE ON guardian_research_results BEGIN SELECT RAISE(ABORT,'Guardian result is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_research_results_no_delete
                  BEFORE DELETE ON guardian_research_results BEGIN SELECT RAISE(ABORT,'Guardian result is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_research_reviews_no_update
                  BEFORE UPDATE ON guardian_research_reviews BEGIN SELECT RAISE(ABORT,'Guardian review is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_research_reviews_no_delete
                  BEFORE DELETE ON guardian_research_reviews BEGIN SELECT RAISE(ABORT,'Guardian review is immutable'); END;
            """)
            conn.commit()

    def _audit(self, conn, kind: str, identity: str, hypothesis: str, now: datetime):
        self.store._append_in_transaction(conn, GuardianEvent(
            source_service="guardian_research", source_component="research", event_type=kind,
            event_id="research_" + identity, timestamp=now,
            evidence={"hypothesis_id": hypothesis, "record_id": identity,
                      "production_changed": False, "live_routing_enabled": False}), now.isoformat())

    def create(self, payload: dict) -> dict:
        fields = {"strategy_id", "strategy_version", "code_commit", "config_hash",
                  "candidate_artifact_sha256", "hypothesis", "evidence_ids"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ValueError("invalid hypothesis fields")
        data = {key: _text(payload[key]) for key in ("strategy_id", "strategy_version", "hypothesis")}
        if len(data["strategy_id"]) > 128 or len(data["strategy_version"]) > 128:
            raise ValueError("strategy identity is oversized")
        data.update({key: _identity(payload[key], _COMMIT if key == "code_commit" else _HASH)
                     for key in ("code_commit", "config_hash", "candidate_artifact_sha256")})
        refs = payload["evidence_ids"]
        if (not isinstance(refs, list) or not 1 <= len(refs) <= 20 or
                any(not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", ref) for ref in refs)):
            raise ValueError("hypothesis requires bounded existing evidence IDs")
        data["evidence_ids"] = sorted(set(refs))
        identity, now = _hash(data), datetime.now(timezone.utc)
        with closing(self.store._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for ref in data["evidence_ids"]:
                    if not conn.execute("SELECT 1 FROM events WHERE event_id=?", (ref,)).fetchone():
                        raise ValueError("hypothesis evidence does not exist")
                if not conn.execute("SELECT 1 FROM guardian_research_hypotheses WHERE hypothesis_id=?",
                                    (identity,)).fetchone():
                    conn.execute("INSERT INTO guardian_research_hypotheses VALUES(?,?,?,?)", (
                        identity, now.isoformat(), "HYPOTHESIS_UNPROVEN", _json(data)))
                    self._audit(conn, "hypothesis_created", identity, identity, now)
                result = self._get(conn, identity)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _get(conn, identity: str) -> dict:
        row = conn.execute("SELECT * FROM guardian_research_hypotheses WHERE hypothesis_id=?",
                           (_identity(identity),)).fetchone()
        if row is None:
            raise ValueError("unknown research hypothesis")
        results = [json.loads(item["payload_json"]) for item in conn.execute(
            "SELECT payload_json FROM guardian_research_results WHERE hypothesis_id=? ORDER BY created_at,result_id",
            (identity,))]
        results.sort(key=lambda item: STAGES.index(item["stage"]))
        reviews = [json.loads(item["payload_json"]) for item in conn.execute(
            "SELECT payload_json FROM guardian_research_reviews WHERE hypothesis_id=? ORDER BY created_at,review_id",
            (identity,))]
        data = json.loads(row["payload_json"])
        return {"hypothesis_id": identity, "created_at": row["created_at"], "status": row["status"],
                **data, "results": results, "reviews": reviews,
                "evidence_digest": _hash({"hypothesis_id": identity,
                                          "results": [result["result_id"] for result in results]}),
                "methods_verified": False, "production_changed": False, "live_routing_enabled": False}

    def get(self, identity: str) -> dict:
        with closing(self.store._connect()) as conn:
            conn.execute("BEGIN")
            result = self._get(conn, identity)
            conn.commit()
            return result

    def record_result(self, identity: str, payload: dict) -> dict:
        fields = {"stage", "expected_digest", "candidate_artifact_sha256", "result_artifact_sha256",
                  "dataset_sha256", "passed", "period", "metrics", "folds", "paper_only"}
        if not isinstance(payload, dict) or set(payload) != fields or payload["stage"] not in STAGES:
            raise ValueError("invalid research result fields")
        now = datetime.now(timezone.utc)
        data = {key: _identity(payload[key]) for key in (
            "candidate_artifact_sha256", "result_artifact_sha256", "dataset_sha256")}
        if type(payload["passed"]) is not bool or payload["paper_only"] is not True:
            raise ValueError("research result requires an explicit paper-only result")
        data.update(stage=payload["stage"], passed=payload["passed"], paper_only=True,
                    period=_period(payload["period"], now))
        metrics = payload["metrics"]
        if (not isinstance(metrics, dict) or set(metrics) - {
                "sample_count", "wins", "losses", "expectancy_r", "profit_factor", "drawdown_r"} or
                type(metrics.get("sample_count")) is not int or not 1 <= metrics["sample_count"] <= 100000000):
            raise ValueError("research requires a finite, nonempty sample")
        for name, value in metrics.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("research metric must be finite")
            if name in ("wins", "losses") and (type(value) is not int or not 0 <= value <= metrics["sample_count"]):
                raise ValueError("invalid outcome count")
            if name in ("profit_factor", "drawdown_r") and value < 0:
                raise ValueError("invalid nonnegative metric")
        if metrics.get("wins", 0) + metrics.get("losses", 0) > metrics["sample_count"]:
            raise ValueError("outcome counts exceed the sample")
        data["metrics"] = _safe_json(metrics)
        folds = payload["folds"]
        if not isinstance(folds, list) or len(folds) > 100 or bool(folds) != (data["stage"] == "WALK_FORWARD"):
            raise ValueError("walk-forward requires explicit folds only in its own stage")
        data["folds"] = []
        for fold in folds:
            if not isinstance(fold, dict) or set(fold) != {"train", "test"}:
                raise ValueError("invalid walk-forward fold")
            train, test = _period(fold["train"], now), _period(fold["test"], now)
            if _time(train["end"]) >= _time(test["start"]):
                raise ValueError("walk-forward train/test periods must have a causal separation")
            if _time(test["end"]) > _time(data["period"]["end"]) or _time(train["start"]) < _time(data["period"]["start"]):
                raise ValueError("fold is outside its declared research period")
            if data["folds"] and _time(test["start"]) < _time(data["folds"][-1]["test"]["end"]):
                raise ValueError("walk-forward test folds overlap or are unordered")
            data["folds"].append({"train": train, "test": test})
        result_id = _hash({"hypothesis_id": identity, **data})
        with closing(self.store._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                hypothesis = self._get(conn, identity)
                existing = next((item for item in hypothesis["results"] if item["stage"] == data["stage"]), None)
                if existing:
                    if existing["result_id"] != result_id:
                        raise ValueError("research result conflicts with immutable prior evidence")
                    conn.commit()
                    return hypothesis
                if hypothesis["evidence_digest"] != _identity(payload["expected_digest"]):
                    raise ValueError("research evidence changed; refresh before recording")
                expected = STAGES[len(hypothesis["results"])] if len(hypothesis["results"]) < len(STAGES) else None
                if (hypothesis["status"] != f"{expected}_PENDING" or data["stage"] != expected):
                    raise ValueError("research is unapproved, terminal or attempts to skip a stage")
                if data["candidate_artifact_sha256"] != hypothesis["candidate_artifact_sha256"]:
                    raise ValueError("candidate changed between validation stages")
                if data["stage"] == "OUT_OF_SAMPLE":
                    training = hypothesis["results"][0]
                    if (_time(data["period"]["start"]) <= _time(training["period"]["end"]) or
                            data["dataset_sha256"] == training["dataset_sha256"]):
                        raise ValueError("out-of-sample data must be distinct and strictly after training")
                record = {"result_id": result_id, "created_at": now.isoformat(), **data,
                          "source_reported_result_only": True, "method_verified": False}
                conn.execute("INSERT INTO guardian_research_results VALUES(?,?,?,?,?)", (
                    result_id, identity, data["stage"], now.isoformat(), _json(record)))
                index = len(hypothesis["results"]) + 1
                status = ("RESEARCH_FAILED" if not data["passed"] else
                          f"{STAGES[index]}_PENDING" if index < len(STAGES) else "OWNER_REVIEW_REQUIRED")
                conn.execute("UPDATE guardian_research_hypotheses SET status=? WHERE hypothesis_id=?", (status, identity))
                self._audit(conn, "research_result_recorded", result_id, identity, now)
                result = self._get(conn, identity)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def review(self, identity: str, *, decision: str, expected_digest: str) -> dict:
        if decision not in ("SEND_TO_BACKTEST", "REJECT", "APPROVE_FOR_DEVELOPMENT"):
            raise ValueError("invalid owner research decision")
        expected_digest = _identity(expected_digest)
        review_id = _hash({"hypothesis_id": identity, "decision": decision, "evidence_digest": expected_digest})
        now = datetime.now(timezone.utc)
        with closing(self.store._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                hypothesis = self._get(conn, identity)
                existing = conn.execute("SELECT 1 FROM guardian_research_reviews WHERE review_id=?", (review_id,)).fetchone()
                if not existing:
                    if hypothesis["evidence_digest"] != expected_digest:
                        raise ValueError("owner must review the current research evidence digest")
                    if hypothesis["status"] in ("REJECTED", "RESEARCH_FAILED", "APPROVED_FOR_DEVELOPMENT_NO_DEPLOYMENT"):
                        raise ValueError("research decision is terminal")
                    if decision == "SEND_TO_BACKTEST" and hypothesis["status"] != "HYPOTHESIS_UNPROVEN":
                        raise ValueError("research has already started")
                    if decision == "APPROVE_FOR_DEVELOPMENT" and hypothesis["status"] != "OWNER_REVIEW_REQUIRED":
                        raise ValueError("all research stages must be recorded before development review")
                    status = {"SEND_TO_BACKTEST": "HISTORICAL_BACKTEST_PENDING", "REJECT": "REJECTED",
                              "APPROVE_FOR_DEVELOPMENT": "APPROVED_FOR_DEVELOPMENT_NO_DEPLOYMENT"}[decision]
                    record = {"review_id": review_id, "decision": decision, "evidence_digest": expected_digest,
                              "authority": "SEPARATE_OWNER_KEY", "created_at": now.isoformat(),
                              "methods_verified": False, "production_changed": False}
                    conn.execute("INSERT INTO guardian_research_reviews VALUES(?,?,?,?,?,?)", (
                        review_id, identity, expected_digest, decision, now.isoformat(), _json(record)))
                    conn.execute("UPDATE guardian_research_hypotheses SET status=? WHERE hypothesis_id=?", (status, identity))
                    self._audit(conn, "research_owner_reviewed", review_id, identity, now)
                result = self._get(conn, identity)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def list(self, *, limit: int = 50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid research list limit")
        with closing(self.store._connect()) as conn:
            conn.execute("BEGIN")
            identities = [row[0] for row in conn.execute(
                "SELECT hypothesis_id FROM guardian_research_hypotheses ORDER BY created_at DESC LIMIT ?", (limit,))]
            result = [self._get(conn, identity) for identity in identities]
            conn.commit()
            return result
