from datetime import datetime, timedelta, timezone
import json
import math

import pytest

from bot.types import Bar
from services.price_action_lab import PriceActionPaperAccount
from services.smc_strategy_lab import SMCPaperAccount, SMCPaperConfig, SMCStrategyLabRuntime
from services.smc_strategy_v1 import evaluate
from services.native_smc_live_visual import reconcile_market_state
from tests.test_smc_strategy_ladder import seeded_engine


RULES = {
    "tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
    "max_quantity": 100.0, "min_notional": 5.0,
}


def test_smc_account_is_durable_and_isolated_from_price_action(tmp_path):
    smc_path, pa_path = tmp_path / "smc.db", tmp_path / "pa.db"
    smc = SMCPaperAccount(smc_path)
    pa = PriceActionPaperAccount(pa_path)
    assert smc.path != pa.path
    assert smc.state()["account_scope"] == "SMC_STRATEGY_LAB_ONLY"
    assert pa.state()["account_scope"] == "PRICE_ACTION_VISUAL_LAB_ONLY"

    pa_order = pa.broker.submit(symbol="BTCUSDT", side="buy", order_type="limit",
                                quantity=0.1, limit_price=90)
    smc.reset("RESET SMC PAPER")
    assert pa.broker.order(pa_order["id"])["status"] == "open"
    reopened = SMCPaperAccount(smc_path)
    assert reopened.state()["paper_only"] is True
    assert reopened.state()["real_execution_allowed"] is False


def test_manual_order_is_idempotent_and_protection_survives_fill(tmp_path):
    account = SMCPaperAccount(tmp_path / "smc.db")
    request = dict(symbol="BTCUSDT", side="long", order_type="market", rules=RULES,
                   reference_price=100, quantity=0.1, stop_loss=90,
                   target_1=120, target_2=130, idempotency_key="manual-1")
    first = account.submit_order(**request)
    duplicate = account.submit_order(**request)
    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["order"]["id"] == first["order"]["id"]

    candle = Bar(datetime(2026, 8, 24, tzinfo=timezone.utc), 100, 105, 95, 102, 100)
    result = account.process_candle("BTCUSDT", candle)
    assert result["duplicate"] is False
    position = account.broker.positions()[0]
    assert position["stop_loss"] == 90
    assert position["take_profit"] > 130
    visible = account.state()["positions"][0]
    assert visible["planned_rr"] == 3
    assert visible["effective_rr"] >= 3
    assert visible["protection_status"] == "PROTECTED"
    assert account.process_candle("BTCUSDT", candle)["duplicate"] is True


def test_smc_restart_repairs_strategy_protection_and_blocks_stacking(tmp_path):
    path = tmp_path / "smc-repair.db"
    account = SMCPaperAccount(path)
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    account.synchronize_candidate(evaluation, rules=RULES,
                                  reference_price=evaluation["trade_plan"]["entry"],
                                  feed_reliable=True)
    entry = evaluation["trade_plan"]["entry"]
    signal_time = evaluation["proposal"]["signal_timestamp"]
    account.process_candle("BTCUSDT", Bar(signal_time + timedelta(minutes=5),
                                            entry, entry + 1, entry - 1, entry, 10_000))
    original = account.state()["positions"][0]
    account.broker._c.execute(
        "UPDATE v2_positions SET stop_loss=NULL,take_profit=NULL WHERE symbol='BTCUSDT'")
    account.broker._c.commit()

    reopened = SMCPaperAccount(path)
    repaired = reopened.state()["positions"][0]
    assert repaired["stop_loss"] == original["stop_loss"]
    assert repaired["take_profit"] == original["take_profit"]
    assert repaired["effective_rr"] >= repaired["planned_rr"]
    assert any(row["kind"] == "paper_position_protection_repaired"
               for row in reopened.state()["activity"])
    with pytest.raises(ValueError, match="stacking is blocked"):
        reopened.submit_order(symbol="BTCUSDT", side="buy", order_type="market",
                              rules=RULES, reference_price=entry, quantity=.1,
                              stop_loss=entry - 10, target_1=entry + 20,
                              target_2=entry + 30, idempotency_key="blocked-stack")


