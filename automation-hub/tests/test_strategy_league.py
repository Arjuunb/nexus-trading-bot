"""Strategy League: ranked by expectancy (not raw win rate), daily-return
correlations, honest no-data verdict, actionable best pairing."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.types import Bar


@pytest.fixture(autouse=True)
def _offline_live_source(monkeypatch):
    """No test reaches an exchange. The venue is stubbed unreachable unless a
    test says otherwise, and the shared series cache never leaks between them."""
    import services.native_smc_live_visual as live
    from services.live_candle_source import reset_cache

    def _down(*_a, **_k):
        raise live.NativeSMCLiveDataUnavailable("offline in tests")

    monkeypatch.setattr(live, "fetch_venue_ohlcv", _down)
    reset_cache()
    yield
    reset_cache()


def _venue_candles(n, *, timeframe_minutes=60, fresh=True):
    step = timedelta(minutes=timeframe_minutes)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    newest = now - step if fresh else now - step - timedelta(days=9)
    price = 100.0
    rows = []
    for i in range(n):
        # A drifting series so strategies actually take trades on it.
        price += (1.0 if (i // 7) % 2 == 0 else -1.0)
        rows.append(Bar(newest - step * (n - 1 - i), price, price + 1.5,
                        price - 1.5, price + 0.5, 100.0))
    return rows

from services.strategy_league import _daily_r, league, pearson


# ─────────────────────────── pure math ───────────────────────────
def test_pearson_basics():
    assert pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == 1.0
    assert pearson([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == -1.0
    assert pearson([1, 2], [1, 2]) is None            # too short
    assert pearson([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None  # zero variance


def test_daily_r_groups_by_exit_day():
    trades = [{"exit_time": "2026-07-01T05:00:00", "r": 1.0},
              {"exit_time": "2026-07-01T18:00:00", "r": -0.5},
              {"exit_time": "2026-07-02T03:00:00", "r": 2.0}]
    d = _daily_r(trades)
    assert d == {"2026-07-01": 0.5, "2026-07-02": 2.0}


# ─────────────────────────── the league ───────────────────────────
def test_league_honest_without_real_data(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config.settings, "market_db", str(tmp_path / "empty.db"))
    monkeypatch.setenv("HUB_REQUIRE_REAL_DATA", "1")
    rep = league(symbols=("BTCUSDT",), timeframe="1h")
    assert rep["available"] is False and "Load real Binance data" in rep["detail"]


def test_league_ranks_and_correlates():
    # synthetic allowed in tests (require_real=False); production stays honest
    rep = league(symbols=("ZZZUSDT",), timeframe="1h", bars=2000,
                 strategies=["Decision Brain", "EMA 8/30", "EMA 20/50"],
                 require_real=False)
    assert rep["available"] is True
    table = rep["table"]
    assert len(table) == 3
    for row in table:
        assert row["verdict"] in ("earning", "losing", "breakeven", "insufficient-sample")
        if row["verdict"] != "insufficient-sample":
            assert row["trades"] >= 10 and row["expectancy_r"] is not None
    # ranked: judged strategies come before insufficient samples, best expectancy first
    judged = [r for r in table if r["verdict"] != "insufficient-sample"]
    exps = [r["expectancy_r"] for r in judged]
    assert exps == sorted(exps, reverse=True)
    # correlations only among judged pairs, with a named relation
    for c in rep["correlations"]:
        assert -1.0 <= c["correlation"] <= 1.0
        assert c["relation"] in ("diversifying", "related", "redundant")
    assert any("win rate alone misleads" in g.lower() or "win rate alone" in g.lower()
               for g in rep["guidance"])


def test_league_best_combo_requires_two_earners():
    rep = league(symbols=("ZZZUSDT",), timeframe="1h", bars=2000,
                 strategies=["Decision Brain", "EMA 8/30", "EMA 20/50"],
                 require_real=False)
    combo = rep["best_combo"]
    if combo is not None:                              # depends on the series
        earners = {r["strategy"] for r in rep["table"] if r["verdict"] == "earning"}
        assert combo["a"] in earners and combo["b"] in earners


# ─────────────────────────── endpoint ───────────────────────────
def test_league_endpoint():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI(); app.include_router(webhook_api.router)
    client = TestClient(app)
    body = client.get("/strategy/league", params={"symbols": "BTCUSDT", "bars": 800}).json()
    assert "available" in body

# ────────────────────────── profit factor ──────────────────────────
def test_profit_factor_is_computed_from_the_trades_not_a_key_nobody_writes():
    """It used to read gross_profit_r / gross_loss_r off the simulator result.

    Neither key exists anywhere in the codebase, so the column rendered a dash
    for every strategy in every league that has ever run -- which reads as "not
    applicable" and is really "never computed". This pins it to the per-trade R
    the league already aggregates for the correlation stream.
    """
    import ast

    from services import strategy_league as module

    # An AST walk, not a substring scan: the comment explaining this fix names
    # both dead keys, and a text search would match its own explanation. What
    # must not exist is a *read* of them.
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    read = {node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)}
    assert "gross_profit_r" not in read, "the simulator has never written this key"
    assert "gross_loss_r" not in read, "the simulator has never written this key"

    rep = league(symbols=("ZZZUSDT",), timeframe="1h", bars=2000,
                 strategies=["Decision Brain", "EMA 8/30", "EMA 20/50"],
                 require_real=False)
    judged = [r for r in rep["table"] if r["verdict"] != "insufficient-sample"]
    assert judged, "the fixture must judge at least one strategy"
    assert any(r["profit_factor"] is not None for r in judged), \
        "a judged strategy with losses must report a profit factor"


def test_profit_factor_agrees_with_the_sign_of_the_result():
    """PF above 1 and a losing verdict cannot both be true."""
    rep = league(symbols=("ZZZUSDT",), timeframe="1h", bars=2000,
                 strategies=["Decision Brain", "EMA 8/30", "EMA 20/50"],
                 require_real=False)
    for row in rep["table"]:
        if row["profit_factor"] is None or row["verdict"] == "insufficient-sample":
            continue
        if row["verdict"] == "earning":
            assert row["profit_factor"] > 1
        elif row["verdict"] == "losing":
            assert row["profit_factor"] < 1


def test_a_strategy_with_no_losing_trade_reports_no_profit_factor():
    """Dividing by zero gross loss is not a profit factor of infinity, and
    printing one would be the most flattering number on the page."""
    from services.strategy_league import _r

    assert _r({"r": None}) == 0.0
    assert _r({}) == 0.0
    assert _r({"r": "bad"}) == 0.0
    assert _r({"r": "1.5"}) == 1.5


# ─────────────────────── live candles, not the cache ───────────────────────
def test_the_league_measures_live_candles_before_the_cache(monkeypatch):
    """The point of the change. The cache is only as current as the last manual
    /data/sync; a league run on it ranks strategies on an older market than the
    one being traded, while labelling itself real data."""
    import data.market_data as market_data
    import services.native_smc_live_visual as live

    monkeypatch.setattr(live, "fetch_venue_ohlcv",
                        lambda *a, **k: _venue_candles(900))

    def _never(*_a, **_k):
        raise AssertionError("the cache must not be read when the venue answers")

    monkeypatch.setattr(market_data, "get_bars", _never)
    rep = league(symbols=("BTCUSDT",), timeframe="1h", bars=800,
                 strategies=["EMA 8/30"])
    assert rep["available"] is True
    assert rep["live"] is True
    assert rep["data_source"] == "venue binance_usdm (live)"
    assert rep["provenance"]["BTCUSDT"]["freshness"]["status"] == "FRESH"


def test_the_cache_is_the_fallback_and_is_named_as_one(monkeypatch):
    """Unreachable venue is not a reason to show nothing -- but it is a reason
    to say which series the ranking came from."""
    import data.market_data as market_data

    monkeypatch.setattr(market_data, "get_bars",
                        lambda *a, **k: (_venue_candles(800), "local store (real)"))
    rep = league(symbols=("BTCUSDT",), timeframe="1h", bars=800,
                 strategies=["EMA 8/30"])
    assert rep["available"] is True
    assert rep["live"] is False
    assert rep["data_source"] == "local store (real)"
    attempts = rep["provenance"]["BTCUSDT"]["attempts"]
    assert attempts[0]["source"] == "venue binance_usdm"
    assert "offline in tests" in attempts[0]["error"]


def test_a_stale_ranking_says_so_in_its_own_guidance(monkeypatch):
    """A table ranking strategies on a nine-day-old market must not read the
    same as one ranking them on this morning's."""
    import data.market_data as market_data

    monkeypatch.setattr(
        market_data, "get_bars",
        lambda *a, **k: (_venue_candles(800, fresh=False), "local store (real)"))
    rep = league(symbols=("BTCUSDT",), timeframe="1h", bars=800,
                 strategies=["EMA 8/30"])
    assert rep["provenance"]["BTCUSDT"]["freshness"]["status"] == "STALE"
    assert any("describes an older market" in g for g in rep["guidance"])


