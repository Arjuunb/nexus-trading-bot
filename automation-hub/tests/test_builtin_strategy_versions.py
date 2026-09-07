"""Regression baselines for built-in strategies with preserved provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bot.data.csv_loader import load_csv_bars
from strategies.brain import TradeBrain
from strategies.brain_strategy import DecisionBrain
from strategies.builtin_versions import BUILTIN_STRATEGY_VERSIONS
from strategies.custom import simulate_strategy
from strategies.donchian_strategy import DonchianStrategy
from strategies.supertrend_strategy import SupertrendStrategy


def _fingerprint(strategy) -> tuple[int, str]:
    """Hash the causal decision stream, not P&L from an unrecorded market run."""
    fixture = Path(__file__).resolve().parents[2] / "data" / "samples" / "BTC-USD.csv"
    bars = load_csv_bars(str(fixture))[-2_000:]
    assert len(bars) == 2_000
    rows = []
    for bar in bars:
        signal = strategy.on_bar(bar)
        if signal is not None:
            rows.append({
                "time": signal.timestamp.isoformat(),
                "side": signal.type.value,
                "entry": round(signal.entry, 8),
                "stop": round(signal.stop_loss, 8),
                "target": round(signal.take_profit, 8),
                "confidence": getattr(signal, "confidence", None),
            })
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return len(rows), hashlib.sha256(encoded).hexdigest()


def test_decision_brain_v1_0_0_causal_signal_baseline():
    version = BUILTIN_STRATEGY_VERSIONS["brain"]
    count, digest = _fingerprint(DecisionBrain(
        "BTCUSDT", allow_legacy_htf_resample=True))
    assert version.version == "1.0.0"
    assert (count, digest) == (version.fixture_signal_count, version.fixture_signal_sha256)


def test_supertrend_v1_0_0_causal_signal_baseline():
    version = BUILTIN_STRATEGY_VERSIONS["supertrend"]
    count, digest = _fingerprint(SupertrendStrategy("BTCUSDT"))
    assert version.version == "1.0.0"
    assert (count, digest) == (version.fixture_signal_count, version.fixture_signal_sha256)


def test_donchian_v1_0_0_causal_signal_baseline():
    version = BUILTIN_STRATEGY_VERSIONS["donchian"]
    count, digest = _fingerprint(DonchianStrategy("BTCUSDT"))
    assert version.version == "1.0.0"
    assert (count, digest) == (version.fixture_signal_count, version.fixture_signal_sha256)


def test_supertrend_v1_0_0_gate_risk_and_execution_baseline():
    """A/B/C/D fixture comparison catches silent pipeline regressions.

    A is raw strategy; B adds the same TradeBrain quality gate used by the
    engine; C adds the enabled, representative loss-streak risk control; D
    adds the current maker-limit execution contract.  It is intentionally a
    fixed fixture, not a claim about live profitability.
    """
    fixture = Path(__file__).resolve().parents[2] / "data" / "samples" / "BTC-USD.csv"
    bars = load_csv_bars(str(fixture))[-2_000:]
    common = {"starting_balance": 500, "fee": 0.0004, "slippage": 0.0003}
    variants = {
        "A_strategy_only": {},
        "B_strategy_plus_decision_gate": {"brain": TradeBrain(), "min_score": 60},
        "C_gate_plus_risk": {"brain": TradeBrain(), "min_score": 60,
                               "max_consecutive_losses": 5},
        "D_current_execution_proxy": {"brain": TradeBrain(), "min_score": 60,
                                        "max_consecutive_losses": 5,
                                        "entry_mode": "limit", "limit_ttl_bars": 3},
    }
    observed = {}
    for name, kwargs in variants.items():
        result = simulate_strategy(SupertrendStrategy("BTCUSDT"), bars, **common, **kwargs)
        observed[name] = (
            result["total_trades"], result["wins"], result["losses"],
            result["win_rate"], result["profit_factor"], result["net_r"],
            result["max_drawdown_pct"], len(result.get("blocked", [])),
            result.get("missed_entries", 0),
        )
    assert observed == {
        "A_strategy_only": (20, 6, 14, 30.0, 1.09, 1.28, 5.8, 0, 0),
        "B_strategy_plus_decision_gate": (15, 5, 10, 33.3, 1.25, 2.46, 4.4, 6, 0),
        "C_gate_plus_risk": (15, 5, 10, 33.3, 1.25, 2.46, 4.4, 6, 0),
        "D_current_execution_proxy": (15, 5, 10, 33.3, 1.26, 2.58, 4.3, 6, 0),
    }
