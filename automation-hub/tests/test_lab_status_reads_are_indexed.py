"""Neither lab's status route may read a whole table to answer one poll.

A thread dump of the hung Price Action status route caught it inside state():

    price_action_lab.py:732  for row in self._db.execute(
      "SELECT * FROM pa_evaluations WHERE session_id=? ORDER BY candle_time DESC ...

state() asks six tables for one session's rows, newest first, and not one of
them carried an index. Every poll was six full table scans plus a sort of
everything that matched, and the dashboard polls this continuously. On a lab
with real history it ran past nginx's ninety-second ceiling and the lab
answered HTTP 504 with nothing rendered at all.

SMC makes the same reads and was missing the same indexes; it had simply not
accumulated enough history to time out yet. These tests assert that SQLite
answers both labs' status reads from an index, since the failure they guard
against is invisible on the small databases the rest of the suite creates.
"""
import pytest

from services.price_action_lab import PriceActionPaperAccount
from services.smc_strategy_lab import SMCPaperAccount

# (table, filter column, ordering column) for every read a lab's state() makes.
PA_READS = [
    ("pa_activity", "session_id", "created_at"),
    ("pa_candidates", "session_id", "created_at"),
    ("pa_order_meta", "session_id", "created_at"),
    ("pa_funding_events", "session_id", "funding_time"),
    ("pa_evaluations", "session_id", "candle_time"),
    ("pa_position_remediations", "session_id", "created_at"),
]
SMC_READS = [
    ("smc_activity", "session_id", "created_at"),
    ("smc_candidates", "session_id", "created_at"),
    ("smc_order_meta", "session_id", "created_at"),
    ("smc_funding_events", "session_id", "funding_time"),
    ("smc_evaluations", "session_id", "candle_time"),
]


def _plan(db, table, filter_column, order_column):
    return " ".join(
        row[3] for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM %s WHERE %s=? ORDER BY %s DESC"
            % (table, filter_column, order_column), ("session",)))


@pytest.fixture()
def pa(tmp_path):
    return PriceActionPaperAccount(tmp_path / "pa.db", starting_balance=10_000.0)


@pytest.fixture()
def smc(tmp_path):
    return SMCPaperAccount(tmp_path / "smc.db", starting_balance=10_000.0)


@pytest.mark.parametrize("table,filter_column,order_column", PA_READS)
def test_price_action_status_reads_use_an_index(pa, table, filter_column, order_column):
    plan = _plan(pa._db, table, filter_column, order_column)
    assert "SCAN" not in plan, "%s is still a full table scan: %s" % (table, plan)
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


@pytest.mark.parametrize("table,filter_column,order_column", PA_READS)
def test_price_action_status_reads_need_no_sort(pa, table, filter_column, order_column):
    """The index must supply the order too, or the sort re-reads every match."""
    assert "USE TEMP B-TREE FOR ORDER BY" not in _plan(
        pa._db, table, filter_column, order_column), table


@pytest.mark.parametrize("table,filter_column,order_column", SMC_READS)
def test_smc_status_reads_use_an_index(smc, table, filter_column, order_column):
    plan = _plan(smc._db, table, filter_column, order_column)
    assert "SCAN" not in plan, "%s is still a full table scan: %s" % (table, plan)
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


@pytest.mark.parametrize("table,filter_column,order_column", SMC_READS)
def test_smc_status_reads_need_no_sort(smc, table, filter_column, order_column):
    assert "USE TEMP B-TREE FOR ORDER BY" not in _plan(
        smc._db, table, filter_column, order_column), table


def test_both_labs_still_answer_state_with_the_indexes_in_place(pa, smc):
    """The indexes are an access-path change; the answers must not move."""
    for account in (pa, smc):
        state = account.state()
        assert state["execution_mode"] == "PAPER"
        assert state["currency"] == "USDT"
        for key in ("activity", "candidates", "evaluations", "funding_events",
                    "order_metadata", "orders", "positions", "trades"):
            assert isinstance(state[key], list), key
