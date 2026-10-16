"""A paper trade must record which strategy produced it.

`paper.strategy_id` was documented as "set when a rule spec is deployed" and
was never assigned by anything, so every trade was written unattributed. The
running ledger holds 2,804 closed trades and not one names a strategy, which
left every per-strategy statistic either blank or, worse, silently computed
over the pooled account -- a monitor once reported a 36.75R drawdown against a
4.3R backtest for a strategy that had taken one trade in a year.

Attribution is per CALL, not per engine: one engine serves the deployed spec,
built-in strategies and inbound webhook alerts, and a single mutable field
would stamp whatever was deployed last onto all of them.
"""
import pytest

from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine


def _engine():
    return PaperExecutionEngine(SqliteLedger(":memory:"), 10_000)


def _open(paper, **kw):
    kw.setdefault("symbol", "BTCUSDT")
    kw.setdefault("side", "BUY")
    kw.setdefault("size", 1)
    kw.setdefault("entry", 100)
    kw.setdefault("stop", 90)
    kw.setdefault("target", 120)
    return paper.open(**kw)


def test_a_trade_records_the_strategy_that_produced_it():
    paper = _engine()
    _open(paper, alert_id="a1", strategy_id="s1")
    assert paper.history() == [] or True          # open, not yet closed
    row = paper.ledger.get_paper_trades()[0]
    assert row["strategy_id"] == "s1"


def test_scoped_history_finds_the_trade_once_it_closes():
    """The end the monitor actually reads."""
    paper = _engine()
    _open(paper, alert_id="a1", strategy_id="s1")
    paper.close(symbol="BTCUSDT", exit_price=110)
    assert [t["strategy_id"] for t in paper.strategy_history("s1")] == ["s1"]
    assert paper.strategy_history("other") == []


def test_no_strategy_id_stays_unattributed():
    """An inbound webhook alert belongs to no deployed spec. Borrowing one
    would credit a strategy with a record it did not produce -- the failure
    this whole change exists to prevent."""
    paper = _engine()
    _open(paper, alert_id="a1")
    assert paper.ledger.get_paper_trades()[0]["strategy_id"] in ("", None)
    assert paper.strategy_history("s1") == []


def test_the_caller_decides_not_a_stray_sizing_context_key():
    """strategy_id used to sit BEFORE **sizing_context in the row literal, so
    any key that rode along in that dict silently decided attribution."""
    paper = _engine()
    _open(paper, alert_id="a1", strategy_id="real",
          sizing_context={"strategy_id": "smuggled", "risk_pct_at_entry": 0.01})
    assert paper.ledger.get_paper_trades()[0]["strategy_id"] == "real"


def test_a_partial_exit_belongs_to_whoever_opened_it():
    """Reading the engine at reduce time would re-attribute a half-closed
    trade to a strategy deployed after the entry."""
    paper = _engine()
    _open(paper, alert_id="a1", size=2, strategy_id="opener")
    paper.strategy_id = "deployed-later"
    paper.reduce(symbol="BTCUSDT", exit_price=110, fraction=0.5)
    ids = {t["strategy_id"] for t in paper.ledger.get_paper_trades()}
    assert ids == {"opener"}, f"remainder was re-attributed: {ids}"


def test_the_engine_default_still_applies_when_no_caller_supplies_one():
    """The documented fallback: an engine dedicated to one strategy may set
    the field, and a call that names nobody inherits it."""
    paper = _engine()
    paper.strategy_id = "engine-owned"
    _open(paper, alert_id="a1")
    assert paper.ledger.get_paper_trades()[0]["strategy_id"] == "engine-owned"


# ─────────────── forward paper: the id must survive the intent queue ───────────────
#
# FORWARD_PAPER does not fill at decision time. open() records an intent and a
# later real quote fills it, so an id held only in a local variable is lost
# between the decision and the trade -- and intents are checkpointed, so it has
# to survive a restart too. This is the mode the running instances use.

def _quote(**kw):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return {"symbol": "BTCUSDT", "last": 100.0, "bid": 99.9, "ask": 100.1,
            "mark": 100.0, "sequence": 1, "received_at": now,
            "event_timestamp": now, "quote_event_id": "q1",
            "candle_id": "BINANCE_USDM:BTCUSDT:5m:1", **kw}


def _forward(tmp_path, listener=None):
    from execution.paper_engine import ForwardPaperExecutionEngine
    from services.trading_instances import InstanceLedger
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    return ForwardPaperExecutionEngine(InstanceLedger(ledger, "inst-1"), 10_000,
                                       intents_listener=listener)


