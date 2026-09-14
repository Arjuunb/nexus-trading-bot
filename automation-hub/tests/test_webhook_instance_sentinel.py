"""``webhook_events.instance_id`` is ``''`` for unscoped rows, never NULL.

The column is declared ``TEXT NOT NULL DEFAULT ''`` in
``trading_instances_schema.sql``, so it cannot hold NULL on any deployment.
SupabaseLedger nonetheless omitted the key on insert and then searched for NULL
when promoting or releasing an unscoped claim, so those two lookups matched no
row on any schema: a claim taken for a legacy alert could never be promoted and
never be released, permanently barring that alert_id. One production database
held 1230 rows with ``''`` and zero with NULL, which is what surfaced it.
"""
from __future__ import annotations

import inspect

from data import ledger as ledger_module
from data.ledger import SqliteLedger


def test_the_schema_forbids_null_so_the_sentinel_is_the_empty_string():
    from pathlib import Path
    schema = (Path(__file__).parents[1] / "data/trading_instances_schema.sql").read_text()
    assert "ALTER TABLE webhook_events ADD COLUMN IF NOT EXISTS instance_id TEXT NOT NULL DEFAULT ''" in schema


def test_supabase_never_searches_for_a_null_instance_id():
    source = inspect.getsource(ledger_module.SupabaseLedger)
    assert 'is_("instance_id"' not in source, (
        "instance_id is NOT NULL DEFAULT '' -- a NULL search matches nothing, "
        "which stranded every unscoped claim.")


def test_supabase_always_writes_the_sentinel_rather_than_omitting_it():
    source = inspect.getsource(ledger_module.SupabaseLedger.insert_webhook_event)
    assert '"instance_id": instance_id or ""' in source, (
        "Omitting the key left the value to the column default, which is what "
        "made the stored sentinel disagree with the one the lookups searched.")
    assert "if instance_id:\n            row[" not in source


def _claim(led, alert_id: str, instance_id: str = "") -> None:
    led.insert_webhook_event(
        alert_id=alert_id, symbol="BTCUSDT", side="buy", entry=1.0, stop=0.9,
        payload={"symbol": "BTCUSDT"}, status="claimed", instance_id=instance_id)


def test_an_unscoped_claim_can_be_promoted_and_released(tmp_path):
    """End to end on the backend the tests can actually exercise.

    Both of these returned "nothing matched" on Supabase, because the claim was
    stored with instance_id '' and looked up with a NULL predicate.
    """
    led = SqliteLedger(str(tmp_path / "l.db"))

    _claim(led, "legacy-1")
    assert led.promote_webhook_event(
        alert_id="legacy-1", status="accepted", instance_id="") is True

    _claim(led, "legacy-2")
    assert led.release_webhook_claim(alert_id="legacy-2", instance_id="") == 1
    # Released, so the same alert may be claimed again rather than being
    # barred forever by a claim nothing could reach.
    _claim(led, "legacy-2")


def test_an_instance_claim_is_not_released_by_an_unscoped_call(tmp_path):
    """The sentinel must not collapse the two scopes into one."""
    led = SqliteLedger(str(tmp_path / "l.db"))
    _claim(led, "shared-alert", instance_id="inst-1")

    assert led.release_webhook_claim(alert_id="shared-alert", instance_id="") == 0
    assert led.release_webhook_claim(alert_id="shared-alert", instance_id="inst-1") == 1


def test_the_idempotency_index_is_scoped_to_instance_rows(tmp_path):
    """Legacy duplicates must not deny the guarantee to instance rows.

    A production database held 84 duplicate ``(alert_id, '', status)`` groups
    from the pre-instance auto engine, every one of them a 'rejected' refusal.
    A unique index spanning them fails outright, leaving the deployment with no
    durable constraint at all rather than one over the rows that matter.
    """
    led = SqliteLedger(str(tmp_path / "l.db"))
    payload = {"symbol": "BTCUSDT", "side": "sell"}
    for _ in range(2):
        led.insert_webhook_event(
            alert_id="auto-ADAUSDT-sell-10", symbol="ADAUSDT", side="sell",
            entry=1.0, stop=1.1, payload=payload, status="rejected", instance_id="")

    report = led._ensure_order_idempotency_index()
    assert report["enforced"] is True, (
        f"legacy duplicates must not block the index: {report}")

    # And it still refuses a genuine instance-scoped replay.
    led.insert_webhook_event(
        alert_id="auto:inst-1:BTCUSDT:5m:1:buy", symbol="BTCUSDT", side="buy",
        entry=1.0, stop=0.9, payload=payload, status="accepted", instance_id="inst-1")
    try:
        led.insert_webhook_event(
            alert_id="auto:inst-1:BTCUSDT:5m:1:buy", symbol="BTCUSDT", side="buy",
            entry=1.0, stop=0.9, payload=payload, status="accepted", instance_id="inst-1")
    except ledger_module.DuplicateOrderIntent:
        pass
    else:
        raise AssertionError("an instance-scoped replay must still be refused")
