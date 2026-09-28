"""Strategy Health monitoring (Phase 3 · strategy quality).

Evaluate a strategy statistically, not emotionally: compute rolling performance
over the most recent N trades, compare against the previous N, and raise
warnings when behaviour deteriorates (win-rate slide, profit-factor decline,
negative expectancy, accelerating drawdown, loss streaks).

Reuses the engine's metric functions (``bot.metrics``). Pure/testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from bot.metrics import expectancy as _expectancy
from bot.metrics import profit_factor as _profit_factor
from risk.daily_limits import chronological


@dataclass
class HealthConfig:
    window: int = 20
    min_sample: int = 8              # don't warn on tiny samples
    win_rate_drop: float = 0.15      # absolute drop vs previous window
    pf_floor: float = 1.0            # recent PF below this == losing
    pf_drop_ratio: float = 0.25      # PF fell >25% vs previous
    max_consecutive_losses: int = 5
    dd_accel_ratio: float = 1.5      # recent dd vs previous dd


@dataclass
class WindowStats:
    n: int
    win_rate: float
    profit_factor: float
    expectancy: float
    avg_rr: float
    max_drawdown: float
    consecutive_losses: int

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class HealthWarning:
    metric: str
    severity: str        # info | warning | critical
    detail: str


@dataclass
class StrategyHealth:
    status: str          # Healthy | Degrading | Unhealthy
    recent: WindowStats
    previous: WindowStats
    warnings: list[HealthWarning] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "recent": self.recent.to_dict(),
            "previous": self.previous.to_dict(),
            "warnings": [w.__dict__ for w in self.warnings],
        }


def entry_risk_factor(health: StrategyHealth) -> float:
    """Defensive sizing modifier derived from measured strategy deterioration.

    This is intentionally one-way: it can reduce new-entry risk but never
    increase it. A strategy keeps trading on paper at a smaller size so its
    current regime can be measured rather than silently frozen after one weak
    sample. Open positions and exits are never affected.
    """
    if health.status == "Unhealthy":
        return 0.50
    if health.status == "Degrading":
        return 0.75
    return 1.0


def _cum_drawdown(trades: Sequence[dict]) -> float:
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for t in trades:
        cum += t.get("pnl", 0.0)
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return abs(worst)


def _stats(trades: Sequence[dict]) -> WindowStats:
    n = len(trades)
    if n == 0:
        return WindowStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    pf = _profit_factor(list(trades))
    pf = 99.0 if pf == float("inf") else round(pf, 4)
    cl = 0
    for t in reversed(trades):
        if t.get("pnl", 0) < 0:
            cl += 1
        else:
            break
    return WindowStats(
        n=n,
        win_rate=round(len(wins) / n, 4),
        profit_factor=pf,
        expectancy=round(_expectancy(list(trades)), 4),
        avg_rr=round(sum(t.get("r", 0.0) for t in trades) / n, 4),
        max_drawdown=round(_cum_drawdown(trades), 4),
        consecutive_losses=cl,
    )


class StrategyHealthMonitor:
    def __init__(self, config: HealthConfig | None = None):
        self.cfg = config or HealthConfig()

    def evaluate(self, trades: Sequence[dict]) -> StrategyHealth:
        cfg = self.cfg
        w = cfg.window
        trades = chronological(trades)          # "recent" means the latest closes
        recent = list(trades[-w:])
        previous = list(trades[-2 * w:-w])
        rs, ps = _stats(recent), _stats(previous)
        warnings: list[HealthWarning] = []

        # Only judge deterioration with enough data in both windows.
        comparable = rs.n >= cfg.min_sample and ps.n >= cfg.min_sample

        if rs.n >= cfg.min_sample and rs.profit_factor < cfg.pf_floor:
            warnings.append(HealthWarning("profit_factor", "critical",
                            f"Profit factor {rs.profit_factor:.2f} below {cfg.pf_floor:.2f} — strategy is losing"))
        if rs.n >= cfg.min_sample and rs.expectancy < 0:
            warnings.append(HealthWarning("expectancy", "critical",
                            f"Negative expectancy ({rs.expectancy:.2f}) over the last {rs.n} trades"))
        if rs.consecutive_losses >= cfg.max_consecutive_losses:
            warnings.append(HealthWarning("consecutive_losses", "warning",
                            f"{rs.consecutive_losses} consecutive losses"))

        if comparable:
            if rs.win_rate < ps.win_rate - cfg.win_rate_drop:
                warnings.append(HealthWarning("win_rate", "warning",
                                f"Win rate fell {(ps.win_rate - rs.win_rate)*100:.0f}pts "
                                f"({ps.win_rate*100:.0f}% → {rs.win_rate*100:.0f}%)"))
            if ps.profit_factor > 0 and rs.profit_factor < ps.profit_factor * (1 - cfg.pf_drop_ratio):
                warnings.append(HealthWarning("profit_factor", "warning",
                                f"Profit factor declining ({ps.profit_factor:.2f} → {rs.profit_factor:.2f})"))
            if ps.max_drawdown > 0 and rs.max_drawdown > ps.max_drawdown * cfg.dd_accel_ratio:
                warnings.append(HealthWarning("drawdown", "warning",
                                f"Drawdown accelerating ({ps.max_drawdown:.0f} → {rs.max_drawdown:.0f})"))

        if any(w.severity == "critical" for w in warnings):
            status = "Unhealthy"
        elif warnings:
            status = "Degrading"
        else:
            status = "Healthy"
        return StrategyHealth(status=status, recent=rs, previous=ps, warnings=warnings)
