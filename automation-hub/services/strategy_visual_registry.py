"""What each Trading Instance strategy shows in the Instance Visual Lab.

One Visual Lab serves every strategy, so the per-strategy knowledge has to live
somewhere. It lives here, as declarations, and deliberately not as a second
implementation of the strategies themselves:

    Trading Instance -> Strategy Engine -> Decision Evidence
                                             -> Visual Adapter -> Visual Lab

An adapter says which features a strategy consumes, which ordered gates it
passes through, and which of its own blocker codes means each gate failed. It
computes no indicator, reads no candle and decides nothing. The gate states are
derived from the blocker the runtime already published: everything before the
failing gate passed, that one failed, everything after is still waiting. That
is why the Lab cannot drift from the strategy -- it has no opinion of its own
to drift with.

Two rules this module exists to enforce:

* A strategy shows only the features it actually consumes. Donchian does not
  get a CHoCH overlay because the renderer can draw one. ``test_strategy_visual
  _registry.py`` checks each declaration against the real module's symbols, so
  a claim here that the implementation does not support fails the build.
* A strategy's identity resolves the same way everywhere. ``adapter_for``
  resolves through services/strategy_registry, the same catalog the instance
  runtime and the dashboard use, so the Lab cannot label a decision with one
  strategy while another produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Optional


class Feature(str, Enum):
    """Everything the chart is allowed to draw, as a closed vocabulary.

    Closed on purpose: an open one invites "the renderer supports it, so show
    it", which is how a chart ends up implying a strategy consults something it
    has never read.
    """
    SUPPORT = "support"
    RESISTANCE = "resistance"
    SUPPLY = "supply"
    DEMAND = "demand"
    SWING = "swing_high_low"
    LIQUIDITY = "liquidity"
    LIQUIDITY_SWEEP = "liquidity_sweep"
    BOS = "bos"
    CHOCH = "choch"
    FVG = "fvg"
    EMA = "ema"
    TREND_STRUCTURE = "trend_structure"
    POI = "poi"
    REJECTION_CANDLE = "rejection_candle"
    DOMINANT_CANDLE = "dominant_candle"
    DONCHIAN_CHANNEL = "donchian_channel"
    SUPERTREND = "supertrend"
    RSI = "rsi"
    ADX = "adx"
    ATR_BAND = "atr_band"
    REGIME = "regime"
    HTF_BIAS = "htf_bias"
    ZONE_FLIP = "zone_flip"
    OPPOSING_ZONE_TARGET = "opposing_zone_target"
    VOLUME = "volume"


class Stage(str, Enum):
    """The decision pipeline, in the order a bar travels through it."""
    MARKET_DATA = "MARKET_DATA"
    FEATURES = "FEATURES"
    CONTEXT = "CONTEXT"
    SETUP = "SETUP"
    CONFIRMATION = "CONFIRMATION"
    STRATEGY_ACCEPT = "STRATEGY_ACCEPT"
    RISK_CHECK = "RISK_CHECK"
    ORDER_INTENT = "ORDER_INTENT"
    PAPER_BROKER = "PAPER_BROKER"
    FILL = "FILL"
    POSITION = "POSITION"
    EXIT = "EXIT"


PIPELINE: tuple[Stage, ...] = tuple(Stage)


class DecisionState(str, Enum):
    """What the instance is doing, in the operator's words.

    "ACTIVE" is deliberately absent. An instance whose feed is stale is not
    active, and a badge that says so while nothing can trade is the failure
    this whole page exists to prevent.
    """
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    WAITING_FOR_HTF = "WAITING_FOR_HTF"
    DATA_BLOCKED = "DATA_BLOCKED"
    SCANNING = "SCANNING"
    WAITING_FOR_POI = "WAITING_FOR_POI"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    SIGNAL_ACCEPTED = "SIGNAL_ACCEPTED"
    RISK_CHECK = "RISK_CHECK"
    RISK_BLOCKED = "RISK_BLOCKED"
    ORDER_INTENT = "ORDER_INTENT"
    ORDER_PENDING = "ORDER_PENDING"
    FILLED = "FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    EXITED = "EXITED"
    SIGNALS_ONLY = "SIGNALS_ONLY"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class GateState(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAITING = "WAITING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class Gate:
    """One requirement, and the blocker codes that mean it is what stopped us.

    ``blockers`` is how a gate is evaluated. The runtime already published a
    code; the Lab does not re-derive the condition, it locates the code in the
    sequence. A gate with no codes is informational and never fails on its own.
    """
    id: str
    stage: Stage
    label: str
    blockers: frozenset[str] = frozenset()
    detail: str = ""


@dataclass(frozen=True)
class VisualAdapter:
    strategy_id: str
    module: str
    #: Where the features really live. Several strategies are thin adapters
    #: over an engine -- smc_strategy computes nothing itself, native_smc does
    #: -- and a declaration has to be checkable against the code that actually
    #: derives the feature, not the file that forwards to it.
    engine_modules: tuple[str, ...] = ()
    features: tuple[Feature, ...] = ()
    setup_gates: tuple[Gate, ...] = ()
    entry_trigger: str = ""
    invalidation: str = ""
    stop_model: str = ""
    target_model: str = ""
    risk_requirements: str = "Instance risk engine: per-trade risk %, open-position and loss limits."
    notes: str = ""

    @property
    def overlays(self) -> tuple[str, ...]:
        return tuple(feature.value for feature in self.features)


# --------------------------------------------------------------------- gates
#
# These belong to the runtime rather than to any strategy: every instance is
# fed by the same market-data path, sized by the same risk engine and executed
# by the same paper broker. Declaring them once is what lets one page serve
# every strategy without each adapter restating the shared pipeline.

MARKET_DATA_GATES: tuple[Gate, ...] = (
    Gate("feed_synchronized", Stage.MARKET_DATA, "Feed synchronized",
         frozenset({"FEED_NOT_SYNCHRONIZED", "STALE_MARKET_DATA", "STALE_CANDLE",
                    "STALE_CANDLES", "NON_REAL_DATA", "MARKET_QUALITY"}),
         "The instance's own feed reports a synchronized, real provider."),
    Gate("closed_candle", Stage.MARKET_DATA, "Closed candle available",
         frozenset({"WARMUP", "WARMING_UP", "MISSING_CANDLE", "NO_CANDLES"}),
         "Enough closed candles for the strategy's declared warm-up."),
    Gate("htf_available", Stage.MARKET_DATA, "Higher timeframe available",
         frozenset({"HTF_NOT_READY", "HTF_UNAVAILABLE"}),
         "The higher timeframe this strategy requires has closed a candle."),
    Gate("htf_fresh", Stage.MARKET_DATA, "Higher timeframe fresh",
         frozenset({"STALE_HTF_CANDLE", "STALE_HTF"}),
         "That higher-timeframe candle is current, not a stale cached one."),
)

RISK_GATES: tuple[Gate, ...] = (
    Gate("risk_sizing", Stage.RISK_CHECK, "Position sizeable within risk",
         frozenset({"INVALID_RISK", "RISK_LIMIT", "RR_TOO_LOW", "INSUFFICIENT_RR"}),
         "The risk engine can size this entry inside the instance's risk budget."),
    Gate("exposure_limits", Stage.RISK_CHECK, "Exposure limits clear",
         frozenset({"MAX_POSITION_LIMIT", "TRADE_LIMIT", "CORRELATED_EXPOSURE",
                    "PORTFOLIO_EXPOSURE", "EXISTING_EXPOSURE",
                    "POSITION_ALREADY_ALIGNED", "POSITION_MANAGED"}),
         "Open positions, correlation and portfolio caps all allow another."),
    Gate("loss_limits", Stage.RISK_CHECK, "Loss limits and cooldown clear",
         frozenset({"DAILY_LOSS_LIMIT", "WEEKLY_LOSS_LIMIT", "LOSS_COOLDOWN",
                    "COOLDOWN_ACTIVE"}),
         "No daily/weekly loss limit or cooldown is currently holding entries."),
)

EXECUTION_GATES: tuple[Gate, ...] = (
    Gate("execution_mode", Stage.ORDER_INTENT, "Execution permitted",
         frozenset({"PAUSED", "SIGNALS_ONLY", "APPROVAL_REQUIRED",
                    "OUTSIDE_SESSION", "TRADING_DAY_DISABLED", "EVENT_BLACKOUT"}),
         "The instance is armed to create orders, not paused or signals-only."),
    Gate("idempotent", Stage.ORDER_INTENT, "Not a duplicate of a live decision",
         frozenset({"DUPLICATE_SIGNAL", "ORDER_PENDING", "LIMIT_EXPIRED"}),
         "This candle's decision has not already produced an order."),
    Gate("broker_accepts", Stage.PAPER_BROKER, "Paper broker accepts the order",
         frozenset({"EXECUTION", "PIPELINE_ERROR"}),
         "The isolated paper broker accepted the order intent."),
)


#: Machine code -> what it means in a sentence. The Lab shows both; a code
#: alone is unreadable and a sentence alone is unsearchable.
BLOCKER_EXPLANATIONS: Mapping[str, str] = {
    # market data
    "FEED_NOT_SYNCHRONIZED": "The market-data feed is not synchronized, so no decision may be made on it.",
    "STALE_MARKET_DATA": "The newest closed candle is older than this instance tolerates.",
    "STALE_CANDLE": "The newest closed candle is older than this instance tolerates.",
    "STALE_CANDLES": "The newest closed candle is older than this instance tolerates.",
    "NON_REAL_DATA": "The candles offered were not from a validated real provider, so they may not reach a decision.",
    "MARKET_QUALITY": "The market-quality gate rejected this candle, usually spread or liquidity.",
    "WARMUP": "Fewer closed candles than the strategy's declared warm-up requires.",
    "WARMING_UP": "Fewer closed candles than the strategy's declared warm-up requires.",
    "MISSING_CANDLE": "A gap in the closed-candle series; the strategy will not decide across one.",
    "NO_CANDLES": "No cached closed candles for this symbol and timeframe.",
    "HTF_NOT_READY": "The higher timeframe has not closed a candle the strategy has not already used.",
    "HTF_UNAVAILABLE": "The higher timeframe this strategy requires is not available.",
    "STALE_HTF_CANDLE": "The higher-timeframe candle is too old to be treated as current.",
    "STALE_HTF": "The higher-timeframe candle is too old to be treated as current.",
    # strategy
    "NO_SETUP": "The strategy evaluated the candle and found no setup. It did not say more than that.",
    "NO_ELIGIBLE_ZONE": "No zone was in reach of price on this candle.",
    "OUTSIDE_POI": "Price is not inside a point of interest this strategy trades from.",
    "NO_SUPPORT_REJECTION": "Price reached support but no candle met the rejection test.",
    "NO_RESISTANCE_REJECTION": "Price reached resistance but no candle met the rejection test.",
    "REJECTION_FAILED": "Price reached the zone but the candle did not meet the rejection test.",
    # The native Price Action engine's own condition keys, mapped in
    # strategies/price_action_rejection.py::_BLOCKER_BY_CONDITION.
    "TREND_NOT_ALIGNED": "The confirmed HH/HL or LH/LL structure does not point this way.",
    "NO_ROLE_FLIP": "The zone has not broken and flipped role, which this setup requires.",
    "RETEST_NOT_HELD": "The flipped zone was retested and did not hold on a closed candle.",
    "NO_FALSE_BREAK": "No breakout failed and reclaimed inside the configured window.",
    "NO_REVERSAL_CLOSE": "The reversal is not confirmed by a closing price.",
    "PIN_BAR_REQUIRED": "This isolated experiment accepts only a directional pin bar as the rejection.",
    "NOT_FIRST_TOUCH": "This experiment accepts only the first distinct visit to the zone.",
    "NO_LIQUIDITY_SWEEP": "No liquidity sweep preceded this candle.",
    "NO_BOS": "No break of structure has confirmed the direction.",
    "NO_CHOCH": "No change of character has confirmed a reversal.",
    "NO_FVG": "No fair value gap is available to trade back into.",
    "NO_DOMINANT_CANDLE": "No dominant candle confirmed the move.",
    "NO_VOLUME_CONFIRMATION": "Volume did not confirm the candle.",
    "REGIME_NOT_ALIGNED": "The higher-timeframe regime does not permit this direction.",
    "CONFIRMATION_EXPIRED": "The setup was never confirmed inside its confirmation window.",
    "ZONE_CONSUMED": "This zone has already produced its setup and may not produce another.",
    "TARGET_UNAVAILABLE": "No opposing level to aim at, so no reward can be measured.",
    "STOP_DISTANCE_INVALID": "The structural stop falls outside the strategy's ATR band.",
    "NET_RR_TOO_LOW": "The plan is complete and does not pay after costs.",
    "INSUFFICIENT_RR": "Reward-to-risk is below the configured minimum.",
    "RR_TOO_LOW": "Reward-to-risk is below the configured minimum.",
    # risk and execution
    "INVALID_RISK": "The risk engine could not size this entry.",
    "RISK_BLOCKED": "A risk gate refused the entry.",
    "RISK_LIMIT": "A risk limit refused the entry.",
    "MAX_POSITION_LIMIT": "The instance already holds its maximum open positions.",
    "TRADE_LIMIT": "The instance has hit its trade count limit for the period.",
    "CORRELATED_EXPOSURE": "An open position in a correlated symbol blocks this one.",
    "PORTFOLIO_EXPOSURE": "Portfolio-level exposure is already at its cap.",
    "EXISTING_EXPOSURE": "A position is already open for this symbol.",
    "POSITION_ALREADY_ALIGNED": "A position in this direction is already open.",
    "POSITION_MANAGED": "The engine is managing an open position rather than scanning for entries.",
    "DAILY_LOSS_LIMIT": "The daily loss limit has paused new entries.",
    "WEEKLY_LOSS_LIMIT": "The weekly loss limit has paused new entries.",
    "LOSS_COOLDOWN": "A cooldown after a loss is still holding entries.",
    "COOLDOWN_ACTIVE": "A cooldown is still holding entries.",
    "OUTSIDE_SESSION": "Outside the instance's configured trading session.",
    "TRADING_DAY_DISABLED": "Trading is disabled for this day.",
    "EVENT_BLACKOUT": "An event blackout window is in force.",
    "PAUSED": "The instance is paused; it evaluates candles but creates no orders.",
    "SIGNALS_ONLY": "The instance is in signals-only mode and will never create an order.",
    "APPROVAL_REQUIRED": "The signal is waiting for manual approval.",
    "DUPLICATE_SIGNAL": "This candle's decision already produced an order.",
    "ORDER_PENDING": "An order from an earlier decision is still pending.",
    "LIMIT_EXPIRED": "The limit order expired before it filled.",
    "EXECUTION": "The paper broker refused the order.",
    "PIPELINE_ERROR": "The decision pipeline raised an error on this candle.",
    "NO_OPEN_POSITION": "There is no open position for this management decision.",
}


def explain(blocker: Optional[str]) -> str:
    """A sentence for a machine code, never an invented one."""
    if not blocker:
        return ""
    code = str(blocker).replace("GATE_REJECTED:", "").strip().upper()
    return BLOCKER_EXPLANATIONS.get(
        code, f"{code}: no explanation is registered for this code.")


def normalise_blocker(blocker: Optional[str]) -> str:
    """Strip the runtime's "GATE_REJECTED: " prefix to the bare code."""
    if not blocker:
        return ""
    return str(blocker).replace("GATE_REJECTED:", "").strip().upper()


