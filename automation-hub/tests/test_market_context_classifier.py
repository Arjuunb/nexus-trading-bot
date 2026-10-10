from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.market_context_classifier import (
    CandleObservation, ClassifierParameters, SessionDefinition,
    classifier_definition, classify_frozen_context, classify_market_context,
    classify_session, classify_trend, classify_volatility,
)
from services.strategy_identity import configuration_fingerprint


UTC = timezone.utc
SMALL = ClassifierParameters(ema_period=2, ema_slope_lag=1, adx_period=1,
                             atr_period=1, volatility_window=2, htf_ema_period=2)


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def candles(count=8, *, step=timedelta(minutes=5), start=None, direction=1):
    start = start or stamp("2026-01-01T00:00:00Z")
    result = []
    for i in range(count):
        price = 100 + direction * i * 2
        bar = Bar(start + i * step, price, price + 1, price - 1, price + .5, 10)
        close = bar.timestamp + step
        result.append(CandleObservation(bar, close, True, close))
    return result


def classify(rows=None, **kwargs):
    rows = rows if rows is not None else candles()
    htf = candles(count=3, step=timedelta(hours=1), start=stamp("2025-12-31T21:00:00Z"))
    return classify_market_context(rows, signal_timestamp=stamp("2026-01-01T00:40:00Z"),
                                  entry_timeframe="5m", higher_timeframe="1h",
                                  higher_candles=htf, market_data_source="test observed provider",
                                  classification_timestamp=stamp("2026-01-02T00:00:00Z"),
                                  parameters=SMALL, **kwargs)


@pytest.mark.parametrize("at,expected", [
    ("2026-01-01T00:00:00Z", "ASIA"),
    ("2026-01-01T07:59:59Z", "ASIA"),
    ("2026-01-01T08:00:00Z", "LONDON"),
    ("2026-01-01T12:59:59Z", "LONDON"),
    ("2026-01-01T13:00:00Z", "LONDON_NEW_YORK_OVERLAP"),
    ("2026-01-01T17:00:00Z", "NEW_YORK"),
    ("2026-01-01T22:00:00Z", "OFF_SESSION"),
    ("2026-07-01T07:00:00Z", "LONDON"),
    ("2026-07-01T12:00:00Z", "LONDON_NEW_YORK_OVERLAP"),
    ("2026-03-15T12:00:00Z", "LONDON_NEW_YORK_OVERLAP"),
    ("2026-03-29T07:00:00Z", "LONDON"),
    ("2026-10-28T12:00:00Z", "LONDON_NEW_YORK_OVERLAP"),
    ("2026-11-04T12:00:00Z", "LONDON"),
])
def test_session_boundaries_and_seasonal_dst(at, expected):
    assert classify_session(stamp(at))["session"] == expected


def test_overlap_has_one_canonical_session_and_active_tags():
    result = classify_session(stamp("2026-01-01T13:00:00Z"))
    assert result == {"session": "LONDON_NEW_YORK_OVERLAP", "active_sessions": ["LONDON", "NEW_YORK"]}


def test_overnight_session_and_start_day_weekday():
    session = SessionDefinition("ASIA", "Asia/Tokyo", "22:00", "02:00", (0,))
    assert classify_session(stamp("2026-01-05T14:00:00Z"), sessions=(session,))["session"] == "ASIA"
    assert classify_session(stamp("2026-01-05T16:59:59Z"), sessions=(session,))["session"] == "ASIA"
    assert classify_session(stamp("2026-01-05T17:00:00Z"), sessions=(session,))["session"] == "OFF_SESSION"
    assert classify_session(stamp("2026-01-06T14:00:00Z"), sessions=(session,))["session"] == "OFF_SESSION"


@pytest.mark.parametrize("at", ["2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z", "2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"])
def test_dst_fold_and_gap_are_deterministic_from_utc(at):
    session = SessionDefinition("NEW_YORK", "America/New_York", "01:00", "04:00")
    assert classify_session(stamp(at), sessions=(session,))["session"] == "NEW_YORK"


@pytest.mark.parametrize("kwargs,expected", [
    ({"adx_value": 19.999}, "RANGE"),
    ({"adx_value": 20, "normalized_slope": .05}, "BULL"),
    ({"adx_value": 25, "normalized_slope": .2}, "STRONG_BULL"),
    ({"adx_value": 25, "normalized_slope": .2, "htf_direction": -1}, "BULL"),
    ({"adx_value": 25, "normalized_slope": -.2, "price_bias": -1, "htf_direction": -1}, "STRONG_BEAR"),
    ({"adx_value": 20, "normalized_slope": -.05, "price_bias": -1, "htf_direction": -1}, "BEAR"),
    ({"normalized_slope": .049999}, "RANGE"),
    ({"price_bias": -1}, "RANGE"),
])
def test_trend_rules_have_explicit_boundaries(kwargs, expected):
    args = {"adx_value": 26, "normalized_slope": .3, "price_bias": 1, "htf_direction": 1}
    args.update(kwargs)
    assert classify_trend(**args) == expected


