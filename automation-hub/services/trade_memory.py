"""Trade-memory composition, reflection, similarity and natural-language query.

``compose_memory`` folds the REAL captured data for one closed trade (its
decision-journal row, the unified decision object, the ledger fill) into the
8-category permanent memory the spec asks for. Fields the bot never measured
are marked honestly ("not captured" / "Not checked") — never invented.

``ask`` answers natural-language questions ("show all losing BTC trades",
"which setup has the highest expectancy?", "why am I losing on Mondays?") by
routing to deterministic analytics over the real memory, falling back to
full-text search. ``similar`` ranks memories by cosine similarity over a
numeric feature vector. None of this claims to be an LLM embedding model — it
is honest local retrieval; the store leaves room to plug one in later.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

_NOT_CAPTURED = "not captured"
_NOT_CHECKED = "Not checked"

# Trading sessions by UTC hour (approximate, standard FX/crypto session windows).
_SESSIONS = (
    ("Sydney", 21, 24), ("Sydney", 0, 6), ("Tokyo", 0, 8),
    ("London", 7, 16), ("New York", 12, 21),
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SETUP_GRADE_BASIS = "pretrade_quality_v1"


def _parse_ts(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _session_for(dt: Optional[datetime]) -> str:
    if dt is None:
        return _NOT_CAPTURED
    h = dt.astimezone(timezone.utc).hour
    # Pick the primary session for the hour (London/NY overlap -> London-NY).
    active = [name for name, a, b in _SESSIONS if a <= h < b]
    if "London" in active and "New York" in active:
        return "London-NY overlap"
    if active:
        # prefer the major session for the hour
        for pref in ("New York", "London", "Tokyo", "Sydney"):
            if pref in active:
                return pref
    return "Off-hours"


def _weekday_for(dt: Optional[datetime]) -> str:
    return _WEEKDAYS[dt.weekday()] if dt else _NOT_CAPTURED


def _num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _identity_field(journal: dict, key: str):
    """Read captured scope only; never derive identity from current settings."""
    value = journal.get(key)
    if value is None:
        value = ((journal.get("sections") or {}).get("provenance") or {}).get(key)
    return value


def _decision_reference(journal: dict) -> tuple[object, str]:
    entry_context = ((journal.get("sections") or {}).get("entry_decision") or {})
    if entry_context.get("decision_reference_status") in ("CONFLICT", "UNRESOLVED"):
        raise ValueError("invalid explicit decision reference")
    header = journal.get("decision_id")
    entry = ((journal.get("sections") or {}).get("entry_decision") or {}).get("decision_reference")
    if header is not None and entry is not None and str(header) != str(entry):
        raise ValueError("conflicting explicit decision references")
    if header is not None:
        return header, "decision_id"
    if entry is not None:
        return entry, "decision_reference"
    return None, "not captured"


def _decision_scope_matches(journal: dict, decision: dict, *, legacy: bool = False) -> bool:
    if not decision.get("executed") or decision.get("decision") != "accepted":
        return False
    for key in ("symbol", "side", "instance_id", "simulation_session_id", "tenant_id",
                "owner_id", "account_id", "strategy_id", "strategy_version",
                "strategy_config_hash", "execution_mode", "lab_id", "source_kind"):
        expected = _identity_field(journal, key)
        if expected not in (None, "") and decision.get(key) != expected:
            return False
        if (legacy and key in ("instance_id", "simulation_session_id", "owner_id", "account_id")
                and expected in (None, "") and decision.get(key) not in (None, "")):
            return False
    return True


def _memory_scope_matches(journal: dict, memory: dict) -> bool:
    info = ((memory.get("sections") or {}).get("trade_information") or {})
    for key in ("tenant_id", "owner_id", "account_id", "instance_id", "simulation_session_id", "lab_id"):
        recorded = info.get(key)
        if recorded not in (None, "", _NOT_CAPTURED) and recorded != _identity_field(journal, key):
            return False
    return True


def _decision_linkage(journal: dict, decision: Optional[dict]) -> dict:
    try:
        reference, basis = _decision_reference(journal)
    except ValueError:
        return {"status": "UNVERIFIED", "basis": "conflicting_explicit_references", "decision_id": None}
    if decision is not None and _decision_scope_matches(journal, decision):
        if reference is not None and str(decision.get("id")) == str(reference):
            return {"status": "VERIFIED", "basis": basis, "decision_id": decision["id"]}
        if reference is None:
            return {"status": "UNVERIFIED", "basis": "legacy_symbol_side_time", "decision_id": decision.get("id")}
    return {"status": "UNVERIFIED", "basis": ("unresolved_" + basis if reference is not None
                                               else "not captured"), "decision_id": None}


def _financial_text(value) -> Optional[str]:
    """Retain receipt precision; missing/nonfinite values never become zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    if not amount.is_finite():
        return None
    if amount == 0:
        return "0"
    text = format(amount, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _cost_evidence(journal: dict, exit_decision: dict) -> dict:
    receipt = exit_decision.get("execution_receipt") or {}
    fees = _financial_text(receipt.get("booked_fees"))
    coverage = str(receipt.get("funding_coverage") or "UNKNOWN").upper()
    coverage = {"NOT_MODELED": "UNMODELED"}.get(coverage, coverage)
    funding = (_financial_text(receipt.get("funding"))
               if coverage in ("BOOKED", "MODELED", "VERIFIED_ZERO") else None)
    if coverage == "VERIFIED_ZERO" and funding != "0":
        funding = None
    if coverage != "UNMODELED" and funding is None:
        coverage = "UNKNOWN"
    return {"fees": fees, "fees_coverage": "BOOKED" if fees is not None else "UNKNOWN",
            "funding": funding, "funding_coverage": coverage,
            "gross_pnl": _financial_text(receipt.get("gross_pnl")),
            "net_pnl": (_financial_text(receipt.get("net_pnl"))
                        or _financial_text(journal.get("pnl"))),
            "basis": "execution_receipt" if receipt else "journal (cost receipt not captured)"}


def _pretrade_setup_grade(journal: dict, entry_decision: dict) -> dict:
    """Grade entry quality without looking at the eventual trade result.

    The former memory composer copied the journal's post-trade A-F review into
    ``setup_grade``.  Since that review is partly calculated from realised R,
    grouping performance by it leaked the answer and made A grades look
    predictively perfect.  Use the quality-gate score captured at entry, then
    the entry-time brain/confidence scores as honest fallbacks.
    """
    candidates = (
        (entry_decision.get("quality_gate_score"), "quality_gate_score"),
        (journal.get("brain_score"), "brain_score"),
        (journal.get("confidence"), "confidence_score"),
    )
    score = source = None
    for raw, label in candidates:
        value = _num(raw)
        if value is not None:
            score, source = value, label
            break
    if score is None:
        return {"grade": "Unscored", "score": None, "source": "not captured",
                "basis": _SETUP_GRADE_BASIS}
    # Accept either 0..1 confidence or the normal 0..100 score scale.
    if 0 <= score <= 1:
        score *= 100
    score = max(0.0, min(100.0, score))
    grade = ("A" if score >= 80 else "B" if score >= 70 else
             "C" if score >= 60 else "D" if score >= 50 else "F")
    return {"grade": grade, "score": round(score, 2), "source": source,
            "basis": _SETUP_GRADE_BASIS}


def compose_memory(journal: dict, *, decision: Optional[dict] = None,
                   exchange: str = "paper", notes: str = "") -> dict:
    """Build the permanent 8-category memory for one CLOSED journal trade."""
    s = journal.get("sections", {}) or {}
    snap = s.get("market_snapshot", {}) or {}
    checklist = s.get("checklist", {}) or {}
    review = s.get("review", {}) or {}
    evolution = s.get("evolution", {}) or {}
    entry_dec = s.get("entry_decision", {}) or {}
    exit_dec = s.get("exit_decision", {}) or {}
    provenance = s.get("provenance", {}) or {}
    linkage = _decision_linkage(journal, decision)
    if linkage["decision_id"] is None:
        decision = None
    costs = _cost_evidence(journal, exit_dec)
    setup_quality = _pretrade_setup_grade(journal, entry_dec)

    created = _parse_ts(journal.get("created_at"))
    closed = _parse_ts(journal.get("closed_at"))
    duration_s = None
    if created and closed:
        duration_s = max(0.0, (closed - created).total_seconds())
    session = _session_for(created)
    weekday = _weekday_for(created)

    entry = _num(journal.get("entry"))
    stop = _num(journal.get("stop"))
    size = _num(journal.get("size"))
    equity = _num(snap.get("account_equity"))
    risk_amount = _num(journal.get("risk_amount"))
    risk_pct = (round(100 * risk_amount / equity, 3)
                if (risk_amount is not None and equity) else _NOT_CAPTURED)

    # ---- 1. Trade Information -------------------------------------------------
    trade_information = {
        "trade_id": journal.get("trade_id"),
        "date": created.date().isoformat() if created else _NOT_CAPTURED,
        "time_utc": created.strftime("%H:%M:%S UTC") if created else _NOT_CAPTURED,
        "exchange": provenance.get("exchange") or exchange,
        "instance_id": _identity_field(journal, "instance_id") or _NOT_CAPTURED,
        **{key: _identity_field(journal, key) for key in (
            "tenant_id", "owner_id", "account_id", "simulation_session_id", "lab_id")},
        "market_data_mode": provenance.get("market_data_mode") or _NOT_CAPTURED,
        "symbol": journal.get("symbol"),
        "direction": "Long" if journal.get("side") == "long" else "Short",
        "entry": entry,
        "exit": _num(journal.get("exit")),
        "stop_loss": stop,
        "take_profit": _num(journal.get("target")),
        "position_size": size,
        "risk_pct": risk_pct,
        "planned_rr": _num(journal.get("planned_rr")),
        "actual_rr": _num(journal.get("actual_rr")),
        "fees": costs["fees"] if costs["fees"] is not None else "not captured (paper/live fee coverage UNKNOWN)",
        "fees_coverage": costs["fees_coverage"],
        "funding": costs["funding"], "funding_coverage": costs["funding_coverage"],
        "cost_basis": costs["basis"],
        "duration": _fmt_duration(duration_s),
    }

    # ---- 2. Market Context ----------------------------------------------------
    market_context = {
        "trend": snap.get("trend") or snap.get("trend_direction") or entry_dec.get("higher_timeframe_trend") or _NOT_CAPTURED,
        "market_structure": snap.get("market_structure") or _NOT_CHECKED,
        "session": session,
        "volatility": snap.get("volatility", _NOT_CAPTURED),
        "atr": snap.get("atr", _NOT_CAPTURED),
        "atr_pct": snap.get("atr_pct", _NOT_CAPTURED),
        "volume": snap.get("volume", _NOT_CAPTURED),
        "avg_volume_20": snap.get("avg_volume_20", _NOT_CAPTURED),
        "liquidity": snap.get("liquidity", _NOT_CAPTURED),
        "support": snap.get("support", _NOT_CAPTURED),
        "resistance": snap.get("resistance", _NOT_CAPTURED),
        "funding_rate": _NOT_CAPTURED,
        "fear_greed_index": _NOT_CAPTURED,
        "btc_dominance": _NOT_CAPTURED,
    }

    # ---- 3. Technical Analysis (honest reads from the brain snapshot/checklist)
    technical_analysis = {
        "ema_fast": snap.get("ema_fast", _NOT_CAPTURED),
        "ema_slow": snap.get("ema_slow", _NOT_CAPTURED),
        "ema_trend": snap.get("ema_trend", _NOT_CAPTURED),
        "rsi": snap.get("rsi", _NOT_CAPTURED),
        "macd": snap.get("macd", _NOT_CHECKED),
        "vwap": snap.get("vwap", _NOT_CHECKED),
        "bollinger_bands": snap.get("bollinger", _NOT_CHECKED),
        "order_blocks": _smc_read_any(checklist, "order_block", "order block"),
        "fair_value_gaps": _smc_read_any(checklist, "fvg", "fair value gap"),
        "supply_demand": _smc_read_any(checklist, "supply", "demand"),
        "break_of_structure": _smc_read_any(checklist, "bos", "break of structure"),
        "change_of_character": _smc_read_any(checklist, "choch", "change of character"),
    }

    # ---- 4. Strategy ----------------------------------------------------------
    strategy = {
        "name": journal.get("strategy"),
        "id": _identity_field(journal, "strategy_id") or _NOT_CAPTURED,
        "version": (_identity_field(journal, "strategy_version") or snap.get("strategy_version")
                    or ((decision or {}).get("strategy_version") if linkage["status"] == "VERIFIED" else None)
                    or _NOT_CAPTURED),
        "config_fingerprint": _identity_field(journal, "strategy_config_hash"),
        "timeframe": journal.get("timeframe"),
        "setup_grade": setup_quality["grade"],
        "setup_quality_score": setup_quality["score"],
        "setup_grade_source": setup_quality["source"],
        "setup_grade_basis": setup_quality["basis"],
        "outcome_grade": journal.get("grade") or review.get("grade") or _NOT_CAPTURED,
        "confidence_score": _num(journal.get("confidence")),
        "brain_score": _num(journal.get("brain_score")),
        "regime": journal.get("regime") or _NOT_CAPTURED,
        "htf_bias": (decision or {}).get("htf_bias", _NOT_CAPTURED),
    }

    # ---- 5. Execution ---------------------------------------------------------
    passed, failed = _conditions(checklist, decision)
    execution = {
        "why_opened": entry_dec.get("main_reason") or "Strategy signal fired.",
        "why_closed": exit_dec.get("exit_reason") or _NOT_CAPTURED,
        "conditions_passed": passed,
        "conditions_failed": failed if failed else ["None — all evaluated gates passed."],
        "fill_model": provenance.get("fill_model") or _NOT_CAPTURED,
        "execution_mode": provenance.get("execution_mode") or journal.get("mode") or _NOT_CAPTURED,
        "data_exchange": provenance.get("exchange") or exchange,
        "instrument_type": provenance.get("instrument_type") or _NOT_CAPTURED,
        "decision_linkage": linkage,
    }

    # ---- 6. Emotion & Journal (manual) ---------------------------------------
    emotion_journal = {"manual_notes": notes or ""}

    # ---- 7. Trade Outcome -----------------------------------------------------
    pnl = _num(journal.get("pnl")) or 0.0
    result = journal.get("result") or ("win" if pnl > 0 else "loss" if pnl < 0 else "breakeven")
    trade_outcome = {
        "result": result,
        "profit": round(pnl, 2) if pnl > 0 else 0.0,
        "loss": round(pnl, 2) if pnl < 0 else 0.0,
        "pnl": round(pnl, 2),
        "gross_pnl": costs["gross_pnl"], "net_pnl": costs["net_pnl"],
        "booked_fees": costs["fees"], "funding": costs["funding"],
        "cost_basis": costs["basis"],
        "actual_rr": _num(journal.get("actual_rr")),
        "mistakes": review.get("mistake", _NOT_CAPTURED),
        "lessons_learned": evolution.get("learned", _NOT_CAPTURED),
        "improvement_notes": review.get("improvement", _NOT_CAPTURED),
    }

    # ---- 8. AI Reflection (deterministic, from the real review + evolution) ---
    ai_reflection = _reflect(review, evolution, trade_outcome, execution)

    sections = {
        "trade_information": trade_information,
        "market_context": market_context,
        "technical_analysis": technical_analysis,
        "strategy": strategy,
        "execution": execution,
        "emotion_journal": emotion_journal,
        "trade_outcome": trade_outcome,
        "ai_reflection": ai_reflection,
    }

    features = _features(journal, snap, session)

    return {
        "trade_id": journal.get("trade_id"),
        "created_at": journal.get("created_at"),
        "closed_at": journal.get("closed_at"),
        "mode": journal.get("mode", "paper"),
        "symbol": journal.get("symbol"),
        "side": journal.get("side"),
        "strategy": journal.get("strategy"),
        "timeframe": journal.get("timeframe"),
        "entry": entry, "exit": _num(journal.get("exit")), "stop": stop,
        "target": _num(journal.get("target")), "size": size,
        "risk_amount": risk_amount, "planned_rr": _num(journal.get("planned_rr")),
        "actual_rr": _num(journal.get("actual_rr")), "pnl": round(pnl, 2),
        "result": result, "grade": journal.get("grade"),
        "confidence": _num(journal.get("confidence")),
        "brain_score": _num(journal.get("brain_score")),
        "regime": journal.get("regime"), "session": session, "weekday": weekday,
        "duration_s": duration_s, "sections": sections, "features": features,
        "notes": notes or "",
    }


# --------------------------------------------------------------- reflection
def _reflect(review: dict, evolution: dict, outcome: dict, execution: dict) -> dict:
    """The 4-question AI reflection, composed from what actually happened. No
    invented insight — every answer traces to a real field."""
    result = outcome.get("result")
    followed = review.get("followed_strategy", True)
    risk_ok = review.get("risk_valid", True)
    grade = review.get("grade", "?")

    if result == "win" and followed:
        went_well = f"Disciplined {grade}-grade win — the plan was followed and it paid ({outcome.get('actual_rr')}R)."
    elif result == "win":
        went_well = "Trade won, but not fully by the book — treat the result with caution."
    elif followed and risk_ok:
        went_well = "Risk was respected and the process was followed even though the trade lost — a controlled loss."
    else:
        went_well = "Little went well — review both process and risk."

    went_wrong = review.get("mistake", _NOT_CAPTURED)
    if result == "loss" and went_wrong.startswith("None"):
        went_wrong = "Nothing mechanical — the stop did its job; the setup simply failed."

    repeat = review.get("improvement", _NOT_CAPTURED)
    if evolution.get("take_similar_again") is True:
        repeat = (repeat + " " if repeat and repeat != _NOT_CAPTURED else "") + \
                 "This setup is worth taking again within existing risk limits."
    elif evolution.get("take_similar_again") is False:
        repeat = "Do not repeat this setup on current evidence — it is net-negative."

    if not risk_ok:
        never = "Never bypass the Risk Manager / Safety Center again — that is the cardinal rule."
    elif not followed:
        never = "Never trade off-process — every entry must clear the checklist."
    else:
        never = "No hard rule was broken; keep the same discipline."

    return {
        "what_went_well": went_well,
        "what_went_wrong": went_wrong,
        "what_to_repeat": repeat,
        "what_to_never_do_again": never,
        "basis": "Composed from the trade's real review + evolution memory (no invented insight).",
    }


# --------------------------------------------------------------- similarity
def _features(journal: dict, snap: dict, session: str) -> dict:
    """Numeric feature vector for similarity. Only real measured values go in;
    absent values are simply omitted so cosine compares on shared axes."""
    f: dict = {}
    f["side"] = 1.0 if journal.get("side") == "long" else -1.0
    for key, src in (("planned_rr", journal.get("planned_rr")),
                     ("actual_rr", journal.get("actual_rr")),
                     ("confidence", journal.get("confidence")),
                     ("brain_score", journal.get("brain_score"))):
        v = _num(src)
        if v is not None:
            f[key] = v
    for key in ("rsi", "atr_pct", "volatility", "ema_fast", "ema_slow"):
        v = _num(snap.get(key))
        if v is not None:
            f[key] = v
    return f


def cosine(a: dict, b: dict) -> float:
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    na = sum(a[k] ** 2 for k in keys) ** 0.5
    nb = sum(b[k] ** 2 for k in keys) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def similar(store, trade_id: str, limit: int = 5) -> list[dict]:
    target = store.get(trade_id)
    if target is None:
        return []
    tf = target.get("features", {})
    scored = []
    for row in store.all_features():
        if row["trade_id"] == trade_id:
            continue
        score = cosine(tf, row.get("features", {}))
        scored.append({"trade_id": row["trade_id"], "symbol": row["symbol"],
                       "side": row["side"], "result": row["result"],
                       "similarity": round(score, 3)})
    scored.sort(key=lambda r: r["similarity"], reverse=True)
    return scored[:limit]


# ------------------------------------------------------------ NL query (ask)
def ask(store, insights_fn, q: str, limit: int = 50) -> dict:
    """Answer a natural-language question over the trade memory. Routes to a
    deterministic analytic when the intent is recognised; otherwise full-text
    search. Always honest about which path answered."""
    ql = (q or "").lower().strip()
    if not ql:
        return {"query": q, "kind": "empty", "answer": "Ask a question about your trades.", "trades": []}

    symbol = _extract_symbol(ql)
    want_loss = any(w in ql for w in ("losing", "loss", "lost", "red"))
    want_win = any(w in ql for w in ("winning", "win", "won", "best", "green", "profit"))

    # "why am I losing on Mondays?" / weekday analysis
    for wd in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        if wd in ql:
            return _weekday_answer(store, insights_fn, wd.capitalize())

    # "which setup / strategy has the highest expectancy?"
    if "expectancy" in ql or ("highest" in ql and ("setup" in ql or "strategy" in ql)):
        ins = insights_fn()
        by = ins.get("by_strategy_live_ready", [])
        ranked = sorted(by, key=lambda r: r.get("expectancy", 0), reverse=True)
        return {"query": q, "kind": "expectancy",
                "answer": (f"Highest expectancy: {ranked[0]['strategy']} at "
                           f"{ranked[0]['expectancy']:+.3f}R/trade over {ranked[0]['trades']} trades."
                           if ranked else "No paper-forward trades with RealisticFill exist yet; "
                           "historical or ideal-fill results are not used to rank live readiness."),
                "ranking": ranked, "trades": []}

    # "repeated mistakes" / mistake library
    if "mistake" in ql or "wrong" in ql or "bad habit" in ql:
        ins = insights_fn()
        return {"query": q, "kind": "mistakes",
                "answer": ("Most frequent recorded mistakes are listed below."
                           if ins.get("mistakes") else "No mistakes recorded yet."),
                "mistakes": ins.get("mistakes", []), "trades": []}

    # "best trade this year" — top winner by pnl
    if want_win and ("best" in ql or "biggest" in ql):
        rows = store.list(limit=500, result="win")
        rows = [r for r in rows if not symbol or r["symbol"] == symbol]
        rows.sort(key=lambda r: r.get("pnl", 0), reverse=True)
        top = rows[0] if rows else None
        return {"query": q, "kind": "best_trade",
                "answer": (f"Best trade: {top['symbol']} {top['side']} for {top['pnl']:+.2f} "
                           f"({top.get('actual_rr')}R) on {top.get('closed_at','?')[:10]}."
                           if top else "No winning trades recorded yet."),
                "trades": rows[:5]}

    # "show all losing/winning [SYMBOL] trades"
    result = "loss" if (want_loss and not want_win) else "win" if (want_win and not want_loss) else None
    if result or symbol:
        rows = store.list(limit=limit, result=result, symbol=symbol)
        label = f"{result or 'all'} {symbol or ''} trades".strip()
        return {"query": q, "kind": "filter",
                "answer": f"Found {len(rows)} {label}.", "trades": rows}

    # fallback: full-text search over the memory
    rows = store.list(limit=limit, q=q)
    return {"query": q, "kind": "search",
            "answer": (f"{len(rows)} memories match your search." if rows
                       else "No memories matched — try naming a symbol, result, weekday or setup."),
            "trades": rows}


def _weekday_answer(store, insights_fn, weekday: str) -> dict:
    ins = insights_fn()
    row = next((r for r in ins.get("by_weekday", []) if r.get("weekday") == weekday), None)
    if not row or row.get("trades", 0) == 0:
        return {"query": weekday, "kind": "weekday",
                "answer": f"No closed trades recorded on {weekday}s yet.", "row": row, "trades": []}
    trades = [t for t in store.list(limit=500) if t.get("weekday") == weekday]
    verdict = (f"On {weekday}s you have {row['trades']} trades, {row['win_rate']}% win rate, "
               f"{row['expectancy']:+.3f}R expectancy.")
    if row.get("trades", 0) < 10:
        verdict += " (Small sample — treat as an early signal, not proof.)"
    return {"query": weekday, "kind": "weekday", "answer": verdict, "row": row, "trades": trades[:limit_default()]}


def limit_default() -> int:
    return 50


# ----------------------------------------------------------------- helpers
def _smc_read(checklist: dict, name: str) -> str:
    """Pull a smart-money-concept read from the entry checklist, honestly."""
    for group in ("entry_reads", "confluence", "smc"):
        for item in (checklist.get(group) or []):
            rule = str(item.get("rule") or item.get("name") or "").lower()
            if name in rule:
                status = str(item.get("status") or "").strip().lower()
                if status in ("not checked", "neutral"):
                    return _NOT_CHECKED
                if item.get("detail"):
                    return item["detail"]
                if "ok" in item:
                    return str(bool(item["ok"]))
                return item.get("status") or _NOT_CHECKED
    return _NOT_CHECKED


def _smc_read_any(checklist: dict, *names: str) -> str:
    for name in names:
        value = _smc_read(checklist, name)
        if value != _NOT_CHECKED:
            return value
    return _NOT_CHECKED


def _conditions(checklist: dict, decision: Optional[dict]) -> tuple[list, list]:
    passed, failed = [], []
    if decision:
        passed = list(decision.get("passed_rules") or [])
        failed = list(decision.get("failed_rules") or [])
    if not passed and not failed:
        for group in ("entry_reads", "risk_gates", "confluence"):
            for item in (checklist.get(group) or []):
                label = item.get("detail") or item.get("name") or item.get("rule") or ""
                status = str(item.get("status") or "").strip().lower()
                if "ok" in item:
                    outcome = bool(item.get("ok"))
                elif "passed" in item:
                    outcome = bool(item.get("passed"))
                elif status == "passed":
                    outcome = True
                elif status == "failed":
                    outcome = False
                else:
                    continue  # neutral / not checked is neither pass nor fail
                (passed if outcome else failed).append(label)
    return passed, failed


def _extract_symbol(ql: str) -> Optional[str]:
    for sym in ("btcusdt", "ethusdt", "solusdt", "btc", "eth", "sol"):
        if sym in ql:
            base = sym.replace("usdt", "").upper()
            return base + "USDT"
    return None


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return _NOT_CAPTURED
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
