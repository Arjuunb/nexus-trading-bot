"""Per-instance switch for the Decision Brain quality gate.

The gate stays on unless an owner turns it off for one Trading Instance.
Off means the Decision Brain's score and its opinions about the setup (trend,
regime, volatility) stop blocking entries, while the blocks that protect
position size and the account still apply, the verdict is still journaled,
and the risk pipeline still runs.
"""
import re
from pathlib import Path

import pytest

from data.ledger import SqliteLedger
from services.auto_engine import QUALITY_GATE_SAFETY_BLOCKS
from services.instance_switches import InstanceSwitches
from services.tenancy import OWNER_TENANT
from services.trading_instances import TradingInstanceManager
from strategies.brain import BrainVerdict
from tests.test_parity_and_cache import _engine, _history, _sig, _Stub


class _Brain:
    """A TradeBrain stand-in returning one fixed verdict."""
    def __init__(self, *, score=90, blocks=()):
        self.verdict = BrainVerdict(allowed=not blocks, score=score, regime="Trending",
                                    htf_bias="bullish", setup_type="trend", components={},
                                    passed=[], failed=["htf alignment"], blocks=list(blocks))

    def evaluate(self, *_args, **_kwargs):
        return self.verdict


def _run(brain, bypass):
    eng, paper, led = _engine()
    eng._quality_brain = brain
    eng.quality_gate_bypass = bypass
    eng._process_bar("BTCUSDT", _history(91)[-1], _Stub([_sig(tp=112.0)], bars=_history()))
    return eng, paper, led


# ─────────────────────────── the engine ───────────────────────────
def test_the_gate_is_on_unless_switched_off():
    eng, paper, led = _run(_Brain(score=30), None)
    assert paper.open_position("BTCUSDT") is None
    assert eng.rejection_counts == {"quality": 1}
    assert any("blocked by decision gate" in row["message"] for row in led.get_logs())


def test_switched_off_a_low_score_no_longer_blocks_and_the_verdict_is_kept():
    decisions = []
    eng, paper, _ = _engine()
    eng._quality_brain = _Brain(score=30)
    eng.quality_gate_bypass = lambda: True
    eng.decisions = type("Store", (), {"record": lambda _self, d: decisions.append(dict(d)) or "d1"})()
    eng._process_bar("BTCUSDT", _history(91)[-1], _Stub([_sig(tp=112.0)], bars=_history()))
    assert paper.open_position("BTCUSDT") is not None
    assert eng.rejection_counts.get("quality", 0) == 0
    [decision] = decisions
    assert decision["decision"] == "accepted"
    assert "Quality gate off" in decision["reason"] and "below minimum 60" in decision["reason"]
    assert decision["setup_quality_score"] == 30.0


def test_switched_off_the_brains_opinions_about_the_setup_no_longer_block():
    for block in ("against strong higher-timeframe bullish trend",
                  "ranging / unclear regime for a trend setup",
                  "volatility too low (ATR 0.01%)"):
        _, paper, _ = _run(_Brain(blocks=[block]), lambda: True)
        assert paper.open_position("BTCUSDT") is not None, block


def test_blocks_that_protect_position_size_always_apply():
    for block in ("reward:risk 0.40 below 1.0", "stop too tight (0.010%) — oversize risk",
                  "stop too wide (12.0%) — likely bad setup", "losing-streak cooldown (4 in a row)"):
        eng, paper, _ = _run(_Brain(blocks=[block]), lambda: True)
        assert paper.open_position("BTCUSDT") is None, block
        assert eng.rejection_counts == {"quality": 1}


def test_the_real_brain_still_refuses_a_sub_1r_target_with_the_gate_off():
    # rr 0.4 (target 102 vs stop 95): TradeBrain's own hard block, end to end.
    eng, paper, _ = _engine()
    eng.quality_gate_bypass = lambda: True
    eng._process_bar("BTCUSDT", _history(91)[-1], _Stub([_sig(tp=102.0)], bars=_history()))
    assert paper.open_position("BTCUSDT") is None


def test_the_safety_prefixes_match_what_tradebrain_writes():
    source = Path(__file__).resolve().parents[1].joinpath("strategies", "brain.py").read_text()
    written = re.findall(r'blocks\.append\(f?"([^"{(]+)', source)
    for prefix in QUALITY_GATE_SAFETY_BLOCKS:
        assert any(text.startswith(prefix) for text in written), prefix


