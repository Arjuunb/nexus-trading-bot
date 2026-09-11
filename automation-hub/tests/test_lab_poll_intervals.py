"""Lab supervisor intervals must be tunable, and unchanged by default.

Each SMC tick rebuilds a market structure engine over an 800-bar window. On a
5m timeframe a new closed candle appears every 300 seconds, so a five-second
supervisor spends roughly fifty-nine ticks out of sixty recomputing a result
that cannot have changed. That is affordable on a workstation and is not on a
two-core VPS, where it competes with the websocket readers it depends on.

Raising the interval is the one lever that reduces that load without touching
any decision rule, so it has to be reachable from the environment. The default
stays at the value both runtimes were hard-coded to, so an untouched deployment
behaves exactly as before.
"""
import pytest


def _settings(monkeypatch, **env):
    """Build a fresh Settings from the environment.

    Deliberately not importlib.reload(config): every module that imported the
    shared ``settings`` object holds a reference to it, so reloading swaps the
    object out from under them and quietly breaks unrelated tests for the rest
    of the session. Each field reads the environment through a default_factory,
    so constructing the dataclass gives the same answer without that damage.
    """
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    from config import Settings
    return Settings()


def test_defaults_match_the_previously_hard_coded_interval(monkeypatch):
    settings = _settings(monkeypatch, HUB_SMC_POLL=None, HUB_PA_POLL=None)
    assert settings.smc_poll_s == 5.0
    assert settings.price_action_poll_s == 5.0


def test_intervals_are_configurable(monkeypatch):
    settings = _settings(monkeypatch, HUB_SMC_POLL="30", HUB_PA_POLL="20")
    assert settings.smc_poll_s == 30.0
    assert settings.price_action_poll_s == 20.0


@pytest.mark.parametrize("requested,expected", [("30", 30.0), ("0.1", 1.0)])
def test_smc_runtime_honours_the_configured_interval(requested, expected, tmp_path):
    """The runtime clamps to a one-second floor, so a typo cannot busy-loop."""
    from services.smc_strategy_lab import SMCPaperAccount, SMCStrategyLabRuntime

    account = SMCPaperAccount(tmp_path / f"smc-{requested}.db", starting_balance=10_000.0)
    runtime = SMCStrategyLabRuntime(object(), account, autostart=False,
                                    poll_seconds=float(requested))
    try:
        assert runtime.poll_seconds == expected
    finally:
        runtime.stop()


@pytest.mark.parametrize("requested,expected", [("30", 30.0), ("0.1", 1.0)])
def test_price_action_runtime_honours_the_configured_interval(requested, expected, tmp_path):
    from services.price_action_lab import PriceActionLabRuntime, PriceActionPaperAccount

    account = PriceActionPaperAccount(tmp_path / f"pa-{requested}.db", starting_balance=10_000.0)

    class _Market:
        @staticmethod
        def public_usdm_window(*_args, **_kwargs):
            return []

    runtime = PriceActionLabRuntime(_Market(), account, autostart=False,
                                    poll_seconds=float(requested))
    try:
        assert runtime.poll_seconds == expected
    finally:
        runtime.stop()
