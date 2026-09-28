"""Daily-loss and consecutive-loss protection.

The engine's RiskManager already enforces the daily-loss kill switch and the
post-loss cooldown during runs; these helpers expose the same checks to the
live supervisor and the Risk Center UI.
"""
from __future__ import annotations

from typing import Sequence


def daily_loss_used(pnl_today: float, equity: float, max_daily_loss_pct: float) -> float:
    """Fraction (0..1+) of the daily-loss budget consumed."""
    limit = max_daily_loss_pct * equity
    if limit <= 0:
        return 0.0
    return max(0.0, -pnl_today) / limit


def daily_limit_hit(pnl_today: float, equity: float, max_daily_loss_pct: float) -> bool:
    return daily_loss_used(pnl_today, equity, max_daily_loss_pct) >= 1.0


def chronological(trades: Sequence[dict]) -> list[dict]:
    """Closed trades, oldest close first.

    A streak or a "recent" window reads the end of the list as the latest
    trade. The paper ledger returns its history newest first, and callers
    passed it straight in: the latest trades were read as the oldest, so a
    run of fresh losses showed no streak while an old one never ended. When
    every trade carries its close time, order by it; a list without close
    times is taken to be in order already.
    """
    items = list(trades)
    if items and all(t.get("closed_at") for t in items):
        return sorted(items, key=lambda t: str(t["closed_at"]))
    return items


def consecutive_losses(trades: Sequence[dict]) -> int:
    """Losses in a row up to the latest closed trade."""
    streak = 0
    for t in reversed(chronological(trades)):
        if float(t.get("pnl") or 0) < 0:
            streak += 1
        else:
            break
    return streak


def consecutive_loss_limit_hit(trades: Sequence[dict], max_streak: int) -> bool:
    return consecutive_losses(trades) >= max_streak
