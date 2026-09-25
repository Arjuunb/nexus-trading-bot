"""Backtesting with the quality gate off, and the one-command backtest script.

A Trading Instance can switch the Decision Brain quality gate off. A backtest
of that must apply the same rule the instance does (services/quality_gate.py):
the score never blocks, the size/account safety blocks still do. The script
(scripts/backtest_strategy.py) runs the lab's checks with the gate on and off.
Real candles are not reachable here, so the script is exercised on generated
candles; only the plumbing is under test, not any result.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from bot.data.synthetic import generate_bars
from services import backtest_lab, strategy_presets
from services.quality_gate import SAFETY_BLOCKS, SafetyOnlyBrain, safety_blocks
from strategies.brain import BrainVerdict, TradeBrain


def _verdict(blocks, score=20):
    return BrainVerdict(allowed=not blocks, score=score, regime="Ranging", htf_bias="bearish",
                        setup_type="trend", blocks=list(blocks))


class _Fixed:
    def __init__(self, verdict):
        self.verdict = verdict

    def evaluate(self, *_a, **_k):
        return self.verdict


# ─────────────────────────── the rule ───────────────────────────
def test_only_the_safety_blocks_survive():
    opinion = ["against strong higher-timeframe bearish trend", "ranging / unclear regime for a trend setup"]
    v = SafetyOnlyBrain(_Fixed(_verdict(opinion))).evaluate()
    assert v.allowed and v.blocks == [] and v.score == 20          # score kept, never used to block
    size = ["stop too tight (0.010%) — oversize risk"]
    v = SafetyOnlyBrain(_Fixed(_verdict(opinion + size))).evaluate()
    assert not v.allowed and v.blocks == size
    assert safety_blocks(_verdict(["losing-streak cooldown (4 in a row)"])) == ["losing-streak cooldown (4 in a row)"]


def test_the_real_brain_still_refuses_a_sub_1r_target():
    bars = generate_bars(n=120, timeframe="1h", seed=7)
    entry = bars[-1].close
    v = SafetyOnlyBrain(TradeBrain()).evaluate(bars, len(bars) - 1, side="long", entry=entry,
                                               stop=entry * 0.99, target=entry * 1.004)
    assert not v.allowed and v.blocks[0].startswith(SAFETY_BLOCKS[0])


# ─────────────────────────── the simulator ───────────────────────────
def test_the_simulator_applies_the_instance_rule_when_asked(monkeypatch):
    seen = []
    import strategies.custom as custom

    def capture(strat, rows, *, brain, min_score, **_kw):
        seen.append((type(brain).__name__, min_score))
        return {"total_trades": 0, "trades": []}
    monkeypatch.setattr(custom, "simulate_strategy", capture)
    rows = generate_bars(n=300, timeframe="1h", seed=1)
    name = "3-Candle Rejection · EMA 9/33"
    strategy_presets._run_on(name, "BTCUSDT", "1h", {}, None, rows)
    strategy_presets._run_on(name, "BTCUSDT", "1h", {"quality_gate": "off"}, None, rows)
    assert seen == [("TradeBrain", 60), ("SafetyOnlyBrain", 0)]


def test_the_lab_passes_the_gate_through_and_has_no_score_to_tune_when_off(monkeypatch):
    rows = generate_bars(n=1200, timeframe="1h", seed=2)
    monkeypatch.setattr(backtest_lab, "_fetch", lambda *_a, **_k: (rows, "test"))
    tunings = []

    def metrics(_s, _sym, _tf, tuning, part, _spec=None):
        tunings.append(dict(tuning or {}))
        return {"trades": 5, "net_r": 1.0, "profit_factor": 1.5}, {"trades": []}
    monkeypatch.setattr(backtest_lab, "_metrics_on", metrics)

    off = backtest_lab.walk_forward("x", "BTCUSDT", "1h", quality_gate="off")
    assert off["quality_gate"] == "off"
    assert {t.get("min_score") for t in tunings} == {0}
    assert all(t.get("quality_gate") == "off" for t in tunings)
    tunings.clear()
    on = backtest_lab.walk_forward("x", "BTCUSDT", "1h")
    assert on["quality_gate"] == "on" and {t.get("min_score") for t in tunings} == {50, 60, 70, 80}
    assert not any("quality_gate" in t for t in tunings)

    tunings.clear()
    backtest_lab.out_of_sample("x", "BTCUSDT", "1h", quality_gate="off")
    backtest_lab.monte_carlo("x", "BTCUSDT", "1h", quality_gate="off")
    assert all(t.get("quality_gate") == "off" for t in tunings)
    with pytest.raises(ValueError):
        backtest_lab.walk_forward("x", "BTCUSDT", "1h", quality_gate="maybe")


# ─────────────────────────── the script ───────────────────────────
def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backtest_strategy.py"
    spec = importlib.util.spec_from_file_location("backtest_strategy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_runs_both_gates_and_writes_the_report(monkeypatch, tmp_path, capsys):
    rows = generate_bars(n=1500, timeframe="1h", seed=4)
    monkeypatch.setattr(backtest_lab, "_fetch", lambda *_a, **_k: (rows, "generated (test)"))
    out = tmp_path / "report.json"
    assert _script().main(["--symbols", "BTCUSDT", "--timeframes", "1h", "--bars", "1500",
                           "--runs", "100", "--no-sync", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "data: generated (test)" in printed            # never claims real data it did not use
    assert "gate on" in printed and "gate off" in printed
    assert "profitable" not in printed.lower()
    report = json.loads(out.read_text())
    [market] = report["markets"]
    assert report["gates"] == ["on", "off"] and market["candles"] == 1500
    for gate in ("on", "off"):
        assert set(market[gate]) == {"whole_period", "out_of_sample", "walk_forward", "monte_carlo"}
        assert "paths" not in market[gate]["monte_carlo"]
    assert market["off"]["walk_forward"]["quality_gate"] == "off"


def test_the_script_refuses_an_unknown_strategy(capsys):
    assert _script().main(["--strategy", "Nope", "--no-sync"]) == 2
    assert "Unknown strategy" in capsys.readouterr().err


def test_no_candles_is_reported_not_invented(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(backtest_lab, "_fetch",
                        lambda *_a, **_k: ([], "unavailable (real data required — run /data/sync)"))
    out = tmp_path / "r.json"
    assert _script().main(["--symbols", "BTCUSDT", "--timeframes", "1h", "--no-sync", "--json", str(out)]) == 0
    [market] = json.loads(out.read_text())["markets"]
    assert market["error"].startswith("no real candles") and "on" not in market
    assert "0 trades in total" in capsys.readouterr().out
