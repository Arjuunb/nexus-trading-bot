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


def report(lab: sqlite3.Connection, agent: sqlite3.Connection | None,
           now: datetime) -> tuple[list[str], list[str]]:
    """Return (report lines, verdict lines). Reads only."""
    lines: list[str] = []
    verdict: list[str] = []
    latest = lab.execute(
        "SELECT session_id, symbol, timeframe FROM smc_evaluations "
        "ORDER BY candle_time DESC LIMIT 1").fetchone()
    if latest is None:
        return ["The SMC lab has not judged a single closed candle yet."], \
            ["NOTHING JUDGED: the lab has never evaluated a candle, so it could not trade."]
    session, symbol, timeframe = latest["session_id"], latest["symbol"], latest["timeframe"]
    saved = lab.execute("SELECT timeframe FROM smc_sessions WHERE id=?", (session,)).fetchone()
    # The session's saved timeframe, not the latest row's: the chart's
    # timeframe buttons change the SESSION, so rows of several timeframes can
    # sit in one session and must not be counted as one candle series.
    timeframe = (saved["timeframe"] if saved else None) or timeframe
    step = timedelta(minutes=TF_MINUTES.get(str(timeframe), 5))

    entry = lab.execute(
        "SELECT created_at, creation_candle, direction, entry, stop, status FROM smc_order_meta "
        "WHERE session_id=? AND ownership='strategy' ORDER BY created_at DESC LIMIT 1",
        (session,)).fetchone()
    since = _when(entry["creation_candle"]) if entry else None
    since = since or (_when(entry["created_at"]) if entry else None)
    if since is None:
        first = lab.execute("SELECT MIN(candle_time) AS t FROM smc_evaluations WHERE session_id=?",
                            (session,)).fetchone()
        since = _when(first["t"]) or now
    lines.append(f"== {symbol} {timeframe}  session {session[:8]} ==")
    if entry:
        lines.append(f"  last entry: candle {_short(since)} UTC  {entry['direction']} "
                     f"entry={entry['entry']} stop={entry['stop']}  status={entry['status']}")
    else:
        lines.append("  no entry in this session; reporting since its first judged candle")

    # Times are compared as datetimes here, never as text in SQL: a stored
    # "2026-09-16 18:35" sorts before "2026-09-16T18:30" and would vanish.
    other_timeframes: Counter = Counter()
    recorded = []
    for row in lab.execute(
            "SELECT candle_time, timeframe, state, missing_conditions_json, created_at "
            "FROM smc_evaluations WHERE session_id=?", (session,)):
        moment = _when(row["candle_time"])
        if moment is None or moment <= since:
            continue
        if row["timeframe"] != timeframe:
            other_timeframes[row["timeframe"]] += 1
            continue
        recorded.append((moment, row["state"], json.loads(row["missing_conditions_json"] or "[]"),
                         str(row["candle_time"]), str(row["created_at"])))
    recorded.sort(key=lambda item: (item[0], item[4]))
    # One judgement per candle is the lab's own rule (its idempotency key is
    # the candle time). The key is the timestamp TEXT, so the same candle
    # written in two formats would be judged twice; count candles, not rows,
    # and report any repeat instead of letting it inflate the table.
    by_candle: dict[datetime, list] = defaultdict(list)
    for item in recorded:
        by_candle[item[0]].append(item)
    rows = [(moment, *items[0][1:3]) for moment, items in sorted(by_candle.items())]
    repeated = {moment: items for moment, items in by_candle.items() if len(items) > 1}
    minutes = int(step.total_seconds() // 60)
    off_grid = [moment for moment in by_candle
                if moment.second or moment.microsecond or (moment.minute % minutes if minutes < 60 else moment.minute)]
    future = [moment for moment in by_candle if moment > now]

    # --- coverage per day ------------------------------------------------------
    per_day: dict[str, list] = defaultdict(list)
    for moment, state, missing in rows:
        per_day[moment.strftime("%Y-%m-%d")].append((moment, state, missing))
    lines.append("")
    lines.append("== CLOSED CANDLES JUDGED PER DAY (UTC) ==")
    lines.append("  day          judged / closed   repeats   signals   closest candle (fewest conditions missing)")
    day = since.replace(hour=0, minute=0, second=0, microsecond=0)
    total_closed = 0
    while day <= now:
        start, end = max(day, since), min(day + timedelta(days=1), now)
        closed = max(0, int((end - start) / step))
        total_closed += closed
        judged = per_day.get(day.strftime("%Y-%m-%d"), [])
        signals = sum(1 for _m, state, _x in judged if state != "WATCHING")
        best = min(judged, key=lambda item: len(item[2]), default=None)
        closest = (f"{best[0].strftime('%H:%M')} missing {len(best[2])}: {', '.join(best[2]) or '-'}"
                   if best else "-")
        repeats = sum(1 for moment in repeated if moment.strftime("%Y-%m-%d") == day.strftime("%Y-%m-%d"))
        lines.append(f"  {day.strftime('%Y-%m-%d')}   {len(judged):5d} / {closed:<5d}   {repeats:7d}   "
                     f"{signals:7d}   {closest[:100]}")
        day += timedelta(days=1)

    # --- where it judged nothing -------------------------------------------------
    gaps = []
    previous = since
    for moment, _state, _missing in rows + [(now, "", [])]:
        if moment - previous > step * 3:
            gaps.append((previous, moment))
        previous = moment
    dark = sum(((end - start) for start, end in gaps), timedelta())
    lines.append("")
    lines.append(f"== GAPS WITH NO JUDGED CANDLE (> {3 * step.seconds // 60} min): "
                 f"{len(gaps)} gaps, {dark.total_seconds() / 3600:.1f} hours in total ==")
    for start, end in sorted(gaps, key=lambda gap: gap[1] - gap[0], reverse=True)[:10]:
        lines.append(f"  {_short(start)} -> {_short(end)} UTC   "
                     f"{(end - start).total_seconds() / 3600:.1f} h")

    # --- how close it came -----------------------------------------------------
    signals = [(m, s) for m, s, _x in rows if s != "WATCHING"]
    near = [(m, x) for m, s, x in rows if s == "WATCHING" and len(x) <= 2]
    blocking = Counter(item for _m, _s, missing in rows for item in missing)
    lines.append("")
    lines.append(f"== NEAR MISSES (1-2 conditions missing): {len(near)} ==")
    for moment, missing in near[-10:]:
        lines.append(f"  {_short(moment)} UTC  missing: {', '.join(missing)}")
    lines.append("")
    lines.append("== CONDITIONS MISSING, ALL CANDLES ==")
    for condition, count in blocking.most_common(8):
        lines.append(f"  {count:5d} / {len(rows)}  {condition}")

    staged_after = (_when(entry["created_at"]) if entry else None) or since
    lines.append("")
    lines.append(f"== DATA CHECK: {len(repeated)} candle(s) judged more than once, "
                 f"{len(off_grid)} off the {minutes}m grid, {len(future)} in the future ==")
    for moment in sorted(repeated)[-8:]:
        lines.append(f"  {_short(moment)} UTC judged {len(repeated[moment])}x as: "
                     + " | ".join(f"{item[3]} (written {item[4][11:19]})" for item in repeated[moment]))
    for moment in sorted(off_grid)[-5:]:
        lines.append(f"  off-grid: {moment.isoformat()}")
    for moment in sorted(future)[-5:]:
        lines.append(f"  future: {moment.isoformat()}")

    lines.append("")
    lines.append(f"== OTHER TIMEFRAMES JUDGED IN THIS SESSION SINCE: "
                 f"{dict(other_timeframes) or 'none'} ==")
    changed_after = (_when(entry["created_at"]) if entry else None) or since
    changes = [row for row in lab.execute(
        "SELECT created_at, payload FROM smc_activity "
        "WHERE session_id=? AND kind='session_configuration_changed'", (session,))
        if (_when(row["created_at"]) or changed_after) > changed_after]
    changes.sort(key=lambda row: str(row["created_at"]))
    lines.append(f"== SESSION CONFIGURATION CHANGES SINCE THE ENTRY: {len(changes)} ==")
    for row in changes[-20:]:
        change = json.loads(row["payload"] or "{}")
        lines.append(f"  {_short(_when(row['created_at']))} UTC  {change.get('symbol')} "
                     f"{change.get('timeframe')}  mode={change.get('operating_mode')}  "
                     f"model={change.get('model_id')}  risk={change.get('risk_pct')}%")

    candidates = Counter(row["status"] for row in lab.execute(
        "SELECT status, created_at FROM smc_candidates WHERE session_id=?", (session,))
        if (_when(row["created_at"]) or staged_after) > staged_after)
    lines.append("")
    lines.append(f"== CANDIDATES STAGED FOR THE AGENT SINCE: {dict(candidates) or 'none'} ==")

    first_agent = None
    if agent is not None:
        # Candle time, like everything else compared with the entry candle.
        span = agent.execute(
            "SELECT MIN(candle_time) AS first, MAX(candle_time) AS last, COUNT(*) AS n "
            "FROM agent_decisions WHERE symbol=? AND timeframe=?", (symbol, timeframe)).fetchone()
        first_agent = _when(span["first"])
        outcomes = Counter(row["outcome"] for row in agent.execute(
            "SELECT outcome, candle_time FROM agent_decisions WHERE symbol=? AND timeframe=?",
            (symbol, timeframe))
            if (_when(row["candle_time"]) or since) > since)
        lines.append(f"== AGENT: first decision {_short(first_agent)} UTC, last "
                     f"{_short(_when(span['last']))} UTC; since the entry: {dict(outcomes) or 'none'} ==")

    # --- verdict -----------------------------------------------------------------
    coverage = (len(rows) / total_closed * 100) if total_closed else 0.0
    if not signals:
        verdict.append(
            f"NO SETUP IN THE WHOLE PERIOD: {len(rows)} closed candles judged since {_short(since)} "
            f"UTC, and not one reached ENTRY_READY. There was nothing for the agent to take.")
    else:
        verdict.append(f"{len(signals)} candle(s) produced a signal since the entry; "
                       f"see CANDIDATES and AGENT above for what happened to them.")
    if dark > step * 12:
        verdict.append(
            f"The lab judged {coverage:.0f}% of the {total_closed} candles that closed. For "
            f"{dark.total_seconds() / 3600:.1f} hours it judged nothing (app stopped or feed down), "
            "so any setup in those hours could not have been seen.")
    if first_agent and first_agent > since:
        verdict.append(f"The agent's first recorded decision is {_short(first_agent)} UTC, after the "
                       "entry: before that the agent was not deciding for this lab.")
    if near:
        verdict.append(f"{len(near)} candle(s) came within one or two conditions of a setup.")
    if other_timeframes:
        verdict.append(
            "The session was switched to another timeframe for part of this period "
            f"({', '.join(f'{count} {name} candles' for name, count in other_timeframes.items())}). "
            "On the SMC Strategy Lab page the chart's timeframe buttons change the TRADING "
            "session, not just the chart. See SESSION CONFIGURATION CHANGES for when.")
    if repeated or off_grid or future:
        verdict.append(
            f"DATA CHECK FAILED: {len(repeated)} candle(s) were judged more than once, {len(off_grid)} "
            f"sit off the {minutes}m grid and {len(future)} are in the future. See DATA CHECK for the "
            "raw timestamps. Entries are keyed by proposal, so a repeat cannot place a second order.")
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
