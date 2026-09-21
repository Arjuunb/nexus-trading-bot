#!/usr/bin/env bash
# Read-only check of whether this host can actually reach Binance USD-M.
#
# Every paper fill in this system is gated on one thing: the feed reaching
# SYNCHRONIZED, which needs kline, bookTicker and markPrice all fresher than
# fifteen seconds at the same instant, over two separate WebSocket connections.
# When instances report MarketDataStaleError and the lab status endpoints hang,
# the question underneath all of it is whether the data is arriving at all.
#
# It opens connections and counts messages. It places no order, changes no
# configuration and writes to no database.
#
#     ./scripts/check_binance_feed.sh            # BTCUSDT 5m
#     SYMBOL=ETHUSDT TIMEFRAME=5m ./scripts/check_binance_feed.sh
#
# It runs inside the app container, so it tests the network path the running
# service actually uses rather than the host's.
#
# Counting matters. An earlier version of this script opened each socket and
# waited for a single message with one timeout covering both the connect and
# the read, so it printed "FAILED TimeoutError" for a socket that had in fact
# connected in 0.7s and stayed open -- a venue that accepts a subscription and
# then sends nothing looked identical to a URL the venue refuses. Those two
# faults live in different layers and are fixed by different people, and the
# conflation cost a full diagnostic round on 2026-09-21. Connect and delivery
# are now timed and reported separately, and every stream is probed alone as
# well as in the pair the runtime uses, because the failure that day was
# specific to which streams were requested and invisible in any single probe.

set -uo pipefail

SYMBOL="${SYMBOL:-BTCUSDT}"
TIMEFRAME="${TIMEFRAME:-5m}"
WINDOW="${WINDOW:-8}"

if ! docker compose exec -T app python -c "pass" >/dev/null 2>&1; then
  echo "Cannot exec into the app container. Run this from the deploy directory" >&2
  echo "with the stack up: docker compose ps" >&2
  exit 1
fi

printf '\033[1mBinance USD-M reachability\033[0m  %s %s  (from inside the app container)\n' \
       "$SYMBOL" "$TIMEFRAME"

docker compose exec -T -e SYMBOL="$SYMBOL" -e TIMEFRAME="$TIMEFRAME" -e WINDOW="$WINDOW" \
  app python - <<'PY'
import asyncio, os, ssl, time, urllib.request

symbol = os.environ.get("SYMBOL", "BTCUSDT").lower()
timeframe = os.environ.get("TIMEFRAME", "5m")
window = float(os.environ.get("WINDOW", "8"))

GREEN, RED, YELLOW, DIM, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")


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

# Three outcomes, deliberately distinct: the socket was refused, the socket
# opened and the venue sent nothing, or data arrived. Only the first is a URL
# or connectivity fault.
REFUSED, SILENT, DELIVERED = "REFUSED", "SILENT", "DELIVERED"


