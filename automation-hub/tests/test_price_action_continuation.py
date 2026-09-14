import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

from bot.types import Bar
from services.native_price_action import NativePriceActionEngine, PriceActionConfig, PriceZone
from services.price_action_lab import PaperExecutionConfig, PriceActionLabRuntime, PriceActionPaperAccount
from services.price_action_research import PriceActionExperimentRunner, PriceActionExperimentStore, controlled_pa_smc_report
from services.price_action_stream import PriceActionPublicStream


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def bar(index, open_=100, high=102, low=98, close=101, volume=10_000):
    return Bar(NOW + timedelta(minutes=5 * index), open_, high, low, close, volume)


RULES = {"tick_size": .1, "quantity_step": .001, "min_quantity": .001,
         "max_quantity": 1000, "min_notional": 5}


def visual(proposal=True, count=10):
    setup = {"id": "setup-1", "zone_id": "zone-1", "strategy_id": "PA1_SR_REJECTION",
             "direction": "bullish", "phase": "ORDER_PENDING"}
    trade = {"id": "proposal-1", "setup_id": "setup-1", "strategy_id": "PA1_SR_REJECTION",
             "direction": "bullish", "entry": 105.0, "stop": 99.0, "target": 120.0,
             "risk_distance": 6.0, "rr_ratio": 2.5, "valid_until_index": count + 2}
    return {"research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "strategy_version": "1.1.0",
            "symbol": "BTCUSDT", "timeframe": "5m",
            "candles": [{}] * count, "setups": [setup], "proposals": [trade] if proposal else [],
            "metrics": {"closed": 0}}


def connect_stream_channels(stream):
    stream._set_channel_state("market", "CONNECTED")
    stream._set_channel_state("public", "CONNECTED")


