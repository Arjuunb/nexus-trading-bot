"""Strict, local-first market-data service for Paper Trading V2.

This module is deliberately separate from :mod:`data.market_data`: the latter
has legacy demo fallbacks which remain available for backwards compatibility.
V2 never calls those fallbacks.  A V2 request therefore returns real provider
candles, cached provider candles, or a clear availability error -- never an
invented price.
"""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from bot.types import Bar
from data.market_data_reliability import CanonicalCandle, CanonicalSymbol, ProviderRegistry, ResilientRequester

TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")
TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
         "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
         "1d": 86_400_000}
CRYPTO_SEEDS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
                "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT"}
_FUTURES_HOSTS = ("https://fapi.binance.com", "https://fstream.binance.com")
_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_YAHOO_INTERVALS = {"1m": ("1m", "7d"), "3m": ("1m", "7d"),
                    "5m": ("5m", "60d"), "15m": ("15m", "60d"),
                    "30m": ("30m", "60d"), "1h": ("1h", "730d"),
                    "4h": ("1h", "730d"), "1d": ("1d", "max")}
_YAHOO_AGGREGATES = {"3m": ("1m", 3), "4h": ("1h", 4)}
_YAHOO_ALIASES = {"SPX": "^GSPC", "NASDAQ": "^NDX", "DOW": "^DJI",
                  "FTSE100": "^FTSE", "DAX": "^GDAXI", "NIKKEI": "^N225",
                  "GOLD": "GC=F", "SILVER": "SI=F", "CRUDEOIL": "CL=F",
                  "NATURALGAS": "NG=F"}
_ASSET_ALIASES = {"SPX": "stocks", "NASDAQ": "stocks", "DOW": "stocks",
                  "FTSE100": "stocks", "DAX": "stocks", "NIKKEI": "stocks",
                  "GOLD": "commodities", "SILVER": "commodities",
                  "CRUDEOIL": "commodities", "NATURALGAS": "commodities"}


def candles_for_period(timeframe: str, period: str = "90d") -> int:
    """Translate an operator-facing history range into a bounded candle count."""
    if timeframe not in TF_MS:
        raise ValueError(f"unsupported timeframe '{timeframe}'")
    key = (period or "90d").lower().replace(" ", "")
    if key == "max":
        return 200_000
    days = {"90d": 90, "3mo": 90, "6mo": 183, "1y": 365,
            "2y": 730, "5y": 1826}.get(key)
    if days is None:
        raise ValueError("period must be 90d, 6mo, 1y, 2y, 5y, or max")
    return max(1, int(days * 86_400_000 / TF_MS[timeframe]))


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").upper().replace("/", "").replace("-", "").strip()


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms is not None else None


