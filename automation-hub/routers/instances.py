"""Trading Instance API — isolated paper engines and instance analytics."""
from __future__ import annotations

import importlib
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional

from services.trading_instances import MAX_ACTIVE_SLOTS_CEILING, WorkerLeaseError

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
    # The API used to cap this at three, matching a manager that capped it at
    # three, over a row that defaulted to one. Concurrency is a capacity
    # decision an operator should be able to make; the manager still refuses
    # anything above MAX_ACTIVE_SLOTS_CEILING.
    max_active_slots: Optional[int] = Field(
        default=None, ge=1, le=MAX_ACTIVE_SLOTS_CEILING)
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


def _assert_selectable(key: str) -> dict:
    """Gate a NEW instance on the registry's lifecycle, not on file existence.

    Deliberately not applied to an instance that already exists: demoting a
    strategy must never stop a running worker or orphan its open paper
    position. Only creation and re-pointing an instance at a different
    strategy go through here.
    """
    from services.strategy_registry import selectable_for_new_instance
    allowed, reason = selectable_for_new_instance(key)
    if not allowed:
        raise HTTPException(400, reason)
    return _catalog(key)


def _manager():
    manager = _wa.instance_manager
    if not manager.store.available:
        raise HTTPException(503, manager.store.error)
    return manager


def _owner(request: Request | None) -> str:
    """The tenant this request may act on.

    Every instance-scoped route resolves this and passes it down. An
    instance_id on its own is a guessable string; without an ownership check
    it would be the only thing standing between one account's session and
    another account's running worker the moment this deployment stops being
    single-owner. Today resolve_tenant() returns the owner for everyone, so
    behaviour is unchanged -- what changes is that the check now exists and is
    tested, instead of being a thing to remember later.
    """
    from services.tenancy import OWNER_TENANT, multi_user_enabled, resolve_tenant
    if request is None or not multi_user_enabled():
        # Single-owner deployment: every caller is the owner, exactly as
        # before. Deliberately NOT app._tenant(), which returns the Supabase
        # UUID even while multi-user is off -- that would re-home every
        # existing instance (they carry the owner tenant) and show an admin an
        # empty list. The tenancy switch stays the one thing that changes this.
        return OWNER_TENANT
    try:
        app_module = importlib.import_module("app")
        return resolve_tenant(app_module._user(request)) or OWNER_TENANT
    except Exception:  # pragma: no cover - direct router consumers
        return OWNER_TENANT


