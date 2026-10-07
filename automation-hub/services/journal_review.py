"""Trade review agent — a structured, deterministic post-trade review.

The review is composed only from facts already in the canonical journal (the
trade row, its frozen decision snapshot, executions and modifications). It is
stored in ``journal_reviews``, separately from the trade, and it never writes
to the trade or the snapshot: an agent may comment on a trade, it may not
change what happened.

External agents (for example an LLM reviewer) can submit a review in the same
shape through ``validate_external_review``; the same separation applies.
"""
from __future__ import annotations

from typing import Optional

REVIEWER = "trade-review-agent"
REVIEW_VERSION = "1.0"
QUALITY_VALUES = ("STRONG", "ACCEPTABLE", "WEAK", "POOR", "UNKNOWN")
_REVIEW_TEXT_FIELDS = ("setup_quality", "execution_quality", "risk_management", "outcome",
                       "grade", "summary", "improvement")
_REVIEW_LIST_FIELDS = ("mistakes", "went_well", "went_wrong", "rule_violations")


def _num(value) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def rule_violations(trade: dict, snapshot: Optional[dict], modifications: list[dict]) -> list[str]:
    """Objective rule checks. These drive ``rule_violation`` on the trade and
    the review's ``rule_violations`` list; they do not depend on the outcome."""
    found: list[str] = []
    if trade.get("risk_rule_status") == "FAILED":
        found.append(
            f"Risk {(_num(trade.get('risk_pct')) or 0):.2f}% exceeded the maximum allowed "
            f"{(_num(trade.get('max_allowed_risk_pct')) or 0):.2f}%.")
    if trade.get("initial_stop") is None and not trade.get("is_operational"):
        found.append("Position was opened without a protective stop.")
    entry = _num(trade.get("entry_price"))
    direction = trade.get("direction")
    for mod in modifications or []:
        if mod.get("field") != "STOP_LOSS" or entry is None:
            continue
        old, new = _num(mod.get("old_value")), _num(mod.get("new_value"))
        if old is None or new is None:
            continue
        widened = (new < old) if direction == "LONG" else (new > old)
        if widened:
            found.append(f"Stop moved further from entry ({old:g} → {new:g}); risk increased after entry.")
    failed = (snapshot or {}).get("conditions_failed") or []
    required_failed = [row for row in failed if isinstance(row, dict) and row.get("required")]
    for row in required_failed:
        found.append(f"Entered with a required condition failed: {row.get('name')}.")
    return found


def _setup_quality(snapshot: Optional[dict]) -> tuple[str, str]:
    if not snapshot:
        return "UNKNOWN", "No decision snapshot was captured for this trade."
    passed = snapshot.get("conditions_passed") or []
    failed = snapshot.get("conditions_failed") or []
    missing = snapshot.get("conditions_missing") or []
    score = _num(snapshot.get("setup_score"))
    if not passed and not failed and score is None:
        return "UNKNOWN", "The strategy did not report per-condition results for this setup."
    if failed or missing:
        quality = "WEAK" if len(failed) + len(missing) > len(passed) / 2 else "ACCEPTABLE"
    elif score is not None and score < 60:
        quality = "ACCEPTABLE"
    else:
        quality = "STRONG"
    detail = f"{len(passed)} condition(s) passed, {len(failed)} failed, {len(missing)} not evaluated"
    if score is not None:
        detail += f", setup score {score:g}"
    return quality, detail + "."


def _execution_quality(trade: dict) -> tuple[str, str]:
    stop_distance = _num(trade.get("stop_distance"))
    slippage = _num(trade.get("entry_slippage"))
    if stop_distance in (None, 0) or slippage is None:
        return "UNKNOWN", "Requested-versus-filled price was not captured."
    slip_r = abs(slippage) / stop_distance
    fees_r = None
    risk = _num(trade.get("risk_amount"))
    if risk and _num(trade.get("fees_total")) is not None:
        fees_r = float(trade["fees_total"]) / risk
    quality = "STRONG" if slip_r <= 0.05 else "ACCEPTABLE" if slip_r <= 0.15 else "POOR"
    text = f"Entry slippage {slip_r:.3f}R"
    if fees_r is not None:
        text += f"; fees {fees_r:.3f}R"
        if fees_r > 0.25 and quality == "STRONG":
            quality = "ACCEPTABLE"
    return quality, text + "."


def _risk_management(trade: dict, violations: list[str]) -> str:
    status = trade.get("risk_rule_status")
    if trade.get("initial_stop") is None:
        return "NO_STOP"
    if status == "FAILED" or any("risk increased" in v for v in violations):
        return "VIOLATED"
    if status == "PASSED":
        return "RESPECTED"
    return "UNKNOWN"


def grade(trade: dict, violations: list[str]) -> str:
    """A–F. Process counts as much as outcome: a rule-breaking win is not an A
    and a disciplined full-stop loss is not an F."""
    r = _num(trade.get("realised_r"))
    planned = _num(trade.get("planned_rr")) or 0.0
    result = trade.get("result")
    if violations:
        return "F" if any("risk" in v.lower() for v in violations) else "D"
    if r is None:
        return "C"
    if result in ("WIN", "PARTIAL_WIN") and r >= max(1.5, 0.6 * planned):
        return "A"
    if result in ("WIN", "PARTIAL_WIN", "BREAK_EVEN") and r >= 0:
        return "B"
    if r > -1.05:
        return "C"
    return "D"