# ------------------------------------------------------------------ adapters
#
# One entry per strategy the instance factory can construct. ``features`` is
# checked against the module's own symbols by the test suite, so these are
# claims the implementation has to back up.

def _g(gate_id: str, stage: Stage, label: str, blockers: Iterable[str],
       detail: str = "") -> Gate:
    return Gate(gate_id, stage, label, frozenset(blockers), detail)


ADAPTERS: dict[str, VisualAdapter] = {
    "donchian": VisualAdapter(
        strategy_id="donchian",
        module="strategies.donchian_strategy",
        features=(Feature.DONCHIAN_CHANNEL, Feature.ATR_BAND),
        setup_gates=(
            _g("channel_ready", Stage.FEATURES, "30-bar channel formed",
               ("WARMUP", "WARMING_UP"),
               "Needs channel + 2 closed bars before the current one."),
            _g("closing_break", Stage.SETUP, "Close beyond the 30-bar extreme",
               ("NO_SETUP",),
               "A close above the prior 30 bars' high, or below their low. "
               "A wick through the level is not enough."),
            _g("direction_flip", Stage.CONFIRMATION, "Direction has flipped since the last signal",
               (),
               "One signal per direction: a long cannot follow a long."),
        ),
        entry_trigger="Close beyond the highest high or lowest low of the previous 30 bars.",
        invalidation="ATR stop only. There is no structural invalidation and no trailing exit.",
        stop_model="1.5 x ATR(14) from the entry close.",
        target_model="Fixed 2.5R from the ATR stop distance. Gross, before fees.",
        notes=("The docstring calls this Turtle trading, but the Turtles exited on the "
               "opposite channel. This takes a fixed target and has no trailing exit. "
               "Its cited walk-forward evidence is 4h; check the instance timeframe."),
    ),
    "supertrend": VisualAdapter(
        strategy_id="supertrend",
        module="strategies.supertrend_strategy",
        features=(Feature.SUPERTREND, Feature.ATR_BAND),
        setup_gates=(
            _g("band_ready", Stage.FEATURES, "Supertrend band formed",
               ("WARMUP", "WARMING_UP")),
            _g("flip", Stage.SETUP, "Supertrend direction flipped", ("NO_SETUP",),
               "Entry is the bar on which the band changes side."),
        ),
        entry_trigger="The bar on which the Supertrend band flips direction.",
        invalidation="ATR stop only.",
        stop_model="ATR multiple from the entry close.",
        target_model="Fixed R multiple of the ATR stop distance.",
    ),
    "ema": VisualAdapter(
        strategy_id="ema",
        module="strategies.ema_strategy",
        features=(Feature.EMA, Feature.ATR_BAND),
        setup_gates=(
            _g("emas_ready", Stage.FEATURES, "Both EMAs formed", ("WARMUP", "WARMING_UP")),
            _g("cross", Stage.SETUP, "Fast EMA crossed the slow EMA", ("NO_SETUP",)),
        ),
        entry_trigger="Fast EMA crossing the slow EMA on a closed bar.",
        invalidation="ATR stop only.",
        stop_model="ATR multiple from the entry close.",
        target_model="Fixed R multiple of the ATR stop distance.",
    ),
    "ensemble": VisualAdapter(
        strategy_id="ensemble",
        module="strategies.ensemble_strategy",
        features=(Feature.EMA, Feature.SUPERTREND, Feature.DONCHIAN_CHANNEL,
                  Feature.ATR_BAND),
        setup_gates=(
            _g("members_ready", Stage.FEATURES, "All three members formed",
               ("WARMUP", "WARMING_UP")),
            _g("agreement", Stage.SETUP, "Two of three members agree", ("NO_SETUP",),
               "EMA, Supertrend and Donchian vote; two must agree."),
        ),
        entry_trigger="Two of EMA, Supertrend and Donchian signalling the same direction.",
        invalidation="ATR stop only.",
        stop_model="ATR multiple from the entry close.",
        target_model="Fixed R multiple of the ATR stop distance.",
    ),
    "liquidity_sweep": VisualAdapter(
        strategy_id="liquidity_sweep",
        module="strategies.liquidity_sweep_strategy",
        # Deliberately no SWING: it sweeps a rolling window extreme, not a
        # confirmed pivot, and its own docstring is explicit that it does not
        # invent CHoCH or FVG confirmation.
        features=(Feature.LIQUIDITY, Feature.LIQUIDITY_SWEEP, Feature.ATR_BAND),
        setup_gates=(
            _g("range_ready", Stage.FEATURES, "Sweep lookback range formed",
               ("WARMUP", "WARMING_UP")),
            _g("sweep", Stage.SETUP, "Wick swept a prior range extreme",
               ("NO_SETUP", "NO_LIQUIDITY_SWEEP")),
            _g("reclaim", Stage.CONFIRMATION, "Candle reclaimed the swept level", ()),
        ),
        entry_trigger="A wick beyond a prior range extreme, reclaimed by the candle's close.",
        invalidation="ATR-defined invalidation beyond the sweep extreme.",
        stop_model="ATR multiple beyond the sweep wick.",
        target_model="Fixed R multiple of the stop distance.",
    ),
    "brain": VisualAdapter(
        strategy_id="brain",
        module="strategies.brain_strategy",
        engine_modules=("strategies.brain",),
        features=(Feature.EMA, Feature.RSI, Feature.TREND_STRUCTURE,
                  Feature.REGIME, Feature.HTF_BIAS, Feature.ATR_BAND),
        setup_gates=(
            _g("indicators_ready", Stage.FEATURES, "Trend, momentum and RSI formed",
               ("WARMUP", "WARMING_UP")),
            _g("htf_bias", Stage.CONTEXT, "Higher-timeframe bias agrees",
               ("HTF_NOT_READY", "STALE_HTF_CANDLE")),
            _g("regime", Stage.CONTEXT, "Regime permits this direction",
               ("REGIME_NOT_ALIGNED",)),
            _g("factors", Stage.SETUP, "Multi-factor score clears its threshold",
               ("NO_SETUP",)),
        ),
        entry_trigger="A multi-factor score over EMA trend, momentum, RSI and regime.",
        invalidation="ATR stop only.",
        stop_model="ATR multiple from the entry close.",
        target_model="Fixed R multiple of the ATR stop distance.",
    ),
    "adaptive_trend_pullback": VisualAdapter(
        strategy_id="adaptive_trend_pullback",
        module="strategies.adaptive_trend_pullback",
        features=(Feature.EMA, Feature.ADX, Feature.TREND_STRUCTURE,
                  Feature.SWING, Feature.HTF_BIAS, Feature.ATR_BAND),
        setup_gates=(
            _g("indicators_ready", Stage.FEATURES, "EMA and ADX formed",
               ("WARMUP", "WARMING_UP")),
            _g("htf_bias", Stage.CONTEXT, "4H bias established",
               ("HTF_NOT_READY", "STALE_HTF_CANDLE")),
            _g("regime_gate", Stage.CONTEXT, "1H regime permits this direction",
               ("REGIME_NOT_ALIGNED",)),
            _g("pullback", Stage.SETUP, "Pullback into trend support", ("NO_SETUP",)),
            _g("resume", Stage.CONFIRMATION, "Trend resumption confirmed", ()),
        ),
        entry_trigger="A pullback inside an established trend, resuming on a closed bar.",
        invalidation="ATR stop beyond the pullback extreme.",
        stop_model="ATR multiple beyond the pullback swing.",
        target_model="Fixed R multiple of the stop distance.",
    ),
    "price_action_rejection": VisualAdapter(
        strategy_id="price_action_rejection",
        module="strategies.price_action_rejection",
        engine_modules=("services.native_price_action",),
        features=(Feature.SUPPORT, Feature.RESISTANCE, Feature.SWING,
                  Feature.POI, Feature.REJECTION_CANDLE, Feature.HTF_BIAS),
        setup_gates=(
            _g("zones_ready", Stage.FEATURES, "Confirmed S/R zones available",
               ("WARMUP", "WARMING_UP")),
            _g("htf_bias", Stage.CONTEXT, "Higher-timeframe bias agrees",
               ("HTF_NOT_READY", "STALE_HTF_CANDLE")),
            _g("trend", Stage.CONTEXT, "Confirmed structure points this way",
               ("TREND_NOT_ALIGNED",)),
            _g("at_zone", Stage.SETUP, "Price reached a confirmed zone",
               ("NO_SETUP", "OUTSIDE_POI", "NO_ELIGIBLE_ZONE", "NOT_FIRST_TOUCH")),
            _g("rejection", Stage.CONFIRMATION, "Closed-candle rejection at the zone",
               ("REJECTION_FAILED", "NO_SUPPORT_REJECTION", "NO_RESISTANCE_REJECTION",
                "PIN_BAR_REQUIRED")),
        ),
        entry_trigger="A closed-candle rejection at a confirmed support or resistance zone.",
        invalidation="Beyond the rejection candle's extreme, or the zone being consumed.",
        stop_model="Rejection extreme plus a buffer.",
        target_model="Configured reward-to-risk multiple.",
    ),
    "price_action_flip_retest": VisualAdapter(
        strategy_id="price_action_flip_retest",
        module="strategies.price_action_rejection",
        engine_modules=("services.native_price_action",),
        features=(Feature.SUPPORT, Feature.RESISTANCE, Feature.SWING,
                  Feature.ZONE_FLIP, Feature.POI, Feature.REJECTION_CANDLE,
                  Feature.HTF_BIAS),
        setup_gates=(
            _g("zones_ready", Stage.FEATURES, "Zone history available",
               ("WARMUP", "WARMING_UP")),
            _g("htf_bias", Stage.CONTEXT, "Higher-timeframe bias agrees",
               ("HTF_NOT_READY", "STALE_HTF_CANDLE")),
            _g("flip", Stage.SETUP, "Zone broken and flipped",
               ("NO_SETUP", "NO_ELIGIBLE_ZONE", "NO_ROLE_FLIP")),
            _g("retest", Stage.CONFIRMATION, "Flipped zone retested and held",
               ("REJECTION_FAILED", "RETEST_NOT_HELD", "NO_FALSE_BREAK",
                "NO_REVERSAL_CLOSE")),
        ),
        entry_trigger="A retest that holds a zone which has broken and flipped role.",
        invalidation="Price closing back through the flipped zone.",
        stop_model="Beyond the flipped zone plus a buffer.",
        target_model="Configured reward-to-risk multiple.",
    ),
    "pa_rulebook": VisualAdapter(
        strategy_id="pa_rulebook",
        module="strategies.pa_rulebook_strategy",
        engine_modules=("services.pa_rulebook_v01",),
        features=(Feature.SUPPORT, Feature.RESISTANCE, Feature.SWING,
                  Feature.REGIME, Feature.REJECTION_CANDLE, Feature.DOMINANT_CANDLE,
                  Feature.ZONE_FLIP, Feature.OPPOSING_ZONE_TARGET, Feature.ATR_BAND),
        setup_gates=(
            _g("warmup", Stage.FEATURES, "200 closed bars on every timeframe",
               ("WARMING_UP", "WARMUP", "MISSING_CANDLE", "NON_REAL_DATA")),
            _g("htf_ready", Stage.CONTEXT, "1H regime candle available and fresh",
               ("HTF_NOT_READY", "STALE_HTF_CANDLE")),
            _g("regime", Stage.CONTEXT, "1H regime permits this direction",
               ("REGIME_NOT_ALIGNED",)),
            _g("zone", Stage.SETUP, "Price at an unconsumed zone",
               ("NO_ELIGIBLE_ZONE", "ZONE_CONSUMED", "NO_SETUP")),
            _g("rejection", Stage.SETUP, "15M rejection or dominant candle at the zone",
               ("REJECTION_FAILED",)),
            _g("confirmation", Stage.CONFIRMATION, "Confirmed within three 5M candles",
               ("CONFIRMATION_EXPIRED",)),
            _g("stop_band", Stage.STRATEGY_ACCEPT, "Structural stop inside the ATR band",
               ("STOP_DISTANCE_INVALID",)),
            _g("target", Stage.STRATEGY_ACCEPT, "An opposing zone to aim at",
               ("TARGET_UNAVAILABLE",)),
            _g("net_rr", Stage.STRATEGY_ACCEPT, "Net reward-to-risk at or above 2.5",
               ("NET_RR_TOO_LOW",),
               "Measured after costs, against a target taken from real structure "
               "rather than an ATR multiple."),
        ),
        entry_trigger="A confirmed 15M rejection at an immutable 1H zone, inside an aligned regime.",
        invalidation="Zone consumed, confirmation window expired, or the cancel price touched.",
        stop_model="Structural: beyond the zone and the rejection sweep, plus a buffer.",
        target_model="The nearest unexpired opposing zone. Never an ATR multiple.",
        notes=("The only strategy here whose target must exist in the market. That is why "
               "it refuses setups the ATR-target strategies would take."),
    ),
    "smc": VisualAdapter(
        strategy_id="smc",
        module="strategies.smc_strategy",
        engine_modules=("services.native_smc",),
        features=(Feature.SUPPLY, Feature.DEMAND, Feature.SWING, Feature.LIQUIDITY,
                  Feature.LIQUIDITY_SWEEP, Feature.BOS, Feature.CHOCH, Feature.FVG,
                  Feature.POI, Feature.HTF_BIAS),
        setup_gates=(
            _g("pivots_ready", Stage.FEATURES, "Swing pivots confirmed",
               ("WARMUP", "WARMING_UP")),
            _g("htf_bias", Stage.CONTEXT, "Primary HTF bias established",
               ("HTF_NOT_READY", "STALE_HTF_CANDLE")),
            _g("sweep", Stage.SETUP, "Liquidity swept", ("NO_LIQUIDITY_SWEEP", "NO_SETUP")),
            _g("structure", Stage.SETUP, "Structure broke in the new direction",
               ("NO_BOS", "NO_CHOCH")),
            _g("poi", Stage.CONFIRMATION, "Price returned to the POI",
               ("OUTSIDE_POI", "NO_FVG")),
        ),
        entry_trigger="A return to a POI after a liquidity sweep and a structure break.",
        invalidation="Price closing beyond the originating supply/demand zone.",
        stop_model="Beyond the zone that produced the move.",
        target_model="Opposing liquidity or a configured reward-to-risk multiple.",
    ),
}


