"""Admin-only application Factory Reset orchestration.

The reset boundary is deliberately allowlist-based. It erases operational
state while preserving identity, credentials, source/deployment files and all
database schema. A failed reset remains stopped and is never reported healthy.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


CONFIRMATION_PHRASE = "FACTORY RESET"
RESET_VERSION = "factory-reset-v1"
PRESERVED_SCOPE = {
    "authentication": "user/login accounts",
    "secrets": ".env and API credentials",
    "application": "source code and database schema/migrations",
    "infrastructure": "VPS, Docker, domain and TLS configuration",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FactoryResetService:
    def __init__(self, runtime, app_store, bot_manager) -> None:
        self.runtime = runtime
        self.app_store = app_store
        self.bot_manager = bot_manager
        self._lock = threading.Lock()
        self.in_progress = False
        self.last_result: dict | None = None

    def status(self) -> dict:
        return {"in_progress": self.in_progress, "last_result": self.last_result,
                "confirmation_phrase": CONFIRMATION_PHRASE}

    @staticmethod
    def validate_confirmation(confirmation: str, final_confirmation: bool) -> None:
        if confirmation != CONFIRMATION_PHRASE:
            raise ValueError(f'Type "{CONFIRMATION_PHRASE}" exactly')
        if final_confirmation is not True:
            raise ValueError("final confirmation is required")

    def run(self, *, initiated_by: str, confirmation: str,
            final_confirmation: bool) -> dict:
        self.validate_confirmation(confirmation, final_confirmation)
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Factory Reset is already running")
        reset_id, started = uuid.uuid4().hex, time.monotonic()
        self.in_progress = True
        audit_started = False
        try:
            self.runtime.ledger.begin_factory_reset_audit({
                "id": reset_id, "requested_at": _now(),
                "initiated_by": initiated_by or "authenticated-admin",
                "reset_version": RESET_VERSION,
                "preserved_scope": PRESERVED_SCOPE,
            })
            audit_started = True
            stopped = self._stop_workers()
            self.runtime.ledger.factory_reset_application_data(reset_id, confirmation)
            self.app_store.clear_application_data()
            cleared = self._clear_local_state()
            health = self._reinitialize_and_check()
            duration = int((time.monotonic() - started) * 1000)
            self.runtime.ledger.finish_factory_reset_audit(
                reset_id, status="succeeded", duration_ms=duration)
            result = {
                "ok": True, "reset_id": reset_id, "reset_version": RESET_VERSION,
                "duration_ms": duration, "workers_stopped": stopped,
                "cleared": cleared, "preserved": PRESERVED_SCOPE,
                "health": health, "execution_mode": "paper", "live_enabled": False,
            }
            self.last_result = result
            return result
        except Exception as exc:
            # Fail closed: trading controls remain stopped and no instance is
            # restarted. Best-effort audit completion must not hide root cause.
            try:
                self.runtime.controls.stop_all()
            except Exception:
                pass
            duration = int((time.monotonic() - started) * 1000)
            if audit_started:
                try:
                    self.runtime.ledger.finish_factory_reset_audit(
                        reset_id, status="failed", duration_ms=duration,
                        error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
            self.last_result = {"ok": False, "reset_id": reset_id,
                                "duration_ms": duration, "error": str(exc),
                                "safe_state": "stopped/degraded"}
            raise
        finally:
            self.in_progress = False
            self._lock.release()

    def _stop_workers(self) -> dict:
        r = self.runtime
        r.controls.stop_all()
        self.bot_manager.emergency_stop_all()
        try:
            r.engine.stop("Factory Reset requested")
        except Exception:
            pass
        manager = r.instance_manager
        for instance_id in list(manager._instances):
            try:
                manager.stop(instance_id)
            except Exception:
                runtime = manager._runtime.get(instance_id)
                if runtime:
                    runtime[0].stop("Factory Reset requested")
        for thread in list(manager._reboot_threads.values()):
            if thread is not threading.current_thread():
                thread.join(timeout=5.0)
            if thread.is_alive():
                raise RuntimeError("a Trading Instance reboot worker did not stop")
        for service in (r.ws_feed, r.watchdog, r.monitor_runner, r.daily_tasks):
            service.stop()
        try:
            if r.grid_runner is not None:
                r.grid_runner.stop()
        except Exception:
            pass
        return {"legacy_bots": len(self.bot_manager.list()),
                "trading_instances": len(manager._instances)}

    @staticmethod
    def _clear_sqlite(store, tables: tuple[str, ...]) -> None:
        conn = getattr(store, "_c", None)
        if conn is None:
            return
        lock = getattr(store, "_lock", threading.RLock())
        with lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
                for table in tables:
                    if table in existing:
                        conn.execute(f'DELETE FROM "{table}"')
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _clear_local_state(self) -> list[str]:
        r = self.runtime
        self.bot_manager._bots.clear()
        self.bot_manager._runners.clear()
        manager = r.instance_manager
        with manager._lock:
            manager._runtime.clear()
            manager._instances.clear()
            manager._reboots.clear()
            manager._reboot_threads.clear()
        if getattr(r, "core_v2_store", None) is not None:
            r.core_v2_store.clear()

        stores = (
            (r.decision_journal_store, ("trade_decision_events", "trade_decision_journal", "evolution_memory")),
            (r.decision_store, ("decisions",)),
            (r.skipped_store, ("skipped_trades",)),
            (r.cycle_store, ("cycle_reports",)),
            (r.trade_memory_store, ("trade_memories_fts", "memory_reviews", "trade_memories")),
            (r.watchlist_store, ("market_prefs",)),
        )
        for store, tables in stores:
            self._clear_sqlite(store, tables)
        # The canonical journal's tables are trigger-protected against
        # deletion; only its own audited reset may clear them.
        if getattr(r, "trade_journal_store", None) is not None:
            r.trade_journal_store.factory_reset()

        # JSON operational stores. Credential stores (providers/alert channels)
        # are intentionally absent.
        for store, empty in (
            (r.journal_store, {}), (r.memory_store, {}), (r.research_store, {}),
            (r.custom_store, {}), (r.publish_store, {"listings": {}, "follows": {}}),
            (r.collab_store, {"comments": {}, "shares": {}}),
            (r.lesson_store, {}), (r.upgrade_store, {}), (r.version_store, {}),
        ):
            store._write(empty)
        with r.learning_book._lock:
            r.learning_book.lessons = []
            r.learning_book.adjustments = {}
            r.learning_book.history = []
            r.learning_book.updated_at = None
            r.learning_book.last_gate_key = None
            r.learning_book._save()
        with r.counterfactual._lock:
            r.counterfactual.open = []
            r.counterfactual.resolved = []
            r.counterfactual._save()
        r.econ_calendar.set_events([])
        with r.safety_state._lock:
            r.safety_state._data = {"emergency_stop_tested_at": None}
            r.safety_state._save()
        r.approvals.clear()
        r.exec_quality.clear()
        from services import ttl_cache
        ttl_cache.clear()
        r.market_store.clear()
        r.paper_broker_v2.factory_reset(r.settings.starting_cash)
        # The Visual Lab ledger is deliberately independent from the general
        # paper broker, so the global Factory Reset must clear it explicitly.
        # Its reset creates a fresh 10,000 USDT research session while keeping
        # the account incapable of reaching any live execution path.
        if hasattr(r, "price_action_runtime"):
            r.price_action_runtime.stop()
        r.price_action_paper.factory_reset()
        if hasattr(r, "smc_runtime"):
            r.smc_runtime.stop()
        if hasattr(r, "smc_paper"):
            r.smc_paper.factory_reset()
        if hasattr(r, "price_action_experiments"):
            r.price_action_experiments.clear()
        r.account_store.set_initial_capital(r.settings.starting_cash, reset_account=True)
        r.paper.starting_balance = r.settings.starting_cash
        r.paper._invalidate_history()

        # Restore mutable legacy settings to environment-backed first-launch
        # defaults, then leave the execution gate stopped regardless of env.
        from config import Settings
        fresh = Settings()
        defaults = {
            "risk_per_trade_pct": fresh.risk_per_trade_pct,
            "exposure_limit_pct": fresh.exposure_limit_pct,
            "max_drawdown_pct": fresh.max_drawdown_pct,
            "max_open_positions": fresh.max_open_positions,
            "dedup_window_s": fresh.dedup_window_s,
            "max_daily_loss_pct": fresh.max_daily_loss_pct,
            "session_start": fresh.session_start, "session_end": fresh.session_end,
            "max_weekly_loss_pct": fresh.max_weekly_loss_pct,
            "max_trades_per_day": fresh.max_trades_per_day,
            "max_consecutive_losses": fresh.max_consecutive_losses,
            "cooldown_after_loss_min": fresh.cooldown_after_loss_min,
            "trading_days_mask": fresh.trading_days_mask,
            "auto_strategy": fresh.auto_strategy,
            "engine_timeframe": fresh.auto_timeframe,
            "engine_symbols": ",".join(fresh.auto_symbols),
            "auto_symbols": ",".join(fresh.auto_symbols),
            "symbol_selection_mode": "auto", "trading_mode": "full",
            "engine_desired_running": 0,
        }
        for key, value in defaults.items():
            r._apply_setting(key, value)
        r.engine.symbols = list(fresh.auto_symbols)
        r.engine.manual_symbol = fresh.auto_symbols[0] if fresh.auto_symbols else "BTCUSDT"
        r.engine.deployed_spec = None
        r.engine.last_trade = None
        r.engine.stats = {"bars": 0, "signals": 0, "accepted_signals": 0,
                          "trades": 0, "rejections": 0}
        r.engine._targets.clear()
        r.engine._managed.clear()
        r.engine._pending.clear()
        r.engine._multi_timeframe_context.clear()
        r.engine._strategy_health.clear()
        r.engine.rejection_counts.clear()
        r.pipeline._alert_info.clear()
        r.pipeline._alert_info_hydrated = False
        r.pipeline.resume()
        Path(r.settings.settings_path).parent.mkdir(parents=True, exist_ok=True)
        Path(r.settings.settings_path).write_text("{}\n", encoding="utf-8")
        try:
            from data import grid_store
            grid_store.save(None)
            r.grid_runner = None
        except Exception:
            pass
        try:
            r.bot_os.bus._log.clear()
        except Exception:
            pass

        r.v2_market_data.clear_cache()
        self._clear_backups()
        return [
            "instances/positions/orders/sessions/trades", "journal/decisions/memory",
            "research/backtests/optimization/forward-validation", "alerts/logs",
            "watchlists/preferences/settings", "paper accounts/price-action session/runtime caches",
        ]

    def _clear_backups(self) -> None:
        """Remove only files under the configured application backup folder."""
        from config import DATA_DIR
        data_root = Path(DATA_DIR).resolve()
        backups = (data_root / "backups").resolve()
        if data_root == Path(data_root.anchor) or backups.parent != data_root:
            raise RuntimeError("unsafe backup directory; refusing cleanup")
        if not backups.exists():
            return
        for candidate in sorted(backups.rglob("*"), reverse=True):
            resolved = candidate.resolve()
            if backups not in resolved.parents:
                raise RuntimeError("backup path escaped configured data directory")
            if resolved.is_file() or resolved.is_symlink():
                resolved.unlink(missing_ok=True)
            elif resolved.is_dir():
                try:
                    resolved.rmdir()
                except OSError:
                    pass

    def _reinitialize_and_check(self) -> dict:
        r = self.runtime
        # A factory reset returns to a stopped, paper-only first-launch state.
        # Non-trading supervisors may restart; no bot/instance/live broker does.
        r.controls.stop_all()
        r.watchdog.start()
        r.monitor_runner.start()
        r.daily_tasks.start()
        market_health = r.v2_market_data.verify_binance_usdm()
        if r.instance_manager._instances or r.ledger.get_positions() or r.ledger.get_paper_trades():
            raise RuntimeError("post-reset verification found operational trading records")
        if r.controls.trading_allowed():
            raise RuntimeError("post-reset trading gate is not stopped")
        return {"application": "healthy", "trading": "stopped",
                "market_data_source": "Binance USD-M Futures",
                "binance_usdm": market_health}
