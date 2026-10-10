"""Restart joins use committed primary IDs and never mutate accounting."""
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.trading_instances import InstanceLedger


def engine(base, instance, session):
    return PaperExecutionEngine(InstanceLedger(base, instance, session), 10000)


def test_receipt_lookup_returns_open_reduce_close_and_never_writes(tmp_path):
    ledger = SqliteLedger(tmp_path / "ledger.db")
    paper = engine(ledger, "one", "session")
    opened = paper.open(symbol="XRPUSDT", side="BUY", size=2, entry=1, stop=.9,
                        alert_id="open-id")
    reduced = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=.5,
                           execution_id="reduce-id")
    paper.close(symbol="XRPUSDT", exit_price=1.2, execution_id="close-id")
    statements = []
    ledger._c.set_trace_callback(statements.append)
    receipts = paper.ledger.get_execution_receipts()
    assert [row["action"] for row in receipts] == ["OPEN", "REDUCE", "CLOSE"]
    assert receipts[0]["trade_id"] == opened.trade_id
    assert receipts[1]["trade_id"] == reduced.remainder_trade_id
    assert receipts[1]["position_id"] == reduced.remainder_position_id
    assert receipts[2]["trade_id"] == reduced.remainder_trade_id
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)


def test_receipt_lookup_isolates_same_symbol_instances_and_sessions(tmp_path):
    ledger = SqliteLedger(tmp_path / "ledger.db")
    for instance, session, key in (("one", "old", "old-id"), ("one", "new", "new-id"),
                                   ("two", "new", "other-id")):
        paper = engine(ledger, instance, session)
        paper.open(symbol="XRPUSDT", side="BUY", size=1, entry=1, stop=.9, alert_id=key)
        paper.close(symbol="XRPUSDT", exit_price=1.1, execution_id=key + "-close")
    receipts = InstanceLedger(ledger, "one", "new").get_execution_receipts()
    assert {row["execution_id"] for row in receipts} == {"new-id", "new-id-close"}
    assert len(ledger.get_execution_receipts(instance_id="one")) == 4
    assert len(ledger.get_execution_receipts()) == 6


def test_remote_receipt_reader_paginates_and_keeps_instance_filter():
    from types import SimpleNamespace
    from data.ledger import SupabaseLedger
    source = [{"execution_id": f"e-{index:04}", "instance_id": "one", "created_at": str(index)}
              for index in range(1001)] + [{"execution_id": "other", "instance_id": "two"}]
    ranges = []
    class Query:
        def select(self, columns):
            assert columns == "*"
            return self
        def eq(self, key, value):
            self.instance = value
            assert key == "instance_id"
            return self
        def order(self, key):
            assert key in ("created_at", "execution_id")
            return self
        def range(self, start, end):
            self.start, self.end = start, end
            ranges.append((start, end))
            return self
        def execute(self):
            scoped = [row for row in source if row["instance_id"] == self.instance]
            return SimpleNamespace(data=scoped[self.start:self.end + 1])
    remote = SupabaseLedger.__new__(SupabaseLedger)
    def table(name):
        assert name == "paper_executions"
        return Query()
    remote._t = table
    rows = remote.get_execution_receipts(instance_id="one")
    assert len(rows) == 1001
    assert ranges == [(0, 999), (1000, 1999)]
    assert all(row["instance_id"] == "one" for row in rows)
