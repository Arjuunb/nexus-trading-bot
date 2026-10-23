"""Restricted decision-only facade over the existing shadow research store.

This facade exposes no order/fill/outcome or financial-account API. The existing
shadow database is physically separate from all financial ledgers.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from services.strategy_identity import canonical_configuration_json as encoded, configuration_fingerprint as digest


class ShadowObservationStore:
    def __init__(self, shadow_store):
        self._store = shadow_store
        migration = Path(__file__).parents[1] / "migrations" / "shadow" / "0003_shadow_observation.sql"
        with shadow_store._lock, shadow_store._db:
            shadow_store._db.executescript(migration.read_text())
            shadow_store._db.executescript(migration.with_name("0004_shadow_comparisons.sql").read_text())
            shadow_store._db.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def transaction(self):
        raw = self._store
        with raw._lock:
            raw._db.execute("BEGIN IMMEDIATE")
            try:
                yield raw._db
                raw._db.commit()
            except BaseException:
                raw._db.rollback()
                raise

    @staticmethod
    def _decode(row):
        if not row: return None
        row = dict(row)
        frozen = json.loads(row.pop("frozen_json"))
        row.pop("request_json")
        row.update(frozen)
        row.update(json.loads(row.pop("runtime_json")))
        row["desired_observing"] = bool(row["desired_observing"])
        return row

    def get(self, registration_id, *, owner_id):
        with self._store._lock:
            return self._decode(self._store._db.execute("SELECT * FROM shadow_registrations WHERE registration_id=? AND owner_id=?",
                                                        (registration_id, owner_id)).fetchone())

    def registrations(self, *, owner_id=None, active_only=False, limit=100):
        where, args = [], []
        if owner_id is not None: where.append("owner_id=?"); args.append(owner_id)
        if active_only: where.append("desired_observing=1")
        sql = "SELECT * FROM shadow_registrations" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        with self._store._lock:
            return [self._decode(r) for r in self._store._db.execute(sql, (*args, min(max(int(limit), 1), 1000)))]

    def create(self, frozen, request, *, max_per_owner=5, max_global=20, max_owner_history=100, max_history=1000):
        owner = frozen["owner_id"]; key = request["idempotency_key"]
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM shadow_registrations WHERE owner_id=? AND idempotency_key=?", (owner, key)).fetchone()
            if existing:
                if existing["request_json"] != encoded(request): raise ValueError("shadow idempotency conflict")
                return self._decode(existing)
            count = db.execute("SELECT COUNT(*) FROM shadow_registrations WHERE owner_id=? AND status<>'STOPPED'", (owner,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM shadow_registrations WHERE status<>'STOPPED'").fetchone()[0]
            history = db.execute("SELECT COUNT(*),COUNT(CASE WHEN owner_id=? THEN 1 END) FROM shadow_registrations", (owner,)).fetchone()
            if count >= max_per_owner or total >= max_global or history[0] >= max_history or history[1] >= max_owner_history: raise ValueError("SHADOW_RESOURCE_QUOTA")
            db.execute("INSERT INTO shadow_registrations(registration_id,owner_id,idempotency_key,request_json,frozen_json,status,readiness,created_at) VALUES (?,?,?,?,?,'DRAFT','WARMING_UP',?)",
                       (frozen["registration_id"], owner, key, encoded(request), encoded(frozen), frozen["registered_at"]))
            self._event(db, frozen["registration_id"], owner, "REGISTER", {"actor": owner, "config_fingerprint": frozen["config_fingerprint"]})
        return self.get(frozen["registration_id"], owner_id=owner)

    @staticmethod
    def _event(db, rid, owner, action, payload):
        timestamp = datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO shadow_observation_events VALUES (?,?,?,?,?,?)", ("shadow-audit:" + uuid.uuid4().hex, rid, owner, action, timestamp, encoded(payload)))

    def event(self, rid, *, owner_id, action, payload):
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM shadow_registrations WHERE registration_id=? AND owner_id=?", (rid, owner_id)).fetchone(): raise KeyError(rid)
            self._event(db, rid, owner_id, action, payload)

    def transition(self, rid, *, owner_id, action, now, reason, valid=None):
        transitions = {"START": {"READY"}, "PAUSE": {"READY", "OBSERVING", "BLOCKED"},
                       "RESUME": {"PAUSED"}, "STOP": {"DRAFT", "READY", "OBSERVING", "PAUSED", "BLOCKED", "FAILED"},
                       "BLOCK": {"READY", "OBSERVING"}, "VALIDATE": {"DRAFT", "READY", "BLOCKED"}}
        with self.transaction() as db:
            raw = db.execute("SELECT * FROM shadow_registrations WHERE registration_id=? AND owner_id=?", (rid, owner_id)).fetchone()
            if not raw: raise KeyError(rid)
            reg = self._decode(raw)
            if action == "START" and reg["desired_observing"]: return reg
            if action == "STOP" and reg["status"] == "STOPPED": return reg
            if reg["status"] not in transitions.get(action, set()): raise ValueError("invalid shadow lifecycle transition")
            new = {"START": "READY", "RESUME": "READY", "PAUSE": "PAUSED", "STOP": "STOPPED", "BLOCK": "BLOCKED"}.get(action)
            runtime = json.loads(raw["runtime_json"])
            if action == "VALIDATE":
                new = "READY" if valid["valid"] else "BLOCKED"
                runtime["blocking_reasons"] = valid["errors"]
            desired = action in {"START", "RESUME"}
            if action == "VALIDATE": desired = reg["desired_observing"]
            resume = now.isoformat() if desired else reg["resume_after"]
            if action == "RESUME": self._event(db, rid, owner_id, "OBSERVATION_GAP", {"reason": "PAUSED_INTERVAL", "until": resume, "backfilled": False})
            db.execute("UPDATE shadow_registrations SET status=?,desired_observing=?,resume_after=?,observation_start=COALESCE(observation_start,?),lease_token=NULL,lease_owner=NULL,lease_expiry=NULL,generation=generation+1,runtime_json=? WHERE registration_id=?",
                       (new, int(desired), resume, now.isoformat() if desired else None, encoded(runtime), rid))
            self._event(db, rid, owner_id, action, {"actor": owner_id, "previous_state": reg["status"], "new_state": new, "reason": reason, "timestamp": now.isoformat(), "config_fingerprint": reg["config_fingerprint"]})
        return self.get(rid, owner_id=owner_id)

    def claim(self, rid, worker_id, *, now, lease_seconds=60):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM shadow_registrations WHERE registration_id=? AND desired_observing=1", (rid,)).fetchone()
            if not row or (row["lease_expiry"] and row["lease_expiry"] > now): return None
            token = uuid.uuid4().hex
            if row["lease_token"]:
                self._event(db, rid, row["owner_id"], "OBSERVATION_GAP", {"reason": "LEASE_EXPIRED", "after": row["last_decision_time"], "until": datetime.fromtimestamp(now, timezone.utc).isoformat(), "backfilled": False})
            elif row["observation_start"] and now - datetime.fromisoformat(row["observation_start"]).timestamp() > 300:
                self._event(db, rid, row["owner_id"], "OBSERVATION_GAP", {"reason": "DELAYED_INITIAL_WORKER_START", "after": row["observation_start"], "until": datetime.fromtimestamp(now, timezone.utc).isoformat(), "backfilled": False})
            db.execute("UPDATE shadow_registrations SET lease_token=?,lease_owner=?,lease_expiry=?,heartbeat=?,generation=generation+1 WHERE registration_id=?", (token, worker_id, now+lease_seconds, now, rid))
            claimed = self._decode(db.execute("SELECT * FROM shadow_registrations WHERE registration_id=?", (rid,)).fetchone())
        # Return ONLY our claimed token, not a post-commit read which could
        # accidentally hand an expired worker its replacement's fencing token.
        return claimed

    def heartbeat(self, rid, token, *, now, lease_seconds=60):
        with self.transaction() as db:
            return db.execute("UPDATE shadow_registrations SET heartbeat=?,lease_expiry=? WHERE registration_id=? AND lease_token=? AND lease_expiry>? AND desired_observing=1", (now, now+lease_seconds, rid, token, now)).rowcount == 1

    def release(self, rid, token):
        with self.transaction() as db:
            db.execute("UPDATE shadow_registrations SET lease_expiry=0 WHERE registration_id=? AND lease_token=?", (rid, token))

    def decision_at(self, rid, candle_close, *, owner_id):
        with self._store._lock:
            row = self._store._db.execute("SELECT payload_json FROM shadow_observations WHERE registration_id=? AND candle_close=? AND owner_id=?", (rid, candle_close, owner_id)).fetchone()
            return json.loads(row[0]) if row else None

    def commit(self, decision, *, generation, lease_token=None, now, max_observations=100000):
        rid = decision["registration_id"]; owner = decision["owner_id"]
        with self.transaction() as db:
            raw = db.execute("SELECT * FROM shadow_registrations WHERE registration_id=? AND owner_id=?", (rid, owner)).fetchone()
            if not raw or not raw["desired_observing"] or raw["generation"] != generation: raise ValueError("STALE_SHADOW_ATTEMPT")
            if lease_token and (raw["lease_token"] != lease_token or raw["lease_expiry"] <= now): raise ValueError("STALE_SHADOW_ATTEMPT")
            existing = db.execute("SELECT * FROM shadow_observations WHERE registration_id=? AND candle_close=?", (rid, decision["candle_close_timestamp"])).fetchone()
            if existing:
                if existing["candle_hash"] != decision["candle_hash"] or existing["context_hash"] != decision["context_hash"]: raise ValueError("CANDLE_REVISION_CONFLICT")
                return json.loads(existing["payload_json"])
            count = db.execute("SELECT COUNT(*) FROM shadow_observations WHERE registration_id=?", (rid,)).fetchone()[0]
            if count >= max_observations: raise ValueError("SHADOW_RETENTION_LIMIT")
            if raw["last_decision_time"] and decision["candle_close_timestamp"] <= raw["last_decision_time"]: raise ValueError("OUT_OF_ORDER_OBSERVATION")
            decision["decision_id"] = "shadow-observation:" + digest({"registration_id": rid, "close": decision["candle_close_timestamp"]})
            decision["evidence_commit_recorded_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
            db.execute("INSERT INTO shadow_observations VALUES (?,?,?,?,?,?,?,?)", (decision["decision_id"], rid, owner, decision["candle_close_timestamp"], decision["candle_hash"], decision["context_hash"], decision["status"], encoded(decision)))
            eligible = decision["status"] in {"SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL"}
            runtime = json.loads(raw["runtime_json"])
            runtime.update(feed_freshness=decision["data_freshness"], last_status=decision["status"], blocking_reasons=decision["rejection_reasons"] if not eligible else [])
            db.execute("UPDATE shadow_registrations SET status=?,readiness=?,last_decision_time=?,runtime_json=? WHERE registration_id=?", ("OBSERVING" if eligible else "BLOCKED", "READY" if eligible else ("WARMING_UP" if decision["status"] == "BLOCKED_WARMUP" else "BLOCKED"), decision["candle_close_timestamp"], encoded(runtime), rid))
            if raw["status"] != ("OBSERVING" if eligible else "BLOCKED"):
                self._event(db, rid, owner, "EVALUATION_STATE", {"previous_state": raw["status"], "new_state": "OBSERVING" if eligible else "BLOCKED", "decision_id": decision["decision_id"]})
        return json.loads(encoded(decision))

    def decisions(self, rid, *, owner_id, limit=100, offset=0):
        if not self.get(rid, owner_id=owner_id): raise KeyError(rid)
        with self._store._lock:
            return [json.loads(r[0]) for r in self._store._db.execute("SELECT payload_json FROM shadow_observations WHERE registration_id=? AND owner_id=? ORDER BY candle_close DESC LIMIT ? OFFSET ?", (rid, owner_id, min(max(int(limit), 1), 1000), max(int(offset), 0)))]

    def comparison_inputs(self, rid, *, owner_id, start, end):
        # Consistent shadow decision + audit snapshot; no calculation inside
        # this short transaction. A competing writer cannot split the view.
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM shadow_registrations WHERE registration_id=? AND owner_id=?", (rid,owner_id)).fetchone(): raise KeyError(rid)
            rows = db.execute("SELECT payload_json FROM shadow_observations WHERE registration_id=? AND owner_id=? AND julianday(candle_close)>=julianday(?) AND julianday(candle_close)<julianday(?) ORDER BY candle_close LIMIT 2017", (rid,owner_id,start.isoformat(),end.isoformat())).fetchall()
            # Include historical conflicts affecting this period even when a
            # revision was discovered later. Lifecycle start/pause evidence is
            # retained too; cap explicitly rather than silently truncating.
            events = db.execute("SELECT * FROM shadow_observation_events WHERE registration_id=? AND owner_id=? ORDER BY timestamp LIMIT 1001", (rid,owner_id)).fetchall()
            if len(rows)>2016 or len(events)>1000: raise ValueError("COMPARISON_EVIDENCE_LIMIT")
            return [json.loads(r[0]) for r in rows], [{"event_id":r["event_id"],"action":r["action"],"timestamp":r["timestamp"],"payload":json.loads(r["payload_json"])} for r in events]

    def comparison_artifact(self, comparison_id, *, owner_id):
        with self._store._lock:
            r = self._store._db.execute("SELECT payload_json FROM shadow_comparison_artifacts WHERE comparison_id=? AND owner_id=?", (comparison_id,owner_id)).fetchone()
            return json.loads(r[0]) if r else None

    def comparison_artifacts(self, *, owner_id, limit=50, offset=0):
        if not 1 <= int(limit) <= 100 or not 0 <= int(offset) <= 10000: raise ValueError("COMPARISON_QUERY_LIMIT")
        with self._store._lock:
            return [dict(r) for r in self._store._db.execute("SELECT comparison_id,registration_id,active_source_id,period_start,period_end,calculation_version,input_watermark,created_at FROM shadow_comparison_artifacts WHERE owner_id=? ORDER BY created_at DESC,comparison_id LIMIT ? OFFSET ?", (owner_id,limit,offset))]

    def comparison_by_key(self, *, owner_id, key, request):
        with self._store._lock:
            row = self._store._db.execute("SELECT request_json,comparison_id FROM shadow_comparison_requests WHERE owner_id=? AND idempotency_key=?", (owner_id,key)).fetchone()
            if not row: return None
            if row["request_json"] != encoded(request): raise ValueError("COMPARISON_IDEMPOTENCY_CONFLICT")
            return self.comparison_artifact(row["comparison_id"], owner_id=owner_id)

    def commit_comparison(self, result, *, request, key):
        owner = result["owner_id"]; rid = result["registration_id"]
        payload = encoded(result)
        if len(payload.encode())>4*1024*1024: raise ValueError("COMPARISON_RESULT_SIZE_LIMIT")
        with self.transaction() as db:
            previous = self.comparison_by_key(owner_id=owner,key=key,request=request)
            if previous: return previous
            if not db.execute("SELECT 1 FROM shadow_registrations WHERE registration_id=? AND owner_id=?", (rid,owner)).fetchone(): raise KeyError(rid)
            count = db.execute("SELECT COUNT(*) FROM shadow_comparison_requests WHERE owner_id=?", (owner,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM shadow_comparison_artifacts").fetchone()[0]
            if count>=1000 or total>=10000: raise ValueError("COMPARISON_RETENTION_QUOTA")
            previous = self.comparison_artifact(result["comparison_id"],owner_id=owner)
            if previous:
                # Creation clock is not evidence identity; reuse the original
                # artifact only if every other fact agrees exactly.
                if encoded({k:v for k,v in previous.items() if k!='created_at'}) != encoded({k:v for k,v in result.items() if k!='created_at'}): raise ValueError("COMPARISON_RESULT_CONFLICT")
                result = previous
            else:
                db.execute("INSERT INTO shadow_comparison_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)", (result["comparison_id"],owner,rid,result["active_source_id"],result["start"],result["end"],result["calculation_version"],result["input_watermark"],result["created_at"],payload))
            db.execute("INSERT INTO shadow_comparison_requests VALUES (?,?,?,?)", (owner,key,encoded(request),result["comparison_id"]))
        return json.loads(encoded(result))

    def events(self, rid, *, owner_id, limit=100):
        if not self.get(rid, owner_id=owner_id): raise KeyError(rid)
        with self._store._lock:
            return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in self._store._db.execute("SELECT * FROM shadow_observation_events WHERE registration_id=? AND owner_id=? ORDER BY timestamp DESC LIMIT ?", (rid, owner_id, min(max(int(limit), 1), 1000)))]

    def analytics(self, rid, *, owner_id):
        if not self.get(rid, owner_id=owner_id): raise KeyError(rid)
        with self._store._lock:
            statuses = Counter({r[0]: r[1] for r in self._store._db.execute("SELECT status,COUNT(*) FROM shadow_observations WHERE registration_id=? AND owner_id=? GROUP BY status", (rid, owner_id))})
            gaps = self._store._db.execute("SELECT COUNT(*) FROM shadow_observation_events WHERE registration_id=? AND owner_id=? AND action='OBSERVATION_GAP'", (rid, owner_id)).fetchone()[0]
            conflicts = self._store._db.execute("SELECT COUNT(*) FROM shadow_observation_events WHERE registration_id=? AND owner_id=? AND action='CANDLE_REVISION_CONFLICT'", (rid, owner_id)).fetchone()[0]
            failures = self._store._db.execute("SELECT COUNT(*) FROM shadow_observation_events WHERE registration_id=? AND owner_id=? AND action='OBSERVATION_FAILURE'", (rid, owner_id)).fetchone()[0]
            payloads = self._store._db.execute("SELECT payload_json FROM shadow_observations WHERE registration_id=? AND owner_id=?", (rid, owner_id))
            reasons, sessions, regimes, volatility = Counter(), Counter(), Counter(), Counter()
            latency_sum, latency_max = 0.0, None
            for item in payloads:
                record = json.loads(item[0]); reasons.update(record["rejection_reasons"])
                context = record.get("market_context") or {}
                sessions[context.get("session", "UNKNOWN")] += 1
                regimes[context.get("trend_regime", "UNKNOWN")] += 1
                volatility[context.get("volatility_regime", "UNKNOWN")] += 1
                latency = record.get("evaluation_latency_ms")
                if latency is not None:
                    latency_sum += latency; latency_max = max(latency_max or 0, latency)
        total = sum(statuses.values()); signals = statuses["SIGNAL_LONG"] + statuses["SIGNAL_SHORT"]
        eligible = signals + statuses["NO_SIGNAL"]
        return {"contract_version": "shadow_decisions.v1", "execution_mode": "shadow_observation", "scheduled_opportunities": total,
                "eligible_opportunities": eligible, "signal_count": signals, "signal_frequency": signals/eligible if eligible else None,
                "long_count": statuses["SIGNAL_LONG"], "short_count": statuses["SIGNAL_SHORT"], "data_block_count": total-eligible,
                "data_block_rate": (total-eligible)/total if total else None, "statuses": dict(statuses), "observation_gaps": gaps,
                "evidence_completeness": "CONFLICTED" if conflicts else "PARTIAL" if total-eligible or gaps or failures else "COMPLETE" if total else "UNKNOWN",
                "conflicting_events": conflicts, "persistence_failures": failures, "rejection_reasons": dict(reasons),
                "sessions": dict(sessions), "market_regimes": dict(regimes), "volatility": dict(volatility),
                "mean_evaluation_latency_ms": latency_sum / total if total else None, "max_evaluation_latency_ms": latency_max,
                "observation_uptime": None, "uptime_reason": "Not measured; gap events are not a continuous feed-uptime measure",
                "profitability": "NOT_ASSESSED", "calculated_at": datetime.now(timezone.utc).isoformat()}