def test_automatic_paper_lifecycle_sizes_rounds_protects_and_stops_first(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "auto.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="automatic", risk_pct=.5))
    first = account.synchronize_strategy(visual(), contract_rules=RULES,
                                         candle=bar(1), feed_reliable=True)
    assert first["created"][0]["accepted"] is True
    order = account.state()["orders"][0]
    assert order["quantity"] == 8.333
    assert account.state()["order_metadata"][0]["config"]["execution_mode"] == "PAPER"

    account.synchronize_strategy(visual(count=11), contract_rules=RULES,
                                 candle=bar(2, 104, 106, 103, 105), feed_reliable=True)
    position = account.state()["positions"][0]
    assert position["stop_loss"] == 99.0
    assert position["take_profit"] > 120.0
    assert position["planned_rr"] == 2.5
    assert position["effective_rr"] >= 2.5
    assert position["protection_status"] == "PROTECTED"

    # Both stop and target occur inside this candle; protective handling is adverse-first.
    account.synchronize_strategy(visual(count=12), contract_rules=RULES,
                                 candle=bar(3, 105, 125, 98, 110), feed_reliable=True)
    state = account.state()
    assert state["positions"] == []
    assert state["order_metadata"][0]["status"] == "COMPLETED"
    assert state["account"]["realized_pnl"] < 0


def test_historical_proposal_is_not_replayed_as_a_current_order(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "historical-proposal.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="automatic"))
    historical = visual()
    historical["proposals"][0]["signal_at"] = bar(0).timestamp.isoformat()

    first = account.synchronize_strategy(
        historical, contract_rules=RULES, candle=bar(10), feed_reliable=True)

    assert first["created"] == []
    assert account.state()["orders"] == []
    evaluation = account.state()["evaluations"][0]
    assert evaluation["state"] == "WATCHING"
    assert evaluation["payload"]["proposal_ids"] == []

    current = visual()
    current["proposals"][0]["signal_at"] = bar(11).timestamp.isoformat()
    second = account.synchronize_strategy(
        current, contract_rules=RULES, candle=bar(11), feed_reliable=True)

    assert len(second["created"]) == 1
    assert second["created"][0]["accepted"] is True


def test_restart_repairs_missing_strategy_protection_without_touching_manual_positions(tmp_path):
    path = tmp_path / "restart-protection.db"
    account = PriceActionPaperAccount(path)
    account.configure(execution_config=PaperExecutionConfig(
        operating_mode="automatic", risk_pct=.5))
    account.synchronize_strategy(visual(), contract_rules=RULES,
                                 candle=bar(1), feed_reliable=True)
    account.synchronize_strategy(visual(count=11), contract_rules=RULES,
                                 candle=bar(2, 104, 106, 103, 105), feed_reliable=True)
    protected = account.state()["positions"][0]
    assert protected["stop_loss"] == 99.0
    assert protected["take_profit"] > 120.0
    protected_target = protected["take_profit"]

    # Simulate a legacy/restored row whose strategy protection was lost.
    account.broker._c.execute(
        "UPDATE v2_positions SET stop_loss=NULL,take_profit=NULL WHERE symbol='BTCUSDT'")
    account.broker._c.commit()
    reopened = PriceActionPaperAccount(path)
    repaired = reopened.state()["positions"][0]
    assert repaired["stop_loss"] == 99.0
    assert repaired["take_profit"] == protected_target
    assert repaired["effective_rr"] >= 2.5
    assert any(row["kind"] == "paper_position_protection_repaired"
               for row in reopened.state()["activity"])

    manual_path = tmp_path / "manual-position.db"
    manual = PriceActionPaperAccount(manual_path)
    manual.broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1)
    manual.broker.process_candle("BTCUSDT", bar(1))
    manual_reopened = PriceActionPaperAccount(manual_path)
    manual_position = manual_reopened.state()["positions"][0]
    assert manual_position["stop_loss"] is None
    assert manual_position["take_profit"] is None


def test_price_action_blocks_same_symbol_stacking_that_would_overwrite_protection(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "stacking.db")
    account.configure(execution_config=PaperExecutionConfig(
        operating_mode="automatic", risk_pct=.5))
    account.synchronize_strategy(visual(), contract_rules=RULES,
                                 candle=bar(1), feed_reliable=True)
    account.synchronize_strategy(visual(count=11), contract_rules=RULES,
                                 candle=bar(2, 104, 106, 103, 105), feed_reliable=True)

    second = visual(count=12)
    second["setups"][0] = {**second["setups"][0], "id": "setup-2", "zone_id": "zone-2"}
    second["proposals"][0] = {**second["proposals"][0], "id": "proposal-2", "setup_id": "setup-2"}
    result = account.synchronize_strategy(second, contract_rules=RULES,
                                          candle=bar(3, 105, 106, 104, 105), feed_reliable=True)
    assert result["created"] == []
    assert "stacking is blocked" in result["rejected"][0]["reason"]
    assert len(account.state()["positions"]) == 1


def test_restart_consolidates_legacy_multiple_owners_and_cancels_pending_entries(tmp_path):
    path = tmp_path / "legacy-aggregate.db"
    account = PriceActionPaperAccount(path)
    account.configure(execution_config=PaperExecutionConfig(
        operating_mode="automatic", risk_pct=.5))
    account.synchronize_strategy(visual(), contract_rules=RULES,
                                 candle=bar(1), feed_reliable=True)
    account.synchronize_strategy(visual(count=11), contract_rules=RULES,
                                 candle=bar(2, 104, 106, 103, 105), feed_reliable=True)

    second = account.broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1)
    account.broker.process_candle("BTCUSDT", bar(3, 105, 106, 104, 105))
    config = dict(account.state()["order_metadata"][0]["config"])
    config.update({"entry": 105, "stop": 98, "target": 122.5,
                   "target_r": 2.5, "planned_rr": 2.5})
    now = NOW.isoformat()
    account._db.execute(
        "INSERT INTO pa_order_meta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (second["id"], account.session()["id"], "proposal-legacy-2", "setup-legacy-2",
         "zone-legacy-2", "bullish", "PA1_SR_REJECTION", json.dumps(config, sort_keys=True),
         "ENTERED", "legacy filled owner", now, now, 99),
    )
    pending = account.broker.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                                    quantity=.1, limit_price=90)
    account.broker._c.execute(
        "UPDATE v2_positions SET stop_loss=NULL,take_profit=NULL WHERE symbol='BTCUSDT'")
    account.broker._c.commit()

    reopened = PriceActionPaperAccount(path)
    repaired = reopened.state()["positions"][0]
    assert repaired["stop_loss"] == 99
    assert repaired["take_profit"] is not None
    assert repaired["planned_rr"] == 2.5
    assert repaired["effective_rr"] >= 2.5
    assert repaired["protection_status"] == "PROTECTED"
    assert reopened.broker.order(pending["id"])["status"] == "cancelled"
    repair = next(row for row in reopened.state()["activity"]
                  if row["kind"] == "paper_position_protection_repaired")
    assert repair["payload"]["ownership_resolution"] == "legacy_aggregate_conservative"