def _owned(manager, instance_id: str, request: Request | None):
    """Resolve an instance the caller actually owns, or 404.

    404 rather than 403 deliberately: a different answer for "exists but is
    not yours" would let one account enumerate another's instance ids.
    """
    try:
        return manager.instance_for(instance_id, _owner(request))
    except KeyError:
        raise HTTPException(404, "Trading instance not found")


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
    # Only PRODUCTION strategies are offered. The dashboard has no list of its
    # own, so this is the whole surface a new Trading Instance can be built
    # from; anything else stays importable for research and for instances that
    # already exist, but cannot be newly selected.
    from services.strategy_registry import all_entries, production_entries
    strategies = []
    for entry in production_entries():
        key = entry.strategy_id
        builtin = str(entry.version or "unversioned")
        strategies.append({"key": key, "label": entry.display_name,
                           "status": entry.lifecycle,
                           "versions": list(dict.fromkeys([builtin, *versions_by_strategy.get(key, [])])),
                           "required_data": list(entry.required_data),
                           "supported_markets": list(entry.supported_markets),
                           "supported_timeframes": list(entry.supported_timeframes)})
    manager = _manager()
    defaults = manager.instance_defaults
    return {
        "symbols": list(SYMBOLS), "timeframes": timeframes,
        "strategies": strategies,
        # The full registry, so an operator can see WHY something is absent
        # from the selector instead of wondering whether it was lost.
        "strategy_registry": [item.public() for item in all_entries()],
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


# Registered ahead of every "/instances/{instance_id}/..." route on purpose.
# FastAPI matches in registration order, so a path parameter declared first
# captures the literal segment "runtime" and answers 404 for these.
@router.get("/instances/runtime/health")
def instance_runtime_health(request: Request = None):  # noqa: B008
    """Process-level answer to "is the backend running my instances?".

    Deliberately not per-instance: the question an operator asks after a
    restart or a login is whether the *runtime* is alive and what it is
    subscribed to, and that cannot be answered by any one instance's row.
    """
    manager = _manager()
    supervisor = getattr(_wa, "instance_supervisor", None)
    hub = getattr(manager, "market_hub", None)
    rows = []
    # Owner-scoped like every sibling route. An unscoped listing would hand one
    # account every instance_id on the deployment, which is exactly the
    # enumeration the 404-not-403 rule elsewhere exists to prevent.
    for inst in manager.owned_instances(_owner(request)):
        rows.append({
            "instance_id": inst.id, "symbol": inst.symbol,
            "timeframe": inst.timeframe, "strategy_id": inst.strategy_key,
            "mode": inst.mode, "state": inst.state,
            "desired_running": inst.desired_running,
            "worker_alive": manager.worker_alive(inst.id),
        })
    return {
        "supervisor": supervisor.status() if supervisor is not None else
                      {"running": False, "detail": "supervisor not configured"},
        "max_active_slots": manager.max_slots,
        "workers": sorted(rows, key=lambda row: row["symbol"]),
        "market_data_channels": hub.channel_report() if hasattr(hub, "channel_report") else [],
    }


@router.get("/instances/runtime/metrics")
def instance_runtime_metrics(request: Request = None):  # noqa: B008
    """Counters that separate "found no setup" from "never evaluated".

    Both produce zero trades. Only these tell them apart.
    """
    from services.instance_metrics import platform_metrics
    return platform_metrics(_manager(), supervisor=getattr(_wa, "instance_supervisor", None),
                            owner_id=_owner(request))


def _start_instance(manager, instance_id: str, *, restart: bool = False,
                    owner_id: Optional[str] = None):
    """Enter instance-first execution without mixing legacy account trades."""
    if _wa.engine.running:
        _wa.engine.stop("Trading Instance started — legacy multi-pair engine disabled to preserve attribution")
        _wa.ledger.log(level="info", stage="instance",
                       message="Legacy multi-pair engine stopped before Trading Instance execution")
    return (manager.restart(instance_id, owner_id=owner_id) if restart
            else manager.start(instance_id, owner_id=owner_id))


@router.get("/instances")
def list_instances(request: Request = None):  # noqa: B008 - FastAPI injects it
    manager = _manager()
    owner = _owner(request)
    rows, positions, trades = manager.snapshot(owner_id=owner)
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
    strategy = _assert_selectable(candidate["default_strategy"])
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
def create_instance(body: InstanceCreate, request: Request = None,  # noqa: B008
                    x_webhook_secret: Optional[str] = Header(default=None)):
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
    strategy = _assert_selectable(strategy_key)
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
            owner_id=_owner(request),
                                 max_open_positions=max_open_positions)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    _wa.ledger.log(level="info", stage="instance", message=f"Instance created: {inst.symbol} {inst.strategy_label} {inst.strategy_version}", symbol=inst.symbol)
    return {"instance": _manager().status(inst.id)}


