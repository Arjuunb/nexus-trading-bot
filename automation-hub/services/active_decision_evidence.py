"""Read persisted active decisions; never construct an execution runtime.

Legacy labs are single-owner databases without tenant columns. They are exposed
ONLY to an explicitly bound legacy owner, never to arbitrary authenticated users.
Every connection is mode=ro/query_only and every read is a bounded snapshot.
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import math
from pathlib import Path
import sqlite3

from bot.data.resample import TF_SECONDS
from services.shadow_router import utc

DECISION_STATUSES = {"SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL", "BLOCKED_STALE_DATA",
                     "BLOCKED_MISSING_CONTEXT", "BLOCKED_WARMUP", "ACTIVE_PAUSED",
                     "ACTIVE_STOPPED", "NO_DECISION_RECORDED", "DATA_UNAVAILABLE",
                     "EVALUATION_ERROR", "UNKNOWN"}


def normalize_active(record, *, source_id, owner_id):
    """Missing facts stay null. Lifecycle order states never imply a decision."""
    fields = ("decision_id", "tenant_id", "strategy_id", "strategy_version", "config_fingerprint",
              "venue", "market_type", "symbol", "timeframe", "candle_close_timestamp",
              "decision_timestamp", "candle_hash", "canonical_candle", "context_hash",
              "finalized", "htf_alignment", "lookback_status", "observation_authorized",
              "signal_direction", "data_freshness", "evaluation_latency_ms", "market_context")
    fields += ("configuration_snapshot", "configuration_reference", "configuration_status",
               "input_schema_version", "evidence_commit_recorded_at", "persistence_status",
               "htf_context_identity", "lookback_spec", "optional_fields", "source")
    result = {key: record.get(key) for key in fields}
    result.update(owner_id=record.get("owner_id", owner_id), source_id=source_id,
                  evidence_kind="ACTIVE_DECISION_OBSERVATION",
                  source_evidence_kind=record.get("source_evidence_kind"),
                  status=record.get("status") if record.get("status") in DECISION_STATUSES else "UNKNOWN",
                  evidence_completeness=record.get("evidence_completeness", "PARTIAL"),
                  rejection_reasons=record.get("rejection_reasons") or [],
                  missing_conditions=record.get("missing_conditions") or [],
                  limitations=record.get("limitations") or [],
                  source_revision=record.get("source_revision"))
    for key in ("candle_close_timestamp", "decision_timestamp"):
        if result[key]:
            try: result[key] = utc(result[key]).isoformat()
            except (ValueError, TypeError): result[key] = None
    return result


class ActiveDecisionSources:
    def __init__(self, *, pa_path=None, smc_path=None, decisions_path=None, lab_owner=None):
        self.paths = {"pa-lab": pa_path, "smc-lab": smc_path, "instance": decisions_path}
        self.lab_owner = lab_owner

    @staticmethod
    @contextmanager
    def connection(path):
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.rollback(); db.close()

    def sources(self, *, owner_id, limit=100):
        limit = min(max(int(limit), 1), 100)
        result = []
        for kind, path in self.paths.items():
            if not path or (kind != "instance" and owner_id != self.lab_owner): continue
            try:
                with self.connection(path) as db:
                    if kind == "instance":
                        # Research/replay/webhook rows and unknown ownership are
                        # deliberately not reclassified as forward active data.
                        rows = db.execute("SELECT instance_id,simulation_session_id,COUNT(*) AS records FROM decisions WHERE owner_id=? AND source_kind='forward_paper' AND instance_id<>'' GROUP BY instance_id,simulation_session_id LIMIT ?", (owner_id, limit)).fetchall()
                        result.extend({"source_id": f"instance:{r['instance_id']}:{r['simulation_session_id'] or ''}",
                                       "kind": kind, "instance_id": r["instance_id"], "session_id": r["simulation_session_id"],
                                       "coverage_class": "SIGNALS_ONLY", "record_count": r["records"],
                                       "limitations": ["No complete no-signal opportunity journal", "Candle close/data/context identity may be absent"]} for r in rows)
                    else:
                        prefix = "pa" if kind == "pa-lab" else "smc"
                        ownership = "(CASE WHEN json_valid(e.payload_json) THEN json_extract(e.payload_json,'$.active_evidence.tenant_id') END)"
                        rows = db.execute(f"SELECT s.id,s.symbol,s.timeframe,s.status,COUNT(e.correlation_id) AS records FROM {prefix}_sessions s LEFT JOIN {prefix}_evaluations e ON e.session_id=s.id WHERE s.mode='LIVE_PAPER' AND ({ownership} IS NULL OR {ownership}=?) GROUP BY s.id ORDER BY s.rowid DESC LIMIT ?", (owner_id,limit)).fetchall()
                        result.extend({"source_id": f"{kind}:{r['id']}", "kind": kind, "session_id": r["id"],
                                       "symbol": r["symbol"], "timeframe": r["timeframe"], "current_session_status": r["status"],
                                       "coverage_class": "PARTIAL_DECISION_EVIDENCE", "record_count": r["records"],
                                       "limitations": ["Legacy rows may lack contemporaneous configuration and native HTF input; inspect window-specific health", "Decision lifecycle fields are mutable; immutable captures or original payloads are used", "Current session state is not historical pause evidence"]} for r in rows)
                        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_decision_receipts'").fetchone():
                            # Legacy global reset is unchanged. Committed evidence
                            # survives it and retains its known original owner.
                            orphaned=db.execute(f"SELECT r.session_id,COUNT(*) AS records,MIN(json_extract(r.snapshot_json,'$.symbol')) AS symbol,MIN(json_extract(r.snapshot_json,'$.timeframe')) AS timeframe FROM active_decision_receipts r LEFT JOIN {prefix}_sessions s ON s.id=r.session_id WHERE s.id IS NULL AND r.tenant_id=? AND r.source_kind=? GROUP BY r.session_id LIMIT ?",(owner_id,prefix,limit)).fetchall()
                            result.extend({"source_id":f"{kind}:{r['session_id']}","kind":kind,"session_id":r["session_id"],"symbol":r["symbol"],"timeframe":r["timeframe"],"coverage_class":"PARTIAL_DECISION_EVIDENCE","record_count":r["records"],"limitations":["Operational session removed; retained receipts only, missing opportunities remain unknown"]} for r in orphaned)
            except sqlite3.Error:
                result.append({"source_id": kind, "kind": kind, "coverage_class": "UNAVAILABLE", "limitations": ["Persisted decision database unavailable or incompatible"]})
        return result[:limit]

    def read(self, source_id, *, owner_id, start, end, limit=2016):
        start,end=utc(start),utc(end)
        if end<=start or end-start>timedelta(days=7): raise ValueError("INVALID_OBSERVATION_WINDOW")
        limit=min(max(int(limit),1),2016)
        descriptor = next((s for s in self.sources(owner_id=owner_id) if s["source_id"] == source_id), None)
        if descriptor is None: raise KeyError(source_id)
        if descriptor["coverage_class"] == "UNAVAILABLE": return descriptor, []
        kind = descriptor["kind"]
        with self.connection(self.paths[kind]) as db:
            if kind == "instance":
                rows = db.execute("SELECT * FROM decisions WHERE owner_id=? AND source_kind='forward_paper' AND instance_id=? AND simulation_session_id IS ? AND julianday(ts)>=julianday(?) AND julianday(ts)<julianday(?) ORDER BY id LIMIT ?",
                                  (owner_id, descriptor["instance_id"], descriptor["session_id"], start.isoformat(), end.isoformat(), limit+1)).fetchall()
            else:
                prefix = "pa" if kind == "pa-lab" else "smc"
                seconds = TF_SECONDS.get(descriptor["timeframe"])
                if seconds is None: return {**descriptor, "coverage_class": "UNAVAILABLE", "limitations": ["Unknown candle interval"]}, []
                durations="CASE timeframe "+" ".join(f"WHEN '{tf}' THEN '{duration} seconds'" for tf,duration in TF_SECONDS.items())+" END"
                # Use the original row's clock, never the current UI/session
                # timeframe to relabel or slice older decisions.
                rows = db.execute(f"SELECT * FROM {prefix}_evaluations WHERE session_id=? AND julianday(candle_time,{durations})>=julianday(?) AND julianday(candle_time,{durations})<julianday(?) AND ((CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.active_evidence.tenant_id') END) IS NULL OR (CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.active_evidence.tenant_id') END)=?) ORDER BY candle_time LIMIT ?",
                                  (descriptor["session_id"],start.isoformat(),end.isoformat(),owner_id,limit+1)).fetchall()
            if len(rows) > limit: raise ValueError("ACTIVE_EVIDENCE_QUERY_LIMIT")
            result = []
            receipts = {}
            if kind != "instance" and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_decision_receipts'").fetchone():
                receipt_duration=durations.replace("CASE timeframe","CASE json_extract(snapshot_json,'$.timeframe')")
                receipt_rows = db.execute(f"SELECT * FROM active_decision_receipts WHERE tenant_id=? AND source_kind=? AND session_id=? AND julianday(candle_time,{receipt_duration})>=julianday(?) AND julianday(candle_time,{receipt_duration})<julianday(?) ORDER BY candle_time LIMIT ?",
                    (owner_id,prefix,descriptor["session_id"],start.isoformat(),end.isoformat(),limit+1)).fetchall()
                if len(receipt_rows)>limit: raise ValueError("ACTIVE_EVIDENCE_QUERY_LIMIT")
                for receipt in receipt_rows:
                    frozen=self._receipt(dict(receipt),source_id,owner_id)
                    superseded=db.execute("SELECT 1 FROM active_evidence_supersessions WHERE tenant_id=? AND decision_id=? LIMIT 1",(owner_id,receipt["decision_id"])).fetchone()
                    if superseded:
                        frozen.update(status="UNKNOWN",evidence_completeness="PARTIAL",limitations=["ACTIVE_EVIDENCE_SUPERSEDED"])
                    receipts[receipt["decision_id"]]=frozen
                result.extend(receipts.values())
            for row in rows:
                if kind != "instance" and row["correlation_id"] in receipts:
                    # Detect source/receipt divergence instead of accepting an
                    # arbitrary winner. Normal lifecycle fields are excluded.
                    from services.strategy_identity import configuration_fingerprint as digest
                    try:
                        original={key:row[key] for key in ("correlation_id","session_id","candle_time","symbol","timeframe","strategy_id","strategy_version","created_at")}
                        original["active_evidence"]=json.loads(row["payload_json"])["active_evidence"]
                        if digest(original)!=receipts[row["correlation_id"]].get("source_revision"):
                            receipts[row["correlation_id"]].update(status="UNKNOWN",evidence_completeness="PARTIAL",limitations=["ACTIVE_RECEIPT_SOURCE_CONFLICT"])
                    except (ValueError,KeyError,TypeError):
                        receipts[row["correlation_id"]].update(status="UNKNOWN",evidence_completeness="PARTIAL",limitations=["ACTIVE_RECEIPT_SOURCE_CONFLICT"])
                    continue
                result.append(self._normalize(dict(row), kind, source_id, owner_id))
            if len(result)>limit: raise ValueError("ACTIVE_EVIDENCE_QUERY_LIMIT")
            result.sort(key=lambda r:r.get("candle_close_timestamp") or "")
            if kind != "instance" and result and all(r["evidence_completeness"]=="COMPLETE" for r in result):
                descriptor={**descriptor,"coverage_class":"FULL_DECISION_EVIDENCE","limitations":[],
                            "coverage_basis":"FROZEN_RECEIPTS_IN_REQUESTED_WINDOW; MISSING_OPPORTUNITIES_CHECKED_SEPARATELY"}
        return descriptor, result

    def health(self, source_id, *, owner_id, start, end):
        """Window-scoped facts; this is active readiness, not shadow eligibility."""
        from services.shadow_comparison_service import _record_errors, canonical_candle
        from collections import Counter
        start,end=utc(start),utc(end)
        if end<=start or end-start>timedelta(days=7): raise ValueError("INVALID_OBSERVATION_WINDOW")
        descriptor,rows=self.read(source_id,owner_id=owner_id,start=start,end=end,limit=2016)
        original_clocks={r.get("timeframe") for r in rows if r.get("timeframe")}
        tf=next(iter(original_clocks)) if len(original_clocks)==1 else descriptor.get("timeframe") if not rows else None
        seconds=TF_SECONDS.get(tf)
        expected=max(0,math.ceil(end.timestamp()/seconds)-math.ceil(start.timestamp()/seconds)) if seconds else None
        if expected is not None and expected>2016: raise ValueError("ACTIVE_EVIDENCE_QUERY_LIMIT")
        valid=[r for r in rows if not _record_errors(r,"ACTIVE",owner_id) and canonical_candle(r) is not None]
        known={r["candle_close_timestamp"] for r in rows if r.get("candle_close_timestamp")}
        verified={r["candle_close_timestamp"] for r in valid}
        reasons=Counter(reason for r in rows for reason in [*_record_errors(r,"ACTIVE",owner_id),*r.get("limitations",[])])
        timed=[r["candle_close_timestamp"] for r in rows if r.get("candle_close_timestamp")]
        if len(timed)>len(known): reasons["DUPLICATE_ACTIVE_OPPORTUNITY"]+=len(timed)-len(known)
        identities={(r.get("strategy_id"),r.get("strategy_version"),r.get("config_fingerprint")) for r in rows}
        if len(identities)>1: reasons["ACTIVE_CONFIGURATION_CHANGED_WITHIN_WINDOW"]+=1
        missing=expected-len(known) if expected is not None else None
        if missing: reasons["NO_DECISION_RECORDED"]+=missing
        matrix={};field_counts={}
        fields=("tenant_id","strategy_id","strategy_version","config_fingerprint","configuration_reference",
                "venue","market_type","symbol","timeframe","candle_close_timestamp","canonical_candle",
                "htf_context_identity","source","data_freshness","status","signal_direction",
                "rejection_reasons","decision_timestamp","persistence_status","session","market_regime","volatility")
        for field in fields:
            counts=Counter()
            for row in rows:
                value=(row.get("market_context") or {}).get({"session":"session","market_regime":"trend_regime","volatility":"volatility_regime"}.get(field)) if field in {"session","market_regime","volatility"} else row.get(field)
                if field=="signal_direction" and row.get("status")=="NO_SIGNAL": state="NOT_APPLICABLE"
                elif value is None or value=="": state="MISSING"
                elif value=="UNKNOWN": state="UNKNOWN"
                else: state="PRESENT_VERIFIED" if row.get("persistence_status")=="COMMITTED" else "PRESENT_UNVERIFIED"
                counts[state]+=1
            field_counts[field]=dict(counts)
            matrix[field]=next(iter(counts)) if len(counts)==1 else "PRESENT_UNVERIFIED" if counts else "UNKNOWN"
        backlog=None;last_reconciliation=None;reconciliation="UNKNOWN"
        if descriptor.get("kind") in {"pa-lab","smc-lab"}:
            prefix="pa" if descriptor["kind"]=="pa-lab" else "smc"
            try:
                with self.connection(self.paths[descriptor["kind"]]) as db:
                    backlog=db.execute(f"SELECT COUNT(*) FROM {prefix}_evaluations e LEFT JOIN active_decision_receipts r ON r.decision_id=e.correlation_id WHERE e.session_id=? AND r.decision_id IS NULL AND (CASE WHEN json_valid(e.payload_json) THEN json_type(e.payload_json,'$.active_evidence.configuration_snapshot') END)='object' AND (CASE WHEN json_valid(e.payload_json) THEN json_extract(e.payload_json,'$.active_evidence.tenant_id') END)=?",(descriptor["session_id"],owner_id)).fetchone()[0]
                    recovery=db.execute("SELECT last_success_at,status FROM active_evidence_recovery WHERE id=1").fetchone()
                    if recovery: last_reconciliation,reconciliation=recovery
                    reconciliation="RECOVERING" if backlog else reconciliation
            except sqlite3.Error: pass
        status="VERIFIED" if expected and len(verified)==expected and len(identities)==1 and not reasons else "PARTIAL" if valid else "BLOCKED" if rows else "UNKNOWN"
        return {"schema_version":"active-evidence-health.v1","source":descriptor,"start":start.isoformat(),"end":end.isoformat(),
                "status":status,"comparison_readiness":"ACTIVE_EVIDENCE_READY" if status=="VERIFIED" else "INCOMPLETE_ACTIVE_EVIDENCE",
                "expected_opportunities":expected,"recorded_opportunities":len(known),"verified_opportunities":len(verified),
                "missing_decisions":missing,"active_decision_coverage":len(known)/expected if expected else None,
                "verified_decision_coverage":len(verified)/expected if expected else None,
                "configuration_completeness":sum(r.get("configuration_status")=="VERIFIED" for r in rows)/len(rows) if rows else None,
                "market_context_completeness":sum(canonical_candle(r) is not None and r.get("htf_alignment")=="VERIFIED" for r in rows)/len(rows) if rows else None,
                "field_matrix":matrix,"field_counts":field_counts,"blocking_reasons":dict(reasons),
                "last_verified_observation":max(verified) if verified else None,
                "reconciliation_backlog":backlog,"reconciliation_status":reconciliation,
                "last_successful_reconciliation":last_reconciliation,
                "duplicate_evidence_count":len(rows)-len(known) if all(r.get("candle_close_timestamp") for r in rows) else None,
                "delivery_retry_count":None,"persistence_failure_count":None,
                "unverifiable_records":sum(r.get("persistence_status")!="COMMITTED" for r in rows),
                "optional_fields":["session","market_regime","volatility","individual_publication_timestamp"],
                "coverage_basis":"REQUESTED_NATIVE_CANDLE_CLOCK; NOT EXCHANGE_UPTIME",
                "profitability":"NOT_ASSESSED"}

    @staticmethod
    def _receipt(row, source_id, owner):
        from services.active_evidence_capture import SCHEMA, MAX_BYTES
        from services.strategy_identity import configuration_fingerprint as digest
        from services.shadow_comparison_service import canonical_candle
        try:
            if len(row["snapshot_json"].encode())>MAX_BYTES+4096: raise ValueError("CAPTURE_SIZE_LIMIT")
            snapshot=json.loads(row["snapshot_json"]);capture=snapshot["active_evidence"]
            cfg=capture["configuration_snapshot"]
            if cfg.get("schema")!="effective-native-lab-configuration-v1": raise ValueError("INCOMPLETE_CONFIGURATION")
            if row["source_kind"]=="pa":
                from services.native_price_action import PriceActionConfig
                from services.price_action_lab import PaperExecutionConfig
                native_cls,execution_cls=PriceActionConfig,PaperExecutionConfig
            else:
                from services.native_smc import SMCConfig
                from services.smc_strategy_lab import SMCPaperConfig
                native_cls,execution_cls=SMCConfig,SMCPaperConfig
            if set(cfg["native_config"])!=set(native_cls.__dataclass_fields__) or set(cfg["execution_config"])!=set(execution_cls.__dataclass_fields__):
                raise ValueError("INCOMPLETE_CONFIGURATION")
            if capture.get("schema")!=SCHEMA or digest(cfg)!=capture.get("config_fingerprint"):
                raise ValueError("CONFIGURATION_IDENTITY_CONFLICT")
            for key in ("strategy_id","strategy_version","symbol","timeframe"):
                if capture[key]!=snapshot[key]: raise ValueError("SOURCE_IDENTITY_CONFLICT")
            if capture["decision_id"]!=row["decision_id"] or capture["tenant_id"]!=owner or capture["owner_id"]!=owner:
                raise ValueError("TENANT_OR_DECISION_CONFLICT")
            if capture["source_session_id"]!=row["session_id"]: raise ValueError("SOURCE_SESSION_CONFLICT")
            close=utc(snapshot["candle_time"])+timedelta(seconds=TF_SECONDS[capture["timeframe"]])
            if close!=utc(capture["candle_close_timestamp"]) or canonical_candle(capture) is None:
                raise ValueError("CANDLE_IDENTITY_CONFLICT")
            material=capture["context_material"]
            if digest(material)!=capture["context_hash"] or material[capture["timeframe"]][-1]!=capture["canonical_candle"]:
                raise ValueError("CONTEXT_IDENTITY_CONFLICT")
            for tf,rows in material.items():
                if len(rows)>1500: raise ValueError("CONTEXT_SIZE_LIMIT")
                for candle in rows:
                    end=utc(candle["open_timestamp"])+timedelta(seconds=TF_SECONDS[tf])
                    if end>close or canonical_candle({"canonical_candle":candle,"timeframe":tf,"candle_close_timestamp":end.isoformat()}) is None:
                        raise ValueError("CONTEXT_DATA_CONFLICT")
            result={**capture,"input_schema_version":SCHEMA,"persistence_status":"COMMITTED",
                    "evidence_commit_recorded_at":row["persisted_at"],
                    "htf_context_identity":capture["htf_context_identity"],
                    "lookback_spec":{"required_candles":capture["required_candles"],"basis":capture["lookback_basis"]},
                    "limitations":capture.get("errors") or [],"source_revision":digest(snapshot)}
            return normalize_active(result,source_id=source_id,owner_id=owner)
        except (ValueError,TypeError,KeyError,AttributeError,IndexError):
            try:
                original=json.loads(row["snapshot_json"])
                tf=original.get("timeframe")
                close=(utc(row["candle_time"])+timedelta(seconds=TF_SECONDS[tf])).isoformat()
            except (ValueError,TypeError,KeyError,AttributeError):
                original={};close=None
            return normalize_active({"decision_id":row.get("decision_id"),"tenant_id":owner,
                                     "candle_close_timestamp":close,
                                     **{k:original.get(k) for k in ("symbol","timeframe","strategy_id","strategy_version")},
                                     "status":"EVALUATION_ERROR","limitations":["INVALID_IMMUTABLE_ACTIVE_RECEIPT"]},source_id=source_id,owner_id=owner)

    @staticmethod
    def _normalize(row, kind, source_id, owner):
        if kind == "instance":
            side = str(row.get("side") or "").upper()
            return normalize_active({**row, "decision_id": "active-instance:" + str(row["id"]),
                                     "status": "SIGNAL_"+side if side in {"LONG", "SHORT"} else "UNKNOWN",
                                     "config_fingerprint": row.get("strategy_config_hash"),
                                     "decision_timestamp": row.get("decided_at"),
                                     "source_evidence_kind": "FORWARD_PAPER_SIGNAL_DECISION",
                                     "rejection_reasons": [row["reason"]] if row.get("reason") else []}, source_id=source_id, owner_id=owner)
        try:
            close = (utc(row["candle_time"]) + timedelta(seconds=TF_SECONDS[row["timeframe"]])).isoformat()
        except (KeyError, ValueError, TypeError):
            close = None
        try:
            text = row.get("payload_json") or "{}"
            if len(text.encode()) > 131072:
                raise ValueError("Oversized decision payload")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("Invalid decision object")
            for name in ("closed_candle", "source_evaluation", "active_evidence"):
                if payload.get(name) is not None and not isinstance(payload[name], dict):
                    raise ValueError("Invalid decision material")
            if payload.get("feed_state") is not None and not isinstance(payload["feed_state"], str):
                raise ValueError("Invalid feed state")
            original = payload.get("source_evaluation") or {}
            for name in ("proposal", "trade_plan"):
                if original.get(name) is not None and not isinstance(original[name], dict):
                    raise ValueError("Invalid native report")
            original_missing = original.get("missing_conditions") or []
            if not isinstance(original_missing, list) or any(not isinstance(x, str) for x in original_missing):
                raise ValueError("Invalid native conditions")
            capture_errors=(payload.get("active_evidence") or {}).get("errors") or []
            if not isinstance(capture_errors,list) or any(not isinstance(x,str) for x in capture_errors):
                raise ValueError("Invalid capture diagnostics")
            missing = json.loads(row.get("missing_conditions_json") or "[]")
            if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
                raise ValueError("Invalid missing conditions")
        except (ValueError, TypeError, AttributeError):
            # Persisted text is not trusted code or a valid neutral decision.
            # Keep the scheduled opportunity visible without leaking its body.
            return normalize_active({"decision_id": row.get("correlation_id"),
                                     "tenant_id": owner, "status": "EVALUATION_ERROR",
                                     "candle_close_timestamp": close,
                                     "limitations": ["INVALID_PERSISTED_DECISION_PAYLOAD"]},
                                    source_id=source_id, owner_id=owner)
        status, direction = "UNKNOWN", None
        material = payload.get("closed_candle")
        if kind == "pa-lab" and isinstance(payload.get("proposal_ids"), list):
            if not payload["proposal_ids"]: status = "NO_SIGNAL"
            # IDs alone cannot establish signal direction; never consult an
            # order/fill as though it were the original strategy decision.
        if kind == "smc-lab":
            original = payload.get("source_evaluation") or {}
            if original.get("state") == "ENTRY_READY" and original.get("proposal") and original.get("trade_plan"):
                direction = {"bullish":"LONG","bearish":"SHORT"}.get(original["proposal"].get("direction"),str(original["proposal"].get("direction") or "").upper())
                if direction in {"LONG", "SHORT"}: status = "SIGNAL_" + direction
            elif original.get("state")=="WATCHING":
                status = "BLOCKED_MISSING_CONTEXT" if "native closed-bar context" in (original.get("missing_conditions") or []) else "NO_SIGNAL"
        feed = payload.get("feed_state")
        if feed in {"STALE_CANDLES", "STALE_HTF_CANDLE", "DATA_PAUSED", "DELAYED", "STALE_QUOTE", "STALE_MARK"}: status = "BLOCKED_STALE_DATA"
        canonical = {"open_timestamp": material.get("timestamp"), "ohlcv": [material.get(k) for k in ("open","high","low","close","volume")]} if material else None
        return normalize_active({**row, "owner_id": owner, "tenant_id": owner,
                                 "decision_id": row["correlation_id"], "status": status, "signal_direction": direction,
                                 "candle_close_timestamp": close, "decision_timestamp": row.get("created_at"),
                                 "canonical_candle": canonical, "data_freshness": "FRESH" if feed == "SYNCHRONIZED" else "UNKNOWN",
                                 "source_evidence_kind": "FORWARD_PAPER_LAB_DECISION", "source_revision": row.get("updated_at"),
                                 "missing_conditions": missing,
                                 "rejection_reasons": [payload.get("feed_reason")] if status == "BLOCKED_STALE_DATA" and payload.get("feed_reason") else [],
                                 "limitations": ["Full effective configuration / venue / finality / HTF attestation unavailable", *((payload.get("active_evidence") or {}).get("errors") or [])],
                                 "persistence_status": "PENDING_PROJECTION" if (payload.get("active_evidence") or {}).get("configuration_snapshot") else "UNKNOWN"}, source_id=source_id, owner_id=owner)
