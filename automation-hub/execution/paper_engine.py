"""Paper Execution Engine (Phase 1) — no real broker.

Signal-driven (not bar-driven): opens a position at the alert's entry, closes on
an opposite/close signal, computes P&L, and persists everything to the Ledger
(positions + paper_trades). Realized P&L drives the paper account balance;
unrealized P&L is computed against supplied mark prices.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from data.ledger import DuplicateOrderIntent, Ledger
from bot.tradecore.rmath import gross_r as _gross_r


@dataclass
class FillResult:
    action: str                 # "opened" | "closed" | "noop"
    symbol: str
    side: str
    size: float
    price: float
    pnl: float = 0.0            # net of fees
    position_id: str = ""
    trade_id: str = ""
    fee: float = 0.0            # round-trip commission charged on this fill
    execution_id: str = ""


def _dir(side: str) -> str:
    return "long" if side.upper() in ("BUY", "LONG") else "short"


class PaperExecutionEngine:
    def __init__(self, ledger: Ledger, starting_balance: float = 10_000.0, fill_model=None):
        self.ledger = ledger
        self.starting_balance = starting_balance
        if fill_model is None:
            from services.fill_model import PerfectFill
            fill_model = PerfectFill()
        self.fill_model = fill_model
        self.quality = None   # optional services.execution_quality.ExecutionQuality
        # optional data.account_store.AccountStore — persists the account snapshot
        # (current equity / available / realized) so capital survives a restart.
        self.account_store = None
        self.equity_listener = None  # optional callable(current_realized_equity)
        # H-5: history() is read ~10x per signal (PnL/streak/Kelly/curve).
        # Cache the closed-trade list and invalidate on any write, so one
        # process() call scans the ledger once, not ten times.
        self._hist_cache = None
        # Which strategy's trades these are. Set when a rule spec is deployed;
        # "" means the trade was taken by a built-in strategy or predates
        # attribution, and is reported as the ACCOUNT's rather than any one
        # strategy's — see strategy_history().
        self.strategy_id = ""

    # --------------------------------------------------------------- queries
    def open_position(self, symbol: str) -> Optional[dict]:
        for p in self.ledger.get_positions("open"):
            if p["symbol"] == symbol:
                return p
        return None

    def positions(self) -> list[dict]:
        return self.ledger.get_positions("open")

    def history(self) -> list[dict]:
        if self._hist_cache is None:
            self._hist_cache = [t for t in self.ledger.get_paper_trades()
                                if t["status"] == "closed"]
        return self._hist_cache

    def strategy_history(self, strategy_id: str) -> list[dict]:
        """Closed trades belonging to ONE strategy.

        Trades written before attribution carry an empty strategy_id and are
        deliberately excluded: counting them would credit this strategy with a
        record it did not produce."""
        if not strategy_id:
            return []
        return [t for t in self.history() if t.get("strategy_id") == strategy_id]

    def _invalidate_history(self) -> None:
        self._hist_cache = None

    def update_stop(self, symbol: str, stop: float) -> int:
        """Persist a new stop on the OPEN position for a symbol (manual on-chart
        adjust) through this engine's own ledger. Returns rows updated."""
        return self.ledger.update_position_stop(symbol=symbol, stop=stop)

    def update_management(self, symbol: str, *, stop=None, target=None,
                          management=None) -> int:
        """Persist the complete live trade plan through the scoped ledger."""
        return self.ledger.update_position_management(
            symbol=symbol, stop=stop, target=target, management=management)

    def realized_pnl(self) -> float:
        return sum((t.get("pnl") or 0.0) for t in self.history())

    def gross_realized_pnl(self) -> float:
        """Realized P&L before fees; legacy ``pnl`` remains net of fees."""
        return self.realized_pnl() + self.fees_paid()

    def current_realized_equity(self) -> float:
        """Authoritative compounding basis (starting capital + net closes)."""
        return self.balance()

    def balance(self) -> float:
        return self.starting_balance + self.realized_pnl()

    def unrealized_pnl(self, marks: dict[str, float]) -> float:
        total = 0.0
        for p in self.positions():
            mark = marks.get(p["symbol"])
            if mark is None:
                continue
            total += self._pnl(p["side"], p["size"], p["entry"], mark)
        return total

    def equity(self, marks: Optional[dict[str, float]] = None) -> float:
        return self.balance() + (self.unrealized_pnl(marks) if marks else 0.0)

    def open_notional(self) -> float:
        return sum((p["size"] * p["entry"]) for p in self.positions())

    def available_balance(self) -> float:
        """Funds not committed to open positions."""
        return self.balance() - self.open_notional()

    def _persist_account_snapshot(self) -> None:
        """Save the account state to the persistent store so it survives a
        backend restart. Never raises into the trading path."""
        realized_equity = self.balance()
        if self.equity_listener is not None:
            try:
                self.equity_listener(realized_equity)
            except Exception:  # noqa: BLE001 — observability must not block trading
                pass
        if self.account_store is None:
            return
        try:
            self.account_store.update_snapshot(
                current_equity=realized_equity,
                available_balance=self.available_balance(),
                realized_pnl=self.realized_pnl())
        except Exception:  # noqa: BLE001 — persistence must never block trading
            pass

    # --------------------------------------------------------------- actions
    @staticmethod
    def _execution_id(action: str, supplied: str = "") -> str:
        value = str(supplied or "").strip()
        return value or f"paper:{action.lower()}:{uuid.uuid4().hex}"

    def _reject_unaffordable(self, symbol: str, size: float, entry: float):
        """Reject an entry the paper account cannot fund. None means allowed."""
        try:
            notional = abs(float(size)) * abs(float(entry))
        except (TypeError, ValueError):
            return FillResult("rejected", symbol, "long", 0.0, 0.0)
        if notional <= 0:
            return None
        available = self.available_balance()
        if notional > available + 1e-9:
            self.ledger.log(
                level="warning", stage="execution", symbol=symbol,
                message=(f"{symbol} entry rejected: notional {notional:.2f} exceeds "
                         f"available paper capital {available:.2f}"))
            return FillResult("rejected", symbol, "long", 0.0, float(entry))
        return None

    def open(self, *, symbol: str, side: str, size: float, entry: float,
             stop: Optional[float], target: Optional[float] = None,
             alert_id: str = "", maker: bool = False,
             sizing_context: Optional[dict] = None) -> FillResult:
        direction = _dir(side)
        rejection = self._reject_unaffordable(symbol, size, entry)
        if rejection is not None:
            return FillResult("rejected", symbol, direction, 0.0, float(entry))
        execution_id = self._execution_id("OPEN", alert_id)
        # route the entry through the fill model (price/size/rejection);
        # maker fills (resting limits) execute at the limit price exactly
        action = "buy" if direction == "long" else "sell"
        f = self.fill_model.apply(action, entry, size, maker=maker,
                                  execution_id=alert_id)
        if f["rejected"] or f["size"] <= 0:
            return FillResult("rejected", symbol, direction, 0.0, entry)
        if self.quality is not None:
            self.quality.record(symbol=symbol, side=action, intended=entry,
                                filled=f["price"], kind="entry", maker=maker)
        entry, size = f["price"], f["size"]
        entry_sizing = dict(sizing_context or {})
        if stop is not None:
            entry_sizing["risk_amount_at_entry"] = abs(entry - float(stop)) * size
        initial_management = {}
        if stop is not None and target is not None and stop != entry:
            initial_management = {
                "side": direction, "entry": entry, "stop": float(stop),
                "target": float(target), "risk": abs(entry - float(stop)),
                "be": False, "scaled": False, "best": entry, "age": 0,
                "mfe": entry, "mae": entry,
            }
        trade_row = {
            "alert_id": alert_id, "symbol": symbol, "side": direction,
            "size": size, "entry": entry, "stop": stop, "target": target,
            "strategy_id": self.strategy_id,
            **entry_sizing,
        }
        atomic_open = getattr(self.ledger, "open_position_and_trade", None)
        if not callable(atomic_open):
            raise RuntimeError("Ledger does not support atomic position/trade open")
        pid, tid = atomic_open(
            position={
                "symbol": symbol, "side": direction, "size": size,
                "entry": entry, "stop": stop, "target": target,
                "management": initial_management,
            },
            trade=trade_row,
            execution_id=execution_id,
        )
        self._invalidate_history()
        return FillResult("opened", symbol, direction, size, entry, 0.0, pid, tid,
                          execution_id=execution_id)

    def reduce(self, *, symbol: str, exit_price: float, fraction: float,
               execution_id: str = "") -> FillResult:
        """Partial close (scale-out): realize P&L on ``fraction`` of the position
        and keep the remainder open at the same entry/stop. Implemented as
        close-then-reopen so every Ledger backend works unchanged."""
        pos = self.open_position(symbol)
        if pos is None or not (0.0 < fraction < 1.0):
            return FillResult("noop", symbol, "", 0.0, exit_price)
        closed_size = pos["size"] * fraction
        f = self.fill_model.apply("sell" if pos["side"] == "long" else "buy",
                                  exit_price, closed_size,
                                  allow_reject=False, allow_partial=False)
        exit_price = f["price"]
        gross = self._pnl(pos["side"], closed_size, pos["entry"], exit_price)
        fee = self._round_trip_fee(closed_size, pos["entry"], exit_price)
        pnl = gross - fee          # realized P&L on the closed fraction, net of fees
        equity_before_close = self.balance()
        rr = self._rr(pos, exit_price)
        remainder = pos["size"] - closed_size
        management = pos.get("management_json") or {}
        if isinstance(management, str):
            try:
                management = json.loads(management)
            except (TypeError, ValueError):
                management = {}
        open_trade = next((t for t in self.ledger.get_paper_trades()
                           if t["symbol"] == symbol and t["status"] == "open"), None)
        if open_trade is None:
            raise RuntimeError(f"Paper ledger invariant failed: {symbol} position has no open trade")
        remainder_trade = {
            "alert_id": "", "symbol": symbol, "side": pos["side"],
            "size": remainder, "entry": pos["entry"], "stop": pos.get("stop"),
            "target": pos.get("target"),
            "strategy_id": self.strategy_id,
            **({key: open_trade.get(key) for key in (
                "sizing_mode", "sizing_engine_version", "risk_basis_at_entry",
                "risk_pct_at_entry", "risk_amount_at_entry", "equity_before_trade",
            )} if open_trade else {}),
        }
        atomic_reduce = getattr(self.ledger, "reduce_position_and_trade", None)
        if not callable(atomic_reduce):
            raise RuntimeError("Ledger does not support atomic paper position reduction")
        execution_id = self._execution_id("REDUCE", execution_id)
        atomic_reduce(
            position=pos, trade_id=open_trade["id"],
            remainder_position={
                "symbol": symbol, "side": pos["side"], "size": remainder,
                "entry": pos["entry"], "stop": pos.get("stop"),
                "target": pos.get("target"), "management": management,
            },
            remainder_trade=remainder_trade, exit_price=exit_price, pnl=pnl,
            rr=rr, closed_size=closed_size, fees=fee,
            equity_after_close=equity_before_close + pnl,
            execution_id=execution_id,
        )
        self._invalidate_history()
        self._persist_account_snapshot()
        return FillResult("reduced", symbol, pos["side"], closed_size, exit_price,
                          pnl, pos["id"], fee=fee, execution_id=execution_id)

    def close(self, *, symbol: str, exit_price: float,
              execution_id: str = "") -> FillResult:
        pos = self.open_position(symbol)
        if pos is None:
            return FillResult("noop", symbol, "", 0.0, exit_price)
        # exits cross the spread the other way; never reject/partial an exit
        action = "sell" if pos["side"] == "long" else "buy"
        f = self.fill_model.apply(action, exit_price, pos["size"],
                                  allow_reject=False, allow_partial=False)
        if self.quality is not None:
            self.quality.record(symbol=symbol, side=action, intended=exit_price,
                                filled=f["price"], kind="exit")
        exit_price = f["price"]
        gross = self._pnl(pos["side"], pos["size"], pos["entry"], exit_price)
        fee = self._round_trip_fee(pos["size"], pos["entry"], exit_price)
        pnl = gross - fee          # realized P&L is net of commission
        equity_before_close = self.balance()
        rr = self._rr(pos, exit_price)
        open_trade = next((t for t in self.ledger.get_paper_trades()
                           if t["symbol"] == symbol and t["status"] == "open"), None)
        if open_trade is None:
            raise RuntimeError(
                f"Paper ledger invariant failed: {symbol} position has no open trade; "
                "execution is fail-closed pending operator reconciliation")
        else:
            atomic_close = getattr(self.ledger, "close_position_and_trade", None)
            if not callable(atomic_close):
                raise RuntimeError("Ledger does not support atomic paper close")
            execution_id = self._execution_id("CLOSE", execution_id)
            atomic_close(
                position_id=pos["id"], trade_id=open_trade["id"],
                exit_price=exit_price, pnl=pnl, rr=rr, fees=fee,
                realized_pnl=pnl, equity_after_close=equity_before_close + pnl,
                execution_id=execution_id,
            )
        self._invalidate_history()
        self._persist_account_snapshot()
        return FillResult("closed", symbol, pos["side"], pos["size"], exit_price,
                          pnl, pos["id"], fee=fee, execution_id=execution_id)

    # --------------------------------------------------------------- helpers
    def _fee_rate(self, *, maker: bool = False) -> float:
        """Commission fraction from the fill model (0 for PerfectFill)."""
        fn = getattr(self.fill_model, "fee_pct", None)
        return fn(maker=maker) if fn else 0.0

    def _round_trip_fee(self, size: float, entry: float, exit_price: float) -> float:
        """Commission for a full round trip (entry + exit notional). Taker rate
        both sides — a conservative paper assumption that never understates cost.
        Zero when the fill model charges no fee, so ideal-fill behaviour is
        unchanged."""
        rate = self._fee_rate(maker=False)
        if rate <= 0:
            return 0.0
        return rate * size * (abs(entry) + abs(exit_price))

    def fees_paid(self) -> float:
        """Total commission booked across all closed trades (recomputed from the
        round-trip notional), for transparency in the account/analytics view."""
        persisted = sum(float(t.get("fees") or 0) for t in self.history())
        if persisted > 0:
            return round(persisted, 8)
        rate = self._fee_rate(maker=False)
        if rate <= 0:
            return 0.0
        total = 0.0
        for t in self.history():
            entry, exit_price, size = t.get("entry"), t.get("exit"), t.get("size")
            if entry and exit_price and size:
                total += rate * size * (abs(entry) + abs(exit_price))
        return round(total, 8)

    @staticmethod
    def _pnl(direction: str, size: float, entry: float, exit_price: float) -> float:
        return (exit_price - entry) * size if direction == "long" else (entry - exit_price) * size

    @staticmethod
    def _rr(pos: dict, exit_price: float) -> float:
        """GROSS R (before costs) — the canonical tradecore definition. Fees are
        booked separately against realized P&L (see _round_trip_fee), so this
        stays the pre-cost figure it has always been."""
        stop = pos.get("stop")
        if not stop:
            return 0.0
        risk = abs(pos["entry"] - stop)
        if risk <= 0:
            return 0.0
        return round(_gross_r(pos["entry"], exit_price, risk, pos["side"]), 3)


