#!/usr/bin/env python3
"""Render a rulebook replay audit as a visual setup review.

The question this answers is the one a summary cannot: for each confirmed
setup, does the coded zone, the rejection candle and the entry match the trade
that was intended? Correct code can still express the wrong idea, and the only
way to see that is on a chart, setup by setup, next to the reason the plan was
accepted or refused.

It also separates the three things a low net reward-to-risk can mean, because
they have different answers and a single ratio hides which one happened:

    stop too wide          large stop distance in ATR terms
    entry too far away     confirmation closed well past the rejection candle
    not enough room        the nearest opposing zone caps the target

Reads the JSON written by ``pa_rulebook_replay.py --audit`` and writes one
self-contained HTML file. No network, no CDN, no orders.

    python scripts/pa_rulebook_replay.py --symbol BTCUSDT --bars 26000 \
        --audit review.json
    python scripts/pa_rulebook_review.py review.json -o review.html
"""
from __future__ import annotations

import argparse
import html
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Optional

# The validated default palette. Both modes pass every check in the data-viz
# validator: lightness band, chroma floor, CVD separation, normal-vision floor
# and contrast against their own surface.
#
# Candles are blue/red rather than the usual green/red on purpose. Up and down
# are a polarity, and blue<->red is the diverging pair that survives colour
# vision deficiency -- green/red is the one pairing that does not, which is a
# poor default for a chart whose whole job is direction.
LIGHT = {
    "surface": "#fcfcfb", "plane": "#f9f9f7", "ink": "#0b0b0b",
    "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
    "axis": "#c3c2b7", "up": "#2a78d6", "down": "#e34948",
    "series1": "#2a78d6", "series2": "#eb6834",
    "good": "#0ca30c", "critical": "#d03b3b", "warning": "#fab219",
    "border": "rgba(11,11,11,0.10)", "zone": "rgba(42,120,214,0.10)",
}
DARK = {
    "surface": "#1a1a19", "plane": "#0d0d0d", "ink": "#ffffff",
    "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
    "axis": "#383835", "up": "#3987e5", "down": "#e66767",
    "series1": "#3987e5", "series2": "#d95926",
    "good": "#0ca30c", "critical": "#d03b3b", "warning": "#fab219",
    "border": "rgba(255,255,255,0.10)", "zone": "rgba(57,135,229,0.16)",
}

CHART_W, CHART_H = 720, 300
PAD_L, PAD_R, PAD_T, PAD_B = 8, 76, 14, 26


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "&mdash;"
    return f"{value:,.{digits}f}"


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso)


