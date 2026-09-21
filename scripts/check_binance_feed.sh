#!/usr/bin/env bash
# Read-only check of whether this host can actually reach Binance USD-M.
#
# Every paper fill in this system is gated on one thing: the feed reaching
# SYNCHRONIZED, which needs kline, bookTicker and markPrice all fresher than
# fifteen seconds at the same instant, over two separate WebSocket connections.
# When instances report MarketDataStaleError and the lab status endpoints hang,
# the question underneath all of it is whether the data is arriving at all.
#
# This opens connections and reads one message from each. It places no order,
# changes no configuration and writes to no database.
#
#     ./scripts/check_binance_feed.sh            # BTCUSDT 5m
#     SYMBOL=ETHUSDT TIMEFRAME=5m ./scripts/check_binance_feed.sh
#
# It runs inside the app container, so it tests the network path the running
# service actually uses rather than the host's.

set -uo pipefail

SYMBOL="${SYMBOL:-BTCUSDT}"
TIMEFRAME="${TIMEFRAME:-5m}"

if ! docker compose exec -T app python -c "pass" >/dev/null 2>&1; then
  echo "Cannot exec into the app container. Run this from the deploy directory" >&2
  echo "with the stack up: docker compose ps" >&2
  exit 1
fi

printf '\033[1mBinance USD-M reachability\033[0m  %s %s  (from inside the app container)\n' \
       "$SYMBOL" "$TIMEFRAME"

docker compose exec -T -e SYMBOL="$SYMBOL" -e TIMEFRAME="$TIMEFRAME" app python - <<'PY'
import asyncio, os, ssl, time, urllib.request

symbol = os.environ.get("SYMBOL", "BTCUSDT").lower()
timeframe = os.environ.get("TIMEFRAME", "5m")

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def rest(label, url):
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(url, timeout=10)
        body = response.read(120).decode("utf-8", "replace")
        print("  %s%-34s %s%s  %.0fms  %s" % (
            GREEN, label, response.getcode(), OFF,
            (time.monotonic() - started) * 1000, body[:60]))
        return True
    except Exception as exc:
        print("  %s%-34s FAILED%s  %s: %s" % (
            RED, label, OFF, type(exc).__name__, str(exc)[:90]))
        return False


print("\nREST")
rest("fapi ping", "https://fapi.binance.com/fapi/v1/ping")
rest("fapi serverTime", "https://fapi.binance.com/fapi/v1/time")
rest("klines %s %s" % (symbol.upper(), timeframe),
     "https://fapi.binance.com/fapi/v1/klines?symbol=%s&interval=%s&limit=2"
     % (symbol.upper(), timeframe))


async def probe(label, url):
    """Open the socket and wait for one real message, as the runtime does."""
    started = time.monotonic()
    try:
        import websockets
    except Exception as exc:
        print("  %s%-34s websockets unavailable: %s%s" % (RED, label, exc, OFF))
        return False
    try:
        async with websockets.connect(url, open_timeout=15, close_timeout=5,
                                      ping_interval=20, ping_timeout=20,
                                      ssl=ssl.create_default_context()) as socket:
            message = await asyncio.wait_for(socket.recv(), timeout=20)
            print("  %s%-34s OK%s  %.0fms  %s" % (
                GREEN, label, OFF, (time.monotonic() - started) * 1000,
                str(message)[:70]))
            return True
    except Exception as exc:
        print("  %s%-34s FAILED%s  %s: %s" % (
            RED, label, OFF, type(exc).__name__, str(exc)[:90]))
        return False


async def main():
    # The two URLs this deployment actually opens -- asked of the running
    # build rather than copied from it. A hardcoded pair is a diagnostic that
    # can quietly start testing URLs the application no longer uses, which is
    # the one thing this script must never do.
    from services.price_action_stream import PriceActionPublicStream
    feed = PriceActionPublicStream(lambda *a, **k: [])
    feed.symbol, feed.timeframe = symbol.upper(), timeframe
    market, public = feed.market_url, feed.public_url
    print("  " + DIM + "asked the running build: " + market + OFF)
    # Binance documents the combined path as /stream?streams= and the raw path
    # as /ws/<stream>. If these two answer while the pair above do not, the
    # deployment is opening paths the venue does not serve, and no amount of
    # restarting will produce a candle.
    documented_combined = ("wss://fstream.binance.com/stream?streams="
                           "%s@kline_%s/%s@markPrice@1s" % (symbol, timeframe, symbol))
    documented_raw = "wss://fstream.binance.com/ws/%s@bookTicker" % symbol

    print("\nWebSocket: the URLs this deployment opens")
    used = [await probe("market  (kline + markPrice)", market),
            await probe("public  (bookTicker)", public)]

    print("\nWebSocket: the paths Binance documents")
    documented = [await probe("/stream?streams=  (combined)", documented_combined),
                  await probe("/ws/  (raw)", documented_raw)]

    print("\nVerdict")
    if all(used):
        print("  %sBoth streams this deployment uses are reachable.%s" % (GREEN, OFF))
        print("  " + DIM + "Staleness is then a delivery or processing problem, not a" + OFF)
        print("  " + DIM + "connectivity one. Check the app log next." + OFF)
    elif any(documented) and not any(used):
        print("  %sThe documented paths work and this deployment's paths do not.%s"
              % (RED, OFF))
        print("  " + DIM + "This is the shape of the 2026-08-24 defect, in which the" + OFF)
        print("  " + DIM + "channel names became path segments. Print the two URLs" + OFF)
        print("  " + DIM + "above and compare them against /stream?streams=." + OFF)
        print("  " + DIM + "The URLs in services/price_action_stream.py are being refused" + OFF)
        print("  " + DIM + "by the venue, so no candle can ever arrive. This is a code" + OFF)
        print("  " + DIM + "fix, not an ops one. Send this output back." + OFF)
    elif not any(documented) and not any(used):
        print("  %sNothing reached Binance over WebSocket.%s" % (RED, OFF))
        print("  " + DIM + "If REST above also failed, this host has no route to Binance" + OFF)
        print("  " + DIM + "at all: firewall, DNS, or a blocked region. If REST passed," + OFF)
        print("  " + DIM + "outbound WebSocket specifically is being blocked." + OFF)
    else:
        print("  %sPartial reachability; read the per-stream lines above.%s" % (RED, OFF))


asyncio.run(main())
PY
