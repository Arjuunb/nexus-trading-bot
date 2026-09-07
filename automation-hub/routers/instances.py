"""Trading Instance API — isolated paper engines and instance analytics."""
from __future__ import annotations

import importlib
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional

router = APIRouter()


class _WebhookAPIProxy:
    """Resolve legacy application singletons without an import-order cycle.

    ``webhook_api`` mounts this router, while a few focused consumers import the
    router directly.  Eagerly importing the parent module here left
    ``routers.instances`` only partly initialised in the latter case.
    """

    def __getattr__(self, name):
        return getattr(importlib.import_module("webhook_api"), name)

    def __setattr__(self, name, value):
        """Keep test/runtime overrides on the authoritative module.

        Without this delegate, ``monkeypatch.setattr(_wa, ...)`` created state on
        the proxy itself.  That shadow survived module reuse and leaked one
        test's TradingInstanceManager into later tests.
        """
        setattr(importlib.import_module("webhook_api"), name, value)

    def __delattr__(self, name):
        delattr(importlib.import_module("webhook_api"), name)


_wa = _WebhookAPIProxy()


class InstanceCreate(BaseModel):
    symbol: Optional[str] = Field(default=None, min_length=3, max_length=30)
    strategy: Optional[str] = Field(default=None, min_length=1)
    # Omitted means use the immutable catalogue version.  Explicit historical
    # labels remain accepted for existing clients and imported records.
    strategy_version: Optional[str] = Field(default=None, min_length=1, max_length=80)
    timeframe: Optional[str] = Field(default=None, min_length=2, max_length=8)
    risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=0.05)
    capital_allocation: Optional[float] = Field(default=None, gt=0)
    mode: str = Field("trading")
    sizing_mode: str = Field("fixed_starting_equity_percent")
    fixed_position_size: float = Field(0.0, ge=0)
    fixed_quantity: Optional[float] = Field(default=None, ge=0)
    profit_reinvestment: bool = False
    maximum_risk_amount: Optional[float] = Field(default=None, gt=0)
    minimum_equity: Optional[float] = Field(default=None, gt=0)
    entry_mode: Optional[str] = Field(default=None)
    fill_model: Optional[str] = Field(default=None)
    exchange: str = Field("inherit", min_length=2, max_length=30)
    instrument_type: str = Field("spot", min_length=2, max_length=30)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=50)


class InstanceUpdate(BaseModel):
    capital_allocation: Optional[float] = Field(default=None, gt=0)
    risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=0.05)
    sizing_mode: Optional[str] = None
    fixed_position_size: Optional[float] = Field(default=None, ge=0)
    fixed_quantity: Optional[float] = Field(default=None, ge=0)
    profit_reinvestment: Optional[bool] = None
    maximum_risk_amount: Optional[float] = Field(default=None, gt=0)
    minimum_equity: Optional[float] = Field(default=None, gt=0)
    entry_mode: Optional[str] = None
    fill_model: Optional[str] = None
    exchange: Optional[str] = Field(default=None, min_length=2, max_length=30)
    instrument_type: Optional[str] = Field(default=None, min_length=2, max_length=30)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=50)
    strategy: Optional[str] = Field(default=None, min_length=1)
    strategy_version: Optional[str] = Field(default=None, min_length=1, max_length=80)
    timeframe: Optional[str] = Field(default=None, min_length=2, max_length=8)


class PlatformConfig(BaseModel):
    max_active_slots: Optional[int] = Field(default=None, ge=1, le=3)
    max_global_risk_pct: Optional[float] = Field(default=None, ge=0.001, le=1)
    max_global_daily_loss_pct: Optional[float] = Field(default=None, ge=0.001, le=1)
    paper_account_capital: Optional[float] = Field(default=None, gt=0)
    max_instance_risk_per_trade_pct: Optional[float] = Field(default=None, ge=0.001, le=0.05)
    default_symbol: Optional[str] = Field(default=None, min_length=3, max_length=30)
    default_timeframe: Optional[str] = Field(default=None, min_length=2, max_length=8)
    default_strategy: Optional[str] = Field(default=None, min_length=1)
    default_capital: Optional[float] = Field(default=None, gt=0)
    default_risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=0.05)
    default_max_open_positions: Optional[int] = Field(default=None, ge=1, le=50)
    default_entry_mode: Optional[str] = None
    default_fill_model: Optional[str] = None


