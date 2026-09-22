"""The agent reading its own history before deciding.

The risk this module carries is not a crash, it is a plausible-looking
conclusion drawn from eight trades. Most of these tests are about refusing to
have an opinion.
"""
import pytest

from services.smc_agent_memory import (PATTERN_EXPECTANCY, MemoryPolicy,
                                       family_of, memory_gates, recall)

FAMILY = "SMC_S5_ORDER_BLOCK_RETEST"
SETUP = f"{FAMILY}-BTCUSDT-5M-BULLISH-20260922T101500"

SPEAKING = MemoryPolicy(enabled=True, min_sample=10).validated()


def trades(*realised, family=FAMILY, hour=10):
    return [{"setup_id": f"{family}-BTCUSDT-5M-BULLISH-2026092{index % 9}",
             "closed_at": f"2026-09-22T{hour:02d}:0{index % 10}:00+00:00",
             "realised_r": value}
            for index, value in enumerate(realised)]


def failed(gates):
    return [gate.name for gate in gates if not gate.passed]


def test_the_family_is_the_pattern_without_the_instrument_or_moment():
    assert family_of(SETUP) == FAMILY
    assert family_of("") == ""
    assert family_of(None) == ""


# ──────────────────────────── refusing to speak ───────────────────────────

def test_a_disabled_policy_recalls_nothing():
    gates = memory_gates(closed_trades=trades(*([-1.0] * 40)), setup_id=SETUP,
                         policy=MemoryPolicy(min_sample=10))

    assert gates == []


def test_below_the_sample_floor_it_has_no_opinion():
    """Nine losing trades in a row is exactly the evidence that feels
    conclusive and is not."""
    gates = memory_gates(closed_trades=trades(*([-1.0] * 9)), setup_id=SETUP,
                         policy=SPEAKING)

    assert failed(gates) == []
    assert "10 needed before history may veto" in gates[0].detail


def test_a_sample_floor_below_five_is_refused():
    with pytest.raises(ValueError, match="anecdote"):
        MemoryPolicy(enabled=True, min_sample=3).validated()


def test_a_signal_with_no_setup_id_is_not_judged():
    gates = memory_gates(closed_trades=trades(*([-1.0] * 40)), setup_id="",
                         policy=SPEAKING)

    assert failed(gates) == []
    assert "no setup id" in gates[0].detail


def test_another_familys_history_is_not_borrowed():
    losing_other = trades(*([-1.0] * 30), family="SMC_S1_SWEEP_REVERSAL")

    gates = memory_gates(closed_trades=losing_other, setup_id=SETUP,
                         policy=SPEAKING)

    assert failed(gates) == []
    assert gates[0].value == 0.0, "it counted another family's trades"


# ──────────────────────────────── the veto ────────────────────────────────

def test_a_family_that_has_lost_over_a_real_sample_is_vetoed():
    gates = memory_gates(closed_trades=trades(*([-1.0] * 12)), setup_id=SETUP,
                         policy=SPEAKING)

    assert failed(gates) == [PATTERN_EXPECTANCY]
    assert "-1.00R per trade over 12 closed trades" in gates[0].detail


def test_a_profitable_family_is_left_alone():
    gates = memory_gates(closed_trades=trades(*([3.0, -1.0] * 6)), setup_id=SETUP,
                         policy=SPEAKING)

    assert failed(gates) == []


def test_a_low_win_rate_at_high_r_is_not_vetoed():
    """The 1:3 floor exists to find setups that lose often and pay well.
    Judging on win rate would veto exactly those."""
    # 3 wins at +4R, 9 losses at -1R: 25% win rate, +0.25R expectancy.
    gates = memory_gates(closed_trades=trades(*([4.0] * 3 + [-1.0] * 9)),
                         setup_id=SETUP, policy=SPEAKING)

    assert failed(gates) == []
    assert gates[0].value == pytest.approx(0.25)


def test_exactly_at_the_floor_is_vetoed_not_allowed():
    """A family that has returned precisely nothing over a real sample has
    paid for its risk with nothing."""
    gates = memory_gates(closed_trades=trades(*([1.0, -1.0] * 6)),
                         setup_id=SETUP, policy=SPEAKING)

    assert failed(gates) == [PATTERN_EXPECTANCY]


# ──────────────────────────────── by hour ─────────────────────────────────

def test_the_hour_split_is_off_by_default():
    assert MemoryPolicy().by_hour is False


def test_with_the_hour_split_only_that_hours_history_counts():
    policy = MemoryPolicy(enabled=True, min_sample=10, by_hour=True).validated()
    history = trades(*([-1.0] * 12), hour=3) + trades(*([2.0] * 12), hour=10)

    at_three = memory_gates(closed_trades=history, setup_id=SETUP,
                            policy=policy, hour=3)
    at_ten = memory_gates(closed_trades=history, setup_id=SETUP,
                          policy=policy, hour=10)

    assert failed(at_three) == [PATTERN_EXPECTANCY]
    assert failed(at_ten) == []


def test_the_hour_split_needs_the_full_sample_within_that_hour():
    """Splitting by hour multiplies the buckets, so the floor must apply per
    bucket -- otherwise the split silently lowers the evidence bar."""
    policy = MemoryPolicy(enabled=True, min_sample=10, by_hour=True).validated()
    # 30 losers overall, but only 4 in this hour.
    history = trades(*([-1.0] * 26), hour=3) + trades(*([-1.0] * 4), hour=10)

    gates = memory_gates(closed_trades=history, setup_id=SETUP, policy=policy,
                         hour=10)

    assert failed(gates) == []


# ──────────────────────────────── counting ────────────────────────────────

def test_an_unrecorded_outcome_is_skipped_not_counted_as_a_scratch():
    """None is not zero. Averaging it in would drag expectancy toward
    nothing and manufacture a veto out of missing data."""
    history = trades(*([2.0] * 12))
    history.append({"setup_id": SETUP, "closed_at": "2026-09-22T10:00:00+00:00",
                    "realised_r": None})

    seen = recall(history, family=FAMILY, policy=SPEAKING)

    assert seen.sample == 12
    assert seen.expectancy_r == pytest.approx(2.0)


def test_recall_reports_the_shape_of_the_sample_not_just_a_verdict():
    seen = recall(trades(*([3.0] * 4 + [-1.0] * 8)), family=FAMILY,
                  policy=SPEAKING)

    assert (seen.sample, seen.wins, seen.losses) == (12, 4, 8)
    assert seen.win_rate == pytest.approx(4 / 12)
    assert seen.sufficient is True


def test_memory_can_only_ever_veto():
    """Swept across samples and outcomes: every gate is a verdict and a
    number, and carries nothing that could instruct the agent to act."""
    for values in ([], [-1.0] * 30, [5.0] * 30, [1.0, -1.0] * 15):
        for policy in (SPEAKING, MemoryPolicy(enabled=True, min_sample=10,
                                              by_hour=True).validated()):
            for gate in memory_gates(closed_trades=trades(*values),
                                     setup_id=SETUP, policy=policy, hour=10):
                assert isinstance(gate.passed, bool)
                assert gate.detail
                assert not hasattr(gate, "size")
                assert not hasattr(gate, "action")
