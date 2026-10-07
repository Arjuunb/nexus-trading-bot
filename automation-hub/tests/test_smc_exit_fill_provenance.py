"""Exit evidence is atomic broker truth, not trading authority or journal proof."""
import ast
import json
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from execution.paper_broker_v2 import PaperBrokerV2


@pytest.fixture
def broker(tmp_path):
    instance = make_broker(tmp_path / "smc.db")
    yield instance
    instance._c.close()


def make_broker(path, account_type="SMC_LAB", **options):
    options.setdefault("leverage", 5)  # finite isolated liquidation boundary in mark fixtures
    return PaperBrokerV2(path, account_type=account_type, execution_engine=account_type,
                         fee_rate=0, spread_bps=0, slippage_bps=0,
                         participation_rate=1, **options)


def submit(broker, key="entry-decision", **changes):
    values = dict(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
                  strategy="SMC_M1_SWEEP_REVERSAL", timeframe="5m", candle_id=key)
    values.update(changes)
    return broker.submit(**values)


def candle(broker, price=100, **changes):
    bar = dict(open=price, high=price + 1, low=price - 1, close=price, volume=100,
               timestamp="2026-10-07T12:00:00+00:00")
    bar.update(changes)
    return broker.process_candle("BTCUSDT", bar)


def quote(price, sequence=1):
    stamp = (datetime.now(timezone.utc) + timedelta(seconds=sequence)).isoformat()
    return dict(bid=price, ask=price + .1, mark=price, received_at=stamp,
                event_timestamp=stamp, sequence=sequence, quote_event_id=f"quote-{sequence}")


def exit_rows(broker):
    from execution.paper_exit_provenance import decode_exit_fill
    return [decode_exit_fill(row[0]) if row[0] is not None else None
            for row in broker._c.execute("SELECT fill_exit_json FROM v2_fills ORDER BY rowid")]


def test_candle_exit_retains_trigger_and_original_identity_in_same_fill(broker):
    entry = submit(broker)
    candle(broker)
    origin = broker.positions()[0]
    broker.set_protection("BTCUSDT", stop_loss=90, take_profit=120)
    candle(broker, price=80)
    opened, exited = exit_rows(broker)
    assert opened is None
    assert exited["trigger_kind"] == "POSITION_STOP_LOSS"
    assert exited["trigger_price"] == exited["effective_stop"] == 90
    assert exited["raw_reference_price"] == exited["price"] == 80  # adverse gap, not stop price
    assert exited["closed_quantity"] == exited["quantity"] == 1
    assert exited["position"]["position_id"] == origin["position_id"]
    assert exited["position"]["entry_order_id"] == entry["id"]
    assert exited["position"]["entry_execution_key"] == "entry-decision"
    assert exited["position"]["entry_timeframe"] == "5m"
    assert exited["protection"] == dict(stop_loss=90, take_profit=120, trailing_offset=None, peak_price=101)
    assert exited["fill_source"] == "CANDLE"
    assert exited["observation"]["timestamp"] == "2026-10-07T12:00:00+00:00"
    assert exited["persisted_order"] is False and exited["reduce_only"] is True
    row = broker._c.execute("SELECT * FROM v2_fills ORDER BY rowid DESC LIMIT 1").fetchone()
    position = json.loads(row["fill_position_json"])
    assert exited["fill_id"] == row["id"] == position["fill_id"]
    assert exited["order_id"] == row["order_id"] == position["order_id"]
    assert exited["position"] == position["before"]
    assert len(broker.orders()) == 1 and not broker.positions() and len(broker.fills()) == 2


