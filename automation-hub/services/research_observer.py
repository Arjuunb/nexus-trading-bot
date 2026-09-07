"""Shared-feed PA/SMC observational research runtime."""
from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from bot.data.indicators import atr
from bot.types import Bar
from services.forward_paper_hub import ForwardPaperMarketDataHub, candle_id
from services.mtf_policy import policy_for
from services.native_price_action import NativePriceActionEngine, PriceActionConfig
from services.native_smc import SMCConfig, SMCMarketStructureEngine
from services.research_context import CausalHTFContext, NamedLiquidityBook, session_tag, stable_hash
from services.research_variants import ShadowVariantRunner
from services.shadow_research import ShadowResearchStore


class ResearchObservationRuntime:
    """Fan one immutable closed-candle projection into all shadow variants.

    This service has no account or broker reference. Exceptions are contained
    within this optional observer and surface as research status; they cannot
    kill the market-data stream used by the real-paper engines.
    """

    def __init__(self, market_hub: ForwardPaperMarketDataHub,
                 store: ShadowResearchStore, *, symbol: str = "BTCUSDT",
                 timeframe: str = "5m", research_config: dict | None = None):
        self.market_hub = market_hub
        self.store = store
        self.symbol = symbol.upper().replace("/", "")
        self.timeframe = timeframe
        self.research_config = dict(research_config or {})
        self.primary_htf, self.secondary_htf = policy_for(self.timeframe)
        if (self.primary_htf, self.secondary_htf) != ("1h", "4h"):
            raise ValueError("shadow research currently accepts the native 5m -> 1h/4h policy only")
        self.htf = CausalHTFContext()
        self.liquidity = NamedLiquidityBook(
            swing_left=int(self.research_config.get("swing_left", 3)),
            swing_right=int(self.research_config.get("swing_right", 3)),
            equal_tolerance_atr=self.research_config.get("equal_tolerance_atr"),
        )
        self.pa = NativePriceActionEngine(PriceActionConfig(
            symbol=self.symbol, timeframe=self.timeframe, execution_allowed=False))
        self.smc = SMCMarketStructureEngine(SMCConfig(
            symbol=self.symbol, timeframe=self.timeframe, execution_allowed=False))
        self._native_mtf_context: dict[str, list[Bar]] = {
            self.primary_htf: [], self.secondary_htf: []}
        self.variants = ShadowVariantRunner(store, research_config={
            **self.research_config, "liquidity_config_hash": self.liquidity.config_hash,
        })
        self._lock = threading.RLock()
        self._last_observation: dict = {}
        self._last_error = ""
        self._market_data_fresh = False
        self._next_funding_at: datetime | None = None
        self._next_funding_rate = 0.0
        self._subscriptions = [
            market_hub.subscription(
                f"research:{self.symbol}:{self.timeframe}:decision",
                bar_sink=self._on_closed_bar, quote_sink=self._on_quote,
                event_sink=self._on_event),
            market_hub.subscription(
                f"research:{self.symbol}:{self.timeframe}:{self.primary_htf}",
                bar_sink=lambda bar: self._on_htf(self.primary_htf, bar)),
            market_hub.subscription(
                f"research:{self.symbol}:{self.timeframe}:{self.secondary_htf}",
                bar_sink=lambda bar: self._on_htf(self.secondary_htf, bar)),
        ]

    def start(self) -> bool:
        # Warm native HTF clocks before any decision callback can run.
        htf_started = self._subscriptions[1].start(self.symbol, self.primary_htf)
        htf4_started = self._subscriptions[2].start(self.symbol, self.secondary_htf)
        started = self._subscriptions[0].start(self.symbol, self.timeframe)
        ready = bool(started and htf_started and htf4_started)
        if not ready:
            self.stop()
        return ready

    def stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.stop()

    def _on_event(self, event: dict) -> None:
        state = str(event.get("state") or "")
        if event.get("kind") == "market_data_health":
            self._market_data_fresh = state == "SYNCHRONIZED"
        elif event.get("kind") == "connection" and state != "CONNECTED":
            self._market_data_fresh = False

    def _feed_reliable(self) -> bool:
        """Resolve freshness from the authoritative decision-feed status."""
        try:
            reliable = bool(self._subscriptions[0].status().get("reliable"))
        except Exception:
            reliable = False
        self._market_data_fresh = reliable
        return reliable

    def _on_htf(self, timeframe: str, bar: Bar) -> None:
        try:
            self.htf.ingest(self.symbol, timeframe, bar,
                            candle_id(self.symbol, timeframe, bar))
            rows = self._native_mtf_context[timeframe]
            if not any(existing.timestamp == bar.timestamp for existing in rows):
                rows.append(bar)
                rows.sort(key=lambda item: item.timestamp)
            self.pa.set_native_mtf_context(self._native_mtf_context)
            self.smc.set_native_mtf_context(self._native_mtf_context)
        except Exception as exc:  # research must not interrupt the shared feed
            with self._lock:
                self._last_error = f"HTF observation failed closed: {exc}"

    def _feature_projection(self, bar: Bar, pa_snapshot, smc_snapshot,
                            liquidity: list[dict]) -> dict:
        decision_time = bar.timestamp + timedelta(
            milliseconds={"1m": 60_000, "3m": 180_000, "5m": 300_000,
                          "15m": 900_000, "30m": 1_800_000,
                          "1h": 3_600_000, "4h": 14_400_000,
                          "1d": 86_400_000}[self.timeframe])
        sweep = self.smc.events.get(smc_snapshot.latest_sweep_id or "")
        direction = getattr(sweep, "direction", None)
        smc_proposal = next((self.smc.proposals[ident] for ident in smc_snapshot.proposal_ids
                             if ident in self.smc.proposals), None)
        pa_proposal = next((self.pa.proposals[ident] for ident in pa_snapshot.proposal_ids
                            if ident in self.pa.proposals), None)
        proposal = smc_proposal or pa_proposal
        if direction is None and proposal is not None:
            direction = proposal.direction
        htf = self.htf.at(self.symbol, decision_time)
        preferred_htf = htf.get(self.primary_htf)
        htf_aligned = bool(direction and preferred_htf and (
            (direction == "bullish" and preferred_htf["htf_bias"] == "BULLISH") or
            (direction == "bearish" and preferred_htf["htf_bias"] == "BEARISH")
        ))
        relevant_liquidity = [row for row in liquidity if (
            row["side"] == ("LOW" if direction == "bullish" else "HIGH")
            and row["freshness"] == "FRESH"
        )] if direction else []
        traces = {(row.strategy_id, row.direction): row for row in pa_snapshot.strategy_traces}
        sr = next((row for (strategy, _), row in traces.items()
                   if strategy == "PA1_SR_REJECTION" and row.state == "ORDER_PENDING"), None)
        flip = next((row for (strategy, _), row in traces.items()
                     if strategy == "PA3_FLIP_RETEST" and row.state == "ORDER_PENDING"), None)
        fvg_on_bar = any(gap.created_at == bar.timestamp for gap in self.smc.fvgs.values())
        closed_structure = [
            self.smc.events[ident] for ident in smc_snapshot.event_ids
            if ident in self.smc.events and hasattr(self.smc.events[ident], "event_type")
        ]
        entry = stop = target = None
        if proposal is not None:
            entry, stop, target = proposal.entry, proposal.stop, proposal.target
        elif sweep is not None:
            current_atr = atr(self.smc.bars, self.smc.config.atr_length)
            if current_atr > 0:
                entry = bar.close
                stop = (bar.low - current_atr * self.smc.config.atr_multiplier
                        if direction == "bullish" else
                        bar.high + current_atr * self.smc.config.atr_multiplier)
                risk = abs(entry - stop)
                target = (entry + risk * self.smc.config.rr_ratio
                          if direction == "bullish" else
                          entry - risk * self.smc.config.rr_ratio)
        exit_tags: dict[str, object] = {}
        if entry is not None and stop is not None and direction in {"bullish", "bearish"}:
            risk = abs(float(entry) - float(stop))
            sign = 1 if direction == "bullish" else -1
            beyond = [
                float(row["price"]) for row in liquidity
                if row["side"] == ("HIGH" if direction == "bullish" else "LOW")
                and row.get("invalidated_at") is None
                and ((float(row["price"]) > float(entry)) if sign > 0
                     else (float(row["price"]) < float(entry)))
            ]
            session_levels = [
                float(row["price"]) for row in liquidity
                if row["type"] in {
                    f"{name}_{'HIGH' if sign > 0 else 'LOW'}" for name in (
                        "ASIA", "LONDON", "LONDON_NY_OVERLAP", "NEW_YORK",
                    )
                }
                and row.get("invalidated_at") is None
            ]
            exit_tags = {
                "2R": float(entry) + sign * risk * 2,
                "2.5R": float(entry) + sign * risk * 2.5,
                "opposing_liquidity": (min(beyond, key=lambda value: abs(value - float(entry)))
                                        if beyond else None),
                "session_high_low": (min(session_levels,
                                           key=lambda value: abs(value - float(entry)))
                                     if session_levels else None),
                "1R_runner_break_even": {
                    "activation": float(entry) + sign * risk,
                    "runner_stop": float(entry),
                },
                "execution_class": "SHADOW",
            }
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "candle_open": bar.timestamp.isoformat(),
            "candle_close": decision_time.isoformat(),
            "market_data_source": "Binance USD-M public WebSocket",
            "market_data_fresh": self._market_data_fresh,
            "direction": direction, "sweep": sweep is not None,
            # Native SMC sweep creation already requires a closed reclaim.
            "closed_reclaim": sweep is not None,
            "sweep_id": getattr(sweep, "id", None),
            "sweep_level": getattr(sweep, "level", None),
            "displacement": fvg_on_bar,
            "fresh_liquidity": bool(relevant_liquidity),
            "liquidity": liquidity,
            "relevant_liquidity_types": sorted({
                row["type"] for row in relevant_liquidity
            }),
            "fvg": fvg_on_bar,
            "choch": any(row.event_type == "CHOCH" for row in closed_structure),
            "bos": any(row.event_type == "BOS" for row in closed_structure),
            "session": session_tag(bar.timestamp),
            "htf": htf,
            "mtf_evidence": {
                "entry_timeframe": self.timeframe,
                "primary": htf.get(self.primary_htf),
                "secondary": htf.get(self.secondary_htf),
            },
            "htf_aligned": htf_aligned,
            "full_smc_ready": bool(smc_snapshot.proposal_ids),
            "smc_missing_condition": smc_snapshot.next_required_event,
            "pa_sr_rejection": sr is not None,
            "pa_flip_retest": flip is not None,
            "pa_snapshot_id": pa_snapshot.id,
            "smc_snapshot_id": smc_snapshot.id,
            "entry": entry, "stop_loss": stop, "take_profit": target,
            "research_exit_tags": exit_tags,
            "order_type": "market",
        }

    def _collect_pa_zones(self) -> None:
        """Project native PA zones as measurements without changing PA rules."""
        current_atr = atr(self.pa.bars, 14)
        existing = self.liquidity.snapshot(self.symbol, self.timeframe)
        for zone in self.pa.zones.values():
            midpoint = (float(zone.low) + float(zone.high)) / 2
            opposing = [
                abs(float(row["price"]) - midpoint) for row in existing
                if row["side"] == ("HIGH" if zone.role == "support" else "LOW")
                and row.get("invalidated_at") is None
            ]
            since = [row for row in self.pa.bars if row.timestamp >= zone.confirmed_at]
            departure = None
            if current_atr > 0 and since:
                favourable = (max(row.high for row in since) - midpoint
                              if zone.role == "support" else
                              midpoint - min(row.low for row in since))
                departure = max(0.0, favourable) / current_atr
            source_bar = next(
                (row for row in self.pa.bars if row.timestamp == zone.created_at), None
            )
            if source_bar is None:
                continue
            related_events = [
                row for row in self.pa.events.values() if row.zone_id == zone.id
            ]
            self.liquidity.add_zone(
                self.symbol, self.timeframe, price=midpoint, role=zone.role,
                created_at=zone.created_at,
                source_candle_id=candle_id(self.symbol, self.timeframe, source_bar),
                flipped=zone.flipped,
                atr_width=((float(zone.high) - float(zone.low)) / current_atr
                           if current_atr > 0 else None),
                departure_strength_atr=departure,
                # Displacement is retained only when the existing SMC engine
                # observed an FVG after this zone; it is not a new PA gate.
                displacement=any(
                    gap.created_at >= zone.confirmed_at for gap in self.smc.fvgs.values()
                ),
                structure_break=any(
                    row.event_type == "confirmed_breakout" for row in related_events
                ),
                distance_to_opposing_liquidity=(min(opposing) if opposing else None),
            )

    def _on_closed_bar(self, bar: Bar) -> None:
        try:
            cid = candle_id(self.symbol, self.timeframe, bar)
            self._feed_reliable()
            self.liquidity.ingest(self.symbol, self.timeframe, bar, cid)
            pa_snapshot = self.pa.process_closed_bar(
                bar, market_data_health=("SYNCHRONIZED" if self._market_data_fresh else "STALE_CANDLES"))
            smc_snapshot = self.smc.process_closed_bar(bar)
            self._collect_pa_zones()
            liquidity = self.liquidity.snapshot(self.symbol, self.timeframe)
            features = self._feature_projection(bar, pa_snapshot, smc_snapshot, liquidity)
            lineage = stable_hash(features)
            decisions = self.variants.evaluate(
                candle_id=cid, snapshot_lineage=lineage,
                decision_timestamp=features["candle_close"], features=features)
            with self._lock:
                self._last_observation = {
                    "candle_id": cid, "snapshot_lineage": lineage,
                    "features": features, "decisions": decisions,
                }
                self._last_error = ""
        except Exception as exc:  # optional observer is fail-closed and isolated
            with self._lock:
                self._last_error = f"research observation failed closed: {exc}"

    def _on_quote(self, quote: dict) -> None:
        if not self._feed_reliable():
            return
        try:
            accepted, _quote_id = self.store.accept_quote(self.symbol, quote)
            if not accepted:
                return
            self._attribute_due_funding(quote)
            for order in self.store.open_orders(self.symbol):
                if order["status"] in {"INTENT", "SHADOW_REJECTED_INTENT"}:
                    self.store.record_fill(
                        order["order_id"], quote,
                        slippage_bps=float(self.research_config.get("slippage_bps", 3.0)),
                        commission_bps=float(self.research_config.get("commission_bps", 4.0)),
                    )
                    continue
                excursion = self.store.observe_mae_mfe(order["order_id"], quote)
                side = str(order["side"]).lower()
                long = side in {"buy", "long"}
                stop_hit = (float(quote["bid"]) <= float(order["stop_loss"]) if long else
                            float(quote["ask"]) >= float(order["stop_loss"]))
                target_hit = (float(quote["bid"]) >= float(order["take_profit"]) if long else
                              float(quote["ask"]) <= float(order["take_profit"]))
                if stop_hit or target_hit:
                    self.store.record_outcome(
                        order["order_id"], quote,
                        exit_reason="STOP" if stop_hit else "TARGET",
                        slippage_bps=float(self.research_config.get("slippage_bps", 3.0)),
                        commission_bps=float(self.research_config.get("commission_bps", 4.0)),
                        funding=self.store.funding_total(
                            account_id=order["account_id"],
                            position_id=order["order_id"],
                        ),
                    )
                _ = excursion
        except Exception as exc:
            with self._lock:
                self._last_error = f"research quote observation failed closed: {exc}"

    @staticmethod
    def _timestamp(value: object) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _attribute_due_funding(self, quote: dict) -> None:
        """Attribute one public funding event once its announced time passes."""
        event_at = self._timestamp(quote.get("event_timestamp") or quote.get("received_at"))
        if self._next_funding_at is not None and event_at >= self._next_funding_at:
            mark = float(quote.get("mark") or 0)
            if mark <= 0:
                raise ValueError("funding attribution requires a positive public mark")
            for order in self.store.open_orders(self.symbol):
                if order["status"] != "FILLED":
                    continue
                direction = 1 if str(order["side"]).lower() in {"buy", "long"} else -1
                amount = (mark * float(order["quantity"]) * self._next_funding_rate
                          * direction)
                self.store.record_funding(
                    account_id=order["account_id"], position_id=order["order_id"],
                    funding_timestamp=self._next_funding_at, amount=amount,
                )
            self._next_funding_at = None
        announced = quote.get("next_funding_time")
        if announced:
            announced_at = self._timestamp(announced)
            if announced_at > event_at and (
                    self._next_funding_at is None or announced_at != self._next_funding_at):
                self._next_funding_at = announced_at
                self._next_funding_rate = float(quote.get("funding_rate") or 0)

    def status(self) -> dict:
        with self._lock:
            observation = dict(self._last_observation)
            error = self._last_error
        feed = self._subscriptions[0].status()
        self._market_data_fresh = bool(feed.get("reliable"))
        return {
            "state": "ERROR" if error else ("OBSERVING" if self._market_data_fresh else "BLOCKED"),
            "error": error or None,
            "execution_class": "SHADOW", "real_execution_allowed": False,
            "symbol": self.symbol, "timeframe": self.timeframe,
            "market_data": feed,
            "last_observation": observation,
            "table_counts": self.store.table_counts(),
            "registry": self.variants.registry,
        }