def build_review(trade: dict, snapshot: Optional[dict], modifications: list[dict]) -> dict:
    violations = rule_violations(trade, snapshot, modifications)
    setup_quality, setup_detail = _setup_quality(snapshot)
    execution_quality, execution_detail = _execution_quality(trade)
    risk_management = _risk_management(trade, violations)
    r = _num(trade.get("realised_r"))
    planned = _num(trade.get("planned_rr"))
    mfe_r = _num(trade.get("mfe_r"))
    exit_reason = trade.get("exit_reason") or "UNKNOWN"
    result = trade.get("result") or "UNKNOWN"
    went_well: list[str] = []
    went_wrong: list[str] = []
    mistakes: list[str] = []
    if risk_management == "RESPECTED":
        went_well.append("Risk was sized within the configured limit and a stop was in place.")
    if exit_reason == "TAKE_PROFIT":
        went_well.append("The planned target was reached.")
    if exit_reason == "STOP_LOSS" and r is not None and r >= -1.1:
        went_well.append("The stop limited the loss to about one R, as planned.")
    if exit_reason == "BREAK_EVEN_STOP":
        went_well.append("Moving the stop to break-even protected the account.")
    if setup_quality == "STRONG":
        went_well.append("Every reported entry condition was satisfied.")
    if execution_quality == "STRONG":
        went_well.append("Entry fill was close to the requested price.")
    if r is not None and planned and result in ("WIN", "PARTIAL_WIN") and r < 0.5 * planned:
        went_wrong.append(f"Banked {r:+.2f}R of a {planned:.2f}R plan.")
        if exit_reason not in ("TAKE_PROFIT",):
            mistakes.append("Exited well short of the planned target.")
    if mfe_r is not None and r is not None and mfe_r - r >= 1.0:
        went_wrong.append(f"Gave back {mfe_r - r:.2f}R from the best unrealised point ({mfe_r:+.2f}R).")
    if r is not None and r < -1.15:
        went_wrong.append(f"Loss of {r:.2f}R exceeded the planned one-R risk (slippage, gap or fees).")
        mistakes.append("Realised loss was larger than the planned risk.")
    if execution_quality == "POOR":
        went_wrong.append("Entry slippage was large relative to the stop distance.")
    gross = _num(trade.get("gross_pnl"))
    fees = _num(trade.get("fees_total"))
    if gross and gross > 0 and fees is not None and fees >= 0.3 * gross:
        went_wrong.append(f"Fees consumed {fees / gross * 100:.0f}% of the gross profit.")
    if trade.get("in_preferred_session") is False:
        mistakes.append("Entered outside the configured trading session window.")
    mistakes.extend(violations)
    if not went_wrong and result in ("LOSS", "PARTIAL_LOSS") and not violations:
        went_wrong.append("The setup did not work; the process was followed.")
    if violations:
        improvement = "Fix the rule violation first — process errors outweigh any single outcome."
    elif mistakes and "short of the planned target" in mistakes[0]:
        improvement = "Review the exit rule: let valid trades reach the planned target or trail deliberately."
    elif mfe_r is not None and r is not None and mfe_r - r >= 1.0:
        improvement = "Study whether a break-even or trailing rule would have kept more of the move."
    elif result in ("LOSS", "PARTIAL_LOSS"):
        improvement = "No change from one trade — revisit only if this pattern repeats in the weekly review."
    else:
        improvement = "Repeat the same disciplined process."
    outcome = result if r is None else f"{result} {r:+.2f}R"
    letter = grade(trade, violations)
    summary = (f"{trade.get('direction', '')} {trade.get('symbol', '')} — {outcome} via "
               f"{exit_reason.replace('_', ' ').lower()}. Setup {setup_quality.lower()}, "
               f"execution {execution_quality.lower()}, risk {risk_management.lower().replace('_', ' ')}.")
    return {
        "reviewer": REVIEWER, "review_version": REVIEW_VERSION,
        "setup_quality": setup_quality, "setup_quality_detail": setup_detail,
        "execution_quality": execution_quality, "execution_quality_detail": execution_detail,
        "risk_management": risk_management, "outcome": outcome, "grade": letter,
        "summary": summary, "mistakes": mistakes, "went_well": went_well,
        "went_wrong": went_wrong, "improvement": improvement,
        "rule_violations": violations,
    }


def validate_external_review(body: dict) -> dict:
    """Shape an externally-supplied review. Unknown keys are dropped so a
    reviewer cannot smuggle trade-fact changes through the review payload."""
    reviewer = str(body.get("reviewer") or "").strip()
    if not reviewer:
        raise ValueError("a review requires a reviewer name")
    out: dict = {"reviewer": reviewer[:80],
                 "review_version": str(body.get("review_version") or "external")[:40]}
    for key in _REVIEW_TEXT_FIELDS:
        if body.get(key) is not None:
            out[key] = str(body[key])[:2000]
    for key in _REVIEW_LIST_FIELDS:
        value = body.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list of strings")
        out[key] = [str(item)[:500] for item in value][:50]
    for key in ("setup_quality", "execution_quality"):
        if key in out and out[key].upper() in QUALITY_VALUES:
            out[key] = out[key].upper()
    return out
