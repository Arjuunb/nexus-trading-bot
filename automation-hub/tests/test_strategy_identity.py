"""Effective configuration identity must describe evidence, never alter execution."""
from __future__ import annotations

from dataclasses import fields, replace
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from services.strategy_identity import (
    canonical_configuration_json,
    configuration_fingerprint,
    observed_strategy_identity,
    strategy_id_for,
)
from strategies.adaptive_trend_pullback import (
    AdaptiveTrendPullbackConfig,
    AdaptiveTrendPullbackStrategy,
)
from strategies.brain import BrainConfig, TradeBrain
from strategies.brain_strategy import DecisionBrain
from strategies.custom_adapter import CustomStrategyAdapter
from strategies.custom import evaluate
from strategies.donchian_strategy import DonchianStrategy
from strategies.price_action_rejection import PriceActionRejectionStrategy
from strategies.supertrend_strategy import SupertrendStrategy


def observe(strategy, key="adaptive_trend_pullback", **kwargs):
    return observed_strategy_identity(strategy, strategy_id=key, **kwargs)


@pytest.mark.parametrize("strategy,expected", [
    (AdaptiveTrendPullbackStrategy("XRPUSDT"), "adaptive_trend_pullback"),
    (DecisionBrain("BTCUSDT"), "brain"),
    (DonchianStrategy("BTCUSDT"), "donchian"),
    (SupertrendStrategy("BTCUSDT"), "supertrend"),
    (PriceActionRejectionStrategy("XRPUSDT"), "price_action_rejection"),
    (None, None),
    (SimpleNamespace(name="adaptive_trend_pullback"), None),
])
def test_strategy_id_for_recognizes_canonical_class_without_guessing_from_label(strategy, expected):
    assert strategy_id_for(strategy) == expected


def test_canonical_hash_ignores_mapping_order_and_equivalent_numeric_spelling():
    left = {"nested": MappingProxyType({"rr": 2.0, "enabled": True}), "bars": (5, 15)}
    right = {"bars": [5, 15], "nested": {"enabled": True, "rr": 2}}
    assert configuration_fingerprint(left) == configuration_fingerprint(right)
    assert len(configuration_fingerprint(left)) == 64
    assert configuration_fingerprint({"value": True}) != configuration_fingerprint({"value": 1})
    assert json.loads(canonical_configuration_json(left)) == right


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), object(), {1: "bad key"}, {1, 2}])
def test_invalid_configuration_is_rejected_without_lossy_string_coercion(bad):
    with pytest.raises(ValueError):
        configuration_fingerprint({"configuration": bad})


@pytest.mark.parametrize("key", ["api_key", "password", "access_token", "private_key", "client_secret"])
def test_secret_fields_never_enter_saved_configuration(key):
    with pytest.raises(ValueError) as error:
        canonical_configuration_json({"nested": {key: "do-not-persist-this-value"}})
    assert "do-not-persist-this-value" not in str(error.value)


def test_adaptive_snapshot_captures_all_effective_fields_and_inherited_defaults():
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT")
    identity = observe(strategy, declared_version="1.0.0")
    assert identity["strategy_id"] == "adaptive_trend_pullback"
    assert identity["strategy_version"] == identity["observed_version"] == "1.0.0"
    assert identity["identity_status"] == "observed"
    config = identity["configuration"]
    assert set(config["config"]) == {field.name for field in fields(strategy.config)}
    assert config["params"] == {"atr_period": 14, "atr_mult": 1.5, "rr_target": 2.5}
    assert config["settings"]["allow_legacy_mtf_resample"] is False
    assert config["timeframes"] == {
        "entry_timeframe": "5m", "decision_timeframe": "5m", "primary_timeframe": "1h",
        "secondary_timeframe": "4h", "required_timeframes": ["1h", "15m", "5m"],
    }
    assert identity["strategy_config_hash"] == configuration_fingerprint(config)
    assert len(identity["source_hash"]) == 64
    assert any(row["path"].endswith("adaptive_trend_pullback/quality_scorer.py")
               for row in identity["source_manifest"]["modules"])


def test_snapshot_has_no_reference_to_mutable_configuration_or_transient_runtime_state():
    minimum = {"4h": 70, "1h": 70, "15m": 70, "5m": 70}
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT", config=AdaptiveTrendPullbackConfig(minimum_bars=minimum))
    initial = observe(strategy)
    strategy.bars.append(object())
    strategy._native_mtf_evidence = {"transient": object()}
    strategy.lifecycle_state = "changed"
    assert observe(strategy) == initial
    minimum["1h"] = 90
    assert initial["configuration"]["config"]["minimum_bars"]["1h"] == 70
    assert observe(strategy)["strategy_config_hash"] != initial["strategy_config_hash"]
    initial["configuration"]["params"]["atr_mult"] = 99
    assert strategy.params["atr_mult"] == 1.5


