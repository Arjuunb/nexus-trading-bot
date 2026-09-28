"""Typed, JSON-safe decision models for the adaptive strategy."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNCERTAIN = "UNCERTAIN"


class SetupState(str, Enum):
    SCANNING = "SCANNING"
    SETUP_FOUND = "SETUP_FOUND"
    WAITING_FOR_PULLBACK = "WAITING_FOR_PULLBACK"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    ORDER_PENDING = "ORDER_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    MANAGING_POSITION = "MANAGING_POSITION"
    POSITION_CLOSED = "POSITION_CLOSED"
    COOLDOWN = "COOLDOWN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class StageAssessment:
    valid: bool
    score: float
    label: str
    reasons: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    swing_low: Optional[float] = None
    swing_high: Optional[float] = None
    location: Optional[str] = None
    volume_confirmed: bool = False
    volatility_ok: bool = True

    def public(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RegimeAssessment:
    regime: MarketRegime
    confidence: float
    bull_score: float
    bear_score: float
    adx: float
    atr_pct: float
    reasons: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def public(self) -> dict:
        row = asdict(self)
        row["regime"] = self.regime.value
        return row


@dataclass
class StrategyDecision:
    state: SetupState
    decision: str
    direction: Optional[str]
    reason: str
    quality_score: float = 0.0
    regime: Optional[RegimeAssessment] = None
    trend: Optional[StageAssessment] = None
    pullback: Optional[StageAssessment] = None
    confirmation: Optional[StageAssessment] = None
    components: dict[str, float] = field(default_factory=dict)
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    rr: Optional[float] = None
    blocker_code: Optional[str] = None

    def public(self) -> dict:
        return {
            "state": self.state.value, "decision": self.decision,
            "direction": self.direction, "reason": self.reason,
            "quality_score": round(self.quality_score, 2),
            "regime": self.regime.public() if self.regime else None,
            "trend": self.trend.public() if self.trend else None,
            "pullback": self.pullback.public() if self.pullback else None,
            "confirmation": self.confirmation.public() if self.confirmation else None,
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "rr": self.rr, "blocker_code": self.blocker_code,
        }
