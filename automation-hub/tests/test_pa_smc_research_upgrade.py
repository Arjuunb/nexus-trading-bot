from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.types import Bar
from execution.paper_broker_v2 import PaperBrokerV2
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.price_action_lab import PaperExecutionConfig
from services.research_analytics import filter_verdict, validation_label
from services.research_context import CausalHTFContext, NamedLiquidityBook, session_tag, stable_hash
from services.research_observer import ResearchObservationRuntime
from services.research_variants import ShadowVariantRunner, registry_payload
from services.shadow_research import ShadowResearchStore
from services.smc_strategy_lab import SMCPaperConfig


UTC = timezone.utc


class _ResearchFeed:
    def __init__(self, _loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, **_kwargs):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.running = False
        self.reliable = True

    def start(self, _symbol, _timeframe):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED" if self.reliable else "STALE_CANDLES",
                "reliable": self.reliable, "new_entries_paused": not self.reliable}

    def snapshot(self):
        return {"closed_bars": [], "forming": None, "quote": {}}

    def emit_bar(self, bar):
        self.bar_sink(bar)


def _features(**updates):
    base = {
        "symbol": "BTCUSDT", "timeframe": "5m",
        "market_data_fresh": True, "direction": "bullish",
        "sweep": True, "closed_reclaim": True, "htf_aligned": True,
        "displacement": True, "fresh_liquidity": True,
        "full_smc_ready": True, "pa_sr_rejection": True,
        "pa_flip_retest": True, "entry": 100.0, "stop_loss": 95.0,
        "take_profit": 110.0, "order_type": "market",
        "session": "LONDON", "htf": {}, "liquidity": [],
    }
    return {**base, **updates}


def _decision_and_order(store, *, at="2026-09-03T12:05:00+00:00"):
    decision = store.record_decision(
        engine="SMC", account_id="shadow:smc:A", strategy_id="SMC_A_SWEEP",
        strategy_version="1.0.0", config_hash="cfg", candle_id="candle-1",
        action_class="ENTRY", direction="bullish", blocker="SETUP_FOUND",
        decision_timestamp=at, snapshot_lineage="lineage", context={"features": {}},
    )
    order = store.record_order(
        decision["decision_id"], symbol="BTCUSDT", order_type="market", side="buy",
        requested_price=100, stop_loss=95, take_profit=110,
    )
    return decision, order


def test_all_variants_share_candle_and_snapshot_lineage_and_are_idempotent(tmp_path):
    store = ShadowResearchStore(tmp_path / "shadow.db")
    runner = ShadowVariantRunner(store, research_config={"equal_tolerance_atr": None})
    features = _features()
    lineage = stable_hash(features)
    first = runner.evaluate(
        candle_id="BINANCE_USDM:BTCUSDT:5m:1", snapshot_lineage=lineage,
        decision_timestamp="2026-09-03T12:05:00+00:00", features=features)
    second = runner.evaluate(
        candle_id="BINANCE_USDM:BTCUSDT:5m:1", snapshot_lineage=lineage,
        decision_timestamp="2026-09-03T12:05:00+00:00", features=features)
    assert len(first) == len(second) == 9
    assert store.table_counts()["shadow_decisions"] == 9
    assert store.table_counts()["shadow_orders"] == 9
    decisions = store.decisions()
    assert {row["candle_id"] for row in decisions} == {"BINANCE_USDM:BTCUSDT:5m:1"}
    assert {row["snapshot_lineage"] for row in decisions} == {lineage}
    assert {row["execution_class"] for row in decisions} == {"SHADOW"}