@pytest.mark.parametrize("side,stop,target,price,kind", [
    ("buy", 90, 120, 80, "POSITION_STOP_LOSS"),
    ("sell", 110, 80, 120, "POSITION_STOP_LOSS"),
    ("buy", 90, 120, 130, "POSITION_TAKE_PROFIT"),
    ("sell", 110, 80, 70, "POSITION_TAKE_PROFIT"),
])
@pytest.mark.parametrize("source", ["CANDLE", "TICK"])
def test_long_short_stop_target_actual_trigger(broker, side, stop, target, price, kind, source):
    submit(broker, side=side)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=stop, take_profit=target)
    if source == "TICK":
        broker.process_tick("BTCUSDT", quote(price))
    else:
        candle(broker, price=price)
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == kind and evidence["fill_source"] == source
    assert evidence["trigger_price"] == (stop if kind == "POSITION_STOP_LOSS" else target)
    assert evidence["position"]["side"] == ("long" if side == "buy" else "short")
    assert evidence["protection"]["stop_loss"] == stop
    assert evidence["protection"]["take_profit"] == target
    assert evidence["order_id"].startswith("protective-")
    assert len(broker.orders()) == 1 and not broker.positions()


@pytest.mark.parametrize("side,stop,target", [("buy", 90, 110), ("sell", 110, 90)])
def test_both_hit_preserves_existing_stop_first_policy(broker, side, stop, target):
    submit(broker, side=side)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=stop, take_profit=target)
    candle(broker, low=80, high=120)
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == "POSITION_STOP_LOSS"
    assert evidence["trigger_price"] == evidence["price"] == stop


@pytest.mark.parametrize("side,stop,high,low,effective,peak", [
    ("buy", 90, 111, 100, 106, 111),
    ("sell", 110, 100, 89, 94, 89),
])
def test_position_trailing_exit_records_computed_not_fabricated_persisted_stop(broker, side, stop, high, low, effective, peak):
    submit(broker, side=side)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=stop, trailing_offset=5)
    candle(broker, high=high, low=low)
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == "POSITION_TRAILING_STOP"
    assert evidence["effective_stop"] == evidence["trigger_price"] == effective
    assert evidence["effective_peak"] == peak
    assert evidence["protection"]["stop_loss"] == stop
    assert evidence["protection"]["trailing_offset"] == 5


def test_trailing_config_does_not_mislabel_static_stop_trigger(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=99, trailing_offset=50)
    candle(broker, price=90)
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == "POSITION_STOP_LOSS" and evidence["effective_stop"] == 99


def test_tick_does_not_invent_a_trailing_trigger(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90, trailing_offset=5)
    broker.process_tick("BTCUSDT", quote(80))
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == "POSITION_STOP_LOSS"
    assert evidence["effective_peak"] is None


def test_explicit_trailing_order_is_not_position_stop(broker):
    submit(broker)
    candle(broker)
    order = submit(broker, "trailing-order", side="sell", reduce_only=True,
                   order_type="trailing_stop", trailing_offset=5)
    candle(broker, high=110, low=100)
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == "ORDER_TRAILING_STOP"
    assert evidence["trigger_price"] == 105
    assert evidence["order_id"] == order["id"] and evidence["persisted_order"] is True
    assert evidence["order"]["trailing_offset"] == 5
    assert evidence["protection"]["stop_loss"] is None


def test_reduce_and_reversal_keep_exit_portion_and_net_origin_distinct(broker):
    entry = submit(broker, quantity=2)
    candle(broker)
    original = broker.positions()[0]
    reduce = submit(broker, "reduce", side="sell", reduce_only=True, quantity=.5)
    candle(broker)
    reverse = submit(broker, "reverse", side="sell", quantity=2.5)
    candle(broker)
    opened, reduced, reversed_ = exit_rows(broker)
    assert opened is None
    assert reduced["trigger_kind"] == "ORDER_REDUCE_ONLY"
    assert reduced["order_id"] == reduce["id"] and reduced["closed_quantity"] == .5
    assert reversed_["trigger_kind"] == "NETTING_FILL"
    assert reversed_["closed_quantity"] == 1.5 and reversed_["quantity"] == 2.5
    assert reversed_["reduce_only"] is False and reversed_["order_id"] == reverse["id"]
    assert reversed_["position"]["entry_order_id"] == entry["id"]
    assert reversed_["position"]["position_id"] == original["position_id"]
    assert broker.positions()[0]["position_id"] != original["position_id"]