def test_identical_effective_adaptive_settings_ignore_symbol_and_parameter_order():
    left = AdaptiveTrendPullbackStrategy("XRPUSDT", config=AdaptiveTrendPullbackConfig(target_rr=3.0, adx_min=21.0))
    right = AdaptiveTrendPullbackStrategy("BTCUSDT", config=AdaptiveTrendPullbackConfig(adx_min=21, target_rr=3))
    assert observe(left)["strategy_config_hash"] == observe(right)["strategy_config_hash"]


CHANGES = {
    "fast_ema": 19, "slow_ema": 51, "adx_period": 15, "adx_min": 21,
    "regime_confidence_min": 71, "trend_confidence_min": 71, "quality_minimum": 76,
    "minimum_rr": 2.1, "target_rr": 3, "atr_period": 15, "stop_atr_buffer": .26,
    "maximum_atr_pct": .05, "maximum_atr_expansion": 3, "maximum_extension_atr": 4,
    "ema_separation_min_atr": .11, "structure_lookback": 31, "pullback_lookback": 21,
    "confirmation_lookback": 9, "abnormal_volume_multiple": 3,
    "confirmation_volume_multiple": 1.2, "rejection_wick_body_ratio": 1.6,
    "corrective_move_atr": .6, "pullback_zone_atr": .9, "target_method": "structure",
    "regime_timeframe": "4h", "trend_timeframe": "4h", "pullback_timeframe": "1h",
    "entry_timeframe": "15m", "minimum_bars": {"4h": 70, "1h": 71, "15m": 70, "5m": 70},
}


@pytest.mark.parametrize("field,value", CHANGES.items())
def test_each_adaptive_effective_configuration_change_changes_fingerprint(field, value):
    original = AdaptiveTrendPullbackStrategy("XRPUSDT")
    changed = AdaptiveTrendPullbackStrategy("XRPUSDT", config=replace(original.config, **{field: value}))
    assert observe(original)["strategy_config_hash"] != observe(changed)["strategy_config_hash"]


def test_inherited_defaults_and_resampling_mode_change_fingerprint():
    original = AdaptiveTrendPullbackStrategy("XRPUSDT")
    changed = AdaptiveTrendPullbackStrategy("XRPUSDT", allow_legacy_mtf_resample=True)
    assert observe(original)["strategy_config_hash"] != observe(changed)["strategy_config_hash"]
    changed = AdaptiveTrendPullbackStrategy("XRPUSDT")
    changed.params["atr_mult"] = 2
    assert observe(original)["strategy_config_hash"] != observe(changed)["strategy_config_hash"]


def test_version_mismatch_is_not_relabelled_as_observed_implementation():
    identity = observe(AdaptiveTrendPullbackStrategy("XRPUSDT"), declared_version="2.0.0")
    assert identity["strategy_version"] == "1.0.0"
    assert identity["declared_version"] == "2.0.0"
    assert identity["identity_status"] == "version_mismatch"


def test_runtime_timeframe_is_part_of_configuration_and_mismatch_remains_visible():
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT")
    regular = observe(strategy, timeframe="5m")
    changed = observe(strategy, timeframe="15m")
    assert changed["strategy_config_hash"] != regular["strategy_config_hash"]
    assert changed["identity_status"] == "timeframe_mismatch"


def test_brain_snapshot_contains_non_params_settings_and_nested_effective_config():
    strategy = DecisionBrain("BTCUSDT", htf_damp=.7, max_history=800)
    identity = observe(strategy, "brain", timeframe="1h")
    assert identity["configuration"]["settings"]["htf_damp"] == .7
    assert identity["configuration"]["settings"]["max_history"] == 800
    assert identity["configuration"]["components"]["_regime"]["window"] == 30
    assert identity["configuration"]["timeframes"]["primary_timeframe"] == "4h"


@pytest.mark.parametrize("strategy,key", [(SupertrendStrategy, "supertrend"), (DonchianStrategy, "donchian")])
def test_constructor_settings_outside_params_are_fingerprinted(strategy, key):
    left = observe(strategy("BTCUSDT"), key, timeframe="1h")
    right = observe(strategy("BTCUSDT", max_history=800), key, timeframe="1h")
    assert left["strategy_config_hash"] != right["strategy_config_hash"]
    assert left["observed_version"] == "1.0.0"


def test_research_alias_preserves_source_id_and_maps_to_builtin_identity():
    identity = observe(DecisionBrain("BTCUSDT"), "decision_brain", timeframe="1h")
    assert identity["strategy_id"] == "brain"
    assert identity["source_strategy_id"] == "decision_brain"


