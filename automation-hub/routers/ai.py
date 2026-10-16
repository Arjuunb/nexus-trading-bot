"""AI Trading Intelligence endpoints.

On-demand pre-trade analysis (`/ai/analyze`), the auto-updating trader profile
(`/ai/profile`), and the confidence legend. All compose the engine's existing
intelligence — nothing here re-implements strategy, risk or memory logic.
"""
import webhook_api as _wa
from fastapi import APIRouter, Query
from typing import Optional

from services import ai_intelligence as _ai

router = APIRouter()


@router.get("/ai/analyze")
def ai_analyze(symbol: str = "BTCUSDT", timeframe: str = "1h",
               side: Optional[str] = Query(None, description="long|short — omit to infer from bias"),
               leverage: float = 1.0,
               min_score: Optional[int] = None):
    """Full AI pre-trade analysis for a symbol: five-category setup score,
    confidence level, BUY/SELL/WAIT/SKIP with reasons, and the risk analysis.

    Cached briefly (per symbol/timeframe/side/leverage) so repeated dashboard
    polls don't re-run the read or re-fetch candles."""
    from data.market_data import get_bars_judged
    from services.ttl_cache import cached

    equity = _wa.paper.balance()
    risk_pct = getattr(_wa.pipeline, "risk_per_trade_pct", None) or getattr(_wa.settings, "risk_per_trade_pct", 0.01)
    ms = int(min_score if min_score is not None else getattr(_wa.engine, "min_quality_score", 60) or 60)
    key = f"ai:analyze:{symbol}:{timeframe}:{side}:{leverage}:{ms}:{round(equity, 2)}"

    def _run() -> dict:
        bars, source, freshness = get_bars_judged(symbol, n=250, timeframe=timeframe)
        if not bars:
            return {"available": False, "symbol": symbol, "note": "no market data"}
        out = _ai.analyze_setup(symbol=symbol, timeframe=timeframe, bars=bars, side=side,
                                equity=equity, risk_pct=float(risk_pct), min_score=ms,
                                leverage=float(leverage))
        out["available"] = True
        out["data_source"] = source
        # This is a scored, sized setup someone acts on. Read from candles that
        # closed hours ago it describes a setup that may no longer exist, and
        # nothing in the response said which candles it was.
        out["as_of"] = freshness.last_close.isoformat() if freshness.last_close else None
        out["stale"] = not freshness.fresh
        out["freshness"] = freshness.to_dict()
        if not freshness.fresh:
            out["note"] = ("Computed from candles that are not current — treat this "
                           "as a reading of that moment, not of the market now.")
        return out

    return cached(key, 20.0, _run)


@router.get("/ai/insights")
def ai_insights(symbols: Optional[str] = None, timeframe: Optional[str] = None):
    """Live market insights (trend / volume / liquidity / reversal / volatility)
    across the tracked symbols — natural-language reads of the real candles."""
    from data.market_data import get_bars_judged
    from services import market_analysis
    from services.ttl_cache import cached

    syms = ([s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols
            else list(getattr(_wa.engine, "symbols", []) or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]))[:8]
    tf = timeframe or getattr(_wa.engine, "timeframe", "1h")
    key = f"ai:insights:{','.join(syms)}:{tf}"

    def _run() -> dict:
        reads, stale = [], []
        for sym in syms:
            try:
                bars, _, freshness = get_bars_judged(sym, n=120, timeframe=tf)
                if bars:
                    reads.append({"symbol": sym, "ma": market_analysis.analyze(bars)})
                    if not freshness.fresh:
                        stale.append(sym)
            except Exception:  # noqa: BLE001
                continue
        # Commentary on "the market" mixing current and day-old symbols is not
        # commentary on the market, and the reader cannot tell which is which.
        return {"insights": _ai.market_insights(reads), "symbols": syms,
                "timeframe": tf, "stale_symbols": stale, "fresh": not stale}

    return cached(key, 30.0, _run)


@router.get("/ai/profile")
def ai_profile():
    """The trader's personal profile (strengths / weaknesses), distilled from the
    trade-memory pattern-recognition insights — updates as trades close."""
    try:
        insights = _wa.trade_memory.insights()
    except Exception:  # noqa: BLE001 — no memory yet -> empty profile
        insights = {}
    return _ai.trader_profile(insights)