def test_observer_uses_shared_closed_feeds_and_stale_state_blocks_entries(tmp_path):
    hub = ForwardPaperMarketDataHub(
        lambda *_args, **_kwargs: [], stream_factory=_ResearchFeed)
    # Assertions here read the observer's state directly after each emit_bar,
    # so delivery must be complete when that call returns. Production dispatches
    # to a pool instead, to keep a slow sink off the socket reader's thread.
    hub.synchronous_delivery = True
    store = ShadowResearchStore(tmp_path / "shadow.db")
    observer = ResearchObservationRuntime(hub, store)
    assert observer.start()
    hub._channels[("BTCUSDT", "1h")].stream.emit_bar(
        Bar(datetime(2026, 9, 3, 10, tzinfo=UTC), 99, 102, 98, 101, 1))
    hub._channels[("BTCUSDT", "4h")].stream.emit_bar(
        Bar(datetime(2026, 9, 3, 8, tzinfo=UTC), 98, 103, 97, 102, 1))
    decision_stream = hub._channels[("BTCUSDT", "5m")].stream
    decision_stream.emit_bar(
        Bar(datetime(2026, 9, 3, 12, 30, tzinfo=UTC), 100, 102, 99, 101, 1))
    first_status = observer.status()
    assert first_status["state"] == "OBSERVING"
    assert len(first_status["last_observation"]["decisions"]) == 9
    assert store.table_counts()["shadow_orders"] == 0

    decision_stream.reliable = False
    decision_stream.emit_bar(
        Bar(datetime(2026, 9, 3, 12, 35, tzinfo=UTC), 101, 103, 100, 102, 1))
    second_status = observer.status()
    assert second_status["state"] == "BLOCKED"
    assert {row["decision"]["blocker"]
            for row in second_status["last_observation"]["decisions"]} == {
                "MARKET_DATA_STALE"
            }
    assert store.table_counts()["shadow_orders"] == 0


def test_snapshot_lineage_mismatch_fails_closed(tmp_path):
    runner = ShadowVariantRunner(ShadowResearchStore(tmp_path / "shadow.db"))
    with pytest.raises(ValueError, match="lineage"):
        runner.evaluate(candle_id="c1", snapshot_lineage="wrong",
                        decision_timestamp="2026-09-03T12:05:00Z",
                        features=_features())


def test_shadow_rejection_is_followed_to_cost_adjusted_outcome(tmp_path):
    store = ShadowResearchStore(tmp_path / "shadow.db")
    runner = ShadowVariantRunner(store)
    features = _features(htf_aligned=False)
    result = runner.evaluate(
        candle_id="candle-rejected", snapshot_lineage=stable_hash(features),
        decision_timestamp="2026-09-03T12:05:00Z", features=features,
        variants=("C",),
    )[0]
    assert result["decision"]["blocker"] == "HTF_MISALIGNED"
    assert result["order"]["status"] == "SHADOW_REJECTED_INTENT"
    quote = {
        "symbol": "BTCUSDT", "bid": 100, "ask": 101, "mark": 100.5,
        "event_timestamp": "2026-09-03T12:05:01Z",
        "received_at": "2026-09-03T12:05:01Z", "sequence": 1,
        "quote_event_id": "rejected-next-quote",
    }
    assert store.record_fill(result["order"]["order_id"], quote,
                             slippage_bps=0, commission_bps=0)
    outcome = store.record_outcome(
        result["order"]["order_id"], {**quote, "bid": 110, "ask": 111,
                                      "event_timestamp": "2026-09-03T12:06:00Z"},
        exit_reason="TARGET", slippage_bps=0, commission_bps=0,
    )
    assert outcome["net_r"] > 0
    measurement = store.measurements()[0]
    assert measurement["blocker"] == "HTF_MISALIGNED"
    assert measurement["execution_class"] == "SHADOW"


