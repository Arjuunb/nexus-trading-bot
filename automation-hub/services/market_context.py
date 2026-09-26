"""Real-world market context for the Evolution page.

Pulls live data from OFFICIAL, public APIs. No-key sources (Fear & Greed,
CoinGecko global + ETH/BTC, Binance funding rate, Binance open interest) are
fetched directly. Key-gated sources (news, liquidations, economic calendar,
social) are only attempted when a key is configured; otherwise they report
"Not connected" — never fabricated. Every fetch fails closed.

API keys can be set in the UI (a local JSON store) or via env vars. We never
scrape private/restricted platforms.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from services.sentiment import _get_json, label_mood, risk_mode

_BINANCE_FAPI = ("https://fapi.binance.com", "https://www.binance.com")

# ---- response cache + per-provider freshness/error tracking ----------------
# Successful fetches are cached for a short TTL so the page is fast and we don't
# hammer public APIs; failures are NOT cached (they retry next call). Each entry
# records when it last refreshed and any error, feeding the debug panel.
_CACHE: dict = {}


def _econ_calendar_status() -> dict:
    """What the event guard's calendar actually holds (services/econ_feed.py)."""
    try:
        import webhook_api as _wa
        feed = _wa.econ_feed.status()
        connected = _wa.econ_calendar.connected
    except Exception:  # noqa: BLE001 -- context is informational
        return {"available": False, "connected": False, "note": "Economic calendar status unavailable."}
    if connected:
        note = (f"{feed['events']} high-impact {'/'.join(feed['countries'])} releases this week "
                f"(fetched {feed['last_success']})" if feed.get("last_success") else "Events added by hand.")
    else:
        note = ("Not connected — " + (f"the calendar feed has not fetched yet ({feed['last_error']})."
                                      if feed.get("last_error")
                                      else "the calendar feed is turned off (HUB_ECON_FEED=off)."
                                      if not feed.get("enabled") else "the calendar feed has not fetched yet."))
    return {"available": bool(connected), "connected": bool(connected), "note": note, "feed": feed}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _cached(key: str, fn, ttl: float):
    """Run ``fn`` with a TTL cache keyed by ``key``. Only results that are
    ``available`` (or non-None) are cached; anything else retries next call.
    Records last-update time + error for the developer debug panel."""
    now = time.time()
    ent = _CACHE.get(key)
    if ent and ent.get("ok") and (now - ent["ts"]) < ttl:
        ent["from_cache"] = True
        return ent["value"]
    try:
        val = fn()
        ok = bool(val.get("available")) if isinstance(val, dict) else val is not None
        err = None if ok else (val.get("note") if isinstance(val, dict) else "no data")
    except Exception as e:  # noqa: BLE001 — fail closed, record the error
        _CACHE[key] = {"value": None, "ts": now, "ok": False, "error": str(e)[:200], "from_cache": False}
        return {"available": False, "value": None, "note": f"fetch error: {str(e)[:120]}"}
    _CACHE[key] = {"value": val, "ts": now, "ok": ok, "error": err, "from_cache": False}
    return val


# provider id -> the cache key whose freshness/error it reflects
_PID_CACHE = {"fear_greed": "fear_greed", "coingecko": "global",
              "binance_funding": "funding", "binance_oi": "oi", "news": "news"}


def provider_debug(settings: "ProviderSettings") -> list:
    """Developer debug rows: name, connection status, last update, errors,
    freshness — for every configured provider."""
    now = time.time()
    out = []
    for p in PROVIDERS:
        connected = (not p["needs_key"]) or bool(settings.key(p["id"]))
        ent = _CACHE.get(_PID_CACHE.get(p["id"], ""))
        ts = ent["ts"] if ent else None
        if not connected:
            status = "Not connected"
        elif ent is None:
            status = "Idle (not fetched yet)"
        elif ent.get("ok"):
            status = "Live (cached)" if ent.get("from_cache") else "Live"
        else:
            status = "Data unavailable"
        out.append({
            "id": p["id"], "label": p["label"], "connected": connected,
            "status": status,
            "last_update": _iso(ts),
            "freshness_s": round(now - ts) if ts else None,
            "error": (ent or {}).get("error"),
        })
    return out

