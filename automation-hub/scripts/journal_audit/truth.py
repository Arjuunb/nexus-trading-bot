"""AUDIT: dump execution truth and the canonical record side by side.

python truth.py <run_dir> <instance_id>
Prints every ledger row for the instance (webhook_events, paper_trades,
positions, paper_executions), the decision rows, and the trade record, then
checks each factual field of the record against the ledger. Exit code 1 on
any mismatch.
"""
import json
import sqlite3
import sys

run, inst = sys.argv[1], sys.argv[2]


def rows(db, sql, args=()):
    c = sqlite3.connect(f"{run}/{db}")
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, args)]


def show(title, items, keys=None):
    print(f"\n== {title} ({len(items)})")
    for r in items:
        print(" ", json.dumps({k: r[k] for k in (keys or r)}, default=str)[:900])


wh = rows("ledger.db", "SELECT id, alert_id, symbol, side, entry, stop, status, received_at, payload_json AS payload, instance_id "
          "FROM webhook_events WHERE alert_id LIKE ? OR instance_id=? ORDER BY id", (f"%{inst}%", inst))
show("webhook_events", wh, ["id", "alert_id", "side", "entry", "stop", "status", "received_at"])
pt = rows("ledger.db", "SELECT * FROM paper_trades WHERE instance_id=? ORDER BY id", (inst,))
show("paper_trades", pt)
pos = rows("ledger.db", "SELECT * FROM positions WHERE instance_id=?", (inst,))
show("positions", pos)
try:
    ex = rows("ledger.db", "SELECT * FROM paper_executions WHERE instance_id=? ORDER BY created_at", (inst,))
except sqlite3.OperationalError as e:
    ex = []
    print("paper_executions:", e)
show("paper_executions", ex)
dec = rows("decisions.db", "SELECT id, ts, final_state, gate_stage, blocker, decision, executed, "
           "decision_identity FROM decisions WHERE instance_id=?", (inst,))
show("decisions", dec)
tr = rows("trade_records.db", "SELECT * FROM trade_records WHERE instance_id=?", (inst,))
show("trade_records", tr, ["journal_record_id", "execution_key", "trade_id", "position_id", "decision_id",
                           "status", "finalized", "record_source", "record_origin"])
drs = rows("trade_records.db", "SELECT decision_record_id, decision_key, decision_type, status, blocker, "
           "journal_record_id FROM decision_records WHERE instance_id=?", (inst,))
show("decision_records", drs)
if len(tr) != 1:
    print(f"\nFAIL: expected exactly one trade record for {inst}, found {len(tr)}")
    sys.exit(1)
r = tr[0]
print("\n== full trade record")
for k, v in r.items():
    print(f"  {k:28} {v}")

# ---------------- field-by-field ----------------
trade = pt[0]
fill_ev = [w for w in wh if ":fill:" in w["alert_id"]]
fev = json.loads(fill_ev[0]["payload"]) if fill_ev else {}
entry_wh = [w for w in wh if w["alert_id"] == trade["alert_id"]]
epl = json.loads(entry_wh[0]["payload"]) if entry_wh else {}
close_wh = [w for w in wh if w["side"] == "CLOSE"]
cpl = json.loads(close_wh[0]["payload"]) if close_wh else {}
d = dec[0] if dec else {}
gross = (trade["pnl"] or 0) + (trade["fees"] or 0)
risk = abs(trade["entry"] - trade["stop"]) * trade["size"] if trade.get("stop") is not None else None
checks = [
    ("trade_id", r["trade_id"], trade["id"]),
    ("instance_id", r["instance_id"], trade["instance_id"]),
    ("session", r.get("session_id"), trade.get("simulation_session_id")),
    ("symbol", r["symbol"], trade["symbol"]),
    ("side", r["side"], trade["side"]),
    ("filled_quantity", r["filled_quantity"], trade["size"]),
    ("actual_entry", r["actual_entry"], trade["entry"]),
    ("actual_exit", r["actual_exit"], trade["exit"]),
    ("stop_loss", r["planned_stop_loss"], trade["stop"]),
    ("position_id", r["position_id"], [e for e in ex if e["action"] == "OPEN"][0]["position_id"] if ex else None),
    ("take_profit", r["planned_take_profit"], trade["target"]),
    ("planned_entry", r["planned_entry"], fev.get("requested_price")),
    ("fees", r["fees"], trade["fees"]),
    ("net_pnl", r["net_pnl"], trade["pnl"]),
    ("gross_pnl", r["gross_pnl"], gross),
    ("risk_amount", r["risk_amount"], risk),
    ("realized_r", r["realized_r"], round(trade["pnl"] / risk, 4) if risk else None),
    ("position_opened_at", r["position_opened_at"], trade["opened_at"]),
    ("position_closed_at", r["position_closed_at"], trade["closed_at"]),
    ("filled_at", r["entry_filled_at"], fev.get("fill_timestamp")),
    ("order_submitted_at", r["order_submitted_at"], fev.get("order_timestamp")),
    # the forward engine writes no acknowledgement event, so there is no ack time
    ("order_acknowledged_at", r["order_acknowledged_at"], None),
    ("signal_at", r["signal_detected_at"], fev.get("signal_timestamp")),
    ("exit_reason", r["exit_reason"], cpl.get("exit_reason")),
    ("decision_id", str(r["decision_id"]), str(d.get("id"))),
    ("exit_filled_at", r["exit_filled_at"], trade["closed_at"]),
    ("equity_before", r["equity_before"], trade["equity_before_trade"]),
    ("bid", r["bid"], fev.get("fill_bid")),
    ("ask", r["ask"], fev.get("fill_ask")),
    ("mfe_r", r["mfe_r"], cpl.get("mfe_r")),
    ("mae_r", r["mae_r"], cpl.get("mae_r")),
    ("strategy_id", r["strategy_id"], trade["strategy_id"]),
    ("side(entry webhook)", {"BUY": "long", "SELL": "short"}[entry_wh[0]["side"]], trade["side"]),
    ("alert_id(execution)", r["execution_key"].split(":", 3)[-1], trade["alert_id"]),
]
fails = 0
print("\n== field-by-field (record vs execution truth)")
for name, got, want in checks:
    ok = (got == want) or (isinstance(got, float) and isinstance(want, (int, float))
                           and abs(got - want) <= 1e-6 * max(1, abs(want)))
    if name == "realized_r" and got is not None and want is not None:
        ok = abs(got - want) < 0.01
    if name in ("position_opened_at", "position_closed_at", "filled_at", "order_submitted_at", "signal_at") \
            and got and want:
        ok = str(got).replace("Z", "+00:00")[:19] == str(want).replace("Z", "+00:00")[:19]
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name:22} record={got!r:45} truth={want!r}")
print(f"\n{len(checks) - fails}/{len(checks)} fields match")
sys.exit(1 if fails else 0)
