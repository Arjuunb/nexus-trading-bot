#!/usr/bin/env python3
"""Benchmark pure v2 aggregation using explicitly synthetic in-memory episodes.

No application startup, database connection, financial write or network call.
Results measure this host and selected inputs; they are not a production SLA.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import platform
import sys
import time
import tracemalloc


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.strategy_evidence_completeness import REPORT_VERSION, metrics_episode_watermark
from services.strategy_intelligence_metrics import EvidenceCohort
from services.strategy_intelligence_v2 import calculate_context_performance


COHORT = EvidenceCohort(strategy_id="synthetic_benchmark_strategy", strategy_version="1.0.0",
    config_fingerprint="a" * 64, instance_id="synthetic_benchmark_instance", lab_id="benchmark",
    simulation_session_id="synthetic", execution_mode="replay", source_kind="benchmark_fixture",
    owner_id="synthetic_benchmark_owner", account_id="synthetic_benchmark_account", symbol=None)
CALCULATION_TIME = "2026-10-09T12:00:00Z"


def fixtures(count):
    rows, contexts = [], []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        opened = base + timedelta(minutes=2 * index)
        symbol = "BENCH_ASSET_" + str(index % 20)
        rows.append({**COHORT.as_dict(), "episode_id": str(index), "symbol": symbol,
            "evidence_kind": "executed", "status": "closed", "identity_status": "observed",
            "opened_at": opened.isoformat(), "closed_at": (opened + timedelta(minutes=1)).isoformat(),
            "initial_risk": "10", "net_pnl": "9", "gross_pnl": "10", "fees": "1", "funding": "0",
            "fees_coverage": "BOOKED", "funding_coverage": "VERIFIED_ZERO",
            "slippage": "0", "slippage_coverage": "VERIFIED_ZERO", "direction": "BUY",
            "fees_cost_model": "synthetic_fee_fixture", "funding_cost_model": "synthetic_funding_fixture",
            "slippage_cost_model": "synthetic_slippage_fixture"})
        contexts.append({**COHORT.as_dict(), "episode_id": str(index), "symbol": symbol,
            "entry_timeframe": "15m", "session": ("ASIA", "LONDON", "NEW_YORK", "LONDON_NEW_YORK_OVERLAP", "OFF_SESSION")[index % 5],
            "trend_regime": ("STRONG_BULL", "BULL", "RANGE", "BEAR", "STRONG_BEAR", "UNKNOWN")[index % 6],
            "volatility_regime": "NORMAL", "structure_regime": "UNKNOWN",
            "signal_timestamp": opened.isoformat(), "entry_timestamp": opened.isoformat(),
            "context_quality": "VALID", "classifier_id": "synthetic_benchmark_classifier",
            "classifier_version": "1.0.0", "parameter_hash": "b" * 64, "classification_kind": "ENTRY"})
    report = {"report_version": REPORT_VERSION, "status": "UNKNOWN", "history_complete": False,
        "cohort": COHORT.as_dict(), "source_watermark": "synthetic_benchmark_input",
        "metrics_evidence_watermark": metrics_episode_watermark(rows, COHORT),
        "calculated_at": CALCULATION_TIME, "counts": {},
        "basis": "synthetic_fixture_has_no_authoritative_ledger"}
    return rows, contexts, report


def benchmark(count, *, measure_memory=False):
    rows, contexts, report = fixtures(count)
    if measure_memory:
        tracemalloc.start()
    started = time.perf_counter()
    result = calculate_context_performance(rows, contexts, cohort=COHORT,
        group_by=("symbol", "session", "trend_regime"), source_watermark=report["source_watermark"],
        completeness_report=report, calculation_timestamp=CALCULATION_TIME)
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1] if measure_memory else None
    if measure_memory:
        tracemalloc.stop()
    actual_count = sum(group["metrics"]["trade_count"] for group in result["groups"])
    actual_net = sum((Decimal(group["metrics"]["net_pnl"]) for group in result["groups"]), Decimal(0))
    if actual_count != count or actual_net != Decimal(9 * count):
        raise AssertionError("Synthetic benchmark episode count or Decimal net sum failed")
    if result["profitability_verified"]:
        raise AssertionError("Synthetic fixtures cannot verify trading profitability")
    return {"episodes": count, "groups": len(result["groups"]), "seconds": round(elapsed, 4),
        "peak_mib": round(peak / 1024**2, 2) if peak is not None else None,
        "memory_instrumentation": measure_memory, "episode_counts_reconciled": True,
        "decimal_net_reconciled": True, "ledger_reads": 0, "ledger_writes": 0,
        "financial_evidence": "SYNTHETIC_BENCHMARK_ONLY", "profitability_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--tracemalloc", action="store_true", help="Include traced peak memory; instrumentation adds runtime")
    arguments = parser.parse_args()
    if any(count < 1 or count > 100_000 for count in arguments.episodes):
        parser.error("Each episode count must be from 1 to 100000")
    output = {"benchmark": "pure_context_metrics_v2", "input_kind": "SYNTHETIC_ONLY",
        "python_version": platform.python_version(), "platform": platform.platform(),
        "fixture_setup_included_in_timing": False,
        "groups": ["symbol", "session", "trend_regime"],
        "results": [benchmark(count, measure_memory=arguments.tracemalloc) for count in arguments.episodes]}
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