class ForwardPaperExecutionEngine(PaperExecutionEngine):
    """Paper executor whose entries fill only from a later public quote.

    The strategy/risk pipeline remains synchronous, but an approved entry is
    persisted in this adapter as an INTENT.  The shared Binance USD-M market
    hub later calls :meth:`process_quote`; no exchange client exists on this
    path. Protective closes continue to use the normal fail-safe close method.
    """

    deferred_entries = True

    def __init__(self, *args, initial_intents: Optional[dict] = None,
                 intents_listener=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._intent_lock = threading.RLock()
        self._intents: dict[str, dict] = dict(initial_intents or {})
        self.intents_listener = intents_listener

    @staticmethod
    def _utc(value: object) -> datetime:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("forward-paper intent requires a UTC decision timestamp") from exc
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)

    def _checkpoint_intents(self) -> None:
        if self.intents_listener is not None:
            self.intents_listener(self.pending_intents())

    def pending_intents(self) -> dict:
        with self._intent_lock:
            return json.loads(json.dumps(self._intents))

    def open(self, *, symbol: str, side: str, size: float, entry: float,
             stop: Optional[float], target: Optional[float] = None,
             alert_id: str = "", maker: bool = False,
             sizing_context: Optional[dict] = None) -> FillResult:
        context = dict(sizing_context or {})
        decision_timestamp = context.get("decision_timestamp")
        self._utc(decision_timestamp)
        # A simulated account cannot spend money it does not have. The sizing
        # pipeline normally keeps an entry well inside the balance, but the
        # execution engine itself had no check at all, so any path that
        # bypasses sizing -- a fixed-quantity configuration, an intent
        # recovered from a previous process, a direct call -- could park an
        # order worth more than the account and drive available capital
        # arbitrarily negative. Refusing here is fail-closed and does not
        # touch how any strategy sizes a trade.
        rejection = self._reject_unaffordable(symbol, size, entry)
        if rejection is not None:
            return rejection
        with self._intent_lock:
            existing = self._intents.get(symbol)
            if existing is not None:
                return FillResult(
                    "intent", symbol, _dir(existing["side"]),
                    float(existing["size"]), float(existing["entry"]),
                    execution_id=str(existing["alert_id"]),
                )
            self._intents[symbol] = {
                "symbol": symbol, "side": side, "size": float(size),
                "entry": float(entry), "stop": stop, "target": target,
                "alert_id": alert_id, "maker": bool(maker),
                "order_timestamp": datetime.now(timezone.utc).isoformat(),
                "decision_timestamp": str(decision_timestamp),
                "sizing_context": context,
            }
        self._checkpoint_intents()
        return FillResult("intent", symbol, _dir(side), float(size), float(entry),
                          execution_id=self._execution_id("INTENT", alert_id))

    def process_quote(self, quote: dict) -> list[FillResult]:
        """Fill eligible intents from the first complete later bid/ask/mark."""
        try:
            bid, ask, mark = (float(quote[key]) for key in ("bid", "ask", "mark"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("forward-paper quote requires bid, ask and mark") from exc
        if bid <= 0 or ask < bid or mark <= 0:
            raise ValueError("forward-paper quote is invalid")
        received = self._utc(quote.get("received_at"))
        fills: list[FillResult] = []
        recovered = False
        with self._intent_lock:
            for symbol, intent in list(self._intents.items()):
                # Atomic ledger open may have committed just before a process
                # crash and before the intent checkpoint cleared. Reconcile
                # that durable position instead of ever opening it twice.
                if self.open_position(symbol) is not None:
                    self._intents.pop(symbol, None)
                    recovered = True
                    continue
                if received <= self._utc(intent["decision_timestamp"]):
                    continue
                side = str(intent["side"]).upper()
                reference = ask if side in ("BUY", "LONG") else bid
                if intent["maker"]:
                    limit = float(intent["entry"])
                    crossed = (reference <= limit if side in ("BUY", "LONG")
                               else reference >= limit)
                    if not crossed:
                        continue
                    reference = limit
                context = {
                    **dict(intent.get("sizing_context") or {}),
                    "fill_timestamp": received.isoformat(),
                    "fill_bid": bid, "fill_ask": ask, "fill_mark": mark,
                    "market_data_source": quote.get("market_data_source")
                    or quote.get("source") or "Binance USD-M public WebSocket",
                    "candle_id": quote.get("candle_id"),
                }
                fill = super().open(
                    symbol=symbol, side=intent["side"], size=float(intent["size"]),
                    entry=reference, stop=intent.get("stop"),
                    target=intent.get("target"), alert_id=intent.get("alert_id", ""),
                    maker=bool(intent.get("maker")), sizing_context=context,
                )
                if fill.action == "opened":
                    commission = (self._fee_rate(maker=bool(intent.get("maker")))
                                  * fill.size * fill.price)
                    evidence = {
                        **context,
                        "signal_timestamp": context.get("signal_timestamp")
                        or intent["decision_timestamp"],
                        "decision_timestamp": intent["decision_timestamp"],
                        "order_timestamp": intent.get("order_timestamp"),
                        "fill_timestamp": received.isoformat(),
                        "signal_price": context.get("signal_price") or intent["entry"],
                        "requested_price": intent["entry"],
                        "fill_price": fill.price,
                        "spread": ask - bid,
                        "slippage": abs(fill.price - reference),
                        "commission": commission, "funding": 0.0,
                        "stop_loss": intent.get("stop"),
                        "take_profit": intent.get("target"),
                        "risk_amount": (abs(fill.price - float(intent["stop"])) * fill.size
                                        if intent.get("stop") is not None else None),
                        "position_size": fill.size,
                        "strategy": context.get("strategy") or self.strategy_id,
                        "strategy_version": context.get("strategy_version"),
                        "symbol": symbol, "timeframe": context.get("timeframe"),
                        "account_id": context.get("account_id"),
                        "execution_engine": "INSTANCE",
                        "candle_id": context.get("candle_id") or quote.get("candle_id"),
                        "blocker": "NONE", "paper_only": True,
                        "real_execution_allowed": False,
                    }
                    try:
                        self.ledger.insert_webhook_event(
                            alert_id=f"{intent.get('alert_id', '')}:fill:{received.isoformat()}",
                            symbol=symbol, side=intent["side"], entry=fill.price,
                            stop=intent.get("stop"), payload=evidence, status="accepted",
                        )
                    except DuplicateOrderIntent:
                        # This exact quote already filled this intent. Clearing
                        # the intent below is still correct and idempotent; a
                        # second fill row is not.
                        pass
                    self._intents.pop(symbol, None)
                    fills.append(fill)
        if fills or recovered:
            self._checkpoint_intents()
        return fills