def candle_chart(record: dict) -> str:
    """One setup on a 15M chart: the zone, the rejection, the entry and the stop.

    The target is deliberately allowed off-scale. It is often thousands of
    points away, and including it would squash the candles into a band a few
    pixels tall -- which would defeat the only purpose of drawing them, namely
    checking that the zone and the rejection candle are where they were meant
    to be. When it falls outside, it is annotated at the edge with its distance
    instead.
    """
    candles = record.get("candles", {}).get("15m", [])
    if not candles:
        return '<p class="empty">no setup-timeframe candles recorded</p>'

    zone = record["zone"]
    plan = record.get("plan") or {}
    entry, stop = plan.get("entry_bound"), plan.get("stop")
    target = plan.get("target")

    lows = [c["l"] for c in candles] + [zone["lower"]]
    highs = [c["h"] for c in candles] + [zone["upper"]]
    for level in (entry, stop):
        if level is not None:
            lows.append(level)
            highs.append(level)
    low, high = min(lows), max(highs)
    span = (high - low) or 1.0
    low -= span * 0.06
    high += span * 0.06
    span = high - low

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B
    step = plot_w / max(len(candles), 1)
    body_w = max(2.0, min(9.0, step * 0.62))

    def y_of(price: float) -> float:
        return PAD_T + (high - price) / span * plot_h

    def x_of(index: int) -> float:
        return PAD_L + index * step + step / 2

    parts: list[str] = []

    # Zone band first, so candles sit on top of it.
    band_top, band_bottom = y_of(zone["upper"]), y_of(zone["lower"])
    parts.append(
        f'<rect x="{PAD_L}" y="{band_top:.1f}" width="{plot_w:.1f}" '
        f'height="{max(1.0, band_bottom - band_top):.1f}" fill="var(--zone)" '
        f'stroke="var(--series1)" stroke-opacity="0.35" stroke-width="1"/>'
        f'<title>{_e(zone["id"])} {_e(zone["kind"])} '
        f'{_num(zone["lower"])} to {_num(zone["upper"])}</title>')

    rejection_t = (record.get("rejection") or {}).get("t")
    for index, candle in enumerate(candles):
        x = x_of(index)
        rising = candle["c"] >= candle["o"]
        colour = "var(--up)" if rising else "var(--down)"
        top, bottom = y_of(max(candle["o"], candle["c"])), y_of(min(candle["o"], candle["c"]))
        is_rejection = rejection_t is not None and candle["t"] == rejection_t
        # The rejection candle is ringed rather than recoloured: its direction
        # is information too, and repainting it would throw that away.
        ring = (' stroke="var(--ink)" stroke-width="1.5"' if is_rejection
                else ' stroke="none"')
        parts.append(
            f'<g><title>{_e(_when(candle["t"]))}  O {_num(candle["o"])}  '
            f'H {_num(candle["h"])}  L {_num(candle["l"])}  C {_num(candle["c"])}'
            f'{"  -- rejection candle" if is_rejection else ""}</title>'
            f'<line x1="{x:.1f}" y1="{y_of(candle["h"]):.1f}" x2="{x:.1f}" '
            f'y2="{y_of(candle["l"]):.1f}" stroke="{colour}" stroke-width="1.5"/>'
            f'<rect x="{x - body_w / 2:.1f}" y="{top:.1f}" width="{body_w:.1f}" '
            f'height="{max(1.5, bottom - top):.1f}" fill="{colour}"{ring} rx="1"/></g>')

    # Levels, labelled directly at the right edge rather than in a legend box.
    for level, label, dash, colour in (
            (entry, "entry", "none", "var(--ink)"),
            (stop, "stop", "4 3", "var(--critical)"),
            (target if target is not None and low <= target <= high else None,
             "target", "4 3", "var(--good)")):
        if level is None:
            continue
        y = y_of(level)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + plot_w:.1f}" y2="{y:.1f}" '
            f'stroke="{colour}" stroke-width="2" stroke-dasharray="{dash}" '
            f'stroke-opacity="0.85"/>'
            f'<text x="{PAD_L + plot_w + 6:.1f}" y="{y + 3.5:.1f}" class="lvl" '
            f'fill="{colour}">{label} {_num(level)}</text>')

    if target is not None and not (low <= target <= high):
        above = target > high
        y = PAD_T + 9 if above else PAD_T + plot_h - 4
        gap = abs(target - (entry or target))
        parts.append(
            f'<text x="{PAD_L + plot_w + 6:.1f}" y="{y:.1f}" class="lvl" '
            f'fill="var(--good)">target {_num(target)}</text>'
            f'<text x="{PAD_L + plot_w + 6:.1f}" y="{y + 13:.1f}" class="lvl sub" '
            f'fill="var(--muted)">{"+" if above else "-"}{_num(gap)} off-chart</text>')

    first, last = _when(candles[0]["t"]), _when(candles[-1]["t"])
    parts.append(
        f'<text x="{PAD_L}" y="{CHART_H - 8}" class="tick">{_e(first)}</text>'
        f'<text x="{PAD_L + plot_w:.1f}" y="{CHART_H - 8}" class="tick" '
        f'text-anchor="end">{_e(last)}</text>')

    return (f'<svg class="chart" viewBox="0 0 {CHART_W} {CHART_H}" '
            f'role="img" aria-label="15-minute candles around setup '
            f'{record["index"]} with its zone, entry and stop">'
            + "".join(parts) + "</svg>")


