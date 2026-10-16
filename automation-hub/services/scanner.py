"""Market Scanner — rank real-time setups across symbols.

Scans the REAL Binance candle store for six setup types and scores each 0-100:
  * Breakout            — close beyond the N-bar range (+ volume)
  * Liquidity sweep     — swept a prior swing and reclaimed/rejected
  * High volume         — last bar volume well above its average
  * Strong momentum     — RSI extreme aligned with the EMA trend
  * Trend continuation  — EMA8>EMA21>EMA50 (or inverse) with price leading
  * Pullback            — dip to the EMA in a trend, closed back in trend

``scan_bars`` is pure (operates on a Bar list) so it is fully unit-testable;
``scan`` adds the real-data load and ranking.
"""
from __future__ import annotations

from typing import Optional

from bot.data.indicators import ema, rsi, atr

SETUP_TYPES = ("Breakout", "Liquidity sweep", "High volume",
               "Strong momentum", "Trend continuation", "Pullback")


def _clamp(x, lo=0, hi=100):
    return int(max(lo, min(hi, round(x))))


def scan_bars(bars, *, lookback: int = 20) -> list:
    """Return the setup signals firing on the LAST bar of ``bars`` (causal)."""
    n = len(bars)
    if n < max(55, lookback + 5):
        return []
    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [b.volume or 0.0 for b in bars]
    last = bars[-1]
    ema_f, ema_s, ema_50 = ema(closes, 8)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    prior_high = max(highs[-lookback - 1:-1])
    prior_low = min(lows[-lookback - 1:-1])
    vavg = sum(vols[-lookback - 1:-1]) / lookback
    vol_ratio = (last.volume / vavg) if vavg > 0 else 1.0
    r = rsi(closes, 14)
    a = atr(bars, 14) or (last.close * 0.001)
    up, down = ema_f > ema_s, ema_f < ema_s
    sig = []

    # breakout
    if last.close > prior_high:
        sig.append({"type": "Breakout", "side": "long",
                    "strength": _clamp(40 + (last.close - prior_high) / a * 30 + (vol_ratio - 1) * 20),
                    "detail": f"close > {lookback}-bar high, vol {vol_ratio:.1f}x"})
    elif last.close < prior_low:
        sig.append({"type": "Breakout", "side": "short",
                    "strength": _clamp(40 + (prior_low - last.close) / a * 30 + (vol_ratio - 1) * 20),
                    "detail": f"close < {lookback}-bar low, vol {vol_ratio:.1f}x"})

    # liquidity sweep (swept a prior swing and closed back through it)
    if last.low < prior_low and last.close > prior_low:
        sig.append({"type": "Liquidity sweep", "side": "long",
                    "strength": _clamp(45 + (prior_low - last.low) / a * 25 + (vol_ratio - 1) * 15),
                    "detail": "swept the prior low and reclaimed it"})
    elif last.high > prior_high and last.close < prior_high:
        sig.append({"type": "Liquidity sweep", "side": "short",
                    "strength": _clamp(45 + (last.high - prior_high) / a * 25 + (vol_ratio - 1) * 15),
                    "detail": "swept the prior high and rejected"})

    # high volume
    if vol_ratio >= 1.5:
        sig.append({"type": "High volume", "side": "long" if last.close >= last.open else "short",
                    "strength": _clamp(40 + (vol_ratio - 1.5) * 40),
                    "detail": f"volume {vol_ratio:.1f}x the {lookback}-bar average"})

    # strong momentum (RSI aligned with the EMA trend)
    if r >= 60 and up:
        sig.append({"type": "Strong momentum", "side": "long",
                    "strength": _clamp((r - 50) * 2 + 8), "detail": f"RSI {r:.0f} with EMA8>EMA21"})
    elif r <= 40 and down:
        sig.append({"type": "Strong momentum", "side": "short",
                    "strength": _clamp((50 - r) * 2 + 8), "detail": f"RSI {r:.0f} with EMA8<EMA21"})

    # trend continuation (stacked EMAs, price leading)
    if up and ema_s > ema_50 and last.close > ema_f:
        sig.append({"type": "Trend continuation", "side": "long",
                    "strength": _clamp(50 + (last.close - ema_50) / ema_50 * 300),
                    "detail": "EMA8>EMA21>EMA50, price leading"})
    elif down and ema_s < ema_50 and last.close < ema_f:
        sig.append({"type": "Trend continuation", "side": "short",
                    "strength": _clamp(50 + (ema_50 - last.close) / ema_50 * 300),
                    "detail": "EMA8<EMA21<EMA50, price leading"})

    # pullback (dip/pop to the EMA inside a trend, closed back in trend)
    if up and last.low <= ema_f * 1.004 and last.close > ema_f:
        sig.append({"type": "Pullback", "side": "long",
                    "strength": _clamp(45 + (8 if r < 60 else 0)),
                    "detail": "dip to the EMA in an uptrend, closed above"})
    elif down and last.high >= ema_f * 0.996 and last.close < ema_f:
        sig.append({"type": "Pullback", "side": "short",
                    "strength": _clamp(45 + (8 if r > 40 else 0)),
                    "detail": "pop to the EMA in a downtrend, closed below"})

    sig.sort(key=lambda s: s["strength"], reverse=True)
    return sig


