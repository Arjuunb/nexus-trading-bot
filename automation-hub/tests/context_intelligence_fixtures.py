"""Synthetic provider observations for isolated intelligence integration tests.

No external market data or profitability claim is represented by this fixture.
The normal classifier, journal, and accounting interfaces remain in use.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.types import Bar
from services.market_context_observer import MarketContextObserver


def observed_market_context_input(*, symbol="XRPUSDT", observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("synthetic observation clock must be aware")
    observed_at = observed_at.astimezone(timezone.utc)
    closed = datetime.fromtimestamp(int(observed_at.timestamp()) // 300 * 300, timezone.utc)
    opened = closed - timedelta(minutes=5)
    higher_open = datetime.fromtimestamp(int(closed.timestamp()) // 3600 * 3600, timezone.utc) - timedelta(hours=1)

    def series(count, step, final):
        result = []
        for index in range(count):
            price = 100 + (index - count + 1) * .05
            result.append(Bar(final - step * (count - index - 1),
                              price - .03, price + .15, price - .15, price, 10 + index))
        return result

    entry, higher = series(150, timedelta(minutes=5), opened), series(80, timedelta(hours=1), higher_open)
    observer = MarketContextObserver()
    source = "live (binance_usdm_hub)"
    for timeframe, bars in (("5m", entry), ("1h", higher)):
        observer.record_closed_batch(symbol, timeframe, bars, available_at=observed_at,
            source=source, exchange="binance_usdm", market_type="perpetual")
    return observer.freeze(symbol, SimpleNamespace(bars=entry, _native_mtf_context={"1h": higher}),
        entry_timeframe="5m", signal_timestamp=opened, signal_observed_at=observed_at,
        source=source, execution_mode="forward_paper")