def test_signals_manual_approval_duplicate_and_unreliable_feed_are_explicit(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "modes.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="signals_only"))
    signal = account.synchronize_strategy(visual(), contract_rules=RULES,
                                          candle=bar(1), feed_reliable=True)
    assert signal["created"] == []
    assert account.state()["candidates"][0]["status"] == "SIGNAL_ONLY"

    account.reset()
    account.configure(execution_config=PaperExecutionConfig(operating_mode="manual_approval"))
    account.synchronize_strategy(visual(), contract_rules=RULES, candle=bar(1), feed_reliable=True)
    assert account.state()["candidates"][0]["status"] == "PENDING_APPROVAL"
    assert account.approve_candidate("proposal-1")["accepted"] is True

    account.reset()
    account.configure(execution_config=PaperExecutionConfig(operating_mode="automatic"))
    account.synchronize_strategy(visual(), contract_rules=RULES, candle=bar(1), feed_reliable=False)
    rejected = account.state()["candidates"][0]
    assert rejected["status"] == "DATA_PAUSED"
    assert "unreliable" in rejected["payload"]["reason"]


def test_new_price_action_session_is_armed_and_places_a_paper_order(tmp_path):
    """A new session places on a valid setup, and cannot reach a venue.

    The default was 'signals_only', which recorded a SIGNAL_ONLY candidate for
    every valid setup and created no order -- a lab could run for weeks looking
    healthy while never placing anything. It is now 'automatic'. What must stay
    impossible is real execution, not simulated execution.
    """
    account = PriceActionPaperAccount(tmp_path / "armed-default.db")
    result = account.synchronize_strategy(
        visual(), contract_rules=RULES, candle=bar(1), feed_reliable=True,
    )
    assert account.session()["operating_mode"] == "automatic"

    assert len(result["created"]) == 1
    created = result["created"][0]
    assert created["accepted"] is True
    order = created["order"]
    assert order["symbol"] == "BTCUSDT"
    assert order["strategy"] == "PA1_SR_REJECTION"
    assert len(account.state()["orders"]) == 1

    # Simulated, and provably only simulated.
    assert order["execution_class"] == "REAL_PAPER"
    assert created["configuration"]["execution_mode"] == "PAPER"
    assert created["configuration"]["live_execution_allowed"] is False
    assert not hasattr(PriceActionPaperAccount, "enable_live")


def test_signals_only_still_records_a_candidate_without_ordering(tmp_path):
    """The mode still exists and still suppresses orders when chosen."""
    from services.price_action_lab import PaperExecutionConfig
    account = PriceActionPaperAccount(tmp_path / "signals-only.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="signals_only"))
    result = account.synchronize_strategy(
        visual(), contract_rules=RULES, candle=bar(1), feed_reliable=True,
    )
    assert result["created"] == []
    assert account.state()["candidates"][0]["status"] == "SIGNAL_ONLY"
    assert account.state()["orders"] == []


def test_ended_session_can_resume_wallet_orders_and_positions(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "sessions.db")
    old = account.session()["id"]
    account.broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1)
    account.broker.process_candle("BTCUSDT", bar(1))
    account.end()
    account.start(symbol="ETHUSDT")
    assert account.state()["positions"] == []
    resumed = account.resume(old)
    assert resumed["session"]["id"] == old
    assert resumed["positions"][0]["symbol"] == "BTCUSDT"
    exported = account.export_session(old)
    assert exported["session"]["state"]["positions"]


def test_restart_does_not_create_new_pa_session_over_ended_session_exposure(tmp_path):
    path = tmp_path / "pa-orphaned-exposure.db"
    account = PriceActionPaperAccount(path)
    old = account.session()["id"]
    account.broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1)
    account.broker.process_candle("BTCUSDT", bar(1))
    account.end()

    reopened = PriceActionPaperAccount(path)

    assert reopened.session() == {}
    assert len(reopened.sessions()) == 1
    assert reopened.state()["positions"][0]["symbol"] == "BTCUSDT"
    resumed = reopened.resume(old)
    assert resumed["session"]["id"] == old
    assert resumed["positions"][0]["symbol"] == "BTCUSDT"