def test_an_unreadable_switch_leaves_the_gate_on():
    def broken():
        raise OSError("disk")
    _, paper, _ = _run(_Brain(score=30), broken)
    assert paper.open_position("BTCUSDT") is None


# ─────────────────────────── the instance ───────────────────────────
@pytest.fixture
def fast_worker(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


def _manager(switches):
    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    manager.quality_gate = switches
    return manager


def _create(manager, owner_id=OWNER_TENANT):
    return manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                          strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000, owner_id=owner_id)


def test_a_running_instance_follows_the_switch_and_it_is_per_instance(tmp_path, fast_worker):
    path = str(tmp_path / "instance_quality_gate.json")
    switches = InstanceSwitches(path)
    manager = _manager(switches)
    first, second = _create(manager), _create(manager)
    manager.start(first.id)
    manager.start(second.id)
    engine = manager._runtime[first.id][0]
    assert engine._quality_gate_bypassed() is False
    assert manager.status(first.id)["quality_gate"]["enforced"] is True

    switches.set(first.id, True, by="owner")
    assert engine._quality_gate_bypassed() is True
    assert manager._runtime[second.id][0]._quality_gate_bypassed() is False
    assert manager.status(first.id)["quality_gate"]["enforced"] is False
    assert InstanceSwitches(path).enabled(first.id)            # survives a restart

    switches.set(first.id, False)
    assert engine._quality_gate_bypassed() is False


def test_without_a_switch_attached_every_instance_keeps_the_gate(fast_worker):
    manager = _manager(None)
    inst = _create(manager)
    manager.start(inst.id)
    assert manager._runtime[inst.id][0].quality_gate_bypass is None
    assert manager.status(inst.id)["quality_gate"] is None


def test_deleting_an_instance_clears_its_switch(fast_worker):
    switches = InstanceSwitches(None)
    manager = _manager(switches)
    inst = _create(manager)
    switches.set(inst.id, True)
    manager.delete(inst.id)
    assert switches.enabled(inst.id) is False


# ─────────────────────────── the API ───────────────────────────
def _patch_api(monkeypatch, manager):
    from routers import instances as instance_api
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _s: None)
    monkeypatch.setattr(instance_api, "_owner", lambda _request: OWNER_TENANT)
    monkeypatch.setattr(instance_api, "_initiated_by", lambda _request: "tester")
    return instance_api


def test_the_api_switches_it_and_refuses_another_owner(monkeypatch, fast_worker):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    switches = InstanceSwitches(None)
    manager = _manager(switches)
    mine, theirs = _create(manager), _create(manager, owner_id="someone-else")
    api = _patch_api(monkeypatch, manager)

    assert api.instance_quality_gate(mine.id)["enforced"] is True
    state = api.update_instance_quality_gate(mine.id, api.QualityGateUpdate(enforced=False))
    assert state["enforced"] is False and state["min_score"] == 60 and switches.enabled(mine.id)
    assert any("quality gate off" in row["message"] for row in manager.store.engine_logs(mine.id))
    assert api.update_instance_quality_gate(mine.id, api.QualityGateUpdate(enforced=True))["enforced"] is True

    body = api.QualityGateUpdate(enforced=False)
    for call in (lambda: api.instance_quality_gate(theirs.id),
                 lambda: api.update_instance_quality_gate(theirs.id, body)):
        with pytest.raises(HTTPException) as refused:
            call()
        assert refused.value.status_code == 404
    assert switches.enabled(theirs.id) is False


def test_the_api_requires_the_control_secret(monkeypatch, fast_worker):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    switches = InstanceSwitches(None)
    manager = _manager(switches)
    inst = _create(manager)
    api = _patch_api(monkeypatch, manager)

    def refuse(_secret):
        raise HTTPException(401, "bad secret")
    monkeypatch.setattr(api._wa, "_check_secret", refuse)
    with pytest.raises(HTTPException) as refused:
        api.update_instance_quality_gate(inst.id, api.QualityGateUpdate(enforced=False))
    assert refused.value.status_code == 401 and switches.enabled(inst.id) is False
