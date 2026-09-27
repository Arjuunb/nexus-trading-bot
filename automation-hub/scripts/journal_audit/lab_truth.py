"""AUDIT: lab trade records vs the lab broker's own v2 fills, field by field."""
import json
import os
import sys

run = sys.argv[1]
d = json.load(open(f"{run}/e2e_labs.json"))
sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..")))
from data.trade_record_store import TradeRecordStore  # noqa: E402
_store = TradeRecordStore(f"{run}/trade_records.db")
for _src in ("SMC_LAB", "PA_LAB"):
    d[_src] = [_store.get(r["journal_record_id"]) for r in
               _store.query_trades(where="record_source=?", params=(_src,), limit=50)]
fails = 0
for lab, source in (("smc", "SMC_LAB"), ("pa", "PA_LAB")):
    fills = sorted(d[lab]["fills"], key=lambda f: f["timestamp"])
    recs = [r for r in d[source] if r["status"] == "CLOSED"]
    print(f"\n== {source}: {len(fills)} broker fills, {len(recs)} closed record(s)")
    if len(recs) != 1:
        print("  FAIL  expected one closed record per lifecycle")
        fails += 1
        continue
    r = recs[0]
    entry, exit_ = fills[0], fills[-1]
    net = sum(f["realized_pnl"] - f["fee"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    risk = abs(entry["price"] - entry["stop_loss"]) * entry["quantity"] if entry.get("stop_loss") else None
    checks = [
        ("actual_entry", r["actual_entry"], entry["price"]),
        ("actual_exit", r["actual_exit"], exit_["price"]),
        ("filled_quantity", r["filled_quantity"], entry["quantity"]),
        ("planned_stop_loss", r["planned_stop_loss"], entry.get("stop_loss")),
        ("planned_take_profit", r["planned_take_profit"], entry.get("take_profit")),
        ("fees", r["fees"], fees),
        ("net_pnl", r["net_pnl"], net),
        ("gross_pnl", r["gross_pnl"], sum(f["realized_pnl"] for f in fills)),
        ("entry_filled_at", r["entry_filled_at"], entry["fill_timestamp"]),
        ("exit_filled_at", r["exit_filled_at"], exit_["fill_timestamp"]),
        ("order_id", r["order_id"], entry["order_id"]),
        ("side", r["side"], "long" if entry["side"] == "buy" else "short"),
        ("risk_amount", r["risk_amount"], risk),
        ("realized_r", r["realized_r"], round(net / risk, 4) if risk else None),
        ("strategy_id", r["strategy_id"], entry.get("strategy")),
    ]
    for name, got, want in checks:
        ok = got == want or (isinstance(got, (int, float)) and isinstance(want, (int, float))
                             and abs(got - want) <= max(1e-6, 1e-6 * abs(want)))
        if name == "realized_r" and got is not None and want is not None:
            ok = abs(got - want) < 0.01
        if name.endswith("_at") and got and want:
            ok = str(got)[:19].replace(" ", "T") == str(want)[:19].replace(" ", "T")
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name:20} record={got!r:42} truth={want!r}")
    print("  exit_reason:", r["exit_reason"], "| outcome:", r["outcome"], "| origin:", r["record_origin"],
          "| agent_id:", r.get("agent_id"), "| decision_id:", r.get("decision_id"))
    print("  setup:", json.dumps(r.get("setup"), default=str)[:600])
    print("  evidence keys:", sorted((r.get("evidence") or {}).keys()))
    print("  missing:", r.get("missing"))
print(f"\n{'ALL MATCH' if not fails else f'{fails} MISMATCH(ES)'}")
sys.exit(1 if fails else 0)
