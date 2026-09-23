"""Multi-asset symbol universe.

One catalog spanning crypto, stocks, ETFs, indices, forex and commodities:

  * Crypto is **auto-synced live from CCXT** (`load_markets`) so the pair list
    tracks whatever the exchange actually lists — new pairs appear, delisted
    ones drop, no code change. When the exchange is unreachable (offline / a
    blocked cloud IP) it falls back to a curated seed list so the universe is
    never empty.
  * Stocks / ETFs / indices / forex / commodities come from a curated reference
    catalog (`data/symbol_catalog.json`) — real names, exchanges and trading
    sessions. Live quotes for these require a connected data provider; until one
    is wired their prices are reported as *unavailable*, never fabricated.

Everything is exposed through a small, testable API: ``catalog``, ``search``,
``filter_symbols``, ``asset_classes`` and ``market_info``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "symbol_catalog.json"

# Exchange cash-session hours in UTC (open_h, open_m, close_h, close_m), Mon–Fri.
_EQUITY_HOURS = {
    "NASDAQ": (13, 30, 20, 0), "NYSE": (13, 30, 20, 0), "NYSE Arca": (13, 30, 20, 0),
    "CBOE": (13, 30, 20, 0), "LSE": (8, 0, 16, 30), "XETRA": (8, 0, 16, 30),
    "TSE": (0, 0, 6, 0),
}


def _load_raw() -> dict:
    try:
        return json.loads(_CATALOG_PATH.read_text())
    except Exception:  # noqa: BLE001 — never let a bad file crash the universe
        return {}


def _crypto_name(base: str, names: dict) -> str:
    return names.get(base.upper(), base.upper())


def _sym(*, symbol, ticker, name, asset_class, exchange, base="", quote="",
         type_="spot", session="") -> dict:
    return {"symbol": symbol, "ticker": ticker, "name": name,
            "asset_class": asset_class, "exchange": exchange, "base": base,
            "quote": quote, "type": type_, "session": session or _session_for(asset_class, exchange)}


def _session_for(asset_class: str, exchange: str) -> str:
    if asset_class == "crypto":
        return "24/7"
    if asset_class in ("forex", "commodity"):
        return "24x5"
    return exchange or "cash"


# ─────────────────────────── crypto (live CCXT) ───────────────────────────
def _ccxt_crypto(names: dict) -> Optional[list[dict]]:
    """Load live tradable crypto markets from CCXT. Returns None on any failure
    so the caller can fall back to the seed list."""
    exchange = os.environ.get("HUB_EXCHANGE", "binance")
    try:
        import ccxt  # optional dependency
        ex = getattr(ccxt, exchange)({"enableRateLimit": True})
        markets = ex.load_markets()
    except Exception:  # noqa: BLE001 — offline / not installed / blocked IP
        return None
    out = []
    label = exchange.capitalize()
    for m in markets.values():
        try:
            if not m.get("active", True):
                continue
            base, quote = m.get("base"), m.get("quote")
            if not base or not quote:
                continue
            is_swap = bool(m.get("swap") or m.get("future"))
            if not (m.get("spot") or is_swap):
                continue
            out.append(_sym(
                symbol=f"{base}/{quote}", ticker=f"{base}{quote}",
                name=_crypto_name(base, names), asset_class="crypto",
                exchange=label, base=base, quote=quote,
                type_="futures" if is_swap else "spot"))
        except Exception:  # noqa: BLE001 — skip a malformed market, keep the rest
            continue
    return out or None


def _crypto_fallback(raw: dict) -> list[dict]:
    names = raw.get("crypto_names", {})
    out = []
    for c in raw.get("crypto_fallback", []):
        base, quote = c["base"], c["quote"]
        out.append(_sym(symbol=f"{base}/{quote}", ticker=f"{base}{quote}",
                        name=_crypto_name(base, names), asset_class="crypto",
                        exchange="Binance", base=base, quote=quote, type_="spot"))
    return out


# ─────────────────────────── non-crypto (curated) ───────────────────────────
def _reference(raw: dict) -> list[dict]:
    out = []
    for s in raw.get("stocks", []):
        out.append(_sym(symbol=s["ticker"], ticker=s["ticker"], name=s["name"],
                        asset_class="stock", exchange=s["exchange"]))
    for s in raw.get("etf", []):
        out.append(_sym(symbol=s["ticker"], ticker=s["ticker"], name=s["name"],
                        asset_class="etf", exchange=s["exchange"]))
    for s in raw.get("index", []):
        out.append(_sym(symbol=s["ticker"], ticker=s["ticker"], name=s["name"],
                        asset_class="index", exchange=s["exchange"]))
    for s in raw.get("forex", []):
        base, quote = s["base"], s["quote"]
        out.append(_sym(symbol=f"{base}/{quote}", ticker=f"{base}{quote}", name=s["name"],
                        asset_class="forex", exchange="FX", base=base, quote=quote))
    for s in raw.get("commodity", []):
        out.append(_sym(symbol=s["ticker"], ticker=s["ticker"], name=s["name"],
                        asset_class="commodity", exchange=s.get("exchange", "OTC"),
                        session=s.get("session", "24x5")))
    return out


# ─────────────────────────── public API ───────────────────────────
def catalog(*, force: bool = False, ttl: float = 3600.0) -> dict:
    """The full merged universe. Crypto is live from CCXT (cached ``ttl`` s);
    everything else is the curated reference set. Returns
    {symbols, crypto_source, counts}."""
    from services.ttl_cache import cached

    def _build() -> dict:
        raw = _load_raw()
        names = raw.get("crypto_names", {})
        live = _ccxt_crypto(names)
        crypto = live if live is not None else _crypto_fallback(raw)
        crypto_source = "live (ccxt)" if live is not None else "fallback (seed list)"
        symbols = crypto + _reference(raw)
        return {"symbols": symbols, "crypto_source": crypto_source,
                "synced_at": datetime.now(timezone.utc).isoformat()}

    if force:
        from services.ttl_cache import invalidate
        try:
            invalidate("symbol_universe")
        except Exception:  # noqa: BLE001
            pass
    return cached("symbol_universe", ttl, _build)


def _all(force: bool = False) -> list[dict]:
    return catalog(force=force)["symbols"]


def asset_classes(force: bool = False) -> list[dict]:
    counts: dict[str, int] = {}
    for s in _all(force):
        counts[s["asset_class"]] = counts.get(s["asset_class"], 0) + 1
    order = ["crypto", "stock", "etf", "index", "forex", "commodity"]
    return [{"asset_class": k, "count": counts[k]}
            for k in order if k in counts] + \
           [{"asset_class": k, "count": v} for k, v in counts.items() if k not in order]


def search(query: str, *, limit: int = 20) -> list[dict]:
    """Instant search by ticker OR name, ranked: exact ticker, ticker prefix,
    name prefix, then substring. Powers the autocomplete."""
    q = (query or "").strip().upper()
    if not q:
        return []
    scored = []
    for s in _all():
        ticker, sym, name = s["ticker"].upper(), s["symbol"].upper(), s["name"].upper()
        if q in (ticker, sym):
            rank = 0
        elif ticker.startswith(q) or sym.startswith(q):
            rank = 1
        elif name.startswith(q):
            rank = 2
        elif q in name or q in ticker or q in sym:
            rank = 3
        else:
            continue
        # tie-break: prefer the liquid quote (USDT/USD), then the shorter ticker,
        # so "BTC" surfaces BTC/USDT ahead of a thin BTC/… cross.
        quote_pri = 0 if s.get("quote", "").upper() in ("USDT", "USD") else 1
        scored.append((rank, quote_pri, len(ticker), s))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [s for _, _, _, s in scored[:limit]]


def filter_symbols(*, asset_class: str = "", quote: str = "", type: str = "",
                   tickers: Optional[list[str]] = None, limit: int = 500) -> list[dict]:
    """List symbols with optional filters: asset class, quote currency
    (USDT/BTC/…), type (spot/futures), or an explicit ticker set (favorites)."""
    ac = (asset_class or "").lower()
    qc = (quote or "").upper()
    tp = (type or "").lower()
    want = {t.upper() for t in tickers} if tickers else None
    out = []
    for s in _all():
        if ac and s["asset_class"] != ac:
            continue
        if qc and s.get("quote", "").upper() != qc:
            continue
        if tp and s["type"] != tp:
            continue
        if want is not None and s["ticker"].upper() not in want and s["symbol"].upper() not in want:
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def find(symbol: str) -> Optional[dict]:
    key = (symbol or "").upper()
    for s in _all():
        if key in (s["ticker"].upper(), s["symbol"].upper()):
            return s
    return None


def market_status(asset_class: str, exchange: str, *, now: Optional[datetime] = None) -> str:
    """open | closed for the asset's trading session (UTC-based)."""
    now = now or datetime.now(timezone.utc)
    if asset_class == "crypto":
        return "open"                       # 24/7
    wd = now.weekday()                       # 0=Mon .. 6=Sun
    if asset_class in ("forex", "commodity"):
        # ~24x5: opens Sun 21:00 UTC, closes Fri 21:00 UTC
        if wd == 5:
            return "closed"
        if wd == 6:
            return "open" if now.hour >= 21 else "closed"
        if wd == 4:
            return "open" if now.hour < 21 else "closed"
        return "open"
    hrs = _EQUITY_HOURS.get(exchange)
    if hrs is None or wd >= 5:              # unknown exchange or weekend
        return "closed" if wd >= 5 else "open"
    oh, om, ch, cm = hrs
    mins = now.hour * 60 + now.minute
    return "open" if (oh * 60 + om) <= mins < (ch * 60 + cm) else "closed"


