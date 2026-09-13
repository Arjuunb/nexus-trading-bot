"""Every engine that can create a paper order reads one venue: Binance USD-M.

The repository does carry multi-venue plumbing — an instance's `exchange` field
accepts kraken/coinbase/bybit, the SMC visual chart can render MEXC or Kraken,
and the funding-rate context widget falls back across venues when Binance blocks
the host IP. None of that may reach a trading decision or a fill. Two engines
comparing strategies are only comparable on one book, and a stop priced against
Kraken spot is not the stop the position has on a Binance perp.

These tests pin the single source at the places where it would actually cost
money: the sockets the shared hub opens, and the funding the paper accounts
charge to open positions.
"""
from pathlib import Path

import pytest

from data.market_data_v2 import TF_MS, MarketDataService
from services.price_action_stream import PriceActionPublicStream

ROOT = Path(__file__).parents[1]

# Hosts and ccxt ids for venues that must never appear on the forward-paper
# data path. MEXC and Kraken are reachable from the visual parity chart, which
# is a different module and is marked VIEW_ONLY_ALTERNATE by its route.
FOREIGN = ("bybit.com", "okx.com", "mexc.com", "kraken.com", "coinbase.com",
           "kucoin.com", "bitfinex.com", "huobi", "gate.io")

# The modules a candle or quote crosses between Binance and a paper fill.
FORWARD_PAPER_PATH = (
    "services/forward_paper_hub.py",
    "services/price_action_stream.py",
    "services/native_context_loader.py",
    "services/mtf_policy.py",
    "execution/paper_broker_v2.py",
    "execution/paper_engine.py",
)


def _stream():
    return PriceActionPublicStream(lambda *_a, **_k: [])


@pytest.mark.parametrize("timeframe", sorted(TF_MS))
def test_both_hub_sockets_are_binance_usdm_on_every_timeframe(timeframe):
    stream = _stream()
    stream.symbol, stream.timeframe = "BTCUSDT", timeframe
    assert stream.market_url.startswith("wss://fstream.binance.com/")
    assert stream.public_url.startswith("wss://fstream.binance.com/")
    # And the streams named are the ones the engines actually consume.
    assert f"btcusdt@kline_{timeframe}" in stream.market_url
    assert "btcusdt@markPrice@1s" in stream.market_url
    assert "btcusdt@bookTicker" in stream.public_url


def test_no_alternate_venue_appears_on_the_forward_paper_path():
    offenders = {}
    for relative in FORWARD_PAPER_PATH:
        text = (ROOT / relative).read_text().lower()
        hits = [name for name in FOREIGN if name in text]
        if hits:
            offenders[relative] = hits
    assert not offenders, "alternate venues reachable from a paper fill: %s" % offenders


def test_the_funding_charged_to_paper_positions_comes_only_from_binance(tmp_path):
    """Funding is real money in the ledger, so it must match the traded book.

    The context widget that fills the dashboard's funding tile deliberately
    falls back Binance -> Bybit -> OKX and labels which answered. That one is
    display. This is the one the accounts charge against open positions, and it
    has no fallback at all.
    """
    service = MarketDataService(str(tmp_path))
    requested: list[str] = []

    def record(url, params=None, **_kwargs):
        requested.append(url)
        if url.endswith("bookTicker"):
            return {"bidPrice": "100.0", "askPrice": "100.2"}
        if url.endswith("premiumIndex"):
            return {"markPrice": "100.1", "indexPrice": "100.05",
                    "nextFundingTime": "1789000000000", "time": "1788995000000"}
        return [{"fundingRate": "0.0001", "fundingTime": "1788990000000"}]

    service._requesters = dict(service._requesters)
    service._requesters["binance-futures"] = record
    service.public_usdm_quote("BTCUSDT")

    assert requested, "public_usdm_quote issued no request at all"
    for url in requested:
        assert url.startswith("https://fapi.binance.com/"), url


def test_an_instance_on_the_hub_cannot_report_a_non_binance_exchange():
    """Asserted on source because the override is one expression inside start().

    Building a full TradingInstanceManager with a live hub to reach it would
    test the fixture more than the rule. What matters is that the hub branch
    wins over the stored field: an operator may save exchange=kraken, and the
    provenance written to the journal must still be the venue the candles and
    quotes actually came from, never the label.
    """
    source = (ROOT / "services/trading_instances.py").read_text()
    assert 'exchange = ("binance_usdm" if self.market_hub is not None and forward else' in source
    assert '"market_data_source": ("Binance USD-M public WebSocket"' in source
    assert 'if self.market_hub is not None and forward else None)' in source
