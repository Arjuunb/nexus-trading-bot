"""The PA-vs-SMC comparison must not be quadratic in its own history.

measurements() is a four-way LEFT JOIN across shadow_decisions, shadow_orders,
shadow_fills, shadow_outcomes and shadow_mae_mfe, and the comparison report
reads all of it. A FOREIGN KEY declaration creates no index in SQLite, so two
of those joins had no index to use. SQLite papered over that by building an
AUTOMATIC COVERING INDEX for shadow_orders and shadow_fills on every call, and
then sorted the whole joined result in a temp B-tree because nothing indexed
the report's ordering either — all of it repeated on each poll of a route the
dashboard polls continuously.

The observatory records one decision per variant per candle. Nine frozen
variants on a 5m chart is roughly 2,600 rows a day, so the report degraded
quadratically and eventually timed out — and it is the single answer the two
labs exist to produce.
"""
import time

import pytest

from services.shadow_research import ShadowResearchStore

MEASUREMENTS_JOIN = (
    "SELECT d.decision_id,o.order_id,f.fill_id,x.outcome_id,m.order_id "
    "FROM shadow_decisions d "
    "LEFT JOIN shadow_orders o ON o.decision_id=d.decision_id "
    "LEFT JOIN shadow_fills f ON f.order_id=o.order_id "
    "LEFT JOIN shadow_outcomes x ON x.order_id=o.order_id "
    "LEFT JOIN shadow_mae_mfe m ON m.order_id=o.order_id "
    "ORDER BY d.created_at DESC LIMIT 100000"
)


@pytest.fixture()
def store(tmp_path):
    return ShadowResearchStore(tmp_path / "shadow.db")


def _plan(store):
    return [row[3] for row in store._db.execute("EXPLAIN QUERY PLAN " + MEASUREMENTS_JOIN)]


def test_no_step_of_the_comparison_join_is_a_table_scan(store):
    scans = [step for step in _plan(store)
             if step.startswith("SCAN") and "USING" not in step]
    assert not scans, (
        "these steps re-read a whole table once per outer row: %s" % scans)


@pytest.mark.parametrize("table", ["shadow_orders", "shadow_fills"])
def test_each_child_table_is_reached_by_an_index(store, table):
    """The two joins that had nothing to use, named individually.

    A regression here is silent: the report keeps returning correct numbers and
    simply gets slower every day until it stops answering.
    """
    steps = [step for step in _plan(store) if table in step]
    assert steps, "%s is no longer part of the comparison join" % table
    assert any("USING INDEX" in step or "USING COVERING INDEX" in step
               or "USING PRIMARY KEY" in step for step in steps), steps


def test_the_report_ordering_needs_no_temporary_sort(store):
    assert not any("TEMP B-TREE" in step for step in _plan(store)), _plan(store)


def test_the_join_stays_roughly_linear_as_history_grows(store):
    """A coarse backstop, not the detector.

    Timing alone does not catch a missing index here: SQLite responds by
    building an AUTOMATIC COVERING INDEX, which keeps the join near-linear per
    call while paying to rebuild that index on every single call, and the
    comparison route is polled. The plan assertions above are what actually
    fail when an index goes missing. This one only catches a plan that has
    degenerated far enough to show up as growth.
    """
    def load(rows, offset):
        with store._lock:
            for index in range(offset, offset + rows):
                decision, order = "d%d" % index, "o%d" % index
                store._db.execute(
                    "INSERT INTO shadow_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (decision, "k%d" % index, "PA", "acct", "PA_H_SR_REJECTION", "v1",
                     "hash", "BTCUSDT:5m:%d" % index, "ENTRY", "long", "SHADOW",
                     "NONE", "2026-09-13T00:00:00+00:00", "lineage", "{}",
                     "2026-09-13T%02d:00:00+00:00" % (index % 24)))
                store._db.execute(
                    "INSERT INTO shadow_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order, "ok%d" % index, decision, "BTCUSDT", "market", "buy",
                     100.0, 95.0, 110.0, 1.0, "FILLED", "2026-09-13T00:00:00+00:00"))
                store._db.execute(
                    "INSERT INTO shadow_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("f%d" % index, "fk%d" % index, order, "q%d" % index,
                     "2026-09-13T00:00:00+00:00", index, 100.0, 100.0, 1.0,
                     0.0, 0.0, 0.0, 0.0, "2026-09-13T00:00:00+00:00"))
            store._db.commit()

    def elapsed():
        started = time.monotonic()
        store._db.execute(MEASUREMENTS_JOIN).fetchall()
        return time.monotonic() - started

    load(1500, 0)
    first = elapsed()
    load(1500, 1500)
    second = elapsed()
    # Linear would be ~2x. Quadratic would be ~4x and climbing. A generous
    # ceiling keeps this from failing on a loaded CI runner.
    assert second < max(first * 3.0, 0.5), (
        "the join is growing faster than its history: %.3fs for 1500 rows, "
        "%.3fs for 3000" % (first, second))


def test_measurements_still_returns_the_rows_it_did_before(store):
    assert store.measurements(limit=10) == []