def adapter_for(strategy_id: str) -> Optional[VisualAdapter]:
    """The adapter for a strategy, resolved the way the runtime resolves it.

    The catalog is the authority for strategy identity. Looking a key up here
    that the catalog does not know would let the Lab describe a strategy the
    instance cannot actually be running, which is the mismatch this indirection
    exists to make impossible.
    """
    key = str(strategy_id or "").strip()
    if not key:
        return None
    try:
        from services.strategy_registry import REGISTRY
        known = set(REGISTRY)
    except Exception:  # noqa: BLE001 — a registry import failure is not a reason to invent one
        known = set(ADAPTERS)
    if key not in known:
        return None
    return ADAPTERS.get(key)


@dataclass
class GateResult:
    gate: Gate
    state: GateState
    blocker: str = ""

    def public(self) -> dict:
        return {"id": self.gate.id, "stage": self.gate.stage.value,
                "label": self.gate.label, "detail": self.gate.detail,
                "state": self.state.value, "blocker": self.blocker,
                "explanation": explain(self.blocker) if self.blocker else ""}


def gate_sequence(adapter: VisualAdapter) -> tuple[Gate, ...]:
    """Market data, then the strategy's own gates, then risk, then execution.

    The order is the order a bar actually travels, which is what makes "every
    gate before the failing one passed" a true statement rather than a
    presentational convenience.
    """
    return MARKET_DATA_GATES + adapter.setup_gates + RISK_GATES + EXECUTION_GATES


