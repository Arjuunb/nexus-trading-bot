"""Continuous monitoring loop.

Contract: it watches the DEPLOYED strategy, recomputes the expensive baseline
only when the strategy definition changes, alerts once per finding per cooldown,
and never modifies anything.
"""
import pytest

from services import monitor_runner as mr

SPEC = {
    "id": "s1", "name": "EMA Trend", "symbol": "BTCUSDT", "timeframe": "4h",
    "entry": {"op": "AND", "rules": [{"type": "ema_cross", "fast": 20, "slow": 50}]},
    "stop": {"type": "atr", "mult": 1.5}, "target": {"type": "rr", "rr": 2},
    "risk_per_trade_pct": 0.01,
}

BASE = {"total_trades": 120, "win_rate": 55.0, "profit_factor": 1.9,
        "net_r": 60.0, "expectancy_r": 0.5, "max_drawdown_r": 8.0, "span_days": 365}


class FakeEngine:
    def __init__(self, spec=SPEC):
        self.deployed_spec = spec
        self.strategy_label = "Custom: EMA Trend"


class FakePaper:
    """Mirrors execution.PaperExecutionEngine: a pooled history, and a scoped
    view that excludes trades carrying no strategy id rather than crediting
    them to whoever asked."""

    def __init__(self, trades=None):
        self._t = trades or []
        self.fill_model = None

    def history(self):
        return self._t

    def strategy_history(self, strategy_id):
        if not strategy_id:
            return []
        return [t for t in self._t if t.get("strategy_id") == strategy_id]


class FakeLedger:
    def __init__(self):
        self.alerts, self.logs = [], []

    def add_alert(self, **kw):
        self.alerts.append(kw)

    def log(self, **kw):
        self.logs.append(kw)


def _trades(n, r, strategy_id=SPEC["id"]):
    """Closed trades ATTRIBUTED to the deployed spec by default.

    The monitor compares a strategy against its own backtest, so its live
    sample is the trades carrying that strategy's id. Pass strategy_id=""
    for the unattributed trades the production ledger is full of."""
    return [{"rr": r, "strategy_id": strategy_id,
             "closed_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00"}
            for i in range(n)]


def _runner(*, engine=None, trades=None, baseline=BASE, vol=None, calls=None,
            **kw):
    def baseline_fn(spec, rng):
        if calls is not None:
            calls.append(spec_snapshot(spec))
        return dict(baseline)
    return mr.MonitorRunner(
        engine or FakeEngine(), FakePaper(trades or []), FakeLedger(),
        baseline_fn=baseline_fn,
        volatility_fn=lambda spec, rng: dict(vol or {}),
        **kw)


def spec_snapshot(spec):
    return spec.get("entry", {}).get("rules", [{}])[0].get("fast")


# ------------------------------------------------------------ what it watches

def test_no_deployed_strategy_is_reported_not_guessed():
    r = _runner(engine=FakeEngine(spec=None))
    out = r.check()
    assert out["available"] is False and out["status"] == "no_strategy"
    assert "Deploy a strategy" in out["note"]


def test_a_builtin_strategy_with_no_rules_is_also_no_strategy():
    """A built-in engine strategy has no rule spec, so there is nothing to
    baseline against — that must read as 'nothing to monitor', not as 'fine'."""
    out = _runner(engine=FakeEngine(spec={"name": "Decision Brain"})).check()
    assert out["status"] == "no_strategy"


def test_it_evaluates_the_deployed_spec_against_a_real_baseline():
    out = _runner(trades=_trades(40, 1.0)).check()
    assert out["available"] is True
    assert out["strategy"] == "EMA Trend" and out["symbol"] == "BTCUSDT"
    assert out["baseline"]["profit_factor"] == 1.9


# -------------------------------------------------------------- baseline cache

def test_the_baseline_is_computed_once_and_reused():
    """Re-running a one-year backtest every cycle would burn minutes of CPU per
    hour recomputing a number that did not change."""
    calls = []
    r = _runner(trades=_trades(40, 1.0), calls=calls)
    r.check(now=1000.0)
    r.check(now=2000.0)
    r.check(now=3000.0)
    assert len(calls) == 1
    assert r.last_result["baseline_cached"] is True


