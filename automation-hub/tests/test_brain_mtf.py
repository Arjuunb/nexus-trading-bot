"""True multi-timeframe confirmation in the DecisionBrain.

Measured on the seeded synthetic regime grid before defaulting ON
(pessimistic fills, 150-bar time stop, tune + holdout seed sets):
    tune:    off +211.1R (559 trades) -> damp +240.1R (494)
    holdout: off +268.3R (585 trades) -> damp +282.6R (494)
The counter-HTF subset under "off" was net-negative in both suites
(-29.0R / -11.8R); damping removes exactly those entries.
"""
from datetime import datetime, timedelta, timezone

from bot.data.synthetic import generate_bars
from bot.types import Bar
from strategies.brain_strategy import DecisionBrain, _htf_trend_vote


def _trend_bars(up: bool, n: int = 400):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    factor = 1.005 if up else 0.995
    return [Bar(start + timedelta(minutes=5 * i), 100, 101, 99,
                100 * (factor ** i), 1000) for i in range(n)]


# ------------------------------------------------------------- the HTF vote
def test_htf_vote_reads_uptrend():
    assert _htf_trend_vote(_trend_bars(True), 12) == 1.0


def test_htf_vote_reads_downtrend():
    assert _htf_trend_vote(_trend_bars(False), 12) == -1.0


def test_htf_vote_honest_without_history():
    # < 24 aggregated buckets -> no read, never a guess
    assert _htf_trend_vote(_trend_bars(True, 100), 12) is None
    assert _htf_trend_vote(_trend_bars(True, 500), 1) is None  # mult<=1 = not an HTF


# ------------------------------------------------- damping filters counter-HTF
def _signals(mode: str, seed: int = 2050):
    """Run the brain over a seeded scenario; classify each emitted signal by
    whether it agreed with the (off-mode) HTF read at that moment."""
    strat = DecisionBrain("TEST", htf_mode=mode, allow_legacy_htf_resample=True)
    counter = agree = 0
    for bar in generate_bars(n=1200, timeframe="5m", drift_per_bar=0.0003,
                             vol_per_bar=0.010, seed=seed):
        sig = strat.on_bar(bar)
        if sig is None:
            continue
        v = _htf_trend_vote(strat.bars, 12)
        sd = 1.0 if sig.type.name == "LONG" else -1.0
        if v is not None and v * sd < 0:
            counter += 1
        elif v is not None:
            agree += 1
    return counter, agree


def test_damp_removes_counter_htf_entries_and_keeps_agreeing_ones():
    off_counter, off_agree = _signals("off")
    damp_counter, damp_agree = _signals("damp")
    assert off_counter > 0                # the raw brain does fire against HTF
    assert damp_counter == 0              # damping filters every one of them
    assert damp_agree == off_agree        # agreeing entries pass untouched


def test_incomplete_epoch_bucket_does_not_repaint_closed_htf_vote():
    rows = _trend_bars(True, 396)  # 33 complete 1h buckets
    baseline = _htf_trend_vote(rows, 12)
    # Eleven bars of the next left-closed bucket cannot alter the closed HTF read.
    extended = _trend_bars(True, 407)
    assert _htf_trend_vote(extended, 12) == baseline


# --------------------------------------------------------- honest reporting
def test_snapshot_and_checklist_carry_the_htf_read():
    strat = DecisionBrain("TEST", htf_mode="damp", allow_legacy_htf_resample=True)
    sigs = []
    for bar in generate_bars(n=1200, timeframe="5m", drift_per_bar=0.0008,
                             vol_per_bar=0.006, seed=7):
        s = strat.on_bar(bar)
        if s is not None:
            sigs.append(s)
    assert sigs, "expected at least one signal in a strong uptrend"
    s = sigs[-1]
    assert s.snapshot["htf_trend"] in ("up", "down")
    item = next(c for c in s.checklist if c["name"].startswith("Native HTF trend"))
    assert item["status"] in ("Passed", "Failed", "Neutral")


def test_htf_off_reports_not_checked():
    strat = DecisionBrain("TEST", htf_mode="off", allow_legacy_htf_resample=True)
    sigs = []
    for bar in generate_bars(n=1200, timeframe="5m", drift_per_bar=0.0008,
                             vol_per_bar=0.006, seed=7):
        s = strat.on_bar(bar)
        if s is not None:
            sigs.append(s)
    assert sigs
    item = next(c for c in sigs[-1].checklist if c["name"].startswith("Native HTF trend"))
    assert item["status"] == "Not checked"
    assert sigs[-1].snapshot["htf_trend"] is None