def test_custom_effective_snapshot_preserves_definition_and_inherited_quality_defaults():
    spec = {"entry": {"op": "AND", "rules": []}, "stop": {"type": "atr"}}
    strategy = CustomStrategyAdapter("BTCUSDT", spec, brain=TradeBrain(BrainConfig(min_rr=1.2)))
    identity = observe(strategy, "custom:example", declared_version="v3", timeframe="5m")
    assert identity["observed_version"] == "unversioned"
    assert identity["declared_version"] == "v3"
    assert identity["identity_status"] == "unversioned"
    assert identity["configuration"]["definition"]["entry"] == spec["entry"]
    assert identity["configuration"]["settings"]["min_score"] == 60
    assert identity["configuration"]["components"]["brain"]["min_rr"] == 1.2
    assert identity["configuration"]["custom_defaults"]["stop"]["mult"] == 1.5
    assert identity["configuration"]["custom_defaults"]["stop"]["period"] == 14
    spec["entry"]["rules"].append({"type": "rsi"})
    assert identity["configuration"]["definition"]["entry"]["rules"] == []


def test_custom_omitted_defaults_and_explicit_defaults_have_identical_effective_fingerprint():
    left = CustomStrategyAdapter("BTCUSDT", {
        "name": "display one", "entry": {"rules": [{"type": "rsi"}]},
    })
    right = CustomStrategyAdapter("BTCUSDT", {
        "name": "display two", "side": "long", "quality_filter": True, "min_score": 60,
        "mtf_filter": True, "entry": {"op": "AND", "rules": [{"type": "rsi", "period": 14, "value": 50, "op": "above"}]},
        "stop": {"type": "atr", "period": 14, "mult": 1.5},
        "target": {"type": "rr", "rr": 1.5}, "exit": {"op": "OR", "rules": []},
    })
    assert observe(left, "custom:example", timeframe="5m")["strategy_config_hash"] == observe(right, "custom:example", timeframe="5m")["strategy_config_hash"]


@pytest.mark.parametrize("rule_type", [
    "ema_cross", "rsi", "sma_trend", "macd", "breakout", "volume", "atr_filter",
    "pullback", "support_bounce", "liquidity_sweep", "fair_value_gap", "vwap", "bollinger",
    "bos", "choch", "adx", "supertrend", "obv", "stoch_rsi", "trend", "ichimoku",
    "order_block", "supply_demand",
])
def test_resolved_custom_rule_defaults_preserve_authoritative_evaluation(rule_type):
    from bot.data.synthetic import generate_bars
    bars = generate_bars(n=260, timeframe="5m", seed=17)
    definition = {"entry": {"rules": [{"type": rule_type}]}, "quality_filter": False}
    identity = observe(CustomStrategyAdapter("BTCUSDT", definition), "custom:example", timeframe="5m")
    resolved = identity["configuration"]["definition"]["entry"]
    for index in (100, 210, 259):
        assert evaluate(definition["entry"], bars, index) == evaluate(resolved, bars, index)


def test_brain_helper_configuration_drift_changes_fingerprint():
    strategy = DecisionBrain("BTCUSDT")
    original = observe(strategy, "brain", timeframe="1h")
    strategy._regime.cfg.er_trending = .5
    changed = observe(strategy, "brain", timeframe="1h")
    assert original["strategy_config_hash"] != changed["strategy_config_hash"]


def test_source_changes_have_separate_hash_without_changing_effective_config(monkeypatch):
    import services.strategy_identity as identity_module
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT")
    original = observe(strategy)
    manifest = json.loads(json.dumps(original["source_manifest"]))
    manifest["modules"][0]["sha256"] = "f" * 64
    monkeypatch.setattr(identity_module, "_source_manifest", lambda strategy: manifest)
    changed = observe(strategy)
    assert original["strategy_config_hash"] == changed["strategy_config_hash"]
    assert original["source_hash"] != changed["source_hash"]


@pytest.mark.parametrize("key", ["apiKey", "ApiSecret", "clientSecret"])
def test_common_credential_key_spelling_is_rejected(key):
    with pytest.raises(ValueError):
        canonical_configuration_json({key: "not-for-evidence"})


def test_price_action_captures_actual_source_version_full_config_and_bound_setup():
    strategy = PriceActionRejectionStrategy("XRPUSDT")
    identity = observe(strategy, "price_action_rejection")
    assert identity["observed_version"] == strategy.strategy_version
    assert identity["configuration"]["settings"]["pa_strategy_id"] == "PA1_SR_REJECTION"
    assert identity["configuration"]["settings"]["warmup_required"] == 400
    assert "symbol" not in identity["configuration"]["config"]


def test_unknown_strategy_and_missing_history_remain_unknown():
    identity = observe(None)
    assert identity["strategy_config_hash"] is None
    assert identity["configuration"] is None
    assert identity["identity_status"] == "unavailable"
    identity = observe(SimpleNamespace(params={"foo": 1}), "unknown")
    assert identity["strategy_config_hash"] is None
    assert identity["identity_status"] == "unsupported_strategy"


def test_identity_failure_does_not_change_strategy_or_expose_bad_values():
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT")
    strategy.params["api_key"] = "do-not-persist-this-value"
    identity = observe(strategy)
    assert identity["identity_status"] == "unavailable"
    assert identity["configuration"] is None
    assert "do-not-persist-this-value" not in json.dumps(identity)
    assert strategy.params["api_key"] == "do-not-persist-this-value"
