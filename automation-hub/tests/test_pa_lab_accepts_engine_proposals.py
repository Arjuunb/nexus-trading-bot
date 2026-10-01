"""The Price Action lab acts on the proposals its own engine makes.

The engine's visual_state() hands proposals over with ``signal_at`` as a
datetime. The lab accepts a proposal only when it was made on the candle being
evaluated, and used to test that by comparing ``str(signal_at)`` with the
candle's ``isoformat()``. A datetime's str() has a space where isoformat() has
a "T", so the two never matched: every candle with a real proposal was saved
as WATCHING with no proposal ids, and the lab never placed a paper order from
its own strategy, live or in replay.

These tests run the real, frozen engine over deterministic candles through the
lab's replay sequence (process_closed_bar, visual_state, synchronize_strategy).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from services.price_action_lab import PaperExecutionConfig, PriceActionPaperAccount
from tests.test_journal_pa_decisions import RULES, _candles

STRATEGY = "PA1_SR_REJECTION"


def _replay(tmp_path, operating_mode: str, *, serialise: bool = False):
    account = PriceActionPaperAccount(str(tmp_path / f"pa-{operating_mode}-{serialise}.db"))
    account.start(mode="HISTORICAL", symbol="BTCUSDT", timeframe="5m",
                  execution_config=PaperExecutionConfig(operating_mode=operating_mode,
                                                        strategy_id=STRATEGY))
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    bars = _candles(400)
    engine.ingest_closed_bars(bars[:150])
    signalled = []
    for bar in bars[150:]:
        engine.process_closed_bar(bar, market_data_health="HISTORICAL_REPLAY")
        state = engine.visual_state(candle_window=3000)
        if serialise:          # a state that went through JSON, as a snapshot does
            state = json.loads(json.dumps(state, default=str))
        made_now = [row["id"] for row in state["proposals"] if row["strategy_id"] == STRATEGY
                    and str(row["signal_at"]).replace(" ", "T") == bar.timestamp.isoformat()]
        account.synchronize_strategy(state, contract_rules=RULES, candle=bar, feed_reliable=True,
                                     feed_status={"state": "HISTORICAL_REPLAY",
                                                  "health_reason": "verified cached completed candle"})
        if made_now:
            signalled.append((bar, made_now))
    return account, signalled


@pytest.fixture(scope="module")
def replays(tmp_path_factory):
    """Each replay is slow (the real engine, candle by candle), so run each once."""
    cache: dict = {}

    def get(operating_mode: str, *, serialise: bool = False):
        key = (operating_mode, serialise)
        if key not in cache:
            cache[key] = _replay(tmp_path_factory.mktemp("pa"), operating_mode, serialise=serialise)
        return cache[key]
    return get


def _evaluation(account, bar) -> dict:
    row = account._db.execute("SELECT state, payload_json FROM pa_evaluations WHERE candle_time=?",
                              (bar.timestamp.isoformat(),)).fetchone()
    return {"state": row["state"], **json.loads(row["payload_json"])}


def test_the_engine_hands_signal_times_over_as_datetimes():
    # The premise: if the engine ever serialises signal_at itself, this file's
    # regression is moot and should be revisited, not silently pass.
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    bars = _candles(400)
    engine.ingest_closed_bars(bars[:150])
    for bar in bars[150:]:
        engine.process_closed_bar(bar, market_data_health="HISTORICAL_REPLAY")
    proposals = engine.visual_state(candle_window=3000)["proposals"]
    assert proposals and all(isinstance(row["signal_at"], datetime) for row in proposals)


def test_the_lab_accepts_the_proposal_its_engine_made_on_the_candle(replays):
    account, signalled = replays("signals_only")
    assert len(signalled) >= 5                                    # the real engine proposed
    for bar, made_now in signalled:
        evaluation = _evaluation(account, bar)
        assert set(made_now) <= set(evaluation["proposal_ids"]), bar.timestamp
        assert evaluation["state"] == "SIGNAL_FOUND"
    candidates = {row["source_proposal_id"]: row["status"] for row in account._db.execute(
        "SELECT source_proposal_id, status FROM pa_candidates")}
    assert set(candidates) == {pid for _, ids in signalled for pid in ids}
    assert set(candidates.values()) == {"SIGNAL_ONLY"}            # signals-only never orders


def test_in_automatic_mode_the_lab_places_paper_orders_from_its_own_strategy(replays):
    account, signalled = replays("automatic")
    orders = account._db.execute("SELECT proposal_id FROM pa_order_meta").fetchall()
    assert orders, "no paper order from the lab's own strategy"
    assert {row["proposal_id"] for row in orders} <= {pid for _, ids in signalled for pid in ids}
    assert account.export_session()["real_execution_allowed"] is False   # paper only


def test_a_proposal_from_another_candle_is_still_not_accepted(replays):
    # Only the candle a proposal was made on may act on it: replayed history
    # must never become a new order.
    account, signalled = replays("signals_only")
    for bar, made_now in signalled:
        evaluation = _evaluation(account, bar)
        assert set(evaluation["proposal_ids"]) == set(made_now), bar.timestamp


def test_a_state_that_went_through_json_is_accepted_too(replays):
    account, signalled = replays("signals_only", serialise=True)
    assert signalled
    for bar, made_now in signalled:
        assert set(made_now) <= set(_evaluation(account, bar)["proposal_ids"])


def test_the_candle_time_is_compared_as_an_instant_not_as_text(tmp_path):
    account = PriceActionPaperAccount(str(tmp_path / "pa.db"))
    account.start(mode="HISTORICAL", symbol="BTCUSDT", timeframe="5m",
                  execution_config=PaperExecutionConfig(operating_mode="signals_only",
                                                        strategy_id=STRATEGY))
    bar = _candles(1)[0]
    when = bar.timestamp
    spellings = [when, when.isoformat(), str(when), when.isoformat().replace("+00:00", "Z")]
    for n, spelling in enumerate(spellings):
        state = {"proposals": [{"id": f"p{n}", "strategy_id": STRATEGY, "signal_at": spelling}],
                 "snapshot": {}}
        account._db.execute("DELETE FROM pa_evaluations")
        evaluation = account.record_evaluation(state, bar, {"state": "HISTORICAL_REPLAY"})
        assert json.loads(evaluation["payload_json"])["proposal_ids"] == [f"p{n}"], repr(spelling)
    for other in [datetime(2026, 3, 2, 0, 5, tzinfo=timezone.utc), "not a time"]:
        state = {"proposals": [{"id": "late", "strategy_id": STRATEGY, "signal_at": other}],
                 "snapshot": {}}
        account._db.execute("DELETE FROM pa_evaluations")
        evaluation = account.record_evaluation(state, bar, {"state": "HISTORICAL_REPLAY"})
        assert json.loads(evaluation["payload_json"])["proposal_ids"] == [], repr(other)
