"""Whose trades are they, really.

An instance's record is every paper trade it ever closed, scoped by
instance_id and never by strategy. Switch the strategy and the old one's wins
and losses keep being reported under the new one's name -- on the card, and in
the "best measured instance" banner. This script is how that gets noticed.
"""
from __future__ import annotations

import importlib.util
import io
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trade_attribution.py"


@pytest.fixture(scope="module")
def attribution():
    spec = importlib.util.spec_from_file_location("trade_attribution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ledger(tmp_path, rows):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE paper_trades(
        id TEXT PRIMARY KEY, symbol TEXT, status TEXT, strategy_id TEXT,
        instance_id TEXT, pnl REAL)""")
    db.executemany("INSERT INTO paper_trades VALUES (?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return path


def _run(attribution, path, **kwargs):
    out = io.StringIO()
    options = {"symbol": None, "instance": None}
    options.update(kwargs)
    totals = attribution.attribute(str(path), out=out, **options)
    return totals, out.getvalue()


def test_a_single_strategy_instance_is_reported_as_correct(attribution, tmp_path):
    path = _ledger(tmp_path, [
        (f"t{n}", "BNBUSDT", "closed", "pa_rulebook:0.1.0", "inst-1", 1.0 if n < 2 else -1.0)
        for n in range(5)])
    totals, text = _run(attribution, path)
    assert totals["mixed"] == []
    assert "attribution is correct" in text
    assert "MIXED" not in text


def test_a_switched_instance_is_flagged(attribution, tmp_path):
    """The case that matters: 24 trades on the card, most of them from a
    strategy that is no longer configured."""
    rows = [(f"old{n}", "BNBUSDT", "closed", "donchian_breakout:1.0.0", "inst-1", -0.5)
            for n in range(20)]
    rows += [(f"new{n}", "BNBUSDT", "closed", "pa_rulebook:0.1.0", "inst-1", 2.0)
             for n in range(4)]
    totals, text = _run(attribution, path := _ledger(tmp_path, rows))
    assert totals["mixed"] == ["inst-1"]
    assert "MIXED ATTRIBUTION" in text
    assert "donchian_breakout:1.0.0" in text and "pa_rulebook:0.1.0" in text
    assert "under whichever strategy is configured now" in text


def test_average_win_and_loss_are_reported(attribution, tmp_path):
    """The number that says whether a trade reached the target its gate asked
    for. A 2.5R gate whose winners average 0.6R is exiting somewhere else."""
    rows = [("w1", "BNBUSDT", "closed", "pa_rulebook:0.1.0", "inst-1", 3.0),
            ("l1", "BNBUSDT", "closed", "pa_rulebook:0.1.0", "inst-1", -2.0),
            ("l2", "BNBUSDT", "closed", "pa_rulebook:0.1.0", "inst-1", -4.0)]
    _, text = _run(attribution, _ledger(tmp_path, rows))
    assert "avg win 3.00" in text
    assert "avg loss 3.00" in text
    assert "PF 0.5" in text
    assert "net -3.00" in text


def test_open_trades_are_counted_but_not_scored(attribution, tmp_path):
    rows = [("o1", "BNBUSDT", "open", "pa_rulebook:0.1.0", "inst-1", None),
            ("o2", "BNBUSDT", "open", "pa_rulebook:0.1.0", "inst-1", None)]
    _, text = _run(attribution, _ledger(tmp_path, rows))
    assert "closed    0" in text
    assert "win " not in text          # no win rate invented from nothing


def test_an_unattributed_trade_is_named_rather_than_dropped(attribution, tmp_path):
    """Trades written before strategy_id existed keep an empty one; silently
    folding them into the current strategy is the bug, not the fix."""
    rows = [("t1", "BNBUSDT", "closed", "", "inst-1", -1.0),
            ("t2", "BNBUSDT", "closed", "pa_rulebook:0.1.0", "inst-1", 1.0)]
    totals, text = _run(attribution, _ledger(tmp_path, rows))
    assert "(unattributed)" in text
    assert totals["mixed"] == ["inst-1"]


def test_it_cannot_write_to_the_ledger(attribution, tmp_path):
    path = _ledger(tmp_path, [("t1", "BNBUSDT", "closed", "x", "inst-1", 1.0)])
    before = path.read_bytes()
    _run(attribution, path)
    assert path.read_bytes() == before