def verdict_bars(counts: dict) -> str:
    """Confirmations by outcome. One series, so no legend and no palette.

    Horizontal because the labels are blocker codes, and a vertical axis of
    rotated NET_RR_TOO_LOW text is unreadable at any size.
    """
    if not counts:
        return '<p class="empty">no confirmations recorded</p>'
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top = max(counts.values())
    width, row_h = 720, 30
    height = row_h * len(rows) + 8
    label_w, value_w = 196, 44
    track = width - label_w - value_w

    parts = []
    for index, (name, count) in enumerate(rows):
        y = index * row_h + 4
        accepted = name == "ACCEPTED"
        colour = "var(--good)" if accepted else "var(--series1)"
        bar = max(2.0, count / top * track)
        parts.append(
            f'<text x="0" y="{y + 15}" class="cat">{"&#10003; " if accepted else ""}'
            f'{_e(name)}</text>'
            f'<rect x="{label_w}" y="{y + 4}" width="{bar:.1f}" height="16" '
            f'rx="4" fill="{colour}"><title>{_e(name)}: {count}</title></rect>'
            f'<text x="{label_w + bar + 8:.1f}" y="{y + 16}" class="val">{count}</text>')
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="confirmations by outcome">' + "".join(parts) + "</svg>")


def room_bars(records: list) -> str:
    """Target room against the room the 2.5R gate needed, per rejected setup.

    This is the chart that separates "there was no room" from "the stop was too
    wide", because the required figure already contains the stop: it is
    min_net_rr x net risk + costs_win. A short orange bar next to a long blue
    one means the level above was close. Two long bars nearly equal means the
    setup was borderline and the stop is what to look at.
    """
    rows = [r for r in records
            if (r.get("plan") or {}).get("blocker") == "NET_RR_TOO_LOW"
            and (r["plan"].get("evidence") or {}).get("target_room") is not None]
    if not rows:
        return '<p class="empty">no setup was refused on net reward-to-risk</p>'

    width, row_h = 720, 44
    height = row_h * len(rows) + 10
    label_w, value_w = 96, 92
    track = width - label_w - value_w
    biggest = max(max(r["plan"]["evidence"]["target_room"],
                      r["plan"]["evidence"]["required_room_for_min_rr"]) for r in rows) or 1.0

    parts = []
    for index, record in enumerate(rows):
        evidence = record["plan"]["evidence"]
        have = evidence["target_room"]
        need = evidence["required_room_for_min_rr"]
        y = index * row_h + 6
        have_w = max(2.0, have / biggest * track)
        need_w = max(2.0, need / biggest * track)
        parts.append(
            f'<text x="0" y="{y + 15}" class="cat">#{record["index"]}</text>'
            f'<text x="0" y="{y + 30}" class="cat sub">{_num(record["plan"]["net_rr"])}R</text>'
            # 2px surface gap between the two bars keeps them from merging.
            f'<rect x="{label_w}" y="{y + 2}" width="{have_w:.1f}" height="14" rx="4" '
            f'fill="var(--series1)"><title>#{record["index"]} room to target '
            f'{_num(have)} (capped by {_e(evidence.get("target_zone_id") or "?")})</title></rect>'
            f'<rect x="{label_w}" y="{y + 20}" width="{need_w:.1f}" height="14" rx="4" '
            f'fill="var(--series2)"><title>#{record["index"]} room needed for '
            f'{_num(2.5, 1)}R: {_num(need)}</title></rect>'
            f'<text x="{label_w + max(have_w, need_w) + 8:.1f}" y="{y + 14}" '
            f'class="val">{_num(have, 0)}</text>'
            f'<text x="{label_w + max(have_w, need_w) + 8:.1f}" y="{y + 32}" '
            f'class="val sub">needs {_num(need, 0)}</text>')
    legend = ('<div class="legend">'
              '<span><i style="background:var(--series1)"></i>room to the nearest '
              'opposing zone</span>'
              '<span><i style="background:var(--series2)"></i>room the 2.5R gate '
              'required</span></div>')
    return (legend + f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="room to target against room required, per refused setup">'
            + "".join(parts) + "</svg>")


