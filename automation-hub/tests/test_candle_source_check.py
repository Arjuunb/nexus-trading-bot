"""Which candle sources a host can reach, and how old each one is.

When the Lab's chart says STALE the useful question is which source answered.
This script says so from a shell, without reading it off a chart -- so the
properties worth pinning are that it asks the platform's own freshness
authority rather than forming a second opinion, that it never confuses
unreachable with stale, and that it cannot write anything.
"""
from __future__ import annotations

import ast
import importlib.util
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.types import Bar

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "candle_source_check.py"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("candle_source_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _series(n, *, newest, minutes=5):
    step = timedelta(minutes=minutes)
    return [Bar(newest - step * i, 100.0, 101.0, 99.0, 100.5, 10.0)
            for i in range(n)][::-1]


def _last_closed_open(minutes=5):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now - timedelta(minutes=now.minute % minutes) - timedelta(minutes=minutes)


def _fresh(n=20):
    return _series(n, newest=_last_closed_open())


def _stale(n=20):
    return _series(n, newest=_last_closed_open() - timedelta(days=2))


def _run(checker, monkeypatch, *, venue=None, venue_error=None, cache=None,
         cache_source="local store (real)", cache_error=None):
    import data.market_data as market_data
    import services.native_smc_live_visual as live

    if venue_error is not None:
        def _raise(*_a, **_k):
            raise live.NativeSMCLiveDataUnavailable(venue_error)
        monkeypatch.setattr(live, "fetch_venue_ohlcv", _raise)
    else:
        monkeypatch.setattr(live, "fetch_venue_ohlcv", lambda *a, **k: list(venue or []))

    if cache_error is not None:
        def _fail(*_a, **_k):
            raise RuntimeError(cache_error)
        monkeypatch.setattr(market_data, "get_bars", _fail)
    else:
        monkeypatch.setattr(market_data, "get_bars",
                            lambda *a, **k: (list(cache or []), cache_source))

    out = io.StringIO()
    result = checker.check("BTCUSDT", "5m", "binance_usdm", 50, out)
    return result, out.getvalue()


def test_a_reachable_fresh_venue_is_reported_as_fresh(checker, monkeypatch):
    result, text = _run(checker, monkeypatch, venue=_fresh(), cache=_stale())
    assert result["any_fresh"] is True
    assert "CHART WILL BE FRESH" in text
    assert "venue read (binance_usdm)" in text


def test_an_unreachable_venue_is_not_reported_as_stale(checker, monkeypatch):
    """Unreachable and stale are different problems with different fixes, and a
    line that conflates them sends an operator to the wrong one."""
    result, text = _run(checker, monkeypatch, venue_error="binance unreachable",
                        cache=_fresh())
    venue_row = result["sources"][0]
    assert venue_row["status"] == "UNAVAILABLE"
    assert venue_row["candles"] == 0
    assert "binance unreachable" in text
    assert "[DOWN" in text
    # The cache was fresh, so the chart still will be.
    assert result["any_fresh"] is True


def test_every_source_behind_says_stale_rather_than_broken(checker, monkeypatch):
    result, text = _run(checker, monkeypatch, venue=_stale(), cache=_stale())
    assert result["any_fresh"] is False
    assert "CHART WILL SAY STALE" in text
    assert "honest state, not a display bug" in text
    assert "it does not substitute anything" in text


def test_nothing_reachable_is_called_out_separately(checker, monkeypatch):
    result, text = _run(checker, monkeypatch, venue_error="no route to host",
                        cache_error="no verified real candles")
    assert result["any_fresh"] is False
    assert "NO REAL CANDLES ON THIS HOST" in text
    assert "fail closed" in text
    assert "no verified real candles" in text


def test_the_verdict_comes_from_the_platform_authority(checker):
    """Not a second opinion formed in the script."""
    source = SCRIPT.read_text()
    assert "from services.market_data_freshness import assess_timeframe" in source
    for banned in ("def _is_fresh", "ALLOWED_AGE", "TOLERANCE =", "* 1.5"):
        assert banned not in source, f"the script must not judge freshness itself ({banned})"


def test_it_demands_real_data_from_the_cache(checker, monkeypatch):
    """The same rule the Lab follows: no sample series, no synthetic generator."""
    seen = {}

    import data.market_data as market_data
    import services.native_smc_live_visual as live

    monkeypatch.setattr(live, "fetch_venue_ohlcv", lambda *a, **k: _fresh())

    def _record(symbol, n=0, timeframe="", require_real=False):
        seen["require_real"] = require_real
        return _fresh(), "local store (real)"

    monkeypatch.setattr(market_data, "get_bars", _record)
    checker.check("BTCUSDT", "5m", "binance_usdm", 50, io.StringIO())
    assert seen["require_real"] is True


def test_the_exit_code_reflects_whether_anything_is_fresh(checker, monkeypatch):
    monkeypatch.setattr(checker, "check", lambda *a, **k: {"any_fresh": True})
    assert checker.main([]) == 0
    monkeypatch.setattr(checker, "check", lambda *a, **k: {"any_fresh": False})
    assert checker.main([]) == 1


def test_it_cannot_write_anything():
    """Read-only by construction, checked structurally rather than by eye."""
    tree = ast.parse(SCRIPT.read_text())
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    for banned in ("execute", "executemany", "commit", "write_text", "post", "put"):
        assert banned not in called, f"the check must not call {banned}"
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "open" not in names


def test_the_absent_source_is_named_rather_than_silently_skipped():
    """The strategy's own series lives in the app process and cannot be read
    from here. Saying so beats letting an operator assume it was checked."""
    doc = SCRIPT.read_text()
    assert "deliberately not checked here" in doc
    assert "separate process" in doc