def test_smc_restart_consolidates_multiple_legacy_fills_conservatively(tmp_path):
    path = tmp_path / "smc-legacy-aggregate.db"
    account = SMCPaperAccount(path)
    first = account.submit_order(
        symbol="BTCUSDT", side="buy", order_type="market", rules=RULES,
        reference_price=100, quantity=.1, stop_loss=90, target_1=110,
        target_2=120, idempotency_key="legacy-first", ownership="strategy",
    )
    account.process_candle("BTCUSDT", Bar(
        datetime(2026, 8, 24, 12, tzinfo=timezone.utc), 100, 101, 99, 100, 1_000))

    second = account.broker.submit(
        symbol="BTCUSDT", side="buy", order_type="market", quantity=.1)
    account.broker.process_candle("BTCUSDT", Bar(
        datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc), 102, 103, 101, 102, 1_000))
    now = datetime.now(timezone.utc).isoformat()
    second_config = {
        "reference_price": 102, "stop_loss": 95, "target_1": 109,
        "target_2": 116, "target_1_r": 1, "target_2_r": 2,
        "rules": RULES,
    }
    account._db.execute(
        "INSERT INTO smc_order_meta(order_id,session_id,ownership,idempotency_key,model_id,model_version,direction,entry,stop,target_1,target_2,status,reason,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (second["id"], account.session()["id"], "strategy", "legacy-second",
         "SMC_M1_SWEEP_REVERSAL", "legacy", "bullish", 102, 95, 109, 116,
         "ENTERED", "legacy filled owner", json.dumps(second_config, sort_keys=True), now, now),
    )
    pending = account.broker.submit(
        symbol="BTCUSDT", side="buy", order_type="limit", quantity=.1, limit_price=90)
    account.broker._c.execute(
        "UPDATE v2_positions SET stop_loss=NULL,take_profit=NULL WHERE symbol='BTCUSDT'")
    account.broker._c.commit()

    reopened = SMCPaperAccount(path)
    repaired = reopened.state()["positions"][0]
    assert repaired["stop_loss"] == 95
    assert repaired["take_profit"] is not None
    assert repaired["planned_rr"] == 2
    assert repaired["effective_rr"] >= 2
    assert repaired["protection_status"] == "PROTECTED"
    assert reopened.broker.order(pending["id"])["status"] == "cancelled"
    repair = next(row for row in reopened.state()["activity"]
                  if row["kind"] == "paper_position_protection_repaired")
    assert repair["payload"]["ownership_resolution"] == "legacy_aggregate_conservative"
    assert first["order"]["id"] in repair["payload"]["owner_order_ids"]


def test_risk_sizing_and_session_identity_guards(tmp_path):
    account = SMCPaperAccount(tmp_path / "smc.db")
    placed = account.submit_order(
        symbol="BTCUSDT", side="buy", order_type="limit", rules=RULES,
        reference_price=100, risk_pct=0.5, limit_price=100, stop_loss=90,
        target_1=120, target_2=130, idempotency_key="risk-1")
    assert placed["order"]["quantity"] == 5.0
    with pytest.raises(ValueError, match="cannot change"):
        account.configure(symbol="ETHUSDT")
    with pytest.raises(ValueError, match="parked"):
        account.configure(config=SMCPaperConfig(model_id="SMC_M2_BOS_CONTINUATION"))


def test_session_lifecycle_and_exact_reset_confirmation(tmp_path):
    account = SMCPaperAccount(tmp_path / "smc.db")
    original = account.session()["id"]
    with pytest.raises(ValueError, match="exactly match"):
        account.reset("reset")
    reset = account.reset("RESET SMC PAPER")
    assert reset["session"]["id"] != original
    assert reset["account"]["balance"] == 10_000
    assert any(row["id"] == original and row["end_reason"] == "paper_reset"
               for row in account.sessions())
    duplicated = account.duplicate(reset["session"]["id"])
    assert duplicated["session"]["id"] != reset["session"]["id"]
    ended = account.end()
    assert ended["status"] == "ended"
    resumed = account.resume(duplicated["session"]["id"])
    assert resumed["session"]["status"] == "active"


