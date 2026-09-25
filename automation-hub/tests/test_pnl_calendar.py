"""App-wide realized P&L calendar (services/pnl_calendar.py, docs/PNL_CALENDAR.md).

Ledger P&L is produced by the real PaperExecutionEngine and lab P&L by the real
PaperBrokerV2; only timestamps are pinned where a test is about dates.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services import pnl_calendar as cal
from services.fill_model import PerfectFill
from services.pnl_calendar import (CalendarError, Filters, PnlCalendar, Realization, collect_ledger,
                                   collect_v2_lab, day_view, month_view, resolve_zone, time_bucket)

UTC = resolve_zone("UTC")
LONDON = resolve_zone("Europe/London")


# --------------------------------------------------------------- builders
def _engine(tmp_path, fee_pct=0.0):
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine

    class Fees(PerfectFill):
        def fee_pct(self, *, maker=False):
            return fee_pct

    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    return ledger, PaperExecutionEngine(ledger, 10_000, fill_model=Fees())


def _round_trip(engine, symbol, side, entry, exit_, size=1.0, stop=None):
    engine.open(symbol=symbol, side=side, size=size, entry=entry, stop=stop)
    engine.close(symbol=symbol, exit_price=exit_)


def _row(tid, net, closed_at, *, symbol="BTCUSDT", fees=0, source="paper", instance_id="", strategy_id="",
         status="closed", opened_at=None, side="long"):
    return {"id": tid, "symbol": symbol, "side": side, "size": 1, "entry": 100, "exit": 101, "pnl": net,
            "realized_pnl": net, "fees": fees, "status": status, "source": source, "closed_at": closed_at,
            "opened_at": opened_at or closed_at, "instance_id": instance_id, "strategy_id": strategy_id,
            "rr": None, "stop": None}


def _r(rid, net, when, *, currency="USDT", trade_id=None, final=True, source="paper_trading", **kw):
    return Realization(id=rid, trade_id=trade_id or rid, source=source, account="t", currency=currency,
                       closed_at=datetime.fromisoformat(when).astimezone(timezone.utc),
                       gross=Decimal(str(net)), fees=Decimal(0), funding=Decimal(0), net=Decimal(str(net)),
                       pnl_basis="test", final=final, **kw)


def _broker(tmp_path, name="lab.db", fee_rate=0.0):
    from execution.paper_broker_v2 import PaperBrokerV2
    return PaperBrokerV2(tmp_path / name, starting_balance=100_000, leverage=5, fee_rate=fee_rate,
                         spread_bps=0, slippage_bps=0, participation_rate=1)


def _bar(price):
    return {"open": price, "high": price, "low": price, "close": price, "volume": 1_000}


def _trade_fills(broker, symbol, side, qty, entry, exits, *, timestamps=None, strategy="", timeframe=""):
    """Open with one market order, then close in parts; returns fill dicts with
    optional pinned timestamps (values are the broker's own)."""
    broker.submit(symbol=symbol, side=side, order_type="market", quantity=qty, strategy=strategy,
                  timeframe=timeframe)
    broker.process_candle(symbol, _bar(entry))
    close_side = "sell" if side == "buy" else "buy"
    for part, price in exits:
        broker.submit(symbol=symbol, side=close_side, order_type="market", quantity=part, reduce_only=True)
        broker.process_candle(symbol, _bar(price))
    fills = sorted(broker.export_state()["fills"], key=lambda f: f["timestamp"])
    if timestamps:
        for f, ts in zip(fills, timestamps):
            f["timestamp"] = f["fill_timestamp"] = ts
    return fills


def _history(fills, *, funding=(), currency="USDT", meta=None):
    return {"currency": currency, "sessions": [{"session_id": "s1", "symbol": "BTCUSDT", "timeframe": "5m",
                                                "fills": fills, "funding": list(funding),
                                                "order_meta": meta or {}}]}


# ---------------------------------------------------------- sign convention
@pytest.mark.parametrize("side, entry, exit_, expected", [
    ("long", 100, 150, "50.00000000"), ("long", 100, 50, "-50.00000000"),
    ("short", 150, 100, "50.00000000"), ("short", 100, 150, "-50.00000000"),
    ("long", 100, 100, "0.00000000"),
])
def test_sign_follows_profit_never_direction(tmp_path, side, entry, exit_, expected):
    ledger, engine = _engine(tmp_path)
    _round_trip(engine, "BTCUSDT", side, entry, exit_)
    rows, _ = collect_ledger(ledger.get_paper_trades(), scope="ledger", default_source="paper_trading")
    assert len(rows) == 1 and cal.amount(rows[0].net) == expected and rows[0].side == side
    today = rows[0].closed_at.date()
    state = month_view(rows, year=today.year, month=today.month, tz=UTC)["summary"]["USDT"]["state"]
    assert state == {"5": "profit", "-": "loss", "0": "breakeven"}[expected[0]]


def test_ledger_pnl_is_already_net_so_fees_are_not_charged_twice(tmp_path):
    ledger, engine = _engine(tmp_path, fee_pct=0.001)
    _round_trip(engine, "BTCUSDT", "long", 100, 110, size=10)   # gross 100, fee 0.001*10*(100+110) = 2.1
    [r], _ = collect_ledger(ledger.get_paper_trades(), scope="ledger", default_source="paper_trading")
    stored = Decimal(str(ledger.get_paper_trades()[0]["pnl"]))
    assert r.net == stored and cal.amount(r.net) == "97.90000000"
    assert cal.amount(r.fees) == "2.10000000" and r.gross == r.net + r.fees == Decimal("100.0")
    assert r.pnl_basis == "ledger_net"


# ---------------------------------------------------------------- currency
def test_currency_is_the_settlement_asset_and_never_combined():
    rows = [_r("a", "100", "2026-09-10T10:00:00+00:00", currency="USDT"),
            _r("b", "50", "2026-09-10T11:00:00+00:00", currency="GBP")]
    m = month_view(rows, year=2026, month=9, tz=UTC)
    assert m["currencies"] == ["GBP", "USDT"]
    assert m["summary"]["USDT"]["net"] == "100.00000000" and m["summary"]["GBP"]["net"] == "50.00000000"
    day = next(d for d in m["days"] if d["date"] == "2026-09-10")
    assert set(day["by_currency"]) == {"GBP", "USDT"} and "150" not in str(day)


@pytest.mark.parametrize("symbol, currency", [("BTCUSDT", "USDT"), ("ETH/USDT", "USDT"), ("EURGBP", "GBP"),
                                              ("BTCFDUSD", "FDUSD"), ("XBTUSD", "USD"), ("BTCUSD", "USD"),
                                              ("BTCTUSD", "TUSD"), ("BTC-USDT-PERP", "USDT"), ("ODD", "UNKNOWN")])
def test_quote_currency_is_read_from_the_symbol(symbol, currency):
    assert cal.quote_currency(symbol) == currency


def test_decimal_precision_has_no_float_drift():
    rows = [_r(f"x{i}", v, "2026-09-10T10:00:00+00:00") for i, v in enumerate(["0.1", "0.2"])]
    assert month_view(rows, year=2026, month=9, tz=UTC)["summary"]["USDT"]["net"] == "0.30000000"


def test_gross_loss_is_a_magnitude_and_net_is_signed():
    rows = [_r("a", "100", "2026-09-10T10:00:00+00:00"), _r("b", "-40", "2026-09-10T11:00:00+00:00"),
            _r("c", "20", "2026-09-10T12:00:00+00:00")]
    s = month_view(rows, year=2026, month=9, tz=UTC)["summary"]["USDT"]
    assert (s["gross_profit"], s["gross_loss"], s["net"]) == ("120.00000000", "40.00000000", "80.00000000")


# -------------------------------------------------------- lab fills (V2)
def test_lab_fills_net_out_entry_and_exit_fees_from_gross(tmp_path):
    broker = _broker(tmp_path, fee_rate=0.001)
    fills = _trade_fills(broker, "BTCUSDT", "buy", 1, 100, [(1, 110)])
    [r], info = collect_v2_lab(_history(fills), source="pa_lab")
    # gross 10; fees 0.1 (entry) + 0.11 (exit)
    assert cal.amount(r.gross) == "10.00000000" and cal.amount(r.fees) == "0.21000000"
    assert cal.amount(r.net) == "9.79000000" and r.final and info["open_positions"] == 0
    account = broker.account()
    assert Decimal(str(account["balance"])) - Decimal("100000") == r.net   # agrees with the broker


@pytest.mark.parametrize("amount_, expected", [("2.5", "7.50000000"), ("-1.5", "11.50000000")])
def test_funding_debit_lowers_and_credit_raises_net(tmp_path, amount_, expected):
    fills = _trade_fills(_broker(tmp_path), "BTCUSDT", "buy", 1, 100, [(1, 110)],
                         timestamps=["2026-09-10T08:00:00+00:00", "2026-09-10T20:00:00+00:00"])
    funding = [{"symbol": "BTCUSDT", "funding_time": "2026-09-10T16:00:00+00:00", "amount": amount_, "applied": 1}]
    [r], _ = collect_v2_lab(_history(fills, funding=funding), source="smc_lab")
    assert cal.amount(r.net) == expected and cal.amount(r.funding) == cal.amount(Decimal(amount_))


def test_partial_exits_land_on_their_own_days_and_count_as_one_trade(tmp_path):
    fills = _trade_fills(_broker(tmp_path), "BTCUSDT", "buy", 2, 100, [(1, 130), (1, 150)],
                         timestamps=["2026-09-23T10:00:00+00:00", "2026-09-24T10:00:00+00:00",
                                     "2026-09-25T10:00:00+00:00"])
    rows, _ = collect_v2_lab(_history(fills), source="pa_lab")
    assert [(r.closed_at.date().day, cal.amount(r.net), r.final) for r in rows] == [
        (24, "30.00000000", False), (25, "50.00000000", True)]
    m = month_view(rows, year=2026, month=9, tz=UTC)
    d24, d25 = (next(d for d in m["days"] if d["date"] == f"2026-09-{n}") for n in (24, 25))
    assert d24["realizations"] == 1 and d24["closed_trades"] == 0
    assert d25["closed_trades"] == 1 and d25["by_currency"]["USDT"]["wins"] == 1
    assert m["summary"]["USDT"]["closed_trades"] == 1 and m["summary"]["USDT"]["net"] == "80.00000000"


def test_open_lab_position_realizes_nothing(tmp_path):
    broker = _broker(tmp_path)
    broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1)
    broker.process_candle("BTCUSDT", _bar(100))
    rows, info = collect_v2_lab(_history(broker.export_state()["fills"]), source="pa_lab")
    assert rows == [] and info["open_positions"] == 1