def test_editing_a_rule_invalidates_the_baseline_immediately():
    calls = []
    eng = FakeEngine()
    r = _runner(engine=eng, trades=_trades(40, 1.0), calls=calls)
    r.check(now=1000.0)
    eng.deployed_spec = {**SPEC, "entry": {"op": "AND", "rules": [
        {"type": "ema_cross", "fast": 30, "slow": 50}]}}
    r.check(now=1001.0)
    assert calls == [20, 30], "the changed rule must force a fresh baseline"


def test_renaming_a_strategy_does_not_invalidate_the_baseline():
    """A rename changes no behaviour, so it must not trigger a backtest."""
    calls = []
    eng = FakeEngine()
    r = _runner(engine=eng, trades=_trades(40, 1.0), calls=calls)
    r.check(now=1000.0)
    eng.deployed_spec = {**SPEC, "name": "Renamed", "favorite": True,
                         "tags": ["btc"], "updated_at": "2026-05-05"}
    r.check(now=1001.0)
    assert len(calls) == 1


def test_the_baseline_refreshes_after_its_ttl():
    calls = []
    r = _runner(trades=_trades(40, 1.0), calls=calls, baseline_ttl_s=100.0)
    r.check(now=1000.0)
    r.check(now=1050.0)
    assert len(calls) == 1
    r.check(now=1200.0)
    assert len(calls) == 2


def test_spec_key_ignores_library_metadata_but_not_risk():
    a = mr.spec_key(SPEC, "1Y")
    assert mr.spec_key({**SPEC, "name": "x", "favorite": True}, "1Y") == a
    assert mr.spec_key({**SPEC, "risk_per_trade_pct": 0.05}, "1Y") != a
    assert mr.spec_key(SPEC, "3Y") != a, "the window is part of the baseline"


# ---------------------------------------------------------------- alerting

def test_a_deviation_raises_an_alert_and_a_log_line():
    r = _runner(trades=_trades(40, -1.0))          # every live trade loses
    r.check(now=1000.0)
    assert r.ledger.alerts, "a deviation must reach the operator"
    assert all(a["category"] == "monitor" for a in r.ledger.alerts)
    assert r.ledger.logs


def test_the_same_finding_is_not_re_alerted_within_the_cooldown():
    """A strategy in drawdown produces the same finding every cycle. Without a
    cooldown the operator learns to ignore the channel."""
    r = _runner(trades=_trades(40, -1.0), cooldown_s=3600.0)
    r.check(now=1000.0)
    first = len(r.ledger.alerts)
    r.check(now=1500.0)
    r.check(now=2000.0)
    assert len(r.ledger.alerts) == first


def test_the_finding_is_re_alerted_once_the_cooldown_expires():
    r = _runner(trades=_trades(40, -1.0), cooldown_s=100.0)
    r.check(now=1000.0)
    first = len(r.ledger.alerts)
    r.check(now=1200.0)
    assert len(r.ledger.alerts) > first


def test_a_healthy_strategy_raises_nothing():
    """Live matching the baseline must be silent — an alert channel that fires
    on normal behaviour is noise."""
    r = _runner(trades=_trades(40, 0.5), baseline=BASE)
    out = r.check(now=1000.0)
    assert out["findings"] == [] or out["status"] == "warming_up"
    assert r.ledger.alerts == []


def test_the_alert_carries_the_recommendation_not_just_the_complaint():
    r = _runner(trades=_trades(40, -1.0))
    r.check(now=1000.0)
    assert any(len(a["detail"]) > len(a["title"]) for a in r.ledger.alerts)


def test_a_failing_notifier_does_not_break_the_check():
    def boom(*a, **k):
        raise RuntimeError("telegram down")
    r = _runner(trades=_trades(40, -1.0), notifier=boom)
    assert r.check(now=1000.0)["available"] is True


def test_a_failing_ledger_does_not_break_the_check():
    class BadLedger(FakeLedger):
        def add_alert(self, **kw):
            raise RuntimeError("disk full")
    r = _runner(trades=_trades(40, -1.0))
    r.ledger = BadLedger()
    assert r.check(now=1000.0)["available"] is True