def resolve_gates(adapter: VisualAdapter, *, blocker: Optional[str],
                  position_open: bool = False) -> list[GateResult]:
    """Locate the runtime's blocker in the sequence; never re-derive it.

    With no blocker and an open position every gate has demonstrably been
    passed. With no blocker and no position the instance is scanning: the
    market-data gates have passed (a decision was reached) and the rest are
    waiting on a setup that has not appeared.

    A blocker the sequence does not recognise is reported as such rather than
    quietly passing everything -- an unattributed veto is exactly the thing
    that hid NET_RR_TOO_LOW behind "no setup" before.
    """
    sequence = gate_sequence(adapter)
    code = normalise_blocker(blocker)
    if not code:
        if position_open:
            return [GateResult(gate, GateState.PASS) for gate in sequence]
        results = []
        for gate in sequence:
            state = (GateState.PASS if gate.stage is Stage.MARKET_DATA
                     else GateState.WAITING)
            results.append(GateResult(gate, state))
        return results

    failing = next((index for index, gate in enumerate(sequence)
                    if code in gate.blockers), None)
    if failing is None:
        # Honest about the gap: the code is real, its place in this strategy's
        # sequence is not registered.
        return [GateResult(gate, GateState.WAITING) for gate in sequence]
    results = []
    for index, gate in enumerate(sequence):
        if index < failing:
            results.append(GateResult(gate, GateState.PASS))
        elif index == failing:
            results.append(GateResult(gate, GateState.FAIL, code))
        else:
            results.append(GateResult(gate, GateState.WAITING))
    return results