class SimulationAccountRestart(BaseModel):
    confirm: bool = False


def _field_error(field: str, message: str, status: int = 422):
    raise HTTPException(status, detail={"field": field, "message": message})


def _catalog(key: str) -> dict:
    row = next((s for s in _wa._STRATEGY_CATALOG if s["key"] == key), None)
    if not row:
        raise HTTPException(400, f"Unknown strategy '{key}'")
    return row


def _manager():
    manager = _wa.instance_manager
    if not manager.store.available:
        raise HTTPException(503, manager.store.error)
    return manager


def _initiated_by(request: Request) -> str:
    """Return authenticated identity without logging credentials."""
    try:
        app_module = importlib.import_module("app")
        identity = app_module._user(request)
        if identity:
            return str(identity)
    except Exception:  # pragma: no cover - defensive for direct router consumers
        pass
    return "control-credential operator"


@router.get("/instances/options")
def instance_options():
    """Authoritative options for the instance-creation screen.

    The dashboard deliberately obtains these from the service rather than
    carrying its own list, so an installed strategy/version or supported market
    cannot silently diverge from what the worker can create.
    """
    from data.historical import SYMBOLS
    from services.mtf_policy import ENTRY_HTF
    timeframes = list(ENTRY_HTF)
    versions_by_strategy: dict[str, list[str]] = {}
    version_store = getattr(_wa, "version_store", None)
    if version_store is not None:
        for row in version_store.list():
            key = str(row.get("strategy") or "")
            label = str(row.get("label") or row.get("version") or "")
            if key and label:
                versions_by_strategy.setdefault(key, []).append(label)
    strategies = []
    for row in _wa._STRATEGY_CATALOG:
        key = row["key"]
        builtin = str(row.get("version") or "unversioned")
        strategies.append({"key": key, "label": row["label"],
                           "versions": list(dict.fromkeys([builtin, *versions_by_strategy.get(key, [])])),
                           "supported_timeframes": row.get("supported_timeframes", timeframes)})
    manager = _manager()
    defaults = manager.instance_defaults
    return {
        "symbols": list(SYMBOLS), "timeframes": timeframes,
        "strategies": strategies,
        # Execution choices are server-owned and persisted per instance. New
        # instances default to realistic costs; existing PerfectFill rows remain
        # valid so historical results and restore behaviour do not change.
        "execution_defaults": {"position_sizing_mode": "fixed_starting_equity_percent",
                               "entry_mode": defaults["default_entry_mode"],
                               "fill_model": defaults["default_fill_model"], "leverage": None,
                               "exchange": str(getattr(_wa.settings, "default_exchange", "binance") or "binance").lower(),
                               "instrument_type": "spot",
                               "max_open_positions": defaults["default_max_open_positions"],
                               "max_quick_risk_pct": manager.max_instance_risk_per_trade_pct,
                               "symbol": defaults["default_symbol"],
                               "timeframe": defaults["default_timeframe"],
                               "strategy": defaults["default_strategy"],
                               "capital": defaults["default_capital"],
                               "risk_per_trade_pct": defaults["default_risk_per_trade_pct"]},
        "platform_defaults": dict(defaults),
        "exchanges": [
            {"key": "inherit", "label": "Server default (HUB_EXCHANGE)"},
            {"key": "binance", "label": "Binance Spot"},
            {"key": "kraken", "label": "Kraken Spot"},
            {"key": "coinbase", "label": "Coinbase Spot"},
            {"key": "bybit", "label": "Bybit Spot"},
        ],
        "fill_models": [
            {"key": "RealisticFill", "label": "Realistic — spread, slippage and fees", "recommended": True},
            {"key": "UnifiedFees", "label": "Backtest parity — shared fees and slippage", "recommended": False},
            {"key": "PerfectFill", "label": "Ideal — research comparison only", "recommended": False},
        ],
        "sizing_modes": [
            {"key": "fixed_starting_equity_percent", "label": "Fixed Starting Equity %", "implemented": True},
            {"key": "dynamic_current_equity_percent", "label": "Dynamic Current Equity %", "implemented": True},
            {"key": "fixed_quantity", "label": "Fixed Quantity", "implemented": True},
        ],
        "market_data_mode": "paper_forward_live_only",
    }


