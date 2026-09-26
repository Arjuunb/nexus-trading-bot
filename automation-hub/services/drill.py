"""Failure drills — recovery paths only count if they're exercised.

Runs the disaster scenarios that matter, each against TEMPORARY state (never
the live ledger), and reports pass/fail with what actually happened:

    crash-mid-position     kill the engine with a position open; a fresh
                           engine must adopt it from the ledger and still
                           exit it at the stop
    ledger-backup-restore  snapshot a ledger with trades, verify the copy
                           opens and holds the same trades
    reconciliation         a fabricated bot-vs-exchange mismatch must be
                           detected, never silently accepted
    kill-switch            the daily-loss guard must block the next entry
                           after the limit is breached

POST /ops/drill runs them all; a failing drill is a red alert, not a shrug.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _result(name: str, ok: bool, detail: str) -> dict:
    return {"drill": name, "ok": bool(ok), "detail": detail}


def drill_crash_mid_position() -> dict:
    from bot.types import Bar
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.auto_engine import AutoStrategyEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    try:
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "drill.db")
            led = SqliteLedger(db)
            paper = PaperExecutionEngine(led, 10_000)
            pipe = SignalPipeline(led, paper, TradingControl(), equity=10_000,
                                  risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
            r = pipe.process({"alert_id": "d1", "symbol": "BTCUSDT", "side": "BUY",
                              "entry": 100.0, "stop": 95.0, "confidence": 1.0})
            if not r.accepted:
                return _result("crash-mid-position", False, f"setup entry rejected: {r.reason}")
            # "crash": everything in memory is gone; only the ledger survives.
            led2 = SqliteLedger(db)
            paper2 = PaperExecutionEngine(led2, 10_000)
            pipe2 = SignalPipeline(led2, paper2, TradingControl(), equity=10_000)
            eng2 = AutoStrategyEngine(pipe2, paper2, led2, symbols=["BTCUSDT"],
                                      entry_mode="market")
            if paper2.open_position("BTCUSDT") is None:
                return _result("crash-mid-position", False,
                               "position lost across restart — ledger did not persist it")
            ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            eng2._check_exit("BTCUSDT", Bar(ts, 96, 96.5, 94.0, 94.5, 1.0))  # dips through 95
            if paper2.open_position("BTCUSDT") is not None:
                return _result("crash-mid-position", False,
                               "adopted position did NOT exit at its stop after restart")
            hist = paper2.history()
            return _result("crash-mid-position", True,
                           f"restarted engine adopted the position and stopped out at "
                           f"{hist[0]['exit']} (pnl {hist[0]['pnl']:+.2f})")
    except Exception as e:  # noqa: BLE001
        return _result("crash-mid-position", False, f"drill crashed: {e}")


def drill_backup_restore() -> dict:
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.backup import backup_now, restore_check, restore_to
    try:
        with tempfile.TemporaryDirectory() as td:
            led = SqliteLedger(str(Path(td) / "ledger.db"))
            paper = PaperExecutionEngine(led, 10_000)
            paper.open(symbol="BTCUSDT", side="BUY", size=1.0, entry=100, stop=95)
            paper.close(symbol="BTCUSDT", exit_price=110)
            b = backup_now(td)
            if not b["ok"] or "ledger.db" not in b["files"]:
                return _result("ledger-backup-restore", False, f"backup failed: {b}")
            chk = restore_check(td, b["snapshot"])
            if not chk["ok"]:
                return _result("ledger-backup-restore", False, f"restore check failed: {chk}")
            # Restore the way an operator would -- into a separate directory,
            # decrypting when the snapshot is sealed -- and read it back.
            back = restore_to(td, b["snapshot"], str(Path(td) / "restored"))
            if not back["ok"]:
                return _result("ledger-backup-restore", False, f"restore failed: {back}")
            restored = SqliteLedger(str(Path(td) / "restored" / "ledger.db"))
            trades = [t for t in restored.get_paper_trades() if t["status"] == "closed"]
            ok = len(trades) == 1 and trades[0]["pnl"] == 10.0
            return _result("ledger-backup-restore", ok,
                           "snapshot opens and holds the same closed trade" if ok
                           else "restored ledger does not match the original")
    except Exception as e:  # noqa: BLE001
        return _result("ledger-backup-restore", False, f"drill crashed: {e}")


def drill_reconciliation() -> dict:
    from bot.types import Position
    from execution.live_readiness import reconcile_startup
    try:
        rep = reconcile_startup(
            [{"symbol": "BTCUSDT", "side": "long", "size": 1.0}],
            [Position(symbol="ETH/USDT", qty=5.0, avg_price=100.0)])
        ok = (not rep["clean"] and rep["missing_on_exchange"]
              and rep["missing_locally"])
        return _result("reconciliation", ok,
                       "fabricated mismatch was detected on both sides" if ok
                       else f"mismatch NOT fully detected: {rep}")
    except Exception as e:  # noqa: BLE001
        return _result("reconciliation", False, f"drill crashed: {e}")


def drill_kill_switch() -> dict:
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    try:
        led = SqliteLedger(":memory:")
        paper = PaperExecutionEngine(led, 10_000)
        # the portfolio exposure cap (10%) limits the position to ~10 units, so
        # the -10/unit close realizes ~-1% of equity; a 0.5% daily limit must trip
        pipe = SignalPipeline(led, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.05, exposure_limit_pct=0.5,
                              max_daily_loss_pct=0.005, adaptive_risk=False,
                              equity_throttle=False)
        pipe.process({"alert_id": "k1", "symbol": "BTCUSDT", "side": "BUY",
                      "entry": 100.0, "stop": 95.0, "confidence": 1.0})
        pipe.process({"alert_id": "k2", "symbol": "BTCUSDT", "side": "CLOSE",
                      "entry": 90.0})                      # big loss > 2% of equity
        nxt = pipe.process({"alert_id": "k3", "symbol": "ETHUSDT", "side": "BUY",
                            "entry": 100.0, "stop": 95.0, "confidence": 1.0})
        ok = (not nxt.accepted) and nxt.stage in ("daily_loss", "risk_guard")
        return _result("kill-switch", ok,
                       f"entry after the breach was blocked at '{nxt.stage}'" if ok
                       else "entry was NOT blocked after the daily-loss breach")
    except Exception as e:  # noqa: BLE001
        return _result("kill-switch", False, f"drill crashed: {e}")


def run_drills() -> dict:
    results = [drill_crash_mid_position(), drill_backup_restore(),
               drill_reconciliation(), drill_kill_switch()]
    passed = sum(1 for r in results if r["ok"])
    return {"ok": passed == len(results), "passed": passed,
            "total": len(results), "results": results,
            "ran_at": datetime.now(timezone.utc).isoformat()}
