"""AUDIT: the remaining record sources and origins, through their own APIs.

* SIMULATION: a Trading Instance in replay mode -- the 3-Candle Rejection
  strategy through AutoStrategyEngine on the synchronous PaperExecutionEngine,
  journal context market_data_mode="replay" (what trading_instances sets for a
  non-forward instance).
* AGENT: the SMC agent journal API, as the agent calls it -- a TAKEN decision
  and a trade on the SMC lab proposal that e2e_labs.py filled, three NOT_READY
  observations of one setup, and an execution intent that failed.
* LEGACY: the old decision journal written by the legacy engine path
  (PaperExecutionEngine on the main ledger + DecisionJournal.record_entry /
  record_exit), then two ledger rows and two journal rows removed, as a paper
  reset and a lost write left production.
Then the full recorder runs as webhook_api wires it.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import e2e_real as h  # noqa: E402
from data.journal_store import JournalStore  # noqa: E402
from data.trade_record_store import TradeRecordStore  # noqa: E402
from execution.paper_engine import PaperExecutionEngine  # noqa: E402
from services.auto_engine import AutoStrategyEngine  # noqa: E402
from services.controls import TradingControl  # noqa: E402
from services.decision_journal import DecisionJournal  # noqa: E402
from services.journal_labs import PALabProjector, SMCLabProjector  # noqa: E402
from services.journal_legacy import LegacyJournalMigration  # noqa: E402
from services.journal_recorder import JournalRecorder, LedgerSource  # noqa: E402
from services.price_action_lab import PriceActionPaperAccount  # noqa: E402
from services.signal_pipeline import SignalPipeline  # noqa: E402
from services.smc_agent_journal import SMCAgentJournal  # noqa: E402
from services.smc_strategy_lab import SMCPaperAccount  # noqa: E402
from services.strategy_factory import make_builtin_strategy  # noqa: E402
from services.trading_instances import InstanceLedger  # noqa: E402
from bot.types import Bar  # noqa: E402
from test_three_candle_rejection import _history, _long_pattern  # noqa: E402

S = h.settings
out: dict = {}

# ------------------------------------------------ SIMULATION (replay instance)
inst = "inst-replay-3cr"
scoped = InstanceLedger(h.ledger, inst, "sess-replay")
paper = PaperExecutionEngine(scoped, 10_000)
paper.strategy_id = "three_candle_rejection:1.0.0"
pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000, risk_per_trade_pct=0.01,
                      exposure_limit_pct=0.05)
pipe.journal = h.journal
pipe.journal_context = {"instance_id": inst, "simulation_session_id": "sess-replay",
                        "strategy_id": "three_candle_rejection", "strategy_name": "3-Candle Rejection · EMA 9/33",
                        "strategy_version": "1.0.0", "market_data_mode": "replay", "market_data_source": None,
                        "execution_mode": "paper", "exchange": "binance", "instrument_type": "spot"}
engine = AutoStrategyEngine(pipe, paper, scoped, symbols=[h.SYM], timeframe="5m", live=False,
                            strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
                            fetcher=lambda *a, **k: ([], "replay (audit)"), entry_mode="market", instance_id=inst)
engine.decisions, engine.reports = h.decisions, h.cycles
engine.strategy_label, engine.strategy_key, engine.strategy_version = "3-Candle Rejection · EMA 9/33 1.0.0", "three_candle_rejection", "1.0.0"
engine.last_source = "replay (audit)"
engine.quality_gate_bypass = lambda: True
rows, i = _history()
bars = h.shifted(rows + _long_pattern(i), datetime(2026, 3, 2, 10, tzinfo=timezone.utc))
strategy = engine.strategy_factory(h.SYM)
strategy.bars.extend(bars[:-3])
for b in bars[-3:]:
    engine._process_bar(h.SYM, b, strategy)
pos = paper.open_position(h.SYM)
t = bars[-1].timestamp
if pos:
    engine._process_bar(h.SYM, Bar(t + h.TF, pos["entry"], pos["target"] + 0.2, pos["entry"] - 0.1,
                                   pos["target"], 1.0), strategy)
out["replay_trades"] = h.ledger.get_paper_trades(instance_id=inst)

# ------------------------------------------------ AGENT
smc = SMCPaperAccount(S.smc_paper_db)
agent = SMCAgentJournal(S.smc_agent_journal_db)
fills = smc.broker.fills()
meta = smc._db.execute("SELECT proposal_id, order_id FROM smc_order_meta WHERE ownership='strategy'").fetchone()
if meta:
    # the order the real agent (services/smc_agent.py) writes in: intent,
    # EXECUTED, the TAKEN decision, the trade, then COMPLETE with the trade id
    proposal_id = meta["proposal_id"]
    agent.create_execution_intent(execution_key="ek-audit-ok", symbol="BTCUSDT", timeframe="5m",
                                  proposal_id=proposal_id, session_id=smc.session()["id"],
                                  payload={"direction": "long"})
    agent.transition_execution("ek-audit-ok", "EXECUTED", broker_order_id=meta["order_id"])
    dec = agent.record_decision(symbol="BTCUSDT", timeframe="5m", smc_state="ENTRY_READY", outcome="TAKEN",
                                reason_code="ALL_GATES_PASSED", reason="entry ready and every agent gate passed",
                                proposal_id=proposal_id, plan={"rr": 2.0})
    trade_id = agent.open_trade(decision_id=dec, symbol="BTCUSDT", timeframe="5m", direction="long",
                                entry=100.0, stop=99.0, target=102.0, planned_rr=2.0, size=0.01,
                                why="approved by the agent", proposal_id=proposal_id, order_id=meta["order_id"])
    agent.transition_execution("ek-audit-ok", "COMPLETE", decision_id=dec, broker_order_id=meta["order_id"],
                               trade_id=trade_id)
    agent.close_trade(trade_id, exit_price=99.0, realised_r=-1.0, result="LOSS", close_reason="stop")
    agent.record_review(trade_id=trade_id, verdict="CORRECT_BUT_LOST", followed_rules=True,
                        why="stop honoured", did_well=["waited for the sweep"], result="LOSS", realised_r=-1.0)
    out["agent_ids"] = {"decision_id": dec, "trade_id": trade_id, "proposal_id": proposal_id}
for k in range(3):
    agent.record_decision(symbol="BTCUSDT", timeframe="5m", smc_state="WAITING", outcome="NOT_READY",
                          reason_code="AWAITING_CHOCH", reason="waiting for a change of character",
                          setup_id="agent-setup-1", candle_time=(datetime.now(timezone.utc) - timedelta(minutes=5 * k)).isoformat())
agent.create_execution_intent(execution_key="ek-audit-fail", symbol="BTCUSDT", timeframe="5m",
                              proposal_id="prop-audit-fail", session_id=smc.session()["id"],
                              decision_id="dec-audit-fail", payload={"direction": "long"})
agent.transition_execution("ek-audit-fail", "EXECUTION_FAILED", error="broker rejected: margin (audit)")
out["agent"] = {"reviews": len(agent.reviews()), "trades": len(agent.trades())}

# ------------------------------------------------ LEGACY (old journal, legacy engine)
old = JournalStore(S.journal_db)
dj = DecisionJournal(old)
legacy_paper = PaperExecutionEngine(h.ledger, 10_000)
ids = []
now = datetime.now(timezone.utc)
for k in range(6):
    side = "buy" if k % 2 == 0 else "sell"
    entry, stop = (100.0, 99.0) if side == "buy" else (100.0, 101.0)
    f = legacy_paper.open(symbol="SOLUSDT", side=side, size=1.0, entry=entry, stop=stop, target=None,
                          alert_id=f"legacy-audit-{k}")
    dj.record_entry(trade_id=f.trade_id, mode="paper", symbol="SOLUSDT", side=f.side, strategy="Decision Brain",
                    timeframe="15m", entry=entry, stop=stop, target=None, size=1.0, equity=10_000,
                    confidence=0.7, brain_score=70, regime="Trending", steps=[], payload={"timestamp": now.isoformat()})
    exit_price = 102.0 if k % 3 == 0 else (99.0 if side == "buy" else 101.0)
    c = legacy_paper.close(symbol="SOLUSDT", exit_price=exit_price)
    dj.record_exit(trade_id=f.trade_id, exit_price=exit_price, pnl=c.pnl, exit_reason="legacy exit")
    ids.append(f.trade_id)
with h.ledger._lock:
    h.ledger._c.execute("DELETE FROM paper_trades WHERE id IN (?,?)", (ids[0], ids[1]))
    h.ledger._c.commit()
with old._lock:
    old._c.execute("DELETE FROM trade_decision_journal WHERE trade_id IN (?,?)", (ids[4], ids[5]))
    old._c.commit()
out["legacy_ids"] = ids

# ------------------------------------------------ the recorder, wired as webhook_api wires it
store = TradeRecordStore(S.trade_records_db)
rec = JournalRecorder(store)
rec.add_ledger(LedgerSource("MAIN", h.ledger, decision_store=h.decisions))
rec.labs = [SMCLabProjector(smc, agent_journal=agent), PALabProjector(PriceActionPaperAccount(S.price_action_paper_db))]
rec.legacy = LegacyJournalMigration(old)
from services.journal_reviews import review_finalized  # noqa: E402
rec.after_pass.append(lambda: review_finalized(store))
out["reconcile"] = rec.reconcile()
out["counts"] = [dict(r) for r in store._c.execute(
    "SELECT record_source, record_origin, status, COUNT(*) n FROM trade_records GROUP BY 1,2,3 ORDER BY 1,2,3")]
out["decision_counts"] = [dict(r) for r in store._c.execute(
    "SELECT record_source, decision_type, COUNT(*) n FROM decision_records GROUP BY 1,2 ORDER BY 1,2")]
json.dump(out, open(os.path.join(os.environ["HUB_DATA_DIR"], "e2e_sources.json"), "w"), indent=2, default=str)
print(json.dumps({"reconcile": out["reconcile"], "counts": out["counts"],
                  "decision_counts": out["decision_counts"], "agent": out["agent"]}, indent=2, default=str))
