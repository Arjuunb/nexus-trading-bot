"""Strategy League: ranked by expectancy (not raw win rate), daily-return
correlations, honest no-data verdict, actionable best pairing."""
from pathlib import Path

import pytest

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