def test_additions_and_entry_fills_have_no_exit_snapshot(broker):
    submit(broker)
    candle(broker)
    submit(broker, "scale", quantity=1)
    candle(broker)
    assert exit_rows(broker) == [None, None]


def test_partial_exit_rows_are_one_per_actual_fill(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80, volume=.4)
    candle(broker, price=80)
    first, partial, close = exit_rows(broker)
    assert first is None
    assert [partial["closed_quantity"], close["closed_quantity"]] == pytest.approx([.4, .6])
    assert partial["fill_id"] != close["fill_id"] and partial["order_id"] != close["order_id"]
    assert partial["position"]["position_id"] == close["position"]["position_id"]
    assert close["position"]["size"] == pytest.approx(.6)
    assert len(broker.orders()) == 1 and len(broker.fills()) == 3 and not broker.positions()


@pytest.mark.parametrize("kind", ["LEGACY_POSITION_REMEDIATION", "PAPER_LIQUIDATION"])
def test_non_strategy_mark_exits_keep_explicit_reason(broker, kind):
    submit(broker)
    candle(broker)
    if kind == "LEGACY_POSITION_REMEDIATION":
        broker.close_position_at_mark("BTCUSDT", 100, reason=kind)
    else:
        broker.process_mark("BTCUSDT", .01)
    evidence = exit_rows(broker)[-1]
    assert evidence["trigger_kind"] == kind and evidence["fill_source"] == "MARK"
    assert evidence["position"]["entry_execution_key"] == "entry-decision"
    assert not broker.positions() and len(broker.orders()) == 1 and len(broker.fills()) == 2


@pytest.mark.parametrize("account_type,engine", [("PAPER", "PAPER"), ("PA_LAB", "PA_LAB"), ("SMC_LAB", "PAPER"), ("PA_LAB", "SMC_LAB")])
def test_other_accounts_or_engines_never_capture_smc_exit(tmp_path, account_type, engine):
    instance = make_broker(tmp_path / "other.db", account_type)
    instance.execution_engine = engine
    try:
        submit(instance)
        candle(instance)
        instance.set_protection("BTCUSDT", stop_loss=90)
        candle(instance, price=80)
        assert exit_rows(instance) == [None, None]
    finally:
        instance._c.close()


@pytest.mark.parametrize("source", ["CANDLE", "TICK", "MARK", "REMEDIATION"])
@pytest.mark.parametrize("boundary", ["encode", "after_insert"])
def test_exit_evidence_failure_rolls_back_fill_and_preserves_original_position(broker, monkeypatch, source, boundary):
    import execution.paper_broker_v2 as module
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    original = broker.positions()[0]
    before = list(broker._c.iterdump())
    account = broker.account(persist_metrics=False)
    if boundary == "encode":
        real = module.encode_exit_fill
        def fail(**fields):
            real(**fields)
            raise RuntimeError("fixture exit serialization failure")
        monkeypatch.setattr(module, "encode_exit_fill", fail)
    else:
        broker._c.execute("CREATE TRIGGER injected AFTER INSERT ON v2_fills WHEN NEW.fill_exit_json IS NOT NULL BEGIN SELECT RAISE(ABORT, 'fixture exit disk full'); END")
    def process():
        if source == "CANDLE":
            return candle(broker, price=80)
        if source == "TICK":
            return broker.process_tick("BTCUSDT", quote(80))
        if source == "MARK":
            return broker.process_mark("BTCUSDT", .01)
        return broker.close_position_at_mark("BTCUSDT", 100, reason="LEGACY_POSITION_REMEDIATION")
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        process()
    assert len(broker.orders()) == len(broker.positions()) == len(broker.fills()) == 1
    assert broker.positions()[0] == original
    assert broker.account(persist_metrics=False) == account
    assert broker._c.execute("SELECT count(*) FROM v2_quote_cursor").fetchone()[0] == 0
    if boundary == "encode":
        monkeypatch.setattr(module, "encode_exit_fill", real)
    else:
        broker._c.execute("DROP TRIGGER injected")
    assert list(broker._c.iterdump()) == before
    process()
    assert len(broker.orders()) == 1 and not broker.positions() and len(broker.fills()) == 2
    assert sum(row is not None for row in exit_rows(broker)) == 1


