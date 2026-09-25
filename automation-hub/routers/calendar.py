"""App-wide realized P&L calendar API (services/pnl_calendar.py).

Every number comes from the one aggregation service; this router only resolves
the request (timezone, display currency, filters) and reports what it used.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import csv
import io

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

import webhook_api as _wa
from services.pnl_calendar import EXPORT_COLUMNS, CalendarError, Filters, resolve_zone

router = APIRouter()


def _saved_settings(request: Request) -> dict:
    """The signed-in user's saved Settings Center blob, or {}."""
    try:
        import app as _app
        user = _app._user(request)
        return (_app.store.get_user_settings(user, "settings-center") or {}) if user else {}
    except Exception:  # noqa: BLE001 -- preferences are optional; defaults apply
        return {}


def _resolve(request: Request, tz: Optional[str], currency: Optional[str]):
    saved = _saved_settings(request)
    zone_name = (tz or (saved.get("region") or {}).get("timezone")
                 or (saved.get("profile") or {}).get("timezone") or "UTC")
    display = (currency or (saved.get("portfolio") or {}).get("baseCurrency") or "USDT").upper()
    return resolve_zone(zone_name), display


def _conversion(currencies: list[str], display: str) -> dict:
    others = [c for c in currencies if c != display]
    return {"display_currency": display, "needed": bool(others), "available": False,
            "unconverted": others,
            "note": ("" if not others else
                     f"No exchange-rate source is configured, so amounts in {', '.join(others)} are shown "
                     f"in their settlement currency and never added to {display}.")}


def _filters(source, instance, strategy, symbol, timeframe) -> Filters:
    return Filters(source=source or None, instance_id=instance or None, strategy=strategy or None,
                   symbol=symbol or None, timeframe=timeframe or None)


@router.get("/calendar/month")
def calendar_month(request: Request, year: int, month: int, tz: Optional[str] = None,
                   currency: Optional[str] = None, source: Optional[str] = None,
                   instance: Optional[str] = None, strategy: Optional[str] = None,
                   symbol: Optional[str] = None, timeframe: Optional[str] = None, fresh: bool = False,
                   x_webhook_secret: Optional[str] = Header(default=None)):
    """Every day of one month: realized P&L per currency, closed trades, wins
    and losses, plus the month's summary. Realized (closed) P&L only."""
    _wa._check_secret(x_webhook_secret)
    try:
        zone, display = _resolve(request, tz, currency)
        data = _wa.pnl_calendar.month(year=year, month=month, tz=zone,
                                      filters=_filters(source, instance, strategy, symbol, timeframe),
                                      fresh=fresh)
    except CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    return {**data, "conversion": _conversion(data["currencies"], display)}


@router.get("/calendar/day")
def calendar_day(request: Request, date_: str, tz: Optional[str] = None, currency: Optional[str] = None,
                 source: Optional[str] = None, instance: Optional[str] = None,
                 strategy: Optional[str] = None, symbol: Optional[str] = None,
                 timeframe: Optional[str] = None, fresh: bool = False,
                 x_webhook_secret: Optional[str] = Header(default=None)):
    """One day in detail: source, strategy and time-of-day breakdowns and the
    closed trades behind the number. ``date_`` is YYYY-MM-DD in the calendar timezone."""
    _wa._check_secret(x_webhook_secret)
    try:
        day = date.fromisoformat(date_)
    except ValueError:
        raise HTTPException(400, "date_ must be YYYY-MM-DD.") from None
    try:
        zone, display = _resolve(request, tz, currency)
        data = _wa.pnl_calendar.day(day=day, tz=zone,
                                    filters=_filters(source, instance, strategy, symbol, timeframe),
                                    fresh=fresh)
    except CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    return {**data, "conversion": _conversion(data["currencies"], display)}


@router.get("/calendar/export.csv")
def calendar_export(request: Request, start: str, end: str, tz: Optional[str] = None,
                    source: Optional[str] = None, instance: Optional[str] = None,
                    strategy: Optional[str] = None, symbol: Optional[str] = None,
                    timeframe: Optional[str] = None, fresh: bool = False,
                    x_webhook_secret: Optional[str] = Header(default=None)):
    """Every realization closed from ``start`` to ``end`` (YYYY-MM-DD, inclusive,
    in the calendar timezone) as CSV: one row per exit, exact amounts, each in
    its own currency, nothing converted or summed."""
    _wa._check_secret(x_webhook_secret)
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "start and end must be YYYY-MM-DD.") from None
    try:
        zone, _ = _resolve(request, tz, None)
        rows = _wa.pnl_calendar.export(start=first, end=last, tz=zone,
                                       filters=_filters(source, instance, strategy, symbol, timeframe),
                                       fresh=fresh)
    except CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    writer.writerows(rows)
    name = f"realized-pnl_{first.isoformat()}_{last.isoformat()}.csv"
    return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store", "X-Calendar-Timezone": zone.key})


@router.get("/calendar/options")
def calendar_options(request: Request, tz: Optional[str] = None, currency: Optional[str] = None,
                     x_webhook_secret: Optional[str] = Header(default=None)):
    """Filter values that exist in the data, and the resolved timezone/currency."""
    _wa._check_secret(x_webhook_secret)
    try:
        zone, display = _resolve(request, tz, currency)
    except CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    return {**_wa.pnl_calendar.options(), "timezone": zone.key, "display_currency": display}