def test_shadow_store_cannot_mutate_paper_accounts_positions_or_capacity(tmp_path):
    broker = PaperBrokerV2(tmp_path / "paper.db", starting_balance=10_000)
    before = broker.account()
    store = ShadowResearchStore(tmp_path / "shadow.db")
    _decision_and_order(store)
    after = broker.account()
    assert before == after
    assert broker.positions() == []
    tables = {row[0] for row in sqlite3.connect(store.path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "v2_account" not in tables
    assert "v2_positions" not in tables
    assert not any("margin" in name or "capacity" in name for name in tables)
    with pytest.raises(ValueError, match="SHADOW"):
        store.record_funding(account_id="x", position_id="p",
                             funding_timestamp="2026-09-03T16:00:00Z", amount=1,
                             execution_class="REAL_PAPER")


def test_shadow_next_quote_fill_excursions_funding_and_no_double_spread(tmp_path):
    store = ShadowResearchStore(tmp_path / "shadow.db")
    _, order = _decision_and_order(store)
    equal = {"symbol": "BTCUSDT", "bid": 100, "ask": 101, "mark": 100.5,
             "event_timestamp": "2026-09-03T12:05:00+00:00", "sequence": 1,
             "quote_event_id": "q0", "received_at": "2026-09-03T12:05:00+00:00"}
    assert store.record_fill(order["order_id"], equal, slippage_bps=10,
                             commission_bps=4) is None
    future = {**equal, "event_timestamp": "2026-09-03T12:05:01+00:00",
              "received_at": "2026-09-03T12:05:01+00:00", "sequence": 2,
              "quote_event_id": "q1"}
    fill = store.record_fill(order["order_id"], future, slippage_bps=10,
                             commission_bps=4)
    assert fill and fill["executable_side_price"] == 101
    assert fill["fill_price"] > fill["executable_side_price"]
    assert fill["spread_attribution"] == 1
    assert fill["spread_charged_again"] == 0
    favourable = {**future, "bid": 106, "ask": 107,
                  "event_timestamp": "2026-09-03T12:05:02+00:00", "sequence": 3,
                  "quote_event_id": "q2"}
    adverse = {**future, "bid": 96, "ask": 97,
               "event_timestamp": "2026-09-03T12:05:03+00:00", "sequence": 4,
               "quote_event_id": "q3"}
    assert store.observe_mae_mfe(order["order_id"], favourable)["mfe_price"] == 5
    excursion = store.observe_mae_mfe(order["order_id"], adverse)
    assert excursion["mae_price"] == 5
    assert excursion["mae_r"] == 5 / 6
    first_funding = store.record_funding(
        account_id="shadow:smc:A", position_id=order["order_id"],
        funding_timestamp="2026-09-03T16:00:00Z", amount=.25)
    repeated = store.record_funding(
        account_id="shadow:smc:A", position_id=order["order_id"],
        funding_timestamp="2026-09-03T16:00:00Z", amount=.25)
    assert first_funding["funding_key"] == repeated["funding_key"]
    assert store.table_counts()["shadow_funding"] == 1
    exit_quote = {**future, "bid": 110, "ask": 111,
                  "event_timestamp": "2026-09-03T12:06:00+00:00", "sequence": 5,
                  "quote_event_id": "q4"}
    outcome = store.record_outcome(
        order["order_id"], exit_quote, exit_reason="TARGET",
        slippage_bps=10, commission_bps=4, funding=.25)
    assert outcome["net_pnl"] == pytest.approx(
        outcome["gross_pnl"] - outcome["commission"] - outcome["slippage"] - .25)


def test_out_of_order_public_quote_never_fills_real_paper_intent(tmp_path):
    broker = PaperBrokerV2(tmp_path / "paper.db", fee_rate=0, slippage_bps=0,
                           spread_bps=0, participation_rate=1,
                           account_id="pa:one", account_type="PA_LAB",
                           execution_engine="PA_LAB")
    decision = datetime(2026, 9, 3, 12, 5, tzinfo=UTC)
    metadata = dict(strategy="PA1_SR_REJECTION", strategy_version="1.1.0",
                    candle_id="candle-1", decision_timestamp=decision.isoformat())
    order = broker.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                          quantity=1, limit_price=100, **metadata)
    same = broker.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                         quantity=1, limit_price=100, **metadata)
    assert same["id"] == order["id"]
    newer_no_cross = {"bid": 101, "ask": 102, "mark": 101.5,
                      "event_timestamp": (decision + timedelta(minutes=1)).isoformat(),
                      "received_at": (decision + timedelta(minutes=1)).isoformat(),
                      "sequence": 2, "quote_event_id": "newer"}
    assert broker.process_tick("BTCUSDT", newer_no_cross)["events"] == []
    old_cross = {"bid": 98, "ask": 99, "mark": 98.5,
                 "event_timestamp": (decision + timedelta(seconds=30)).isoformat(),
                 "received_at": (decision + timedelta(minutes=1, seconds=1)).isoformat(),
                 "sequence": 99, "quote_event_id": "older"}
    rejected = broker.process_tick("BTCUSDT", old_cross)
    assert rejected["blocker"] == "OUT_OF_ORDER_QUOTE"
    assert broker.positions() == []
    next_cross = {**old_cross,
                  "event_timestamp": (decision + timedelta(minutes=1, seconds=2)).isoformat(),
                  "received_at": (decision + timedelta(minutes=1, seconds=2)).isoformat(),
                  "sequence": 3, "quote_event_id": "next"}
    assert len(broker.process_tick("BTCUSDT", next_cross)["events"]) == 1
    reopened = PaperBrokerV2(tmp_path / "paper.db", account_id="pa:one",
                             account_type="PA_LAB", execution_engine="PA_LAB")
    assert reopened.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                           quantity=1, limit_price=100, **metadata)["id"] == order["id"]
    assert len(reopened.orders()) == 1
    assert len(reopened.fills()) == 1


