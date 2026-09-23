#!/usr/bin/env python3
"""Every closed candle the SMC lab judged since its last entry, day by day.

The HTTP API returns only the newest 500 candles (about 42 hours of 5m), so a
question about the whole week since a trade needs the lab's own records. This
opens the two SQLite files READ-ONLY (sqlite "mode=ro"): it cannot write,
place, cancel or change anything, and it does not import or start the app.

    cd /opt/nexus-trading-bot && python3 scripts/smc_week_report.py

The databases live in the app's Docker volume, so from a host shell the
script re-runs itself inside the app container. It answers:

  * how many closed candles the lab judged each day, against how many closed
  * the gaps where it judged nothing (app stopped, or feed down)
  * whether any candle reached ENTRY_READY
  * the near misses: candles with only one or two conditions missing
  * what the agent recorded, and since when
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DATA_DIR = os.environ.get("HUB_DATA_DIR", "/var/lib/tradexa")
LAB_DB = os.environ.get("HUB_SMC_PAPER_DB", os.path.join(DATA_DIR, "smc_strategy_paper.db"))
AGENT_DB = os.environ.get("HUB_SMC_AGENT_JOURNAL_DB", os.path.join(DATA_DIR, "smc_agent_journal.db"))
IN_CONTAINER = "SMC_WEEK_IN_CONTAINER"
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _short(moment: datetime | None) -> str:
    return moment.strftime("%m-%d %H:%M") if moment else "?"


def _read_only(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _rerun_in_app_container() -> int | None:
    if os.environ.get(IN_CONTAINER):
        return None
    here = os.path.abspath(__file__)
    if not os.path.isfile(here):
        return None
    print(f"({LAB_DB} is inside the app's Docker volume; reading it from the app container)",
          flush=True)
    try:
        with open(here, "rb") as source:
            return subprocess.run(
                ["docker", "compose", "exec", "-T", "-e", f"{IN_CONTAINER}=1", "app", "python", "-"],
                stdin=source, cwd=os.path.dirname(os.path.dirname(here)), check=False).returncode
    except OSError:
        return None


def _step(timeframe: str) -> timedelta:
    return timedelta(minutes=TF_MINUTES.get(str(timeframe), 5))


def _closes(start: datetime, end: datetime, timeframe: str) -> int:
    """How many candles of this timeframe closed in [start, end).

    Half-open like the rows it is compared with: a candle closing at exactly
    00:00 is judged seconds later, so it belongs to the day that starts then.
    """
    seconds = int(_step(timeframe).total_seconds())
    return max(0, -(-int(end.timestamp()) // seconds) - -(-int(start.timestamp()) // seconds))


def _on_grid(candle: datetime, timeframe: str) -> bool:
    return not candle.second and not candle.microsecond and \
        int(candle.timestamp()) % int(_step(timeframe).total_seconds()) == 0


def report(lab: sqlite3.Connection, agent: sqlite3.Connection | None,
           now: datetime) -> tuple[list[str], list[str]]:
    """Return (report lines, verdict lines). Reads only.

    One clock throughout: the time each row was WRITTEN. The lab is live, so
    it judges a candle seconds after it closes, and configuration changes are
    stamped on the same clock. Candle times are only used to check each row
    against its own timeframe's grid.
    """
    lines: list[str] = []
    verdict: list[str] = []
    latest = lab.execute(
        "SELECT session_id, symbol FROM smc_evaluations ORDER BY created_at DESC LIMIT 1").fetchone()
    if latest is None:
        return ["The SMC lab has not judged a single closed candle yet."], \
            ["NOTHING JUDGED: the lab has never evaluated a candle, so it could not trade."]
    session, symbol = latest["session_id"], latest["symbol"]
    saved = lab.execute("SELECT timeframe FROM smc_sessions WHERE id=?", (session,)).fetchone()
    current_tf = str((saved["timeframe"] if saved else None) or "5m")

    entry = lab.execute(
        "SELECT created_at, creation_candle, direction, entry, stop, status FROM smc_order_meta "
        "WHERE session_id=? AND ownership='strategy' ORDER BY created_at DESC LIMIT 1",
        (session,)).fetchone()
    evaluations = [row for row in lab.execute(
        "SELECT candle_time, timeframe, state, missing_conditions_json, created_at "
        "FROM smc_evaluations WHERE session_id=?", (session,))
        if _when(row["created_at"]) is not None and _when(row["candle_time"]) is not None]
    since = (_when(entry["created_at"]) if entry else None) or \
        min((_when(row["created_at"]) for row in evaluations), default=now)
    lines.append(f"== {symbol}  session {session[:8]}  (trading {current_tf} now) ==")
    if entry:
        lines.append(f"  last entry: {_short(since)} UTC  {entry['direction']} "
                     f"entry={entry['entry']} stop={entry['stop']}  status={entry['status']}")
    else:
        lines.append("  no entry in this session; reporting since its first judged candle")

    # --- which timeframe was active when ---------------------------------------
    # The chart's timeframe buttons change the TRADING session, so one session
    # can hold several candle series. Each stretch is measured on its own grid.
    changes = sorted(
        ((when, json.loads(row["payload"] or "{}")) for row in lab.execute(
            "SELECT created_at, payload FROM smc_activity "
            "WHERE session_id=? AND kind='session_configuration_changed'", (session,))
         if (when := _when(row["created_at"])) is not None),
        key=lambda item: item[0])
    before = [change for when, change in changes if when <= since and change.get("timeframe")]
    after_entry = sorted((row for row in evaluations if _when(row["created_at"]) > since),
                         key=lambda row: str(row["created_at"]))
    active = str(before[-1]["timeframe"] if before else
                 (after_entry[0]["timeframe"] if after_entry else current_tf))
    spans: list[tuple[datetime, datetime, str]] = []
    cursor = since
    for when, change in changes:
        timeframe = str(change.get("timeframe") or active)
        if when <= since or timeframe == active:
            continue
        spans.append((cursor, when, active))
        cursor, active = when, timeframe
    spans.append((cursor, now, active))

    def active_at(moment: datetime) -> str:
        return next((tf for start, stop, tf in spans if start <= moment < stop), spans[-1][2])

    # --- one row per (timeframe, candle) -------------------------------------
    by_candle: dict[tuple[str, datetime], list] = defaultdict(list)
    for row in after_entry:
        by_candle[(row["timeframe"], _when(row["candle_time"]))].append(row)
    repeated = {key: rows for key, rows in by_candle.items() if len(rows) > 1}
    off_grid = [key for key in by_candle if not _on_grid(key[1], key[0])]
    future = [key for key in by_candle if key[1] > now]
    judged = sorted(
        ((_when(rows[0]["created_at"]), key[0], key[1], rows[0]["state"],
          json.loads(rows[0]["missing_conditions_json"] or "[]"))
         for key, rows in by_candle.items()),
        key=lambda item: item[0])
    several = len({tf for _w, tf, _c, _s, _m in judged}) > 1

    # --- coverage per day ------------------------------------------------------
    lines.append("")
    lines.append("== CLOSED CANDLES JUDGED PER DAY (UTC) ==")
    lines.append("  day          judged / closed   timeframes   signals   closest candle (fewest missing)")
    day = since.replace(hour=0, minute=0, second=0, microsecond=0)
    total_closed = 0
    while day <= now:
        day_end = day + timedelta(days=1)
        closed = sum(_closes(max(start, day), min(stop, day_end), tf)
                     for start, stop, tf in spans if start < day_end and stop > day)
        total_closed += closed
        today = [item for item in judged if day <= item[0] < day_end]
        frames = sorted({tf for start, stop, tf in spans if start < day_end and stop > day},
                        key=lambda tf: TF_MINUTES.get(tf, 0))
        signals = sum(1 for item in today if item[3] != "WATCHING")
        best = min(today, key=lambda item: len(item[4]), default=None)
        closest = (f"{best[2].strftime('%H:%M')} {best[1]} missing {len(best[4])}: "
                   f"{', '.join(best[4]) or '-'}" if best else "-")
        lines.append(f"  {day.strftime('%Y-%m-%d')}   {len(today):5d} / {closed:<5d}   "
                     f"{','.join(frames):<10s}   {signals:7d}   {closest[:100]}")
        day = day_end

    # --- where it judged nothing -------------------------------------------------
    gaps = []
    previous = since
    for written, *_rest in judged + [(now,)]:
        allowed = max(_step(active_at(previous)), _step(active_at(written))) * 3
        if written - previous > allowed:
            gaps.append((previous, written))
        previous = written
    dark = sum(((stop - start) for start, stop in gaps), timedelta())
    lines.append("")
    lines.append(f"== GAPS WITH NO JUDGED CANDLE (> 3 candles of the active timeframe): "
                 f"{len(gaps)} gaps, {dark.total_seconds() / 3600:.1f} hours in total ==")
    for start, stop in sorted(gaps, key=lambda gap: gap[1] - gap[0], reverse=True)[:10]:
        lines.append(f"  {_short(start)} -> {_short(stop)} UTC   "
                     f"{(stop - start).total_seconds() / 3600:.1f} h   ({active_at(start)})")

    # --- how close it came -----------------------------------------------------
    signals = [item for item in judged if item[3] != "WATCHING"]
    near = [item for item in judged if item[3] == "WATCHING" and len(item[4]) <= 2]
    blocking = Counter(condition for item in judged for condition in item[4])
    lines.append("")
    lines.append(f"== NEAR MISSES (1-2 conditions missing): {len(near)} ==")
    for _written, tf, candle, _state, missing in near[-10:]:
        lines.append(f"  {_short(candle)} UTC{' ' + tf if several else ''}  missing: {', '.join(missing)}")
    lines.append("")
    lines.append("== CONDITIONS MISSING, ALL CANDLES ==")
    for condition, count in blocking.most_common(8):
        lines.append(f"  {count:5d} / {len(judged)}  {condition}")

    lines.append("")
    lines.append(f"== DATA CHECK: {len(repeated)} candle(s) judged more than once, "
                 f"{len(off_grid)} off their timeframe's grid, {len(future)} in the future ==")
    for key in sorted(repeated, key=lambda key: key[1])[-8:]:
        lines.append(f"  {_short(key[1])} UTC {key[0]} judged {len(repeated[key])}x, written "
                     + " | ".join(str(row["created_at"])[11:19] for row in repeated[key]))
    for tf, candle in sorted(off_grid, key=lambda key: key[1])[-5:]:
        lines.append(f"  off-grid: {tf} {candle.isoformat()}")
    for tf, candle in sorted(future, key=lambda key: key[1])[-5:]:
        lines.append(f"  future: {tf} {candle.isoformat()}")

    lines.append("")
    recent = [(when, change) for when, change in changes if when > since]
    lines.append(f"== SESSION CONFIGURATION CHANGES SINCE THE ENTRY: {len(recent)} ==")
    for when, change in recent[-20:]:
        lines.append(f"  {_short(when)} UTC  {change.get('symbol')} {change.get('timeframe')}  "
                     f"mode={change.get('operating_mode')}  model={change.get('model_id')}  "
                     f"risk={change.get('risk_pct')}%")
    lines.append("  timeframe in force: " + "  ->  ".join(
        f"{tf} from {_short(start)}" for start, _stop, tf in spans))

    candidates = Counter(row["status"] for row in lab.execute(
        "SELECT status, created_at FROM smc_candidates WHERE session_id=?", (session,))
        if (_when(row["created_at"]) or since) > since)
    lines.append("")
    lines.append(f"== CANDIDATES STAGED FOR THE AGENT SINCE: {dict(candidates) or 'none'} ==")

    first_agent = None
    if agent is not None:
        decisions = [row for row in agent.execute(
            "SELECT at, timeframe, outcome FROM agent_decisions WHERE symbol=?", (symbol,))
            if _when(row["at"]) is not None]
        first_agent = min((_when(row["at"]) for row in decisions), default=None)
        last_agent = max((_when(row["at"]) for row in decisions), default=None)
        outcomes = Counter(f"{row['timeframe']} {row['outcome']}" for row in decisions
                           if _when(row["at"]) > since)
        lines.append(f"== AGENT: first decision {_short(first_agent)} UTC, last "
                     f"{_short(last_agent)} UTC; since the entry: {dict(outcomes) or 'none'} =="
                     if decisions else "== AGENT: no decisions recorded for this symbol ==")

    # --- verdict -----------------------------------------------------------------
    coverage = (len(judged) / total_closed * 100) if total_closed else 0.0
    if not signals:
        verdict.append(
            f"NO SETUP IN THE WHOLE PERIOD: {len(judged)} closed candles judged since {_short(since)} "
            f"UTC, and not one reached ENTRY_READY. There was nothing for the agent to take.")
    else:
        verdict.append(f"{len(signals)} candle(s) produced a signal since the entry; "
                       f"see CANDIDATES and AGENT above for what happened to them.")
    verdict.append(
        f"The lab judged {len(judged)} of the {total_closed} candles that closed ({coverage:.0f}%)"
        + (f"; for {dark.total_seconds() / 3600:.1f} hours it judged nothing (app stopped or feed "
           "down), so a setup in those hours could not have been seen." if dark > timedelta(hours=1)
           else "."))
    if first_agent and first_agent > since:
        verdict.append(f"The agent's first recorded decision is {_short(first_agent)} UTC, after the "
                       "entry: before that the agent was not deciding for this lab.")
    if near:
        verdict.append(f"{len(near)} candle(s) came within one or two conditions of a setup.")
    if len(spans) > 1:
        verdict.append(
            f"The trading timeframe changed {len(spans) - 1} time(s) in this period (see "
            "SESSION CONFIGURATION CHANGES). On the SMC Strategy Lab page the chart's timeframe "
            "buttons change what the bot TRADES, not just the chart.")
    if repeated or off_grid or future:
        verdict.append(
            f"DATA CHECK FAILED: {len(repeated)} candle(s) were judged more than once, "
            f"{len(off_grid)} sit off their timeframe's grid and {len(future)} are in the future. "
            "See DATA CHECK. Entries are keyed by proposal, so a repeat cannot place a second order.")
    return lines, verdict


def main() -> int:
    if not os.path.exists(LAB_DB):
        rerun = _rerun_in_app_container()
        if rerun is not None:
            return rerun
        print(f"FAIL: {LAB_DB} not found (set HUB_SMC_PAPER_DB to its path)")
        return 2
    lab = _read_only(LAB_DB)
    agent = _read_only(AGENT_DB) if os.path.exists(AGENT_DB) else None
    try:
        lines, verdict = report(lab, agent, datetime.now(timezone.utc))
    finally:
        lab.close()
        if agent is not None:
            agent.close()
    print("\n".join(lines))
    print("\n== VERDICT ==")
    for line in verdict:
        print("  * " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