def test_restart_does_not_create_new_session_over_ended_session_exposure(tmp_path):
    path = tmp_path / "orphaned-exposure.db"
    account = SMCPaperAccount(path)
    old = account.session()["id"]
    account.submit_order(symbol="BTCUSDT", side="buy", order_type="market", rules=RULES,
                         reference_price=100, quantity=.1, stop_loss=90,
                         target_1=110, target_2=120,
                         idempotency_key="restart-exposure")
    account.process_candle(
        "BTCUSDT", Bar(datetime(2026, 8, 24, tzinfo=timezone.utc), 100, 101, 99, 100, 100))
    account.end()

    reopened = SMCPaperAccount(path)

    assert reopened.session() == {}
    assert len(reopened.sessions()) == 1
    assert reopened.state()["positions"][0]["symbol"] == "BTCUSDT"
    resumed = reopened.resume(old)
    assert resumed["session"]["id"] == old
    assert resumed["positions"][0]["symbol"] == "BTCUSDT"


def test_a_new_session_is_armed_but_live_execution_stays_impossible(tmp_path):
    """Arming the default changes which branch runs, not what is reachable.

    The safety property is that neither lab can route to a venue. That is
    asserted here independently of the operating mode, because the mode is a
    behavioural choice and this is the boundary that must never move.
    """
    account = SMCPaperAccount(tmp_path / "smc.db")
    state = account.state()
    assert state["session"]["operating_mode"] == "automatic"
    assert state["execution_mode"] == "PAPER"
    assert state["real_execution_allowed"] is False
    assert not hasattr(SMCPaperAccount, "enable_live")

    evaluation = evaluate(seeded_engine())
    result = account.synchronize_candidate(
        evaluation, rules=RULES,
        reference_price=evaluation["trade_plan"]["entry"], feed_reliable=True,
    )
    assert result["candidate_status"] == "APPROVED_AUTOMATIC"
    assert len(account.broker.orders()) == 1
    # Still paper after placing, not merely before.
    assert account.state()["real_execution_allowed"] is False


@pytest.mark.parametrize("mode,expected,orders", [
    ("signals_only", "SIGNAL_ONLY", 0),
    ("manual_approval", "PENDING_APPROVAL", 0),
    ("automatic", "APPROVED_AUTOMATIC", 1),
])
def test_strategy_candidate_obeys_saved_operating_mode(tmp_path, mode, expected, orders):
    account = SMCPaperAccount(tmp_path / f"{mode}.db")
    account.configure(config=SMCPaperConfig(operating_mode=mode))
    evaluation = evaluate(seeded_engine())
    result = account.synchronize_candidate(evaluation, rules=RULES,
                                           reference_price=evaluation["trade_plan"]["entry"],
                                           feed_reliable=True)
    assert result["candidate_status"] == expected
    assert len(account.broker.orders()) == orders
    assert account.journal()["journal"][0]["native_object_ids"]
    assert account.metrics()["detected_setups"] == 1
    if mode == "manual_approval":
        account.approve_candidate(evaluation["proposal_id"])
        assert len(account.broker.orders()) == 1