def test_restart_duplicate_quote_and_100_refreshes_do_not_add_exit_evidence(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    tick = quote(80)
    broker.process_tick("BTCUSDT", tick)
    before = list(broker._c.iterdump())
    raw = broker._c.execute("SELECT fill_exit_json FROM v2_fills WHERE fill_exit_json IS NOT NULL").fetchone()[0]
    restarted = make_broker(broker.path)
    try:
        for _ in range(100):
            assert restarted.process_tick("BTCUSDT", tick)["accepted"] is False
            assert not candle(restarted, price=80)["events"]
            restarted.positions()
            restarted.fills()
            restarted.orders()
            assert exit_rows(restarted)[-1]["fill_id"] == json.loads(raw)["fill_id"]
        assert list(restarted._c.iterdump()) == before
        assert len(restarted.orders()) == 1 and not restarted.positions() and len(restarted.fills()) == 2
    finally:
        restarted._c.close()


def test_legacy_schema_upgrade_and_restore_never_reconstruct_exit_evidence(broker, tmp_path):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80)
    snapshot = broker.export_state()
    copied = make_broker(tmp_path / "copied.db", account_id=broker._account_row()["account_id"])
    try:
        copied.restore_state(snapshot)
        assert exit_rows(copied) == exit_rows(broker)
        legacy = deepcopy(snapshot)
        for row in legacy["fills"]:
            row.pop("fill_exit_json")
        copied.restore_state(legacy)
        assert exit_rows(copied) == [None, None]
        copied._c.execute("ALTER TABLE v2_fills DROP COLUMN fill_exit_json")
        copied._c.commit()
    finally:
        copied._c.close()
    upgraded = make_broker(tmp_path / "copied.db")
    try:
        assert exit_rows(upgraded) == [None, None]
        assert len(upgraded.orders()) == 1 and not upgraded.positions() and len(upgraded.fills()) == 2
    finally:
        upgraded._c.close()


def test_missing_or_foreign_parent_is_not_invented(broker):
    entry = submit(broker)
    candle(broker)
    broker._c.execute("UPDATE v2_orders SET account_id='other' WHERE id=?", (entry["id"],))
    broker._c.commit()
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80)
    position = exit_rows(broker)[-1]["position"]
    assert position["entry_order_id"] == entry["id"] and position["entry_execution_key"] is None
    assert position["entry_timeframe"] is None


@pytest.mark.parametrize("source", ["CANDLE", "TICK", "MARK"])
def test_capture_does_not_change_broker_execution_math(tmp_path, source):
    brokers = [make_broker(tmp_path / f"{kind}.db", kind) for kind in ("SMC_LAB", "PAPER")]
    try:
        for instance in brokers:
            submit(instance)
            candle(instance)
            instance.set_protection("BTCUSDT", stop_loss=90, take_profit=120, trailing_offset=5)
            if source == "CANDLE":
                candle(instance, low=80, high=130)
            elif source == "TICK":
                instance.process_tick("BTCUSDT", quote(80))
            else:
                instance.process_mark("BTCUSDT", .01)
        fields = "quantity,price,fee,realized_pnl,spread,slippage,stop_loss,take_profit,risk_amount"
        fills = [list(instance._c.execute(f"SELECT {fields} FROM v2_fills ORDER BY rowid")) for instance in brokers]
        assert [tuple(row) for row in fills[0]] == [tuple(row) for row in fills[1]]
        assert brokers[0].positions() == brokers[1].positions() == []
        for field in ("balance", "fees_paid", "realized_pnl", "funding_paid"):
            assert brokers[0].account(persist_metrics=False)[field] == brokers[1].account(persist_metrics=False)[field]
    finally:
        for instance in brokers:
            instance._c.close()