@router.delete("/instances/{instance_id}")
def delete_instance(instance_id: str, request: Request = None,  # noqa: B008
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    try:
        deleted_id = manager.delete(instance_id, owner_id=_owner(request))
    except KeyError:
        raise HTTPException(404, "Trading instance not found")
    except ValueError as exc:
        # A refused delete carries the route forward with it, so the dashboard
        # can offer the explicit close rather than leaving a dead end.
        detail = {"message": str(exc)}
        try:
            detail["open_positions"] = manager.open_position_disposition(
                instance_id, _owner(request))
        except Exception:  # noqa: BLE001 — the refusal reason is the point
            pass
        raise HTTPException(409, detail)
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
def instance_detail(instance_id: str, request: Request = None):  # noqa: B008
    manager = _manager()
    _owned(manager, instance_id, request)
    return manager.status(instance_id)


@router.patch("/instances/{instance_id}")
def update_instance(instance_id: str, body: InstanceUpdate, request: Request = None,  # noqa: B008
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    _owned(_manager(), instance_id, request)
    try:
        if (body.risk_per_trade_pct is not None
                and body.risk_per_trade_pct > _manager().max_instance_risk_per_trade_pct):
            _field_error("risk_per_trade_pct",
                         f"Risk exceeds the platform ceiling of {_manager().max_instance_risk_per_trade_pct}")
        from services.mtf_policy import ENTRY_HTF
        timeframes = tuple(ENTRY_HTF)
        strategy = _assert_selectable(body.strategy) if body.strategy is not None else None
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


class EventGuardUpdate(BaseModel):
    enabled: bool


def _event_guard():
    guard = getattr(_manager(), "event_guard", None)
    if guard is None:
        raise HTTPException(503, "News blackout is not available on this server")
    return guard


@router.get("/instances/{instance_id}/event-guard")
def instance_event_guard(instance_id: str, request: Request = None):  # noqa: B008
    """Whether this instance sits out high-impact releases, and what the
    economic calendar says right now."""
    _owned(_manager(), instance_id, request)
    return _event_guard().state(instance_id)


@router.patch("/instances/{instance_id}/event-guard")
def update_instance_event_guard(instance_id: str, body: EventGuardUpdate, request: Request = None,  # noqa: B008
                                x_webhook_secret: Optional[str] = Header(default=None)):
    """Switch the news blackout on or off for one instance. Applies from the
    next signal; a running worker is not rebuilt and open positions keep
    their stops and targets."""
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    _owned(manager, instance_id, request)
    state = _event_guard().set(instance_id, body.enabled, by=_initiated_by(request))
    try:
        manager.store.append_engine_log(
            instance_id, level="info",
            message=("News blackout on: no new entries from 30 min before to "
                     f"{state['window']['blackout_after_min']} min after a high-impact release; "
                     "half size in the 2 h before." if body.enabled else "News blackout off."))
    except Exception:  # noqa: BLE001 -- the switch is saved; the log line is a courtesy
        pass
    return state


@router.post("/instances/{instance_id}/simulation-account/restart")
def restart_simulation_account(instance_id: str, body: SimulationAccountRestart,
                               request: Request,
                               x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    if not body.confirm:
        raise HTTPException(400, "Explicit confirmation is required to restart a simulation account")
    _owned(_manager(), instance_id, request)
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
def simulation_sessions(instance_id: str, request: Request = None):  # noqa: B008
    manager = _manager()
    _owned(manager, instance_id, request)
    return {"sessions": manager.store.simulation_sessions(instance_id)}


@router.post("/instances/{instance_id}/{action}")
def instance_action(instance_id: str, action: str, request: Request = None,  # noqa: B008
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    owner = _owner(request)
    _owned(manager, instance_id, request)
    try:
        if action == "start": inst = _start_instance(manager, instance_id, owner_id=owner)
        elif action == "stop": inst = manager.stop(instance_id, owner_id=owner)
        elif action == "pause": inst = manager.pause(instance_id, owner_id=owner)
        elif action == "resume": inst = manager.resume(instance_id, owner_id=owner)
        elif action == "restart": inst = _start_instance(manager, instance_id, restart=True, owner_id=owner)
        else: raise HTTPException(404, "Unknown instance action")
    except KeyError: raise HTTPException(404, "Trading instance not found")
    except WorkerLeaseError as exc: raise HTTPException(409, str(exc))
    except ValueError as exc: raise HTTPException(409, str(exc))
    except RuntimeError as exc: raise HTTPException(503, str(exc))
    return {"instance": manager.status(inst.id)}


@router.get("/instances/{instance_id}/status")
def instance_status(instance_id: str, request: Request = None):  # noqa: B008
    """The truthful four-axis status for one instance.

    Separated from the full detail payload so a status poll does not have to
    pull metrics, performance and the decision journal with it.
    """
    manager = _manager()
    _owned(manager, instance_id, request)
    row = manager.status(instance_id)
    return {key: row[key] for key in (
        "id", "symbol", "strategy_key", "strategy_label", "strategy_version",
        "strategy_lifecycle", "timeframe", "mode", "state", "ui_status",
        "desired_running", "effective_exchange", "effective_instrument_type",
        "runtime_status", "market_status", "market_status_reason",
        "strategy_status", "strategy_status_reason",
        "execution_status", "execution_status_reason",
        "current_blocker", "feed", "subscription", "worker",
        "configuration_revision", "last_error",
    ) if key in row}


@router.get("/instances/{instance_id}/positions")
def instance_positions(instance_id: str, request: Request = None):  # noqa: B008
    manager = _manager()
    inst = _owned(manager, instance_id, request)
    return {"instance_id": instance_id,
            "positions": _wa.ledger.get_positions(
                "open", instance_id=instance_id,
                simulation_session_id=inst.simulation_session_id)}


@router.get("/instances/{instance_id}/orders")
def instance_orders(instance_id: str, request: Request = None):  # noqa: B008
    """Working forward-paper intents and resting strategy limits.

    A forward-paper entry is an intent until a Binance quote fills it, so
    "orders" here means exactly those unfilled intents -- no exchange order
    exists or ever will.
    """
    manager = _manager()
    _owned(manager, instance_id, request)
    pending = manager.store.market_state(instance_id).get("pending_orders_json") or {}
    return {"instance_id": instance_id,
            "forward_paper_intents": pending.get("forward_paper_intents") or {},
            "strategy_limit_intents": pending.get("strategy_limit_intents") or {},
            "quarantined_intents": pending.get("quarantined_intents") or {},
            "execution_mode": "forward_paper", "exchange_routing": False}


@router.get("/instances/{instance_id}/metrics")
def instance_metrics(instance_id: str, request: Request = None):  # noqa: B008
    manager = _manager()
    _owned(manager, instance_id, request)
    row = manager.status(instance_id)
    return {"instance_id": instance_id, "metrics": row["metrics"],
            "performance": row["performance"], "execution": row["execution"],
            "risk": row["risk"], "worker_counts": row["worker_counts"]}


@router.get("/instances/{instance_id}/trades")
def instance_trades(instance_id: str, request: Request = None):  # noqa: B008
    manager = _manager()
    inst = _owned(manager, instance_id, request)
    # Scoped to the instance AND its current simulation session. Without the
    # session scope a restarted paper account still returned the previous
    # session's trades, so the trade list disagreed with the balance and the
    # metrics computed beside it.
    return {"instance_id": instance_id,
            "simulation_session_id": inst.simulation_session_id,
            "trades": _wa.ledger.get_paper_trades(
                instance_id=instance_id,
                simulation_session_id=inst.simulation_session_id)}


@router.get("/instances/{instance_id}/logs")
def instance_logs(instance_id: str, limit: int = 100, request: Request = None):  # noqa: B008
    manager = _manager()
    _owned(manager, instance_id, request)
    limit = max(1, min(limit, 500))
    # Both halves of this instance's record: the trading log and the worker's
    # own lifecycle timeline. The lifecycle events were written to
    # instance_engine_logs and never served, so every structured
    # MARKET_STALE / INSTANCE_RESTORED event was invisible to the operator
    # they were written for.
    return {"logs": _wa.ledger.get_logs(limit, instance_id=instance_id),
            "engine_events": manager.store.engine_logs(instance_id, limit)}


@router.get("/instances/{instance_id}/open-positions")
def instance_open_positions(instance_id: str, request: Request = None):  # noqa: B008
    """What blocks deletion, and the one explicit action that resolves it."""
    manager = _manager()
    _owned(manager, instance_id, request)
    return manager.open_position_disposition(instance_id, _owner(request))


@router.post("/instances/{instance_id}/close-open-positions")
def close_instance_open_positions(instance_id: str, body: SimulationAccountRestart,
                                  request: Request = None,  # noqa: B008
                                  x_webhook_secret: Optional[str] = Header(default=None)):
    """Realise every open paper position at the last observed mark.

    Deliberately explicit and confirmed: it writes real P&L into this
    instance's trade history, so it must never happen as a side effect of
    pressing Delete.
    """
    _wa._check_secret(x_webhook_secret)
    manager = _manager()
    _owned(manager, instance_id, request)
    if not body.confirm:
        raise HTTPException(400, "Explicit confirmation is required to close open "
                                 "paper positions")
    try:
        return manager.close_open_positions(
            instance_id, owner_id=_owner(request), initiated_by=_initiated_by(request))
    except KeyError:
        raise HTTPException(404, "Trading instance not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.get("/instances/{instance_id}/reconciliation")
def instance_reconciliation(instance_id: str, request: Request = None):  # noqa: B008
    """Cross-check this instance's durable records on demand. Read-only."""
    from services.instance_reconciliation import reconcile
    manager = _manager()
    _owned(manager, instance_id, request)
    return reconcile(manager, instance_id).public()