def test_lab_attribution_comes_from_order_metadata(tmp_path):
    fills = _trade_fills(_broker(tmp_path), "BTCUSDT", "sell", 1, 100, [(1, 90)], strategy="PA1_SR_REJECTION",
                         timeframe="15m")
    [r], _ = collect_v2_lab(_history(fills), source="pa_lab")
    assert (r.source, r.strategy, r.timeframe, r.side, cal.amount(r.net)) == (
        "pa_lab", "PA1_SR_REJECTION", "15m", "short", "10.00000000")
    meta = {fills[0]["order_id"]: {"model_id": "SMC_M1_SWEEP_REVERSAL", "model_version": "1.0"}}
    [s], _ = collect_v2_lab(_history(fills, meta=meta), source="smc_lab")
    assert s.source == "smc_lab" and s.strategy == "SMC_M1_SWEEP_REVERSAL 1.0"


# ------------------------------------------------------- de-duplication
def test_the_same_fill_in_the_live_broker_and_a_snapshot_counts_once(tmp_path):
    fills = _trade_fills(_broker(tmp_path), "BTCUSDT", "buy", 1, 100, [(1, 105)])
    history = _history(fills)
    history["sessions"].append({**history["sessions"][0], "session_id": "s1-snapshot"})
    calendar = PnlCalendar({"pa_lab": lambda: collect_v2_lab(history, source="pa_lab")})
    rows, diagnostics = calendar.collect()
    assert len(rows) == 1 and diagnostics["duplicates_dropped"] == 1


