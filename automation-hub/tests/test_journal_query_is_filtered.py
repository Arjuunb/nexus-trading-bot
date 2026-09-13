"""journal.list() must not read the whole journal to answer a narrow question.

A thread dump of the hung Price Action status route caught it here:

    price_action_lab.py:2044  paper_rows = self.account.journal.list(
    price_action_governance.py:734  records = self._latest_records()
    price_action_governance.py:722  -> .fetchall()

_latest_records() selected every entry ever written, joined every revision,
and json.loads()'d every payload, after which list() filtered in Python.
bot_status calls it three times per poll. On a journal with real history that
ran past nginx's ninety-second ceiling, so the lab answered HTTP 504 and the
dashboard showed nothing at all.

The filters that map to indexed columns now run in SQL. These tests pin that
the answers are unchanged and that the database does the narrowing.
"""
import json
import uuid

import pytest

from services.price_action_governance import PriceActionJournalStore


def _payload(session_id, partition, symbol, timeframe, direction):
    return {
        "identity": {"session_id": session_id, "research_partition": partition,
                     "strategy_version": "v1", "execution_mode": "PAPER"},
        "setup": {"trigger_classification": "generic_rejection"},
        "review": {"rule_compliance": True},
        "outcome": {"result": "win", "net_r": 1.5},
        "market_context": {"data_health_state": "SYNCHRONIZED", "zone_role": "support",
                           "touch_count": 1, "market_regime": "trending"},
        "order_risk": {"entry_model": "confirmation"},
        "symbol": symbol, "timeframe": timeframe, "direction": direction,
    }


def _insert(store, *, session_id, partition, symbol="BTCUSDT", timeframe="5m",
            direction="long", opened_at="2026-09-12T00:00:00+00:00"):
    """Write one entry plus its revision directly, matching the real schema."""
    journal_id, setup_id = str(uuid.uuid4()), str(uuid.uuid4())
    payload = _payload(session_id, partition, symbol, timeframe, direction)
    with store._lock:
        store._db.execute(
            "INSERT INTO pa_journal_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (journal_id, session_id, None, setup_id, "SR_REJECTION", "v1",
             "cfg", "eng", "data", symbol, timeframe, direction, "LIVE",
             partition, "CLOSED", "win", opened_at, opened_at,
             json.dumps(payload), opened_at))
        store._db.execute(
            "INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), journal_id, 1, "STATE_TRANSITION", opened_at,
             "test", uuid.uuid4().hex, json.dumps(payload)))
        store._db.commit()
    return journal_id


@pytest.fixture()
def store(tmp_path):
    return PriceActionJournalStore(tmp_path / "journal.db")


def test_partition_filter_returns_only_that_partition(store):
    _insert(store, session_id="s1", partition="paper_forward")
    _insert(store, session_id="s1", partition="development")
    _insert(store, session_id="s1", partition="validation")

    for partition in ("paper_forward", "development", "validation"):
        rows = store._latest_records(partition=partition)
        assert len(rows) == 1, partition
        assert rows[0]["index"]["partition_label"] == partition


def test_session_filter_returns_only_that_session(store):
    _insert(store, session_id="wanted", partition="paper_forward")
    _insert(store, session_id="other", partition="paper_forward")

    rows = store._latest_records(session_id="wanted")
    assert [row["index"]["session_id"] for row in rows] == ["wanted"]


def test_filters_combine(store):
    _insert(store, session_id="s1", partition="paper_forward", symbol="BTCUSDT")
    _insert(store, session_id="s1", partition="paper_forward", symbol="ETHUSDT")
    _insert(store, session_id="s2", partition="paper_forward", symbol="BTCUSDT")

    assert len(store._latest_records(session_id="s1")) == 2
    assert len(store._latest_records(symbol="BTCUSDT")) == 2
    assert len(store._latest_records(session_id="s1", symbol="BTCUSDT")) == 1
    # Case is normalised the way the Python check did it.
    assert len(store._latest_records(symbol="btcusdt")) == 2


def test_an_unfiltered_read_still_returns_everything(store):
    for index in range(4):
        _insert(store, session_id="s%d" % index, partition="paper_forward")
    assert len(store._latest_records()) == 4


def test_the_database_does_the_narrowing_not_python(store):
    """The point of the fix: a narrow question must not load the whole table."""
    _insert(store, session_id="wanted", partition="paper_forward")
    for index in range(25):
        _insert(store, session_id="noise%d" % index, partition="development")

    assert len(store._latest_records(session_id="wanted")) == 1
    # Narrowed, not lost: everything is still there when nothing is asked for.
    assert len(store._latest_records()) == 26


def test_date_window_is_applied(store):
    _insert(store, session_id="old", partition="paper_forward",
            opened_at="2026-01-01T00:00:00+00:00")
    _insert(store, session_id="new", partition="paper_forward",
            opened_at="2026-09-12T00:00:00+00:00")

    recent = store._latest_records(date_from="2026-06-01T00:00:00+00:00")
    assert [row["index"]["session_id"] for row in recent] == ["new"]


def test_list_returns_the_same_rows_through_the_public_api(store):
    _insert(store, session_id="s1", partition="paper_forward")
    _insert(store, session_id="s1", partition="development")

    assert len(store.list(partition="paper_forward")["entries"]) == 1
    assert len(store.list(partition="development")["entries"]) == 1
    assert len(store.list()["entries"]) == 2
    # A payload-only filter still works, since only the JSON can answer it.
    assert len(store.list(strategy_version="v1")["entries"]) == 2
    assert store.list(strategy_version="nope")["entries"] == []