async def probe(label, url):
    started = time.monotonic()
    try:
        import websockets
    except Exception as exc:
        print("  %s%-34s websockets unavailable: %s%s" % (RED, label, exc, OFF))
        return REFUSED
    try:
        socket = await asyncio.wait_for(
            websockets.connect(url, close_timeout=5, ping_interval=20,
                               ping_timeout=20,
                               ssl=ssl.create_default_context()), timeout=15)
    except Exception as exc:
        print("  %s%-34s REFUSED%s  after %.1fs  %s: %s" % (
            RED, label, OFF, time.monotonic() - started,
            type(exc).__name__, str(exc)[:70]))
        return REFUSED
    opened = time.monotonic() - started
    count, first, sample, closed = 0, None, "", ""
    try:
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(socket.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            count += 1
            if first is None:
                first, sample = time.monotonic() - started - opened, str(message)[:46]
    except Exception as exc:
        closed = "  closed: %s: %s" % (type(exc).__name__, str(exc)[:50])
    finally:
        try:
            await socket.close()
        except Exception:
            pass
    if count:
        print("  %s%-34s OK%s  connect %.1fs  %d msgs  first +%.2fs  %s" % (
            GREEN, label, OFF, opened, count, first, sample))
        return DELIVERED
    print("  %s%-34s CONNECTED %.1fs then SILENT %.0fs%s%s" % (
        YELLOW, label, opened, window, OFF, closed))
    return SILENT


async def main():
    # The two URLs this deployment actually opens -- asked of the running
    # build rather than copied from it. A hardcoded pair is a diagnostic that
    # can quietly start testing URLs the application no longer uses, which is
    # the one thing this script must never do.
    from services.price_action_stream import PriceActionPublicStream
    feed = PriceActionPublicStream(lambda *a, **k: [])
    feed.symbol, feed.timeframe = symbol.upper(), timeframe
    market, public = feed.market_url, feed.public_url
    print("\n" + DIM + "  asked the running build:\n    market  " + market
          + "\n    public  " + public + OFF)

    print("\nWebSocket: the URLs this deployment opens  (%.0fs window each)" % window)
    used = [await probe("market  (kline + markPrice)", market),
            await probe("public  (bookTicker)", public)]

    # Each stream alone. The runtime needs kline and markPrice specifically;
    # a venue serving bookTicker while withholding those looks "reachable"
    # from every coarser test and still cannot produce a single candle.
    base = "wss://fstream.binance.com/stream?streams=%s@"
    print("\nWebSocket: each stream on its own connection")
    each = {
        "kline_%s" % timeframe: await probe("kline_%s" % timeframe,
                                            (base % symbol) + "kline_%s" % timeframe),
        "markPrice@1s": await probe("markPrice@1s", (base % symbol) + "markPrice@1s"),
        "bookTicker": await probe("bookTicker", (base % symbol) + "bookTicker"),
        "aggTrade": await probe("aggTrade", (base % symbol) + "aggTrade"),
        "depth5@100ms": await probe("depth5@100ms", (base % symbol) + "depth5@100ms"),
    }

    print("\nVerdict")
    states = list(each.values())
    needed = [each["kline_%s" % timeframe], each["markPrice@1s"]]
    if all(state == DELIVERED for state in used):
        print("  %sBoth streams this deployment uses are delivering data.%s" % (GREEN, OFF))
        print("  " + DIM + "Staleness is then a delivery or processing problem, not a" + OFF)
        print("  " + DIM + "connectivity one. Check the app log next." + OFF)
    elif all(state == REFUSED for state in states):
        print("  %sNothing reached Binance over WebSocket.%s" % (RED, OFF))
        print("  " + DIM + "If REST above also failed, this host has no route to Binance" + OFF)
        print("  " + DIM + "at all: firewall, DNS, or a blocked region. If REST passed," + OFF)
        print("  " + DIM + "outbound WebSocket specifically is being blocked." + OFF)
    elif REFUSED in states:
        print("  %sSome sockets were refused outright.%s" % (RED, OFF))
        print("  " + DIM + "A refused socket is a URL or connectivity fault. Compare the" + OFF)
        print("  " + DIM + "URLs printed above against /stream?streams= and /ws/." + OFF)
    elif SILENT in needed and DELIVERED in states:
        print("  %sThe venue accepts every subscription and serves only some of them.%s"
              % (RED, OFF))
        print("  " + DIM + "kline and markPrice are what the runtime needs; a connection" + OFF)
        print("  " + DIM + "carrying bookTicker or depth proves the socket, the route and" + OFF)
        print("  " + DIM + "the URL are all fine. This is not a code fault and no restart" + OFF)
        print("  " + DIM + "will clear it. Re-run the same probe from a different network" + OFF)
        print("  " + DIM + "to tell a venue-side incident from something specific to this" + OFF)
        print("  " + DIM + "host's egress IP. Entries stay blocked either way." + OFF)
    elif all(state == SILENT for state in states):
        print("  %sEvery socket opened and no stream delivered anything.%s" % (RED, OFF))
        print("  " + DIM + "The route is fine and the venue is sending nothing at all." + OFF)
    else:
        print("  %sPartial delivery; read the per-stream lines above.%s" % (RED, OFF))


asyncio.run(main())
PY