def test_the_owner_survives_the_intent_queue_to_the_fill(tmp_path):
    from datetime import datetime, timezone
    paper = _forward(tmp_path)
    paper.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100.0, stop=95.0,
               alert_id="fwd-1", strategy_id="s1",
               sizing_context={"decision_timestamp":
                               datetime.now(timezone.utc).isoformat()})
    assert paper.positions() == [], "forward paper must not fill at decision time"
    paper.process_quote(_quote())
    rows = paper.ledger.get_paper_trades()
    assert rows and rows[0]["strategy_id"] == "s1"


def test_the_owner_is_checkpointed_with_the_intent(tmp_path):
    """Intents outlive the process. An id kept only in memory would be lost on
    the restart, and the trade would fill unattributed."""
    from datetime import datetime, timezone
    saved: dict = {}
    paper = _forward(tmp_path, listener=saved.update)
    paper.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100.0, stop=95.0,
               alert_id="fwd-1", strategy_id="s1",
               sizing_context={"decision_timestamp":
                               datetime.now(timezone.utc).isoformat()})
    intent = saved["BTCUSDT"]
    assert (intent.get("sizing_context") or {}).get("strategy_id") == "s1"


def test_a_forward_intent_with_no_owner_fills_unattributed(tmp_path):
    from datetime import datetime, timezone
    paper = _forward(tmp_path)
    paper.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100.0, stop=95.0,
               alert_id="fwd-1",
               sizing_context={"decision_timestamp":
                               datetime.now(timezone.utc).isoformat()})
    paper.process_quote(_quote())
    rows = paper.ledger.get_paper_trades()
    assert rows and rows[0]["strategy_id"] in ("", None)


# ───────────── end to end: the payload's owner reaches the ledger ─────────────

def _pipeline():
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    return SignalPipeline(ledger, paper, TradingControl(), equity=10_000), paper


def test_a_signal_carrying_a_strategy_id_produces_an_attributed_trade():
    pipeline, paper = _pipeline()
    res = pipeline.process({"alert_id": "e2e-1", "symbol": "BTCUSDT", "side": "BUY",
                            "entry": 100.0, "stop": 95.0, "strategy_id": "s1"})
    assert res.accepted, f"signal was not accepted: {res.stage} {res.reason}"
    assert paper.ledger.get_paper_trades()[0]["strategy_id"] == "s1"


def test_a_webhook_alert_naming_no_strategy_stays_unattributed():
    """An inbound TradingView alert belongs to no deployed spec. It must not
    inherit whichever strategy happens to be deployed."""
    pipeline, paper = _pipeline()
    paper.strategy_id = ""
    res = pipeline.process({"alert_id": "e2e-2", "symbol": "BTCUSDT", "side": "BUY",
                            "entry": 100.0, "stop": 95.0})
    assert res.accepted, f"signal was not accepted: {res.stage} {res.reason}"
    assert paper.ledger.get_paper_trades()[0]["strategy_id"] in ("", None)


# ───────────────── which strategy the engine says owns a trade ─────────────────

def _auto_engine():
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 1000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=1000)
    return AutoStrategyEngine(pipeline, paper, ledger, symbols=["BTCUSDT"],
                              timeframe="5m", live=False,
                              fetcher=lambda *_: ([], "test"))


def test_a_deployed_rule_spec_owns_its_trades_by_spec_id():
    """The id the monitor scopes by, so the two agree by construction."""
    eng = _auto_engine()
    eng.deployed_spec = {"id": "s1", "name": "EMA Trend"}
    assert eng.owning_strategy_id() == "s1"


def test_an_instance_strategy_owns_its_trades_by_strategy_key():
    """Instances run built-in strategies that have no rule spec. Without this
    the fix would attribute nothing in the mode the instances actually run."""
    eng = _auto_engine()
    eng.strategy_key = "pa_rulebook_v01"
    assert eng.owning_strategy_id() == "pa_rulebook_v01"


def test_the_spec_id_wins_when_both_are_present():
    eng = _auto_engine()
    eng.strategy_key = "builtin"
    eng.deployed_spec = {"id": "s1"}
    assert eng.owning_strategy_id() == "s1"


def test_with_neither_the_trade_stays_unowned():
    """Not a fallback to the display label: it is not unique, and a wrong
    owner is worse than none."""
    eng = _auto_engine()
    eng.strategy_label = "Some Strategy"
    assert eng.owning_strategy_id() == ""