def _median(values: list) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def diagnosis(records: list) -> list:
    """Rank the three candidate causes by how far each is from its own limit.

    Stated as a ratio in each measure's own units so the three are comparable:
    stop distance against the 2.50 ATR ceiling, room against the room the gate
    needed, entry drift against the setup ATR. This is a summary of measured
    values, not a verdict -- the per-setup charts below are what settle it.
    """
    refused = [r for r in records
               if (r.get("plan") or {}).get("blocker") == "NET_RR_TOO_LOW"]
    if not refused:
        return []

    stops = [r["plan"].get("stop_distance_atr") for r in refused]
    drifts = [r["plan"].get("entry_drift_atr") for r in refused]
    shortfalls = [(r["plan"]["evidence"].get("target_room") or 0)
                  / (r["plan"]["evidence"].get("required_room_for_min_rr") or 1)
                  for r in refused if r["plan"].get("evidence")]
    costs = [(r["plan"]["evidence"] or {}).get("cost_share_of_risk") for r in refused]

    median_stop, median_drift = _median(stops), _median(drifts)
    median_short, median_cost = _median(shortfalls), _median(costs)
    n = len(refused)
    out = []
    if median_short is not None:
        out.append(("Room to the next opposing level",
                    f"median {median_short * 100:.0f}% of the room the gate needed (target_room / required_room, n={n})",
                    "The nearest unexpired opposing zone caps the target. If this is "
                    "well under 100%, the structure simply did not offer 2.5R and no "
                    "stop tuning will change that.",
                    median_short < 0.8))
    if median_stop is not None:
        out.append(("Stop width",
                    f"median {median_stop:.2f} ATR15 of a 2.50 ceiling (|E-S| / ATR15, n={n})",
                    "The stop clears the whole zone plus the sweep plus the buffer, so "
                    "a wide zone forces a wide stop and the target has to be that much "
                    "further away.",
                    median_stop > 1.6))
    if median_drift is not None:
        out.append(("Entry drift from the rejection",
                    f"median {median_drift:.2f} ATR15 past the rejection close ((E - rejection close) / ATR15, n={n})",
                    "How far the confirming 5M candle dragged the entry away from the "
                    "candle that defined the setup. A late confirmation widens the stop "
                    "and eats the target room at the same time.",
                    median_drift > 0.5))
    if median_cost is not None:
        out.append(("Cost share of risk",
                    f"median {median_cost * 100:.0f}% of net risk is fees (costs_loss / (|E-S| + costs_loss), n={n})",
                    "Fees are charged on notional, so a tight stop on an expensive "
                    "symbol is uneconomic before the chart is consulted.",
                    median_cost > 0.25))
    return sorted(out, key=lambda row: not row[3])


def _vars(palette: dict) -> str:
    return "".join(f"--{key.replace('_', '-')}:{value};" for key, value in palette.items())


STYLE = """
:root{color-scheme:light;%LIGHT%}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;%DARK%}}
:root[data-theme="dark"]{color-scheme:dark;%DARK%}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
 font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:32px 16px 72px}
h1{font-size:25px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:18px;margin:36px 0 10px;letter-spacing:-.01em}
h3{font-size:15px;margin:0 0 2px}
p{margin:0 0 12px;color:var(--ink2)}
.lede{color:var(--ink2);margin:0 0 20px}
.flag{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;
 font-weight:600;background:var(--warning);color:#0b0b0b;margin-bottom:14px}
.flag.danger{background:var(--critical);color:#fff}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
 padding:18px;margin:0 0 14px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:0 0 8px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.stat b{display:block;font-size:24px;font-weight:600;letter-spacing:-.02em}
.stat span{font-size:12px;color:var(--ink2);display:block}
.stat em{font-size:10.5px;color:var(--muted);font-style:normal;display:block;margin-top:3px;
 line-height:1.35}
.chart{width:100%;height:auto;display:block;overflow:visible}
text{font:11px system-ui,-apple-system,sans-serif}
.cat{fill:var(--ink2);font-size:12px}
.val{fill:var(--ink);font-size:12px;font-variant-numeric:tabular-nums}
.lvl{font-size:11px;font-weight:600;font-variant-numeric:tabular-nums}
.tick{fill:var(--muted);font-size:10px;font-variant-numeric:tabular-nums}
.sub{font-size:10px;opacity:.8}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin:0 0 10px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;
 vertical-align:-1px}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.badge{font-size:12px;font-weight:600;padding:3px 9px;border-radius:999px;white-space:nowrap}
.ok{background:var(--good);color:#fff}
.no{background:var(--critical);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px 16px;
 margin-top:12px;font-size:13px}
.grid div{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid var(--grid);
 padding:4px 0}
.grid span:first-child{color:var(--muted)}
.grid span:last-child{font-variant-numeric:tabular-nums}
.why{border-left:3px solid var(--series2);padding-left:12px;margin:14px 0 0;font-size:13px}
.why.lead{border-left-color:var(--critical)}
.why b{display:block}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid);
 font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left;font-variant-numeric:normal}
th{color:var(--muted);font-weight:600}
details{margin-top:10px}
summary{cursor:pointer;color:var(--ink2);font-size:13px}
.empty{color:var(--muted);font-style:italic;font-size:13px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
@media (max-width:640px){.wrap{padding:20px 16px 56px}h1{font-size:21px}}
"""


