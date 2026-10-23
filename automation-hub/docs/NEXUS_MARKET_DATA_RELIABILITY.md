# NEXUS market-data reliability — Sprint 13

## Inspected architecture

`PriceActionPublicStream` uses Binance USDⓈ-M Futures public routed endpoints:
`wss://fstream.binance.com/market/stream` for kline/mark-price and
`wss://fstream.binance.com/public/stream` for book ticker. The existing
`MarketDataService.public_usdm_window` calls the USD-M REST provider and never
substitutes spot. Native `Bar.timestamp` is a UTC **open**; close is open plus
the inspected timeframe duration. The existing stream bootstraps bounded REST
history, removes forming candles, sorts native bars, deduplicates by timestamp,
tracks gaps, repairs from REST and exposes separate candle/quote/HTF freshness.
Provider availability time is unknown; local receipt time is not exchange time.

`ForwardPaperMarketDataHub` already fans out one channel per symbol/timeframe
within a process. Financial delivery has its own idempotency/retry semantics.
Observation notices preserve LIVE_WEBSOCKET versus REST_REPAIR origins and must
do no I/O in the stream callback. An independently started shadow worker previously
constructed another process-local hub; there was no shared durable candle cursor.
Historical research reads immutable retained inputs and is a separate category.

## Opt-in finalized-candle transport

`services.market_data_distribution` adds a **public market-data namespace**, not
a ledger, provider, strategy engine or research queue. `JournalFeedPublisher`
attaches a read-only notice subscriber to the existing hub; callbacks only enqueue
to a bounded 256-item queue. Draining happens outside the existing delivery path.
The delivery-equivalence test verifies that an original consumer still receives
the same bar and no extra financial signal/engine is invoked.

`CandleJournal` records only explicitly finalized, valid native OHLCV. It checks
UTC awareness, timeframe alignment, positive finite prices, volume, high/low,
closed-before-receipt, receipt clock drift, source/venue/market/symbol/timeframe
and approved origin. It reuses existing candle identity and strategy SHA-256
canonicalization utilities. Venue is `binance_usdm`, market is `perpetual`.
The revision identifies the reviewed transport transformation; each candle has
its own canonical content hash. It is not a fabricated exchange dataset revision.

SQLite identity, append-only candle rows and transport-health rows use a fresh
purpose-specific database. Update/delete triggers protect normal candle writes.
At-least-once delivery has one logical row per canonical candle identity.
Duplicates return the **original persisted** timestamp/origin; they cannot turn
REST history into a live receipt. Conflicting content sets a persistent blocked
fault and never overwrites the original. This is not exactly-once physical delivery.

Independent consumers mount the journal read-only and keep their own separate
cursor database. Cursors are scoped by revision/symbol/timeframe and bound to a
durable journal authority, so another file with the same revision is not silently
accepted. Reads verify hashes, canonical identities and chronological/finality
boundaries. Late REST repair can fill historical context; gaps, stale candles,
stale transport heartbeats, conflicts, corrupt restoration, unavailable journal/
cursor and wrong revisions fail closed. Required HTF has its own native clock;
future rows cannot affect earlier snapshots. There is no resampling shortcut.

`shadow_worker_main` accepts all three opt-in `--feed-journal`, `--feed-revision`,
`--feed-cursors` bindings together. It then uses the journal adapter and does not
construct an exchange provider. Without this option, its existing behavior remains
unchanged. The remote profile is disabled by default and mounts this transport
read-only; no registrations/start actions are manufactured. Cached/replayed
history is warm-up/context only. Existing attached/resume boundaries and immutable
shadow decision gates continue to exclude offline periods from forward evidence.

## Limits, evidence and known limitations

Maximum 50,000 candles, 256 MiB journal, 1,500 bars per channel read and 256 notices
per drain. Storage/backpressure exhaustion is blocked and recorded; there is no
automatic raw deletion or silent drop-to-green. Clock checks use UTC event/receipt
times and local processing time separately. Transport health is a sampled recent
report, not continuous uptime. A shared journal remains a filesystem/process
boundary, not a process sandbox or cross-host replication protocol.

Deterministic tests exercise finality, invalid OHLCV, venue mismatches, alignment,
out-of-order REST repair, missing/duplicate/conflicting bars, immutable timestamps,
native HTF staleness, future-data invariance, two independent reader processes,
restart cursors, authority/revision conflicts, stale/disconnected feeds, read-only
storage, bounded capacity/backpressure and original hub delivery preservation.
The broader existing market-data suites continue to cover REST pagination,
historical slicing, reconnect, decision provenance and strategy lookbacks.

Upstream timestamp deduplication can suppress a revised/duplicate raw event before
the notice subscriber sees it. Existing source duplicate counters remain distinct
from journal duplicates; raw suppressed conflicts are not claimed detectable by
the journal. Altering active source deduplication/fill eligibility is outside
this sprint's authorized execution semantics. A provider-level audit/authorized
raw observation is still required. Unknown required lookback remains blocked.

## Bounded public-feed soak — NOT RUN

`ops/public-feed-distribution-soak.py` defaults to a NOT_RUN plan and creates no
output or connection without `--authorize-public-feed`. That flag is an operator
control, not authorization supplied by this document. The user must explicitly
authorize the network test before invoking it. Allowed symbols are XRPUSDT,
BTCUSDT, ETHUSDT; sampling is 1–300 seconds; four native timeframes are bounded.
Existing public-only clients are reused, execution credentials are rejected,
bootstrap is REST_BOOTSTRAP and repairs remain REST_REPAIR. No strategy evaluation,
orders, fills, account writes or market observations are fabricated.

The harness retains sampled transport uptime/reconnect/gap/duplicate/freshness
fields, receipt/commit times, canonical hashes, venue/revision and consistency of
two reader snapshots. Those readers share one process and are explicitly labelled;
separately supervised consumers require an independently authorized operational
soak. Source exchange-availability latency is UNKNOWN. Startup/network requests
use existing provider timeouts; the sampling deadline is cooperative and not a
hard OS runtime limit. Apply reviewed external resource limits for a real soak.

No public endpoint was contacted in Sprint 13. Fixture candles are labelled
DETERMINISTIC_FIXTURE and production readers reject them as forward origin.
No statement of live uptime, forward decision coverage, signal quality or
profitability follows from these tests. XRP profitability remains UNVERIFIED.

**MARKET DATA: PARTIAL. CROSS-PROCESS DISTRIBUTION: PARTIAL.** Local transport
integrity is tested; actual public-feed and cross-process operational reliability,
authorized producer placement, filesystem durability, storage/retention monitoring
and remote failure recovery remain unverified. Never silently backfill missed
live opportunities or turn shadow observations into trades.
