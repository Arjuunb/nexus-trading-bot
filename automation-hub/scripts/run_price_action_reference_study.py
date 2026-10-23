#!/usr/bin/env python3
"""Download public research data and run the frozen PA1–PA4 reference ladder."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
REPOSITORY = HUB.parent
for source_root in (REPOSITORY, HUB):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from data.market_data_v2 import MarketDataService  # noqa: E402
from services.price_action_reference_study import (  # noqa: E402
    REFERENCE_TIMEFRAMES, REFERENCE_UNIVERSE, export_reference_artifact,
    run_reference_study,
)
from services.price_action_research import (  # noqa: E402
    PriceActionExperimentRunner, PriceActionExperimentStore,
)
from services.research_funding import HistoricalFundingSeries  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Public-data-only, cost-aware PA1-PA4 walk-forward reference study")
    parser.add_argument("--cache-dir", required=True,
                        help="Durable provider cache directory outside source control")
    parser.add_argument("--research-db", required=True,
                        help="SQLite experiment store outside source control")
    parser.add_argument("--output", required=True,
                        help="Directory for JSON and Markdown research artifacts")
    parser.add_argument("--bars", type=int, default=3000)
    parser.add_argument("--skip-download", action="store_true",
                        help="Use only previously verified cached candles/funding")
    parser.add_argument("--normalized-smc",
                        help="Optional read-only normalized SMC JSON using identical assumptions")
    args = parser.parse_args()
    from services.feature_flags import price_action_lab_enabled
    if not price_action_lab_enabled():
        raise SystemExit('PRICE_ACTION_LAB_DISABLED')
    if not 500 <= args.bars <= 10_000:
        raise SystemExit("--bars must be between 500 and 10000")

    market = MarketDataService(args.cache_dir)
    if not args.skip_download:
        for symbol in REFERENCE_UNIVERSE:
            for timeframe in REFERENCE_TIMEFRAMES:
                # Binance includes the current forming candle. The cache
                # correctly rejects it, so request one extra to retain exactly
                # the declared number of immutable closed candles.
                market.download(symbol, timeframe, candles=args.bars + 1)
    datasets = {}
    for symbol in REFERENCE_UNIVERSE:
        for timeframe in REFERENCE_TIMEFRAMES:
            rows = market.bars(symbol, timeframe, limit=args.bars)
            if len(rows) < args.bars:
                raise SystemExit(
                    f"verified dataset incomplete for {symbol} {timeframe}: {len(rows)}/{args.bars}")
            datasets[(symbol, timeframe)] = rows

    funding = {}
    for symbol in REFERENCE_UNIVERSE:
        related = [rows for (candidate, _), rows in datasets.items() if candidate == symbol]
        start = min(rows[0].timestamp for rows in related)
        end = max(rows[-1].timestamp for rows in related)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        if not args.skip_download:
            market.download_usdm_funding_history(symbol, start_ms=start_ms, end_ms=end_ms)
        records = market.funding_history(symbol, start_ms=start_ms, end_ms=end_ms)
        funding[symbol] = HistoricalFundingSeries.build(
            symbol, records, requested_start=start, requested_end=end)

    store = PriceActionExperimentStore(args.research_db)
    normalized_smc = (json.loads(Path(args.normalized_smc).read_text(encoding="utf-8"))
                      if args.normalized_smc else None)
    artifact = run_reference_study(
        PriceActionExperimentRunner(store), datasets, funding, save=True,
        normalized_smc=normalized_smc)
    paths = export_reference_artifact(artifact, args.output)
    baseline = artifact["baseline"]
    print(json.dumps({
        "artifact_id": artifact["artifact_id"], "research_only": True,
        "real_execution_allowed": False, "dataset_version": baseline["dataset_version"],
        "code_version": baseline["code_version"], "funding_coverage": artifact["funding_coverage"],
        "metrics": baseline["metrics"], "by_partition": baseline["by_partition"],
        "quality_gates": baseline["quality_gates"], "outputs": paths,
        "notice": "Historical performance does not guarantee future results.",
    }, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