def test_causal_htf_never_exposes_a_forming_candle():
    context = CausalHTFContext()
    first = Bar(datetime(2026, 9, 3, 10, tzinfo=UTC), 100, 102, 99, 101, 1)
    second = Bar(datetime(2026, 9, 3, 11, tzinfo=UTC), 101, 103, 100, 102, 1)
    context.ingest("BTCUSDT", "1h", first, "htf-1")
    context.ingest("BTCUSDT", "1h", second, "htf-2")
    assert context.at("BTCUSDT", datetime(2026, 9, 3, 10, 59, tzinfo=UTC))["1h"] is None
    at_first_close = context.at("BTCUSDT", datetime(2026, 9, 3, 11, tzinfo=UTC))["1h"]
    assert at_first_close["candle_id"] == "htf-1"
    assert datetime.fromisoformat(at_first_close["close_timestamp"]) <= datetime(2026, 9, 3, 11, tzinfo=UTC)


def test_london_session_attribution_obeys_uk_dst():
    assert session_tag(datetime(2026, 1, 15, 7, 30, tzinfo=UTC)) == "LONDON"
    assert session_tag(datetime(2026, 7, 15, 6, 30, tzinfo=UTC)) == "LONDON"
    assert session_tag(datetime(2026, 1, 15, 13, 30, tzinfo=UTC)) == "LONDON_NY_OVERLAP"
    assert session_tag(datetime(2026, 7, 15, 12, 30, tzinfo=UTC)) == "LONDON_NY_OVERLAP"


def test_named_liquidity_preserves_origin_and_freezes_optional_equal_threshold():
    book = NamedLiquidityBook(equal_tolerance_atr=None)
    # London is UTC in January, so these bars cross the configured local day.
    first = Bar(datetime(2026, 1, 2, 23, 55, tzinfo=UTC), 100, 105, 95, 101, 1)
    second = Bar(datetime(2026, 1, 3, 0, 0, tzinfo=UTC), 101, 103, 98, 102, 1)
    book.ingest("BTCUSDT", "5m", first, "c1")
    rows = book.ingest("BTCUSDT", "5m", second, "c2")
    assert {row["type"] for row in rows} >= {"PREVIOUS_DAY_HIGH", "PREVIOUS_DAY_LOW"}
    assert not {"EQUAL_HIGHS", "EQUAL_LOWS"} & {row["type"] for row in rows}
    assert all(row["source_candle_id"] for row in rows)
    assert book.config_hash != NamedLiquidityBook(equal_tolerance_atr=.1).config_hash

    sessions = NamedLiquidityBook(equal_tolerance_atr=None)
    asia = Bar(datetime(2026, 1, 3, 6, 55, tzinfo=UTC), 100, 104, 96, 101, 1)
    london = Bar(datetime(2026, 1, 3, 7, 0, tzinfo=UTC), 101, 103, 98, 102, 1)
    sessions.ingest("BTCUSDT", "5m", asia, "asia-extreme")
    session_rows = sessions.ingest("BTCUSDT", "5m", london, "london-open")
    asia_rows = [row for row in session_rows if row["type"].startswith("ASIA_")]
    assert {row["type"] for row in asia_rows} == {"ASIA_HIGH", "ASIA_LOW"}
    assert {row["source_candle_id"] for row in asia_rows} == {"asia-extreme"}


