"""Journal-only derived intelligence; public reads never submit or replay orders.

Classification and aggregation run from the existing recovery worker. Cached
reports describe a specific assessed source view; startup, dirty scopes and
expired views cannot make current profitability-verification claims.
"""
from __future__ import annotations

import copy
from dataclasses import fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import logging
import threading
import uuid

from services.strategy_evidence import SCOPE_FIELDS, evidence_json
from services.strategy_evidence_completeness import assess_evidence_completeness
from services.strategy_intelligence_metrics import EvidenceCohort


log = logging.getLogger(__name__)
CONTRACT_VERSION = "strategy_intelligence.v2"
_COHORT_FIELDS = tuple(field.name for field in fields(EvidenceCohort))
_SCOPE_FIELDS = ("owner_id", "instance_id", "simulation_session_id", "account_id")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(evidence_json(value).encode()).hexdigest()


def _scope_match(row, scope):
    return all(row.get(key) == value for key, value in scope.items())


def _entry_key(row, trade_id):
    return (*tuple(row.get(key) for key in SCOPE_FIELDS), trade_id)


def _cohort(row, symbol=None):
    material = {key: row.get(key) for key in _COHORT_FIELDS}
    material["config_fingerprint"] = row.get("config_fingerprint", row.get("strategy_config_hash"))
    material["symbol"] = symbol
    return EvidenceCohort(**material)


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


