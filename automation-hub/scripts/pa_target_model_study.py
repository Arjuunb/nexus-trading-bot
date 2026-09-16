#!/usr/bin/env python3
"""Does the rulebook's target model, or the 2.5R gate, cost it every trade?

The 2025 BTCUSDT replay produced 1,664 setups, 57 confirmations and 1 accepted
plan. The rejections were not spread evenly: most were NET_RR_TOO_LOW, the
median net RR was a fraction of the 2.5R gate, and the room from entry to the
chosen target was a small fraction of what that gate required. The stop was not
the cause -- corr(net_rr, stop_ATR) was ~0.

That pointed at the target model, so this measures the target model. For each
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

WHAT IT ANSWERED, on 2025 BTCUSDT 5M (104,242 venue candles, 57
confirmations): no. Every variant that resolved at least MIN_RESOLVED trades
lost money, each with negative expectancy, so every extra plan a looser target
admitted was a losing trade in expectation. 'gate_exact' -- the gate stated
as a price -- cleared all 57, paid ~2.5R on a win, needed >28.6% of them to
reach target and reached 21%. Loosening the target converts refusals into
losses. The binding constraint is the gate and the entries, not the target
model. One symbol, one year: a direction to test next, not a conclusion about
money.

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
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ATR_MULTIPLES = (2.0, 3.0, 4.0, 6.0)
R_MULTIPLES = (2.5, 3.0, 4.0)
#: 5M bars a plan may stay open before the run calls it unresolved. 288 is 24h.
#: Reported separately from wins and losses: a trade that never resolved is not
#: a scratch, and folding it into either would flatter or punish arbitrarily.
DEFAULT_MAX_HOLD = 288
#: Resolved trades a variant needs before the summary will compare it to
#: anything. Below this the R total is one or two outcomes wearing a decimal
#: point, and a search over ten variants will always surface one of them.
MIN_RESOLVED = 5


def _live_opposing(zones: list[dict], direction: str, entry: float) -> list[dict]:
    """Opposing pivot zones still live at the decision, nearest first.

    origin == "pivot" and not retired: a flip object is not an original zone
    and chapter 10 does not let it supply a target. The audit records zones as
    they stood at the decision, so this re-reads history rather than replaying
    it.

    This is a RECONSTRUCTION of the shipped model's view, not proof of
    identity with it: this finds two opposing zones on confirmations the
    engine itself rejected with TARGET_UNAVAILABLE, so the engine applies at
    least one constraint beyond "pivot, live, beyond entry". Read the
    zone-derived variants as an upper bound on what structure offered, not as
    what the engine would have chosen.
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


def _load_confirm_bars(symbol: str, bars: int, until=None):
    """Closed 5M candles for outcome resolution, venue first, real only.

    Anchored at ``until`` so the window walked is the one the confirmations
    live in, not the newest candles: outcomes resolved on a series that does
    not reach them come back "unresolved", and a study that quietly resolved
    nothing looks identical to one where nothing hit its target.
    """
    from services.live_candle_source import (
        LiveCandlesUnavailable,
        live_series,
        pages_for,
    )

    try:
        rows = live_series(symbol, "5m", limit=bars, until=until,
                           max_pages=pages_for(bars), use_cache=False)
    except LiveCandlesUnavailable as exc:
        venue_error = str(exc)
    else:
        return list(rows), "venue binance_usdm (live)"

    from data.market_data import get_bars

    rows, source = get_bars(symbol, n=bars, timeframe="5m", require_real=True)
    return list(rows), f"{source} [venue unavailable: {venue_error}]"


def _covers(bars, records, max_hold: int) -> dict:
    """Whether the outcome series reaches every confirmation it must judge."""
    from datetime import datetime, timedelta

    stamps = [datetime.fromisoformat(r["at"]) for r in records if r.get("at")]
    if not bars or not stamps:
        return {"ok": not stamps, "missing": len(stamps)}
    first, last = bars[0].timestamp, bars[-1].timestamp
    needed_end = max(stamps) + timedelta(minutes=5 * max_hold)
    # Unreachable in either direction: a confirmation before the series begins
    # has no candles to be judged on, and one at or after its last bar has no
    # candles AFTER it -- which is the half that decides the outcome.
    unreachable = [s for s in stamps if s < first or s >= last]
    return {
        "ok": not unreachable and last >= min(needed_end, datetime.now(last.tzinfo)),
        "missing": len(unreachable),
        "series": f"{first:%Y-%m-%d} -> {last:%Y-%m-%d}",
        "needed": f"{min(stamps):%Y-%m-%d} -> {needed_end:%Y-%m-%d}",
    }


def _resolved(bucket: dict) -> int:
    """Trades that reached an outcome. A timeout is not one: it is neither a
    win nor a loss, and folding it into either would flatter or punish."""
    return bucket["target"] + bucket["stop"] + bucket["ambiguous"]