def test_the_same_ledger_row_from_two_collectors_counts_once():
    row = _row("t1", 10, "2026-09-10T10:00:00+00:00")
    collect = lambda: collect_ledger([row], scope="ledger", default_source="paper_trading")  # noqa: E731
    rows, diagnostics = PnlCalendar({"a": collect, "b": collect}).collect()
    assert len(rows) == 1 and diagnostics["duplicates_dropped"] == 1


# --------------------------------------------------- realized vs not
def test_only_closed_paper_or_live_rows_count():
    rows, info = collect_ledger([
        _row("a", 10, "2026-09-10T10:00:00+00:00"),
        _row("b", 99, None, status="open"),
        _row("c", 99, "2026-09-10T10:00:00+00:00", source="backtest"),
        _row("d", 5, "2026-09-10T10:00:00+00:00", source="live"),
    ], scope="ledger", default_source="paper_trading")
    assert sorted(r.id for r in rows) == ["ledger:a", "ledger:d"]
    assert info["open_positions"] == 1 and info["skipped"] == {"not_trading:backtest": 1}


# ------------------------------------------------------ attribution
def test_instances_are_named_from_the_record_and_legacy_gaps_stay_empty():
    instances = {"i1": {"name": "Supply & Demand · BTCUSDT", "strategy_key": "sd", "strategy_label": "Supply & Demand"}}
    rows, _ = collect_ledger([
        _row("a", 10, "2026-09-10T10:00:00+00:00", instance_id="i1", strategy_id="sd:1.0"),
        _row("b", 10, "2026-09-10T10:00:00+00:00", instance_id="gone", strategy_id="x:1"),
        _row("c", 10, "2026-09-10T10:00:00+00:00"),
    ], scope="ledger", default_source="paper_trading", instances=instances,
        journal=lambda tid: {"timeframe": "15m", "sections": {"exit_decision": {"exit_reason": "target"}}}
        if tid == "a" else None)
    a, b, c = sorted(rows, key=lambda r: r.id)
    assert (a.source, a.instance_id, a.instance_name, a.strategy, a.timeframe, a.exit_reason) == (
        "trading_instance", "i1", "Supply & Demand · BTCUSDT", "Supply & Demand", "15m", "target")
    assert (b.instance_name, b.strategy) == ("Deleted instance", "x:1")
    assert c.source == "paper_trading" and c.strategy is None and {"strategy", "timeframe", "exit_reason"} <= set(c.missing)