class MarketDataService:
    """Provider-backed OHLCV cache with per-asset SQLite files and metadata.

    Cache files intentionally live under ``market_data/<asset>/`` rather than
    beside application state, making market data portable, inspectable and
    safe to delete/rebuild independently of trade/account data.
    """
    def __init__(self, root: str | Path, *, request_json: Optional[Callable] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.request_json = request_json or self._request_json
        self._lock = threading.RLock()
        self._perpetuals: tuple[float, list[str]] = (0.0, [])
        self._usdm_rules: tuple[float, dict[str, dict]] = (0.0, {})
        self.registry = ProviderRegistry()
        self._requesters = {p: ResilientRequester(self.request_json, self.registry, p)
                            for p in self.registry.providers}

    @staticmethod
    def _request_json(url: str, params: dict) -> object:
        # Stdlib keeps the strict V2 path available even in a minimal runtime;
        # no optional HTTP client should turn an otherwise reachable provider
        # into a misleading "no market data" condition.
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        query = urlencode(params or {})
        request = Request(url + ("?" + query if query else ""),
                          headers={"User-Agent": "TradeLogX-MarketDataV2/1.0"})
        with urlopen(request, timeout=15) as response:  # nosec B310: fixed trusted provider URLs
            return json.loads(response.read().decode("utf-8"))

    def asset_for(self, symbol: str) -> str:
        key = normalize_symbol(symbol)
        if key in CRYPTO_SEEDS or key.endswith("USDT"):
            return "crypto"
        if key in _ASSET_ALIASES:
            return _ASSET_ALIASES[key]
        try:
            from services.symbol_universe import find
            rec = find(symbol)
            if rec:
                return {"stock": "stocks", "etf": "stocks", "index": "stocks",
                        "forex": "forex", "commodity": "commodities"}.get(
                    rec["asset_class"], "stocks")
        except Exception:  # catalog unavailability must not fabricate a market
            pass
        # Yahoo is the trusted no-key provider for exchange-listed equities.
        # Do not require every S&P/NASDAQ/NYSE constituent to be duplicated in
        # a hand-maintained catalog: a normal alphabetic ticker is resolvable
        # directly and will still fail closed if Yahoo does not recognise it.
        if key.isalpha() and 1 <= len(key) <= 6:
            return "stocks"
        return "unknown"

    def _db_path(self, symbol: str, asset: Optional[str] = None) -> Path:
        asset = asset or self.asset_for(symbol)
        if asset == "unknown":
            raise ValueError(f"unavailable symbol '{symbol}'")
        safe = "".join(c for c in normalize_symbol(symbol) if c.isalnum() or c == "_")
        directory = self.root / asset
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{safe}.sqlite3"

    def _conn(self, symbol: str, asset: Optional[str] = None) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path(symbol, asset))
        c.execute("""CREATE TABLE IF NOT EXISTS candles (
            timeframe TEXT NOT NULL, open_time INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            PRIMARY KEY(timeframe, open_time))""")
        c.execute("""CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS funding_rates (
            funding_time INTEGER PRIMARY KEY,
            funding_rate REAL NOT NULL,
            mark_price REAL,
            provider TEXT NOT NULL,
            received_at TEXT NOT NULL,
            source_quality TEXT NOT NULL)""")
        # Additive migration from V2 cache schema 2: provenance exists both in
        # dataset metadata and alongside every persisted candle.
        for column, kind in (("provider", "TEXT"), ("market_type", "TEXT"), ("is_closed", "INTEGER"),
                             ("received_at", "TEXT"), ("source_quality", "TEXT")):
            try: c.execute(f"ALTER TABLE candles ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError: pass
        return c

    def _meta(self, c: sqlite3.Connection) -> dict:
        return {k: json.loads(v) for k, v in c.execute("SELECT key,value FROM metadata")}

    @staticmethod
    def _set_meta(c: sqlite3.Connection, **values: object) -> None:
        c.executemany("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)",
                      [(k, json.dumps(v)) for k, v in values.items()])

    @staticmethod
    def _valid(row: tuple) -> bool:
        try:
            t, o, h, l, close, v = row
            return int(t) >= 0 and min(float(o), float(h), float(l), float(close), float(v)) >= 0 and \
                float(h) >= max(float(o), float(close), float(l)) and \
                float(l) <= min(float(o), float(close), float(h))
        except (TypeError, ValueError):
            return False

    def upsert(self, symbol: str, timeframe: str, rows: list[tuple], *, provider: str) -> dict:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe '{timeframe}'")
        canonical = CanonicalSymbol.parse(symbol, "crypto" if self.asset_for(symbol) == "crypto" else "")
        received = datetime.now(timezone.utc).isoformat()
        now_ms = int(time.time() * 1000)
        valid = []
        for r in rows:
            if not self._valid(tuple(r[:6])): continue
            # Provider endpoints often include the currently-forming bar. It
            # cannot be a deterministic historical candle yet, so reject it.
            if int(r[0]) + TF_MS[timeframe] > now_ms: continue
            candle = CanonicalCandle(canonical.value, timeframe, int(r[0]), *map(float, r[1:6]), provider,
                                     self.asset_for(symbol), True, received)
            candle.validate(); valid.append(tuple(r[:6]))
        valid = sorted({int(r[0]): r for r in valid}.values())
        if not valid:
            raise ValueError("provider returned no valid OHLCV candles")
        asset = self.asset_for(symbol)
        with self._lock:
            c = self._conn(symbol, asset)
            try:
                c.executemany("INSERT OR REPLACE INTO candles(timeframe,open_time,open,high,low,close,volume,provider,market_type,is_closed,received_at,source_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              [(timeframe, *r, provider, asset, 1, received, "verified") for r in valid])
                report = self._integrity_conn(c, timeframe, asset)
                checksum = self._checksum_conn(c, timeframe)
                meta = self._meta(c)
                checksums = dict(meta.get("checksums_by_timeframe") or {})
                versions = dict(meta.get("dataset_versions_by_timeframe") or {})
                qualities = dict(meta.get("quality_by_timeframe") or {})
                missing = dict(meta.get("missing_ranges_by_timeframe") or {})
                checksums[timeframe] = checksum
                versions[timeframe] = f"v4:{checksum[:16]}"
                qualities[timeframe] = report["status"]
                missing[timeframe] = report["missing_ranges"]
                self._set_meta(c, symbol=normalize_symbol(symbol), canonical_symbol=canonical.value, asset_class=asset,
                               provider=provider, downloaded_at=datetime.now(timezone.utc).isoformat(),
                               last_updated=datetime.now(timezone.utc).isoformat(),
                               missing_ranges=report["missing_ranges"], schema_version=4, checksum=checksum,
                               dataset_version=f"v4:{checksum[:16]}", quality_status=report["status"],
                               checksums_by_timeframe=checksums,
                               dataset_versions_by_timeframe=versions,
                               quality_by_timeframe=qualities,
                               missing_ranges_by_timeframe=missing)
                c.commit()
            finally:
                c.close()
        return {"symbol": normalize_symbol(symbol), "timeframe": timeframe,
                "stored": len(valid), "provider": provider, "integrity": report}

    @staticmethod
    def _checksum_rows(rows: list[tuple]) -> str:
        return hashlib.sha256(json.dumps(rows, separators=(",", ":"), default=str).encode()).hexdigest()

    def _checksum_conn(self, c: sqlite3.Connection, timeframe: str) -> str:
        rows = c.execute("SELECT open_time,open,high,low,close,volume FROM candles WHERE timeframe=? ORDER BY open_time", (timeframe,)).fetchall()
        return self._checksum_rows(rows)

    def _rows(self, symbol: str, timeframe: str, *, limit: Optional[int] = None) -> list[tuple]:
        try:
            c = self._conn(symbol)
        except ValueError:
            return []
        try:
            rows = c.execute("SELECT open_time,open,high,low,close,volume FROM candles "
                             "WHERE timeframe=? ORDER BY open_time", (timeframe,)).fetchall()
        finally:
            c.close()
        return rows[-limit:] if limit else rows

    def bars(self, symbol: str, timeframe: str, *, limit: int = 1500) -> list[Bar]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe '{timeframe}'")
        state = self.status(symbol, timeframe)
        if not state.get("available"):
            if state.get("integrity", {}).get("candles", 0) == 0:
                return []
            raise ValueError("verified market-data cache required; download the dataset")
        return [Bar(datetime.fromtimestamp(r[0] / 1000, timezone.utc), *map(float, r[1:]))
                for r in self._rows(symbol, timeframe, limit=limit)]

    def latest(self, symbol: str, timeframe: str = "1h") -> Optional[dict]:
        if not self.status(symbol, timeframe).get("available"):
            return None
        rows = self._rows(symbol, timeframe, limit=1)
        if not rows:
            return None
        r = rows[-1]
        return {"timestamp": _iso(r[0]), "open": r[1], "high": r[2], "low": r[3],
                "close": r[4], "volume": r[5], "source": "local real cache"}

    def _integrity_conn(self, c: sqlite3.Connection, timeframe: str, asset: str) -> dict:
        rows = c.execute("SELECT open_time,open,high,low,close,volume FROM candles "
                         "WHERE timeframe=? ORDER BY open_time", (timeframe,)).fetchall()
        corrupt = sum(1 for r in rows if not self._valid(r))
        # A crypto feed is continuous.  Other markets close overnight/weekends;
        # flagging every normal equity close as a bad data gap would be false.
        missing: list[dict] = []
        if asset == "crypto":
            step = TF_MS[timeframe]
            for a, b in zip(rows, rows[1:]):
                if b[0] - a[0] > step:
                    missing.append({"from": _iso(a[0] + step), "to": _iso(b[0] - step)})
        return {"candles": len(rows), "corrupt": corrupt, "duplicates": 0,
                "timezone": "UTC", "ascending": all(a[0] < b[0] for a, b in zip(rows, rows[1:])),
                "missing_ranges": missing, "status": "incomplete" if missing else "corrupted" if corrupt else "healthy"}

    def status(self, symbol: str, timeframe: str = "1h") -> dict:
        asset = self.asset_for(symbol)
        if asset == "unknown":
            return {"available": False, "symbol": normalize_symbol(symbol), "reason": "unknown symbol"}
        try:
            c = self._conn(symbol, asset)
            meta = self._meta(c)
            integrity = self._integrity_conn(c, timeframe, asset)
            last = c.execute("SELECT MAX(open_time) FROM candles WHERE timeframe=?", (timeframe,)).fetchone()[0]
            checksum = self._checksum_conn(c, timeframe)
            timeframe_count = c.execute(
                "SELECT COUNT(DISTINCT timeframe) FROM candles").fetchone()[0]
        finally:
            c.close()
        expected_checksum = (meta.get("checksums_by_timeframe") or {}).get(timeframe)
        if expected_checksum is None and timeframe_count == 1:
            # Backward-compatible read of the schema-v3 single-timeframe cache.
            expected_checksum = meta.get("checksum")
        checksum_ok = bool(expected_checksum) and expected_checksum == checksum
        quarantined = None
        if not checksum_ok and (expected_checksum is not None or integrity["candles"] > 0):
            # Never keep a checksum-invalid SQLite file in the active cache.
            # It is moved aside for forensic inspection; a subsequent explicit
            # download rebuilds the dataset from the recorded provider.
            path = self._db_path(symbol, asset)
            if path.exists():
                quarantined_path = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
                path.replace(quarantined_path)
                quarantined = str(quarantined_path)
        fresh = max(0, int(time.time() - last / 1000)) if last else None
        stale = fresh is not None and fresh > TF_MS.get(timeframe, 0) * 2 / 1000
        return {"available": integrity["candles"] > 0 and checksum_ok, "symbol": normalize_symbol(symbol),
                "asset_class": asset, "timeframe": timeframe, "last_candle": _iso(last),
                "freshness_seconds": fresh, "stale": stale, "checksum_ok": checksum_ok,
                "quarantined_cache": quarantined, "needs_download": not checksum_ok,
                "quality_score": 100 if checksum_ok and not stale and integrity["status"] == "healthy" else 60 if checksum_ok else 0,
                "metadata": meta, "integrity": integrity}

    def upsert_funding(self, symbol: str, rows: list[dict], *, provider: str,
                       requested_start_ms: int | None = None,
                       requested_end_ms: int | None = None) -> dict:
        """Persist verified public funding events with deterministic deduplication."""
        key = normalize_symbol(symbol)
        if not key.endswith("USDT"):
            raise ValueError("historical funding requires a USDT perpetual symbol")
        received = datetime.now(timezone.utc).isoformat()
        normalized: dict[int, tuple] = {}
        rejected = 0
        for row in rows:
            try:
                stamp = int(row["fundingTime"])
                rate = float(row["fundingRate"])
                raw_mark = row.get("markPrice")
                mark = float(raw_mark) if raw_mark not in (None, "") else None
                if stamp < 0 or not math.isfinite(rate) or (mark is not None and
                                                            (not math.isfinite(mark) or mark <= 0)):
                    raise ValueError("invalid funding row")
            except (KeyError, TypeError, ValueError, OverflowError):
                rejected += 1
                continue
            normalized[stamp] = (stamp, rate, mark, provider, received, "verified_public_provider")
        if not normalized:
            raise ValueError("provider returned no valid historical funding records")
        with self._lock:
            c = self._conn(key, "crypto")
            try:
                before = c.execute("SELECT COUNT(*) FROM funding_rates").fetchone()[0]
                c.executemany(
                    "INSERT OR REPLACE INTO funding_rates(funding_time,funding_rate,mark_price,provider,received_at,source_quality) VALUES (?,?,?,?,?,?)",
                    [normalized[stamp] for stamp in sorted(normalized)],
                )
                after = c.execute("SELECT COUNT(*) FROM funding_rates").fetchone()[0]
                coverage = self._funding_status_conn(
                    c, requested_start_ms=requested_start_ms, requested_end_ms=requested_end_ms)
                meta = self._meta(c)
                requests = list(meta.get("funding_requests") or [])[-19:]
                requests.append({"requested_start": _iso(requested_start_ms),
                                 "requested_end": _iso(requested_end_ms),
                                 "received_at": received, "provider": provider,
                                 "coverage_state": coverage["state"]})
                self._set_meta(c, funding_provider=provider,
                               funding_last_updated=received, funding_requests=requests)
                c.commit()
            finally:
                c.close()
        return {"symbol": key, "received": len(rows), "valid": len(normalized),
                "rejected": rejected, "inserted": after - before,
                "duplicates_or_updates": len(normalized) - (after - before),
                "coverage": coverage, "provider": provider}

    @staticmethod
    def _funding_status_conn(c: sqlite3.Connection, *, requested_start_ms: int | None,
                             requested_end_ms: int | None,
                             intentionally_disabled: bool = False) -> dict:
        if intentionally_disabled:
            return {"state": "FUNDING_INTENTIONALLY_DISABLED", "available": False,
                    "complete": False, "records": 0, "warnings": []}
        clauses, params = [], []
        if requested_start_ms is not None:
            clauses.append("funding_time>=?"); params.append(int(requested_start_ms))
        if requested_end_ms is not None:
            clauses.append("funding_time<=?"); params.append(int(requested_end_ms))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = c.execute(
            "SELECT funding_time,funding_rate,mark_price,provider,source_quality FROM funding_rates" +
            where + " ORDER BY funding_time", params).fetchall()
        if not rows:
            return {"state": "HISTORICAL_FUNDING_UNAVAILABLE", "available": False,
                    "complete": False, "records": 0,
                    "requested_start": _iso(requested_start_ms),
                    "requested_end": _iso(requested_end_ms),
                    "warnings": ["No historical funding records cover the requested interval; funding is unknown, not zero."]}
        stamps = [int(row[0]) for row in rows]
        eight_hours = 8 * 60 * 60 * 1000
        gaps = [{"from": _iso(left), "to": _iso(right), "hours": round((right - left) / 3_600_000, 3)}
                for left, right in zip(stamps, stamps[1:]) if right - left > eight_hours * 1.5]
        starts_late = requested_start_ms is not None and stamps[0] > requested_start_ms + eight_hours
        ends_early = requested_end_ms is not None and stamps[-1] < requested_end_ms - eight_hours
        complete = not starts_late and not ends_early and not gaps
        state = "HISTORICAL_FUNDING_AVAILABLE" if complete else "HISTORICAL_FUNDING_PARTIALLY_AVAILABLE"
        warnings = []
        if not complete:
            warnings.append("Historical funding coverage is incomplete; uncovered holding periods remain unknown.")
        if any(row[2] is None for row in rows):
            warnings.append("Some funding events do not include provider mark price; trade entry price is used only for notional conversion and is disclosed per trade.")
        return {"state": state, "available": True, "complete": complete,
                "records": len(rows), "first": _iso(stamps[0]), "last": _iso(stamps[-1]),
                "requested_start": _iso(requested_start_ms),
                "requested_end": _iso(requested_end_ms), "missing_ranges": gaps,
                "starts_late": starts_late, "ends_early": ends_early,
                "provider": rows[-1][3], "source_quality": rows[-1][4],
                "warnings": warnings}

    def funding_status(self, symbol: str, *, start_ms: int | None = None,
                       end_ms: int | None = None, intentionally_disabled: bool = False) -> dict:
        c = self._conn(symbol, "crypto")
        try:
            return {"symbol": normalize_symbol(symbol), **self._funding_status_conn(
                c, requested_start_ms=start_ms, requested_end_ms=end_ms,
                intentionally_disabled=intentionally_disabled)}
        finally:
            c.close()

    def funding_history(self, symbol: str, *, start_ms: int | None = None,
                        end_ms: int | None = None) -> list[dict]:
        c = self._conn(symbol, "crypto")
        try:
            clauses, params = [], []
            if start_ms is not None:
                clauses.append("funding_time>=?"); params.append(int(start_ms))
            if end_ms is not None:
                clauses.append("funding_time<=?"); params.append(int(end_ms))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = c.execute(
                "SELECT funding_time,funding_rate,mark_price,provider,source_quality FROM funding_rates" +
                where + " ORDER BY funding_time", params).fetchall()
        finally:
            c.close()
        return [{"symbol": normalize_symbol(symbol), "funding_time": _iso(row[0]),
                 "funding_time_ms": row[0], "funding_rate": row[1], "mark_price": row[2],
                 "provider": row[3], "source_quality": row[4]} for row in rows]

    def download_usdm_funding_history(self, symbol: str, *, start_ms: int,
                                      end_ms: int) -> dict:
        """Page Binance's public funding history endpoint and persist the result."""
        key = normalize_symbol(symbol)
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("funding time range is invalid")
        cursor, collected = int(start_ms), {}
        while cursor <= end_ms:
            payload = self._requesters["binance-futures"](
                _FUTURES_HOSTS[0] + "/fapi/v1/fundingRate",
                {"symbol": key, "startTime": cursor, "endTime": int(end_ms), "limit": 1000})
            if not isinstance(payload, list) or not payload:
                break
            valid_stamps = []
            for row in payload:
                try:
                    stamp = int(row["fundingTime"])
                except (KeyError, TypeError, ValueError):
                    continue
                if start_ms <= stamp <= end_ms:
                    collected[stamp] = row
                    valid_stamps.append(stamp)
            if not valid_stamps:
                break
            next_cursor = max(valid_stamps) + 1
            if next_cursor <= cursor or len(payload) < 1000:
                break
            cursor = next_cursor
            time.sleep(0.05)
        if not collected:
            # Persist nothing and report the truth without manufacturing zero-rate rows.
            return {"symbol": key, "received": 0, "valid": 0, "inserted": 0,
                    "coverage": self.funding_status(key, start_ms=start_ms, end_ms=end_ms),
                    "provider": "binance-usdt-perpetual-public-funding"}
        return self.upsert_funding(
            key, [collected[stamp] for stamp in sorted(collected)],
            provider="binance-usdt-perpetual-public-funding",
            requested_start_ms=start_ms, requested_end_ms=end_ms)

    def quality(self, symbol: str, timeframe: str = "1h") -> dict:
        state = self.status(symbol, timeframe)
        return {"symbol": state["symbol"], "timeframe": timeframe, "quality_score": state.get("quality_score", 0),
                "status": state.get("integrity", {}).get("status", "unavailable"), "stale": state.get("stale"),
                "checksum_ok": state.get("checksum_ok"), "gaps": state.get("integrity", {}).get("missing_ranges", []),
                "duplicates": state.get("integrity", {}).get("duplicates", 0), "corrupt": state.get("integrity", {}).get("corrupt", 0)}

    def delete_cache(self, symbol: str, timeframe: str) -> dict:
        path = self._db_path(symbol)
        if not path.exists(): return {"deleted": False, "reason": "cache not found"}
        with self._lock:
            c = self._conn(symbol)
            try:
                cur = c.execute("DELETE FROM candles WHERE timeframe=?", (timeframe,)); c.commit()
            finally: c.close()
        return {"deleted": True, "symbol": normalize_symbol(symbol), "timeframe": timeframe, "rows": cur.rowcount}

    def _crypto_rows(self, symbol: str, timeframe: str, *, start_ms: Optional[int], limit: int,
                     end_ms: Optional[int] = None) -> list[tuple]:
        params = {"symbol": normalize_symbol(symbol), "interval": timeframe, "limit": min(1500, limit)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        error = None
        for host in _FUTURES_HOSTS:
            try:
                payload = self._requesters["binance-futures"](host + "/fapi/v1/klines", params)
                return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in payload]
            except Exception as exc:  # try public mirror before reporting failure
                error = exc
        raise RuntimeError(f"Binance USDT perpetual data unavailable: {error}")

    def public_usdm_window(self, symbol: str, timeframe: str, *, limit: int = 1000,
                           end_ms: Optional[int] = None) -> list[Bar]:
        """Return a bounded public Binance USD-M Futures OHLCV window.

        Unlike :meth:`bars`, this read-through method does not require a
        downloaded cache.  Callers must still separate the current forming
        candle before passing rows to any closed-candle decision engine.
        """
        key = normalize_symbol(symbol)
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe '{timeframe}'")
        if not key.endswith("USDT"):
            raise ValueError("Binance USD-M visual data requires a USDT perpetual symbol")
        bounded = max(50, min(int(limit), 1500))
        rows = self._crypto_rows(key, timeframe, start_ms=None, end_ms=end_ms, limit=bounded)
        return [Bar(datetime.fromtimestamp(r[0] / 1000, timezone.utc), *map(float, r[1:])) for r in rows]

    def public_usdm_quote(self, symbol: str) -> dict:
        """Return factual public bid/ask/mark/funding data for one contract."""
        key = normalize_symbol(symbol)
        book = self._requesters["binance-futures"](
            _FUTURES_HOSTS[0] + "/fapi/v1/ticker/bookTicker", {"symbol": key})
        premium = self._requesters["binance-futures"](
            _FUTURES_HOSTS[0] + "/fapi/v1/premiumIndex", {"symbol": key})
        funding_rows = self._requesters["binance-futures"](
            _FUTURES_HOSTS[0] + "/fapi/v1/fundingRate", {"symbol": key, "limit": 1})
        try:
            funding = funding_rows[-1]
            return {
                "symbol": key,
                "bid": float(book["bidPrice"]),
                "ask": float(book["askPrice"]),
                "mark": float(premium["markPrice"]),
                "index": float(premium["indexPrice"]),
                "funding_rate": float(funding["fundingRate"]),
                "last_funding_time": datetime.fromtimestamp(
                    int(funding["fundingTime"]) / 1000, timezone.utc).isoformat(),
                "next_funding_time": datetime.fromtimestamp(
                    int(premium["nextFundingTime"]) / 1000, timezone.utc).isoformat(),
                "provider_time": datetime.fromtimestamp(
                    int(premium.get("time") or book.get("time")) / 1000, timezone.utc).isoformat(),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Binance USD-M returned malformed public quote data: {exc}") from exc

    def _crypto_history(self, symbol: str, timeframe: str, candles: int) -> list[tuple]:
        """Page backwards through real Binance Futures candles without overlap."""
        rows: list[tuple] = []
        end_ms: Optional[int] = None
        while len(rows) < candles:
            batch = self._crypto_rows(symbol, timeframe, start_ms=None,
                                      end_ms=end_ms, limit=min(1500, candles - len(rows)))
            if not batch:
                break
            rows = batch + rows
            if len(batch) < min(1500, candles - len(rows) + len(batch)):
                break  # provider reached listing/retention boundary
            next_end = batch[0][0] - 1
            if end_ms is not None and next_end >= end_ms:
                break  # defensive provider-loop guard
            end_ms = next_end
            time.sleep(0.05)  # public endpoint rate-limit courtesy
        # Provider rows can be repeated at paging boundaries. The upsert would
        # dedupe them too; doing it here keeps returned status exact.
        return sorted({r[0]: r for r in rows}.values())[-candles:]

    def _yahoo_rows(self, symbol: str, timeframe: str, *, limit: int) -> list[tuple]:
        from data.yahoo_bars import yahoo_symbol_for
        ticker = yahoo_symbol_for(symbol) or _YAHOO_ALIASES.get(normalize_symbol(symbol)) or normalize_symbol(symbol)
        interval, range_ = _YAHOO_INTERVALS[timeframe]
        data = self._requesters["yahoo-finance"](_YAHOO.format(symbol=ticker), {"interval": interval, "range": range_})
        try:
            result = data["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            out = []
            for i, stamp in enumerate(result.get("timestamp") or []):
                vals = (quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i])
                if any(v is None for v in vals):
                    continue
                volume = (quote.get("volume") or [0] * len(result["timestamp"]))[i] or 0
                out.append((int(stamp) * 1000, *map(float, vals), float(volume)))
            aggregate = _YAHOO_AGGREGATES.get(timeframe)
            if aggregate:
                base_tf, factor = aggregate
                out = self._aggregate_rows(out, TF_MS[base_tf], factor)
            return out[-limit:]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Yahoo returned malformed historical data: {exc}") from exc

    @staticmethod
    def _aggregate_rows(rows: list[tuple], step_ms: int, factor: int) -> list[tuple]:
        """Aggregate complete adjacent provider candles only.

        This is an OHLCV transformation of genuine smaller provider candles,
        never interpolation. Incomplete groups (including market/session gaps)
        are omitted rather than made into a plausible-looking larger candle.
        """
        out: list[tuple] = []
        bucket: list[tuple] = []
        width = step_ms * factor
        for row in rows:
            if bucket and (row[0] // width != bucket[0][0] // width or row[0] - bucket[-1][0] != step_ms):
                if len(bucket) == factor:
                    out.append((bucket[0][0], bucket[0][1], max(x[2] for x in bucket),
                                min(x[3] for x in bucket), bucket[-1][4], sum(x[5] for x in bucket)))
                bucket = []
            bucket.append(row)
        if len(bucket) == factor:
            out.append((bucket[0][0], bucket[0][1], max(x[2] for x in bucket),
                        min(x[3] for x in bucket), bucket[-1][4], sum(x[5] for x in bucket)))
        return out

    def download(self, symbol: str, timeframe: str = "1h", *, candles: Optional[int] = None,
                 period: str = "90d") -> dict:
        """Download real provider data. Repeated calls are idempotent upserts."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe '{timeframe}'")
        asset = self.asset_for(symbol)
        if asset == "unknown":
            raise ValueError(f"unavailable symbol '{symbol}'")
        candles = int(candles) if candles is not None else candles_for_period(timeframe, period)
        if candles <= 0 or candles > 200_000:
            raise ValueError("candles must be between 1 and 200000")
        if asset == "crypto":
            rows, provider = self._crypto_history(symbol, timeframe, candles), "binance-usdt-perpetual"
        else:
            rows, provider = self._yahoo_rows(symbol, timeframe, limit=candles), "yahoo-finance"
        return self.upsert(symbol, timeframe, rows, provider=provider)

    def crypto_perpetuals(self, *, ttl_seconds: int = 3600) -> list[str]:
        """Discover active Binance USDT perpetual pairs, with a safe seed fallback.

        Discovery is metadata only; candle requests still validate a pair at the
        provider. A temporary exchange outage must not make the paper UI empty.
        """
        with self._lock:
            cached_at, cached = self._perpetuals
            if cached and time.time() - cached_at < ttl_seconds:
                return list(cached)
        try:
            payload = self._requesters["binance-futures"](_FUTURES_HOSTS[0] + "/fapi/v1/exchangeInfo", {})
            pairs = sorted({x["symbol"] for x in payload.get("symbols", [])
                            if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"
                            and x.get("contractType") == "PERPETUAL"})
            if not pairs:
                raise ValueError("no USDT perpetual pairs returned")
        except Exception:
            pairs = sorted(CRYPTO_SEEDS)
        with self._lock:
            self._perpetuals = (time.time(), pairs)
        return pairs

    def usdm_contract_rules(self, symbol: str, *, ttl_seconds: int = 3600) -> dict:
        """Return provider-declared tick, quantity, and notional constraints."""
        key = normalize_symbol(symbol)
        with self._lock:
            cached_at, cached = self._usdm_rules
            if cached and time.time() - cached_at < ttl_seconds and key in cached:
                return dict(cached[key])
        payload = self._requesters["binance-futures"](
            _FUTURES_HOSTS[0] + "/fapi/v1/exchangeInfo", {})
        rules: dict[str, dict] = {}
        for contract in payload.get("symbols", []):
            if (contract.get("status") != "TRADING" or contract.get("quoteAsset") != "USDT"
                    or contract.get("contractType") != "PERPETUAL"):
                continue
            filters = {row.get("filterType"): row for row in contract.get("filters", [])}
            price = filters.get("PRICE_FILTER", {})
            lot = filters.get("LOT_SIZE", {})
            notional = filters.get("MIN_NOTIONAL", {})
            rules[contract["symbol"]] = {
                "symbol": contract["symbol"],
                "tick_size": float(price.get("tickSize") or 0),
                "min_price": float(price.get("minPrice") or 0),
                "quantity_step": float(lot.get("stepSize") or 0),
                "min_quantity": float(lot.get("minQty") or 0),
                "max_quantity": float(lot.get("maxQty") or 0),
                "min_notional": float(notional.get("notional") or notional.get("minNotional") or 0),
                "price_precision": int(contract.get("pricePrecision") or 0),
                "quantity_precision": int(contract.get("quantityPrecision") or 0),
                "max_lab_leverage": 20,
            }
        with self._lock:
            self._usdm_rules = (time.time(), rules)
        if key not in rules:
            raise ValueError(f"active Binance USD-M perpetual metadata unavailable for '{key}'")
        return dict(rules[key])

    def usdm_symbol_rules(self, symbol: str):
        """Typed execution boundary; the public/lab contract remains a dict."""
        from bot.brokers.symbol_rules import SymbolRules

        spec = self.usdm_contract_rules(symbol)
        if isinstance(spec, SymbolRules):
            return spec
        return SymbolRules(
            symbol=spec["symbol"], step_size=spec["quantity_step"],
            tick_size=spec["tick_size"], min_qty=spec["min_quantity"],
            min_notional=spec["min_notional"],
        )

    def verify_binance_usdm(self) -> dict:
        """Perform a real Binance USD-M Futures metadata health check.

        Unlike discovery this never uses the UI seed fallback, so callers can
        distinguish a genuine provider reconnection from an offline catalog.
        """
        payload = self._requesters["binance-futures"](
            _FUTURES_HOSTS[0] + "/fapi/v1/exchangeInfo", {})
        pairs = sorted({x["symbol"] for x in payload.get("symbols", [])
                        if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"
                        and x.get("contractType") == "PERPETUAL"})
        if "BTCUSDT" not in pairs:
            raise RuntimeError("Binance USD-M Futures returned no active BTCUSDT perpetual")
        with self._lock:
            self._perpetuals = (time.time(), pairs)
        return {"connected": True, "provider": "Binance USD-M Futures",
                "active_usdt_perpetuals": len(pairs)}

    def clear_cache(self) -> int:
        root = self.root.resolve()
        if root == Path(root.anchor):
            raise RuntimeError("refusing to clear a filesystem root as market cache")
        removed = 0
        with self._lock:
            for candidate in root.rglob("*.sqlite3"):
                resolved = candidate.resolve()
                if root not in resolved.parents:
                    raise RuntimeError("market cache path escaped configured root")
                resolved.unlink(missing_ok=True)
                removed += 1
            self._perpetuals = (0.0, [])
            self._usdm_rules = (0.0, {})
        return removed

    def update(self, symbol: str, timeframe: str = "1h") -> dict:
        """Incremental update. Provider rows are upserted; duplicates cannot accrue."""
        existing = self._rows(symbol, timeframe, limit=1)
        asset = self.asset_for(symbol)
        if asset == "crypto":
            start = existing[-1][0] + TF_MS[timeframe] if existing else None
            rows, provider = self._crypto_rows(symbol, timeframe, start_ms=start, limit=1500), "binance-usdt-perpetual"
        else:
            # Yahoo's public range endpoint does not provide reliable universal
            # cursor paging; retrieve its finite window and idempotently upsert.
            rows, provider = self._yahoo_rows(symbol, timeframe, limit=1500), "yahoo-finance"
        if not rows:
            return {"symbol": normalize_symbol(symbol), "timeframe": timeframe, "stored": 0,
                    "provider": provider, "message": "provider had no newer candles"}
        return self.upsert(symbol, timeframe, rows, provider=provider)

    def repair(self, symbol: str, timeframe: str = "1h") -> dict:
        """Repair a continuous crypto series by refetching missing ranges.

        No interpolation is performed.  If a provider cannot supply a range it
        remains visible in ``missing_ranges`` for the operator to investigate.
        """
        state = self.status(symbol, timeframe)
        if state.get("asset_class") != "crypto":
            return {**state, "repaired": 0, "message": "session-aware provider; no synthetic gap repair"}
        repaired = 0
        for gap in state["integrity"]["missing_ranges"]:
            start = int(datetime.fromisoformat(gap["from"]).timestamp() * 1000)
            rows = self._crypto_rows(symbol, timeframe, start_ms=start, limit=1500)
            repaired += self.upsert(symbol, timeframe, rows, provider="binance-usdt-perpetual")["stored"]
        return {**self.status(symbol, timeframe), "repaired": repaired}


class MarketDataUpdateJob:
    """Single background update batch with observable, bounded progress.

    The job intentionally does not schedule its own process-wide timer: Docker
    restarts and deployment windows should not create surprise external traffic.
    An operator (or a platform scheduler) explicitly starts a batch through the
    API and can poll the returned status.
    """
    def __init__(self, service: MarketDataService):
        self.service = service
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.state: dict = {"running": False, "started_at": None, "finished_at": None,
                            "total": 0, "done": 0, "current": None, "results": []}

    def start(self, symbols: list[str], timeframes: list[str]) -> dict:
        pairs = [(normalize_symbol(s), tf) for s in symbols for tf in timeframes]
        if not pairs:
            raise ValueError("at least one symbol and timeframe is required")
        if any(tf not in TIMEFRAMES for _, tf in pairs):
            raise ValueError("one or more timeframes are unsupported")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "reason": "V2 update already running", **self.state}
            self.state = {"running": True, "started_at": datetime.now(timezone.utc).isoformat(),
                          "finished_at": None, "total": len(pairs), "done": 0,
                          "current": None, "results": []}
            self._thread = threading.Thread(target=self._run, args=(pairs,), name="market-data-v2-update", daemon=True)
            self._thread.start()
        return {"started": True, **self.state}

    def _run(self, pairs: list[tuple[str, str]]) -> None:
        for symbol, timeframe in pairs:
            self.state["current"] = {"symbol": symbol, "timeframe": timeframe}
            try:
                result = self.service.update(symbol, timeframe)
            except Exception as exc:  # one provider error must not stop other markets
                result = {"symbol": symbol, "timeframe": timeframe, "error": str(exc)}
            self.state["results"].append(result)
            self.state["done"] += 1
        self.state["running"] = False
        self.state["current"] = None
        self.state["finished_at"] = datetime.now(timezone.utc).isoformat()

    def status(self) -> dict:
        with self._lock:
            return dict(self.state)