def _breakeven(bucket: dict):
    """The win rate this variant's average reward needs, to break even.

    A win pays its net RR, a loss costs 1R, so p x reward = (1 - p) x 1 and
    p = 1 / (1 + reward). Printed beside the observed win rate because a
    variant with a big target and a low hit rate reads as promising right up
    until the two numbers are put next to each other.
    """
    nets = bucket.get("pass_nets") or []
    if not nets:
        return None
    reward = sum(nets) / len(nets)
    if reward <= -1.0:
        return None
    return 100.0 / (1.0 + reward)


def _candidate(results: dict, names):
    """The one variant, if any, worth calling a hypothesis.

    Three bars, not one. Enough resolved trades to be a sample, a POSITIVE
    realised R, and more of it than the shipped model. Ranking on realised R
    alone named the least-bad loser: every variant can be under water and one
    of them is still the maximum.
    """
    control = results["control"]
    sized = [n for n in names
             if n != "control" and _resolved(results[n]) >= MIN_RESOLVED]
    candidates = [n for n in sized if results[n]["r_sum"] > 0
                  and results[n]["r_sum"] > control["r_sum"]]
    best = max(candidates, key=lambda n: results[n]["r_sum"]) if candidates else None
    return best, sized


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
                      "pass_nets": [], "target": 0, "stop": 0, "ambiguous": 0,
                      "timeout": 0, "unresolved": 0, "r_sum": 0.0, "holds": [],
                      # Why a confirmation could not be priced, by the verdict
                      # the replay recorded. "control" is the target the engine
                      # actually chose, so it is absent on every rejection --
                      # including ones decided by the STOP, where it is not
                      # evidence about the target model at all.
                      "undefined_by": Counter()}
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
                bucket["undefined_by"][record.get("verdict") or "UNKNOWN"] += 1
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
            # Reward on the plans that became trades, which is what the
            # break-even win rate is computed from. net_rrs includes the ones
            # the gate refused, so it would understate what a win pays.
            bucket["pass_nets"].append(net)
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
            f"{'hit':>6}{'stop':>6}{'amb':>5}{'t/o':>5}{'win%':>7}{'b/e%':>7}"
            f"{'exp R':>8}{'net R':>8}")
    print(head, file=out)
    print("  " + "-" * (len(head) - 2), file=out)
    for name in names:
        b = results[name]
        resolved = b["target"] + b["stop"] + b["ambiguous"]
        median = statistics.median(b["net_rrs"]) if b["net_rrs"] else None
        win = (100.0 * b["target"] / resolved) if resolved else None
        exp = (b["r_sum"] / resolved) if resolved else None
        breakeven = _breakeven(b)
        print(f"  {name:<16}{b['priced']:>7}{b['undefined']:>5}{b['passed']:>6}"
              f"{(f'{median:.2f}' if median is not None else '--'):>8}"
              f"{b['target']:>6}{b['stop']:>6}{b['ambiguous']:>5}{b['timeout']:>5}"
              f"{(f'{win:.0f}' if win is not None else '--'):>7}"
              f"{(f'{breakeven:.0f}' if breakeven is not None else '--'):>7}"
              f"{(f'{exp:+.2f}' if exp is not None else '--'):>8}"
              f"{b['r_sum']:>+8.2f}", file=out)

    print(file=out)
    print("  priced  confirmations this variant could put a target on.", file=out)
    print("  n/a     confirmations it could not -- counted, never skipped.", file=out)
    print("  pass    priced plans that cleared the net RR gate, so would trade.", file=out)
    print("  median  median net RR across every priced plan, gate or no gate.", file=out)
    print("  b/e%    win rate this variant's average reward needs, to break even.", file=out)
    print("          win% under b/e% is a losing variant however green it looks.", file=out)
    print("  amb     one bar spanned target AND stop; resolved as a LOSS.", file=out)
    print("  t/o     open past the max hold; counted in neither win nor loss.", file=out)
    print(file=out)

    unpriced = [n for n in names if results[n]["undefined_by"]]
    if unpriced:
        print("  Could not be priced, by the verdict the replay recorded", file=out)
        for name in unpriced:
            counts = results[name]["undefined_by"]
            detail = "   ".join(f"{count} {verdict}"
                                for verdict, count in counts.most_common())
            print(f"    {name:<16}{detail}", file=out)
        # The distinction the bare n/a column cannot make: a plan the engine
        # threw out on the STOP never had a target to judge, so counting it
        # against the target model is counting the wrong gate.
        not_target = sum(count for verdict, count
                         in results["control"]["undefined_by"].items()
                         if verdict != "TARGET_UNAVAILABLE")
        if not_target:
            print(f"    {not_target} of control's are not TARGET_UNAVAILABLE: the engine"
                  " rejected those plans on", file=out)
            print("    another gate and never recorded a target, so they say nothing"
                  " about the target", file=out)
            print("    model. Every other variant prices them anyway, which gives"
                  " those variants", file=out)
            print("    chances the shipped model never took.", file=out)
        print(file=out)

    control = results["control"]
    best, sized = _candidate(results, names)
    losers = [n for n in sized if results[n]["r_sum"] < 0]
    # Positive, and too thin to mean it. Named rather than left in the table
    # to be read as a winner: across this many variants it is the cell a
    # search is likeliest to produce by chance.
    thin = [n for n in names if n != "control" and results[n]["r_sum"] > 0
            and 0 < _resolved(results[n]) < MIN_RESOLVED]

    print("  READING THIS TABLE", file=out)
    print(f"    The shipped model priced {control['priced']} of {len(records)} "
          f"confirmations and {control['passed']} cleared the gate.", file=out)
    if best:
        b = results[best]
        print(f"    '{best}' cleared it {b['passed']} times for {b['r_sum']:+.2f}R "
              f"across {_resolved(b)} resolved trades.", file=out)
        print("    That is a HYPOTHESIS, not a result: one symbol, one year, one"
              " parameter set,", file=out)
        print("    chosen after seeing the outcomes. It needs an out-of-sample"
              " window and then", file=out)
        print("    forward paper before it means anything about money.", file=out)
    elif sized:
        print("    No variant beat the shipped model on realised R.", file=out)
        if losers and len(losers) == len(sized):
            # Every sized variant has negative expectancy, so this holds
            # per trade and not merely in total -- which a "traded most,
            # lost most" ranking would not, the moment two variants tied.
            print(f"    All {len(sized)} variants that resolved at least "
                  f"{MIN_RESOLVED} trades lost money, so every extra", file=out)
            print("    plan a looser target admits is a losing trade in"
                  " expectation. Loosening the", file=out)
            print("    target converts refusals into losses -- the failure an"
                  " acceptance count", file=out)
            print("    alone would have scored as a win.", file=out)
        elif losers:
            print(f"    {len(losers)} of the {len(sized)} variants that resolved "
                  f"at least {MIN_RESOLVED} trades lost money,", file=out)
            print("    so on this window a looser target mostly bought losing"
                  " trades.", file=out)
        print("    The target model is not the binding constraint it looked"
              " like.", file=out)
    else:
        print("    No variant resolved enough trades to compare. The sample is"
              " the finding:", file=out)
        print("    this strategy does not trade often enough to measure at this"
              " window size.", file=out)

    gate = results.get("gate_exact")
    if gate and _resolved(gate) >= MIN_RESOLVED and gate["pass_nets"]:
        reward = sum(gate["pass_nets"]) / len(gate["pass_nets"])
        breakeven = _breakeven(gate)
        win = 100.0 * gate["target"] / _resolved(gate)
        if breakeven is not None:
            print(f"    'gate_exact' is the {min_net_rr}R gate stated as a price."
                  f" It pays {reward:.2f}R on a win,", file=out)
            print(f"    so it needs {breakeven:.1f}% of its trades to reach "
                  f"target to break even. It reached {win:.0f}%.", file=out)
            print("    That is a question about the gate and the entries, not"
                  " about the target model.", file=out)

    for name in thin:
        b = results[name]
        print(f"    '{name}' shows {b['r_sum']:+.2f}R, but on {_resolved(b)} "
              f"resolved trades. That is not a", file=out)
        print(f"    finding -- it is the cell a search across {len(names)} "
              "variants is likeliest to", file=out)
        print("    produce by chance, and it is the one most likely to be"
              " mistaken for one.", file=out)
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

    from datetime import datetime, timedelta

    records = audit.get("confirmations") or []
    stamps = [datetime.fromisoformat(r["at"]) for r in records if r.get("at")]
    # Resolve forward from the last confirmation by the hold, so the series
    # covers the trades it has to judge rather than ending among them.
    until = (max(stamps) + timedelta(minutes=5 * (args.max_hold + 5))) if stamps else None
    if until is not None and until > datetime.now(until.tzinfo):
        until = None
    bars, source = _load_confirm_bars(symbol, args.bars, until)
    if not bars:
        print(f"no real 5M candles for {symbol} ({source}) -- outcomes cannot be "
              "resolved, and a study that skipped them would be arithmetic only",
              file=sys.stderr)
        return 3

    config = RulebookConfig(symbol=symbol)
    study(audit, bars, max_hold=args.max_hold, min_net_rr=config.min_net_rr,
          tick=config.tick_size, costs=CostModel(), out=sys.stdout)
    coverage = _covers(bars, records, args.max_hold)
    print(f"\n  outcomes resolved on {len(bars)} real 5M candles ({source})")
    if not coverage["ok"]:
        print(f"  !! OUTCOME SERIES DOES NOT COVER THE CONFIRMATIONS", file=sys.stderr)
        print(f"     series {coverage.get('series')} · needed "
              f"{coverage.get('needed')}", file=sys.stderr)
        print(f"     {coverage['missing']} confirmations start before the series "
              "begins; their outcomes could not be resolved and are NOT losses.",
              file=sys.stderr)
    print("  Research output. No order was placed and none may be derived "
          "retrospectively from this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