def test_a_dropped_symbol_is_reported_rather_than_vanishing(monkeypatch):
    """A league labelled BTCUSDT + ETHUSDT that silently measured one of them
    is the bug this reports: the badge said both, the numbers were one."""
    import data.market_data as market_data
    import services.native_smc_live_visual as live

    def _by_symbol(symbol, *_a, **_k):
        if symbol.upper() == "BTCUSDT":
            return _venue_candles(900)
        return _venue_candles(50)          # far short of MIN_BARS

    monkeypatch.setattr(live, "fetch_venue_ohlcv", _by_symbol)
    monkeypatch.setattr(market_data, "get_bars", lambda *a, **k: ([], "empty"))
    rep = league(symbols=("BTCUSDT", "ETHUSDT"), timeframe="1h", bars=800,
                 strategies=["EMA 8/30"])
    assert rep["symbols"] == ["BTCUSDT"]
    assert [row["symbol"] for row in rep["dropped"]] == ["ETHUSDT"]
    assert rep["dropped"][0]["candles"] == 50
    assert any("Not measured: ETHUSDT" in g for g in rep["guidance"])


def test_the_measured_window_is_reported(monkeypatch):
    """Which candles produced these verdicts, without having to trust a badge."""
    import services.native_smc_live_visual as live

    rows = _venue_candles(900)
    monkeypatch.setattr(live, "fetch_venue_ohlcv", lambda *a, **k: rows)
    rep = league(symbols=("BTCUSDT",), timeframe="1h", bars=800,
                 strategies=["EMA 8/30"])
    assert rep["window"]["bars_requested"] == 800
    assert rep["window"]["last"] == rows[-1].timestamp.isoformat()
    assert rep["window"]["first"] < rep["window"]["last"]


def test_nothing_reachable_refuses_rather_than_inventing_a_ranking(monkeypatch):
    import data.market_data as market_data

    monkeypatch.setattr(market_data, "get_bars", lambda *a, **k: ([], "empty cache"))
    rep = league(symbols=("BTCUSDT",), timeframe="1h", bars=800,
                 strategies=["EMA 8/30"])
    assert rep["available"] is False
    assert rep["dropped"][0]["symbol"] == "BTCUSDT"
    assert "venue was unreachable" in rep["detail"]