# ------------------------------------------------------------ dates
def test_pnl_belongs_to_the_close_date_not_the_entry_date():
    [r], _ = collect_ledger([_row("a", 50, "2026-09-24T02:15:00+00:00", opened_at="2026-09-23T22:30:00+00:00")],
                            scope="ledger", default_source="paper_trading")
    m = month_view([r], year=2026, month=9, tz=UTC)
    assert next(d for d in m["days"] if d["date"] == "2026-09-24")["realizations"] == 1
    assert next(d for d in m["days"] if d["date"] == "2026-09-23")["state"] == "none"


def test_the_calendar_timezone_decides_the_day():
    r = _r("a", "10", "2026-09-24T23:30:00+00:00")           # 00:30 on the 25th in London (BST)
    assert month_view([r], year=2026, month=9, tz=UTC)["days"][23]["realizations"] == 1
    assert month_view([r], year=2026, month=9, tz=LONDON)["days"][24]["realizations"] == 1


def test_midnight_boundaries():
    rows = [_r("a", "1", "2026-09-10T23:59:59+00:00"), _r("b", "2", "2026-09-11T00:00:00+00:00")]
    days = {d["date"]: d for d in month_view(rows, year=2026, month=9, tz=UTC)["days"]}
    assert days["2026-09-10"]["by_currency"]["USDT"]["net"] == "1.00000000"
    assert days["2026-09-11"]["by_currency"]["USDT"]["net"] == "2.00000000"


