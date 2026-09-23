"""The Binance USD-M market-data endpoint, pinned to what the venue serves.

Binance routes public futures market data by CATEGORY, carried as a path
segment: bookTicker and depth come from /public/ws, kline and markPrice from
/market/ws. A socket that names no category is served as /public. It completes
the handshake, accepts a SUBSCRIBE for a market-category stream, acknowledges
it, lists it under LIST_SUBSCRIPTIONS, and never delivers one frame.

Every wrong URL in this file's history failed silently, which is why the file
exists. Two of them shipped:

  * /market/stream?streams=... and /public/stream?streams=... -- the right
    category on a path shape the venue does not serve.
  * /stream?streams=... -- a real endpoint, correctly formed, and unrouted, so
    bookTicker flowed and kline and markPrice were accepted and never sent.

The second was committed as a fix for the first and made the feed look
healthier while leaving it just as dead: the channel that mattered was still
silent, and the one that worked proved nothing about it. Measured against the
live venue on 2026-09-21 from two networks:

    /public/ws  + SUBSCRIBE bookTicker            3,616 frames / 15s
    /market/ws  + SUBSCRIBE kline_5m, markPrice   first frame at +0.38s
    /ws         + SUBSCRIBE kline_5m              acknowledged, 0 frames

So these tests assert the category is present, and refuse every unrouted form.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from services.price_action_stream import (BINANCE_USDM_CATEGORY,
                                          BINANCE_USDM_ROOT,
                                          PriceActionPublicStream)

#: Path shapes that either do not exist or are served as /public regardless of
#: what is subscribed. None of them can carry a kline.
UNROUTED_PATHS = ("/stream", "/ws")


def feed_for(symbol="BTCUSDT", timeframe="5m", *, quotes=True):
    feed = PriceActionPublicStream(lambda *a, **k: [], quotes_enabled=quotes)
    feed.symbol, feed.timeframe = symbol, timeframe
    return feed


def test_btcusdt_5m_opens_both_routed_endpoints():
    feed = feed_for()

    assert feed.market_url == "wss://fstream.binance.com/market/ws"
    assert feed.public_url == "wss://fstream.binance.com/public/ws"
    assert feed.url == feed.market_url


def test_the_market_channel_subscribes_to_kline_and_mark_price():
    assert feed_for().market_subscriptions == ["btcusdt@kline_5m",
                                               "btcusdt@markPrice@1s"]


def test_the_public_channel_subscribes_to_book_ticker_alone():
    assert feed_for().public_subscriptions == ["btcusdt@bookTicker"]


def test_a_kline_only_channel_does_not_ask_for_mark_price():
    feed = feed_for(quotes=False)

    assert feed.market_subscriptions == ["btcusdt@kline_5m"]
    assert not any("markPrice" in name for name in feed.market_subscriptions)


@pytest.mark.parametrize("path", UNROUTED_PATHS)
def test_no_url_falls_back_to_an_unrouted_path(path):
    """An unrouted socket is the failure that looks like success."""
    feed = feed_for()

    for url in (feed.market_url, feed.public_url):
        assert urlsplit(url).path != path


@pytest.mark.parametrize("channel, category", [("market", "market"),
                                               ("public", "public")])
def test_every_url_names_its_category_as_the_first_path_segment(channel, category):
    url = getattr(feed_for(), f"{channel}_url")
    segments = [part for part in urlsplit(url).path.split("/") if part]

    assert segments == [category, "ws"]


def test_no_url_carries_a_query_string():
    """The ?streams= form belongs to the unrouted endpoint. A category and a
    query string cannot be combined, and a URL carrying both is the earlier
    bug rebuilt."""
    feed = feed_for()

    for url in (feed.market_url, feed.public_url):
        assert urlsplit(url).query == ""


def test_the_two_channels_do_not_share_one_endpoint():
    """They are different categories at the venue. If a refactor ever makes
    these equal, one of the two is being served the wrong data."""
    feed = feed_for()

    assert feed.market_url != feed.public_url


def test_the_root_constant_carries_no_path_of_its_own():
    assert BINANCE_USDM_ROOT == "wss://fstream.binance.com"
    assert urlsplit(BINANCE_USDM_ROOT).path == ""
    assert BINANCE_USDM_CATEGORY == {"market": "market", "public": "public"}


@pytest.mark.parametrize("given", ["btcusdt", "BTCUSDT", "BtcUsdt"])
def test_the_symbol_is_lowercased_whatever_case_it_arrives_in(given):
    feed = feed_for(given)

    for name in feed.market_subscriptions + feed.public_subscriptions:
        symbol = name.split("@", 1)[0]
        assert symbol == symbol.lower()


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h"])
def test_the_kline_subscription_carries_the_channels_own_timeframe(timeframe):
    feed = feed_for(timeframe=timeframe)

    assert feed.market_subscriptions[0] == f"btcusdt@kline_{timeframe}"


def test_stream_names_keep_the_casing_the_venue_defines():
    """Only the symbol is lowercased. bookTicker and markPrice are camelCase
    at the venue and a lowercased name is silently never delivered."""
    feed = feed_for()

    assert "btcusdt@bookTicker" in feed.public_subscriptions
    assert "btcusdt@markPrice@1s" in feed.market_subscriptions


def test_the_service_layer_has_exactly_one_binance_websocket_endpoint():
    """A second URL built anywhere else is a second thing to get wrong, and
    it would not be covered by anything above."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "services"
    offenders = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "wss://fstream.binance.com" not in line:
                continue
            if path.name == "price_action_stream.py" and "BINANCE_USDM_ROOT =" in line:
                continue
            if line.lstrip().startswith("#"):
                continue
            offenders.append(f"{path.name}:{number}")
    assert not offenders, f"a websocket URL was built outside the constant: {offenders}"