@pytest.mark.parametrize("percentile,expected", [(0, "LOW"), (24.999, "LOW"), (25, "NORMAL"),
                                                   (74.999, "NORMAL"), (75, "HIGH"), (95, "HIGH"), (95.001, "EXTREME"), (100, "EXTREME")])
def test_volatility_boundaries(percentile, expected):
    assert classify_volatility(percentile) == expected


def test_future_entry_and_htf_ohlcv_cannot_change_historical_context():
    original = candles()
    future = candles(count=2, start=stamp("2026-01-01T00:40:00Z"))
    before = classify(original + future)
    for observation in future:
        observation.bar.close = float("nan")
        observation.bar.high = 1e12
        observation.bar.volume = -10
    after = classify(original + future)
    assert before == after
    assert before["context_quality"] == "VALID"


def test_mandatory_five_candle_lookahead_at_third_close():
    rows = candles(5)
    params = replace(SMALL, volatility_window=1)
    kwargs = dict(signal_timestamp=stamp("2026-01-01T00:15:00Z"), entry_timeframe="5m",
                  higher_timeframe="1h", higher_candles=candles(3, step=timedelta(hours=1),
                  start=stamp("2025-12-31T21:00:00Z")), market_data_source="test",
                  classification_timestamp=stamp("2026-01-02T00:00:00Z"), parameters=params)
    before = classify_market_context(rows, **kwargs)
    for item in rows[3:]:
        item.bar.high = 1e100
        item.bar.low = float("nan")
    assert classify_market_context(rows, **kwargs) == before
    assert before["context_quality"] == "VALID"


def test_higher_timeframe_closure_and_delayed_publication_are_causal():
    htf = candles(4, step=timedelta(hours=1), start=stamp("2025-12-31T21:00:00Z"))
    common = dict(signal_timestamp=stamp("2026-01-01T00:40:00Z"), entry_timeframe="5m",
                  higher_timeframe="1h", market_data_source="test",
                  classification_timestamp=stamp("2026-01-02T00:00:00Z"), parameters=SMALL)
    expected = classify_market_context(candles(), higher_candles=htf, **common)
    htf[-1].bar.close = float("inf")
    assert classify_market_context(candles(), higher_candles=htf, **common) == expected
    delayed = [replace(item, available_at=stamp("2026-01-01T01:00:00Z")) for item in htf[:3]]
    result = classify_market_context(candles(), higher_candles=delayed, **common)
    assert result["context_quality"] == "MISSING_HTF"
    assert result["trend_regime"] == "UNKNOWN"


@pytest.mark.parametrize("mutation,expected", [
    (lambda r: r[-2:], "INSUFFICIENT_HISTORY"),
    (lambda r: r[:-3], "STALE_DATA"),
    (lambda r: r[:5] + r[6:], "GAPPED_CANDLES"),
    (lambda r: [replace(x, available_at=None) for x in r], "UNKNOWN"),
    (lambda r: [replace(x, is_closed=None) for x in r], "UNKNOWN"),
])
def test_quality_gates_never_invent_regimes(mutation, expected):
    result = classify(mutation(candles()))
    assert result["context_quality"] == expected
    assert result["trend_regime"] == result["volatility_regime"] == "UNKNOWN"
    assert result["quality_reasons"]


def test_out_of_order_and_identical_duplicates_are_order_independent():
    rows = candles()
    assert classify(rows) == classify(list(reversed(rows)) + [rows[-1]])


def test_conflicting_duplicate_is_unknown():
    rows = candles()
    changed = replace(rows[-1], bar=replace(rows[-1].bar, high=rows[-1].bar.high + 1))
    assert classify(rows + [changed])["context_quality"] == "UNKNOWN"


def test_zero_atr_is_unknown_not_range():
    rows = candles()
    for item in rows:
        item.bar.open = item.bar.high = item.bar.low = item.bar.close = 100
    result = classify(rows)
    assert result["context_quality"] == "UNKNOWN"
    assert result["atr_value"] == 0
    assert result["trend_regime"] == result["volatility_regime"] == "UNKNOWN"


