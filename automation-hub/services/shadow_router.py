"""Frozen, read-only strategy evaluation. No broker, account or execution client.

The only outputs are decision evidence in the existing isolated shadow store.
Public-feed transport is injected separately by ShadowObservationWorker.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import math
import json
import time
import uuid

from bot.data.resample import TF_SECONDS
from services.market_context_classifier import classify_market_context
from services.strategy_identity import canonical_configuration_json, configuration_fingerprint as digest, observed_strategy_identity


def utc(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None: raise ValueError("UTC_TIMESTAMP_REQUIRED")
    return result.astimezone(timezone.utc)


def candle_material(bar):
    return {"open_timestamp": utc(bar.timestamp).isoformat(), "ohlcv": [x if isinstance(x, (int, float)) and math.isfinite(x) else "INVALID" for x in (bar.open, bar.high, bar.low, bar.close, bar.volume)]}


def causal(context, cutoff):
    return {tf: [b for b in rows if utc(b.timestamp) + timedelta(seconds=TF_SECONDS[tf]) <= cutoff]
            for tf, rows in context.items() if tf in TF_SECONDS}


class FrozenSignalAdapter:
    """Allowlisted existing evaluators; reconstruct isolated state, never execute.

    Full effective snapshots are required: absent historical settings are NOT
    replaced with current defaults. Round-trip identity verifies reconstruction.
    """
    supported = ("adaptive_trend_pullback", "price_action_rejection", "price_action_flip_retest")

    def __init__(self, candidate, *, require_source=True):
        self.candidate = deepcopy(candidate)
        config = candidate.get("frozen_config") or {}
        if config.get("schema") != "effective-strategy-configuration-v1":
            raise ValueError("FULL_EFFECTIVE_CONFIGURATION_REQUIRED")
        if digest(config) != candidate.get("config_fingerprint"): raise ValueError("CONFIGURATION_IDENTITY_MISMATCH")
        self.strategy = self._construct()
        identity = observed_strategy_identity(self.strategy, strategy_id=candidate["strategy_id"], timeframe=candidate["timeframe"])
        if identity["strategy_config_hash"] != candidate["config_fingerprint"]: raise ValueError("CONFIGURATION_RECONSTRUCTION_MISMATCH")
        if identity["identity_status"] != "observed" or identity["strategy_version"] != candidate.get("strategy_version"):
            raise ValueError("STRATEGY_VERSION_MISMATCH")
        self.observed_source_hash = identity["source_hash"]
        if require_source and not candidate.get("source_hash"): raise ValueError("SOURCE_IDENTITY_UNVERIFIED")
        if candidate.get("source_hash") and identity["source_hash"] != candidate["source_hash"]: raise ValueError("STRATEGY_SOURCE_CHANGED")
        self.required = tuple(self.strategy.required_timeframes)
        self.optional = ("4h",)
        cfg = self.strategy.config
        if candidate["strategy_id"] == "adaptive_trend_pullback":
            required = {"5m": max(150, cfg.minimum_bars["5m"], cfg.atr_period + 21, cfg.confirmation_lookback + 2),
                        "15m": max(cfg.minimum_bars["15m"], cfg.slow_ema + 3, cfg.pullback_lookback + 4),
                        "1h": max(cfg.minimum_bars["1h"], cfg.slow_ema + 3, 2 * cfg.adx_period + 1, cfg.atr_period + 21, cfg.structure_lookback + 4)}
        else:
            # Native PA retains structure/zone state across its reviewed 400-bar
            # bootstrap. HTF classifier consumes 51 closed native bars.
            required = {"5m": max(400, cfg.swing_left + cfg.swing_right + 5, cfg.zone_expiry_bars + cfg.swing_right + 2, cfg.event_memory_bars + 2, cfg.setup_expiry_bars + 2), "1h": 51}
            if cfg.zone_timeframe_scope == "higher_timeframe": required["1h"] = required["5m"]
        self.lookback = {"version": "shadow-lookback.v1", "strategy_id": candidate["strategy_id"],
                         "config_fingerprint": candidate["config_fingerprint"], "required_candles": required,
                         "optional_timeframes": list(self.optional), "basis": "REVIEWED_EXISTING_STRATEGY_DEPENDENCIES"}
        if max(required.values()) > 1500: raise ValueError("LOOKBACK_EXCEEDS_SHARED_FEED_CAPACITY")

    def _construct(self):
        candidate = self.candidate; frozen = candidate["frozen_config"]
        kind = candidate["strategy_id"]
        if kind not in self.supported: raise ValueError("READ_ONLY_ADAPTER_UNVERIFIED")
        if candidate["timeframe"] != "5m": raise ValueError("TIMEFRAME_UNSUPPORTED")
        if kind == "adaptive_trend_pullback":
            from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
            from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig
            cls, config_cls = AdaptiveTrendPullbackStrategy, AdaptiveTrendPullbackConfig
        else:
            from strategies.price_action_rejection import PriceActionRejectionStrategy, PriceActionFlipRetestStrategy
            from services.native_price_action import PriceActionConfig
            cls = PriceActionRejectionStrategy if kind == "price_action_rejection" else PriceActionFlipRetestStrategy
            config_cls = PriceActionConfig
        values = frozen.get("config") or {}
        if set(values) != {f.name for f in fields(config_cls) if f.name != "symbol"}:
            raise ValueError("FULL_EFFECTIVE_CONFIGURATION_REQUIRED")
        if kind == "adaptive_trend_pullback":
            if frozen.get("settings", {}).get("allow_legacy_mtf_resample"):
                raise ValueError("NATIVE_HTF_REQUIRED")
            return cls(candidate["symbol"], config=config_cls(**values))
        return cls(candidate["symbol"], config=config_cls(symbol=candidate["symbol"], **values), **frozen["params"])

    def evaluate(self, context, cutoff):
        strategy = self.strategy
        context = causal(deepcopy(context), cutoff)
        entries = context["5m"]
        if self.candidate["strategy_id"].startswith("price_action_"):
            from services.native_price_action import NativePriceActionEngine
            strategy._engine = NativePriceActionEngine(strategy.config)
            # Existing PA catch-up would apply current HTF to earlier bootstrap
            # bars. Prime the SAME engine causally, suppressing bootstrap votes.
            for bar in entries[:-1]:
                prior = causal(context, utc(bar.timestamp) + timedelta(seconds=300))
                strategy._engine.set_native_mtf_context({tf: prior.get(tf, []) for tf in ("1h", "4h")})
                strategy._engine.process_closed_bar(bar, market_data_health="LIVE_BOOTSTRAP_RECONCILED")
            strategy._history_proposals |= set(strategy._engine.proposals)
        strategy.set_timeframe_context(context)
        strategy.set_native_mtf_context(context, {})
        signal = strategy.on_bar(entries[-1])
        report = strategy.decision_report() if hasattr(strategy, "decision_report") else {"reason": strategy.last_reason}
        report = json.loads(canonical_configuration_json(report))
        return {"status": "SIGNAL_" + signal.type.value.upper() if signal else "NO_SIGNAL",
                "signal_direction": signal.type.value.upper() if signal else None,
                "signal_strength": getattr(signal, "confidence", None) if signal and self.candidate["strategy_id"] == "adaptive_trend_pullback" else None,
                "strategy_decision": report, "entry_conditions": deepcopy(getattr(signal, "checklist", None)) if signal else None,
                "rejection_reasons": [] if signal else [report.get("reason", "NO_SIGNAL")], "missing_conditions": []}


class ShadowRouter:
    """Authorized observation lifecycle and immutable decision generation.

    Owner identity is server-resolved. This service has no trading references.
    Live inputs are only supplied internally, never through an HTTP candle route.
    """
    policy = {"version": "shadow-observation.v1", "allowed_strategies": list(FrozenSignalAdapter.supported),
              "venue": "binance_usdm", "market_type": "perpetual", "timeframes": ["5m"],
              "max_per_owner": 5, "max_global": 20, "max_registrations_per_owner": 100, "max_total_registrations": 1000,
              "max_history": 1500, "max_observations": 100000,
              "max_candle_delay_seconds": 30, "max_feed_age_seconds": 15, "max_latency_seconds": 10,
              "max_duration_days": 30, "capabilities": ["READ_FINALIZED_MARKET_DATA", "EVALUATE_FROZEN_STRATEGY", "WRITE_SHADOW_DECISION_EVIDENCE", "READ_SHADOW_ANALYTICS", "GENERATE_ADVISORY_REPORT"]}

    def __init__(self, store, *, candidate_reader, clock=None, active_reader=None):
        self.store = store; self.candidate_reader = candidate_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc)); self.active_reader = active_reader
        self.worker = None

    def requirements(self, candidate_id, *, owner_id):
        candidate = self.candidate_reader(candidate_id, owner_id)
        if not candidate: raise KeyError(candidate_id)
        try:
            adapter = FrozenSignalAdapter(candidate, require_source=False)
            return {"valid": True, "lookback_spec": adapter.lookback, "reviewed_source_hash": adapter.observed_source_hash,
                    "source_identity_basis": "OBSERVED_NOW_NOT_HISTORICAL_ATTESTATION", "errors": []}
        except ValueError as exc: return {"valid": False, "lookback_spec": None, "errors": [str(exc)]}
        except (TypeError, KeyError): return {"valid": False, "lookback_spec": None, "errors": ["READ_ONLY_CONFIGURATION_OR_LOOKBACK_UNVERIFIED"]}

    def register(self, request, *, owner_id):
        if set(request) - {"candidate_id", "idempotency_key", "authorize", "authorization_reason", "lookback_spec", "reviewed_source_hash"}:
            raise ValueError("UNKNOWN_SHADOW_REGISTRATION_FIELDS")
        request = {**request, "reviewed_source_hash": request.get("reviewed_source_hash")}
        candidate = self.candidate_reader(request["candidate_id"], owner_id)
        if not candidate: raise KeyError(request["candidate_id"])
        frozen = {**deepcopy(candidate), "registration_id": "shadow:" + uuid.uuid4().hex,
                  "owner_id": owner_id, "tenant_id": owner_id, "registered_at": self.clock().isoformat(),
                  "authorization": {"authorized": request.get("authorize") is True, "actor": owner_id,
                                    "reason": request.get("authorization_reason", ""), "timestamp": self.clock().isoformat()},
                  "lookback_spec": deepcopy(request.get("lookback_spec", {})), "observation_policy_version": self.policy["version"]}
        if not frozen.get("source_hash"):
            frozen["source_hash"] = request.get("reviewed_source_hash")
            frozen["source_identity_basis"] = "HUMAN_REVIEWED_CURRENT_ADAPTER_NOT_HISTORICAL_ATTESTATION"
        # Strict canonicalization also rejects embedded secrets/unknown objects.
        return self.store.create(frozen, request, max_per_owner=self.policy["max_per_owner"], max_global=self.policy["max_global"], max_owner_history=self.policy["max_registrations_per_owner"], max_history=self.policy["max_total_registrations"])

    def validate(self, reg):
        errors = []
        if not reg["authorization"]["authorized"] or not reg["authorization"]["reason"].strip(): errors.append("AUTHORIZATION_REQUIRED")
        if reg.get("venue") != self.policy["venue"] or reg.get("market_type") != self.policy["market_type"]: errors.append("SOURCE_MISMATCH")
        if not isinstance(reg.get("symbol"), str) or not reg["symbol"].isalnum() or len(reg["symbol"]) > 30: errors.append("INVALID_SYMBOL")
        try:
            adapter = FrozenSignalAdapter(reg)
            if reg["lookback_spec"] != adapter.lookback: errors.append("LOOKBACK_UNVERIFIED")
        except ValueError as exc: errors.append(str(exc))
        except (TypeError, KeyError): errors.append("INVALID_FROZEN_CONFIGURATION")
        return {"valid": not errors, "errors": errors}

    def action(self, rid, action, *, owner_id, reason):
        if action not in {"VALIDATE", "START", "PAUSE", "RESUME", "STOP", "BLOCK"}: raise PermissionError("FORBIDDEN_SHADOW_CAPABILITY")
        reg = self.store.get(rid, owner_id=owner_id)
        if not reg: raise KeyError(rid)
        if not reason.strip(): raise ValueError("AUDIT_REASON_REQUIRED")
        valid = self.validate(reg)
        if action in {"START", "RESUME"} and not valid["valid"]: raise ValueError("SHADOW_VALIDATION_REQUIRED")
        result = self.store.transition(rid, owner_id=owner_id, action=action, now=self.clock(), reason=reason, valid=valid)
        if action in {"START", "RESUME"} and self.worker: self.worker.start()
        if self.worker: self.worker.wake()
        return result

    def _quality(self, reg, frame, cutoff, now):
        errors = []
        if cutoff.timestamp() % TF_SECONDS[reg["timeframe"]]: errors.append("INVALID_DECISION_TIME")
        if any(frame.get(key) != reg.get(key) for key in ("venue", "market_type", "symbol", "timeframe")) or frame.get("source") != "BINANCE_USDM_PUBLIC": errors.append("SOURCE_MISMATCH")
        if frame.get("finalized") is not True: errors.append("INVALID_CANDLE")
        if frame.get("connected") is not True: errors.append("FEED_DISCONNECTED")
        try:
            if utc(frame["available_at"]) > now: errors.append("DATA_NOT_AVAILABLE")
            age = (now - utc(frame["stream_observed_at"])).total_seconds()
            if age < 0 or age > self.policy["max_feed_age_seconds"]: errors.append("STALE_CANDLES")
        except (KeyError, ValueError, TypeError): errors.append("STALE_CANDLES")
        if (now - cutoff).total_seconds() < 0 or (now - cutoff).total_seconds() > self.policy["max_candle_delay_seconds"]: errors.append("STALE_CANDLES")
        context = causal(frame.get("context", {}), cutoff)
        normalized = {}
        optional_invalid = set()
        for tf, rows in context.items():
            before = len(errors)
            selected = {}; previous = None
            for bar in rows[-self.policy["max_history"]:]:
                at = utc(bar.timestamp)
                values = [bar.open, bar.high, bar.low, bar.close, bar.volume]
                if not all(math.isfinite(x) for x in values) or min(values[:4]) <= 0 or bar.volume < 0 or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.high < bar.low or at.timestamp() % TF_SECONDS[tf]: errors.append("INVALID_CANDLE")
                if previous and at < previous: errors.append("INVALID_CANDLE_ORDER")
                previous = at
                if at in selected and selected[at] != bar: errors.append("CONFLICTING_CANDLE")
                selected[at] = bar
            normalized[tf] = list(selected.values())
            if tf in reg["lookback_spec"]["optional_timeframes"] and len(errors) > before:
                del errors[before:]; optional_invalid.add(tf)
        required = reg["lookback_spec"]["required_candles"]
        for tf, count in required.items():
            rows = normalized.get(tf, [])
            if not rows: errors.append("MISSING_CANDLE" if tf == reg["timeframe"] else "MISSING_CONTEXT"); continue
            if len(rows) < count: errors.append("INSUFFICIENT_HISTORY")
            if any((utc(b.timestamp)-utc(a.timestamp)).total_seconds() != TF_SECONDS[tf] for a, b in zip(rows, rows[1:])): errors.append("DATA_GAP")
            expected = int(cutoff.timestamp() // TF_SECONDS[tf]) * TF_SECONDS[tf]
            if utc(rows[-1].timestamp).timestamp() + TF_SECONDS[tf] != expected: errors.append("MISSING_CANDLE" if tf == reg["timeframe"] else "STALE_HTF_CANDLE")
            if tf != reg["timeframe"] and frame.get("htf_health", {}).get(tf) is not True: errors.append("STALE_HTF_CANDLE")
        # Optional bias never becomes an entry gate. Invalid/stale optional data
        # is excluded and explicitly reported, rather than passed as fresh.
        warnings = []
        for tf in reg["lookback_spec"]["optional_timeframes"]:
            rows = normalized.get(tf, [])
            expected = int(cutoff.timestamp() // TF_SECONDS[tf]) * TF_SECONDS[tf]
            if tf in optional_invalid or not rows or frame.get("htf_health", {}).get(tf) is not True or utc(rows[-1].timestamp).timestamp() + TF_SECONDS[tf] != expected:
                normalized[tf] = []; warnings.append("OPTIONAL_CONTEXT_UNAVAILABLE:" + tf)
        return normalized, sorted(set(errors)), warnings

    def evaluate(self, rid, frame, *, owner_id, lease_token=None):
        reg = self.store.get(rid, owner_id=owner_id)
        if not reg: raise KeyError(rid)
        if not reg["desired_observing"]: return None
        now = self.clock(); cutoff = utc(frame["decision_time"])
        if cutoff < utc(reg["resume_after"]): return None  # never label replay forward
        if reg["observation_start"] and now - utc(reg["observation_start"]) > timedelta(days=self.policy["max_duration_days"]):
            self.action(rid, "STOP", owner_id=owner_id, reason="Observation duration policy limit"); return None
        valid = self.validate(reg)
        start = time.monotonic()
        if valid["valid"]:
            try: context, errors, warnings = self._quality(reg, frame, cutoff, now)
            except (ValueError, TypeError, AttributeError, KeyError): context, errors, warnings = {}, ["INVALID_CANDLE"], []
        else:
            context, errors, warnings = causal(frame.get("context", {}), cutoff), valid["errors"], []
        entries = context.get(reg["timeframe"], [])
        candle_hash = digest(candle_material(entries[-1]) if entries else {"missing": cutoff.isoformat()})
        context_hash = digest({tf: [candle_material(b) for b in rows] for tf, rows in context.items()})
        # Transport freshness is mutable; it must not turn a duplicate candle
        # into a fictional data revision. Preserve input and used-context IDs.
        try:
            inputs = causal(frame.get("context", {}), cutoff)
            input_hash = digest({tf: [candle_material(b) for b in rows[-self.policy["max_history"]:]] for tf, rows in inputs.items()})
        except (ValueError, TypeError, AttributeError, KeyError): input_hash = digest({"invalid_input": True})
        existing = self.store.decision_at(rid, cutoff.isoformat(), owner_id=owner_id)
        if existing:
            if existing["input_context_hash"] != input_hash:
                self.store.event(rid, owner_id=owner_id, action="CANDLE_REVISION_CONFLICT", payload={"original_decision_id": existing["decision_id"], "received_candle_hash": candle_hash, "received_context_hash": context_hash})
                raise ValueError("CANDLE_REVISION_CONFLICT")
            self.store.event(rid, owner_id=owner_id, action="REPLAY_IGNORED", payload={"original_decision_id": existing["decision_id"], "input_context_hash": input_hash})
            return existing
        decision = {"registration_id": rid, "owner_id": owner_id, "tenant_id": reg["tenant_id"],
                    **{key: reg.get(key) for key in ("strategy_id", "strategy_version", "config_fingerprint", "candidate_id", "venue", "market_type", "symbol", "timeframe", "source_hash")},
                    "execution_mode": "shadow_observation", "evidence_kind": "FORWARD_SHADOW_DECISION",
                    "candle_close_timestamp": cutoff.isoformat(), "decision_timestamp": now.isoformat(), "observed_at": now.isoformat(),
                    "data_available_at": frame.get("available_at"), "last_feed_update": frame.get("stream_observed_at"),
                    "source": frame.get("source"), "candle_hash": candle_hash, "context_hash": context_hash, "input_context_hash": input_hash,
                    "canonical_candle": candle_material(entries[-1]) if entries else None,
                    "finalized": frame.get("finalized") is True and bool(entries),
                    "htf_alignment": "VERIFIED" if not errors else "UNKNOWN",
                    "lookback_status": "VERIFIED" if not errors else "UNKNOWN",
                    "observation_authorized": reg.get("authorization", {}).get("authorized") is True,
                    "evaluation_started_at": now.isoformat(), "data_receipt_timestamp": None,
                    "snapshot_read_at": frame.get("available_at"), "exchange_to_system_latency_ms": None,
                    "availability_assumption": "NATIVE_CLOSED_CANDLES_AT_OR_BEFORE_DECISION; INDIVIDUAL_PUBLICATION_UNKNOWN",
                    "policy_version": self.policy["version"], "lookback_spec": reg["lookback_spec"],
                    "data_freshness": "BLOCKED" if errors else "FRESH", "quality_warnings": warnings,
                    "rejection_reasons": errors, "missing_conditions": errors, "signal_direction": None, "signal_strength": None,
                    "strategy_decision": None, "entry_conditions": None, "market_context": None}
        if errors:
            status = "BLOCKED_WARMUP" if "INSUFFICIENT_HISTORY" in errors else "BLOCKED_STALE_DATA" if any(e in errors for e in ("STALE_CANDLES", "STALE_HTF_CANDLE", "FEED_DISCONNECTED")) else "BLOCKED_MISSING_CONTEXT"
            decision.update(status=status, evidence_completeness="PARTIAL")
        else:
            try:
                decision.update(FrozenSignalAdapter(reg).evaluate(context, cutoff), evidence_completeness="COMPLETE")
                # Without per-candle publication evidence the existing classifier
                # correctly returns UNKNOWN, never a fabricated regime label.
                decision["market_context"] = classify_market_context(context.get("5m", []), higher_candles=context.get("1h", []), signal_timestamp=cutoff, entry_timeframe="5m", higher_timeframe="1h", market_data_source=frame["source"], classification_timestamp=now)
            except Exception as exc:
                decision.update(status="EVALUATION_ERROR", rejection_reasons=["EVALUATOR_ERROR:" + type(exc).__name__], evidence_completeness="PARTIAL", signal_direction=None, signal_strength=None)
        decision["evaluation_latency_ms"] = round((time.monotonic() - start) * 1000, 3)
        decision["evaluation_completed_at"] = self.clock().isoformat()
        if decision["evaluation_latency_ms"] > self.policy["max_latency_seconds"] * 1000:
            decision.update(status="EVALUATION_ERROR", rejection_reasons=["EVALUATION_LATENCY_LIMIT"], evidence_completeness="PARTIAL", signal_direction=None)
        return self.store.commit(decision, generation=reg["generation"], lease_token=lease_token, now=self.clock().timestamp(), max_observations=self.policy["max_observations"])

    def comparison(self, rid, *, owner_id, limit=1000):
        rows = self.store.decisions(rid, owner_id=owner_id, limit=limit)
        eligible = [r for r in rows if r["status"] in {"SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL"}]
        matched = []
        for row in eligible:
            active = self.active_reader(deepcopy(row), owner_id) if self.active_reader else None
            keys = ("venue", "market_type", "symbol", "timeframe", "candle_close_timestamp", "candle_hash", "context_hash")
            if active and all(active.get(k) == row[k] for k in keys) and active.get("data_freshness") == "FRESH" and active.get("status") in {"SIGNAL_LONG", "SIGNAL_SHORT", "NO_SIGNAL"}:
                matched.append((row["status"], active["status"]))
        agree = sum(a == b for a, b in matched)
        return {"contract_version": "shadow_comparison.v1", "matched_opportunities": len(matched), "agreement_count": agree,
                "agreement_rate": agree / len(matched) if matched else None, "disagreement_rate": 1 - agree / len(matched) if matched else None,
                "unmatched_shadow_opportunities": len(eligible) - len(matched), "active_state": "MATCHED_EVIDENCE" if matched else "NO_DECISION_RECORDED",
                "scope": "LATEST_BOUNDED_DECISIONS", "query_limit": limit, "profitability": "NOT_ASSESSED",
                "warning": "Signal agreement is not trading performance. Missing active evidence is not NO_SIGNAL."}
