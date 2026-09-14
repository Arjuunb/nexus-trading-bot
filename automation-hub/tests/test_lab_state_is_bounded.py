"""The lab ``state()`` reads that took a production container down.

Two research lab sessions were left open for ten and twenty days. Each call to
``state()`` re-read and re-materialised every candidate, order-metadata and
funding row of the session, and it runs on every lab tick and every
``bot-status`` poll. Measured on the box: ~500 MB read and ~500 MB written
every 30 seconds, a core pegged at 104%, and memory climbing 30 MB/s until the
11.4 GB ceiling. Ending both sessions took it to flat memory and 1-7% CPU.

These tests fail if any of those reads becomes unbounded again, if a total
starts being derived from the length of a capped window, or if the SMC metrics
summary goes back to calling ``state()`` once per completed trade.
"""
from __future__ import annotations

import inspect
import re

from services import price_action_lab, smc_strategy_lab


def _state_source(module) -> str:
    """The body of that module's ``state`` method."""
    account = next(
        obj for _, obj in vars(module).items()
        if inspect.isclass(obj) and obj.__module__ == module.__name__
        and "state" in vars(obj) and callable(vars(obj)["state"])
        and "collection_totals" in inspect.getsource(vars(obj)["state"]))
    return inspect.getsource(vars(account)["state"])


def test_every_session_scoped_read_in_state_is_bounded():
    for module, tables in (
        (price_action_lab, ("pa_candidates", "pa_order_meta", "pa_funding_events",
                            "pa_activity", "pa_evaluations", "pa_position_remediations")),
        (smc_strategy_lab, ("smc_order_meta", "smc_candidates", "smc_funding_events",
                            "smc_activity", "smc_evaluations")),
    ):
        source = _state_source(module)
        for table in tables:
            # Find the SELECT for this table and confirm the statement it
            # builds cannot run without a row bound -- either a literal LIMIT
            # or the `window` suffix, which is "" only when a caller has
            # explicitly asked for the whole collection.
            match = re.search(
                r'"SELECT \* FROM ' + table + r'\b[^"]*"(?:\s*\+\s*(\w+))?',
                source)
            assert match, f"{module.__name__}: no SELECT found for {table}"
            statement, suffix = match.group(0), match.group(1)
            assert "LIMIT" in statement or suffix == "window", (
                f"{module.__name__}.state() reads {table} with no row bound. "
                "An unbounded read here is what exhausted the container.")


def test_the_row_bound_is_on_by_default():
    """A caller that does not think about it must get the bounded read."""
    for module in (price_action_lab, smc_strategy_lab):
        source = _state_source(module)
        assert "limit: int | None = STATE_ROW_LIMIT" in source, (
            f"{module.__name__}.state() must default to a bounded read; "
            "defaulting to unbounded puts the leak one forgotten caller away.")
        assert isinstance(module.STATE_ROW_LIMIT, int)
        assert module.STATE_ROW_LIMIT > 0


def test_totals_come_from_a_count_not_from_the_window():
    """Capping a list must not silently cap a number derived from it."""
    for module in (price_action_lab, smc_strategy_lab):
        source = _state_source(module)
        assert "COUNT(*)" in source, (
            f"{module.__name__}.state() must report exact totals via COUNT(*), "
            "so a caller reading a capped collection still knows how many "
            "rows exist.")
        assert '"collection_totals"' in source


def test_smc_metrics_reads_state_once_not_once_per_trade():
    """This was three ``state()`` calls, the first inside a comprehension.

    ``target_1_hits`` evaluated ``self.state()["order_metadata"]`` for every
    completed trade, so an N-trade session performed N+2 whole-session reads
    each time the metrics were requested.
    """
    source = inspect.getsource(smc_strategy_lab)
    summary = source[source.index("target_1_hits = sum("):]
    summary = summary[:summary.index("fees_paid")]
    assert "self.state()" not in summary, (
        "The SMC metrics summary must take one snapshot and reuse it; "
        "calling state() inside the comprehension over `completed` made the "
        "cost quadratic in the number of trades.")
    assert "snapshot[" in summary


def test_orders_placed_is_the_true_count_not_the_window_length():
    source = inspect.getsource(smc_strategy_lab)
    assert 'len(self.state()["order_metadata"])' not in source, (
        "orders_placed must not be the length of a bounded window -- it would "
        "silently report STATE_ROW_LIMIT forever once a session grew past it.")
    assert '"orders_placed": snapshot["collection_totals"]["order_metadata"]' in source