def current_stage(results: Iterable[GateResult], *, position_open: bool = False) -> Stage:
    """The stage the bar reached: the failing gate's, or as far as it got."""
    results = list(results)
    if position_open:
        return Stage.POSITION
    for result in results:
        if result.state is GateState.FAIL:
            return result.gate.stage
    for result in results:
        if result.state is GateState.WAITING:
            return result.gate.stage
    return Stage.STRATEGY_ACCEPT


#: Blocker code -> the state to show. Anything unmapped falls to SCANNING with
#: the code still displayed, never to a reassuring "ACTIVE".
_STATE_BY_BLOCKER: Mapping[str, DecisionState] = {
    "FEED_NOT_SYNCHRONIZED": DecisionState.DATA_BLOCKED,
    "STALE_MARKET_DATA": DecisionState.DATA_BLOCKED,
    "STALE_CANDLE": DecisionState.DATA_BLOCKED,
    "STALE_CANDLES": DecisionState.DATA_BLOCKED,
    "NON_REAL_DATA": DecisionState.DATA_BLOCKED,
    "MARKET_QUALITY": DecisionState.DATA_BLOCKED,
    "MISSING_CANDLE": DecisionState.DATA_BLOCKED,
    "NO_CANDLES": DecisionState.WAITING_FOR_DATA,
    "WARMUP": DecisionState.WAITING_FOR_DATA,
    "WARMING_UP": DecisionState.WAITING_FOR_DATA,
    "HTF_NOT_READY": DecisionState.WAITING_FOR_HTF,
    "HTF_UNAVAILABLE": DecisionState.WAITING_FOR_HTF,
    "STALE_HTF_CANDLE": DecisionState.WAITING_FOR_HTF,
    "STALE_HTF": DecisionState.WAITING_FOR_HTF,
    "OUTSIDE_POI": DecisionState.WAITING_FOR_POI,
    "NO_ELIGIBLE_ZONE": DecisionState.WAITING_FOR_POI,
    "ZONE_CONSUMED": DecisionState.WAITING_FOR_POI,
    "REJECTION_FAILED": DecisionState.WAITING_CONFIRMATION,
    "NO_SUPPORT_REJECTION": DecisionState.WAITING_CONFIRMATION,
    "NO_RESISTANCE_REJECTION": DecisionState.WAITING_CONFIRMATION,
    "NO_LIQUIDITY_SWEEP": DecisionState.WAITING_CONFIRMATION,
    "NO_BOS": DecisionState.WAITING_CONFIRMATION,
    "NO_CHOCH": DecisionState.WAITING_CONFIRMATION,
    "NO_FVG": DecisionState.WAITING_CONFIRMATION,
    "NO_DOMINANT_CANDLE": DecisionState.WAITING_CONFIRMATION,
    "NO_VOLUME_CONFIRMATION": DecisionState.WAITING_CONFIRMATION,
    "CONFIRMATION_EXPIRED": DecisionState.WAITING_CONFIRMATION,
    "REGIME_NOT_ALIGNED": DecisionState.SCANNING,
    "NO_SETUP": DecisionState.SCANNING,
    "NET_RR_TOO_LOW": DecisionState.SIGNAL_REJECTED,
    "INSUFFICIENT_RR": DecisionState.SIGNAL_REJECTED,
    "RR_TOO_LOW": DecisionState.SIGNAL_REJECTED,
    "TARGET_UNAVAILABLE": DecisionState.SIGNAL_REJECTED,
    "STOP_DISTANCE_INVALID": DecisionState.SIGNAL_REJECTED,
    "INVALID_RISK": DecisionState.RISK_BLOCKED,
    "RISK_BLOCKED": DecisionState.RISK_BLOCKED,
    "RISK_LIMIT": DecisionState.RISK_BLOCKED,
    "MAX_POSITION_LIMIT": DecisionState.RISK_BLOCKED,
    "TRADE_LIMIT": DecisionState.RISK_BLOCKED,
    "CORRELATED_EXPOSURE": DecisionState.RISK_BLOCKED,
    "PORTFOLIO_EXPOSURE": DecisionState.RISK_BLOCKED,
    "DAILY_LOSS_LIMIT": DecisionState.RISK_BLOCKED,
    "WEEKLY_LOSS_LIMIT": DecisionState.RISK_BLOCKED,
    "LOSS_COOLDOWN": DecisionState.RISK_BLOCKED,
    "COOLDOWN_ACTIVE": DecisionState.RISK_BLOCKED,
    "EXISTING_EXPOSURE": DecisionState.POSITION_OPEN,
    "POSITION_ALREADY_ALIGNED": DecisionState.POSITION_OPEN,
    "POSITION_MANAGED": DecisionState.POSITION_OPEN,
    "SIGNALS_ONLY": DecisionState.SIGNALS_ONLY,
    "PAUSED": DecisionState.PAUSED,
    "APPROVAL_REQUIRED": DecisionState.ORDER_INTENT,
    "ORDER_PENDING": DecisionState.ORDER_PENDING,
    "DUPLICATE_SIGNAL": DecisionState.ORDER_INTENT,
    "OUTSIDE_SESSION": DecisionState.PAUSED,
    "TRADING_DAY_DISABLED": DecisionState.PAUSED,
    "EVENT_BLACKOUT": DecisionState.PAUSED,
    "LIMIT_EXPIRED": DecisionState.SIGNAL_REJECTED,
    "EXECUTION": DecisionState.SIGNAL_REJECTED,
    "PIPELINE_ERROR": DecisionState.DATA_BLOCKED,
}


def decision_state(*, blocker: Optional[str], running: bool,
                   position_open: bool = False) -> DecisionState:
    """One badge, and never a flattering one.

    A stopped instance is STOPPED even when its last blocker was benign, and a
    blocked instance is never shown as merely running.
    """
    if not running:
        return DecisionState.STOPPED
    code = normalise_blocker(blocker)
    if not code:
        return DecisionState.POSITION_OPEN if position_open else DecisionState.SCANNING
    state = _STATE_BY_BLOCKER.get(code)
    if state is None:
        return DecisionState.POSITION_OPEN if position_open else DecisionState.SCANNING
    if position_open and state in (DecisionState.SCANNING, DecisionState.WAITING_FOR_POI,
                                   DecisionState.WAITING_CONFIRMATION):
        return DecisionState.POSITION_OPEN
    return state
