"""Context vetoes: the setups a human would pass on.

Every rule here can only skip a trade. The tests that matter most are the
ones proving it cannot do anything else -- and that a rule whose input is
missing fails closed rather than quietly switching itself off.
"""
from datetime import datetime, timezone

import pytest

from services.smc_agent_context import (CONSECUTIVE_LOSSES, DAILY_LOSS_CAP,
                                        MINIMUM_VOLATILITY, SESSION_HOURS,
                                        ContextPolicy, context_gates)

NOON = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def closed(*realised, day="2026-09-22"):
    """Closed trades, most-recent-first, as the journal returns them."""
    return [{"closed_at": f"{day}T10:0{index}:00+00:00", "realised_r": value}
            for index, value in enumerate(realised)]


def candles(*ranges, close=100.0):
    return [{"high": close + span / 2, "low": close - span / 2, "close": close}
            for span in ranges]


def failed(gates):
    return [gate.name for gate in gates if not gate.passed]


# ───────────────────────────────────── off ────────────────────────────────

def test_a_disabled_policy_checks_nothing():
    gates = context_gates(now=NOON, closed_trades=closed(-5.0, -1.0, -1.0),
                          policy=ContextPolicy(daily_loss_cap_r=1.0,
                                               max_consecutive_losses=2))

    assert gates == []


def test_an_enabled_policy_with_no_rules_set_vetoes_nothing():
    gates = context_gates(now=NOON, policy=ContextPolicy(enabled=True))

    assert gates == []


# ─────────────────────────────── daily loss cap ───────────────────────────

def test_the_day_stops_once_the_loss_cap_is_reached():
    policy = ContextPolicy(enabled=True, daily_loss_cap_r=2.0).validated()

    gates = context_gates(now=NOON, closed_trades=closed(-1.0, -1.2), policy=policy)

    assert failed(gates) == [DAILY_LOSS_CAP]
    assert "no more trades today" in gates[0].detail


def test_a_day_inside_the_cap_still_trades():
    policy = ContextPolicy(enabled=True, daily_loss_cap_r=2.0).validated()

    gates = context_gates(now=NOON, closed_trades=closed(-1.0, -0.5), policy=policy)

    assert failed(gates) == []


def test_yesterdays_losses_do_not_stop_today():
    """The cap is a daily stop. Carrying it over would stand the agent down
    for a run of days after one bad session."""
    policy = ContextPolicy(enabled=True, daily_loss_cap_r=2.0).validated()

    gates = context_gates(now=NOON, policy=policy,
                          closed_trades=closed(-3.0, -4.0, day="2026-09-21"))

    assert failed(gates) == []


def test_a_winning_day_offsets_earlier_losses():
    policy = ContextPolicy(enabled=True, daily_loss_cap_r=2.0).validated()

    gates = context_gates(now=NOON, closed_trades=closed(-1.5, -1.5, 3.0),
                          policy=policy)

    assert failed(gates) == []


def test_an_open_trade_does_not_count_toward_the_day():
    """realised_r is None until it closes. Counting it as zero would be
    harmless; counting it as a loss would not."""
    policy = ContextPolicy(enabled=True, daily_loss_cap_r=1.0).validated()
    rows = [{"closed_at": None, "realised_r": None},
            {"closed_at": "2026-09-22T10:00:00+00:00", "realised_r": -0.5}]

    assert failed(context_gates(now=NOON, closed_trades=rows, policy=policy)) == []


# ────────────────────────────── consecutive losses ────────────────────────

def test_three_losses_in_a_row_stand_the_agent_down():
    policy = ContextPolicy(enabled=True, max_consecutive_losses=3).validated()

    gates = context_gates(now=NOON, closed_trades=closed(-1.0, -1.0, -1.0),
                          policy=policy)

    assert failed(gates) == [CONSECUTIVE_LOSSES]


def test_a_win_breaks_the_streak():
    policy = ContextPolicy(enabled=True, max_consecutive_losses=2).validated()

    # most-recent-first: the newest trade won.
    gates = context_gates(now=NOON, closed_trades=closed(1.5, -1.0, -1.0),
                          policy=policy)

    assert failed(gates) == []