def _start_instance(manager, instance_id: str, *, restart: bool = False):
    """Enter instance-first execution without mixing legacy account trades."""
    if _wa.engine.running:
        _wa.engine.stop("Trading Instance started — legacy multi-pair engine disabled to preserve attribution")
        _wa.ledger.log(level="info", stage="instance",
                       message="Legacy multi-pair engine stopped before Trading Instance execution")
    return manager.restart(instance_id) if restart else manager.start(instance_id)


@router.get("/instances")
def list_instances():
    manager = _manager()
    rows, positions, trades = manager.snapshot()
    return {"instances": rows, **manager.platform_status(
        runtime_states=rows, open_positions=positions, instance_trades=trades)}


@router.post("/instances/platform")
def configure_platform(body: PlatformConfig, x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    from data.historical import SYMBOLS
    from services.mtf_policy import ENTRY_HTF
    timeframes = tuple(ENTRY_HTF)
    current = _manager().instance_defaults
    supplied = body.model_dump(exclude_none=True)
    default_keys = {key: value for key, value in supplied.items() if key.startswith("default_")}
    candidate = {**current, **default_keys}
    if candidate["default_symbol"].upper() not in SYMBOLS:
        _field_error("default_symbol", f"Unsupported pair '{candidate['default_symbol'].upper()}'")
    if candidate["default_timeframe"] not in timeframes:
        _field_error("default_timeframe", f"Unsupported timeframe '{candidate['default_timeframe']}'")
    strategy = _catalog(candidate["default_strategy"])
    if candidate["default_timeframe"] not in strategy.get("supported_timeframes", timeframes):
        _field_error("default_timeframe", f"{strategy['label']} does not support {candidate['default_timeframe']}")
    if candidate["default_entry_mode"] not in ("limit", "market"):
        _field_error("default_entry_mode", "Entry mode must be limit or market")
    try:
        from services.fill_model import normalize_fill_model
        candidate["default_fill_model"] = normalize_fill_model(candidate["default_fill_model"])
    except ValueError as exc:
        _field_error("default_fill_model", str(exc))
    ceiling = body.max_instance_risk_per_trade_pct or _manager().max_instance_risk_per_trade_pct
    if float(candidate["default_risk_per_trade_pct"]) > float(ceiling):
        _field_error("default_risk_per_trade_pct", "Default risk cannot exceed the platform instance-risk ceiling")
    try:
        return _manager().configure(max_active_slots=body.max_active_slots,
                                    max_global_risk_pct=body.max_global_risk_pct,
                                    max_global_daily_loss_pct=body.max_global_daily_loss_pct,
                                    max_instance_risk_per_trade_pct=body.max_instance_risk_per_trade_pct,
                                    paper_account_capital=body.paper_account_capital,
                                    defaults=candidate if default_keys else None)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/instances/auto-select")
def auto_select_instance(x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    try:
        manager = _manager()
        # Rank before stopping legacy work, so a no-candidate response is
        # non-disruptive. Starting the selected instance remains exclusive.
        candidate = manager.best_measured_instance()
        slots = manager.platform_status()
        if slots["active_slots"] >= slots["max_active_slots"]:
            raise ValueError("Maximum active trading slots reached")
        if _wa.engine.running:
            _wa.engine.stop("Trading Instance auto-selection started — legacy multi-pair engine disabled")
        instance = manager.start(candidate.id)
        return {"selected": _manager().status(instance.id), "selection": "measured isolated performance"}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/instances")
def create_instance(body: InstanceCreate, x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    defaults = manager.instance_defaults
    symbol = (body.symbol or defaults["default_symbol"]).upper()
    strategy_key = body.strategy or defaults["default_strategy"]
    timeframe = body.timeframe or defaults["default_timeframe"]
    risk_per_trade_pct = (body.risk_per_trade_pct if body.risk_per_trade_pct is not None
                          else defaults["default_risk_per_trade_pct"])
    capital_allocation = (body.capital_allocation if body.capital_allocation is not None
                          else defaults["default_capital"])
    max_open_positions = (body.max_open_positions if body.max_open_positions is not None
                          else defaults["default_max_open_positions"])
    entry_mode = body.entry_mode or defaults["default_entry_mode"]
    fill_model = body.fill_model or defaults["default_fill_model"]
    strategy = _catalog(strategy_key)
    from data.historical import SYMBOLS
    from services.mtf_policy import ENTRY_HTF
    timeframes = tuple(ENTRY_HTF)
    if symbol not in SYMBOLS:
        _field_error("symbol", f"Unsupported pair '{symbol}'", 400)
    if timeframe not in timeframes:
        _field_error("timeframe", f"Unsupported timeframe '{timeframe}'", 400)
    if timeframe not in strategy.get("supported_timeframes", timeframes):
        raise HTTPException(400, f"{strategy['label']} requires a 5m Trading Instance decision timeframe")
    if float(risk_per_trade_pct) > manager.max_instance_risk_per_trade_pct:
        _field_error("risk_per_trade_pct",
                     f"Risk exceeds the platform ceiling of {manager.max_instance_risk_per_trade_pct}")
    try:
        inst = manager.create(symbol=symbol, strategy_key=strategy["key"], strategy_label=strategy["label"],
                                 strategy_version=body.strategy_version or strategy.get("version", "unversioned"), timeframe=timeframe,
                                 risk_per_trade_pct=risk_per_trade_pct,
                                 capital_allocation=capital_allocation, mode=body.mode,
                                 sizing_mode=body.sizing_mode, fixed_position_size=body.fixed_position_size,
                                 fixed_quantity=body.fixed_quantity,
                                 profit_reinvestment=body.profit_reinvestment,
                                 maximum_risk_amount=body.maximum_risk_amount,
                                 minimum_equity=body.minimum_equity,
                                 entry_mode=entry_mode, fill_model=fill_model,
                                 exchange=body.exchange, instrument_type=body.instrument_type,
                                 max_open_positions=max_open_positions)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    _wa.ledger.log(level="info", stage="instance", message=f"Instance created: {inst.symbol} {inst.strategy_label} {inst.strategy_version}", symbol=inst.symbol)
    return {"instance": _manager().status(inst.id)}


@router.delete("/instances/{instance_id}")
def delete_instance(instance_id: str, x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    try:
        deleted_id = manager.delete(instance_id)
    except KeyError:
        raise HTTPException(404, "Trading instance not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        # Preserve an actionable persistence/backend reason for the dashboard
        # instead of collapsing a Supabase outage into an opaque HTTP 500.
        raise HTTPException(503, str(exc))
    _wa.ledger.log(level="info", stage="instance",
                   message=f"Trading Instance deleted: {deleted_id}")
    return {"deleted_instance_id": deleted_id}


@router.get("/instances/leaderboard")
def instance_leaderboard(sort: str = "realized_pnl"):
    return {"rows": _manager().leaderboard(sort), "sort": sort}


@router.get("/instances/{instance_id}")
def instance_detail(instance_id: str):
    try: return _manager().status(instance_id)
    except KeyError: raise HTTPException(404, "Trading instance not found")


@router.patch("/instances/{instance_id}")
def update_instance(instance_id: str, body: InstanceUpdate,
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    try:
        if (body.risk_per_trade_pct is not None
                and body.risk_per_trade_pct > _manager().max_instance_risk_per_trade_pct):
            _field_error("risk_per_trade_pct",
                         f"Risk exceeds the platform ceiling of {_manager().max_instance_risk_per_trade_pct}")
        from services.mtf_policy import ENTRY_HTF
        timeframes = tuple(ENTRY_HTF)
        strategy = _catalog(body.strategy) if body.strategy is not None else None
        if body.timeframe is not None:
            if body.timeframe not in timeframes:
                raise HTTPException(400, f"Unsupported timeframe '{body.timeframe}'")
        if strategy is not None and body.strategy_version is not None:
            valid_versions = next((row["versions"] for row in instance_options()["strategies"]
                                   if row["key"] == strategy["key"]), [])
            if body.strategy_version not in valid_versions:
                raise HTTPException(400, f"Unknown version '{body.strategy_version}' for {strategy['label']}")
        current = _manager().status(instance_id)
        effective_strategy = strategy or _catalog(current["strategy_key"])
        effective_timeframe = body.timeframe or current["timeframe"]
        if effective_timeframe not in effective_strategy.get("supported_timeframes", list(timeframes)):
            raise HTTPException(400, f"{effective_strategy['label']} requires a 5m Trading Instance decision timeframe")
        inst = _manager().update_configuration(
            instance_id, capital_allocation=body.capital_allocation,
            risk_per_trade_pct=body.risk_per_trade_pct, sizing_mode=body.sizing_mode,
            fixed_position_size=body.fixed_position_size, fixed_quantity=body.fixed_quantity,
            profit_reinvestment=body.profit_reinvestment,
            maximum_risk_amount=body.maximum_risk_amount,
            minimum_equity=body.minimum_equity,
            entry_mode=body.entry_mode,
            fill_model=body.fill_model,
            exchange=body.exchange,
            instrument_type=body.instrument_type,
            max_open_positions=body.max_open_positions,
            strategy_key=strategy["key"] if strategy else None,
            strategy_label=strategy["label"] if strategy else None,
            strategy_version=(body.strategy_version or strategy.get("version")) if strategy else None,
            timeframe=body.timeframe)
        return {"instance": _manager().status(inst.id)}
    except KeyError: raise HTTPException(404, "Trading instance not found")
    except ValueError as exc: raise HTTPException(409, str(exc))
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@router.post("/instances/{instance_id}/simulation-account/restart")
def restart_simulation_account(instance_id: str, body: SimulationAccountRestart,
                               request: Request,
                               x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    if not body.confirm:
        raise HTTPException(400, "Explicit confirmation is required to restart a simulation account")
    try:
        return _manager().restart_simulation_account(
            instance_id, initiated_by=_initiated_by(request))
    except KeyError:
        raise HTTPException(404, "Trading instance not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))


@router.get("/instances/{instance_id}/simulation-sessions")
def simulation_sessions(instance_id: str):
    manager = _manager()
    try:
        manager.status(instance_id)
    except KeyError:
        raise HTTPException(404, "Trading instance not found")
    return {"sessions": manager.store.simulation_sessions(instance_id)}


@router.post("/instances/{instance_id}/{action}")
def instance_action(instance_id: str, action: str, x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    try:
        if action == "start": inst = _start_instance(manager, instance_id)
        elif action == "stop": inst = manager.stop(instance_id)
        elif action == "pause": inst = manager.pause(instance_id)
        elif action == "resume": inst = manager.resume(instance_id)
        elif action == "restart": inst = _start_instance(manager, instance_id, restart=True)
        else: raise HTTPException(404, "Unknown instance action")
    except KeyError: raise HTTPException(404, "Trading instance not found")
    except ValueError as exc: raise HTTPException(409, str(exc))
    except RuntimeError as exc: raise HTTPException(503, str(exc))
    return {"instance": manager.status(inst.id)}


@router.get("/instances/{instance_id}/trades")
def instance_trades(instance_id: str):
    manager = _manager()
    try: manager.status(instance_id)
    except KeyError: raise HTTPException(404, "Trading instance not found")
    return {"trades": _wa.ledger.get_paper_trades(instance_id=instance_id)}


@router.get("/instances/{instance_id}/logs")
def instance_logs(instance_id: str, limit: int = 100):
    manager = _manager()
    try: manager.status(instance_id)
    except KeyError: raise HTTPException(404, "Trading instance not found")
    return {"logs": _wa.ledger.get_logs(max(1, min(limit, 500)), instance_id=instance_id)}