def test_percentile_uses_previous_observations_and_normalized_asset_scale():
    rows = candles()
    original = classify(rows)
    for item in rows:
        for field in ("open", "high", "low", "close"):
            setattr(item.bar, field, getattr(item.bar, field) * 1000)
    scaled = classify(rows)
    assert scaled["atr_percentile"] == original["atr_percentile"]
    assert scaled["volatility_regime"] == original["volatility_regime"]
    assert original["atr_percentile"] == 0


def test_tied_percentile_uses_midrank():
    rows = candles()
    for item in rows:
        item.bar.open = item.bar.close = 100
        item.bar.high, item.bar.low = 101, 99
    assert classify(rows)["atr_percentile"] == 50


def test_definitions_hash_all_effective_parameters_and_are_detached():
    one = classifier_definition()
    two = classifier_definition()
    assert one == two
    assert one["parameter_hash"] == configuration_fingerprint(one["parameters"])
    changed = classifier_definition(parameters=replace(ClassifierParameters(), strong_adx=26), classifier_version="1.0.1")
    assert changed["parameter_hash"] != one["parameter_hash"]
    one["parameters"]["ema_period"] = 100
    assert classifier_definition() == two
    with pytest.raises(FrozenInstanceError):
        SMALL.ema_period = 100


def test_frozen_dictionary_replays_exact_original_definition():
    result = classify()
    frozen = result["classification_input"]
    actual = classify_frozen_context(frozen, classification_timestamp=result["classification_timestamp"])
    assert actual == result
    frozen["classifier_definition"]["parameters"]["strong_adx"] = 99
    with pytest.raises(ValueError, match="hash"):
        classify_frozen_context(frozen, classification_timestamp=result["classification_timestamp"])


def test_naive_timestamps_are_not_silently_assumed_utc():
    with pytest.raises(ValueError, match="timezone"):
        classify_session(datetime(2026, 1, 1))


@pytest.mark.parametrize("kwargs", [{"atr_period": 0}, {"volatility_window": 0}, {"strong_adx": 10},
                                    {"volatility_low": 80}, {"publication_delay_seconds": -1}])
def test_invalid_classifier_parameters_rejected(kwargs):
    with pytest.raises(ValueError):
        ClassifierParameters(**kwargs)


def test_unknown_observer_provenance_cannot_become_valid_with_good_timestamps():
    original = classify()["classification_input"]
    original["entry_candles"][-1]["source_quality"] = "UNKNOWN"
    result = classify_frozen_context(original, classification_timestamp="2026-01-02T00:00:00Z")
    assert result["context_quality"] == "UNKNOWN"
    assert result["trend_regime"] == result["volatility_regime"] == "UNKNOWN"
    assert "entry:unknown_source_quality" in result["quality_reasons"]
    replay = classify_frozen_context(result["classification_input"], classification_timestamp=result["classification_timestamp"])
    assert replay == result


def test_conflicting_original_observations_remain_unknown_after_snapshot_replay():
    rows = candles()
    changed = replace(rows[-1], bar=replace(rows[-1].bar, high=rows[-1].bar.high + 1))
    first = classify(rows + [changed])
    replay = classify_frozen_context(first["classification_input"], classification_timestamp=first["classification_timestamp"])
    assert replay == first


def test_original_observer_failure_is_not_erased_by_bounded_window():
    original = classify()["classification_input"]
    original.update(evidence_quality="UNKNOWN", quality_reasons=["CONFLICTING_CANDLE_OBSERVATIONS"])
    result = classify_frozen_context(original, classification_timestamp="2026-01-02T00:00:00Z")
    assert result["context_quality"] == "UNKNOWN"
    assert "CONFLICTING_CANDLE_OBSERVATIONS" in result["quality_reasons"]


def test_missing_htf_has_specific_quality_despite_observer_unknown_summary():
    original = classify()["classification_input"]
    original.update(higher_candles=[], evidence_quality="UNKNOWN", quality_reasons=["NATIVE_HTF_UNAVAILABLE"])
    result = classify_frozen_context(original, classification_timestamp="2026-01-02T00:00:00Z")
    assert result["context_quality"] == "MISSING_HTF"
    assert result["trend_regime"] == "UNKNOWN"