def market_info(symbol: str, *, timeframe: str = "1d") -> dict:
    """Current price / 24h change / 24h volume / market status / exchange /
    asset type / session for one symbol. Crypto uses the real cached/live candle
    feed; other classes return status + metadata with price unavailable until a
    data provider is connected (never fabricated)."""
    s = find(symbol)
    if s is None:
        return {"symbol": symbol, "found": False}
    status = market_status(s["asset_class"], s["exchange"])
    info = {"symbol": s["symbol"], "ticker": s["ticker"], "name": s["name"],
            "asset_class": s["asset_class"], "exchange": s["exchange"],
            "type": s["type"], "session": s["session"], "market_status": status,
            "found": True, "price_available": False}
    if s["asset_class"] == "crypto":
        try:
            from data.market_data import get_bars_judged
            rows, src, freshness = get_bars_judged(
                s["ticker"], n=30, timeframe=timeframe, require_real=True)
            if rows and len(rows) >= 2:
                last, prev = rows[-1].close, rows[-2].close
                vol = sum(getattr(b, "volume", 0.0) or 0.0 for b in rows[-1:])
                info.update({
                    "price_available": True, "source": src,
                    "price": round(last, 8),
                    "change_24h_pct": round((last / prev - 1) * 100, 2) if prev else 0.0,
                    "volume_24h": round(vol, 2),
                    "spark": [round(b.close, 8) for b in rows[-30:]],
                    # "price" is the close of the newest CACHED candle, and the
                    # cache has been measured 15.8h behind the venue. Reported
                    # without its age it reads as the current price.
                    "as_of": (freshness.last_close.isoformat()
                              if freshness.last_close else None),
                    "stale": not freshness.fresh,
                    "freshness": freshness.to_dict(),
                })
            else:
                info["source"] = src
                info["note"] = "no real candles cached for this pair yet"
        except Exception as e:  # noqa: BLE001 — quote lookup must never 500
            info["note"] = f"quote unavailable: {type(e).__name__}"
    else:
        # stocks / ETFs / indices / forex / commodities — live via Yahoo (no key).
        try:
            from services import quote_provider
            q = quote_provider.quote(s)
        except Exception:  # noqa: BLE001 — quote lookup must never 500
            q = None
        if q:
            info.update({"price_available": True, "source": q.get("source"),
                         "price": q["price"], "change_24h_pct": q.get("change_24h_pct", 0.0),
                         "volume_24h": q.get("volume_24h")})
        else:
            info["note"] = "live quote unavailable right now (source unreachable) — reference metadata shown"
    return info