def sample(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80)
    return broker._c.execute("SELECT fill_exit_json FROM v2_fills WHERE fill_exit_json IS NOT NULL").fetchone()[0]


@pytest.mark.parametrize("mutation", [
    lambda v: v.update(schema_version=True),
    lambda v: v.update(scope="PA_LAB"),
    lambda v: v.update(quantity=True),
    lambda v: v.update(price=float("nan")),
    lambda v: v.update(price=10 ** 1000),
    lambda v: v.update(closed_quantity=2),
    lambda v: v.update(side="buy"),
    lambda v: v.update(trigger_kind="UNKNOWN_GUESS"),
    lambda v: v.update(trigger_kind=[]),
    lambda v: v.update(trigger_price=95, effective_stop=95),
    lambda v: v.update(effective_stop=0),
    lambda v: v.update(account_id="\ud800"),
    lambda v: v["position"].update(extra="unexpected"),
    lambda v: v["protection"].update(stop_loss=-1),
    lambda v: v["observation"].update(bid=True),
    lambda v: v["observation"].update(timestamp="not a timestamp"),
    lambda v: v["order"].update(type="guessed"),
])
def test_read_decoder_fails_closed_without_using_validator_to_trade(broker, mutation):
    from execution.paper_exit_provenance import decode_exit_fill
    value = json.loads(sample(broker))
    mutation(value)
    with pytest.raises(ValueError):
        decode_exit_fill(json.dumps(value))


@pytest.mark.parametrize("raw", [None, "[]", "{}", "{" * 30, "x" * 8193, "[" * 1500 + "]" * 1500])
def test_invalid_payloads_are_not_exit_proof(raw):
    from execution.paper_exit_provenance import decode_exit_fill
    with pytest.raises(ValueError):
        decode_exit_fill(raw)


def test_formatter_has_no_guardian_network_or_strategy_dependency():
    source = Path(__file__).parents[1] / "execution" / "paper_exit_provenance.py"
    tree = ast.parse(source.read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names]
    assert not any(name.startswith(("tradexa", "services", "bot", "requests", "urllib", "sqlite3")) for name in imports)


@pytest.mark.parametrize("boundary,exit_count,position_count", [
    ("encode", 0, 1), ("after_insert", 0, 1), ("after_commit", 1, 0),
])
def test_hard_process_exit_before_or_after_commit_keeps_atomic_truth(broker, boundary, exit_count, position_count):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    origin = broker.positions()[0]["position_id"]
    script = '''
import os, sys
from execution import paper_broker_v2 as module
b = module.PaperBrokerV2(sys.argv[1], account_type="SMC_LAB", execution_engine="SMC_LAB",
                         leverage=5, fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
boundary = sys.argv[2]
if boundary == "encode":
    def crash(**fields):
        os._exit(73)
    module.encode_exit_fill = crash
else:
    class Connection:
        def __init__(self, inner): self.inner = inner
        def __getattr__(self, name): return getattr(self.inner, name)
        def execute(self, sql, *args):
            result = self.inner.execute(sql, *args)
            if boundary == "after_insert" and sql.startswith("INSERT INTO v2_fills"):
                os._exit(73)
            return result
        def commit(self):
            self.inner.commit()
            os._exit(73)
    b._c = Connection(b._c)
b.process_candle("BTCUSDT", dict(open=80, high=81, low=79, close=80, volume=100))
raise AssertionError("fixture crash was not reached")
'''
    child = subprocess.run([sys.executable, "-c", script, broker.path, boundary],
                           capture_output=True, text=True, timeout=15)
    assert child.returncode == 73, child.stderr
    restarted = make_broker(broker.path)
    try:
        assert len(restarted.orders()) == 1
        assert len(restarted.positions()) == position_count
        assert len(restarted.fills()) == 1 + exit_count
        assert sum(row is not None for row in exit_rows(restarted)) == exit_count
        if position_count:
            assert restarted.positions()[0]["position_id"] == origin
            candle(restarted, price=80)
        recorded = exit_rows(restarted)[-1]
        assert recorded["position"]["position_id"] == origin
        assert recorded["position"]["entry_execution_key"] == "entry-decision"
        for _ in range(3):
            another = make_broker(broker.path)
            try:
                assert candle(another, price=80)["events"] == []
                assert exit_rows(another)[-1] == recorded
                assert len(another.orders()) == 1 and not another.positions() and len(another.fills()) == 2
            finally:
                another._c.close()
    finally:
        restarted._c.close()


