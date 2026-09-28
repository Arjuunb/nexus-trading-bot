"""Transparent 0-100 confluence score; major stages remain hard requirements."""
from __future__ import annotations

from .models import RegimeAssessment, StageAssessment


class QualityScorer:
    def score(self, *, regime: RegimeAssessment, trend: StageAssessment,
              pullback: StageAssessment, confirmation: StageAssessment,
              rr: float, minimum_rr: float) -> tuple[float, dict[str, float]]:
        components = {
            "1h_regime_alignment": 20 * min(1, regime.confidence / 100),
            "1h_trend_alignment": 20 * min(1, trend.score / 100),
            "15m_pullback_quality": 15 * min(1, pullback.score / 100),
            "location_quality": 10 if pullback.location else 0,
            "5m_confirmation": 15 * min(1, confirmation.score / 100),
            "volume_confirmation": 10 if confirmation.volume_confirmed else 0,
            "volatility_quality": 5 if pullback.volatility_ok else 0,
            "risk_reward_quality": 5 if rr >= minimum_rr else 0,
        }
        return sum(components.values()), components
