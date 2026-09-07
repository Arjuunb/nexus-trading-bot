# Troubleshooting

## Engine running but bars = 0

**Symptom:** the dashboard shows *Engine: Running* but the Overview banner says
"No market data has been processed yet" (or, after this fix, a more precise
message) and `bars 0 · signals 0 · trades 0` never moves.

The engine only counts a bar when a **closed candle is processed**. Four causes,
in the order to check them:

### 1. Live feed failed → static fallback (the most common on cloud hosts)

`HUB_USE_LIVE_DATA=1` makes the engine fetch real candles via ccxt (Binance by
default). **Binance's API blocks many cloud/datacenter IPs — including Render's
US regions — with HTTP 451.** Every fetch fails, the data layer silently falls
back to bundled/synthetic *static* history, and since static data never grows a
new candle, `bars` stays 0 **forever**. The tell: mode says `live` but source
says `bundled sample` or `synthetic`.

**Diagnosis (after this fix):**
- The Overview banner shows: *"Running — data feed FAILED (static fallback
  active)"* with the **real fetch error**.
- `GET /engine/diagnostics` → `status: "stale_feed"`, `feed_status: "fallback"`,
  `feed_error: "<the exact ccxt error>"`.
- Render Logs show one line per fetch: `[data] fetch BTCUSDT 4h via binance:
  FAILED: ExchangeNotAvailable: ... 451 ...`.

**Fixes (pick one):**
- Set `HUB_EXCHANGE=kraken` (or `coinbase`) on Render and redeploy — exchanges
  that serve cloud IPs. Watch Render logs for `[data] fetch ... OK n=...`.
- Or unset `HUB_USE_LIVE_DATA` to run **replay mode** — synthetic/bundled bars
  replay continuously, `bars` climbs within seconds (demo, not live market).
- The engine **never fabricates new "live" candles** from static data — that
  would create fake trades on a fake market labelled live.

### 2. Feed connected — the timeframe is just slow

On `4h`, a new candle closes only every 4 hours; the engine warms up on history
without counting it, then waits for the **next close**. So `bars 0` for up to
4 hours after boot is normal on a healthy feed.

- Banner: *"Running — data feed connected, waiting for the first candle"*.
- `GET /engine/diagnostics` → `status: "waiting_first_candle"`,
  `feed_status: "connected"` (or `waiting-for-candle`).
- Choose the intended entry timeframe explicitly. For example, a `5m` entry
  worker waits for 5m closes while consuming native closed `1h` as its HTF gate
  and native closed `4h` as bias. The dashboard must show that full mapping;
  `4h` is not a hidden validated entry timeframe for a `5m` instance.

### 3. The host was asleep (free tiers)

Render's free tier spins the service down after ~15 min idle — a sleeping
engine processes nothing. Add a free uptime ping (UptimeRobot / cron-job.org)
hitting `/health` every 10 minutes. See `docs/FREE_PERSISTENCE.md`.

### 4. Symbols unsupported by the venue

`BTCUSDT / ETHUSDT / SOLUSDT` map to `BTC/USDT` etc. and exist on Binance,
Kraken and Coinbase. For other symbols/venues, a per-symbol fetch error appears
in Render logs (`[data] fetch <SYM> ...: FAILED: BadSymbol ...`) and in
`feed_error`.

### Where to look, in order

1. **Overview banner** — now states the feed status precisely (connected /
   waiting / failed+fallback) instead of a generic warm-up line.
2. **`GET /engine/diagnostics`** — `status`, `feed_status`, `feed_error`,
   `data_source`, `bars`, `last_activity_age_s`.
3. **Render Logs** — `[data] fetch <symbol> <tf> via <exchange>: OK n=… /
   FAILED: <error>` for every attempt, plus one deduplicated engine warning per
   symbol when a fallback engages (and a recovery line when the feed returns).
