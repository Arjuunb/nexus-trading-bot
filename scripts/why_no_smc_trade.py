#!/usr/bin/env python3
"""Why has the SMC Strategy Lab not placed another order?

Read-only: four GET requests to the running server. It places, cancels,
approves and changes nothing. Standard library only, so it runs on the host
without the app's virtualenv.

    cd /opt/nexus-trading-bot && python3 scripts/why_no_smc_trade.py

The app container publishes nothing to the host (compose says "expose: 8000",
not "ports"), so from a host shell 127.0.0.1:8000 is refused. When that
happens the script re-runs itself inside the app container, where the port is
reachable -- the same transport scripts/triage_paper_orders.sh uses. Set
HUB_URL to skip that, e.g. HUB_URL=https://trade-logx.com from a workstation.

Either way it only makes HTTP requests to the running server. It never
imports the app: a "docker compose exec app python" that imports webhook_api
builds a fresh process with its own state, and reading that process has
already produced a wrong diagnosis once.

Every possible answer comes from a rule in the code, checked in this order:

  1. the lab is BLOCKED               (bot-status blockers)
  2. the session cannot place orders  (signals_only mode)
  3. the last trade is still open     (one SMC position per symbol; the lab
                                       refuses same-symbol stacking)
  4. an entry order is still pending  (same rule)
  5. no new setup since the last one  (every closed candle NOT ENTRY_READY)
  6. a setup came and was not taken   (agent REJECTED / MISSED, or the lab
                                       refused the candidate) -- with reasons
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE = os.environ.get("HUB_URL", "http://127.0.0.1:8000").rstrip("/")
IN_CONTAINER = "SMC_PROBE_IN_CONTAINER"
OPEN_ORDER_STATUSES = {"open", "partially_filled", "triggered"}


def _control_key() -> str:
    key = os.environ.get("HUB_CONTROL_KEY", "")
    if key:
        return key
    for path in (".env", "/opt/nexus-trading-bot/.env"):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("HUB_CONTROL_KEY="):
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return ""


def get(path: str) -> dict:
    request = urllib.request.Request(
        BASE + path, headers={"x-webhook-secret": _control_key()})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _short(value) -> str:
    moment = _when(value)
    return moment.strftime("%m-%d %H:%M UTC") if moment else str(value or "?")


def _strategy_state(row: dict) -> str:
    source = ((row.get("payload") or {}).get("source_evaluation") or {})
    return str(source.get("state") or row.get("state") or "?")


def diagnose(status: dict, agent: dict, paper: dict, policy: dict) -> tuple[list[str], list[str]]:
    """Return (report lines, verdict lines). Pure: no I/O."""
    lines: list[str] = []
    verdict: list[str] = []
    mode = str(status.get("operating_mode") or (paper.get("session") or {}).get("operating_mode") or "?")
    blockers = list(status.get("blockers") or agent.get("blockers") or [])
    feed = status.get("feed") or agent.get("feed") or {}

    lines.append("== LAB ==")
    lines.append(f"  {status.get('symbol') or agent.get('symbol')} "
                 f"{status.get('timeframe') or agent.get('timeframe')}  mode={mode}  "
                 f"execution_state={status.get('execution_state') or agent.get('execution_state')}")
    lines.append(f"  feed={feed.get('state')} reliable={feed.get('reliable')}  "
                 f"blockers={blockers or 'none'}")

    # --- every entry this session has placed ---------------------------------
    entries = sorted(
        [row for row in (paper.get("order_metadata") or []) if row.get("ownership") == "strategy"],
        key=lambda row: str(row.get("created_at") or ""))
    agent_trades = sorted(agent.get("trades") or [], key=lambda row: str(row.get("opened_at") or ""))
    lines.append("")
    lines.append(f"== ENTRIES PLACED THIS SESSION: {len(entries)} ==")
    for row in entries:
        lines.append(f"  {_short(row.get('created_at'))}  {row.get('direction')}  "
                     f"entry={row.get('entry')} stop={row.get('stop')} "
                     f"T2={row.get('target_2')}  status={row.get('status')}")
    if agent_trades:
        lines.append("  agent journal:")
        for row in agent_trades:
            closed = (f"closed {_short(row.get('closed_at'))} {row.get('result')} "
                      f"{row.get('realised_r')}R ({row.get('close_reason')})"
                      if row.get("closed_at") else "STILL OPEN in the journal")
            lines.append(f"    opened {_short(row.get('opened_at'))} {row.get('direction')} "
                         f"size={row.get('size')}  {closed}")

    # Two clocks: when the order was written (wall time) and the closed candle
    # it came from (market time). Candles are compared with candles, rows with
    # rows -- mixing them miscounts anything replayed or restored late.
    last_entry = max([_when(row.get("created_at")) for row in entries if _when(row.get("created_at"))]
                     + [_when(row.get("opened_at")) for row in agent_trades if _when(row.get("opened_at"))],
                     default=None)
    last_entry_candle = max([_when(row.get("creation_candle")) for row in entries
                             if _when(row.get("creation_candle"))], default=None) or last_entry

    # --- what is open right now ---------------------------------------------
    positions = list(paper.get("positions") or status.get("positions") or [])
    pending = [row for row in (paper.get("orders") or [])
               if str(row.get("status")) in OPEN_ORDER_STATUSES and not row.get("reduce_only")]
    lines.append("")
    lines.append(f"== OPEN NOW: {len(positions)} position(s), {len(pending)} pending entry order(s) ==")
    for row in positions:
        lines.append(f"  POSITION {row.get('symbol')} {row.get('side')} size={row.get('size')} "
                     f"entry={row.get('entry_price')} stop={row.get('stop_loss')} "
                     f"target={row.get('take_profit')}")
    for row in pending:
        lines.append(f"  ORDER {row.get('id')} {row.get('side')} {row.get('type')} "
                     f"status={row.get('status')} created={_short(row.get('created_at'))}")

    # --- what the strategy saw since the last entry ---------------------------
    evaluations = sorted(paper.get("evaluations") or [], key=lambda row: str(row.get("candle_time") or ""))
    since = [row for row in evaluations
             if last_entry_candle is None
             or (_when(row.get("candle_time")) or last_entry_candle) > last_entry_candle]
    states = Counter(_strategy_state(row) for row in since)
    ready = [row for row in since if _strategy_state(row) == "ENTRY_READY"]
    missing = Counter(str(item) for row in since for item in (row.get("missing_conditions") or []))
    label = f"since the last entry ({_short(last_entry)})" if last_entry else "in this session"
    lines.append("")
    lines.append(f"== STRATEGY {label.upper()}: {len(since)} closed candles evaluated ==")
    if evaluations and last_entry_candle and \
            (_when(evaluations[0].get("candle_time")) or last_entry_candle) > last_entry_candle:
        lines.append("  (the server returns the newest 500 candles; older ones are not shown)")
    for state, count in states.most_common():
        lines.append(f"  {state}: {count}")
    if missing:
        lines.append("  most often missing:")
        for condition, count in missing.most_common(6):
            lines.append(f"    {count:4d} x {condition}")
    for row in since[-5:]:
        lines.append(f"  last: {_short(row.get('candle_time'))}  {_strategy_state(row)}  "
                     f"missing={row.get('missing_conditions') or []}")

    # --- what the agent and the lab did with the setups ----------------------
    def after_entry(row: dict) -> bool:
        if last_entry is None:
            return True
        candle = _when(row.get("candle_time"))
        if candle is not None and last_entry_candle is not None:
            return candle > last_entry_candle
        return (_when(row.get("at")) or last_entry) > last_entry

    decisions = [row for row in (agent.get("decisions") or []) if after_entry(row)]
    acted = [row for row in decisions if row.get("outcome") not in ("NOT_READY",)]
    lines.append("")
    lines.append(f"== AGENT DECISIONS {label.upper()}: "
                 f"{dict(Counter(str(row.get('outcome')) for row in decisions)) or 'none'} ==")
    for row in sorted(acted, key=lambda row: str(row.get("at") or ""))[-10:]:
        lines.append(f"  {_short(row.get('at'))}  {row.get('outcome')}  "
                     f"{row.get('reason_code')}: {row.get('reason')}")
    candidates = [row for row in (paper.get("candidates") or [])
                  if last_entry is None or (_when(row.get("created_at")) or last_entry) > last_entry]
    if candidates:
        lines.append("  lab candidates:")
        for row in sorted(candidates, key=lambda row: str(row.get("created_at") or ""))[-10:]:
            lines.append(f"    {_short(row.get('created_at'))}  {row.get('status')}: {row.get('reason')}")

    context = (policy or {}).get("context") or {}
    memory = (policy or {}).get("memory") or {}
    lines.append("")
    lines.append(f"== AGENT RULES == context={'ON' if context.get('enabled') else 'off'} "
                 f"memory={'ON' if memory.get('enabled') else 'off'}")
    if context.get("enabled"):
        lines.append(f"  context: {json.dumps(context, sort_keys=True)}")
    if memory.get("enabled"):
        lines.append(f"  memory: {json.dumps(memory, sort_keys=True)}")

    # --- verdict, in the order the code applies these rules -------------------
    if blockers:
        verdict.append(f"BLOCKED: {'; '.join(map(str, blockers))}. Nothing trades until this clears.")
    if mode == "signals_only":
        verdict.append("The session is in Signals-only mode. It records signals and can never place an order.")
    if positions:
        verdict.append("Your last trade is STILL OPEN. The SMC lab holds one position per symbol "
                       "and refuses a second entry until this one exits at its stop or target "
                       "(so a new order cannot overwrite the open trade's stop and targets).")
    if pending:
        verdict.append("An entry order is still pending. No new entry until it fills or is cancelled.")
    if not (blockers or positions or pending or mode == "signals_only"):
        if not ready:
            top = ", ".join(f"{name} ({count}x)" for name, count in missing.most_common(3))
            verdict.append(
                f"NO NEW SETUP: the strategy evaluated {len(since)} closed candles {label} and "
                "none reached ENTRY_READY. The strategy is waiting for a full setup; nothing is "
                "blocking it." + (f" Most often missing: {top}." if top else ""))
        else:
            reasons = Counter(f"{row.get('outcome')} {row.get('reason_code')}" for row in acted)
            verdict.append(
                f"{len(ready)} setup(s) reached ENTRY_READY {label} but none became an order. "
                "Agent outcomes: " + (", ".join(f"{k} x{v}" for k, v in reasons.most_common())
                                      or "none recorded")
                + ". See AGENT DECISIONS above for each reason.")
            if mode == "automatic":
                verdict.append("Mode is automatic: the lab places orders itself and the agent stands "
                               "down, so check the lab candidates' status/reason above.")
    return lines, verdict


def _rerun_in_app_container() -> int | None:
    """Run this same file inside the app container; None if that is not possible."""
    if os.environ.get(IN_CONTAINER) or "HUB_URL" in os.environ:
        return None
    here = os.path.abspath(__file__)
    if not os.path.isfile(here):
        return None
    repo = os.path.dirname(os.path.dirname(here))
    print(f"({BASE} is not published on this host; asking from inside the app container)",
          flush=True)
    try:
        with open(here, "rb") as source:
            return subprocess.run(
                ["docker", "compose", "exec", "-T", "-e", f"{IN_CONTAINER}=1",
                 "app", "python", "-"],
                stdin=source, cwd=repo, check=False).returncode
    except OSError:
        return None


def main() -> int:
    try:
        agent = get("/research/smc/agent?limit=500")
        paper = get("/research/smc/paper")
    except urllib.error.HTTPError as exc:
        print(f"FAIL: {exc.code} from {exc.url}: {exc.read()[:300]!r}")
        return 2
    except (urllib.error.URLError, OSError) as exc:
        rerun = _rerun_in_app_container()
        if rerun is not None:
            return rerun
        print(f"FAIL: cannot reach {BASE}: {exc}")
        return 2
    status: dict = {}
    policy: dict = {}
    for path, target in (("/research/smc/bot-status", status), ("/research/smc/agent/policy", policy)):
        try:
            target.update(get(path))
        except Exception as exc:  # noqa: BLE001 - report what could not be read
            print(f"(could not read {path}: {exc})")
    lines, verdict = diagnose(status, agent, paper, policy)
    print("\n".join(lines))
    print("\n== VERDICT ==")
    for line in verdict:
        print("  * " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
