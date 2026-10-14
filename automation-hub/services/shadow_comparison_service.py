"""Versioned forward DECISION comparisons, never trade or profitability metrics."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import re

from bot.data.resample import TF_SECONDS
from services.shadow_router import utc
from services.strategy_identity import configuration_fingerprint as digest

VERSION = "shadow_comparison.v2"
ELIGIBLE = {"SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL"}
SIGNALS = {"SIGNAL_LONG", "SIGNAL_SHORT"}


def _latencies(values):
    selected=sorted(v for v in values if isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0)
    return {"sample_count":len(selected),"mean":sum(selected)/len(selected) if selected else None,
            "max":selected[-1] if selected else None,"p95":selected[math.ceil(.95*len(selected))-1] if selected else None}


def _close_to_commit(record):
    try: return (utc(record["evidence_commit_recorded_at"])-utc(record["candle_close_timestamp"])).total_seconds()*1000
    except (KeyError,ValueError,TypeError): return None


def canonical_candle(record):
    material = record.get("canonical_candle")
    if not isinstance(material, dict): return None
    try:
        at = utc(material["open_timestamp"])
        seconds = TF_SECONDS[record["timeframe"]]
        values = material["ohlcv"]
        if len(values) != 5 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values): return None
        o, h, l, c, v = values
        if min(o,h,l,c) <= 0 or v < 0 or h < max(o,c,l) or l > min(o,c) or at.timestamp() % seconds: return None
        if at + timedelta(seconds=seconds) != utc(record["candle_close_timestamp"]): return None
        return {"open_timestamp": at.isoformat(), "ohlcv": values}
    except (KeyError, ValueError, TypeError): return None


def _metrics(pairs):
    counts = Counter()
    for a, s in pairs:
        if a == s: counts[{"SIGNAL_LONG":"both_long","SIGNAL_SHORT":"both_short","NO_SIGNAL":"both_no_signal"}[a]] += 1
        elif a in SIGNALS and s in SIGNALS: counts["opposite_signals"] += 1
        elif a in SIGNALS: counts["active_only_signals"] += 1
        else: counts["shadow_only_signals"] += 1
    n = len(pairs); relevant = n - counts["both_no_signal"]
    agreement = counts["both_long"] + counts["both_short"] + counts["both_no_signal"]
    result = {key: counts[key] for key in ("both_long", "both_short", "both_no_signal", "opposite_signals", "active_only_signals", "shadow_only_signals")}
    result.update(matched_opportunities=n, all_eligible_denominator=n, signal_relevant_denominator=relevant,
                  all_eligible_agreement=agreement/n if n else None,
                  all_eligible_disagreement=(n-agreement)/n if n else None,
                  signal_relevant_agreement=(counts["both_long"]+counts["both_short"])/relevant if relevant else None,
                  signal_relevant_disagreement=(relevant-counts["both_long"]-counts["both_short"])/relevant if relevant else None)
    return result


def _record_errors(record, side, owner):
    errors = []
    if record.get("owner_id") != owner or record.get("tenant_id") != owner: errors.append("TENANT_MISMATCH")
    expected_kind = "ACTIVE_DECISION_OBSERVATION" if side == "ACTIVE" else "FORWARD_SHADOW_DECISION"
    if record.get("evidence_kind") != expected_kind or (side == "ACTIVE" and record.get("source_evidence_kind") not in {None,"FORWARD_PAPER_LAB_DECISION","FORWARD_PAPER_SIGNAL_DECISION","LIVE_STRATEGY_DECISION"}): errors.append(side+"_EVIDENCE_CATEGORY_MISMATCH")
    if record.get("status") not in ELIGIBLE: errors.append(side + "_" + str(record.get("status") or "UNKNOWN"))
    for field, expected, reason in (("data_freshness", "FRESH", "FRESHNESS_UNVERIFIED"),
                                    ("finalized", True, "FINALITY_UNVERIFIED"),
                                    ("htf_alignment", "VERIFIED", "HTF_UNVERIFIED"),
                                    ("lookback_status", "VERIFIED", "LOOKBACK_UNVERIFIED"),
                                    ("observation_authorized", True, "AUTHORIZATION_UNVERIFIED"),
                                    ("evidence_completeness", "COMPLETE", "EVIDENCE_INCOMPLETE")):
        if (record.get(field) is not True if expected is True else record.get(field) != expected):
            errors.append(side + "_" + reason)
    if not record.get("strategy_id") or not record.get("strategy_version"): errors.append(side+"_STRATEGY_IDENTITY_UNVERIFIED")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("config_fingerprint") or "")): errors.append(side+"_CONFIGURATION_UNVERIFIED")
    if not record.get("context_hash"): errors.append(side+"_CONTEXT_IDENTITY_UNVERIFIED")
    try:
        if utc(record["decision_timestamp"]) < utc(record["candle_close_timestamp"]): errors.append(side+"_DECISION_BEFORE_CLOSE")
    except (KeyError, ValueError, TypeError): errors.append(side+"_DECISION_TIME_UNVERIFIED")
    return errors


def compare_decisions(active, shadow, *, owner_id, start, end, timeframe="5m"):
    """Compare only explicitly eligible opportunities in [start,end).

    Expected counts are a scheduled native clock, NOT a claim of observed market
    availability, uninterrupted authorization or exchange feed uptime.
    """
    start, end = utc(start), utc(end)
    seconds = TF_SECONDS.get(timeframe)
    if seconds is None or end <= start or (end-start).total_seconds() > 7*86400: raise ValueError("INVALID_OBSERVATION_WINDOW")
    first = math.ceil(start.timestamp()/seconds)*seconds
    opportunities = []; at = datetime.fromtimestamp(first, timezone.utc)
    while at < end:
        opportunities.append(at.isoformat()); at += timedelta(seconds=seconds)
    if len(opportunities) > 2016 or len(active) > 2016 or len(shadow) > 2016: raise ValueError("COMPARISON_QUERY_LIMIT")
    def indexed(records):
        found = defaultdict(list)
        for record in records:
            try:
                at = utc(record["candle_close_timestamp"])
                if start <= at < end: found[at.isoformat()].append(record)
            except (KeyError, ValueError, TypeError): continue
        return found
    a_by_time, s_by_time = indexed(active), indexed(shadow)
    pairs, timeline = [], []
    groups = {key: defaultdict(list) for key in ("session", "market_regime", "volatility", "symbol", "timeframe", "signal_direction")}
    exclusions, active_reasons, shadow_reasons, active_missing, shadow_missing = Counter(), Counter(), Counter(), Counter(), Counter()
    missing_active = 0; observed = 0; evaluated = 0
    identities = {(r.get("strategy_id"),r.get("strategy_version"),r.get("config_fingerprint")) for rs in a_by_time.values() for r in rs}
    for timestamp in opportunities:
        aa, ss = a_by_time.get(timestamp, []), s_by_time.get(timestamp, [])
        # Identical re-delivery is one logical observation; a changed fact under
        # the same opportunity cannot be resolved by choosing the newest row.
        aa = list({digest(r):r for r in aa}.values()); ss = list({digest(r):r for r in ss}.values())
        a, s = aa[0] if len(aa)==1 else None, ss[0] if len(ss)==1 else None
        reasons = []
        if len(aa)>1: reasons.append("CONFLICTING_ACTIVE_DECISIONS")
        if len(ss)>1: reasons.append("CONFLICTING_SHADOW_DECISIONS")
        if not aa: reasons.append("NO_DECISION_RECORDED"); missing_active += 1
        if not ss: reasons.append("NO_SHADOW_DECISION_RECORDED")
        if len(identities)>1: reasons.append("ACTIVE_CONFIGURATION_CHANGED_WITHIN_WINDOW")
        if a: reasons += _record_errors(a, "ACTIVE", owner_id)
        if s:
            shadow_errors = _record_errors(s, "SHADOW", owner_id)
            reasons += shadow_errors
            observed += int(s.get("finalized") is True and canonical_candle(s) is not None)
            evaluated += int(not shadow_errors and canonical_candle(s) is not None)
        if a and s:
            for key in ("venue", "market_type", "symbol", "timeframe"):
                if not a.get(key) or not s.get(key) or a[key] != s[key]: reasons.append("SCOPE_MISMATCH:"+key)
            ca, cs = canonical_candle(a), canonical_candle(s)
            if ca is None or cs is None: reasons.append("CANDLE_IDENTITY_UNVERIFIED")
            elif digest(ca) != digest(cs): reasons.append("CANDLE_DATA_CONFLICT")
        reasons = sorted(set(reasons)); exclusions.update(reasons)
        for record, rc, mc in ((a,active_reasons,active_missing),(s,shadow_reasons,shadow_missing)):
            if record: rc.update(str(x) for x in record.get("rejection_reasons") or []); mc.update(str(x) for x in record.get("missing_conditions") or [])
        if not reasons:
            pair = (a["status"], s["status"]); pairs.append(pair)
            ac, sc = a.get("market_context") or {}, s.get("market_context") or {}
            for dimension, field in (("session","session"),("market_regime","trend_regime"),("volatility","volatility_regime")):
                value = sc.get(field) or "UNKNOWN"
                if ac.get(field) and ac[field] != value: value = "CONTEXT_CLASSIFICATION_MISMATCH"
                groups[dimension][value].append(pair)
            groups["symbol"][s["symbol"]].append(pair); groups["timeframe"][s["timeframe"]].append(pair)
            groups["signal_direction"][a["status"]+" / "+s["status"]].append(pair)
        timeline.append({"candle_close_timestamp":timestamp, "active_status":a["status"] if a else "UNKNOWN" if aa else "NO_DECISION_RECORDED",
                         "shadow_status":s["status"] if s else "UNKNOWN" if ss else "NO_DECISION_RECORDED",
                         "active_decision_id":a.get("decision_id") if a else None, "shadow_decision_id":s.get("decision_id") if s else None,
                         "eligible":not reasons, "exclusion_reasons":reasons})
    n = len(opportunities); metrics = _metrics(pairs)
    metrics.update(excluded_opportunities=n-len(pairs), missing_active_decisions=missing_active,
                   missing_decision_denominator=n, missing_decision_rate=missing_active/n if n else None)
    return {"contract_version":VERSION, "eligibility":"COMPARABLE" if n and len(pairs)==n else "PARTIALLY_COMPARABLE" if pairs else "NOT_COMPARABLE" if exclusions else "UNKNOWN",
            "evidence_completeness":"COMPLETE" if n and len(pairs)==n else "PARTIAL" if active or shadow else "UNKNOWN",
            "metrics":metrics, "timeline":timeline, "exclusion_reasons":dict(exclusions),
            "coverage":{"expected_scheduled_opportunities":n, "observed_finalized_candles":observed,
                        "successfully_evaluated_shadow_opportunities":evaluated,
                        "missing_shadow_opportunities":sum(not s_by_time.get(t) for t in opportunities),
                        "market_data_coverage":observed/n if n else None, "shadow_evaluation_coverage":evaluated/n if n else None,
                        "active_decision_coverage":(n-missing_active)/n if n else None,
                        "matched_comparison_coverage":len(pairs)/n if n else None,
                        "denominator_basis":"REQUESTED_NATIVE_CANDLE_CLOCK; PAUSED/OFFLINE PERIODS REMAIN VISIBLE",
                        "market_data_basis":"FINALIZED_SNAPSHOTS_RECORDED_BY_SHADOW; NOT RAW_FEED_UPTIME"},
            "breakdowns":{key:{value:_metrics(rows) for value,rows in grouped.items()} for key,grouped in groups.items()},
            "rejection_reasons":{"active":dict(active_reasons),"shadow":dict(shadow_reasons)},
            "missing_conditions":{"active":dict(active_missing),"shadow":dict(shadow_missing)},
            "profitability":"NOT_ASSESSED", "warnings":["Signal agreement and frequency do not establish trading performance or causation.", "Unknown regime/volatility is not a classified subgroup."]}


class ShadowComparisonService:
    def __init__(self, store, *, source_reader, clock=None):
        self.store = store; self.source_reader = source_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get(self, comparison_id, *, owner_id):
        return self.store.comparison_artifact(comparison_id, owner_id=owner_id)

    def list(self, *, owner_id, limit=50, offset=0):
        return self.store.comparison_artifacts(owner_id=owner_id, limit=limit, offset=offset)

    def create(self, request, *, owner_id):
        allowed = {"registration_id","active_source_id","start","end","idempotency_key"}
        if set(request) != allowed or not 8 <= len(str(request.get("idempotency_key") or "")) <= 160: raise ValueError("INVALID_COMPARISON_REQUEST")
        frozen = {k:request[k] for k in allowed if k != "idempotency_key"}
        start,end = utc(frozen["start"]),utc(frozen["end"])
        frozen.update(start=start.isoformat(),end=end.isoformat(),calculation_version=VERSION)
        existing = self.store.comparison_by_key(owner_id=owner_id, key=request["idempotency_key"], request=frozen)
        if existing: return existing
        reg = self.store.get(frozen["registration_id"], owner_id=owner_id)
        if not reg: raise KeyError(frozen["registration_id"])
        if end > self.clock() or end <= start or end-start > timedelta(days=7): raise ValueError("INVALID_OBSERVATION_WINDOW")
        source, active = self.source_reader(frozen["active_source_id"], owner_id=owner_id, start=start, end=end, limit=2016)
        shadow, events = self.store.comparison_inputs(reg["registration_id"], owner_id=owner_id, start=start, end=end)
        result = compare_decisions(active,shadow,owner_id=owner_id,start=start,end=end,timeframe=reg["timeframe"])
        if source["coverage_class"] != "FULL_DECISION_EVIDENCE":
            result["warnings"].extend(source.get("limitations") or [])
            if result["eligibility"] == "COMPARABLE": result["eligibility"] = "PARTIALLY_COMPARABLE"
            result["evidence_completeness"] = "PARTIAL"
        # Retain exact normalized source inputs: mutable upstream lab rows
        # cannot silently rewrite this artifact or its reproduced calculation.
        inputs = {"active":active,"shadow":shadow,"audit_events":events}
        result.update(**frozen,owner_id=owner_id,tenant_id=owner_id,active_source=source,
                      shadow_strategy={k:reg.get(k) for k in ("strategy_id","strategy_version","config_fingerprint","candidate_id")},
                      inputs=inputs,input_watermark=digest(inputs),created_at=self.clock().isoformat(),
                      source_snapshot_semantics="SEPARATE_BOUNDED_READ_TRANSACTIONS; NOT A CROSS_STORE_ATOMIC_SNAPSHOT")
        if any(e["action"]=="CANDLE_REVISION_CONFLICT" for e in events):
            result.update(eligibility="NOT_COMPARABLE", evidence_completeness="CONFLICTED")
            result["warnings"].append("Unresolved shadow candle revision conflict; all agreement metrics withheld")
            result["metrics"] = _metrics([])
            result["metrics"].update(excluded_opportunities=len(result["timeline"]), missing_active_decisions=sum(row["active_status"]=="NO_DECISION_RECORDED" for row in result["timeline"]), missing_decision_denominator=len(result["timeline"]), missing_decision_rate=None)
            result["breakdowns"] = {}
            result["coverage"]["matched_comparison_coverage"] = 0 if result["timeline"] else None
            for row in result["timeline"]:
                row["eligible"] = False
                row["exclusion_reasons"] = sorted(set([*row["exclusion_reasons"],"UNRESOLVED_CANDLE_REVISION_CONFLICT"]))
        result["reliability"] = {"gap_events":sum(e["action"]=="OBSERVATION_GAP" for e in events),
                                 "persistence_failures":sum(e["action"]=="OBSERVATION_FAILURE" for e in events),
                                 "duplicate_events":sum(e["action"]=="REPLAY_IGNORED" for e in events),
                                 "worker_attachments":sum(e["action"]=="WORKER_ATTACHED" for e in events),
                                 "continuous_feed_uptime":None, "exchange_to_system_latency_ms":None,
                                 "shadow_evaluation_ms":_latencies(d.get("evaluation_latency_ms") for d in shadow),
                                 "active_evaluation_ms":_latencies(d.get("evaluation_latency_ms") for d in active),
                                 "shadow_close_to_commit_ms":_latencies(_close_to_commit(d) for d in shadow),
                                 "clock_semantics":"UTC_HOST_WALL_CLOCK; EVALUATION_DURATION_MONOTONIC; SKEW_UNMEASURED",
                                 "audit_scope":"ALL_RETAINED_REGISTRATION_EVENTS; NOT WINDOW UPTIME OR DURATION"}
        identity = {**frozen,"owner_id":owner_id,"input_watermark":result["input_watermark"],"active_source":source}
        result["comparison_id"] = "shadow-comparison:"+digest(identity)
        return self.store.commit_comparison(result, request=frozen, key=request["idempotency_key"])