def test_global_factory_reset_erases_price_action_session_history(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "factory.db")
    old = account.session()["id"]
    account.record_external_event({"kind": "connection", "state": "CONNECTED"})
    account.reset()
    assert len(account.sessions()) == 2
    fresh = account.factory_reset()
    assert len(account.sessions()) == 1
    assert fresh["session"]["id"] != old
    assert fresh["activity"] == []


def test_public_stream_dedupes_closed_candles_and_marks_gaps_unreliable():
    history = [bar(0), bar(1)]
    clock = lambda: bar(3).timestamp + timedelta(minutes=5)
    stream = PriceActionPublicStream(lambda *_args, **_kwargs: history, stale_after_seconds=60,
                                     clock=clock)
    stream.bootstrap("BTCUSDT", "5m")
    stream.reconciliation_complete = True
    connect_stream_channels(stream)
    stream.ingest_event({"data": {"e": "bookTicker", "s": "BTCUSDT", "b": "100", "a": "100.1"}})
    stream.ingest_event({"data": {"e": "markPriceUpdate", "p": "100.05", "r": ".0001", "T": 1767225600000}})
    closed = {"e": "kline", "k": {"t": int(bar(2).timestamp.timestamp() * 1000),
              "o": "100", "h": "102", "l": "98", "c": "101", "v": "1000", "x": True}}
    assert stream.ingest_event({"data": closed})["accepted"] is True
    assert stream.ingest_event({"data": closed})["duplicate"] is True
    gap = {"e": "kline", "k": {**closed["k"], "t": int(bar(4).timestamp.timestamp() * 1000)}}
    stream.ingest_event({"data": gap})
    status = stream.status()
    assert status["state"] == "RECONCILING"
    assert status["new_entries_paused"] is True
    assert status["duplicate_events"] >= 1


def test_journal_persistence_block_does_not_disconnect_stream_worker():
    class Blocked(RuntimeError):
        code = "PERSISTENCE_BLOCKED"

    events = []
    history = [bar(0), bar(1)]
    stream = PriceActionPublicStream(
        lambda *_args, **_kwargs: history,
        event_sink=events.append,
        bar_sink=lambda _bar: (_ for _ in ()).throw(Blocked("journal locked")),
    )
    stream.bootstrap("BTCUSDT", "5m")
    stream.reconciliation_complete = True
    connect_stream_channels(stream)
    closed = {"e": "kline", "k": {
        "t": int(bar(2).timestamp.timestamp() * 1000),
        "o": "100", "h": "102", "l": "98", "c": "101", "v": "1000", "x": True,
    }}

    result = stream.ingest_event({"data": closed})

    assert result["accepted"] is True
    assert result["sink_persisted"] is False
    assert result["persistence_state"] == "PERSISTENCE_BLOCKED"
    assert stream.state == "CONNECTED"
    assert stream.status()["persistence_blocked_events"] == 1
    assert any(event.get("state") == "PERSISTENCE_BLOCKED" for event in events)


