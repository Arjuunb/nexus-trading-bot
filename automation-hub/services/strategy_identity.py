"""Read-only identity of the instantiated strategy and its effective settings.

This module neither constructs execution strategies nor reads environment values.
It describes observations made *now*; a missing historical object stays unknown.
The journal store owns immutable persistence of the detached JSON snapshots.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from functools import lru_cache
import hashlib
import importlib.metadata
import inspect
import json
import math
from pathlib import Path
import sys


_HUB = Path(__file__).resolve().parents[1]
_REPO = _HUB.parent
_ALIASES = {"decision_brain": "brain", "trend_following": "supertrend"}
_CLASSES = {
    "adaptive_trend_pullback": ("strategies.adaptive_trend_pullback.strategy", "AdaptiveTrendPullbackStrategy"),
    "brain": ("strategies.brain_strategy", "DecisionBrain"),
    "supertrend": ("strategies.supertrend_strategy", "SupertrendStrategy"),
    "donchian": ("strategies.donchian_strategy", "DonchianStrategy"),
    "ema": ("strategies.ema_strategy", "EMAStrategy"),
    "ensemble": ("strategies.ensemble_strategy", "ConfirmationEnsemble"),
    "smc": ("strategies.smc_strategy", "SMCStrategy"),
    "liquidity_sweep": ("strategies.liquidity_sweep_strategy", "LiquiditySweepStrategy"),
    "price_action_rejection": ("strategies.price_action_rejection", "PriceActionRejectionStrategy"),
    "price_action_flip_retest": ("strategies.price_action_rejection", "PriceActionFlipRetestStrategy"),
    "custom": ("strategies.custom_adapter", "CustomStrategyAdapter"),
}
_SECRET_KEYS = {
    "api_key", "api_secret", "secret", "password", "passwd", "token", "access_token",
    "refresh_token", "auth_token", "private_key", "client_secret", "credential", "credentials",
}
_SECRET_COMPACT_KEYS = {key.replace("_", "") for key in _SECRET_KEYS}


def strategy_id_for(strategy) -> str | None:
    """Resolve a registered implementation; never infer identity from a label."""
    if strategy is None:
        return None
    implementation = (type(strategy).__module__, type(strategy).__name__)
    return next((key for key, expected in _CLASSES.items() if expected == implementation), None)


def _json_value(value):
    """Strict, detached canonical data; never stringify unknown objects."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("configuration contains a non-finite number")
        # Python strategy parameters treat 2 and 2.0 equally. Keep bool distinct.
        return int(value) if value.is_integer() else value
    if is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("configuration keys must be strings")
            if key.strip().lower().replace("-", "").replace("_", "") in _SECRET_COMPACT_KEYS:
                raise ValueError("configuration contains a credential field")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise ValueError("configuration contains an unsupported value type")


