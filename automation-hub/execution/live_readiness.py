"""Live-exchange readiness: broker factory, startup reconciliation, checklist.

Three things every bot needs BEFORE its first real order, exposed honestly:

    make_live_broker()   env-driven factory. Testnet is the DEFAULT — real
                         trading requires explicitly setting HUB_TESTNET=0
                         on top of providing API keys.
    reconcile_startup()  compare the bot's ledger against what the exchange
                         actually holds, and say exactly what differs —
                         positions the bot forgot, positions it remembers
                         that the exchange doesn't have, size mismatches.
    live_readiness()     a checklist (keys, ccxt, mode, connectivity, symbol
                         rules) the dashboard can show. Anything missing shows
                         as not ready — no pretending.

Environment:
    HUB_EXCHANGE     ccxt exchange id (default "binance")
    HUB_EXCHANGE_API_KEY      exchange API key
    HUB_EXCHANGE_API_SECRET   exchange API secret
    HUB_ORDER_STATE_DB        durable execution state database
    HUB_TESTNET      "0" to trade real (default is testnet/sandbox)
"""
from __future__ import annotations

import os
from typing import Optional


# ---------------------------------------------------------------- factory
def make_live_broker():
    """Build a CCXTBroker from env. Raises with a clear message when the
    environment isn't ready — callers surface it instead of guessing."""
    if os.environ.get("HUB_ENABLE_EXTERNAL_LIVE", "0").lower() not in (
            "1", "true", "yes", "on"):
        raise RuntimeError("HUB_ENABLE_EXTERNAL_LIVE is disabled")
    exchange_id = os.environ.get("HUB_EXCHANGE", "binance")
    key, secret = exchange_credentials(exchange_id)
    if not key or not secret:
        raise RuntimeError("No exchange key attached and HUB_EXCHANGE_API_KEY / "
                           "HUB_EXCHANGE_API_SECRET not set — cannot connect to an exchange")
    if not is_testnet():
        # Real money: the key must not be able to move funds out, whichever
        # way it arrived (the vault checked it on attach; the environment
        # never did). Testnet keys have no withdrawal endpoint to ask.
        from services import key_scope
        try:
            scope = key_scope.check(exchange_id, key, secret)
        except key_scope.ScopeCheckUnavailable as exc:
            raise RuntimeError(f"Exchange key scope could not be confirmed: {exc}") from None
        if not scope["allowed"]:
            raise RuntimeError("Exchange key refused: it " + "; it ".join(scope["refusals"]))
    from config import DATA_DIR
    state_path = os.environ.get("HUB_ORDER_STATE_DB", str(DATA_DIR / "execution_orders.db"))
    from bot.brokers.ccxt_broker import CCXTBroker
    return CCXTBroker(
        exchange_id=exchange_id,
        api_key=key, api_secret=secret,
        sandbox=os.environ.get("HUB_TESTNET", "1") != "0",
        state_path=state_path,
    )


def exchange_credentials(exchange_id: str) -> tuple[str, str]:
    """The owner's active vault key for the venue (services/key_vault.py),
    else the environment pair. Decrypted in memory for the caller only."""
    try:
        from services.key_vault import default_vault
        from services.tenancy import OWNER_TENANT
        pair = default_vault().load_active(OWNER_TENANT, exchange_id)
        if pair:
            return pair
    except Exception:  # noqa: BLE001 -- an unreadable vault falls back to env, never to a guess
        pass
    return (os.environ.get("HUB_EXCHANGE_API_KEY", ""),
            os.environ.get("HUB_EXCHANGE_API_SECRET", ""))


def is_testnet() -> bool:
    return os.environ.get("HUB_TESTNET", "1") != "0"


