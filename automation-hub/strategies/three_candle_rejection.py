"""3-Candle Rejection at support and resistance, filtered by EMA 9/33.

The owner's own setup, written down as the rules they chose. BUY at support
(SELL at resistance is the mirror image):

1. Levels. Swing lows and highs -- a candle whose low (high) is beyond the
   ``pivot`` candles on either side -- confirmed before the pattern starts,
   inside the last ``level_lookback`` candles. Swing points within
   ``level_tolerance_atr_mult`` x ATR of each other form one level, and a level
   counts once price has respected it at least ``min_touches`` times.
2. Trend. EMA ``ema_fast`` (9) above EMA ``ema_slow`` (33) on the decision
   candle. Rejections against that trend are skipped.
3. Candle 1, the push: a bearish candle that opened above the level and whose
   low reached the level's zone.
4. Candle 2, the rejection: its low goes below the level, it closes back
   above it, and its low is at or beyond candle 1's.
5. Candle 3, the confirmation: closes above candle 2's high.

Entry at candle 3's close. Stop below the lower of candles 2 and 3, less
``stop_buffer_atr_mult`` x ATR. Target ``rr_target`` (2) times the risk.

Decisions use closed candles only and every input is recomputed from
``self.bars`` on each call, because the engine's warm-up loads history
straight into ``self.bars`` without calling ``on_bar``. Nothing here has a
proven edge: it is written to be run on paper and measured.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from bot.data.indicators import atr, ema
from bot.types import Bar, Signal, SignalType
from strategies.base_strategy import HubStrategy


@dataclass(frozen=True)
class Level:
    """A price respected by at least ``min_touches`` confirmed swing points."""
    id: str
    price: float
    lower: float
    upper: float
    touches: int
    first_touch: object
    last_touch: object


@dataclass(frozen=True)
class RejectionEvent:
    """A completed push-reject-confirm pattern that produced a signal."""
    id: str
    direction: str            # "long" at support, "short" at resistance
    level: float
    occurred_at: object       # candle 2, the rejection
    confirmed_at: object      # candle 3, the confirmation
    entry: float
    stop: float
    target: float


class ThreeCandleRejectionStrategy(HubStrategy):
    name = "three_candle_rejection"
    label = "3-Candle Rejection · EMA 9/33"
    supported_regimes = ()
    #: level_lookback (150) plus the pattern and pivot confirmation, with room
    #: for EMA 33 to settle. The engine warms up to at least this many.
    warmup_required = 200

    def __init__(self, symbol: str, *, ema_fast: int = 9, ema_slow: int = 33,
                 pivot: int = 3, level_lookback: int = 150, min_touches: int = 2,
                 level_tolerance_atr_mult: float = 0.25,
                 stop_buffer_atr_mult: float = 0.1,
                 rr_target: float = 2.0, atr_period: int = 14,
                 max_history: int = 600, **params):
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be shorter than ema_slow")
        super().__init__(symbol, ema_fast=ema_fast, ema_slow=ema_slow, pivot=pivot,
                         level_lookback=level_lookback, min_touches=min_touches,
                         level_tolerance_atr_mult=level_tolerance_atr_mult,
                         stop_buffer_atr_mult=stop_buffer_atr_mult,
                         rr_target=rr_target, atr_period=atr_period, **params)
        self.max_history = max_history
        self.levels: list[Level] = []
        self.events: deque[RejectionEvent] = deque(maxlen=50)
        self.last_reason = "Waiting for the first closed candle."
        self._report: dict = {"decision": "WAIT", "reason": self.last_reason,
                              "blocker_code": "WARMUP", "direction": None}

    # ------------------------------------------------------------- levels
    def _swing_points(self, bars: list[Bar], end: int) -> list[tuple[float, object]]:
        """Confirmed swing highs and lows among ``bars[:end]``.

        A swing at index i needs ``pivot`` candles after it, all of which must
        also lie before ``end`` (candle 1), so no level uses the pattern itself.
        """
        k = int(self.params["pivot"])
        start = max(k, end - int(self.params["level_lookback"]))
        points: list[tuple[float, object]] = []
        for i in range(start, end - k):
            here = bars[i]
            if here.high > max(b.high for b in bars[i - k:i]) and \
                    here.high >= max(b.high for b in bars[i + 1:i + k + 1]):
                points.append((here.high, here.timestamp))
            if here.low < min(b.low for b in bars[i - k:i]) and \
                    here.low <= min(b.low for b in bars[i + 1:i + k + 1]):
                points.append((here.low, here.timestamp))
        return points

    def _levels(self, bars: list[Bar], end: int, atr_value: float) -> list[Level]:
        tolerance = float(self.params["level_tolerance_atr_mult"]) * atr_value
        points = sorted(self._swing_points(bars, end), key=lambda row: row[0])
        groups: list[list[tuple[float, object]]] = []
        for point in points:
            if groups and point[0] - groups[-1][0][0] <= tolerance:
                groups[-1].append(point)
            else:
                groups.append([point])
        levels = []
        for group in groups:
            if len(group) < int(self.params["min_touches"]):
                continue
            prices = [price for price, _ in group]
            times = sorted(time for _, time in group)
            levels.append(Level(
                id=f"level-{times[0].isoformat()}",
                price=sum(prices) / len(prices), lower=min(prices), upper=max(prices),
                touches=len(group), first_touch=times[0], last_touch=times[-1]))
        return levels

    # ------------------------------------------------------------- pattern
    def _evaluate(self, direction: SignalType, c1: Bar, c2: Bar, c3: Bar,
                  trend_up: bool) -> tuple[int, str, Optional[Level], str]:
        """How far one direction got: (stage reached, blocker, level, reason).

        Stages: 0 at a level, 1 rejection, 2 confirmation, 3 EMA trend filter,
        4 complete. The trend filter is checked last so that a pattern which
        formed against the trend is reported as filtered, not as absent; the
        signals are the same either way.
        """
        long = direction == SignalType.LONG
        side = "support" if long else "resistance"
        if long:
            reached = [lv for lv in self.levels if c1.open > lv.price and c1.low <= lv.upper]
        else:
            reached = [lv for lv in self.levels if c1.open < lv.price and c1.high >= lv.lower]
        if not reached:
            return 0, "NO_ELIGIBLE_ZONE", None, f"No {side} level within reach of the last candles"
        if long:
            rejected = [lv for lv in reached
                        if c1.close < c1.open and c2.low < lv.price < c2.close
                        and c2.low <= c1.low]
        else:
            rejected = [lv for lv in reached
                        if c1.close > c1.open and c2.close < lv.price < c2.high
                        and c2.high >= c1.high]
        if not rejected:
            return 1, "NO_SUPPORT_REJECTION" if long else "NO_RESISTANCE_REJECTION", None, (
                f"Price reached {side} but the push and rejection candles did not form")
        level = max(rejected, key=lambda lv: (lv.touches, -abs(lv.price - c2.close)))
        confirmed = c3.close > c2.high if long else c3.close < c2.low
        if not confirmed:
            return 2, "NO_CONFIRMATION", level, (
                f"Rejection at {side} {level.price:.8g}; candle 3 did not close "
                f"{'above' if long else 'below'} the rejection candle")
        if trend_up != long:
            return 3, "EMA_TREND_NOT_ALIGNED", level, (
                f"3-candle rejection at {side} {level.price:.8g}, but EMA "
                f"{self.params['ema_fast']} is {'below' if long else 'above'} EMA "
                f"{self.params['ema_slow']}; filtered")
        return 4, "", level, f"3-candle rejection at {side} {level.price:.8g} ({level.touches} touches)"

    def generate(self, bar: Bar) -> Optional[Signal]:
        if len(self.bars) > self.max_history:
            del self.bars[:-self.max_history]
        bars = self.bars
        if len(bars) < self.warmup_required:
            self.levels = []
            return self._wait("WARMUP", f"Warming up: {len(bars)}/{self.warmup_required} closed candles")

        atr_value = atr(bars, int(self.params["atr_period"]))
        if atr_value <= 0:
            return self._wait("WARMUP", "ATR is not formed yet")
        closes = [b.close for b in bars]
        fast = ema(closes, int(self.params["ema_fast"]))[-1]
        slow = ema(closes, int(self.params["ema_slow"]))[-1]
        trend_up = fast > slow
        c1, c2, c3 = bars[-3], bars[-2], bars[-1]
        self.levels = self._levels(bars, len(bars) - 3, atr_value)

        results = {d: self._evaluate(d, c1, c2, c3, trend_up)
                   for d in (SignalType.LONG, SignalType.SHORT)}
        direction, (stage, blocker, level, reason) = max(
            results.items(), key=lambda row: row[1][0])
        if stage < 4 or level is None:
            return self._wait(blocker, reason, direction)

        buffer = float(self.params["stop_buffer_atr_mult"]) * atr_value
        entry = c3.close
        if direction == SignalType.LONG:
            stop = min(c2.low, c3.low) - buffer
            risk = entry - stop
            target = entry + float(self.params["rr_target"]) * risk
        else:
            stop = max(c2.high, c3.high) + buffer
            risk = stop - entry
            target = entry - float(self.params["rr_target"]) * risk
        if risk <= 0:
            return self._wait("NO_CONFIRMATION", "The stop would sit on the wrong side of entry", direction)

        trend = (f"EMA{self.params['ema_fast']} {'>' if trend_up else '<'} "
                 f"EMA{self.params['ema_slow']}")
        text = f"{reason} · {trend} · stop beyond the wick · {self.params['rr_target']}R target"
        self.events.append(RejectionEvent(
            id=f"{direction.value}-{c3.timestamp.isoformat()}", direction=direction.value,
            level=level.price, occurred_at=c2.timestamp, confirmed_at=c3.timestamp,
            entry=entry, stop=stop, target=target))
        self.last_reason = text
        self._report = {"decision": "ENTER", "reason": text, "blocker_code": None,
                        "direction": direction.value, "level": level.price,
                        "level_touches": level.touches}
        return Signal(timestamp=c3.timestamp, symbol=self.symbol, type=direction,
                      entry=entry, stop_loss=stop, take_profit=target, reason=text)

    # ------------------------------------------------------------- report
    def _wait(self, code: str, reason: str, direction: Optional[SignalType] = None) -> None:
        self.last_reason = reason
        self._report = {"decision": "WAIT", "reason": reason, "blocker_code": code,
                        "direction": direction.value if direction else None}
        return None

    def decision_report(self) -> dict:
        """Why the last closed candle did or did not produce a signal.

        Both directions are evaluated; the one that got furthest through the
        rules is reported, with the first rule it failed as the blocker."""
        return dict(self._report)
