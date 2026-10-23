"""Deterministic weekly findings, never configuration changes or research jobs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from services.historical_forward_evidence import timestamp, number
from services.strategy_decay import calculate_decay, digest, financial_summary, text


def completed_week(as_of):
    at = timestamp(as_of)
    if at is None:
        raise ValueError("aware as-of timestamp required")
    monday = at.date()-timedelta(days=at.weekday())
    return (monday-timedelta(days=7)).isoformat()


def weekly_policy():
    p = {"version":"weekly_strategy_research.v1", "timezone":"UTC", "week_start":"MONDAY",
         "minimum_pattern_episodes":20, "loss_cluster_minimum":3,
         "schedule":"after_completed_week_on_next_successful_evidence_refresh",
         "proposal_status":"PENDING_APPROVAL", "automatic_execution":False}
    return {**p,"fingerprint":digest(p)}


def review_week(week_start, as_of):
    value=week_start or completed_week(as_of)
    try:
        start=datetime.strptime(value,"%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):
        raise ValueError("week_start requires a Monday ISO date") from None
    at=timestamp(as_of)
    if start.weekday()!=0 or at is None or start+timedelta(days=7)>at:
        raise ValueError("only completed Monday-to-Monday UTC weeks may be reviewed")
    return start.date().isoformat()


def build_weekly_review(bound, *, week_start, as_of):
    with localcontext() as precision:
        precision.prec=50
        return _build(bound,week_start,as_of)


def _build(bound, week_start, as_of):
    at = timestamp(as_of)
    start = datetime.fromisoformat(review_week(week_start,as_of)).replace(tzinfo=timezone.utc)
    end = start+timedelta(days=7)
    if start.weekday()!=0 or at is None or end>at:
        raise ValueError("only completed Monday-to-Monday UTC weeks may be reviewed")
    if any(timestamp(r["closed_at"])>at for r in bound["episodes"]):
        raise ValueError("future episode evidence cannot enter a weekly review")
    policy=weekly_policy()
    rows=sorted(bound["episodes"],key=lambda r:(timestamp(r["closed_at"]),r["episode_id"]))
    current=[r for r in rows if start<=timestamp(r["closed_at"])<end]
    prior=[r for r in rows if start-timedelta(days=7)<=timestamp(r["closed_at"])<start]
    period=lambda subset:{"episode_ids":[r["episode_id"] for r in subset],"metrics":financial_summary(subset)}
    c,p=period(current),period(prior)
    eligible=len(current)>=policy["minimum_pattern_episodes"] and len(prior)>=policy["minimum_pattern_episodes"]
    change=(number(c["metrics"]["expectancy_net_r"])-number(p["metrics"]["expectancy_net_r"])) if eligible else None
    grouped={}
    for dimension in ("symbol","direction","entry_timeframe","session","trend_regime","volatility_regime","setup","exit_reason"):
        buckets={}
        for row in current:
            context=bound["contexts"].get(row["episode_id"]) or {}
            value=row.get(dimension) if dimension in ("symbol","setup","exit_reason","direction") else context.get(dimension)
            if dimension=="direction": value={"BUY":"LONG","SELL":"SHORT","long":"LONG","short":"SHORT","LONG":"LONG","SHORT":"SHORT"}.get(value)
            label=value if isinstance(value,str) and value else "UNKNOWN"
            buckets.setdefault(label,[]).append(row)
        grouped[dimension]=[{"value":key,**period(subset),"pattern_sample_sufficient":len(subset)>=policy["minimum_pattern_episodes"],
                             "statistical_proof_of_profitability":False} for key,subset in sorted(buckets.items())]
    observations={}
    for field in ("planned_rr","mae_r","mfe_r"):
        values=[number(r.get(field)) for r in current]
        known=[v for v in values if v is not None]
        complete=bool(current) and len(known)==len(current)
        observations[field]={"status":"COMPLETE" if complete else "PARTIAL" if known else "UNKNOWN",
            "sample_size":len(known),"total_episodes":len(current),
            "mean":text(sum(known,Decimal(0))/len(known)) if complete else None,
            "known_mean":text(sum(known,Decimal(0))/len(known)) if known else None}
    actual=number(c["metrics"]["expectancy_net_r"])
    planned=number(observations["planned_rr"]["mean"])
    observations["planned_vs_achieved_rr"]={"planned_mean":observations["planned_rr"]["mean"],
        "achieved_net_r_mean":c["metrics"]["expectancy_net_r"],
        "difference_r":text(actual-planned) if actual is not None and planned is not None else None,
        "note":"Planned reward/risk targets and booked mean net R describe different quantities."}
    clusters=[]; streak=[]
    for row in current+[None]:
        if row is not None and number(row["net_pnl"])<0:
            streak.append(row["episode_id"])
        else:
            if len(streak)>=policy["loss_cluster_minimum"]:
                clusters.append({"length":len(streak),"episode_ids":streak})
            streak=[]
    findings=[]
    if change is not None and change<0:
        findings.append({"kind":"WEEKLY_EXPECTANCY_DECLINE", "expectancy_change_r":text(change),
                         "episode_ids":c["episode_ids"]+p["episode_ids"]})
    for cluster in clusters:
        findings.append({"kind":"CONSECUTIVE_BOOKED_LOSSES",**cluster})
    for dimension in ("session","trend_regime","direction"):
        for group in grouped[dimension]:
            if group["value"]!="UNKNOWN" and group["pattern_sample_sufficient"] and number(group["metrics"]["expectancy_net_r"])<0:
                findings.append({"kind":"NEGATIVE_OBSERVED_SUBGROUP","dimension":dimension,"value":group["value"],"episode_ids":group["episode_ids"]})
    # Causal rolling state at the end of this week excludes all later outcomes.
    causal={**bound,"episodes":[r for r in rows if timestamp(r["closed_at"])<end]}
    decay=calculate_decay(causal,as_of=end.isoformat())
    proposals=[{"proposal_id":"weekly-proposal:"+digest(f), "status":"PENDING_APPROVAL","auto_apply":False,
        "question":"Investigate this observed pattern using a separately versioned, independently tested research candidate.",
        "finding":f["kind"],"episode_ids":f["episode_ids"],
        "requirements":["Preserve the current frozen strategy.","Separate historical, untouched OOS and forward-paper validation.",
                        "Explicit approval before any promotion or execution change."]} for f in findings]
    references=[]; known=0
    for row in current+prior:
        ids=row.get("trade_ids")
        valid=isinstance(ids,list) and bool(ids) and all(isinstance(v,str) and v for v in ids) and len(ids)==len(set(ids))
        known+=bool(valid)
        references.append({"episode_id":row["episode_id"],"trade_ids":list(ids) if valid else [],
                           "status":"OBSERVED" if valid else "UNKNOWN"})
    trade_ids=sorted({tid for ref in references for tid in ref["trade_ids"]})
    result={"contract_version":policy["version"],"identity":bound["identity"],"policy":policy,
        "status":"COMPLETE" if current else "NO_EPISODES", "period":{"start":start.isoformat(),"end":end.isoformat(),"timezone":"UTC"},
        "current":c,"previous":p,"comparison":{"status":"COMPARABLE" if eligible else "INSUFFICIENT_EVIDENCE",
            "expectancy_change_r":text(change) if change is not None else None},
        "groups":grouped,"observations":observations,"losing_clusters":clusters,"findings":findings,"proposals":proposals,
        "review_episode_ids":c["episode_ids"]+p["episode_ids"],"review_trade_ids":trade_ids,
        "episode_trade_references":references,
        "trade_reference_coverage":{"status":"COMPLETE" if references and known==len(references) else "PARTIAL" if known else "UNKNOWN",
                                    "known_episodes":known,"reviewed_episodes":len(references)},
        "decay":decay,"lineage":bound["lineage"],
        "missed_opportunities":{"status":"UNKNOWN","reason":"Executed episodes cannot prove outcomes for rejected or missed entries."},
        "violations":{"status":"UNKNOWN","reason":"No complete independently recorded violation dataset is bound to this review."},
        "execution_quality":{"status":"FINANCIAL_COSTS_OBSERVED","fees":c["metrics"]["fees"],
            "funding":c["metrics"]["funding"],"slippage":c["metrics"]["slippage"],"latency_status":"UNKNOWN"},
        "context_unknown_episodes":sum(bound["contexts"].get(r["episode_id"]) is None for r in current),
        "advisory_only":True,"orders_allowed":False,"strategy_changes_allowed":False,
        "limitations":["Deterministic observations and hypotheses, not causal explanations or proof of future profit.",
            "Unknown observations stay unknown; executed episodes cannot measure missed opportunity profits.",
            "UTC calendar weeks are half-open by actual final close time; partial exits are not separate trades.",
            "Subgroups have their own sample sizes; correlated observations are not independent proof."]}
    result["review_id"]="weekly-review:"+digest(result)
    return result
