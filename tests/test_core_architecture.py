"""The dependency rule, enforced.

A layering convention that lives only in a README is a convention until the
first deadline. These tests fail the build instead.

The central one is `test_core_never_imports_infrastructure`: it walks every
module under `tradexa.core` and asserts none of them reaches for FastAPI,
sqlite3, ccxt, requests or any other I/O library — the whole point of the
layer. It reads the AST rather than the source text, so a name appearing in a
docstring or a comment (this file's own prose, for instance) cannot fail it and
a real import cannot hide from it.
"""
from __future__ import annotations

import ast
import pkgutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradexa.core import events as ev
from tradexa.core import exceptions as ex
from tradexa.core import interfaces as ifc
from tradexa.core import models as m

CORE = Path(ifc.__file__).resolve().parents[1]

# Infrastructure the domain must never reach for. Stated as top-level module
# names; a dotted import is matched on its root.
FORBIDDEN = {
    "fastapi", "starlette", "pydantic",
    "sqlite3", "sqlalchemy", "psycopg2", "asyncpg", "redis",
    "ccxt", "requests", "httpx", "urllib", "aiohttp", "websocket", "websockets",
    "streamlit", "flask", "django",
}


def _imports(path: Path) -> set[str]:
    """Top-level module names imported by ``path``, via AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import — stays inside the package
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def _core_modules() -> list[Path]:
    return sorted(p for p in CORE.rglob("*.py") if "__pycache__" not in p.parts)


def test_core_has_modules_to_check():
    """Guards the guard: an empty glob would make every test below vacuous."""
    assert len(_core_modules()) >= 4


@pytest.mark.parametrize("path", _core_modules(), ids=lambda p: p.stem)
def test_core_never_imports_infrastructure(path):
    leaked = _imports(path) & FORBIDDEN
    assert not leaked, (
        f"{path.relative_to(CORE.parent)} imports {sorted(leaked)}. Business "
        "logic must depend on an interface from tradexa.core.interfaces, and "
        "infrastructure must implement it from the outside."
    )


def test_core_does_not_import_the_web_or_persistence_layer():
    """`automation-hub` holds the FastAPI app and the SQLite stores. Core
    importing it would invert the dependency arrow regardless of which
    third-party libraries appear."""
    for path in _core_modules():
        assert "webhook_api" not in _imports(path)
        assert "app" not in _imports(path)


# ── models ──────────────────────────────────────────────────────────────────

def test_canonical_types_are_reexported_not_redefined():
    """The single most important property of the models package: it must be the
    SAME Order the live engine and brokers use, not a lookalike. Two types with
    one name and no conversion is worse than no package at all."""
    import bot.types as canonical
    for name in ("Bar", "Signal", "Order", "Position", "Fill", "AccountSnapshot",
                 "Side", "OrderType", "SignalType"):
        assert getattr(m, name) is getattr(canonical, name), f"{name} was forked"


def test_added_models_are_immutable():
    """Domain facts are statements about a moment. A subscriber holding one off
    the event bus must not be able to rewrite it for everyone else."""
    t = m.Trade(symbol="BTCUSDT", side=m.Side.BUY, qty=1.0, entry_price=100.0,
                exit_price=110.0, opened_at=datetime.now(timezone.utc),
                closed_at=datetime.now(timezone.utc), pnl=10.0)
    with pytest.raises(Exception):
        t.pnl = 999.0            # type: ignore[misc]


def test_trade_classifies_its_own_outcome():
    now = datetime.now(timezone.utc)
    def trade(pnl):
        return m.Trade(symbol="X", side=m.Side.BUY, qty=1, entry_price=1,
                       exit_price=1, opened_at=now, closed_at=now, pnl=pnl)
    assert trade(5).outcome is m.TradeOutcome.WIN
    assert trade(-5).outcome is m.TradeOutcome.LOSS
    assert trade(0).outcome is m.TradeOutcome.BREAK_EVEN


def test_trade_duration_is_derived_from_its_timestamps():
    start = datetime.now(timezone.utc)
    t = m.Trade(symbol="X", side=m.Side.BUY, qty=1, entry_price=1, exit_price=1,
                opened_at=start, closed_at=start + timedelta(minutes=30), pnl=0)
    assert t.duration_s == 1800


def test_risk_assessment_carries_the_size_it_approved():
    """Approval and sizing are one decision. Splitting them is how the executed
    size drifts from the size risk actually sanctioned."""
    ok = m.RiskAssessment.allow(0.25, risk_amount=100.0)
    assert ok.approved and ok.approved_qty == 0.25 and ok.risk_amount == 100.0


def test_a_rejection_names_the_rule_that_caused_it():
    r = m.RiskAssessment.reject("daily_loss_cap", "3% daily loss reached")
    assert r.approved is False and r.approved_qty == 0.0
    assert r.rule == "daily_loss_cap" and "3%" in r.reason


def test_gross_exposure_counts_shorts_as_risk():
    """A hedged book nets to zero and is still fully exposed. Netting here
    would report a margin-callable position as flat."""
    snap = m.PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc),
        account=m.AccountSnapshot(cash=1000.0, equity=1000.0),
        positions=(m.Position("BTCUSDT", 1.0, 100.0),
                   m.Position("ETHUSDT", -2.0, 50.0)))
    assert snap.gross_exposure == 200.0
    assert snap.exposure_pct() == pytest.approx(0.2)


def test_exposure_is_unknown_rather_than_infinite_on_a_blown_account():
    snap = m.PortfolioSnapshot(timestamp=datetime.now(timezone.utc),
                               account=m.AccountSnapshot(cash=0.0, equity=0.0),
                               positions=(m.Position("BTCUSDT", 1.0, 100.0),))
    assert snap.exposure_pct() is None


def test_unrealized_pnl_sums_the_open_positions():
    snap = m.PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc),
        account=m.AccountSnapshot(cash=0.0, equity=100.0),
        positions=(m.Position("A", 1, 10, unrealized_pnl=5.0),
                   m.Position("B", 1, 10, unrealized_pnl=-2.0)))
    assert snap.unrealized_pnl == 3.0


def test_strategy_result_can_say_no_and_say_why():
    r = m.StrategyResult(strategy_id="ema", symbol="BTCUSDT",
                         timestamp=datetime.now(timezone.utc),
                         reason="trend filter: htf is down")
    assert r.has_signal is False and "htf" in r.reason


def test_execution_report_ok_covers_partial_fills():
    """A partial fill is a position that exists. Treating it as failure is how
    live inventory goes untracked."""
    order = m.Order(symbol="BTCUSDT", side=m.Side.BUY, qty=1.0)
    def report(status):
        return m.ExecutionReport(status=status, order=order)
    assert report(m.ExecutionStatus.FILLED).ok
    assert report(m.ExecutionStatus.PARTIAL).ok
    assert report(m.ExecutionStatus.ACCEPTED).ok
    assert not report(m.ExecutionStatus.REJECTED).ok
    assert not report(m.ExecutionStatus.ERROR).ok


def test_candle_binds_a_bar_to_its_market_and_timeframe():
    """An anonymous Bar is what lets a 4h candle be treated as 1h somewhere
    downstream."""
    bar = m.Bar(datetime.now(timezone.utc), 1, 2, 0.5, 1.5, 10)
    c = m.Candle(symbol="BTCUSDT", timeframe="4h", bar=bar)
    assert c.symbol == "BTCUSDT" and c.timeframe == "4h" and c.close == 1.5


# ── exceptions ──────────────────────────────────────────────────────────────

def test_every_exception_descends_from_one_root():
    """So `except TradexaError` is complete, without also catching
    KeyboardInterrupt or a genuine AttributeError."""
    for name in ex.__all__:
        obj = getattr(ex, name)
        assert issubclass(obj, ex.TradexaError), name


def test_retryable_is_visible_from_the_type():
    """Retry policy is the most consequential branch in a live loop."""
    assert ex.ExchangeConnectionError("timeout").retryable is True
    assert ex.ConfigurationError("bad key").retryable is False
    assert ex.OrderRejected("too small").retryable is False


def test_context_survives_into_the_message_and_the_log_form():
    e = ex.OrderRejected("venue refused", symbol="BTCUSDT", qty=0.001)
    assert "BTCUSDT" in str(e)
    d = e.as_dict()
    assert d["error"] == "OrderRejected" and d["symbol"] == "BTCUSDT"
    assert d["retryable"] is False


def test_a_risk_rejection_records_which_rule_fired():
    e = ex.RiskRejection("blocked", rule="max_open_positions", open=3)
    assert e.rule == "max_open_positions" and e.context["open"] == 3


def test_a_bare_exception_message_has_no_stray_punctuation():
    assert str(ex.StrategyError("could not evaluate")) == "could not evaluate"


# ── interfaces ──────────────────────────────────────────────────────────────

def test_the_live_broker_abc_already_satisfies_the_broker_port():
    """The point of using Protocols rather than ABCs: production classes
    conform without inheriting from, or importing, anything in tradexa."""
    from bot.brokers.base import Broker as LiveBroker
    for method in ("submit_order", "cancel_order", "get_account", "get_position"):
        assert hasattr(LiveBroker, method)


def test_the_live_strategy_abc_already_satisfies_the_strategy_port():
    from bot.strategies.base import Strategy as LiveStrategy
    assert hasattr(LiveStrategy, "on_bar")


def test_a_plain_object_satisfies_a_port_structurally():
    class Fake:
        def now(self):
            return datetime.now(timezone.utc)
    assert isinstance(Fake(), ifc.Clock)


def test_an_object_missing_a_method_does_not_satisfy_the_port():
    class NotAClock:
        pass
    assert not isinstance(NotAClock(), ifc.Clock)


def test_interfaces_are_stated_only_in_domain_terms():
    """An interface that mentions a ccxt client or a DB cursor has relocated
    the coupling rather than removed it.

    Reads the actual parameter and return ANNOTATIONS, not the file's text —
    prose describing what must not appear (including this module's own
    docstrings) is not a leak, and a text search cannot tell the difference.
    """
    tree = ast.parse(Path(ifc.__file__).read_text(encoding="utf-8"))
    banned = {"ccxt", "sqlite3", "Session", "Cursor", "Response", "APIRouter",
              "Request", "Connection"}
    annotations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [*node.args.args, *node.args.kwonlyargs]:
                if arg.annotation is not None:
                    annotations.append(ast.unparse(arg.annotation))
            if node.returns is not None:
                annotations.append(ast.unparse(node.returns))

    assert annotations, "no annotations found — the check would be vacuous"
    for text in annotations:
        names = set(ast.unparse(ast.parse(text)).replace("[", " ").replace("]", " ")
                    .replace(",", " ").replace(".", " ").split())
        leaked = names & banned
        assert not leaked, f"{sorted(leaked)} leaked into the signature `{text}`"


# ── events ──────────────────────────────────────────────────────────────────

def test_every_event_is_timestamped_on_construction():
    before = datetime.now(timezone.utc)
    e = ev.BotStarted(mode="paper")
    assert before <= e.occurred_at <= datetime.now(timezone.utc)


def test_events_are_immutable():
    """Several subscribers hold the same instance; one must not be able to
    edit what the others see."""
    e = ev.RiskBreached(rule="daily_loss", detail="hit")
    with pytest.raises(Exception):
        e.rule = "something_else"      # type: ignore[misc]


def test_every_event_descends_from_event_and_is_registered():
    for cls in ev.ALL_EVENTS:
        assert issubclass(cls, ev.Event)
    assert len(set(ev.ALL_EVENTS)) == len(ev.ALL_EVENTS), "duplicate registration"


def test_the_documented_vocabulary_is_all_present():
    """The names the brief asked for, so a rename cannot silently drop one."""
    required = {"MarketDataReceived", "SignalGenerated", "RiskValidated",
                "OrderSubmitted", "OrderFilled", "PositionOpened",
                "PositionClosed", "PortfolioUpdated", "BotStarted", "BotStopped"}
    assert required <= {c.__name__ for c in ev.ALL_EVENTS}


def test_event_name_is_its_type():
    assert ev.OrderFilled().name == "OrderFilled"


# ── packaging ───────────────────────────────────────────────────────────────

def test_core_subpackages_are_importable_standalone():
    """Catches a circular import between models, interfaces and events before
    it reaches a caller."""
    import tradexa.core
    for _, name, _ in pkgutil.walk_packages(tradexa.core.__path__,
                                            prefix="tradexa.core."):
        __import__(name)


#: Production modules that deliberately depend on `tradexa`, and why. Every
#: migration adds a line here as part of the change that makes it. The point is
#: not to forbid coupling — it is to make each instance a decision someone
#: recorded, so an accidental import is visible against a short, explained list
#: rather than lost in a growing one.
DELIBERATE_TRADEXA_CONSUMERS = {
    # Phase 2: publishes MarketDataReceived from _process_bar. Publish-only —
    # the engine never subscribes and never reads a result back, so no trading
    # control flow depends on it.
    "automation-hub/services/auto_engine.py",
    # Phase 3b: composes position-sizing factors via tradexa.risk.sizing. This
    # one IS load-bearing — it decides trade size — which is why it ships with
    # a differential test asserting bit-identical output against the expression
    # it replaced, and why the import carries a byte-identical fallback.
    "automation-hub/services/signal_pipeline.py",
    # Stage 3 sizing: one shared deterministic PositionSizingService now owns
    # paper/live and backtest quantity arithmetic. These adapters provide
    # instance state; they do not duplicate the sizing formula.
    "automation-hub/services/trading_instances.py",
    "automation-hub/risk/position_sizing.py",
    "bot/risk.py",
    # Existing core-engine bridge evaluates the standalone risk proposal; its
    # import is local to the adapter and keeps policy out of the engine shell.
    "automation-hub/core_engine/risk.py",
    # Phase 4: the adapter for tradexa.portfolio. Read-only and translation-only
    # — it assembles a Portfolio from the paper engine, ledger and broker
    # registry and computes nothing itself, which is what keeps portfolio
    # mathematics out of the execution engine. Adding a venue touches this file
    # and no arithmetic.
    "automation-hub/services/portfolio_view.py",
    # Guardian: the GET-only primary-ledger adapter translates bounded source
    # rows into a pure paper-pair observation. No trading control path consumes
    # its result, and Guardian never imports the trading ledger back.
    "automation-hub/services/guardian_instance_ledger_read_model.py",
    # Guardian's PA/SMC GET-only adapter shares pure bounded validation, not
    # the Guardian store or any trading instruction. Source files stay read-only.
    "automation-hub/services/guardian_lab_execution_read_model.py",
    # Retained fill export reuses validation/identity hashing only. The query-
    # only HTTP path never opens Guardian storage or invokes a trading runtime.
    "automation-hub/services/guardian_lab_fill_read_model.py",
    # Closed Agent journal repeat-scan export shares a pure projection/cursor
    # contract only. It does not import Guardian storage or trading authority.
    "automation-hub/services/guardian_smc_journal_read_model.py",
    # Retained intent-event GET export shares pure validation/hash helpers only.
    # Guardian persistence and broker/runtime authority remain separate.
    "automation-hub/services/guardian_smc_intent_read_model.py",
    # Fill-position source export reuses pure bounds/hash/redaction contracts;
    # source capture never imports Guardian and stays in the broker ledger.
    "automation-hub/services/guardian_smc_fill_transition_read_model.py",
    # Exit source GET shares only bounded validation/cursor contracts. No
    # Guardian store, journal or broker authority is invoked by this reader.
    "automation-hub/services/guardian_smc_exit_fill_read_model.py",
    # Existing Agent stop rows are exported through pure validation/hash
    # helpers only. No source writer, broker or Guardian store is constructed.
    "automation-hub/services/guardian_smc_stop_read_model.py",
    # Lab lifecycle GET export shares pure saved-settings hash/validation only.
    # Capture remains source-local SQL; neither writer imports Guardian, and
    # this query-only adapter constructs no Guardian store or trading runtime.
    "automation-hub/services/guardian_read_model.py",
    # Instance GET export validates bounded immutable applied settings with a
    # pure contract only; no Guardian store/runtime is reached by the writer.
    "automation-hub/services/guardian_instance_read_model.py",
}


def test_only_deliberately_migrated_modules_import_tradexa():
    """Guards against coupling nobody chose.

    Phase 1 was purely additive and this test forbade every import. Phase 2
    wires one seam on purpose, so the check became an allowlist — which is
    stricter in the way that matters: an unlisted import now fails the build,
    and a listed one carries its rationale next to it.
    """
    root = CORE.parents[1]
    offenders = []
    for area in ("bot", "trading_bot", "automation-hub"):
        base = root / area
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            # Tests are exempt: the rule is about PRODUCTION modules acquiring
            # a dependency. A test importing tradexa to verify a seam is the
            # intended use, and listing each one would bury the signal.
            if "__pycache__" in p.parts or "tests" in p.parts or p.name == "conftest.py":
                continue
            rel = p.relative_to(root).as_posix()
            if "tradexa" in _imports(p) and rel not in DELIBERATE_TRADEXA_CONSUMERS:
                offenders.append(rel)
    assert not offenders, (
        f"undeclared dependency on tradexa: {offenders}. If the import is "
        "intended, add it to DELIBERATE_TRADEXA_CONSUMERS with the reason.")


def test_the_allowlist_has_no_stale_entries():
    """A migration that gets reverted must not leave a permanent exemption
    behind — the next accidental import would then pass unnoticed."""
    root = CORE.parents[1]
    for rel in DELIBERATE_TRADEXA_CONSUMERS:
        path = root / rel
        assert path.exists(), f"allowlisted file no longer exists: {rel}"
        assert "tradexa" in _imports(path), (
            f"{rel} is allowlisted but no longer imports tradexa — remove the entry")
