"""Non-financial, detached evidence at existing native lab decision boundaries.

The source decision insert is the durable outbox. Receipt projection is optional
and fail-soft; only contemporaneous frozen source material may be replayed.
No strategy evaluation, provider I/O, order interface or account mutation occurs.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3

from bot.data.resample import TF_SECONDS
from services.mtf_policy import policy_for, evidence_at, material_evidence
from services.shadow_router import utc, candle_material
from services.shadow_comparison_service import canonical_candle
from services.strategy_identity import canonical_configuration_json, configuration_fingerprint as digest

log = logging.getLogger(__name__)
SCHEMA = "active-decision.v1"
MAX_BYTES = 512 * 1024


def native_observation_token(engine):
    """Transient race guard, not persisted identity or a lock on execution."""
    return (id(engine.config),len(engine.bars),engine.bars[-1].timestamp,
            id(engine._native_mtf_context)) if engine.bars else None


def _series(rows, tf, cutoff):
    selected = [r for r in rows if utc(r.timestamp)+timedelta(seconds=TF_SECONDS[tf]) <= cutoff]
    material = [candle_material(r) for r in selected[-1500:]]
    errors = []
    stamps = [utc(r["open_timestamp"]) for r in material]
    if len(stamps)!=len(set(stamps)): errors.append("DUPLICATE_CANDLE:"+tf)
    if any(b-a!=timedelta(seconds=TF_SECONDS[tf]) for a,b in zip(stamps,stamps[1:])):
        errors.append("DATA_GAP:"+tf)
    for r in material:
        close=utc(r["open_timestamp"])+timedelta(seconds=TF_SECONDS[tf])
        if canonical_candle({"canonical_candle":r,"timeframe":tf,"candle_close_timestamp":close.isoformat()}) is None:
            errors.append("INVALID_CANDLE:"+tf);break
    return material, errors


def native_input(engine, *, venue, source):
    """Observe the ACTUAL instantiated engine; never rebuild from UI settings.

    Only the two audited native engines are accepted. Future context is excluded
    using the same closed-candle convention as shadow observations. This does
    not change or invoke the engine and does not certify generic SMC shadow.
    """
    try:
        token=native_observation_token(engine)
        implementation=(type(engine).__module__,type(engine).__name__)
        if implementation not in {("services.native_price_action","NativePriceActionEngine"),
                                  ("services.native_smc","SMCMarketStructureEngine")}:
            raise ValueError("UNSUPPORTED_NATIVE_EVALUATOR")
        cfg=asdict(engine.config);tf=cfg["timeframe"]
        primary,secondary=policy_for(tf)
        cutoff=utc(engine.bars[-1].timestamp)+timedelta(seconds=TF_SECONDS[tf])
        # The live engine can retain years of entry bars. Only the bounded
        # declared capture window is copied; larger lookbacks remain unverified.
        context={tf:list(engine.bars[-1500:]),**deepcopy(engine._native_mtf_context)}
        required={tf:max(400,cfg.get("swing_left",0)+cfg.get("swing_right",0)+5,
                         cfg.get("zone_expiry_bars",0)+cfg.get("swing_right",0)+2,
                         cfg.get("event_memory_bars",0)+2,cfg.get("setup_expiry_bars",0)+2,
                         cfg.get("confusion_candles",0)+2,cfg.get("entry_expiry_bars",0)+2,
                         2*cfg.get("swing_pivot_length",0)+1,2*cfg.get("internal_pivot_length",0)+1,
                         cfg.get("sweep_lookback",0)+1,cfg.get("atr_length",0)+21),primary:51}
        if cfg.get("zone_timeframe_scope")=="higher_timeframe": required[primary]=required[tf]
        normalized={};errors=[];warnings=[]
        for clock,rows in context.items():
            if clock not in {tf,primary,secondary}: continue
            material,quality=_series(rows,clock,cutoff)
            # Preserve actual causal input, including invalid optional input;
            # never claim it was excluded by the active evaluator. Absence of
            # optional context is allowed, malformed supplied context is not.
            errors.extend(quality)
            normalized[clock]=material
        for clock,count in required.items():
            rows=normalized.get(clock,[])
            if len(rows)<count: errors.append("INSUFFICIENT_HISTORY:"+clock)
            expected=int(cutoff.timestamp()//TF_SECONDS[clock])*TF_SECONDS[clock]
            if not rows or utc(rows[-1]["open_timestamp"]).timestamp()+TF_SECONDS[clock]!=expected:
                errors.append("MISSING_OR_STALE_CONTEXT:"+clock)
        if venue!="binance_usdm" or source!="BINANCE_USDM_PUBLIC": errors.append("SOURCE_UNVERIFIED")
        # The native SMC source report consumes separately supplied MTF policy
        # evidence. Bind that decision input to the captured native candles;
        # inspecting only the engine's HTF store would miss this loss boundary.
        mtf_material=None
        if not errors:
            causal={clock:[r for r in rows if utc(r.timestamp)+timedelta(seconds=TF_SECONDS[clock])<=cutoff]
                    for clock,rows in context.items() if clock in {primary,secondary}}
            mtf_material=material_evidence(evidence_at(cfg["symbol"],tf,causal,cutoff))
        rules=None
        if implementation[1]=="SMCMarketStructureEngine":
            from services.smc_strategy_v1 import manifest
            rules=manifest();rules.pop("configuration_hash",None)
        market_context={"session":"UNKNOWN","trend_regime":"UNKNOWN","volatility_regime":"UNKNOWN"}
        if implementation[1]=="SMCMarketStructureEngine":
            snapshot=engine.snapshots.get(engine.bars[-1].timestamp)
            if snapshot and snapshot.session:
                market_context["session"]=snapshot.session
        result={"evaluator_implementation":implementation[1],"native_config":cfg,"rule_manifest":rules,"venue":venue,"market_type":"perpetual" if venue=="binance_usdm" else None,
                "source":source,"context_material":normalized,"context_hash":digest(normalized),
                "native_mtf_material":mtf_material,
                "market_context":market_context,
                "htf_context_identity":digest({k:v for k,v in normalized.items() if k!=tf}),
                "canonical_candle":normalized.get(tf,[])[-1] if normalized.get(tf) else None,
                "candle_close_timestamp":cutoff.isoformat(),"required_candles":required,
                "lookback_basis":"REVIEWED_NATIVE_DEPENDENCIES.v1; FINITE_HISTORY_NOT_INFINITE_STATE_REPLAY",
                "htf_alignment":"VERIFIED" if not errors else "UNKNOWN",
                "lookback_status":"VERIFIED" if not errors else "UNKNOWN",
                "availability_assumption":"CLOSED_AT_OR_BEFORE_DECISION; INDIVIDUAL_PUBLICATION_UNKNOWN",
                "history_scope":"LAST_1500_ENTRY_BARS_AND_NATIVE_CONTEXT; NOT FULL_STATE_RECONSTRUCTION",
                "errors":sorted(set(errors)),"warnings":warnings}
        if len(canonical_configuration_json(result).encode())>MAX_BYTES: raise ValueError("CAPTURE_SIZE_LIMIT")
        if token!=native_observation_token(engine):
            result["errors"]=sorted({*result["errors"],"EVALUATOR_STATE_CHANGED_DURING_CAPTURE"})
            result.update(htf_alignment="UNKNOWN",lookback_status="UNKNOWN")
        return json.loads(canonical_configuration_json(result))
    except Exception as exc:
        # Evidence telemetry cannot change strategy decisions. Exception text
        # and raw inputs may contain private data and are never logged here.
        log.warning("active_input_capture_failed",extra={"error_category":type(exc).__name__})
        return {"errors":["NATIVE_INPUT_CAPTURE_UNAVAILABLE"]}


def decision_capture(native, *, owner, session, strategy_id, version, execution_config,
                     status, direction, missing, reasons, decision_id, recorded_at):
    try:
        if not native or not native.get("native_config"):
            return {"schema":SCHEMA,"errors":["CONTEMPORANEOUS_EVALUATOR_INPUT_MISSING"]}
        cfg=native["native_config"]
        if native.get("evaluator_implementation")=="NativePriceActionEngine":
            from services.native_price_action import PriceActionConfig
            from services.price_action_lab import PaperExecutionConfig
            native_cls,execution_cls=PriceActionConfig,PaperExecutionConfig
        elif native.get("evaluator_implementation")=="SMCMarketStructureEngine":
            from services.native_smc import SMCConfig
            from services.smc_strategy_lab import SMCPaperConfig
            native_cls,execution_cls=SMCConfig,SMCPaperConfig
        else: raise ValueError("UNSUPPORTED_EVALUATOR_CONFIGURATION")
        if set(cfg)!=set(native_cls.__dataclass_fields__) or set(execution_config)!=set(execution_cls.__dataclass_fields__):
            raise ValueError("INCOMPLETE_EFFECTIVE_CONFIGURATION")
        errors=list(native.get("errors",[]))
        if not owner: errors.append("TENANT_UNVERIFIED")
        if session.get("mode")!="LIVE_PAPER": errors.append("NOT_FORWARD_ACTIVE_EVIDENCE")
        if session.get("status")!="active":
            errors.append("ACTIVE_SOURCE_NOT_RUNNING")
            status="ACTIVE_PAUSED" if session.get("status")=="paused" else "ACTIVE_STOPPED"
        if (cfg.get("symbol"),cfg.get("timeframe"))!=(session.get("symbol"),session.get("timeframe")):
            errors.append("EVALUATOR_SCOPE_MISMATCH")
        close=utc(native["candle_close_timestamp"]);at=utc(recorded_at)
        if at<close: errors.append("DECISION_BEFORE_CLOSE")
        if at-close>timedelta(seconds=30): errors.append("STALE_DECISION_CANDLE")
        snapshot={"schema":"effective-native-lab-configuration-v1","strategy_id":strategy_id,
                  "strategy_version":version,"native_config":cfg,"execution_config":execution_config,
                  "rule_manifest":native.get("rule_manifest"),
                  "timeframes":{"entry":cfg["timeframe"],"primary":policy_for(cfg["timeframe"])[0],
                                "secondary":policy_for(cfg["timeframe"])[1]}}
        if status=="UNKNOWN": errors.append("DECISION_OUTCOME_UNVERIFIED")
        result={**deepcopy(native),"schema":SCHEMA,"owner_id":owner,"tenant_id":owner,
                "source_session_id":session["id"],"decision_id":decision_id,"strategy_id":strategy_id,
                "strategy_version":version,"symbol":cfg["symbol"],"timeframe":cfg["timeframe"],
                "configuration_snapshot":snapshot,"config_fingerprint":digest(snapshot),
                "configuration_reference":"active-config:"+digest(snapshot),
                "configuration_status":"VERIFIED","decision_timestamp":at.isoformat(),
                "status":status,"signal_direction":direction,"missing_conditions":missing,
                "rejection_reasons":reasons,"finalized":at>=close,
                "observation_authorized":bool(owner and session.get("mode")=="LIVE_PAPER" and session.get("status")=="active"),
                "source_evidence_kind":"FORWARD_PAPER_LAB_DECISION","data_freshness":"FRESH" if not errors else "UNKNOWN",
                "evidence_completeness":"COMPLETE" if not errors else "PARTIAL",
                "market_context":deepcopy(native.get("market_context") or {"session":"UNKNOWN","trend_regime":"UNKNOWN","volatility_regime":"UNKNOWN"}),
                "optional_fields":["session","market_regime","volatility","individual_publication_timestamp"],
                "errors":sorted(set(errors))}
        text=canonical_configuration_json(result)
        if len(text.encode())>MAX_BYTES: raise ValueError("CAPTURE_SIZE_LIMIT")
        return json.loads(text)
    except Exception as exc:
        log.warning("active_decision_capture_failed",extra={"error_category":type(exc).__name__})
        return {"schema":SCHEMA,"errors":["DECISION_CAPTURE_UNAVAILABLE"]}


def install_active_evidence(db, prefix):
    if prefix not in {"pa","smc"}: raise ValueError("UNSUPPORTED_ACTIVE_SOURCE")
    sql=(Path(__file__).resolve().parents[1]/"migrations/active/0001_active_decision_receipts.sql").read_text()
    db.executescript(sql.replace("__PREFIX__",prefix))


def lab_capture(native, *, proposals=None, report=None, feed_status, decision_mtf_evidence=None,
                configuration_consistent=True, **kwargs):
    """Fail-soft evidence preparation including malformed internal metadata."""
    try:
        native=deepcopy(native or {})
        used_mtf=decision_mtf_evidence if report is None else report.get("mtf_evidence")
        native["decision_mtf_material"]=material_evidence(used_mtf) if used_mtf else None
        if not used_mtf:
            native["errors"]=[*native.get("errors",[]),"DECISION_HTF_CONTEXT_UNVERIFIED"]
            native["htf_alignment"]="UNKNOWN"
        elif native.get("native_mtf_material") is None:
            native["errors"]=[*native.get("errors",[]),"DECISION_HTF_CONTEXT_UNVERIFIED"]
            native["htf_alignment"]="UNKNOWN"
        elif native.get("native_mtf_material")!=native["decision_mtf_material"]:
            native["errors"]=[*native.get("errors",[]),"DECISION_HTF_CONTEXT_CONFLICT"]
            native["htf_alignment"]="UNKNOWN"
        if not configuration_consistent:
            native["errors"]=[*native.get("errors",[]),"CONFIGURATION_CHANGED_AT_DECISION_BOUNDARY"]
        if report is None:
            directions={r.get("direction") for r in proposals or []}
            direction={"bullish":"LONG","bearish":"SHORT"}.get(next(iter(directions))) if len(directions)==1 else None
            status="NO_SIGNAL" if not proposals else "SIGNAL_"+direction if direction else "UNKNOWN"
        else:
            evaluated_model=(report.get("model") or {}).get("id")
            configured_model=kwargs["execution_config"].get("model_id")
            if not evaluated_model or evaluated_model!=configured_model:
                # The source evaluator selected its model before the recorder
                # read current lab settings. Do not fingerprint a later model
                # as though it produced this report.
                return {"schema":SCHEMA,"errors":["DECISION_MODEL_CONFIGURATION_UNVERIFIED"],
                        "evaluated_model_id":evaluated_model,"configured_model_id":configured_model}
            direction={"bullish":"LONG","bearish":"SHORT"}.get((report.get("proposal") or {}).get("direction"))
            ready=report.get("state")=="ENTRY_READY" and report.get("proposal") and report.get("trade_plan")
            status="SIGNAL_"+direction if ready and direction else "NO_SIGNAL" if report.get("state")=="WATCHING" else "UNKNOWN"
            if "native closed-bar context" in (report.get("missing_conditions") or []): status="BLOCKED_MISSING_CONTEXT"
        if feed_status.get("state")!="SYNCHRONIZED" or feed_status.get("reliable") is not True:
            native["errors"]=[*native.get("errors",[]),"FEED_FRESHNESS_UNVERIFIED"]
            status="BLOCKED_STALE_DATA"
        return decision_capture(native,status=status,direction=direction,**kwargs)
    except Exception as exc:
        log.warning("active_decision_capture_failed",extra={"error_category":type(exc).__name__})
        return {"schema":SCHEMA,"errors":["DECISION_CAPTURE_UNAVAILABLE"]}


def supersede_active_receipt(db, *, decision_id, replacement_reference, owner, actor, reason, event_id):
    """Trusted, explicit non-financial annotation; never edits either receipt.

    There is no automatic correction or public mutation endpoint. A replacement
    must itself be a committed contemporaneous receipt of the same tenant.
    """
    if not actor or not reason or not 8<=len(event_id)<=160 or decision_id==replacement_reference:
        raise ValueError("INVALID_SUPERSESSION")
    for reference in (decision_id,replacement_reference):
        if not db.execute("SELECT 1 FROM active_decision_receipts WHERE decision_id=? AND tenant_id=?",(reference,owner)).fetchone():
            raise KeyError(reference)
    existing=db.execute("SELECT * FROM active_evidence_supersessions WHERE event_id=?",(event_id,)).fetchone()
    facts=(event_id,decision_id,owner,actor,reason,replacement_reference)
    if existing:
        if tuple(existing)[:6]!=facts: raise ValueError("SUPERSESSION_IDENTITY_CONFLICT")
        return dict(existing)
    db.execute("INSERT INTO active_evidence_supersessions VALUES (?,?,?,?,?,?,?)",(*facts,datetime.now(timezone.utc).isoformat()))
    return dict(db.execute("SELECT * FROM active_evidence_supersessions WHERE event_id=?",(event_id,)).fetchone())


def project_active_decision(db, prefix, row):
    """Exactly-once logical receipt effects. Projection failure never vetoes execution."""
    try:
        payload=json.loads(row["payload_json"])
        capture=payload.get("active_evidence")
        if not capture or capture.get("schema")!=SCHEMA or not capture.get("configuration_snapshot"): return False
        snapshot={key:row[key] for key in ("correlation_id","session_id","candle_time","symbol","timeframe","strategy_id","strategy_version","created_at")}
        snapshot["active_evidence"]=capture
        encoded=canonical_configuration_json(snapshot)
        existing=db.execute("SELECT snapshot_json FROM active_decision_receipts WHERE decision_id=?",(row["correlation_id"],)).fetchone()
        if existing:
            if existing[0]!=encoded: raise ValueError("ACTIVE_RECEIPT_CONFLICT")
            return False
        db.execute("INSERT INTO active_decision_receipts VALUES (?,?,?,?,?,?,?)",
                   (row["correlation_id"],row["session_id"],capture.get("tenant_id"),prefix,
                    row["candle_time"],encoded,datetime.now(timezone.utc).isoformat()))
        return True
    except Exception as exc:
        log.warning("active_receipt_projection_failed",extra={"source":prefix,"decision_id":row.get("correlation_id"),"error_category":type(exc).__name__})
        return False


def reconcile_active_evidence(db, prefix, *, limit=200):
    """Bounded source-owned recovery; reads no current configuration or financial table."""
    if prefix not in {"pa","smc"}: raise ValueError("UNSUPPORTED_ACTIVE_SOURCE")
    limit=min(max(int(limit),1),200)
    try:
        rows=db.execute(f"SELECT e.* FROM {prefix}_evaluations e LEFT JOIN active_decision_receipts r ON r.decision_id=e.correlation_id WHERE r.decision_id IS NULL AND json_valid(e.payload_json) AND json_type(e.payload_json,'$.active_evidence.configuration_snapshot')='object' AND json_extract(e.payload_json,'$.active_evidence.schema')=? ORDER BY e.candle_time LIMIT ?",(SCHEMA,limit))
        recovered=0;size=0
        for r in rows:
            size+=len(r["payload_json"].encode())
            if size>4*1024*1024:
                log.warning("active_evidence_reconciliation_batch_limit",extra={"source":prefix,"limit_bytes":4*1024*1024})
                break
            recovered+=project_active_decision(db,prefix,dict(r))
        backlog=db.execute(f"SELECT COUNT(*) FROM {prefix}_evaluations e LEFT JOIN active_decision_receipts r ON r.decision_id=e.correlation_id WHERE r.decision_id IS NULL AND json_valid(e.payload_json) AND json_type(e.payload_json,'$.active_evidence.configuration_snapshot')='object' AND json_extract(e.payload_json,'$.active_evidence.schema')=?",(SCHEMA,)).fetchone()[0]
        now=datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO active_evidence_recovery VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET last_success_at=CASE WHEN excluded.status='COMPLETE' THEN excluded.last_success_at ELSE active_evidence_recovery.last_success_at END,status=excluded.status,backlog=excluded.backlog",(now if not backlog else None,"RECOVERING" if backlog else "COMPLETE",backlog))
        return {"recovered":recovered,"backlog":backlog,"status":"RECOVERING" if backlog else "COMPLETE"}
    except sqlite3.Error:
        log.warning("active_evidence_reconciliation_unavailable",extra={"source":prefix})
        return {"recovered":0,"backlog":None,"status":"UNKNOWN"}