def scan(symbols, timeframe: str = "4h", bars: int = 300,
         types: Optional[list] = None, loader=None, now=None) -> dict:
    """Scan ``symbols`` over real candles and rank the opportunities.

    Every row carries the age of the candles it was computed from. The store
    this reads can be hours behind the venue with nothing in the return value
    saying so -- it was observed 15.8h stale for BTCUSDT and absent for
    BNBUSDT -- and a ranked "opportunity" with a last price is read as a
    statement about now. Ranking is unchanged: this reports the age, it does
    not reorder, filter or suppress on it. What to do about a stale row is the
    caller's decision, made with the number in hand instead of without it.
    """
    from data.market_data import get_bars
    from services.market_data_freshness import judge_bars
    results, opps = [], []
    tset = set(types) if types else None
    for sym in symbols:
        if loader is not None:
            rows, src = loader(sym, timeframe, bars)
        else:
            rows, src = get_bars(sym, n=max(60, min(int(bars), 1000)), timeframe=timeframe, require_real=True)
        verdict = judge_bars(sym, timeframe, rows, now=now)
        age = {"as_of": verdict.last_close.isoformat() if verdict.last_close else None,
               "stale": not verdict.fresh, "freshness": verdict.to_dict()}
        if not rows:
            results.append({"symbol": sym, "available": False, "source": src,
                            "signals": [], "score": 0, **age})
            continue
        sigs = scan_bars(rows)
        if tset:
            sigs = [s for s in sigs if s["type"] in tset]
        score = sigs[0]["strength"] if sigs else 0
        bias = "long" if sum(1 for s in sigs if s["side"] == "long") > sum(1 for s in sigs if s["side"] == "short") \
            else "short" if sigs else "—"
        results.append({"symbol": sym, "available": True, "source": src, "signals": sigs,
                        "score": score, "bias": bias, "last": round(rows[-1].close, 6), **age})
        # An opportunity travels to the UI on its own, detached from its row,
        # so it has to carry its own age or it arrives looking current.
        for s in sigs:
            opps.append({"symbol": sym, **s, "as_of": age["as_of"], "stale": age["stale"]})
    opps.sort(key=lambda o: o["strength"], reverse=True)
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    stale = [r["symbol"] for r in results if r.get("stale")]
    return {"timeframe": timeframe, "symbols": results, "opportunities": opps,
            "count": len(opps), "stale_symbols": stale,
            # False the moment ANY scanned symbol is behind: a ranking mixing
            # current and day-old symbols is not a ranking.
            "fresh": not stale}