def test_pa_zone_measurements_are_additive_and_do_not_become_gates():
    book = NamedLiquidityBook()
    book.add_zone(
        "BTCUSDT", "5m", price=100, role="support",
        created_at=datetime(2026, 1, 3, 7, tzinfo=UTC),
        source_candle_id="zone-source", atr_width=.4,
        departure_strength_atr=1.7, displacement=False,
        structure_break=True, distance_to_opposing_liquidity=8,
    )
    row = book.snapshot("BTCUSDT", "5m")[0]
    assert row["type"] == "SUPPORT_ZONE"
    assert row["atr_width"] == .4
    assert row["departure_strength_atr"] == 1.7
    assert row["displacement"] is False
    assert row["structure_break"] is True
    assert row["distance_to_opposing_liquidity"] == 8


def test_validation_and_filter_verdicts_are_sample_aware():
    small = {"sample_size": 37, "expectancy_r": .24, "gross_expectancy_r": .3,
             "profit_factor": 1.31}
    promising = {"sample_size": 286, "expectancy_r": .17, "gross_expectancy_r": .25,
                 "profit_factor": 1.24}
    no_edge = {"sample_size": 286, "expectancy_r": -.01, "gross_expectancy_r": .2,
               "profit_factor": .99}
    assert validation_label(small) == "INSUFFICIENT_SAMPLE"
    assert validation_label(promising) == "PROMISING"
    assert validation_label(no_edge) == "NO NET EDGE"
    assert filter_verdict(small, promising) == "INSUFFICIENT_SAMPLE"
    before = {**promising, "expectancy_r": .05}
    assert filter_verdict(before, promising) == "HELPFUL"
    assert filter_verdict(promising, before) == "HARMFUL"


def test_strategy_versions_hashes_blockers_and_defaults_are_explicit():
    registry = registry_payload({"equal_tolerance_atr": None})
    assert len({row["strategy_id"] for row in registry["variants"]}) == 9
    assert len({row["config_hash"] for row in registry["variants"]}) == 9
    assert registry["execution_class"] == "SHADOW"
    # New sessions arm automatic paper placement by deliberate choice; the
    # safety that matters is that neither lab can reach a real venue.
    assert PaperExecutionConfig().operating_mode == "automatic"
    assert SMCPaperConfig().operating_mode == "automatic"


def test_pr6_real_paper_sources_match_the_declared_native_mtf_delta():
    root = Path(__file__).parents[1]
    manifest = json.loads((root / "data/pr6_real_paper_freeze.json").read_text())
    assert manifest["baseline_commit"] == "5b351e9"
    assert manifest["approved_delta"] == (
        "Replace synthesized HTF clocks with services/mtf_policy.py native-candle evidence only.")
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected


def test_research_path_has_no_private_exchange_order_submission():
    root = Path(__file__).parents[1] / "services"
    paths = [root / name for name in (
        "research_context.py", "shadow_research.py", "research_variants.py",
        "research_observer.py", "research_analytics.py")]
    assert all("create_order(" not in path.read_text() for path in paths)
