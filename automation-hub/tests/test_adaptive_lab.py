"""The Adaptive MTF Trend Pullback lab: a private paper bot on the instance path.

The lab must trade exactly like a Trading Instance running the same strategy
-- same strategy object, same engine, same fills -- while keeping every row
of its state in its own database, so it can neither leak into the Trading
Instances list nor take one of its slots.
"""
from data.ledger import SqliteLedger
from services.adaptive_lab import (
    DEFAULT_SYMBOL, STRATEGY_KEY, AdaptiveLab, AdaptiveLabError,
)
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.strategy_factory import make_builtin_strategy
from strategies.adaptive_trend_pullback.strategy import AdaptiveTrendPullbackStrategy
from tests.test_instance_runtime_reliability import _Stream, _reset_streams  # noqa: F401

RULES = {"symbol": "X", "tick_size": 0.0001, "step_size": 0.1,
         "min_qty": 0.1, "min_notional": 5.0}


def _lab(tmp_path, name="adaptive-lab.db"):
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True
    ledger = SqliteLedger(str(tmp_path / name))
    lab = AdaptiveLab(ledger, strategy_factory=make_builtin_strategy,
                      strategy_version="1.0.0", live_poll_s=1.0, market_hub=hub,
                      symbol_rules_provider=lambda _symbol: RULES)
    return lab


def _gate_open(lab) -> bool:
    return lab.manager._runtime[lab.current().id][3].trading_allowed()


def test_first_use_runs_the_real_strategy_on_xrp_armed(tmp_path):
    lab = _lab(tmp_path)
    bot = lab.ensure_started()

    assert (bot.symbol, bot.strategy_key, bot.timeframe) == (DEFAULT_SYMBOL, STRATEGY_KEY, "5m")
    assert lab.manager.worker_alive(bot.id)
    engine = lab.manager._runtime[bot.id][0]
    # The unmodified strategy class, built by the same factory instances use.
    assert isinstance(engine.strategy_factory(bot.symbol), AdaptiveTrendPullbackStrategy)
    status = lab.status()
    assert status["mode"] == "automatic" and _gate_open(lab)
    assert status["paper_only"] is True and status["real_execution_allowed"] is False
    assert status["symbol"] == "XRPUSDT" and status["risk_pct"] == 0.5
    # A second call never builds a second bot.
    assert lab.ensure_started().id == bot.id and len(lab.status()["bots"]) == 1
    lab.shutdown()


def test_the_lab_keeps_its_state_out_of_the_trading_instances_database(tmp_path):
    main = SqliteLedger(str(tmp_path / "main.db"))
    lab = _lab(tmp_path)
    lab.ensure_started()

    from services.trading_instances import InstanceStore
    assert InstanceStore(main).list() == []           # nothing in the main instances table
    assert [i.strategy_key for i in InstanceStore(lab.ledger).list()] == [STRATEGY_KEY]
    lab.shutdown()


def test_signals_only_keeps_the_worker_but_closes_the_entry_gate(tmp_path):
    lab = _lab(tmp_path)
    bot = lab.ensure_started()

    lab.configure(mode="signals_only")
    assert lab.status()["mode"] == "signals_only"
    assert lab.manager.worker_alive(bot.id)            # still judging every candle
    assert _gate_open(lab) is False                     # but cannot place an order

    lab.configure(mode="automatic")
    assert lab.status()["mode"] == "automatic" and _gate_open(lab) is True

    lab.configure(mode="off")
    assert lab.status()["mode"] == "off" and not lab.manager.worker_alive(bot.id)
    lab.shutdown()


def test_risk_is_saved_and_bounded(tmp_path):
    lab = _lab(tmp_path)
    bot = lab.ensure_started()

    status = lab.configure(risk_pct=0.8)
    assert status["risk_pct"] == 0.8
    assert abs(lab.manager._instances[bot.id].risk_per_trade_pct - 0.008) < 1e-12
    assert lab.status()["mode"] == "automatic"          # a risk edit keeps the bot armed
    for bad in (0, -1, 1.5):
        try:
            lab.configure(risk_pct=bad)
        except AdaptiveLabError:
            pass
        else:
            raise AssertionError(f"risk {bad}% was accepted")
    lab.shutdown()


def test_switching_symbol_swaps_bots_and_keeps_each_history(tmp_path):
    lab = _lab(tmp_path)
    xrp = lab.ensure_started()

    status = lab.configure(symbol="ADAUSDT")
    ada = lab.current()
    assert status["symbol"] == "ADAUSDT" and ada.symbol == "ADAUSDT" and ada.id != xrp.id
    assert not lab.manager.worker_alive(xrp.id)          # one bot trades at a time
    assert lab.manager.worker_alive(ada.id) and status["mode"] == "automatic"

    lab.configure(symbol="XRPUSDT")
    assert lab.current().id == xrp.id                    # same bot, same paper account
    assert sorted(row["symbol"] for row in lab.status()["bots"]) == ["ADAUSDT", "XRPUSDT"]
    lab.shutdown()