def test_no_trade_days_are_empty_not_zero():
    days = month_view([], year=2026, month=2, tz=UTC)["days"]
    assert len(days) == 28 and all(d["state"] == "none" and d["by_currency"] == {} for d in days)


def test_bad_month_and_zone_are_refused():
    with pytest.raises(CalendarError):
        month_view([], year=2026, month=13, tz=UTC)
    with pytest.raises(CalendarError):
        resolve_zone("Mars/Base")


# ------------------------------------------------------- drawdown
def test_daily_drawdown_is_peak_to_trough_not_the_largest_loss():
    rows = [_r("a", "100", "2026-09-10T09:00:00+00:00"), _r("b", "-60", "2026-09-10T10:00:00+00:00"),
            _r("c", "-70", "2026-09-10T11:00:00+00:00"), _r("d", "20", "2026-09-10T12:00:00+00:00")]
    s = day_view(rows, day=date(2026, 9, 10), tz=UTC)["summary"]["USDT"]
    assert s["max_drawdown"] == "130.00000000" and s["net"] == "-10.00000000"


def test_drawdown_counts_from_the_start_of_day_level():
    rows = [_r("a", "-30", "2026-09-10T09:00:00+00:00"), _r("b", "10", "2026-09-10T10:00:00+00:00")]
    assert day_view(rows, day=date(2026, 9, 10), tz=UTC)["summary"]["USDT"]["max_drawdown"] == "30.00000000"


# -------------------------------------------------- day detail
def test_day_detail_breaks_down_by_source_strategy_and_time_of_day():
    rows = [_r("a", "62", "2026-09-18T08:00:00+00:00", source="smc_lab", strategy="SMC"),
            _r("b", "-14", "2026-09-18T13:00:00+00:00", source="pa_lab", strategy="S/R Rejection"),
            _r("c", "57", "2026-09-18T23:30:00+00:00", source="trading_instance", strategy="Supply & Demand",
               instance_id="i3", instance_name="Supply & Demand · BTCUSDT"),
            _r("d", "5", "2026-09-18T02:00:00+00:00", source="smc_lab", strategy="SMC")]
    d = day_view(rows, day=date(2026, 9, 18), tz=UTC)
    assert d["summary"]["USDT"]["net"] == "110.00000000" and d["summary"]["USDT"]["closed_trades"] == 4
    sources = {tuple(s["key"][:2]): s["by_currency"]["USDT"]["net"] for s in d["sources"]}
    assert sources[("smc_lab", "SMC Lab")] == "67.00000000"
    assert sources[("trading_instance", "Trading Instance")] == "57.00000000"
    buckets = {b["key"]: b["by_currency"].get("USDT", {}).get("net") for b in d["time_of_day"]}
    assert buckets == {"morning": "62.00000000", "afternoon": "-14.00000000", "evening": None,
                       "night": "62.00000000"}
    assert [t["id"] for t in d["trades"]] == ["d", "a", "b", "c"]            # chronological
    assert d["trades"][2]["outcome"] == "loss" and d["trades"][2]["source_label"] == "Price Action Lab"


@pytest.mark.parametrize("hour, bucket", [(6, "night"), (7, "morning"), (11, "morning"), (12, "afternoon"),
                                          (16, "afternoon"), (17, "evening"), (21, "evening"), (22, "night"),
                                          (0, "night")])
