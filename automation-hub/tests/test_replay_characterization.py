"""Replay characterization gate (Sprint 4, S4.0 for the replay engine).

docs/TRADECORE_PLAN.md §6: build the gate BEFORE changing an engine. S4.4 will
replace services/replay.py's inline TP1/BE/TP2 state machine with the shared
TradeManager — a change that CAN move user-visible journal numbers. This test
pins replay's current per-trade output (side/entry/exit/sl/rr/result/
exit_reason/partial) on deterministic synthetic fixtures, so that any shift
becomes a visible, reviewable snapshot diff instead of a silent regression.

The fixtures deliberately cover the exit paths S4.4 touches:
  - "Break-even stop after partial"  (the TP1 -> partial -> BE path)
  - "Stop loss hit"

Regenerating the snapshot (ONLY when a change is intended and reviewed):
    cd automation-hub && python -m tests.test_replay_characterization --update
Then eyeball the git diff of tests/fixtures/replay_snapshot.json — that diff IS
the user-visible impact of the change.
"""
import json
import os

import pytest

SNAPSHOT = os.path.join(os.path.dirname(__file__), "fixtures", "replay_snapshot.json")

# (symbol, exec_tf, limit, strategy) — must match the snapshot keys.
#
# The two Supply/Demand rows are EMPTY, and that is the pinned fact. Replay's
# "Supply/Demand" is strategies/smc_strategy.py, which used to be a
# self-contained confluence model (a sweep, a structure shift and a fair-value
# gap each merely recent, in any order, with no retest and no invalidation) and
# is now a thin adapter over the SMC Strategy Lab's sequential state machine.
# The state machine opens 164 setups on this synthetic series and invalidates or
# expires every one of them, so it proposes nothing — the same verdict the Lab
# reaches on the same data. Their emptiness is therefore load-bearing: if a
# future change makes SMC fire loosely on a random walk again, these two rows
# turn non-empty and this gate fails. Do not delete them to "clean up".
#
# Because they no longer carry trades, Support/Resistance Rejection supplies the
# exit-path coverage the gate exists for — and covers "Final take-profit
# reached", which the snapshot never guarded before.
CASES = [
    ("BTCUSDT", "15m", 1200, "Supply/Demand"),
    ("ETHUSDT", "15m", 1200, "Supply/Demand"),
    ("BTCUSDT", "15m", 1200, "Trend Following"),
    ("BTCUSDT", "15m", 1200, "Support/Resistance Rejection"),
]


def _key(sym, tf, lim, strat):
    return f"{sym}|{tf}|{lim}|{strat}"


def _closed_rows(sym, tf, lim, strat):
    """Replay's closed trades, reduced to the fields a user actually sees."""
    from services.replay import build_replay
    r = build_replay(sym, tf, lim, strategy=strat, source="demo")
    return [
        {"side": t["side"], "entry": round(t["entry"], 6), "exit": round(t["exit"], 6),
         "sl": round(t["sl"], 6), "rr": t["rr"], "result": t["result"],
         "exit_reason": t.get("exit_reason"), "partial": bool(t.get("partial"))}
        for t in r["trades"] if t.get("rr") is not None
    ]


def _build_snapshot() -> dict:
    return {_key(*c): _closed_rows(*c) for c in CASES}


def _load() -> dict:
    with open(SNAPSHOT, encoding="utf-8") as f:
        return json.load(f)


def test_snapshot_exists_and_covers_the_paths_s44_changes():
    snap = _load()
    assert set(snap) == {_key(*c) for c in CASES}
    reasons = {row["exit_reason"] for rows in snap.values() for row in rows}
    # the multi-stage partial/break-even path, a plain stop AND the run to the
    # final target must all be represented, or the gate wouldn't actually guard
    # S4.4's TP1 -> partial -> break-even -> TP2 machinery.
    assert any("Break-even" in (r or "") for r in reasons), reasons
    assert any("Stop loss" in (r or "") for r in reasons), reasons
    assert any("Final take-profit" in (r or "") for r in reasons), reasons
    assert sum(len(v) for v in snap.values()) >= 5


def test_replay_is_deterministic():
    """A characterization gate is only meaningful if the engine is reproducible."""
    sym, tf, lim, strat = CASES[0]
    assert _closed_rows(sym, tf, lim, strat) == _closed_rows(sym, tf, lim, strat)


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[3]}")
def test_replay_output_matches_snapshot(case):
    """The gate itself: replay's trade output must equal the committed baseline.

    If this fails after an intentional engine change, regenerate the snapshot
    and REVIEW the diff — it is the user-visible impact."""
    got = _closed_rows(*case)
    want = _load()[_key(*case)]
    assert got == want, (
        f"replay output changed for {_key(*case)}.\n"
        f"expected {len(want)} trades, got {len(got)}.\n"
        "If this change is intended, regenerate with:\n"
        "  python -m tests.test_replay_characterization --update\n"
        "and review the snapshot diff."
    )


def test_s44_documented_semantic_change_wide_bar_reaches_target_same_bar():
    """S4.4's ONE intentional behaviour change, pinned so it is never a surprise.

    Replay's old inline state machine, on a single bar that traded through BOTH
    the 1R partial AND the final target, would book the partial and leave the
    runner OPEN until a later bar. The shared TradeManager closes it on that bar
    at the target — which is the more faithful reading of the data (price really
    did reach the target on that bar), so the old behaviour was a latent bug.

    This case does NOT occur anywhere in the snapshot fixtures (88 closed trades
    across 24 symbol/timeframe/strategy combinations showed byte-identical
    output), because a synthetic candle rarely spans 2R — but it is reachable on
    real, volatile data, so it is asserted here rather than left implicit."""
    from services.replay import PARTIAL_FRAC, TP1_R
    from services.trade_manager import ManagedTrade, TradeManager

    # long: entry 100, stop 95 (risk 5) -> TP1 at 105 (1R), final target 115 (3R)
    mt = ManagedTrade(side="long", entry=100.0, stop=95.0, target=115.0, risk=5.0)
    mgr = TradeManager(be_at_r=TP1_R, scale_at_r=TP1_R, scale_frac=PARTIAL_FRAC,
                       trail_r=0.0, max_hold_bars=0)
    act = mgr.on_bar(mt, high=116.0, low=99.0, close=115.5)   # one wide bar

    assert act.partial_price == 105.0        # partial booked at 1R
    assert act.exit_price == 115.0           # AND closed at the target, same bar
    assert act.exit_reason == "target"
    assert mt.scaled is True
    # blended R is the same formula replay always used: 0.5*1R + 0.5*3R
    assert mgr.r_multiple(mt, act.exit_price, act.partial_price) == pytest.approx(2.0)


if __name__ == "__main__":  # pragma: no cover - maintenance helper
    import sys
    if "--update" in sys.argv:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            f.write(json.dumps(_build_snapshot(), indent=2, sort_keys=True) + "\n")
        print(f"snapshot written -> {SNAPSHOT}")
    else:
        print(__doc__)