# Provider key fields shown in the settings panel: (id, label, env var, needs_key)
PROVIDERS = [
    {"id": "fear_greed", "label": "Crypto Fear & Greed (alternative.me)", "env": None, "needs_key": False},
    {"id": "coingecko", "label": "CoinGecko (dominance / mcap / ETHBTC)", "env": "COINGECKO_API_KEY", "needs_key": False},
    {"id": "binance_funding", "label": "Binance funding rate", "env": None, "needs_key": False},
    {"id": "binance_oi", "label": "Binance open interest", "env": None, "needs_key": False},
    {"id": "news", "label": "Crypto news (CryptoPanic token)", "env": "CRYPTOPANIC_TOKEN", "needs_key": True},
    {"id": "liquidations", "label": "Liquidation data (Coinglass key)", "env": "COINGLASS_API_KEY", "needs_key": True},
    {"id": "econ_calendar", "label": "Economic calendar (provider key)", "env": "ECON_CALENDAR_KEY", "needs_key": True},
    {"id": "twitter", "label": "X / Twitter API (optional)", "env": "TWITTER_BEARER_TOKEN", "needs_key": True},
    {"id": "reddit", "label": "Reddit API (optional)", "env": "REDDIT_CLIENT_ID", "needs_key": True},
]


class ProviderSettings:
    """Local JSON store for provider API keys (gitignored). UI-settable."""
    def __init__(self, path: str):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            if self.path.exists():
                return json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            pass
        return {}

    def save(self, keys: dict) -> dict:
        data = self._load()
        # only keep known provider ids; ignore blanks (don't wipe existing on blank)
        for p in PROVIDERS:
            if p["id"] in keys and keys[p["id"]]:
                data[p["id"]] = str(keys[p["id"]]).strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        return self.status()

    def key(self, pid: str) -> str | None:
        data = self._load()
        if data.get(pid):
            return data[pid]
        env = next((p["env"] for p in PROVIDERS if p["id"] == pid), None)
        return os.environ.get(env) if env else None

    def status(self) -> list:
        """Per-provider connection status (never exposes the key value)."""
        out = []
        for p in PROVIDERS:
            connected = (not p["needs_key"]) or bool(self.key(p["id"]))
            out.append({"id": p["id"], "label": p["label"], "needs_key": p["needs_key"],
                        "connected": connected})
        return out