def test_time_of_day_buckets_are_defined_once(hour, bucket):
    assert time_bucket(hour) == bucket


# --------------------------------------------------------- filters
def test_filters_narrow_on_real_fields_and_reject_unknown_sources():
    rows = [_r("a", "1", "2026-09-10T10:00:00+00:00", source="smc_lab", symbol="BTCUSDT", strategy="SMC",
               timeframe="5m"),
            _r("b", "2", "2026-09-10T10:00:00+00:00", source="trading_instance", symbol="ETHUSDT",
               instance_id="i1", strategy="Donchian", timeframe="1h")]
    pick = lambda f: sorted(r.id for r in rows if f.match(r))  # noqa: E731
    assert pick(Filters(source="smc_lab")) == ["a"]
    assert pick(Filters(instance_id="i1")) == ["b"]
    assert pick(Filters(strategy="SMC")) == ["a"]
    assert pick(Filters(symbol="ethusdt")) == ["b"]
    assert pick(Filters(timeframe="5m")) == ["a"]
    with pytest.raises(CalendarError):
        Filters(source="made_up")


# -------------------------------------------------------------- API
def test_the_api_resolves_timezone_and_reports_unconverted_currencies(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    import webhook_api
    rows = [_r("a", "84.2", "2026-09-15T10:00:00+00:00"), _r("b", "32.1", "2026-09-15T11:00:00+00:00", currency="GBP")]
    monkeypatch.setattr(webhook_api, "pnl_calendar", PnlCalendar({"t": lambda: (rows, {})}))
    c = TestClient(hub_app.app)
    h = {"x-webhook-secret": "dev-control-key"}
    m = c.get("/calendar/month?year=2026&month=9&tz=Europe/London", headers=h).json()
    assert m["timezone"] == "Europe/London" and m["summary"]["USDT"]["net"] == "84.20000000"
    assert m["conversion"]["needed"] and m["conversion"]["unconverted"] == ["GBP"] and not m["conversion"]["available"]
    d = c.get("/calendar/day?date_=2026-09-15&source=paper_trading", headers=h).json()
    assert d["summary"]["USDT"]["closed_trades"] == 1 and d["filters"] == {"source": "paper_trading"}
    assert c.get("/calendar/month?year=2026&month=9&tz=Nowhere/Zone", headers=h).status_code == 400
    assert c.get("/calendar/day?date_=15-09-2026", headers=h).status_code == 400
    assert c.get("/calendar/month?year=2026&month=9").status_code in (401, 403)


def test_a_supabase_ledger_is_read_past_the_server_row_cap():
    """PostgREST caps a select at max-rows (1,000 by default); the calendar
    must page through, or the oldest trades would silently vanish."""
    from data.ledger import SupabaseLedger
    stored = [{"id": f"t{i:05d}"} for i in range(2345)]

    class Query:
        def __init__(self):
            self.lo = self.hi = None

        def select(self, _cols):
            return self

        def order(self, _col):
            return self

        def range(self, lo, hi):
            self.lo, self.hi = lo, min(hi, lo + 999)   # the server cap wins
            return self

        def execute(self):
            return type("R", (), {"data": stored[self.lo:self.hi + 1]})()

    led = object.__new__(SupabaseLedger)
    led._db = type("DB", (), {"table": lambda self, name: Query()})()
    rows = cal.read_paper_trades(led, page=5000)
    assert [r["id"] for r in rows] == [r["id"] for r in stored]


def test_a_degraded_ledger_is_an_error_not_an_empty_history():
    from data.ledger import ReadOnlyDegradedLedger
    calendar = PnlCalendar({"ledger": lambda: collect_ledger(
        cal.read_paper_trades(ReadOnlyDegradedLedger("supabase down")),
        scope="ledger", default_source="paper_trading")})
    _rows, diagnostics = calendar.collect()
    assert diagnostics["sources"]["ledger"]["ok"] is False
    assert "supabase down" in diagnostics["sources"]["ledger"]["error"]
