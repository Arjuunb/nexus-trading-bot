"""The public status feed behind the /status page (services/status_monitor.py).

Unauthenticated by design: a status page has to answer the people who cannot
sign in. It carries component states, daily uptime and incident times with
details written by the monitor in plain language -- never exception text,
hostnames, account data or secrets -- and is cached for 30 seconds, so it is
cheap to poll.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import webhook_api as _wa

router = APIRouter()


@router.get("/status/public")
def public_status():
    view = _wa.status_monitor.public_view()
    return JSONResponse(view, headers={"Cache-Control": "public, max-age=30"})