@pytest.mark.parametrize("boundary", ["format", "order_update"])
def test_explicit_order_exit_failure_never_leaves_a_filled_order_without_evidence(broker, monkeypatch, boundary):
    import execution.paper_broker_v2 as module
    submit(broker)
    candle(broker)
    origin = broker.positions()[0]
    close = submit(broker, "close-decision", side="sell", reduce_only=True)
    if boundary == "format":
        real = module.encode_exit_fill
        def fail(**fields):
            raise RuntimeError("fixture format failure")
        monkeypatch.setattr(module, "encode_exit_fill", fail)
    else:
        broker._c.execute("CREATE TRIGGER injected BEFORE UPDATE OF filled ON v2_orders BEGIN SELECT RAISE(ABORT, 'fixture order update'); END")
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        candle(broker)
    assert len(broker.orders()) == 2 and len(broker.positions()) == 1 and len(broker.fills()) == 1
    assert broker.positions()[0] == origin and broker.order(close["id"])["status"] == "open"
    if boundary == "format":
        monkeypatch.setattr(module, "encode_exit_fill", real)
    else:
        broker._c.execute("DROP TRIGGER injected")
    candle(broker)
    assert len(broker.orders()) == 2 and not broker.positions() and len(broker.fills()) == 2
    assert exit_rows(broker)[-1]["order_id"] == close["id"]


def test_no_fill_or_refresh_never_creates_exit_evidence(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    for _ in range(100):
        assert not candle(broker)["events"]
    assert exit_rows(broker) == [None]
    candle(broker, price=80, volume=0)
    assert exit_rows(broker) == [None] and len(broker.positions()) == 1


def test_bounded_formatter_rejects_nonfinite_and_ever_growing_fields(broker):
    from execution.paper_exit_provenance import encode_exit_fill
    fields = json.loads(sample(broker))
    fields.pop("schema_version")
    fields.pop("scope")
    fields["price"] = float("nan")
    with pytest.raises(ValueError):
        encode_exit_fill(**fields)
    fields["price"] = 80
    fields["observation"]["quote_event_id"] = "x" * 9000
    with pytest.raises(ValueError, match="bound"):
        encode_exit_fill(**fields)


@pytest.mark.parametrize("timestamp", [None, datetime(2026, 10, 7, 12, tzinfo=timezone.utc)])
def test_input_observation_time_is_not_inferred_from_fill_time(broker, timestamp):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    candle(broker, price=80, timestamp=timestamp)
    stamp = exit_rows(broker)[-1]["observation"]["timestamp"]
    assert stamp == (timestamp.isoformat() if timestamp is not None else None)


def test_duplicate_json_identity_fields_are_not_exit_proof(broker):
    from execution.paper_exit_provenance import decode_exit_fill
    raw = sample(broker)
    with pytest.raises(ValueError):
        decode_exit_fill('{"fill_id":"different-fill",' + raw[1:])


def test_oversized_quote_evidence_rolls_back_cursor_and_exit(broker):
    submit(broker)
    candle(broker)
    broker.set_protection("BTCUSDT", stop_loss=90)
    before = list(broker._c.iterdump())
    with pytest.raises(ValueError, match="bound"):
        broker.process_tick("BTCUSDT", {**quote(80), "quote_event_id": "x" * 9000})
    assert list(broker._c.iterdump()) == before
    assert len(broker.orders()) == len(broker.positions()) == len(broker.fills()) == 1
    broker.process_tick("BTCUSDT", quote(80))
    assert len(broker.orders()) == 1 and not broker.positions() and len(broker.fills()) == 2