# ---- individual real fetchers (fail closed) ----
def fetch_funding_rate(symbol: str = "BTCUSDT"):
    """Perp funding rate — Binance, then Bybit, then OKX (all keyless).
    Binance futures blocks many cloud IPs; the fallbacks cover that."""
    for host in _BINANCE_FAPI:
        d = _get_json(f"{host}/fapi/v1/premiumIndex?symbol={symbol}")
        try:
            return {"available": True, "value": round(float(d["lastFundingRate"]) * 100, 4),
                    "symbol": symbol, "source": "binance"}
        except Exception:  # noqa: BLE001
            continue
    d = _get_json(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    try:
        row = d["result"]["list"][0]
        return {"available": True, "value": round(float(row["fundingRate"]) * 100, 4),
                "symbol": symbol, "source": "bybit"}
    except Exception:  # noqa: BLE001
        pass
    base = symbol.replace("USDT", "")
    d = _get_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={base}-USDT-SWAP")
    try:
        return {"available": True, "value": round(float(d["data"][0]["fundingRate"]) * 100, 4),
                "symbol": symbol, "source": "okx"}
    except Exception:  # noqa: BLE001
        pass
    return {"available": False, "value": None, "symbol": symbol,
            "note": "No perp venue reachable (Binance/Bybit/OKX all failed)."}


def fetch_open_interest(symbol: str = "BTCUSDT"):
    """Perp open interest — Binance, then Bybit, then OKX (all keyless)."""
    for host in _BINANCE_FAPI:
        d = _get_json(f"{host}/fapi/v1/openInterest?symbol={symbol}")
        try:
            return {"available": True, "value": round(float(d["openInterest"]), 1),
                    "symbol": symbol, "source": "binance"}
        except Exception:  # noqa: BLE001
            continue
    d = _get_json(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}")
    try:
        row = d["result"]["list"][0]
        return {"available": True, "value": round(float(row["openInterest"]), 1),
                "symbol": symbol, "source": "bybit"}
    except Exception:  # noqa: BLE001
        pass
    base = symbol.replace("USDT", "")
    d = _get_json(f"https://www.okx.com/api/v5/public/open-interest?instId={base}-USDT-SWAP")
    try:
        return {"available": True, "value": round(float(d["data"][0]["oiCcy"]), 1),
                "symbol": symbol, "source": "okx"}
    except Exception:  # noqa: BLE001
        pass
    return {"available": False, "value": None, "symbol": symbol,
            "note": "No perp venue reachable (Binance/Bybit/OKX all failed)."}


def _eth_btc_from_local_candles():
    """ETH/BTC 30d from OUR OWN cached real candles (zero network) — the most
    reliable source once /data/backfill has run."""
    from config import settings as _settings
    from data.historical import HistoricalStore
    store = HistoricalStore(_settings.market_db)
    eth = store.get_bars("ETHUSDT", "1d", n=31)
    btc = store.get_bars("BTCUSDT", "1d", n=31)
    if len(eth) < 20 or len(btc) < 20:
        return None
    n = min(len(eth), len(btc))
    first = eth[-n].close / btc[-n].close
    last = eth[-1].close / btc[-1].close
    chg = (last - first) / first * 100 if first else 0.0
    trend = "Bullish" if chg > 1 else "Bearish" if chg < -1 else "Neutral"
    return {"available": True, "ratio": round(last, 6), "change_30d_pct": round(chg, 2),
            "trend": trend, "source": "local candles (real)"}


def fetch_eth_btc():
    """ETH/BTC 30d ratio — local cached candles first, CoinGecko as fallback."""
    try:
        local = _eth_btc_from_local_candles()
        if local:
            return local
    except Exception:  # noqa: BLE001 — empty/missing store -> network fallback
        pass
    d = _get_json("https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=btc&days=30")
    try:
        prices = [p[1] for p in d["prices"]]
        first, last = prices[0], prices[-1]
        chg = (last - first) / first * 100 if first else 0.0
        trend = "Bullish" if chg > 1 else "Bearish" if chg < -1 else "Neutral"
        return {"available": True, "ratio": round(last, 6), "change_30d_pct": round(chg, 2),
                "trend": trend, "source": "coingecko"}
    except Exception:  # noqa: BLE001
        return {"available": False, "trend": None,
                "note": "No source — load real data (backfill) or wait for CoinGecko."}


def fetch_news(token: str | None, limit: int = 6):
    if not token:
        return {"available": False, "connected": False,
                "note": "Not connected — add a CryptoPanic token in Data Providers.", "headlines": []}
    d = _get_json(f"https://cryptopanic.com/api/v1/posts/?auth_token={token}&public=true&currencies=BTC,ETH")
    try:
        items = [{"title": r["title"], "url": r.get("url", ""),
                  "published": r.get("published_at", "")} for r in d["results"][:limit]]
        return {"available": True, "connected": True, "headlines": items}
    except Exception:  # noqa: BLE001
        return {"available": False, "connected": True, "headlines": [],
                "note": "News provider returned no data."}


# ---- aggregate ----
def market_context(settings: ProviderSettings) -> dict:
    from services.sentiment import fetch_fear_greed, fetch_global

    def _fg_block():
        fg = fetch_fear_greed()
        return {"available": fg is not None,
                "value": fg["value"] if fg else None,
                "label": fg["classification"] if fg else None,
                "mood": label_mood(fg["value"]) if fg else None}

    def _global():
        glob = fetch_global()
        return {"available": glob is not None,
                "btc_dominance": (glob or {}).get("btc_dominance"),
                "total_mcap_usd": (glob or {}).get("total_mcap_usd")}

    fg_block = _cached("fear_greed", _fg_block, ttl=60)
    glob_block = _cached("global", _global, ttl=120)
    fg = fg_block if fg_block.get("available") else None

    out = {
        "fear_greed": fg_block,
        "btc_dominance": {"available": glob_block.get("available"), "value": glob_block.get("btc_dominance")},
        "total_mcap_usd": {"available": glob_block.get("available"), "value": glob_block.get("total_mcap_usd")},
        "eth_btc": _cached("eth_btc", fetch_eth_btc, ttl=300),
        "funding_rate": _cached("funding", lambda: fetch_funding_rate("BTCUSDT"), ttl=60),
        "open_interest": _cached("oi", lambda: fetch_open_interest("BTCUSDT"), ttl=60),
        "news": _cached("news", lambda: fetch_news(settings.key("news")), ttl=180),
        # key-gated, honestly reported as not connected unless configured
        "liquidations": {"available": False, "connected": bool(settings.key("liquidations")),
                         "note": "Not connected — add a Coinglass API key in Data Providers."
                                 if not settings.key("liquidations") else
                                 "Configured — provider endpoint not wired yet."},
        "economic_calendar": _econ_calendar_status(),
        "providers": settings.status(),
        "provider_debug": provider_debug(settings),
        "last_updated": _iso(time.time()),
    }
    # plain-English sentiment summary from whatever is available
    dom = glob_block.get("btc_dominance") if glob_block.get("available") else None
    if fg:
        mood = label_mood(fg["value"])
        out["sentiment_summary"] = (f"{mood} (Fear & Greed {fg['value']}). Risk mode: {risk_mode(mood)}."
                                    + (f" BTC dominance {dom}%." if dom is not None else ""))
    else:
        out["sentiment_summary"] = ("Live sentiment sources unavailable — not faking a value. "
                                    "Run on a host with network/API access.")
    return out