def render(audit: dict) -> str:
    meta, summary = audit.get("meta", {}), audit.get("summary", {})
    records = audit.get("confirmations", [])
    accepted = [r for r in records if r["verdict"] == "ACCEPTED"]
    rrs = [(r.get("plan") or {}).get("net_rr") for r in records]
    stops = [(r.get("plan") or {}).get("stop_distance_atr") for r in records]
    drifts = [(r.get("plan") or {}).get("entry_drift_atr") for r in records]

    def _count(values: list) -> int:
        return len([v for v in values if v is not None])

    # Every figure carries its own definition and its own n. A median over
    # three setups and a median over twenty-five are different claims, and a
    # tile that shows only the number invites them to be read as the same one.
    stats = [
        (len(records), "confirmations",
         "setups that reached CONFIRMED"),
        (len(accepted), "reached a plan",
         f"accepted / {len(records)} confirmed"),
        (_num(_median(rrs)), "median net RR",
         f"(|T&minus;E| &minus; costs_win) / (|E&minus;S| + costs_loss), n={_count(rrs)}"),
        (_num(_median(stops)), "median stop, ATR15",
         f"|E&minus;S| / ATR15, n={_count(stops)}"),
        (_num(_median(drifts)), "median entry drift",
         f"(E &minus; rejection close) / ATR15, n={_count(drifts)}"),
    ]
    stat_html = "".join(
        f'<div class="stat"><b>{value}</b><span>{label}</span>'
        f'<em>{formula}</em></div>' for value, label, formula in stats)

    why_html = ""
    for title, figure, note, leading in diagnosis(records):
        why_html += (f'<div class="why{" lead" if leading else ""}"><b>{_e(title)} '
                     f'&mdash; {_e(figure)}</b>{_e(note)}</div>')

    cards = []
    for record in records:
        plan = record.get("plan") or {}
        evidence = plan.get("evidence") or {}
        ok = record["verdict"] == "ACCEPTED"
        fields = [
            ("entry", _num(plan.get("entry_bound"))),
            ("stop", _num(plan.get("stop"))),
            ("target", _num(plan.get("target"))),
            ("net RR", _num(plan.get("net_rr"))),
            ("stop distance", f'{_num(plan.get("stop_distance"))} '
                              f'({_num(plan.get("stop_distance_atr"))} ATR)'),
            ("entry drift", f'{_num(plan.get("entry_drift"))} '
                            f'({_num(plan.get("entry_drift_atr"))} ATR)'),
            ("confirmed on slot", f'{record.get("confirm_slot", "&mdash;")} of 3'),
            ("zone", _e(record["zone"]["id"])),
            ("zone width", _num(record["zone"]["upper"] - record["zone"]["lower"])),
            ("target capped by", _e(evidence.get("target_zone_id") or "&mdash;")),
            ("room to target", _num(evidence.get("target_room"))),
            ("room needed", _num(evidence.get("required_room_for_min_rr"))),
            ("costs, loss path", _num(plan.get("costs_loss"))),
            ("fees as share of risk",
             f'{evidence["cost_share_of_risk"] * 100:.0f}%'
             if evidence.get("cost_share_of_risk") is not None else "&mdash;"),
        ]
        grid = "".join(f'<div><span>{label}</span><span>{value}</span></div>'
                       for label, value in fields)
        cards.append(
            f'<div class="card"><div class="head"><h3>#{record["index"]} '
            f'{_e(record["strategy_id"])} {_e(record["direction"])} '
            f'&middot; {_e(_when(record["at"]))}</h3>'
            f'<span class="badge {"ok" if ok else "no"}">{_e(record["verdict"])}</span></div>'
            f'<p class="lede">regime {_e(record["regime"])} &middot; '
            f'ATR15 {_num(record.get("setup_atr15"))} &middot; zone '
            f'{_num(record["zone"]["lower"])}&ndash;{_num(record["zone"]["upper"])}</p>'
            + candle_chart(record)
            + f'<div class="grid">{grid}</div></div>')

    table_rows = "".join(
        f'<tr><td>#{r["index"]}</td><td>{_e(_when(r["at"]))}</td>'
        f'<td>{_e(r["direction"])}</td><td>{_num((r.get("plan") or {}).get("net_rr"))}</td>'
        f'<td>{_num((r.get("plan") or {}).get("stop_distance_atr"))}</td>'
        f'<td>{_num((r.get("plan") or {}).get("entry_drift_atr"))}</td>'
        f'<td>{_e(r["verdict"])}</td></tr>' for r in records)

    period = f'{_when(meta.get("first_candle", ""))} to {_when(meta.get("last_candle", ""))}'
    # A review built on manufactured candles measures nothing, and once it is a
    # standalone HTML file nothing else on the page says so. The rulebook
    # confines synthetic data to tests for exactly this reason.
    sources = " ".join(str(v) for v in (meta.get("sources") or {}).values()).lower()
    synthetic = any(word in sources for word in ("fixture", "synthetic", "sample"))
    banner = ('<span class="flag danger">Synthetic candles &middot; shape demonstration '
              'only, these numbers measure nothing</span><br>' if synthetic else "")
    style = (STYLE.replace("%LIGHT%", _vars(LIGHT)).replace("%DARK%", _vars(DARK)))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rulebook Setup Review</title>