def test_smc_automatic_uses_saved_risk_and_attested_core_identity(tmp_path):
    account = SMCPaperAccount(tmp_path / "saved-risk.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic", risk_pct=.25))
    evaluation = evaluate(seeded_engine())
    reference = float(evaluation["trade_plan"]["entry"])
    stop = float(evaluation["trade_plan"]["stop"])
    result = account.synchronize_candidate(
        evaluation, rules=RULES, reference_price=reference, feed_reliable=True)
    assert result["candidate_status"] == "APPROVED_AUTOMATIC"
    metadata = account.state()["order_metadata"][0]
    expected = math.floor(
        (10_000 * .25 / 100) /
        abs(float(metadata["entry"]) - float(metadata["stop"])) / .001 + 1e-12
    ) * .001
    assert result["order"]["order"]["quantity"] == pytest.approx(expected)
    assert metadata["risk_pct"] == .25

    other = SMCPaperAccount(tmp_path / "identity-rejected.db")
    other.configure(config=SMCPaperConfig(operating_mode="automatic"))
    mismatched = json.loads(json.dumps(evaluation, default=str))
    mismatched["data_identity"]["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="identity does not match"):
        other.synchronize_candidate(
            mismatched, rules=RULES, reference_price=reference, feed_reliable=True)
    assert other.broker.orders() == []


def test_smc_invalid_mode_and_failed_automatic_placement_fail_closed(tmp_path):
    invalid = SMCPaperAccount(tmp_path / "invalid-mode.db")
    invalid._db.execute(
        "UPDATE smc_sessions SET operating_mode='unexpected' WHERE id=?",
        (invalid.session()["id"],),
    )
    evaluation = evaluate(seeded_engine())
    result = invalid.synchronize_candidate(
        evaluation, rules=RULES,
        reference_price=evaluation["trade_plan"]["entry"], feed_reliable=True)
    assert result["candidate_status"] == "DATA_PAUSED"
    assert invalid.broker.orders() == []

    rejected = SMCPaperAccount(tmp_path / "placement-rejected.db")
    rejected.configure(config=SMCPaperConfig(operating_mode="automatic"))
    direction = evaluation["proposal"]["direction"]
    stop = float(evaluation["trade_plan"]["stop"])
    crossed_reference = stop - 1 if direction == "bullish" else stop + 1
    failed = rejected.synchronize_candidate(
        evaluation, rules=RULES, reference_price=crossed_reference, feed_reliable=True)
    assert failed["candidate_status"] == "REJECTED"
    assert "automatic paper placement rejected" in failed["reason"]
    assert rejected.broker.orders() == []


def test_unreliable_feed_cannot_create_strategy_order(tmp_path):
    account = SMCPaperAccount(tmp_path / "paused.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    result = account.synchronize_candidate(evaluation, rules=RULES,
                                           reference_price=evaluation["trade_plan"]["entry"],
                                           feed_reliable=False)
    assert result["candidate_status"] == "DATA_PAUSED"
    assert account.broker.orders() == []


def test_staged_smc_entry_is_cancelled_on_feed_failure_before_activation(tmp_path):
    account = SMCPaperAccount(tmp_path / "staged-data-pause.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    signal_time = evaluation["proposal"]["signal_timestamp"]
    account.synchronize_candidate(
        evaluation, rules=RULES, reference_price=evaluation["trade_plan"]["entry"],
        feed_reliable=True, closed_candle_time=signal_time)

    account.synchronize_candidate(
        evaluation, rules=RULES, reference_price=evaluation["trade_plan"]["entry"],
        feed_reliable=False, closed_candle_time=signal_time)

    state = account.state()
    assert state["orders"][0]["status"] == "cancelled"
    assert state["order_metadata"][0]["status"] == "DATA_PAUSED"
    assert state["positions"] == []


def test_staged_smc_entry_expires_instead_of_filling_after_missed_candles(tmp_path):
    account = SMCPaperAccount(tmp_path / "staged-expiry.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    signal_time = evaluation["proposal"]["signal_timestamp"]
    result = account.synchronize_candidate(
        evaluation, rules=RULES, reference_price=evaluation["trade_plan"]["entry"],
        feed_reliable=True, closed_candle_time=signal_time.isoformat())
    metadata = account.state()["order_metadata"][0]
    assert datetime.fromisoformat(metadata["expiry_candle"]) == signal_time + timedelta(minutes=5)

    entry = float(evaluation["trade_plan"]["entry"])
    recovery = Bar(signal_time + timedelta(minutes=15), entry, entry + 1,
                   entry - 1, entry, 1_000)
    processed = account.process_candle("BTCUSDT", recovery)

    assert processed["events"] == []
    assert processed["expired_entries"][0]["order_id"] == result["order"]["order"]["id"]
    assert account.state()["order_metadata"][0]["status"] == "EXPIRED"
    assert account.state()["positions"] == []


def test_strategy_fill_creates_half_size_t1_and_uses_stop_first_on_ambiguous_bar(tmp_path):
    account = SMCPaperAccount(tmp_path / "scale-out.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    account.synchronize_candidate(evaluation, rules=RULES,
                                  reference_price=evaluation["trade_plan"]["entry"],
                                  feed_reliable=True)
    entry = evaluation["trade_plan"]["entry"]
    signal_time = evaluation["proposal"]["signal_timestamp"]
    first = Bar(signal_time + timedelta(minutes=5), entry, entry + 1,
                entry - 1, entry, 10_000)
    account.process_candle("BTCUSDT", first)
    position = account.broker.positions()[0]
    t1_meta = next(row for row in account.state()["order_metadata"]
                   if row["ownership"] == "strategy_target_1")
    t1_order = account.broker.order(t1_meta["order_id"])
    assert t1_order["quantity"] == pytest.approx(position["size"] * 0.5)
    assert position["stop_loss"] is not None and position["take_profit"] is not None

    ambiguous = Bar(datetime(2026, 8, 24, 12, 5, tzinfo=timezone.utc), entry,
                    float(t1_meta["target_1"]) + 1, float(position["stop_loss"]) - 1, entry, 10_000)
    account.process_candle("BTCUSDT", ambiguous)
    assert account.broker.order(t1_meta["order_id"])["status"] == "cancelled"
    assert account.broker.positions() == []
    assert any(row["kind"] == "intrabar_ambiguity_stop_first" for row in account.state()["activity"])


def test_runtime_reprocessing_same_closed_bar_cannot_duplicate_strategy_order(tmp_path, monkeypatch):
    account = SMCPaperAccount(tmp_path / "runtime.db")
    account.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    price = evaluation["trade_plan"]["entry"]
    now = datetime.now(timezone.utc)
    candle_time = now - timedelta(minutes=5)
    candle = {"timestamp": candle_time.isoformat(), "open": price, "high": price + 1,
              "low": price - 1, "close": price, "volume": 1_000}
    monkeypatch.setattr("services.native_smc_live_visual.live_visual_state",
                        lambda *args, **kwargs: {"candles": [candle], "source_strategy": evaluation,
                                                "live_display": {"last_price": price},
                                                "data_provenance": {"last_closed_candle": candle_time.isoformat()}})

    class Market:
        def usdm_contract_rules(self, symbol): return RULES
        def public_usdm_quote(self, symbol):
            return {"bid": price, "ask": price + .01, "mark": price,
                    "provider_time": datetime.now(timezone.utc).isoformat(),
                    "funding_rate": 0.0001,
                    "last_funding_time": "2026-08-24T08:00:00+00:00",
                    "next_funding_time": "2026-08-24T16:00:00+00:00"}

    runtime = SMCStrategyLabRuntime(Market(), account, autostart=False)
    runtime.tick(); runtime.tick()
    strategy_orders = [row for row in account.state()["order_metadata"] if row["ownership"] == "strategy"]
    assert len(strategy_orders) == 1
    assert len(account.state()["funding_events"]) == 1


def test_smc_runtime_retries_failed_or_dead_market_stream(tmp_path):
    account = SMCPaperAccount(tmp_path / "stream-retry.db")
    closed = Bar(datetime(2026, 8, 24, 12, tzinfo=timezone.utc), 100, 102, 98, 101, 1_000)

    class Market:
        def public_usdm_window(self, *_args, **_kwargs):
            return [closed]

        def public_usdm_quote(self, _symbol):
            return {"bid": 100, "ask": 101, "mark": 100.5}

    class RetryStream:
        def __init__(self):
            self.attempts = 0
            self._running = False

        @property
        def running(self):
            return self._running

        def start(self, _symbol, _timeframe):
            self.attempts += 1
            self._running = self.attempts > 1
            return self._running

        def snapshot(self):
            return {
                "connection": {"state": "SYNCHRONIZED", "reliable": True,
                               "health_reason": "test stream synchronized"},
                "quote": {"bid": 100, "ask": 101, "mark": 100.5},
                "closed_bars": [closed],
            }

        def stop(self):
            self._running = False

    runtime = SMCStrategyLabRuntime(Market(), account, autostart=False)
    runtime.stream = RetryStream()
    visual = {
        "live_display": {},
        "data_provenance": {"last_closed_candle": closed.timestamp.isoformat()},
    }

    with pytest.raises(RuntimeError, match="failed to start"):
        runtime.reconcile_visual(visual, symbol="BTCUSDT", timeframe="5m")
    assert runtime._stream_identity is None

    runtime.reconcile_visual(visual, symbol="BTCUSDT", timeframe="5m")
    assert runtime.stream.attempts == 2
    assert runtime._stream_identity == ("BTCUSDT", "5m")

    runtime.stream._running = False
    runtime.reconcile_visual(visual, symbol="BTCUSDT", timeframe="5m")
    assert runtime.stream.attempts == 3


def test_market_reconciliation_fails_closed_and_accepts_fresh_matching_quote():
    now = datetime(2026, 8, 24, 12, 5, 5, tzinfo=timezone.utc)
    base = {"candles": [{"close": 100}], "live_display": {"last_price": 100},
            "data_provenance": {"last_closed_candle": "2026-08-24T12:00:00+00:00"}}
    stale = reconcile_market_state(json.loads(json.dumps(base)), None, timeframe="5m", now=now)
    assert stale["live_display"]["reliable"] is False
    assert stale["live_display"]["new_entries_paused"] is True
    fresh = reconcile_market_state(json.loads(json.dumps(base)), {
        "bid": 99.99, "ask": 100.01, "mark": 100,
        "provider_time": now.isoformat(), "funding_rate": 0.0001,
        "last_funding_time": "2026-08-24T08:00:00+00:00",
        "next_funding_time": "2026-08-24T16:00:00+00:00",
    }, timeframe="5m", now=now)
    assert fresh["live_display"]["connection_state"] == "SYNCHRONIZED"
    assert fresh["live_display"]["reliable"] is True


def test_funding_is_idempotent_and_journal_notes_are_append_only(tmp_path):
    account = SMCPaperAccount(tmp_path / "funding.db")
    account.submit_order(symbol="BTCUSDT", side="buy", order_type="market", rules=RULES,
                         reference_price=100, quantity=.1, stop_loss=90, target_1=110,
                         target_2=120, idempotency_key="funding-position")
    account.process_candle("BTCUSDT", Bar(datetime(2026, 8, 24, tzinfo=timezone.utc),
                                           100, 101, 99, 100, 100))
    first = account.apply_funding_once(symbol="BTCUSDT", funding_time="2026-08-24T08:00:00+00:00",
                                       rate=.0001, mark_price=100)
    second = account.apply_funding_once(symbol="BTCUSDT", funding_time="2026-08-24T08:00:00+00:00",
                                        rate=.0001, mark_price=100)
    assert first["applied"] is True
    assert second["reason"] == "funding event already processed"

    account.configure(config=SMCPaperConfig(operating_mode="signals_only"))
    evaluation = evaluate(seeded_engine())
    account.synchronize_candidate(evaluation, rules=RULES,
                                  reference_price=evaluation["trade_plan"]["entry"], feed_reliable=True)
    journal_id = account.journal()["journal"][0]["journal_id"]
    account.add_journal_note(journal_id, "reviewed without changing engine evidence")
    assert account.journal()["journal"][0]["notes"][0]["note"].startswith("reviewed")


def test_historical_replay_is_session_owned_and_hides_future_bars(tmp_path):
    bars = seeded_engine().bars
    account = SMCPaperAccount(tmp_path / "replay.db")
    account.start(mode="HISTORICAL", symbol="BTCUSDT", timeframe="5m",
                  config=SMCPaperConfig(operating_mode="signals_only"))

    class Market:
        def bars(self, symbol, timeframe, limit): return bars
        def usdm_contract_rules(self, symbol): return RULES

    runtime = SMCStrategyLabRuntime(Market(), account, autostart=False)
    first = runtime.replay_step(steps=5)
    second = runtime.replay_step(steps=3)
    assert first["cursor"] == 5
    assert second["cursor"] == 8
    assert second["session_id"] == account.session()["id"]
    assert second["future_candles_visible"] is False
    account.configure(mode="LIVE_PAPER")
    with pytest.raises(ValueError, match="HISTORICAL"):
        runtime.replay_step()


def test_completed_session_journal_survives_account_reset(tmp_path):
    account = SMCPaperAccount(tmp_path / "journal-history.db")
    evaluation = evaluate(seeded_engine())
    account.synchronize_candidate(evaluation, rules=RULES,
                                  reference_price=evaluation["trade_plan"]["entry"], feed_reliable=True)
    previous_session = account.session()["id"]
    previous_journal = account.journal(previous_session)["journal"]
    account.reset("RESET SMC PAPER")
    assert previous_journal
    assert account.journal(previous_session)["journal"][0]["proposal_id"] == evaluation["proposal_id"]
    assert any(row["session_id"] == previous_session for row in account.journal()["journal"])