def canonical_configuration_json(configuration) -> str:
    """Stable UTF-8 JSON representation, suitable for immutable storage."""
    try:
        return json.dumps(_json_value(configuration), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (RecursionError, TypeError, OverflowError) as exc:
        raise ValueError("configuration cannot be canonicalized") from exc


def configuration_fingerprint(configuration) -> str:
    """SHA-256 of canonical effective configuration, independently of source."""
    return hashlib.sha256(canonical_configuration_json(configuration).encode("utf-8")).hexdigest()


def _resolve_module(module: str) -> Path | None:
    if not module:
        return None
    for root in (_HUB, _REPO):
        relative = Path(*module.split("."))
        for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
            if candidate.is_file():
                return candidate.resolve()
    return None


def _module_name(path: Path) -> str:
    root = _HUB if path.is_relative_to(_HUB) else _REPO
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@lru_cache(maxsize=512)
def _source_record(path: str, mtime_ns: int, size: int):
    # Stat fields invalidate the cache; they are never part of the fingerprint.
    source = Path(path).read_bytes()
    tree = ast.parse(source, filename=path)
    module = _module_name(Path(path))
    package = module if Path(path).name == "__init__.py" else module.rpartition(".")[0]
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                segments = package.split(".")
                base = ".".join(segments[:len(segments) - node.level + 1])
                target = ".".join(part for part in (base, node.module) if part)
            else:
                target = node.module or ""
            if target:
                imports.add(target)
                imports.update(f"{target}.{alias.name}" for alias in node.names if alias.name != "*")
    return hashlib.sha256(source).hexdigest(), tuple(sorted(imports))


def _source_manifest(strategy) -> dict:
    """Hash actual local Python dependencies, including conditional imports.

    Static import traversal does not execute imported modules. External library
    versions record the dependency boundary; source hashes do not substitute
    for effective configuration hashes.
    """
    pending = {_resolve_module(cls.__module__) for cls in type(strategy).__mro__ if cls is not object}
    pending.add(_HUB / "services" / "mtf_policy.py")
    pending.discard(None)
    seen, rows, libraries = set(), [], set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        if not path.is_relative_to(_REPO):
            raise ValueError("strategy source is outside the observed repository")
        seen.add(path)
        stat = path.stat()
        digest, imports = _source_record(str(path), stat.st_mtime_ns, stat.st_size)
        rows.append({"path": path.relative_to(_REPO).as_posix(), "sha256": digest})
        for imported in imports:
            dependency = _resolve_module(imported)
            if dependency is not None:
                pending.add(dependency)
            elif imported.split(".")[0] in {"pandas", "numpy"}:
                libraries.add(imported.split(".")[0])
    versions = {}
    for package in sorted(libraries):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {"schema": "strategy-source-v1", "modules": sorted(rows, key=lambda row: row["path"]),
            "dependencies": versions, "python": ".".join(map(str, sys.version_info[:3]))}


def _settings(strategy) -> dict:
    """Observe retained constructor settings; do not serialize instance state."""
    result = {}
    namespace = vars(strategy)
    excluded = {"self", "symbol", "config", "spec", "brain", "on_block", "min_score", "params"}
    for cls in type(strategy).__mro__:
        if cls is object:
            continue
        for name, parameter in inspect.signature(cls.__init__).parameters.items():
            if name in excluded or parameter.kind in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL):
                continue
            if name in namespace:
                result[name] = namespace[name]
    for name in ("min_score", "_use_brain", "_mtf_filter", "_reversal", "pa_strategy_id", "warmup_required", "supported_regimes"):
        if hasattr(strategy, name):
            result[name] = getattr(strategy, name)
    return result


def _components(strategy) -> dict:
    result = {}
    for name in ("brain", "_regime", "regime_engine", "trend_engine", "pullback_detector", "confirmation_engine"):
        component = vars(strategy).get(name)
        if component is None:
            continue
        for config_name in ("cfg", "config"):
            config = vars(component).get(config_name)
            if config is not None and is_dataclass(config):
                result[name] = config
                break
        detector = vars(component).get("detector")
        if detector is not None and is_dataclass(vars(detector).get("cfg")):
            result[f"{name}.detector"] = detector.cfg
    if strategy.name == "smc":
        # SMC explicitly resolves this fixed default lazily on the first bar.
        from strategies.brain import BrainConfig
        result["htf_brain"] = vars(strategy).get("_cfg") or BrainConfig()
    return result


def _rule_defaults() -> dict:
    """Read literal defaults from the authoritative custom rule implementation."""
    path = _HUB / "strategies" / "custom.py"
    tree = ast.parse(path.read_bytes())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_rule")
    result = {}
    for branch in function.body:
        if not isinstance(branch, ast.If) or not isinstance(branch.test, ast.Compare):
            continue
        comparison = branch.test
        if not (isinstance(comparison.left, ast.Name) and comparison.left.id == "t"
                and len(comparison.comparators) == 1
                and isinstance(comparison.comparators[0], ast.Constant)
                and isinstance(comparison.comparators[0].value, str)):
            continue
        defaults = {}
        for node in ast.walk(branch):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("p", "rule") and len(node.args) == 2):
                try:
                    key, value = (ast.literal_eval(argument) for argument in node.args)
                except (ValueError, TypeError):
                    continue
                defaults[key] = value
        result[comparison.comparators[0].value] = defaults
    return result


