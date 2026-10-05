# Tradexa Trading Bot — Architecture

How a market candle becomes (or deliberately does not become) a paper trade.
Live trading is **hard-locked by design**; everything below runs in paper mode.

```
   market data                      DECISION LAYER                        EXECUTION
┌───────────────┐   signal   ┌──────────────────────────┐  accepted  ┌──────────────────┐
│ AutoEngine    │ ─────────► │ Decision Brain gate      │ ─────────► │ Risk pipeline    │
│ (closed bars, │            │  TradeBrain 0–100 score  │            │  controls/dedup/ │
│  live or      │            │  + hard blocks           │            │  daily loss/     │
│  replay feed) │            │  → decision object       │  rejected  │  cooldown/expos. │
└───────────────┘            │  (persisted EVERY time)  │ ────► ∅    │  → paper fill    │
                             └──────────────────────────┘  (stored)  └──────────────────┘
                                        │                                     │
                                        ▼                                     ▼
                              decisions.db (durable)                journal / ledger /
                              /decisions/* endpoints                skipped log / account
```

## The decision flow (what changed and why)

Every entry signal produced by the strategy is scored by the **TradeBrain**
(the same scorer every backtest and simulation uses — parity by construction)
and turned into a **unified decision object** *before* anything else happens:

| Field | Source (all real, never fabricated) |
|---|---|
| `symbol`, `timeframe`, `strategy`, `side` | the signal + engine config |
| `regime` | TradeBrain market-regime classification |
| `htf_bias` | higher-timeframe trend (aggregated from base bars, no lookahead) |
| `setup_quality_score` | TradeBrain composite 0–100 |
| `volume_score`, `rr_score` | TradeBrain components (`volume`, `rr_quality`) |
| `confidence` | the strategy's own 0–1 confidence |
| `passed_rules`, `failed_rules` | per-component checks + hard blocks |
| `decision` | `accepted` / `rejected` |
| `reason` | plain-English explanation (exact blocks / failed rules / score) |
| `executed` | set true only when the trade actually opens |

**Rules:**
1. The decision is **persisted first** (accepted AND rejected) to
   `decisions.db` (`HUB_DECISIONS_DB`, under `HUB_DATA_DIR`).
2. **No paper/live trade is placed unless the decision is accepted**
   (`no hard blocks AND score ≥ HUB_MIN_SCORE`, default 60 — the same gate the
   engine always enforced, now first-class and auditable).
3. An accepted decision still has to clear the **risk pipeline** (emergency
   controls, dedup, **max daily loss**, **cooldown after loss**, weekly loss,
   session, exposure, correlation, drawdown breaker). Pipeline rejections land
   in the skipped-trade log with the failed gate + market snapshot.
4. If the quality gate stands down (warm-up / disabled), the decision says so
   honestly (`"not evaluated"`) — scores are never invented.

## Storage map (all under `HUB_DATA_DIR`, or Supabase for the ledger)

| Store | Contents |
|---|---|
| `decisions.db` | every accept/reject decision object |
| `ledger.db` / Supabase | positions, paper trades, logs, alerts (source of truth) |
| `journal.db` | canonical trade journal (`journal_*` tables: one structured, immutable record per executed trade with fills, fees, modifications, frozen decision snapshot, timeline, reviews and corrections) plus the legacy decision journal; see [TRADE_JOURNAL_ARCHITECTURE.md](TRADE_JOURNAL_ARCHITECTURE.md) |
| `skipped.db` | pipeline rejections with failed gate + snapshot |
| `account.db` | initial capital + current equity snapshot |
| `safety_state.json` | emergency-stop verification record |

## Dashboard API (decision layer)

| Endpoint | Returns |
|---|---|
| `GET /decisions/latest` | newest decisions (accepted + rejected) |
| `GET /decisions/rejected` | rejected signals only — why the bot said no |
| `GET /decisions/state` | composite: bot state, risk status, active positions, latest decisions |
| `GET /engine/diagnostics` | feed status + plain-English "why isn't it trading" |
| `GET /safety/live-readiness` | the enforced live-trading gate (locked by default) |
| `GET /journal/v2/*` | canonical trade journal: filtered trades, trade detail, server-side analytics, weekly review |

## Safety invariants (unchanged, enforced in code + tests)

- Live trading **locked by default**; `live_allowed` is only ever true if every
  Safety Center requirement passes AND the build's hard lock is off (it is on).
- Paper mode is the default everywhere.
- Risk is never increased automatically; the Risk Manager / Safety Center are
  never bypassed; strategy retunes are sample-size gated (30/50) and shadow-first.
- No fake data anywhere: missing feeds, stood-down gates and empty samples say
  so explicitly instead of inventing numbers.