# ------------------------------------------------------------------ status

def test_status_returns_the_last_result_without_recomputing():
    calls = []
    r = _runner(trades=_trades(40, 1.0), calls=calls)
    r.check(now=1000.0)
    s1, s2 = r.status(), r.status()
    assert len(calls) == 1
    assert s1["last_check"] == s2["last_check"]
    assert s1["result"]["available"] is True


def test_status_before_any_check_is_empty_not_fabricated():
    s = _runner().status()
    assert s["result"] is None and s["last_check"] is None
    assert s["running"] is False


def test_the_runner_never_modifies_and_says_so():
    r = _runner(trades=_trades(40, -1.0))
    assert r.check(now=1000.0)["auto_modify"] is False
    for forbidden in ("apply", "fix", "retune", "adjust_strategy"):
        assert not hasattr(r, forbidden)


def test_the_deployed_spec_is_never_written_back():
    eng = FakeEngine()
    before = dict(eng.deployed_spec)
    r = _runner(engine=eng, trades=_trades(40, -1.0))
    r.check(now=1000.0)
    assert eng.deployed_spec == before


# --------------------------------------------------------------- lifecycle

def test_start_is_idempotent_and_stop_ends_the_thread():
    r = _runner(interval_s=0.05)
    assert r.start() is True
    assert r.start() is False, "a second start must not spawn a second thread"
    assert r.status()["running"] is True
    r.stop()


def test_an_exception_inside_check_does_not_kill_the_loop():
    r = _runner()
    calls = {"n": 0}

    def boom(now=None):
        calls["n"] += 1
        raise RuntimeError("bad cycle")
    r.check = boom
    r.interval_s = 0.01
    r.start()
    deadline = __import__("time").time() + 1.0
    while calls["n"] < 2 and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    r.stop()
    assert calls["n"] >= 2, "the loop must survive a failing cycle"


# ------------------------------------------------------------------ endpoint

@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI()
    app.include_router(webhook_api.router)
    return TestClient(app)


def test_monitor_status_endpoint_reports_without_running_a_backtest(client):
    r = client.get("/strategy/monitor/status")
    assert r.status_code == 200
    j = r.json()
    assert "running" in j and "last_check" in j


def test_monitor_check_endpoint_forces_a_cycle(client):
    from config import settings
    r = client.post("/strategy/monitor/check",
                    headers={"X-Webhook-Secret": settings.admin_key})
    assert r.status_code == 200
    assert r.json()["auto_modify"] is False


def test_monitor_check_requires_the_control_credential(client):
    assert client.post("/strategy/monitor/check").status_code == 401


def test_the_first_alert_is_never_swallowed_by_the_cooldown():
    """Regression: a never-sent finding must not count as recently-sent.
    Defaulting the last-sent stamp to 0 only looks correct because wall-clock
    time is huge — it silently ate the first alert of any run whose clock
    started near zero, including every test that passes an explicit `now`."""
    r = _runner(trades=_trades(40, -1.0), cooldown_s=3600.0)
    r.check(now=1.0)
    assert r.ledger.alerts, "the very first finding must alert immediately"


def test_the_timer_can_be_disabled_without_disabling_the_endpoints():
    """Multi-worker deployments would otherwise alert the same deviation once
    per worker. Turning the loop off must leave on-demand checks working."""
    import os
    import subprocess
    import sys
    # webhook_api lives in automation-hub/, which is not the repo root the whole
    # suite runs from — anchor the child process to this package explicitly.
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import webhook_api as w;"
            "print(w.monitor_runner.status()['running'],"
            " w.monitor_runner.check()['auto_modify'])")
    # The child starts in automation-hub/, so include both the hub modules and
    # the repository root that owns the installed ``bot`` package. Docker has
    # ``pip install -e .``; this path makes the source-tree test equivalent.
    repo_root = os.path.dirname(pkg_root)
    env = {**os.environ, "HUB_MONITOR_AGENT": "0",
           "PYTHONPATH": os.pathsep.join((
               pkg_root, repo_root, os.environ.get("PYTHONPATH", ""),
           ))}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=pkg_root, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().endswith("False False"), r.stdout