def test_switching_symbol_is_refused_while_a_trade_is_open(tmp_path):
    lab = _lab(tmp_path)
    xrp = lab.ensure_started()
    lab.ledger.open_position(symbol="XRPUSDT", side="long", size=10, entry=0.5, stop=0.49,
                             instance_id=xrp.id, simulation_session_id=xrp.simulation_session_id)

    try:
        lab.configure(symbol="ADAUSDT")
    except AdaptiveLabError as exc:
        assert "open paper position" in str(exc)
    else:
        raise AssertionError("switched away from an open trade")
    assert lab.current().id == xrp.id and lab.manager.worker_alive(xrp.id)
    assert lab.paper()["positions"][0]["symbol"] == "XRPUSDT"
    lab.shutdown()


def test_a_restart_restores_the_saved_symbol_and_mode(tmp_path):
    lab = _lab(tmp_path)
    lab.ensure_started()
    lab.configure(symbol="ADAUSDT", mode="signals_only")
    lab.shutdown()

    again = _lab(tmp_path)
    again.restore()
    status = again.status()
    assert status["symbol"] == "ADAUSDT" and status["mode"] == "signals_only"
    assert again.manager.worker_alive(again.current().id)
    assert _gate_open(again) is False
    again.shutdown()


def test_an_unknown_mode_is_refused(tmp_path):
    lab = _lab(tmp_path)
    lab.ensure_started()
    try:
        lab.configure(mode="manual_approval")
    except AdaptiveLabError as exc:
        assert "automatic" in str(exc)
    else:
        raise AssertionError("accepted a mode this execution path cannot honour")
    lab.shutdown()


# ------------------------------------------------------------ journal
def test_the_engine_writes_its_per_candle_reports_into_the_labs_journal(tmp_path):
    lab = _lab(tmp_path)
    bot = lab.ensure_started()
    engine = lab.manager._runtime[bot.id][0]
    assert engine.reports is lab.journal          # the per-candle report hook is the journal
    # The engine publishes its strategy during warm-up, before it records any
    # candle -- the order the journal relies on in production.
    import time
    deadline = time.monotonic() + 10
    while bot.symbol not in engine._live_strategies and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bot.symbol in engine._live_strategies

    # What the engine hands over after each closed candle; the journal adds
    # the strategy's own decision for that same candle.
    lab.journal.record({"instance_id": bot.id, "symbol": bot.symbol, "timeframe": "5m",
                        "ts": "2026-09-23T21:40:00+00:00", "decision": "WAIT", "price": 0.52,
                        "decision_identity": "c-2140", "reasons": ["No qualifying setup"]})
    strategy = engine._live_strategies[bot.symbol]
    entry = lab.journal_entries()["entries"][0]
    assert entry["candle_time"] == "2026-09-23T21:40:00+00:00"
    assert entry["engine_decision"] == "WAIT"
    assert entry["strategy_state"] == strategy.decision_report()["state"]
    assert entry["reason"] == strategy.decision_report()["reason"]
    assert entry["engine_reasons"] == ["No qualifying setup"]
    lab.shutdown()


def test_the_journal_is_append_only_and_one_row_per_candle(tmp_path):
    import sqlite3

    import pytest

    lab = _lab(tmp_path)
    bot = lab.ensure_started()
    report = {"instance_id": bot.id, "symbol": bot.symbol, "ts": "2026-09-23T21:45:00+00:00",
              "decision": "WAIT", "decision_identity": "c-2145"}
    lab.journal.record(report)
    lab.journal.record(report)                      # a replayed candle is not a second row
    assert len(lab.journal_entries()["entries"]) == 1
    with pytest.raises(sqlite3.DatabaseError):
        lab.journal._c.execute("UPDATE adaptive_journal SET reason='rewritten'")
    with pytest.raises(sqlite3.DatabaseError):
        lab.journal._c.execute("DELETE FROM adaptive_journal")
    lab.shutdown()


# --------------------------------------------------------- live chart
def test_the_live_chart_is_the_bots_own_feed_with_its_trade_drawn(tmp_path):
    lab = _lab(tmp_path)
    bot = lab.ensure_started()
    lab.ledger.open_position(symbol="XRPUSDT", side="long", size=10, entry=100.2, stop=99.5,
                             target=101.9, instance_id=bot.id,
                             simulation_session_id=bot.simulation_session_id)

    chart = lab.live_chart(300)
    feed = lab.manager._runtime[bot.id][0].ws_feed.snapshot()
    assert len(chart["candles"]) == 300
    assert chart["candles"][-1]["timestamp"] == feed["closed_bars"][-1].timestamp.isoformat()
    assert chart["data_provenance"]["last_closed_candle"] == chart["candles"][-1]["timestamp"]
    live = chart["live_display"]
    assert live["reliable"] is True and live["execution_uses_closed_bars_only"] is True
    assert live["quote_source"] == "BINANCE_USDM_PUBLIC_WEBSOCKET"
    assert chart["trade_plan"] == {"entry": 100.2, "stop": 99.5, "target_1": 101.9, "target_2": 101.9}
    assert chart["real_execution_allowed"] is False
    lab.shutdown()


def test_a_bot_that_is_off_has_no_live_chart_to_invent(tmp_path):
    lab = _lab(tmp_path)
    lab.ensure_started()
    lab.configure(mode="off")
    try:
        lab.live_chart()
    except AdaptiveLabError as exc:
        assert "off" in str(exc)
    else:
        raise AssertionError("served a live chart for a bot with no feed")
    lab.shutdown()