# ---------------------------------------------------------- reconciliation
def reconcile_startup(ledger_positions: list[dict],
                      broker_positions: list,
                      tol: float = 1e-9) -> dict:
    """Compare the bot's open positions (ledger dicts with symbol/side/size)
    against the exchange's (bot.types.Position: signed qty). Pure — inject
    both sides. Returns an actionable report; ``clean`` is True only when the
    two views agree exactly.
    """
    def norm(sym: str) -> str:
        return (sym or "").upper().replace("/", "").replace("-", "")

    local = {}
    for p in ledger_positions:
        signed = p["size"] if p.get("side") != "short" else -p["size"]
        local[norm(p["symbol"])] = local.get(norm(p["symbol"]), 0.0) + signed
    remote = {}
    for p in broker_positions:
        qty = getattr(p, "qty", None)
        sym = getattr(p, "symbol", None)
        if qty is None and isinstance(p, dict):       # tolerate dicts too
            qty, sym = p.get("qty", 0.0), p.get("symbol", "")
        remote[norm(sym)] = remote.get(norm(sym), 0.0) + float(qty or 0.0)

    missing_on_exchange = []   # bot remembers it; exchange doesn't have it
    missing_locally = []       # exchange holds it; bot doesn't know about it
    size_mismatch = []
    matched = []
    for sym, lqty in local.items():
        rqty = remote.get(sym)
        if rqty is None or abs(rqty) <= tol:
            missing_on_exchange.append({"symbol": sym, "local_qty": lqty})
        elif abs(lqty - rqty) > max(tol, 1e-6 * abs(lqty)):
            size_mismatch.append({"symbol": sym, "local_qty": lqty, "exchange_qty": rqty})
        else:
            matched.append(sym)
    for sym, rqty in remote.items():
        if sym not in local and abs(rqty) > tol:
            missing_locally.append({"symbol": sym, "exchange_qty": rqty})

    return {
        "clean": not (missing_on_exchange or missing_locally or size_mismatch),
        "matched": matched,
        "missing_on_exchange": missing_on_exchange,
        "missing_locally": missing_locally,
        "size_mismatch": size_mismatch,
    }


# -------------------------------------------------------------- readiness
def live_readiness(broker=None, symbols: Optional[list[str]] = None) -> dict:
    """Checklist for going live. Every check reports pass/fail + detail;
    ``ready`` is True only when all blocking checks pass. Network checks run
    only when a broker is supplied (or constructible from env)."""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str, blocking: bool = True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "blocking": blocking})

    try:
        import ccxt  # noqa: F401
        check("ccxt installed", True, "ccxt available")
    except ImportError:
        check("ccxt installed", False, "pip install ccxt")

    has_keys = all(exchange_credentials(os.environ.get("HUB_EXCHANGE", "binance")))
    check("api keys", has_keys, "exchange key attached or set in the environment"
          if has_keys else "no exchange key attached and HUB_EXCHANGE_API_KEY / "
                           "HUB_EXCHANGE_API_SECRET not set")
    enabled = os.environ.get("HUB_ENABLE_EXTERNAL_LIVE", "0").lower() in (
        "1", "true", "yes", "on")
    check("external live feature flag", enabled, "explicitly enabled" if enabled else "disabled")
    check("testnet mode", True,
          "sandbox/testnet (set HUB_TESTNET=0 for real trading)" if is_testnet()
          else "REAL trading mode", blocking=False)

    if broker is None and has_keys:
        try:
            broker = make_live_broker()
        except Exception as e:  # noqa: BLE001
            check("broker init", False, str(e))
    if broker is not None:
        try:
            acct = broker.get_account()
            check("exchange connectivity", True,
                  f"account reachable (equity {acct.equity:.2f})")
        except Exception as e:  # noqa: BLE001
            check("exchange connectivity", False, f"account fetch failed: {e}")
        for sym in (symbols or []):
            try:
                r = broker.rules_for(sym)
                ok = r.step_size > 0 or r.tick_size > 0 or r.min_notional > 0
                check(f"symbol rules {sym}", ok,
                      f"lot {r.step_size} tick {r.tick_size} minNotional {r.min_notional}"
                      if ok else "no filters returned")
            except Exception as e:  # noqa: BLE001
                check(f"symbol rules {sym}", False, str(e))
    else:
        check("exchange connectivity", False, "no broker (keys missing)", blocking=True)

    ready = all(c["ok"] for c in checks if c["blocking"])
    return {"ready": ready, "testnet": is_testnet(), "checks": checks}
