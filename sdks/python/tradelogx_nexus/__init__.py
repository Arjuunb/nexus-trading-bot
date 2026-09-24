"""Python client for the TradeLogX Nexus public API (``/v1``).

    from tradelogx_nexus import Client

    client = Client()  # reads NEXUS_API_KEY (and NEXUS_API_BASE, optional)
    for d in client.decisions.list(verdict="rejected", limit=20):
        print(d["symbol"], d["quality_score"], d["blocked_by"], d["reason"])

Standard library only. Pages are followed automatically; 429 and temporary
5xx answers are retried with exponential backoff (honouring Retry-After);
writes are retried only when the server says it did not process them (429).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Optional

__all__ = ["Client", "NexusError", "API_VERSION", "__version__"]
__version__ = "0.1.0"
API_VERSION = "2026-09-24"
DEFAULT_BASE = "https://trade-logx.com"
_RETRYABLE = {429, 502, 503, 504}


class NexusError(Exception):
    """An error answer from the API: ``status``, a stable ``code`` and a message."""

    def __init__(self, status: int, code: str, message: str, body: Optional[dict] = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.body = status, code, message, body or {}


class Client:
    def __init__(self, api_key: Optional[str] = None, *, base_url: Optional[str] = None,
                 version: str = API_VERSION, max_retries: int = 3, timeout: float = 30.0,
                 backoff: float = 0.5):
        self.api_key = api_key or os.environ.get("NEXUS_API_KEY", "")
        if not self.api_key:
            raise ValueError("No API key: pass api_key= or set NEXUS_API_KEY.")
        self.base_url = (base_url or os.environ.get("NEXUS_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.version, self.max_retries, self.timeout, self.backoff = version, max_retries, timeout, backoff
        self.strategies = _Strategies(self)
        self.decisions = _Decisions(self)
        self.positions = _Positions(self)
        self.backtests = _Backtests(self)

    # ----------------------------------------------------------------- HTTP
    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                body: Optional[dict] = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.base_url}/v1{path}" + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}", "Nexus-Version": self.version,
                   "Accept": "application/json", "User-Agent": f"tradelogx-nexus-python/{__version__}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 -- https base URL
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                try:
                    payload = json.loads(raw) if raw else {}
                except ValueError:
                    payload = {}
                retryable = exc.code == 429 or (method == "GET" and exc.code in _RETRYABLE)
                if retryable and attempt < self.max_retries:
                    time.sleep(_delay(exc.headers.get("Retry-After"), attempt, self.backoff))
                    attempt += 1
                    continue
                err = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(err, dict):
                    raise NexusError(exc.code, err.get("code", "error"), err.get("message", ""), payload) from None
                raise NexusError(exc.code, "http_error", raw.decode("utf-8", "replace")[:200]) from None
            except urllib.error.URLError as exc:
                if method == "GET" and attempt < self.max_retries:
                    time.sleep(_delay(None, attempt, self.backoff))
                    attempt += 1
                    continue
                raise NexusError(0, "network_error", str(exc.reason)) from None


def _delay(retry_after: Optional[str], attempt: int, backoff: float) -> float:
    try:
        if retry_after:
            return max(0.0, float(retry_after))
    except ValueError:
        pass
    return backoff * (2 ** attempt)


class _Strategies:
    def __init__(self, client: Client):
        self._c = client

    def list(self) -> list[dict]:
        return self._c.request("GET", "/strategies")["data"]

    def promote(self, strategy_id: str, mode: str) -> dict:
        """``mode="paper"`` or ``"live"``. Live is refused while live routing is
        locked (NexusError with code ``live_routing_locked``)."""
        return self._c.request("POST", f"/strategies/{urllib.parse.quote(strategy_id)}/promote",
                               body={"mode": mode})


class _Decisions:
    def __init__(self, client: Client):
        self._c = client

    def list(self, *, verdict: Optional[str] = None, symbol: Optional[str] = None,
             since: Optional[str] = None, limit: Optional[int] = None,
             page_size: int = 100) -> Iterator[dict]:
        """Every matching decision, newest first, following pages as needed.
        ``limit`` caps the total; ``None`` walks to the end."""
        cursor, seen = None, 0
        while True:
            size = page_size if limit is None else min(page_size, limit - seen)
            if size <= 0:
                return
            page = self._c.request("GET", "/decisions", params={
                "verdict": verdict, "symbol": symbol, "since": since, "limit": size, "cursor": cursor})
            for item in page["data"]:
                yield item
                seen += 1
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def get(self, decision_id: str) -> dict:
        return self._c.request("GET", f"/decisions/{urllib.parse.quote(decision_id)}")


class _Positions:
    def __init__(self, client: Client):
        self._c = client

    def list(self) -> list[dict]:
        return self._c.request("GET", "/positions")["data"]

    def close(self, position_id: str, *, reason: str = "") -> dict:
        return self._c.request("POST", f"/positions/{urllib.parse.quote(str(position_id))}/close",
                               body={"reason": reason})


class _Backtests:
    def __init__(self, client: Client):
        self._c = client

    def create(self, strategy: str, *, symbol: str = "BTCUSDT", timeframe: str = "15m",
               bars: int = 800) -> dict:
        return self._c.request("POST", "/backtests", body={
            "strategy": strategy, "symbol": symbol, "timeframe": timeframe, "bars": bars})

    def get(self, job_id: str) -> dict:
        return self._c.request("GET", f"/backtests/{urllib.parse.quote(job_id)}")

    def run(self, strategy: str, *, poll_interval: float = 2.0, timeout: float = 600.0, **kwargs) -> dict:
        """Queue a backtest and wait for it. Returns the finished job; raises
        NexusError if it failed or TimeoutError if it outlived ``timeout``."""
        job = self.create(strategy, **kwargs)
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job["id"])
            if job["status"] == "complete":
                return job
            if job["status"] == "failed":
                raise NexusError(200, "backtest_failed", str((job.get("result") or {}).get("error", "")), job)
            if time.monotonic() > deadline:
                raise TimeoutError(f"backtest {job['id']} still {job['status']} after {timeout:.0f}s")
            time.sleep(poll_interval)