def test_publication_delay_does_not_infer_availability():
    rows = candles()
    result = classify_market_context(rows, signal_timestamp="2026-01-01T00:40:00Z",
        entry_timeframe="5m", higher_timeframe="1h", higher_candles=candles(3,
        step=timedelta(hours=1), start=stamp("2025-12-31T21:00:00Z")),
        market_data_source="test", classification_timestamp="2026-01-02T00:00:00Z",
        parameters=replace(SMALL, publication_delay_seconds=1))
    assert result["last_closed_candle_timestamp"] == "2026-01-01T00:35:00+00:00"
    assert result["context_quality"] == "VALID"
    unknown = classify([item.bar for item in rows])
    assert unknown["context_quality"] == "UNKNOWN"


def test_binance_inclusive_close_normalizes_to_exclusive_boundary():
    rows = [replace(item, close_timestamp=item.close_timestamp - timedelta(milliseconds=1)) for item in candles()]
    assert classify(rows) == classify()


@pytest.mark.parametrize("mutation", [
    lambda row: replace(row, available_at=row.close_timestamp - timedelta(milliseconds=1)),
    lambda row: replace(row, close_timestamp=row.close_timestamp - timedelta(seconds=1)),
    lambda row: replace(row, bar=replace(row.bar, timestamp=row.bar.timestamp + timedelta(seconds=1))),
])
def test_invalid_publication_and_exchange_boundaries_fail_closed(mutation):
    rows = candles()
    rows[-2] = mutation(rows[-2])
    result = classify(rows)
    assert result["context_quality"] == "UNKNOWN"
    assert result["trend_regime"] == "UNKNOWN"


def test_extreme_finite_inputs_never_return_nonfinite_snapshot_values():
    rows = candles()
    for item in rows:
        item.bar.open = item.bar.close = 1e307
        item.bar.high, item.bar.low = 1e308, 1e306
    result = classify_market_context(rows, signal_timestamp="2026-01-01T00:40:00Z",
        entry_timeframe="5m", higher_timeframe="1h", higher_candles=candles(3,
        step=timedelta(hours=1), start=stamp("2025-12-31T21:00:00Z")),
        market_data_source="test", classification_timestamp="2026-01-02T00:00:00Z",
        parameters=replace(SMALL, atr_period=2))
    assert result["context_quality"] == "UNKNOWN"
    configuration_fingerprint(result)


def test_explainable_bull_and_bear_indicator_pipeline():
    bull = classify()
    bear = classify(candles(direction=-1))
    assert bull["trend_regime"] == "STRONG_BULL"
    assert bear["trend_regime"] == "BEAR"  # Existing HTF inputs are bullish.
    assert bull["trend_strength"] == bull["adx_value"] == 100
    assert bull["ema_normalized_slope"] > 0
    assert bear["ema_normalized_slope"] < 0


def test_classifier_version_boundary_preserves_same_math_and_distinct_identity():
    one = classify()
    two = classify(classifier_version="1.0.1")
    assert one["trend_regime"] == two["trend_regime"]
    assert one["classifier_version"] != two["classifier_version"]
    assert one["parameter_hash"] == two["parameter_hash"]
    with pytest.raises(ValueError, match="version"):
        classifier_definition(classifier_version="2.0.0")


def test_session_definition_changes_parameter_hash():
    one = classifier_definition()
    sessions = (SessionDefinition("ASIA", "Asia/Tokyo", "10:00", "17:00"),)
    two = classifier_definition(sessions=sessions, classifier_version="1.0.1")
    assert one["parameter_hash"] != two["parameter_hash"]


def test_frozen_legacy_timezone_and_algorithm_changes_are_rejected():
    original = classify()["classification_input"]
    original["classifier_definition"]["parameters"]["conventions"]["ema"] = "OTHER"
    original["classifier_definition"]["parameter_hash"] = configuration_fingerprint(original["classifier_definition"]["parameters"])
    with pytest.raises(ValueError, match="conventions"):
        classify_frozen_context(original, classification_timestamp="2026-01-02T00:00:00Z")


def test_timezone_rules_are_hashed_and_changed_rules_block_original_replay(monkeypatch):
    import services.market_context_classifier as classifier
    original = classify()["classification_input"]
    rules = original["classifier_definition"]["parameters"]["timezone_rules"]
    assert set(rules) == {"Europe/London", "America/New_York", "Asia/Tokyo"}
    assert all(len(value) == 64 for value in rules.values())
    original_hash = original["classifier_definition"]["parameter_hash"]
    monkeypatch.setattr(classifier, "_timezone_rule_hash", lambda name: "0" * 64)
    with pytest.raises(ValueError, match="timezone rule hash"):
        classify_frozen_context(original, classification_timestamp="2026-01-02T00:00:00Z")
    updated = classifier_definition(parameters=SMALL, classifier_version="1.0.1")
    assert updated["parameter_hash"] != original_hash
