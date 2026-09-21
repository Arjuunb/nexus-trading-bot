"""The websocket URLs this application opens against Binance USD-M.

A wrong path here is the quietest failure the platform has. Nothing raises:
the socket is refused, the REST loader has already seeded history, so charts
draw, strategies evaluate and the lab looks alive -- while no live candle,
mark price or book update ever arrives again. It shipped in a commit titled
"fix: restore routed Binance price action streams", which replaced the
documented /stream?streams= path with /market/stream and /public/stream,
turning this application's two CHANNEL NAMES into URL path segments the venue
does not serve.

The venue publishes exactly two market-data paths:

    wss://fstream.binance.com/stream?streams=<s1>/<s2>    combined
    wss://fstream.binance.com/ws/<stream>                 raw

These tests pin the builder to the first and refuse anything else. They are
offline by design -- they assert what we ASK for, which is the half that was
wrong; whether Binance answers is a network fact a unit test cannot own, and
scripts/check_binance_feed.sh exists to establish it from a host that can
reach the venue.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from services.price_action_stream import (BINANCE_USDM_COMBINED,
                                          PriceActionPublicStream)

#: Paths the venue does not serve. Naming them individually rather than
#: asserting a single correct string means a future third invention is still
#: caught by the structural tests below.
REFUSED_PATHS = ("/market/stream", "/public/stream", "/market", "/public")


def stream(symbol: str = "BTCUSDT", timeframe: str = "5m", *, quotes: bool = True):
    feed = PriceActionPublicStream(lambda *args, **kwargs: [], quotes_enabled=quotes)
    feed.symbol, feed.timeframe = symbol, timeframe
    return feed


def urls(feed) -> list[str]:
    return [feed.market_url, feed.public_url, feed.url]


# ───────────────────────────── the exact URLs ─────────────────────────────

def test_btcusdt_5m_builds_the_documented_combined_urls():
    """Requirement: prove the builder itself, not a mock of it."""
    feed = stream()

    assert feed.market_url == (
        "wss://fstream.binance.com/stream?streams="
        "btcusdt@kline_5m/btcusdt@markPrice@1s")
    assert feed.public_url == (
        "wss://fstream.binance.com/stream?streams=btcusdt@bookTicker")
    assert feed.url == feed.market_url


def test_a_kline_only_channel_asks_for_kline_alone():
    """Higher-timeframe context channels carry no quote streams, so the URL
    must not subscribe to markPrice the entry channel already owns."""
    feed = stream(quotes=False)

    assert feed.market_url == (
        "wss://fstream.binance.com/stream?streams=btcusdt@kline_5m")
    assert "markPrice" not in feed.market_url


# ──────────────────────────── the refused paths ───────────────────────────

@pytest.mark.parametrize("bad", REFUSED_PATHS)
def test_no_url_uses_a_path_the_venue_does_not_serve(bad):
    for url in urls(stream()):
        assert bad not in url, f"{bad} is back in {url}"


def test_every_url_uses_the_one_combined_endpoint():
    """Structural, so a fourth invented path fails even if it is not listed
    in REFUSED_PATHS."""
    for url in urls(stream()):
        split = urlsplit(url)
        assert split.scheme == "wss"
        assert split.netloc == "fstream.binance.com"
        assert split.path == "/stream", f"unexpected path {split.path!r}"
        assert url.startswith(BINANCE_USDM_COMBINED + "?streams=")


def test_the_endpoint_constant_carries_no_path_of_its_own():
    assert BINANCE_USDM_COMBINED == "wss://fstream.binance.com/stream"


# ─────────────────────────── the stream names ─────────────────────────────

def test_the_symbol_is_lowercased_whatever_case_it_arrives_in():
    """Binance rejects an upper-case symbol in a stream name, and the session
    stores the symbol upper-case, so the lowering has to happen in the builder.

    Only the SYMBOL lowercases. The stream name after "@" is the venue's own
    spelling and is asserted separately -- lowercasing the whole thing would
    turn bookTicker into a stream that does not exist, which is the same class
    of mistake as the path that caused this file to exist.
    """
    for symbol in ("BTCUSDT", "btcusdt", "BtcUsdt"):
        feed = stream(symbol)
        for url in urls(feed):
            for name in parse_qs(urlsplit(url).query)["streams"][0].split("/"):
                ticker, _, rest = name.partition("@")
                assert ticker == "btcusdt", f"{ticker!r} in {name!r}"
                assert rest, f"{name!r} has no stream name"


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h"])
def test_the_kline_stream_carries_the_channels_own_timeframe(timeframe):
    feed = stream(timeframe=timeframe)
    names = parse_qs(urlsplit(feed.market_url).query)["streams"][0].split("/")

    assert names[0] == f"btcusdt@kline_{timeframe}"


def test_the_market_channel_asks_for_kline_and_mark_price_in_that_order():
    names = parse_qs(urlsplit(stream().market_url).query)["streams"][0].split("/")

    assert names == ["btcusdt@kline_5m", "btcusdt@markPrice@1s"]


def test_the_public_channel_asks_for_book_ticker_alone():
    names = parse_qs(urlsplit(stream().public_url).query)["streams"][0].split("/")

    assert names == ["btcusdt@bookTicker"]


def test_book_ticker_and_mark_price_keep_the_casing_the_venue_defines():
    """The SYMBOL lowercases; the stream NAME does not. bookTicker and
    markPrice are camelCase at the venue and a lowercased spelling is a
    different, non-existent stream."""
    feed = stream()

    assert "@bookTicker" in feed.public_url
    assert "@markPrice@1s" in feed.market_url
    assert "@bookticker" not in feed.public_url
    assert "@markprice" not in feed.market_url


# ──────────────────────── nothing else builds a URL ───────────────────────

def test_the_service_layer_has_exactly_one_binance_websocket_endpoint():
    """The defect survived because a URL was assembled inline. One constant,
    referenced everywhere, is what stops the next edit reintroducing a path.
    """
    from pathlib import Path
    services = Path(__file__).resolve().parents[1] / "services"
    offenders = []
    for path in services.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "wss://fstream.binance.com" not in line:
                continue
            if path.name == "price_action_stream.py" and "BINANCE_USDM_COMBINED =" in line:
                continue
            offenders.append(f"{path.name}:{number}")
    assert not offenders, f"a websocket URL was built outside the constant: {offenders}"