class StrategyIntelligenceService:
    def __init__(self, journal, *, max_records=100_000, batch_size=500):
        if max_records < 1 or batch_size < 1:
            raise ValueError("intelligence bounds must be positive")
        self.journal = journal
        self.context = journal.context
        self.max_records, self.batch_size = max_records, batch_size
        self._lock = threading.RLock()
        self._ready_lock = threading.Lock()
        self._ready_scopes = set()
        self._scope_generations = {}

    @staticmethod
    def _scope_key(scope):
        return evidence_json(scope)

    @staticmethod
    def _cache_key(cohort, group_by):
        return "strategy_intelligence.v2:" + _hash({"cohort": cohort.as_dict(), "group_by": list(group_by)})

    def invalidate_scope(self, scope):
        with self._ready_lock:
            self._ready_scopes = {key for key in self._ready_scopes
                                  if not _scope_match(json.loads(key), scope)}
            key = self._scope_key(scope)
            self._scope_generations[key] = self._scope_generations.get(key, 0) + 1

    @staticmethod
    def _unknown_context(header, original, definition, now):
        return {
            "trade_id": header["root_trade_id"], "episode_id": header["episode_id"],
            "entry_timestamp": header.get("opened_at"),
            "signal_timestamp": None, "signal_candle_timestamp": original.get("timestamp"),
            "entry_timeframe": original.get("timeframe"), "higher_timeframe": None,
            "exchange": (original.get("journal_execution") or {}).get("exchange"),
            "market_type": (original.get("journal_execution") or {}).get("instrument_type"),
            "market_data_source": original.get("market_data_source"),
            "market_data_timestamp": None, "last_closed_candle_timestamp": None,
            "session": "UNKNOWN", "trend_regime": "UNKNOWN", "volatility_regime": "UNKNOWN",
            "structure_regime": "UNKNOWN", "trend_strength": None, "atr_value": None,
            "atr_percentile": None, "classifier_id": definition["classifier_id"],
            "classifier_version": definition["classifier_version"], "parameter_hash": definition["parameter_hash"],
            "classification_timestamp": now, "context_quality": "UNKNOWN", "evidence_quality": "UNKNOWN",
            "quality_reasons": ["ORIGINAL_POINT_IN_TIME_MARKET_EVIDENCE_UNAVAILABLE"],
            "reconstruction_status": "UNKNOWN", "classification_kind": "ENTRY",
        }

    def _classify_missing(self, material, scope, now):
        from services.market_context_classifier import classifier_definition, classify_frozen_context
        episodes = {row["episode_id"]: row for row in material["episodes"] if _scope_match(row, scope)}
        headers = []
        for event in material["events"]:
            header = episodes.get(event.get("episode_id"))
            if (header is not None and event["kind"] == "execution_fill"
                    and event["payload"].get("action") in ("opened", "recovered")
                    and all(event.get(key) == header.get(key) for key in SCOPE_FIELDS)):
                headers.append({**header, "root_trade_id": event["trade_id"],
                                "opened_at": event.get("observed_at")})
        contexts = {_entry_key(row, row["trade_id"]): row["payload"] for row in material["events"]
                    if row["kind"] == "TRADE_ENTRY_CONTEXT"}
        identities = self.context.context_identity_snapshot(filters=scope, max_records=self.max_records)
        if not identities["source_complete"]:
            log.warning("market_context_identity_bound_exceeded scope=%s", scope.get("instance_id"))
            return 0
        existing = set(identities["keys"])
        processed = 0
        for header in headers:
            original = contexts.get(_entry_key(header, header["root_trade_id"]), {})
            frozen = original.get("market_context_input")
            definition = (frozen or {}).get("classifier_definition") or classifier_definition()
            identity = (header["episode_id"], header["root_trade_id"], definition["classifier_id"],
                        definition["classifier_version"], definition["parameter_hash"], "ENTRY")
            if identity in existing:
                continue
            if processed >= self.batch_size:
                break
            self.context.save_classifier_version(definition)
            snapshot = self._unknown_context(header, original, definition, now)
            if frozen:
                try:
                    snapshot.update(classify_frozen_context(frozen, classification_timestamp=now))
                    snapshot.update(entry_timestamp=header.get("opened_at"),
                        reconstruction_status=("SAFE_TO_RECONSTRUCT" if snapshot["context_quality"] == "VALID"
                                               else "PARTIALLY_RECONSTRUCTABLE"))
                except (ValueError, TypeError, KeyError):
                    log.exception("market_context_classification_failed episode_id=%s", header["episode_id"])
                    snapshot["quality_reasons"] = ["INVALID_OR_CONFLICTING_FROZEN_MARKET_EVIDENCE"]
            snapshot.update({key: header.get(key) for key in (
                "strategy_id", "strategy_version", "strategy_config_hash", "instance_id",
                "simulation_session_id", "execution_mode", "owner_id", "account_id", "lab_id", "source_kind", "symbol")})
            snapshot.update(trade_id=header["root_trade_id"], episode_id=header["episode_id"],
                config_fingerprint=header.get("strategy_config_hash"),
                configuration_hash=header.get("strategy_config_hash"),
                direction={"BUY": "long", "SELL": "short"}.get(original.get("side"), original.get("side")),
                raw_market_evidence=copy.deepcopy(frozen),
                original_input_hash=((frozen or {}).get("original_input_hash") or _hash(frozen)))
            self.context.record_market_context(snapshot)
            existing.add(identity)
            processed += 1
            log.info("market_context_classified episode_id=%s quality=%s classifier=%s",
                     header["episode_id"], snapshot["context_quality"], snapshot["classifier_version"])
        return processed

    @staticmethod
    def _enrich_episodes(material):
        journals = {_entry_key(row, row["trade_id"]): row for row in material["journals"]}
        events, receipts = {}, {}
        headers = {row["episode_id"]: row for row in material["episodes"]}
        for event in material["events"]:
            header = headers.get(event.get("episode_id"))
            if (event["kind"] == "execution_fill" and header is not None
                    and all(event.get(key) == header.get(key) for key in SCOPE_FIELDS)):
                receipts.setdefault(event["episode_id"], []).append(event["payload"])
                if event["payload"]["action"] in ("opened", "recovered"):
                    events.setdefault(event["episode_id"], event)
        rows = []
        for episode in material["episodes"]:
            row = dict(episode)
            entry = events.get(row["episode_id"], {})
            closing = [(fact.get("receipt") or {}) for fact in receipts.get(row["episode_id"], [])
                       if fact["action"] in ("closed", "reduced")]
            def model(name):
                models = set()
                for receipt in closing:
                    value = receipt.get(name + "_cost_model") or "UNKNOWN"
                    if name == "funding" and str(receipt.get("funding_coverage")).upper() in ("NOT_MODELED", "UNMODELED"):
                        value = "UNMODELED"
                    models.add(value)
                return next(iter(models)) if len(models) == 1 else "UNKNOWN"
            journal = journals.get(_entry_key(row, row["root_trade_id"]), {})
            row.update(direction=(entry.get("payload") or {}).get("side"),
                       planned_rr=_finite(journal.get("planned_rr")),
                       fees_cost_model=model("fees"), funding_cost_model=model("funding"),
                       slippage_cost_model="UNKNOWN", slippage=None, slippage_coverage="UNKNOWN")
            # MAE/MFE are observations, not inferred from final P&L or candles.
            final = next((journals[_entry_key(row, tid)] for tid in row.get("trade_ids", [])
                          if _entry_key(row, tid) in journals
                          and journals[_entry_key(row, tid)].get("closed_at") == row.get("closed_at")), {})
            exit_context = (final.get("sections") or {}).get("exit_decision") or {}
            row["mfe_r"] = _finite(exit_context.get("max_profit_r"))
            row["mae_r"] = _finite(exit_context.get("max_drawdown_r"))
            # Existing receipts do not quantify actual slippage separately.
            # Its inclusion in executed price cannot prove complete coverage.
            all_receipts = [(fact.get("receipt") or {}) for fact in receipts.get(row["episode_id"], [])]
            if all_receipts and all(receipt.get("slippage_coverage") in ("BOOKED", "MODELED", "VERIFIED_ZERO")
                                    and _finite(receipt.get("slippage")) is not None for receipt in all_receipts):
                models = {receipt.get("slippage_cost_model") or "UNKNOWN" for receipt in all_receipts}
                coverages = {receipt["slippage_coverage"] for receipt in all_receipts}
                with localcontext() as precision:
                    precision.prec = 50
                    amount = str(sum((Decimal(str(receipt["slippage"])) for receipt in all_receipts), Decimal(0)))
                row.update(slippage=amount, slippage_coverage=next(iter(coverages)) if len(coverages) == 1 else "MODELED",
                    slippage_cost_model=next(iter(models)) if len(models) == 1 else "UNKNOWN")
            rows.append(row)
        return rows

    def refresh(self, ledger, *, scope=None, reconciliation_report=None):
        """Observe authority and rebuild only changed/expired derived caches."""
        from services.strategy_intelligence_v2 import SUPPORTED_GROUPINGS, calculate_context_performance
        scope = dict(scope or {key: getattr(ledger, key) for key in ("instance_id", "simulation_session_id")
                               if hasattr(ledger, key)})
        now = _now()
        with self._lock:
            self.invalidate_scope(scope)
            with self._ready_lock:
                generation = self._scope_generations[self._scope_key(scope)]
            primary = ledger.get_authoritative_evidence_snapshot()
            material = self.journal.get_evidence_completeness_snapshot(max_records=self.max_records)
            processed = self._classify_missing(material, scope, now)
            context_source = self.context.context_snapshot(max_records=self.max_records, filters=scope)
            all_contexts = context_source["contexts"]
            episodes = self._enrich_episodes(material)
            cohorts = {}
            for row in episodes:
                if not _scope_match(row, scope):
                    continue
                for symbol in (None, row.get("symbol")):
                    try:
                        cohort = _cohort(row, symbol=symbol)
                    except ValueError:
                        continue
                    cohorts[evidence_json(cohort.as_dict())] = cohort
            current = ledger.get_authoritative_evidence_snapshot()
            if current.get("source_watermark") != primary.get("source_watermark"):
                primary = {**primary, "source_complete": False, "read_error": "SOURCE_CHANGED_DURING_CONTEXT_PROCESSING"}
            runs = 0
            for cohort in cohorts.values():
                report = assess_evidence_completeness(primary, material, cohort=cohort, calculated_at=now,
                                                      max_records=self.max_records)
                if not context_source["source_complete"]:
                    report.update(status="UNKNOWN", history_complete=False)
                input_watermark = _hash({"source": report["source_watermark"],
                                         "contexts": context_source["watermark"], "cohort": cohort.as_dict()})
                for group_by in SUPPORTED_GROUPINGS:
                    cache_key = self._cache_key(cohort, group_by)
                    old = self.context.get_latest_intelligence_run(cache_key,
                        filters={key: cohort.as_dict()[key] for key in ("owner_id", "account_id", "instance_id")})
                    age = ((datetime.fromisoformat(now) - datetime.fromisoformat(old["calculated_at"])).total_seconds()
                           if old else 999)
                    if old and old["input_watermark"] == input_watermark and 0 <= age < 240:
                        continue
                    result = calculate_context_performance(episodes, all_contexts, cohort=cohort,
                        group_by=list(group_by), source_watermark=report["source_watermark"],
                        completeness_report=report, calculation_timestamp=now,
                        evidence_episodes=material["episodes"])
                    result.update(run_id=uuid.uuid4().hex, cache_key=cache_key, input_watermark=input_watermark,
                        calculated_at=now, scope={key: cohort.as_dict()[key] for key in _SCOPE_FIELDS},
                        cohort=cohort.as_dict(), group_by=list(group_by), contract_version=CONTRACT_VERSION)
                    self.context.record_intelligence_run(result)
                    runs += 1
            final_source = ledger.get_authoritative_evidence_snapshot()
            source_stable = (primary.get("source_complete") is True
                             and final_source.get("source_complete") is True
                             and final_source.get("source_watermark") == primary.get("source_watermark"))
            with self._ready_lock:
                ready = source_stable and self._scope_generations[self._scope_key(scope)] == generation
                if ready:
                    self._ready_scopes.add(self._scope_key(scope))
        return {"processed_contexts": processed, "calculation_runs": runs,
                "cohort_count": len(cohorts), "calculation_timestamp": now,
                "status": "READY" if ready else "RECOVERING"}

    def _ready(self, scope):
        with self._ready_lock:
            return any(_scope_match(scope, json.loads(key)) for key in self._ready_scopes)

    def recompute_research(self, *, scope, definition, source_classifier):
        """Append a separate research version using only original observations.

        This internal operation has no market downloads or accounting access.
        Consumers must supply an exact authorized scope; public GET routes never
        invoke it. Missing historical observations remain unknown.
        """
        from services.market_context_classifier import classify_frozen_context
        if any(key not in scope for key in _SCOPE_FIELDS) or not scope.get("owner_id"):
            raise ValueError("research requires exact owner/account/instance/session scope")
        identity_fields = ("classifier_id", "classifier_version", "parameter_hash")
        if set(source_classifier) != set(identity_fields):
            raise ValueError("research requires an exact source classifier identity")
        if (definition.get("classifier_id"), definition.get("classifier_version")) == (
                source_classifier["classifier_id"], source_classifier["classifier_version"]):
            raise ValueError("research requires a different classifier version")
        with self._lock:
            self.context.save_classifier_version(definition)
            source = self.context.context_snapshot(max_records=self.max_records,
                filters={**scope, **source_classifier, "classification_kind": "ENTRY"})
            identities = self.context.context_identity_snapshot(filters={**scope, "classification_kind": "RESEARCH"},
                                                                 max_records=self.max_records)
            if not source["source_complete"] or not identities["source_complete"]:
                return {"processed_contexts": 0, "status": "PARTIAL", "reason": "RECOMPUTATION_BOUND_EXCEEDED"}
            existing = set(identities["keys"])
            processed = 0
            for original in source["contexts"]:
                identity = (original["episode_id"], original["trade_id"], definition["classifier_id"],
                            definition["classifier_version"], definition["parameter_hash"], "RESEARCH")
                if identity in existing:
                    continue
                if processed >= self.batch_size:
                    return {"processed_contexts": processed, "status": "RECOVERING"}
                revised = copy.deepcopy(original)
                revised.pop("snapshot_id", None)
                revised.update({key: definition[key] for key in identity_fields})
                revised.update(classification_kind="RESEARCH", source_snapshot_id=original["snapshot_id"],
                               classification_timestamp=_now())
                frozen = copy.deepcopy(original.get("raw_market_evidence"))
                if frozen:
                    frozen["classifier_definition"] = copy.deepcopy(definition)
                    revised.update(classify_frozen_context(frozen,
                        classification_timestamp=revised["classification_timestamp"]))
                self.context.record_market_context(revised)
                existing.add(identity)
                processed += 1
            return {"processed_contexts": processed, "status": "COMPLETE"}

    def read(self, view, scope, filters):
        """Bounded metadata/cache SELECTs only; no market or ledger access."""
        from services.strategy_intelligence_v2 import SUPPORTED_GROUPINGS
        scope, filters = dict(scope or {}), dict(filters or {})
        if not scope.get("owner_id"):
            raise ValueError("owner scope required for intelligence reads")
        envelope = {"contract_version": CONTRACT_VERSION, "calculation_timestamp": None,
                    "evidence_quality": "UNKNOWN", "cost_coverage": {"fees": "UNKNOWN", "funding": "UNKNOWN", "slippage": "UNKNOWN"},
                    "sample_confidence": {"classification": "INSUFFICIENT", "label": "INSUFFICIENT"},
                    "profitability_verified": False, "cache_status": "PENDING"}
        if view in ("sessions", "regimes", "volatility"):
            from services.market_context_classifier import classifier_definition
            return {**envelope, "definition": classifier_definition(), "view": view}
        if view == "overview":
            return self._read_overview(scope, filters, envelope)
        if view == "edge_map":
            return self._read_edge_map(scope, filters, envelope)
        if view == "comparison":
            return self._read_comparison(scope, filters, envelope)
        if view == "episodes":
            return self._read_episodes(scope, filters, envelope)
        if view == "qualification":
            return self._read_qualification(scope, filters, envelope)
        if view in ("oos", "walk_forward", "robustness"):
            return self._read_validation_projection(view, scope, filters, envelope)
        if view == "validation_history":
            return self._read_validation_history(scope, filters, envelope)
        if view == "cohorts":
            rows = self.context.cached_cohorts(filters=scope, limit=500)
            cohorts = []
            seen = set()
            for row in rows:
                try:
                    cohort = _cohort(row).as_dict()
                except ValueError:
                    continue
                key = evidence_json(cohort)
                if key not in seen:
                    cohorts.append(cohort)
                    seen.add(key)
            return {**envelope, "cohorts": cohorts, "supported_groupings": [list(group) for group in SUPPORTED_GROUPINGS]}
        exact = {key: filters[key] for key in _COHORT_FIELDS if key in filters}
        if view == "context":
            query = {**exact, **scope}
            if query.get("symbol") is None:
                query.pop("symbol", None)
            if "config_fingerprint" in query:
                query["strategy_config_hash"] = query.pop("config_fingerprint")
            contexts = self.context.market_contexts(filters=query, limit=int(filters.get("limit", 100)))
            return {**envelope, "contexts": contexts, "cache_status": "READY"}
        if view not in ("performance", "confidence"):
            raise ValueError("unknown intelligence view")
        cohort = _cohort({**exact, **scope}, symbol=filters.get("symbol"))
        group_by = tuple(filters.get("group_by") or ("symbol", "session", "trend_regime"))
        if group_by not in SUPPORTED_GROUPINGS:
            raise ValueError("unsupported intelligence grouping")
        cached = self.context.get_latest_intelligence_run(self._cache_key(cohort, group_by),
            filters={key: scope.get(key) for key in ("owner_id", "account_id", "instance_id")})
        if cached is None:
            return {**envelope, "cohort": cohort.as_dict(), "groups": []}
        if (cached.get("cohort") != cohort.as_dict() or cached.get("group_by") != list(group_by)
                or any(key not in cached.get("scope", {}) or cached["scope"][key] != scope.get(key)
                       for key in _SCOPE_FIELDS)):
            log.error("strategy_intelligence_cache_identity_conflict instance_id=%s", scope.get("instance_id"))
            return {**envelope, "cohort": cohort.as_dict(), "groups": [],
                    "cache_status": "CONFLICTED", "evidence_quality": "CONFLICTED",
                    "quality_reasons": ["CACHE_IDENTITY_BINDING_MISMATCH"]}
        result = copy.deepcopy(cached)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(result["calculated_at"])).total_seconds()
        fresh = 0 <= age <= 300 and self._ready(scope)
        result["cache_status"] = "READY" if fresh else "STALE" if age > 300 else "RECONCILIATION_REQUIRED"
        if not fresh:
            result["profitability_verified"] = False
            result["evidence_quality"] = "UNKNOWN"
            for group in result.get("groups", []):
                group["profitability_verified"] = False
                quality = group.get("evidence_quality")
                group["evidence_quality"] = {
                    **(quality if isinstance(quality, dict) else {}),
                    "status": "UNKNOWN", "binding_status": result["cache_status"],
                    "reasons": list(dict.fromkeys((quality.get("reasons", []) if isinstance(quality, dict) else [])
                                                  + [result["cache_status"]]))}
                group.setdefault("metrics", {}).update(profitability_verified=False,
                    ready_for_evidence_review=False, history_complete=False,
                    evidence_completeness_status="UNKNOWN", completeness_binding_status=result["cache_status"])
                profitability = group.get("profitability", {})
                group["profitability"] = {**profitability, "verified": False, "status": "UNVERIFIED",
                    "blockers": list(dict.fromkeys(profitability.get("blockers", []) + [result["cache_status"]]))}
        return {**envelope, **result}

    def _read_overview(self, scope, filters, envelope):
        """Build overview cards from already persisted cohort caches."""
        cohorts = self.context.cached_cohorts(filters=scope, limit=500)
        rows, seen = [], set()
        requested = set(filters.get("strategy_ids") or [])
        for raw in cohorts:
            if raw.get("symbol") is None:
                continue
            if requested and raw.get("strategy_id") not in requested:
                continue
            cohort = _cohort(raw, symbol=raw.get("symbol"))
            key = evidence_json(cohort.as_dict())
            if key in seen:
                continue
            seen.add(key)
            grouping = ("symbol",)
            report = self.context.get_latest_intelligence_run(
                self._cache_key(cohort, grouping),
                filters={key: scope.get(key) for key in ("owner_id", "account_id", "instance_id")})
            if report is None:
                rows.append({"cohort": cohort.as_dict(), "cache_status": "PENDING",
                             "evidence_quality": "UNKNOWN", "profitability_verified": False})
                continue
            group = next(iter(report.get("groups", [])), {})
            metrics = group.get("metrics", {})
            rows.append({"cohort": cohort.as_dict(), "cache_status": "RECONCILIATION_REQUIRED",
                         "calculation_timestamp": report.get("calculated_at"),
                         "completed_episode_count": metrics.get("completed_episode_count", report.get("completed_episode_count", 0)),
                         "win_rate_pct": metrics.get("win_rate_pct"),
                         "profit_factor": metrics.get("net_profit_factor"),
                         "expectancy_r": metrics.get("expectancy_net_r"),
                         "net_pnl": metrics.get("net_pnl"),
                         "evidence_quality": group.get("evidence_quality", "UNKNOWN"),
                         "sample_confidence": group.get("sample_confidence", {"classification": "INSUFFICIENT"}),
                         "cost_coverage": group.get("cost_coverage", {"status": "UNKNOWN"}),
                         "profitability_verified": False})
        strategy_ids = {row["cohort"].get("strategy_id") for row in rows}
        return {**envelope, "view": "overview", "rows": rows,
                "summary": {"total_strategies": len(strategy_ids),
                            "strategies_under_observation": len(strategy_ids),
                            "qualified_strategies": 0,
                            "total_completed_episodes": sum(int(row.get("completed_episode_count") or 0) for row in rows),
                            "qualification_status": "NOT_AVAILABLE",
                            "evidence_quality": "UNKNOWN" if not rows else "PARTIAL"}}

    def _read_edge_map(self, scope, filters, envelope):
        x_dimension, y_dimension = filters.get("x_dimension"), filters.get("y_dimension")
        if not isinstance(x_dimension, str) or not isinstance(y_dimension, str) or x_dimension == y_dimension:
            raise ValueError("edge map dimensions must be distinct")
        performance = self.read("performance", scope, {**filters, "group_by": [y_dimension, x_dimension]})
        metric = filters.get("metric", "expectancy_r")
        metric_aliases = {"expectancy_r": "expectancy_net_r", "profit_factor": "net_profit_factor",
                          "win_rate": "win_rate_pct", "net_pnl": "net_pnl", "max_drawdown": "max_drawdown",
                          "completed_episodes": "completed_episode_count", "average_realised_r": "average_realised_rr"}
        field = metric_aliases.get(metric)
        if field is None:
            raise ValueError("unsupported edge map metric")
        cells = []
        for group in performance.get("groups", []):
            dimensions = group.get("group", {})
            cells.append({"x": dimensions.get(x_dimension), "y": dimensions.get(y_dimension),
                          "metric": group.get("metrics", {}).get(field),
                          "completed_episodes": group.get("metrics", {}).get("completed_episode_count"),
                          "evidence_quality": group.get("evidence_quality", "UNKNOWN"),
                          "sample_confidence": group.get("sample_confidence", {"classification": "INSUFFICIENT"}),
                          "cost_coverage": group.get("cost_coverage", {"status": "UNKNOWN"}),
                          "profitability_verified": group.get("profitability_verified", False),
                          "group_key": group.get("group_key")})
        return {**performance, "view": "edge_map", "x_dimension": x_dimension,
                "y_dimension": y_dimension, "metric": metric, "cells": cells}

    def _read_comparison(self, scope, filters, envelope):
        requested = [item for item in filters.get("strategy_ids", []) if item]
        if len(requested) < 2 or len(requested) > 4:
            raise ValueError("comparison requires two to four strategy IDs")
        rows = []
        cohorts = self.context.cached_cohorts(filters=scope, limit=500)
        for raw in cohorts:
            if raw.get("strategy_id") not in requested or (filters.get("symbol") and raw.get("symbol") != filters["symbol"]):
                continue
            cohort = _cohort(raw, symbol=raw.get("symbol"))
            report = self.context.get_latest_intelligence_run(self._cache_key(cohort, ("symbol",)),
                filters={key: scope.get(key) for key in ("owner_id", "account_id", "instance_id")})
            if not report:
                continue
            group = next(iter(report.get("groups", [])), {})
            rows.append({"strategy_id": cohort.strategy_id, "cohort": cohort.as_dict(),
                         "metrics": group.get("metrics", {}), "evidence_quality": group.get("evidence_quality", "UNKNOWN"),
                         "sample_confidence": group.get("sample_confidence", {"classification": "INSUFFICIENT"}),
                         "cost_coverage": group.get("cost_coverage", {"status": "UNKNOWN"}),
                         "profitability_verified": False})
        comparable_keys = {(row["cohort"].get("execution_mode"), row["cohort"].get("source_kind"),
                           row["cohort"].get("symbol"), row["cohort"].get("simulation_session_id")) for row in rows}
        compatible = len(comparable_keys) <= 1 and {row["strategy_id"] for row in rows} == set(requested)
        return {**envelope, "view": "comparison", "rows": rows,
                "comparison_status": "COMPARABLE" if compatible else "NOT_DIRECTLY_COMPARABLE",
                "comparison_reasons": [] if compatible else ["COHORT_DIMENSIONS_DO_NOT_MATCH"]}

    def _read_episodes(self, scope, filters, envelope):
        limit = min(max(int(filters.get("limit", 50)), 1), 100)
        offset = max(int(filters.get("offset", 0)), 0)
        query = {key: scope.get(key) for key in SCOPE_FIELDS if key in scope}
        for key in ("strategy_id", "strategy_version", "config_fingerprint", "execution_mode", "source_kind", "symbol", "episode_id"):
            if key in filters and filters[key] is not None:
                query["strategy_config_hash" if key == "config_fingerprint" else key] = filters[key]
        episodes = self.journal.completed_evidence_episodes(**query)
        page = episodes[offset:offset + limit]
        contexts = {row.get("episode_id"): row for row in self.context.market_contexts(filters=query, limit=100000)}
        rows = []
        for episode in page:
            context = contexts.get(episode.get("episode_id"))
            rows.append({"episode_id": episode.get("episode_id"), "trade_id": episode.get("root_trade_id"),
                         "strategy_id": episode.get("strategy_id"), "strategy_version": episode.get("strategy_version"),
                         "config_fingerprint": episode.get("config_fingerprint"), "symbol": episode.get("symbol"),
                         "opened_at": episode.get("opened_at"), "closed_at": episode.get("closed_at"),
                         "net_pnl": episode.get("net_pnl"), "gross_pnl": episode.get("gross_pnl"),
                         "fees": episode.get("fees"), "funding": episode.get("funding"),
                         "funding_coverage": episode.get("funding_coverage", "UNKNOWN"),
                         "initial_risk": episode.get("initial_risk"), "trade_ids": episode.get("trade_ids", []),
                         "context": context, "evidence_quality": "UNKNOWN" if context is None else context.get("evidence_quality", "UNKNOWN")})
        return {**envelope, "view": "episodes", "rows": rows, "total": len(episodes),
                "offset": offset, "limit": limit}

    def _validation_filters(self, scope, filters):
        result = {}
        for key in _SCOPE_FIELDS:
            if scope.get(key) is not None:
                result["config_fingerprint" if key == "strategy_config_hash" else key] = scope[key]
        for key in ("run_id", "strategy_id", "strategy_version", "config_fingerprint", "dataset_identity",
                    "execution_mode", "method", "status"):
            if filters.get(key) is not None:
                result[key] = filters[key]
        return result

    def _read_validation_history(self, scope, filters, envelope):
        from services.worker_backends import research_context
        rows = research_context(self.context).validation_runs(filters=self._validation_filters(scope, filters),
                                            limit=min(int(filters.get("limit", 100)), 1000))
        return {**envelope, "view": "validation_history", "rows": rows, "total": len(rows)}

    def _read_validation_projection(self, view, scope, filters, envelope):
        method = {"oos": "out_of_sample", "walk_forward": "walk_forward", "robustness": "robustness"}[view]
        qualification = self._read_qualification(scope, filters, envelope)
        chain = qualification["evidence_chain"]
        rows = [row for row in qualification["validation_history"] if row.get("method") == method]
        run_id = chain["validation_run_ids"].get(method)
        latest = next((row for row in rows if row.get("run_id") == run_id), None)
        stage = chain["stages"][method]
        return {**envelope, "view": view, "status": stage["status"], "binding_status": stage["binding_status"],
                "result": latest, "rows": rows, "available": latest is not None,
                "evidence_chain": chain}

    def _read_qualification(self, scope, filters, envelope):
        from services.strategy_qualification import QualificationPolicy, evaluate_qualification
        from services.historical_forward_evidence import build_evidence_chain, _authorized
        identity = {key: filters.get(key) for key in (
            "strategy_id", "strategy_version", "config_fingerprint", "symbol", "execution_mode",
            "source_kind", "lab_id")}
        identity.update({key: scope.get(key) for key in _SCOPE_FIELDS})
        performance = {}
        if filters.get("strategy_id") and filters.get("execution_mode") and filters.get("source_kind"):
            performance = self.read("performance", scope, {**filters, "group_by": ["symbol"]})
        from services.worker_backends import research_context
        context = research_context(self.context)
        # Deliberately omit execution_mode: identity joins the backtest and
        # paper partitions. Scope remains explicit, including isolated research.
        query = {"owner_id": scope["owner_id"], **{key: identity.get(key) for key in (
            "strategy_id", "strategy_version", "config_fingerprint")}}
        candidates = context.validation_runs(filters=query, limit=1000)
        records = [row for row in candidates if _authorized(row, scope)]
        jobs = context.research_jobs(filters={"owner_id": scope["owner_id"],
                                               "strategy_id": identity.get("strategy_id")}, limit=1000)
        frozen = self.journal.strategy_identity(identity.get("strategy_id"), identity.get("strategy_version"),
                                                identity.get("config_fingerprint"))
        chain = build_evidence_chain(scope=scope, identity=identity, frozen=frozen, records=records,
            performance=performance, jobs=jobs, bounded=len(candidates) == 1000 or len(jobs) == 1000)
        reports = chain["reports"]
        metrics = chain["historical"]["metrics"]
        report = evaluate_qualification(identity, metrics,
            oos=reports.get("out_of_sample"), walk_forward=reports.get("walk_forward"),
            robustness=reports.get("robustness"), forward_paper=chain["forward_paper"],
            cost_stress=reports.get("cost_stress"), policy=QualificationPolicy(),
            evidence_quality=metrics.get("evidence_quality", "UNKNOWN"))
        if chain["status"] != "COMPLETE" and report["status"] == "QUALIFIED":
            report.update(status="NOT_QUALIFIED", blocking_reasons=chain["blocking_reasons"])
        chain.pop("reports")
        return {**envelope, "view": "qualification", **report,
                "contract_version": CONTRACT_VERSION, "qualification_contract_version": report["contract_version"],
                "evidence_chain": chain, "validation_history": records, "qualification_status": report["status"]}
