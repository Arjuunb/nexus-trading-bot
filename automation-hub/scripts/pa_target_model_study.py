#!/usr/bin/env python3
"""Does the rulebook's target model, or the 2.5R gate, cost it every trade?

The 2025 BTCUSDT replay produced 1,730 setups, 55 confirmations and 1 accepted
plan. The rejections were not spread evenly: 32 of 55 were NET_RR_TOO_LOW, the
median net RR was 0.21R against a 2.5R gate, and the room from entry to the
chosen target was a median 15% of what that gate required. The stop was not the
cause -- corr(net_rr, stop_ATR) was -0.02.

That points at the target model, so this measures the target model. For each
confirmation the replay already recorded, it recomputes the plan under several
candidate targets on identical terms -- same entry, same stop, same cost model,
same gate -- and then walks the real 5M candles forward to see which came
first, the target or the stop.

The last part is what makes this more than arithmetic. A target model that
passes the gate more often but is never reached is worse than one that refuses:
it converts refusals into losses. Acceptance count alone would rank it first.

Candidates:

  control        the shipped model: nearest unexpired opposing pivot zone.
                 Not modified anywhere in this repo by this script.
  next_zone_out  the SECOND opposing zone, on the hypothesis that the nearest
                 one is simply too close to pay after costs.
  atr_k          entry +/- k x setup ATR15, structure ignored.
  r_k            entry +/- k x stop distance.
  gate_exact     the nearest target at which the net gate is exactly satisfied.
                 Its hit rate is the cleanest question available: from these
                 entries, does the market ever travel the distance the 2.5R
                 gate demands before the stop? If it does not, no target model
                 can rescue this strategy and the gate itself is the subject.

RESEARCH ONLY. It reads a replay audit and closed candles and prints. It places
no order, writes to no database, and changes no strategy: services/
pa_rulebook_v01.py is imported for its cost model and its config defaults and
is never edited. Chapter 2 permits exactly this use of historical data and
forbids the other one -- no retrospective forward orders are derived here.

    python scripts/pa_target_model_study.py --audit /tmp/pa_2025.json
    python scripts/pa_target_model_study.py --audit /tmp/pa_2025.json --max-hold 288
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ATR_MULTIPLES = (2.0, 3.0, 4.0, 6.0)
R_MULTIPLES = (2.5, 3.0, 4.0)
#: 5M bars a plan may stay open before the run calls it unresolved. 288 is 24h.
#: Reported separately from wins and losses: a trade that never resolved is not
#: a scratch, and folding it into either would flatter or punish arbitrarily.
DEFAULT_MAX_HOLD = 288


def _live_opposing(zones: list[dict], direction: str, entry: float) -> list[dict]:
    """The same filter the shipped model uses, applied to the recorded registry.

    origin == "pivot" and not retired: a flip object is not an original zone
    and chapter 10 does not let it supply a target. The audit records zones as
    they stood at the decision, so this re-reads history rather than replaying
    it.
    """
    live = [z for z in zones
            if z.get("origin") == "pivot" and not z.get("retired")]
    if direction == "long":
        beyond = [z for z in live
                  if z.get("kind") == "resistance" and float(z["lower"]) > entry]
        return sorted(beyond, key=lambda z: float(z["lower"]))
    beyond = [z for z in live
              if z.get("kind") == "support" and float(z["upper"]) < entry]
    return sorted(beyond, key=lambda z: -float(z["upper"]))


def _targets(record: dict, tick: float, min_net_rr: float = 2.5,
             costs=None) -> dict[str, float | None]:
    """Every candidate target for one confirmation, or None where undefined."""
    plan = record.get("plan") or {}
    direction = record["direction"]
    entry = plan.get("entry_bound")
    stop = plan.get("stop")
    atr = record.get("setup_atr15")
    if entry is None or stop is None:
        return {}
    distance = abs(float(entry) - float(stop))
    sign = 1.0 if direction == "long" else -1.0
    out: dict[str, float | None] = {"control": plan.get("target")}

    beyond = _live_opposing(record.get("zones_in_view") or [], direction, float(entry))
    if len(beyond) >= 2:
        second = beyond[1]
        edge = float(second["lower"]) - tick if direction == "long" \
            else float(second["upper"]) + tick
        out["next_zone_out"] = edge
    else:
        # Fewer than two opposing zones is not a target of zero; it is a
        # confirmation this variant cannot price, and it is counted as such.
        out["next_zone_out"] = None

    for k in ATR_MULTIPLES:
        out[f"atr_{k:g}"] = (float(entry) + sign * k * float(atr)) if atr else None
    for k in R_MULTIPLES:
        out[f"r_{k:g}"] = float(entry) + sign * k * distance
    if costs is not None:
        out["gate_exact"] = _gate_exact_target(float(entry), float(stop), direction,
                                               min_net_rr, costs, tick)
    return out


def _gate_exact_target(entry: float, stop: float, direction: str,
                       min_net_rr: float, costs, tick: float = 0.0) -> float:
    """The nearest target at which the net RR gate is exactly satisfied.

    This is the question the whole study exists to ask, stated as a price: from
    this entry, with this stop, how far must the market actually travel before
    a 2.5R NET gate is met? Note that it is strictly further than 2.5x the stop
    distance, because the gate is net and the costs come out of the reward --
    which is why an r_2.5 target fails a 2.5R gate rather than just meeting it.

    Solved against the supplied cost model by fixed-point rather than algebra,
    so a different cost shape does not silently invalidate it.
    """
    sign = 1.0 if direction == "long" else -1.0
    risk = abs(entry - stop) + costs.loss_path(entry, stop)
    gross = min_net_rr * risk
    distance = gross
    for _ in range(40):
        target = entry + sign * distance
        updated = gross + costs.win_path(entry, target)
        if abs(updated - distance) < 1e-9:
            break
        distance = updated
    target = entry + sign * distance
    if tick > 0:
        # Solved exactly, the target sits ON the gate, and whether it clears
        # becomes a floating-point coin toss. Rounding to the next tick AWAY
        # from entry settles it the way an exchange would: no order can be
        # placed at a sub-tick price, and the extra fraction of a tick is a
        # cost to this variant, never a discount.
        steps = distance / tick
        distance = (math.ceil(steps) if steps > 0 else math.floor(steps)) * tick
        target = entry + sign * distance
    return target


def _net_rr(entry: float, stop: float, target: float, costs) -> float | None:
    """The shipped arithmetic, so a variant is never scored on softer terms."""
    costs_loss = costs.loss_path(entry, stop)
    costs_win = costs.win_path(entry, target)
    reward = abs(target - entry) - costs_win
    risk = abs(entry - stop) + costs_loss
    return (reward / risk) if risk > 0 else None


def _outcome(bars, start_at, direction: str, entry: float, stop: float,
             target: float, max_hold: int) -> tuple[str, int]:
    """Which came first from the real candles: target, stop, or neither.

    A bar whose range spans BOTH is ambiguous -- OHLC cannot say which the
    market touched first -- and is resolved as a loss. That is the conservative
    reading and it is reported separately, because a study that silently
    resolved ties as wins would be measuring its own optimism.
    """
    seen = 0
    for bar in bars:
        if bar.timestamp <= start_at:
            continue
        seen += 1
        if seen > max_hold:
            return "timeout", seen
        high, low = float(bar.high), float(bar.low)
        if direction == "long":
            hit_t, hit_s = high >= target, low <= stop
        else:
            hit_t, hit_s = low <= target, high >= stop
        if hit_t and hit_s:
            return "ambiguous", seen
        if hit_t:
            return "target", seen
        if hit_s:
            return "stop", seen
    return "unresolved", seen


def _load_confirm_bars(symbol: str, bars: int):
    """Closed 5M candles, real only -- the same rule the replay itself follows."""
    from data.market_data import get_bars

    rows, source = get_bars(symbol, n=bars, timeframe="5m", require_real=True)
    return list(rows), source


def study(audit: dict, bars, *, max_hold: int, min_net_rr: float, tick: float,
          costs, out) -> dict:
    from datetime import datetime

    records = audit.get("confirmations") or []
    meta = audit.get("meta") or {}
    names = ["control", "next_zone_out"]
    names += [f"atr_{k:g}" for k in ATR_MULTIPLES]
    names += [f"r_{k:g}" for k in R_MULTIPLES]
    names.append("gate_exact")
    results = {name: {"priced": 0, "undefined": 0, "passed": 0, "net_rrs": [],
                      "target": 0, "stop": 0, "ambiguous": 0, "timeout": 0,
                      "unresolved": 0, "r_sum": 0.0, "holds": []}
               for name in names}

    for record in records:
        plan = record.get("plan") or {}
        entry, stop = plan.get("entry_bound"), plan.get("stop")
        if entry is None or stop is None:
            continue
        entry, stop = float(entry), float(stop)
        direction = record["direction"]
        at = datetime.fromisoformat(record["at"])
        for name, target in _targets(record, tick, min_net_rr, costs).items():
            bucket = results[name]
            if target is None:
                bucket["undefined"] += 1
                continue
            bucket["priced"] += 1
            net = _net_rr(entry, stop, float(target), costs)
            if net is None:
                bucket["undefined"] += 1
                continue
            bucket["net_rrs"].append(net)
            if net < min_net_rr:
                continue                      # the gate refused it; no trade
            bucket["passed"] += 1
            verdict, held = _outcome(bars, at, direction, entry, stop,
                                     float(target), max_hold)
            bucket[verdict] += 1
            bucket["holds"].append(held)
            # R is realised against the plan's own risk, so variants with
            # different targets stay comparable.
            if verdict == "target":
                bucket["r_sum"] += net
            elif verdict in ("stop", "ambiguous"):
                bucket["r_sum"] -= 1.0

    print("Nexus PA rulebook -- TARGET MODEL STUDY (research only, no orders)",
          file=out)
    print(f"  replay      {meta.get('symbol', '?')} "
          f"v{meta.get('rulebook_version', '?')} · "
          f"{meta.get('first_candle', '?')[:10]} -> {meta.get('last_candle', '?')[:10]}",
          file=out)
    print(f"  source      {meta.get('sources', {})}", file=out)
    if not meta.get("complete", True):
        print("  WARNING     the replay audit is a PARTIAL checkpoint", file=out)
    window = meta.get("window") or {}
    if window.get("short"):
        # The audit knows its own window fell short. Carrying that here matters
        # more than in the replay: by this point the numbers have been through
        # two tools and look like a year's worth of evidence.
        print(f"  WARNING     the replay covered {window.get('held')}, "
              f"not the {window.get('asked')} it was asked for", file=out)
        print("              every count below is over that shorter window",
              file=out)
    print(f"  confirmations {len(records)}   gate {min_net_rr}R   "
          f"max hold {max_hold} bars", file=out)
    print(file=out)

    head = (f"  {'variant':<16}{'priced':>7}{'n/a':>5}{'pass':>6}{'median':>8}"
            f"{'hit':>6}{'stop':>6}{'amb':>5}{'t/o':>5}{'win%':>7}{'exp R':>8}"
            f"{'net R':>8}")
    print(head, file=out)
    print("  " + "-" * (len(head) - 2), file=out)
    for name in names:
        b = results[name]
        resolved = b["target"] + b["stop"] + b["ambiguous"]
        median = statistics.median(b["net_rrs"]) if b["net_rrs"] else None
        win = (100.0 * b["target"] / resolved) if resolved else None
        exp = (b["r_sum"] / resolved) if resolved else None
        print(f"  {name:<16}{b['priced']:>7}{b['undefined']:>5}{b['passed']:>6}"
              f"{(f'{median:.2f}' if median is not None else '--'):>8}"
              f"{b['target']:>6}{b['stop']:>6}{b['ambiguous']:>5}{b['timeout']:>5}"
              f"{(f'{win:.0f}' if win is not None else '--'):>7}"
              f"{(f'{exp:+.2f}' if exp is not None else '--'):>8}"
              f"{b['r_sum']:>+8.2f}", file=out)

    print(file=out)
    print("  priced  confirmations this variant could put a target on.", file=out)
    print("  n/a     confirmations it could not -- counted, never skipped.", file=out)
    print("  pass    priced plans that cleared the net RR gate, so would trade.", file=out)
    print("  median  median net RR across every priced plan, gate or no gate.", file=out)
    print("  amb     one bar spanned target AND stop; resolved as a LOSS.", file=out)
    print("  t/o     open past the max hold; counted in neither win nor loss.", file=out)
    print(file=out)

    control, best = results["control"], None
    for name in names:
        b = results[name]
        resolved = b["target"] + b["stop"] + b["ambiguous"]
        if resolved >= 5 and (best is None or b["r_sum"] > results[best]["r_sum"]):
            best = name
    print("  READING THIS TABLE", file=out)
    print(f"    The shipped model priced {control['priced']} of {len(records)} "
          f"confirmations and {control['passed']} cleared the gate.", file=out)
    if best and best != "control":
        b = results[best]
        print(f"    '{best}' cleared it {b['passed']} times for {b['r_sum']:+.2f}R "
              f"across {b['target'] + b['stop'] + b['ambiguous']} resolved trades.",
              file=out)
        print("    That is a HYPOTHESIS, not a result: one symbol, one year, one"
              " parameter set,", file=out)
        print("    chosen after seeing the outcomes. It needs an out-of-sample"
              " window and then", file=out)
        print("    forward paper before it means anything about money.", file=out)
    elif best == "control":
        print("    No variant beat the shipped model on realised R. The target"
              " model is not", file=out)
        print("    the binding constraint it looked like.", file=out)
    else:
        print("    No variant resolved enough trades to compare. The sample is"
              " the finding:", file=out)
        print("    this strategy does not trade often enough to measure at this"
              " window size.", file=out)
    return {"variants": results, "confirmations": len(records)}


def main(argv=None) -> int:
    from services.pa_rulebook_v01 import CostModel, RulebookConfig

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", required=True,
                        help="JSON written by pa_rulebook_replay.py --audit")
    parser.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD,
                        help="5M bars a plan may stay open (default 288 = 24h)")
    parser.add_argument("--bars", type=int, default=120_000,
                        help="5M candles to load for outcome resolution")
    args = parser.parse_args(argv)

    path = Path(args.audit)
    if not path.exists():
        print(f"no such audit: {path}", file=sys.stderr)
        return 2
    audit = json.loads(path.read_text())
    symbol = (audit.get("meta") or {}).get("symbol") or "BTCUSDT"

    bars, source = _load_confirm_bars(symbol, args.bars)
    if not bars:
        print(f"no real 5M candles for {symbol} ({source}) -- outcomes cannot be "
              "resolved, and a study that skipped them would be arithmetic only",
              file=sys.stderr)
        return 3

    config = RulebookConfig(symbol=symbol)
    study(audit, bars, max_hold=args.max_hold, min_net_rr=config.min_net_rr,
          tick=config.tick_size, costs=CostModel(), out=sys.stdout)
    print(f"\n  outcomes resolved on {len(bars)} real 5M candles ({source})")
    print("  Research output. No order was placed and none may be derived "
          "retrospectively from this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