<style>{style}</style></head><body><div class="wrap">
{banner}<span class="flag">Research replay &middot; no orders were placed</span>
<h1>Price Action rulebook v{_e(meta.get('rulebook_version', '0.1.0'))} &mdash; setup review</h1>
<p class="lede">{_e(meta.get('symbol', ''))} &middot; {_e(period)} &middot;
{_e(', '.join(meta.get('strategies', [])))} &middot;
{summary.get('setups_raised', 0)} setups raised,
{summary.get('confirmations', 0)} confirmed</p>
<div class="stats">{stat_html}</div>

<h2>Outcome of every confirmation</h2>
<div class="card">{verdict_bars(summary.get('blockers_at_confirmation')
                                or _verdict_counts(records))}</div>

<h2>Why net reward-to-risk failed</h2>
<p>Three causes produce the same rejected ratio and have different answers.
Each is measured in its own units below, with the count it was computed over;
the charts decide between them. E is the entry bound, S the stop, T the target,
and ATR15 the previous Wilder ATR on the setup timeframe.</p>
<div class="card">{why_html or '<p class="empty">nothing was refused on net RR</p>'}</div>
<div class="card">{room_bars(records)}</div>

<h2>Every confirmation, on the chart</h2>
<p>Check the coded zone, the rejection candle and the entry against the trade
you meant to take. Correct code can still express the wrong idea.</p>
{''.join(cards) or '<p class="empty">no confirmations in this run</p>'}

<h2>Table view</h2>
<table><thead><tr><th>#</th><th>when</th><th>side</th><th>net RR</th>
<th>stop ATR</th><th>drift ATR</th><th>outcome</th></tr></thead>
<tbody>{table_rows}</tbody></table>

<footer>Generated from a rulebook replay audit.
Sources: {_e(json.dumps(meta.get('sources', {})))}.
The rulebook's own status stands: a research hypothesis, not a proven edge.</footer>
</div></body></html>"""


def _verdict_counts(records: list) -> dict:
    counts: dict = {}
    for record in records:
        counts[record["verdict"]] = counts.get(record["verdict"], 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audit", help="JSON written by pa_rulebook_replay.py --audit")
    parser.add_argument("-o", "--out", default="review.html")
    args = parser.parse_args()

    audit = json.loads(Path(args.audit).read_text())
    Path(args.out).write_text(render(audit))
    count = len(audit.get("confirmations", []))
    print(f"wrote {args.out} ({count} confirmations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