def _custom_configuration(strategy) -> dict:
    spec = _json_value(strategy.spec)
    rule_defaults = _rule_defaults()
    defaults = {"side": "long", "entry": {"op": "AND", "rules": []},
                "stop": {"type": "atr", "mult": 1.5, "period": 14, "pct": 2},
                "target": {"type": "rr", "rr": 1.5, "pct": 3},
                "exit": {"op": "OR", "rules": [], "ai_exit": False},
                "quality_filter": True, "min_score": 60, "mtf_filter": True,
                "warmup": 210, "rule_defaults": rule_defaults}

    def condition(tree):
        nodes = []
        for rule in tree.get("rules") or []:
            if rule.get("rules") is not None and rule.get("type") is None:
                resolved = condition(rule)
            else:
                rule_type = rule.get("type")
                resolved = {"type": rule_type, **rule_defaults.get(rule_type, {}), **rule}
            if rule.get("negate"):
                resolved["negate"] = True
            else:
                resolved.pop("negate", None)
            nodes.append(resolved)
        return {"op": (tree.get("op") or "AND").upper(), "rules": nodes}

    stop = spec.get("stop") or {}
    target = spec.get("target") or {}
    exit_config = spec.get("exit") or {}
    # Resolve exactly the defaults used by the adapter. Research-only session
    # and risk-manager spec fields are not executed by this strategy object.
    definition = {
        "side": spec.get("side", "long"),
        "entry": condition(spec.get("entry") or {}),
        "stop": ({"type": "pct", "pct": float(stop.get("pct", 2))}
                 if (stop.get("type") or "atr") == "pct" else
                 {"type": "atr", "period": int(stop.get("period", 14)),
                  "mult": float(stop.get("mult", 1.5))}),
        "target": ({"type": "pct", "pct": float(target.get("pct", 3))}
                   if (target.get("type") or "rr") == "pct" else
                   {"type": "rr", "rr": float(target.get("rr", 1.5))}),
        "exit": {**condition({"op": exit_config.get("op", "OR"),
                              "rules": exit_config.get("rules") or []}),
                 "ai_exit": bool(exit_config.get("ai_exit"))},
    }
    return {"definition": definition, "custom_defaults": defaults}


def observed_strategy_identity(strategy, *, strategy_id: str, declared_version=None,
                               timeframe: str | None = None) -> dict:
    """Return detached JSON evidence, or explicit unknowns on capture failure.

    Unknown strategies and unsupported values are not assigned guessed defaults
    or historical fingerprints. Callers can safely retain their original trading
    path even when evidence is unavailable.
    """
    source_id = str(strategy_id or "")
    key = _ALIASES.get(source_id, source_id)
    kind = "custom" if key.startswith("custom:") else key
    result = {"strategy_id": key, "source_strategy_id": source_id,
              "strategy_version": "unversioned", "observed_version": "unversioned",
              "declared_version": str(declared_version) if declared_version is not None else None,
              "strategy_config_hash": None, "configuration": None,
              "source_hash": None, "source_manifest": None, "identity_status": "unavailable"}
    if strategy is None:
        return result
    if _CLASSES.get(kind) != (type(strategy).__module__, type(strategy).__name__):
        result["identity_status"] = "unsupported_strategy"
        return result
    try:
        from services.mtf_policy import policy_for
        from strategies.builtin_versions import builtin_strategy_version

        observed = getattr(strategy, "strategy_version", None) or builtin_strategy_version(kind)
        result["strategy_version"] = result["observed_version"] = str(observed)
        config = vars(strategy).get("config")
        if config is not None and not is_dataclass(config):
            raise ValueError("unsupported effective configuration")
        effective_config = ({field.name: getattr(config, field.name) for field in fields(config)
                             if field.name != "symbol"} if config is not None else None)
        decision_timeframe = getattr(strategy, "decision_timeframe", None)
        entry_timeframe = timeframe or decision_timeframe or (effective_config or {}).get("entry_timeframe")
        if entry_timeframe is not None:
            entry_timeframe = str(entry_timeframe).strip().lower()
            primary, secondary = policy_for(entry_timeframe)
        else:
            primary = secondary = None
        snapshot = {"schema": "effective-strategy-configuration-v1",
                    "params": strategy.params, "config": effective_config,
                    "settings": _settings(strategy), "components": _components(strategy),
                    "timeframes": {"entry_timeframe": entry_timeframe,
                                   "decision_timeframe": decision_timeframe,
                                   "primary_timeframe": primary, "secondary_timeframe": secondary,
                                   "required_timeframes": getattr(strategy, "required_timeframes", ())}}
        if kind == "custom":
            snapshot.update(_custom_configuration(strategy))
        snapshot = _json_value(snapshot)
        manifest = _source_manifest(strategy)
        result.update(configuration=snapshot, strategy_config_hash=configuration_fingerprint(snapshot),
                      source_manifest=manifest, source_hash=configuration_fingerprint(manifest))
        status = "observed" if observed != "unversioned" else "unversioned"
        if result["declared_version"] and observed != "unversioned" and result["declared_version"] != observed:
            status = "version_mismatch"
        elif decision_timeframe and entry_timeframe != decision_timeframe:
            status = "timeframe_mismatch"
        elif effective_config and effective_config.get("version") not in (None, observed):
            status = "version_mismatch"
        result["identity_status"] = status
    except (ValueError, TypeError, AttributeError, OSError, SyntaxError, RecursionError):
        # Neither bad config values nor exception messages belong in evidence.
        result.update(configuration=None, strategy_config_hash=None, source_hash=None,
                      source_manifest=None, identity_status="unavailable")
    return result