@router.get("/ai/alerts")
def ai_alerts():
    """Live AI alert feed: strong / weak setups, risk over limit, trading halt /
    max daily loss, and outside-session — all from real current state. Cached
    briefly so it doesn't re-analyse every poll."""
    from datetime import datetime, timezone
    from data.market_data import get_bars_judged
    from services.ttl_cache import cached

    def _run() -> dict:
        symbols = list(getattr(_wa.engine, "symbols", []) or [])[:6]
        equity = _wa.paper.balance()
        risk_pct = getattr(_wa.pipeline, "risk_per_trade_pct", 0.01) or 0.01
        ms = int(getattr(_wa.engine, "min_quality_score", 60) or 60)
        tf = getattr(_wa.engine, "timeframe", "1h")
        analyses = []
        for sym in symbols:
            try:
                bars, _, freshness = get_bars_judged(sym, n=250, timeframe=tf)
                if bars:
                    a = _ai.analyze_setup(symbol=sym, timeframe=tf, bars=bars,
                                          equity=equity, risk_pct=float(risk_pct), min_score=ms)
                    a["available"] = True
                    a["stale"] = not freshness.fresh
                    a["as_of"] = (freshness.last_close.isoformat()
                                  if freshness.last_close else None)
                    analyses.append(a)
            except Exception:  # noqa: BLE001 — one bad symbol shouldn't drop the feed
                continue

        risk = {
            "exposure_pct": (sum(p["size"] * p["entry"] for p in _wa.paper.positions()) / equity) if equity else 0.0,
            "exposure_limit_pct": getattr(_wa.settings, "exposure_limit_pct", None),
            "trading_state": _wa.controls.state,
            "auto_halted": getattr(_wa.pipeline, "halted", False),
            "halt_reason": getattr(_wa.pipeline, "halt_reason", None),
        }

        # preferred-session check from the pipeline's configured window (UTC)
        now = datetime.now(timezone.utc)
        s, e = getattr(_wa.pipeline, "session_start", 0), getattr(_wa.pipeline, "session_end", 24)
        mask = getattr(_wa.pipeline, "trading_days_mask", 127)
        in_session = ((s <= now.hour < e) if s < e else True) and bool((mask >> now.weekday()) & 1)
        window = f"{s:02d}:00–{e:02d}:00 UTC" if (s, e) != (0, 24) else ""

        alerts = _ai.evaluate_alerts(analyses, risk, in_session=in_session,
                                     session_window=window, min_score=ms)
        return {"alerts": alerts, "count": len(alerts), "checked": symbols}

    return cached("ai:alerts", 30.0, _run)


@router.get("/ai/confidence-accuracy")
def ai_confidence_accuracy():
    """Confidence calibration: do higher-confidence setups actually win more?
    Computed from the closed-trade memory (real outcomes)."""
    try:
        rows = _wa.trade_memory.store.list(limit=100000)
    except Exception:  # noqa: BLE001
        rows = []
    return _ai.confidence_accuracy(rows)


@router.get("/ai/coach")
def ai_coach():
    """Daily-style AI coach over the real closed trades (reuses the trade-memory
    insights): trade count, win rate, main mistake, a suggestion, risk discipline."""
    try:
        insights = _wa.trade_memory.insights()
    except Exception:  # noqa: BLE001
        insights = {}
    return _ai.daily_coach(insights)


@router.get("/ai/recommendations")
def ai_recommendations():
    """Actionable risk-setting recommendations, each mapping to one editable
    setting with a concrete suggested value (the frontend can apply in a click).
    Config-hygiene items come from the current settings; behaviour items require
    real coach evidence."""
    try:
        editable = _wa._settings_snapshot()
    except Exception:  # noqa: BLE001
        editable = {}
    try:
        coach = _ai.daily_coach(_wa.trade_memory.insights())
    except Exception:  # noqa: BLE001
        coach = {}
    return _ai.settings_recommendations(editable, coach)


@router.get("/ai/confidence-levels")
def ai_confidence_levels():
    """Static legend: the score band each confidence level maps to."""
    return {"levels": [
        {"level": "Very High", "min_score": 85},
        {"level": "High", "min_score": 70},
        {"level": "Medium", "min_score": 55},
        {"level": "Low", "min_score": 40},
        {"level": "Very Low", "min_score": 0},
    ]}