# ───────────────── the live sample is THIS strategy's trades ─────────────────
#
# Production reported "Live drawdown has reached 36.75R against a worst
# backtest drawdown of 4.3R" for a strategy whose own backtest took a single
# trade in a year. The 36.75R was the peak-to-trough of 2,804 pooled trades
# from every strategy and symbol, none of them attributed to anything.

def _keys(out):
    return {f["key"] for f in out.get("findings") or []}


def test_another_strategys_drawdown_is_not_reported_as_this_ones():
    """The reported bug, in the shape it actually occurred: a deep pooled
    history that this strategy did not produce."""
    pooled = [{"rr": -3.0, "strategy_id": "", "closed_at": "2026-01-05T00:00:00"}
              for _ in range(40)]
    out = _runner(trades=pooled,
                  baseline={**BASE, "max_drawdown_r": 4.3}).check()
    assert "drawdown-exceeds-backtest" not in _keys(out), (
        "pooled trades from other strategies became this strategy's drawdown")
    assert out["attribution"] == {"strategy_id": "s1", "scoped": 0, "pooled": 40}


def test_a_blind_monitor_says_so_rather_than_going_quiet():
    """Scoping correctly must not turn a false CRITICAL into a silent pass.
    A monitor with nothing to look at reads exactly like an all-clear."""
    pooled = [{"rr": -3.0, "strategy_id": "", "closed_at": "2026-01-05T00:00:00"}
              for _ in range(40)]
    out = _runner(trades=pooled).check()
    assert "monitor-unattributed-trades" in _keys(out)
    finding = [f for f in out["findings"] if f["key"] == "monitor-unattributed-trades"][0]
    assert finding["severity"] == "warning"
    assert "40" in finding["detail"]
    assert "not an all-clear" in finding["detail"]
    assert "attribution" in out["note"] or "id" in out["note"]


def test_trades_belonging_to_a_different_strategy_are_never_counted():
    """Attributed, just not to this strategy. Still not its record."""
    other = [{"rr": -3.0, "strategy_id": "someone-else",
              "closed_at": "2026-01-05T00:00:00"} for _ in range(40)]
    out = _runner(trades=other, baseline={**BASE, "max_drawdown_r": 4.3}).check()
    assert "drawdown-exceeds-backtest" not in _keys(out)
    assert out["attribution"]["scoped"] == 0
    assert "monitor-unattributed-trades" in _keys(out)


def test_an_empty_ledger_is_not_an_attribution_failure():
    """Nothing has traded at all. That is a quiet strategy, not a blind
    monitor, and calling it a defect would cry wolf on every fresh install."""
    out = _runner(trades=[]).check()
    assert "monitor-unattributed-trades" not in _keys(out)
    assert out["attribution"] == {"strategy_id": "s1", "scoped": 0, "pooled": 0}


def test_a_strategys_own_drawdown_is_still_reported():
    """The guard must not have disabled the check it was protecting. Same
    deep drawdown, now genuinely attributed to this strategy."""
    mine = _trades(40, -3.0)
    out = _runner(trades=mine, baseline={**BASE, "max_drawdown_r": 4.3}).check()
    assert "drawdown-exceeds-backtest" in _keys(out)
    assert "monitor-unattributed-trades" not in _keys(out)
    assert out["attribution"] == {"strategy_id": "s1", "scoped": 40, "pooled": 40}


def test_a_mixed_ledger_counts_only_this_strategys_trades():
    """The state after attribution is fixed: this strategy's trades alongside
    everyone else's."""
    mixed = _trades(20, 0.5) + [
        {"rr": -9.0, "strategy_id": "other", "closed_at": "2026-01-05T00:00:00"}
        for _ in range(30)]
    out = _runner(trades=mixed).check()
    assert out["attribution"] == {"strategy_id": "s1", "scoped": 20, "pooled": 50}
    assert out["live"]["total_trades"] == 20
    assert out["live"]["net_r"] == 10.0, "only this strategy's R may be summed"
    assert "monitor-unattributed-trades" not in _keys(out)