def test_a_scratch_breaks_the_streak_rather_than_extending_it():
    """A breakeven exit is not a loss, and a run of them is not a reason to
    stop -- especially now that the trade manager creates them."""
    policy = ContextPolicy(enabled=True, max_consecutive_losses=2).validated()

    gates = context_gates(now=NOON, closed_trades=closed(0.0, -1.0, -1.0),
                          policy=policy)

    assert failed(gates) == []


# ──────────────────────────────── session hours ───────────────────────────

@pytest.mark.parametrize("hour, blocked", [(3, True), (8, False), (16, False),
                                           (22, True)])
def test_only_the_named_hours_are_traded(hour, blocked):
    policy = ContextPolicy(enabled=True,
                           allowed_hours_utc=((7, 11), (13, 17))).validated()
    moment = NOON.replace(hour=hour)

    gates = context_gates(now=moment, policy=policy)

    assert (SESSION_HOURS in failed(gates)) is blocked


def test_a_window_that_does_not_move_forward_is_refused():
    """A session spanning midnight is two ranges. Accepting 22-2 would make
    the comparison silently match nothing."""
    with pytest.raises(ValueError, match="does not move forward"):
        ContextPolicy(enabled=True, allowed_hours_utc=((22, 2),)).validated()


def test_a_naive_timestamp_is_read_as_utc_not_local():
    policy = ContextPolicy(enabled=True, allowed_hours_utc=((7, 11),)).validated()

    gates = context_gates(now=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc),
                          policy=policy)

    assert failed(gates) == []


# ───────────────────────────────── volatility ─────────────────────────────

def test_a_flat_range_is_skipped():
    policy = ContextPolicy(enabled=True, min_candle_range_bps=20.0,
                           volatility_lookback=3).validated()

    # 0.1 wide on a 100 close is 10 bps, under the 20 bps floor.
    gates = context_gates(now=NOON, candles=candles(0.1, 0.1, 0.1), policy=policy)

    assert failed(gates) == [MINIMUM_VOLATILITY]


def test_a_live_range_passes():
    policy = ContextPolicy(enabled=True, min_candle_range_bps=20.0,
                           volatility_lookback=3).validated()

    gates = context_gates(now=NOON, candles=candles(0.5, 0.6, 0.4), policy=policy)

    assert failed(gates) == []


def test_unmeasurable_volatility_fails_closed():
    """A rule that was asked for and cannot be evaluated must not pass. A
    gate that silently succeeds when its input is missing is a rule that
    switches itself off exactly when the data is worst."""
    policy = ContextPolicy(enabled=True, min_candle_range_bps=20.0).validated()

    assert failed(context_gates(now=NOON, candles=[], policy=policy)) == [MINIMUM_VOLATILITY]
    assert failed(context_gates(now=NOON, policy=policy,
                                candles=[{"high": 1.0, "low": None, "close": 1.0}])) \
        == [MINIMUM_VOLATILITY]
    assert failed(context_gates(now=NOON, policy=policy,
                                candles=[{"high": 1.0, "low": 0.5, "close": 0.0}])) \
        == [MINIMUM_VOLATILITY]


# ──────────────────────────── the asymmetry itself ────────────────────────

def test_no_rule_can_ever_make_the_agent_trade():
    """The safety argument for the whole module: swept across every rule and
    a spread of inputs, a gate either passes (says nothing) or fails (skips).
    Nothing here returns a reason to act, a size, or a plan."""
    policy = ContextPolicy(enabled=True, daily_loss_cap_r=2.0,
                           max_consecutive_losses=2,
                           allowed_hours_utc=((7, 11),),
                           min_candle_range_bps=20.0,
                           volatility_lookback=3).validated()

    for trades in (closed(), closed(5.0, 5.0), closed(-9.0), closed(0.0, -1.0)):
        for bars in ([], candles(0.01, 0.01, 0.01), candles(2.0, 2.0, 2.0)):
            for hour in range(0, 24, 4):
                gates = context_gates(now=NOON.replace(hour=hour),
                                      closed_trades=trades, candles=bars,
                                      policy=policy)
                for gate in gates:
                    assert isinstance(gate.passed, bool)
                    assert gate.detail, "a gate must say what it judged"
                    # A gate carries a verdict and a number. It has no field
                    # that could instruct the agent to do anything.
                    assert not hasattr(gate, "size")
                    assert not hasattr(gate, "action")
