"""AUDIT: the SMC and PA labs through their own execution paths.

SMC: the frozen SMC strategy v1 evaluates a seeded native market-structure
engine (tests/test_smc_strategy_ladder.seeded_engine), the SMC lab account's
synchronize_candidate places the order in each operating mode, process_candle
fills it and a later candle exits it. PA: the lab's synchronize_strategy
places a proposal shaped as the native PA engine emits it (the PA strategy is
not run here), a quote fills it and a later candle exits it. Nothing is
inserted by hand. Then the real projectors run and the records are compared
with the broker's own fills.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

HUB = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, HUB)
sys.path.insert(0, os.path.dirname(__file__))
from _guard import require_scratch  # noqa: E402

require_scratch()
from config import settings  # noqa: E402
from bot.types import Bar  # noqa: E402
from data.trade_record_store import TradeRecordStore  # noqa: E402
from services.journal_labs import PALabProjector, SMCLabProjector  # noqa: E402
from services.price_action_lab import PaperExecutionConfig, PriceActionPaperAccount  # noqa: E402
from services.smc_agent_journal import SMCAgentJournal  # noqa: E402
from services.smc_strategy_lab import SMCPaperAccount, SMCPaperConfig  # noqa: E402
from services.smc_strategy_v1 import evaluate  # noqa: E402
from tests.test_smc_strategy_ladder import seeded_engine  # noqa: E402

RULES = {"tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
         "max_quantity": 100.0, "min_notional": 5.0}
store = TradeRecordStore(settings.trade_records_db)
out: dict = {"smc": {}, "pa": {}}

# ------------------------------------------------------------------ SMC
smc = SMCPaperAccount(settings.smc_paper_db)
agent = SMCAgentJournal(settings.smc_agent_journal_db)
evaluation = evaluate(seeded_engine())
out["smc"]["evaluation_state"] = evaluation.get("state")
out["smc"]["proposal"] = {k: evaluation.get("proposal", {}).get(k) for k in
                          ("id", "symbol", "timeframe", "direction", "entry", "stop", "signal_timestamp")}
out["smc"]["trade_plan"] = evaluation.get("trade_plan")
modes = {}
side_accounts = []
for mode in ("signals_only", "manual_approval", "automatic"):
    # proposal ids are unique per account, so each mode runs in its own lab
    # account; the automatic one is the lab's main database
    acct = smc if mode == "automatic" else SMCPaperAccount(
        os.path.join(os.environ["HUB_DATA_DIR"], f"smc_{mode}.db"))
    acct.configure(config=SMCPaperConfig(operating_mode=mode))
    res = acct.synchronize_candidate(evaluation, rules=RULES,
                                     reference_price=evaluation["trade_plan"]["entry"],
                                     feed_reliable=True)
    modes[mode] = {"session": acct.session()["id"], "status": res.get("candidate_status"),
                   "reason": res.get("reason"), "duplicate": res.get("duplicate", False)}
    if acct is not smc:
        side_accounts.append(acct)
unreliable = SMCPaperAccount(os.path.join(os.environ["HUB_DATA_DIR"], "smc_unreliable.db"))
unreliable.configure(config=SMCPaperConfig(operating_mode="automatic"))
res = unreliable.synchronize_candidate(evaluation, rules=RULES,
                                       reference_price=evaluation["trade_plan"]["entry"], feed_reliable=False)
modes["automatic_feed_unreliable"] = {"status": res.get("candidate_status"), "reason": res.get("reason")}
side_accounts.append(unreliable)
out["smc"]["modes"] = modes
entry = evaluation["trade_plan"]["entry"]
stop = evaluation["trade_plan"]["stop"]
t_sig = evaluation["proposal"]["signal_timestamp"]
long_ = str(evaluation["proposal"]["direction"]).lower() in ("long", "bullish", "buy")
fill = smc.process_candle("BTCUSDT", Bar(t_sig + timedelta(minutes=5), entry, entry + 0.5, entry - 0.5, entry, 10_000))
pos = smc.broker.positions()
out["smc"]["position_after_fill"] = pos[0] if pos else None
# the next candle trades through the protective stop
far = stop - 1 if long_ else stop + 1
exit_ = smc.process_candle("BTCUSDT", Bar(t_sig + timedelta(minutes=10), entry,
                                           max(entry, far) + 0.1, min(entry, far) - 0.1, far, 10_000))
out["smc"]["positions_after_exit"] = len(smc.broker.positions())
out["smc"]["fills"] = smc.broker.fills()
out["smc"]["session_risk_pct"] = smc.session()["risk_pct"]      # configured in percent
proj = SMCLabProjector(smc, agent_journal=agent)
out["smc"]["project"] = proj.project(store)
out["smc"]["project_again"] = proj.project(store)
for acct in side_accounts:
    SMCLabProjector(acct).project(store)

# ------------------------------------------------------------------ PA
pa = PriceActionPaperAccount(settings.price_action_paper_db)
pa.start(mode="LIVE_PAPER", symbol="BTCUSDT", timeframe="5m",
         execution_config=PaperExecutionConfig(operating_mode="automatic", strategy_id="PA1_SR_REJECTION"))
now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=15)
state = {"research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "strategy_version": "1.1.0",
         "symbol": "BTCUSDT", "timeframe": "5m",
         "setups": [{"id": "pa-setup-audit", "strategy_id": "PA1_SR_REJECTION", "direction": "bullish",
                     "phase": "ORDER_PENDING", "zone_id": "zone-audit"}],
         "proposals": [{"id": "pa-proposal-audit", "setup_id": "pa-setup-audit",
                        "strategy_id": "PA1_SR_REJECTION", "direction": "bullish",
                        "entry": 105, "stop": 100, "target": 117.5, "valid_until_index": 20}],
         "metrics": {}}
pa.synchronize_strategy(state, contract_rules=RULES, candle=Bar(now, 100, 104, 99, 103, 1000),
                        feed_reliable=True, feed_status={"state": "SYNCHRONIZED"})
pa.synchronize_strategy(state, contract_rules=RULES, candle=Bar(now + timedelta(minutes=5), 104, 106, 101, 105, 1000),
                        feed_reliable=True,
                        feed_status={"state": "SYNCHRONIZED", "last_quote_update": now.isoformat(),
                                     "last_mark_update": now.isoformat()},
                        execution_quote={"bid": 104.9, "ask": 105.1, "mark": 105.0})
out["pa"]["positions_after_fill"] = pa.broker.positions()
done = dict(state, setups=[], proposals=[])
pa.synchronize_strategy(done, contract_rules=RULES, candle=Bar(now + timedelta(minutes=10), 105, 118.5, 104.5, 118, 1000),
                        feed_reliable=True,
                        feed_status={"state": "SYNCHRONIZED", "last_quote_update": now.isoformat(),
                                     "last_mark_update": now.isoformat()},
                        execution_quote={"bid": 117.9, "ask": 118.1, "mark": 118.0})
out["pa"]["positions_after_exit"] = len(pa.broker.positions())
out["pa"]["fills"] = pa.broker.fills()
papr = PALabProjector(pa)
out["pa"]["project"] = papr.project(store)
out["pa"]["project_again"] = papr.project(store)

# ------------------------------------------------------------------ what the store holds
for source in ("SMC_LAB", "PA_LAB"):
    recs = store.query_trades(where="record_source=?", params=(source,), limit=50)
    out[source] = [store.get(r["journal_record_id"]) for r in recs]
    decs = store.query_decisions(where="record_source=?", params=(source,), limit=50)
    out[source + "_decisions"] = [{k: d.get(k) for k in ("decision_type", "status", "reason", "journal_record_id",
                                                          "record_origin", "conditions_passed")} for d in decs]
json.dump(out, open(os.path.join(os.environ["HUB_DATA_DIR"], "e2e_labs.json"), "w"), indent=2, default=str)
print(json.dumps({"smc_modes": modes, "smc_state": out["smc"]["evaluation_state"],
                  "smc_fills": len(out["smc"]["fills"]), "pa_fills": len(out["pa"]["fills"]),
                  "smc_project": out["smc"]["project"], "pa_project": out["pa"]["project"],
                  "SMC_LAB_decisions": out["SMC_LAB_decisions"], "PA_LAB_decisions": out["PA_LAB_decisions"],
                  "SMC_records": [{k: r.get(k) for k in ("journal_record_id", "status", "outcome", "record_origin",
                                                         "exit_reason", "net_pnl", "realized_r", "actual_entry",
                                                         "actual_exit", "planned_stop_loss", "fees", "session_id",
                                                         "htf_bias", "setup_type", "decision_id")} for r in out["SMC_LAB"]],
                  "PA_records": [{k: r.get(k) for k in ("journal_record_id", "status", "outcome", "record_origin",
                                                        "exit_reason", "net_pnl", "realized_r", "actual_entry",
                                                        "actual_exit", "planned_stop_loss", "fees", "session_id",
                                                        "setup_type", "market_regime")} for r in out["PA_LAB"]]},
                 indent=2, default=str))