def test_public_stream_rest_reconciles_a_missing_closed_candle_once():
    history = [bar(0), bar(1), bar(2)]
    clock = lambda: bar(3).timestamp + timedelta(minutes=5)
    stream = PriceActionPublicStream(lambda *_args, **_kwargs: history, stale_after_seconds=60,
                                     clock=clock)
    stream.bootstrap("BTCUSDT", "5m")
    stream._bars.remove(history[1])
    stream.missing_candles += 1
    connect_stream_channels(stream)
    stream._set_state("DELAYED", "controlled test gap")

    assert asyncio.run(stream.reconcile()) == 1
    assert [row.timestamp for row in stream.snapshot()["closed_bars"]] == [
        row.timestamp for row in history]
    forming = {"e": "kline", "k": {"t": int(bar(3).timestamp.timestamp() * 1000),
               "o": "101", "h": "103", "l": "100", "c": "102", "v": "1000", "x": False}}
    stream.ingest_event({"data": forming})
    stream.ingest_event({"data": {"e": "bookTicker", "s": "BTCUSDT", "b": "101.9", "a": "102.1"}})
    stream.ingest_event({"data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "102", "r": ".0001", "T": 1767225600000}})
    assert stream.status()["state"] == "SYNCHRONIZED"
    assert stream.status()["reliable"] is True
    assert asyncio.run(stream.reconcile()) == 0


def test_public_stream_routes_required_events_and_aggregates_channel_health():
    stream = PriceActionPublicStream(lambda *_args, **_kwargs: [bar(0), bar(1)])
    stream.bootstrap("BTCUSDT", "5m")
    stream.reconciliation_complete = True

    assert stream.market_url == (
        "wss://fstream.binance.com/market/stream?streams="
        "btcusdt@kline_5m/btcusdt@markPrice@1s")
    assert stream.public_url == (
        "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker")
    assert stream.url == stream.market_url

    stream._set_channel_state("market", "CONNECTED")
    assert stream.state == "CONNECTING"
    stream._set_channel_state("public", "CONNECTED")
    status = stream.status()
    assert status["transport_state"] == "CONNECTED"
    assert status["transport_channels"] == {"market": "CONNECTED", "public": "CONNECTED"}
    assert status["public_streams"] == {
        "market": ["kline", "markPrice"], "public": ["bookTicker"]}

    stream._set_channel_state("market", "RECONNECTING", "controlled test")
    status = stream.status()
    assert status["transport_state"] == "RECONNECTING"
    assert status["reliable"] is False
    assert status["new_entries_paused"] is True


def test_public_stream_fails_closed_until_all_identity_bound_channels_are_fresh():
    clock_now = [bar(3).timestamp + timedelta(minutes=5)]
    stream = PriceActionPublicStream(lambda *_args, **_kwargs: [bar(0), bar(1), bar(2)],
                                     stale_after_seconds=10, clock=lambda: clock_now[0])
    stream.bootstrap("BTCUSDT", "5m")
    stream.reconciliation_complete = True
    connect_stream_channels(stream)
    assert stream.status()["state"] == "STALE_CANDLES"

    kline = {"e": "kline", "s": "BTCUSDT", "k": {
        "s": "BTCUSDT", "i": "5m", "t": int(bar(3).timestamp.timestamp() * 1000),
        "o": "100", "h": "102", "l": "98", "c": "101", "v": "1000", "x": False,
    }}
    stream.ingest_event({"data": kline})
    assert stream.status()["state"] == "STALE_QUOTE"
    stream.ingest_event({"data": {"e": "bookTicker", "s": "BTCUSDT", "b": "100.9", "a": "101.1"}})
    assert stream.status()["state"] == "STALE_MARK"
    stream.ingest_event({"data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "101", "r": "0", "T": 1767225600000}})
    assert stream.status()["state"] == "SYNCHRONIZED"

    clock_now[0] += timedelta(seconds=11)
    stale = stream.status()
    assert stale["state"] == "STALE_CANDLES"
    assert stale["new_entries_paused"] is True


def test_public_stream_rejects_old_symbol_or_timeframe_events_and_quote_mismatch():
    clock = lambda: bar(3).timestamp + timedelta(minutes=5)
    stream = PriceActionPublicStream(lambda *_args, **_kwargs: [bar(0), bar(1), bar(2)],
                                     stale_after_seconds=60, clock=clock,
                                     quote_mismatch_bps=50)
    stream.bootstrap("BTCUSDT", "5m")
    stream.reconciliation_complete = True
    connect_stream_channels(stream)
    wrong = stream.ingest_event({"data": {"e": "bookTicker", "s": "ETHUSDT", "b": "100", "a": "101"}})
    assert wrong["identity_mismatch"] is True
    wrong_tf = stream.ingest_event({"data": {"e": "kline", "s": "BTCUSDT", "k": {
        "s": "BTCUSDT", "i": "15m", "t": int(bar(3).timestamp.timestamp() * 1000),
        "o": "100", "h": "102", "l": "98", "c": "101", "v": "1000", "x": False,
    }}})
    assert wrong_tf["identity_mismatch"] is True

    stream.ingest_event({"data": {"e": "kline", "s": "BTCUSDT", "k": {
        "s": "BTCUSDT", "i": "5m", "t": int(bar(3).timestamp.timestamp() * 1000),
        "o": "100", "h": "102", "l": "98", "c": "101", "v": "1000", "x": False,
    }}})
    stream.ingest_event({"data": {"e": "bookTicker", "s": "BTCUSDT", "b": "120", "a": "120.1"}})
    stream.ingest_event({"data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "120", "r": "0", "T": 1767225600000}})
    assert stream.status()["state"] == "QUOTE_MISMATCH"
    assert stream.status()["reliable"] is False


def test_pending_order_reconciliation_expires_strategy_only_and_preserves_manual(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "reconcile.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="automatic"))
    account.synchronize_strategy(visual(count=10), contract_rules=RULES,
                                 candle=bar(1), feed_reliable=True)
    manual = account.broker.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                                   quantity=.1, limit_price=90)

    result = account.reconcile_pending_orders(visual(proposal=False, count=14), bar(4))

    assert result["actions"][0]["to"] == "EXPIRED"
    assert account.broker.order(manual["id"])["status"] == "open"
    assert result["after"]["pending_strategy_orders"] == 0
    assert result["after"]["pending_manual_orders"] == 1
    assert result["manual_orders_changed"] == 0
    assert any(row["kind"] == "paper_order_reconciled" for row in account.state()["activity"])


def test_pending_order_expiry_uses_absolute_index_beyond_capped_visual_window(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "absolute-expiry.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="automatic"))
    initial = visual(count=1500)
    initial["absolute_last_bar_index"] = 1499
    initial["proposals"][0]["valid_until_index"] = 1502
    account.synchronize_strategy(initial, contract_rules=RULES,
                                 candle=bar(1), feed_reliable=True)

    advanced = visual(proposal=False, count=1500)
    advanced["absolute_last_bar_index"] = 1503
    result = account.reconcile_pending_orders(advanced, bar(4))

    assert result["actions"][0]["to"] == "EXPIRED"
    assert result["after"]["pending_strategy_orders"] == 0
    assert account.state()["orders"][0]["status"] == "cancelled"


def test_session_identity_change_is_blocked_while_paper_exposure_exists(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "identity.db")
    account.broker.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                          quantity=.1, limit_price=90)
    try:
        account.configure(symbol="ETHUSDT")
        assert False, "identity change must fail while an order is pending"
    except ValueError as exc:
        assert "pending orders" in str(exc)
    assert account.session()["symbol"] == "BTCUSDT"


def test_runtime_rejects_view_identity_that_differs_from_the_active_session(tmp_path):
    class Market:
        def public_usdm_window(self, *_args, **_kwargs):
            raise AssertionError("identity must be rejected before market I/O")

    account = PriceActionPaperAccount(tmp_path / "runtime-identity.db")
    runtime = PriceActionLabRuntime(Market(), account)
    try:
        runtime.live_state("ETHUSDT", "15m")
        assert False, "mismatched view identity must fail closed"
    except ValueError as exc:
        assert "does not match active Price Action session" in str(exc)
    assert runtime.stream.state == "DISCONNECTED"


def test_runtime_supervisor_starts_saved_live_session_without_browser_request(tmp_path):
    class Market:
        def public_usdm_window(self, *_args, **_kwargs):
            return []

    account = PriceActionPaperAccount(tmp_path / "pa-autonomous.db")
    runtime = PriceActionLabRuntime(Market(), account, autostart=False)
    maintained = []
    runtime.ensure = lambda symbol, timeframe: maintained.append((symbol, timeframe))  # type: ignore[method-assign]

    runtime._maintain_live_session()
    assert maintained == [("BTCUSDT", "5m")]

    account.configure(mode="HISTORICAL")
    runtime._maintain_live_session()
    assert maintained == [("BTCUSDT", "5m")]


def test_price_action_runtime_autostart_invokes_server_supervisor(monkeypatch, tmp_path):
    started = threading.Event()

    class Market:
        def public_usdm_window(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(
        PriceActionLabRuntime, "_maintain_live_session",
        lambda _self: started.set() or {"active": True},
    )
    runtime = PriceActionLabRuntime(
        Market(), PriceActionPaperAccount(tmp_path / "pa-server-autostart.db"),
        poll_seconds=1, autostart=True,
    )
    try:
        assert started.wait(1)
    finally:
        runtime.stop()


def test_price_action_rejects_unattested_or_detached_core_proposals(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "pa-core-boundary.db")
    account.configure(execution_config=PaperExecutionConfig(operating_mode="automatic"))

    wrong_version = visual()
    wrong_version["strategy_version"] = "unknown"
    try:
        account.synchronize_strategy(wrong_version, contract_rules=RULES,
                                     candle=bar(1), feed_reliable=True)
        assert False, "unattested native state must fail closed"
    except ValueError as exc:
        assert "not attested" in str(exc)
    assert account.broker.orders() == []

    detached = visual()
    detached["setups"][0]["phase"] = "WAITING_FOR_CONFIRMATION"
    result = account.synchronize_strategy(detached, contract_rules=RULES,
                                          candle=bar(1), feed_reliable=True)
    assert result["created"] == []
    assert result["rejected"][0]["reason"].startswith("native setup")
    assert account.broker.orders() == []


def test_visual_metrics_expose_honest_aggregate_and_strategy_scopes():
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    engine.ingest_closed_bars([bar(index) for index in range(12)])
    state = engine.visual_state()
    assert state["metrics_scope"]["scope"] == "AGGREGATE_PA1_PA4"
    assert state["metrics_scope"]["symbol"] == "BTCUSDT"
    assert state["metrics_scope"]["timeframe"] == "5m"
    assert state["metrics_scope"]["configuration_id"]
    assert set(state["metrics"]["by_strategy"]) == {
        "PA1_SR_REJECTION", "PA2_TREND_PULLBACK", "PA3_FLIP_RETEST", "PA4_FALSE_BREAK_REVERSAL",
    }
    assert state["metrics"]["net_r"] == sum(
        row["net_r"] for row in state["metrics"]["by_strategy"].values())


def test_pattern_combinations_are_metadata_and_generic_rejection_has_no_pattern_gate():
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", swing_left=1, swing_right=1))
    mother = bar(0, 100, 110, 90, 105)
    inside_pin = bar(1, 104, 108, 96, 107)
    engine.process_closed_bar(mother)
    snapshot = engine.process_closed_bar(inside_pin)
    names = {row["name"] for row in snapshot.patterns}
    assert {"inside_bar", "inside_pin_bar", "mother_bar_reference"}.issubset(names)

    fakey_engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", swing_left=1, swing_right=1))
    rows = [mother, bar(1, 104, 108, 95, 100), bar(2, 98, 103, 88, 96)]
    snapshots = fakey_engine.ingest_closed_bars(rows)
    assert any("fakey" in row["name"] for row in snapshots[-1].patterns)


def test_higher_timeframe_zone_uses_confirmation_time_for_age_expiry():
    engine = NativePriceActionEngine(PriceActionConfig(
        symbol="BTCUSDT", zone_expiry_bars=1, zone_timeframe_scope="higher_timeframe"))
    engine.bars = [bar(index) for index in range(4)]
    engine.zones["htf-zone"] = PriceZone(
        id="htf-zone", role="support", original_role="support", low=99, high=100,
        created_at=bar(0).timestamp, confirmed_at=bar(1).timestamp,
        source_swing_ids=["foreign-htf-swing"], timeframe_scope="higher_timeframe:4h")

    engine._expire_zones(3)

    assert engine.zones["htf-zone"].active is False
    assert engine.zones["htf-zone"].expiration_reason == "maximum_age"


def test_replay_and_live_completed_candles_are_deterministic():
    rows = [bar(i, 100 + (i % 6), 103 + (i % 6), 97 + (i % 6), 101 + (i % 6)) for i in range(80)]
    live = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    replay = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m"))
    live.ingest_closed_bars(rows)
    for row in rows:
        replay.process_closed_bar(row)
    left, right = live.visual_state(), replay.visual_state()
    assert left["snapshot"] == right["snapshot"]
    assert left["zones"] == right["zones"]
    assert left["proposals"] == right["proposals"]


def test_mark_price_liquidation_is_paper_only_and_estimated(tmp_path):
    account = PriceActionPaperAccount(tmp_path / "liquidation.db")
    account.set_leverage(10)
    account.broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1)
    account.broker.process_candle("BTCUSDT", bar(1, 100, 101, 99, 100))
    boundary = account.state()["positions"][0]["estimated_liquidation_price"]
    result = account.broker.process_mark("BTCUSDT", boundary - .1)
    assert result["liquidated"] is True
    assert "estimate" in result["model"]
    assert account.state()["positions"] == []


def test_research_runs_are_deterministic_partitioned_and_comparison_is_guarded(tmp_path):
    rows = [bar(i, 100 + (i % 8), 103 + (i % 8), 97 + (i % 8), 101 + (i % 8)) for i in range(120)]
    runner = PriceActionExperimentRunner(PriceActionExperimentStore(tmp_path / "research.db"))
    config = PriceActionConfig(symbol="BTCUSDT", swing_left=2, swing_right=2)
    first = runner.run({("BTCUSDT", "5m"): rows}, config, walk_forward_folds=3)
    second = runner.run({("BTCUSDT", "5m"): rows}, config, walk_forward_folds=3)
    assert first["experiment_id"] == second["experiment_id"]
    assert set(first["by_partition"]) <= {"development", "validation", "untouched_oos"}
    assert len(first["walk_forward"]) >= 1
    pa = {"assumptions": {"source_data": "same", "symbols": ["BTCUSDT"], "timeframes": ["5m"],
          "date_partitions": {}, "cost_model": {}, "fill_model": "same", "ambiguity": "stop_first",
          "risk_per_trade_pct": .5}, "metrics": {"expectancy_r": 0}}
    smc = {**pa, "metrics": {"expectancy_r": -0.1}}
    assert controlled_pa_smc_report(pa, smc)["mixed_strategy"] is False
    smc = {**smc, "assumptions": {**smc["assumptions"], "fill_model": "different"}}
    try:
        controlled_pa_smc_report(pa, smc)
        assert False, "comparison should refuse mismatched assumptions"
    except ValueError:
        pass
