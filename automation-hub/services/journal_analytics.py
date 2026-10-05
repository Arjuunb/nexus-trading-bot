"""Journal analytics — every financial statistic is computed here, server-side.

All functions take canonical journal trade rows (already filtered by the
caller, including the trading-mode filter) and return plain dicts. Only
trades with ``counts_in_stats`` contribute to performance; operational events
(rejected / failed / uncertain / cancelled orders) are counted separately and
never treated as losses.

Small samples are labelled, not hidden: every block carries ``sample`` and a
``sample_warning`` under ``MIN_RELIABLE_SAMPLE`` trades.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Callable, Iterable, Optional

from services.journal_sessions import SESSION_LABELS, SESSIONS, iso_week_key, session_order

MIN_RELIABLE_SAMPLE = 30
MIN_OBSERVATION_SAMPLE = 3
WIN_RESULTS = ("WIN", "PARTIAL_WIN")
LOSS_RESULTS = ("LOSS", "PARTIAL_LOSS")


def _num(value) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: Iterable) -> Optional[float]:
    nums = [float(v) for v in values if v is not None]
    return mean(nums) if nums else None


def _r(value, places: int = 4):
    return None if value is None else round(float(value), places)


def stat_trades(trades: Iterable[dict]) -> list[dict]:
    return [t for t in trades if t.get("counts_in_stats")]


def _exit_key(t: dict) -> str:
    return t.get("exit_at") or t.get("entry_filled_at") or t.get("created_at") or ""


def drawdown(trades: list[dict], field: str = "net_pnl") -> dict:
    """Peak-to-trough of the cumulative series ordered by exit time."""
    peak = equity = 0.0
    worst = 0.0
    worst_at = None
    for t in sorted(trades, key=_exit_key):
        value = _num(t.get(field))
        if value is None:
            continue
        equity += value
        peak = max(peak, equity)
        if equity - peak < worst:
            worst, worst_at = equity - peak, _exit_key(t)
    return {"max_drawdown": round(worst, 8), "at": worst_at}


def metrics(trades: Iterable[dict]) -> dict:
    """The full performance block for a set of trades."""
    trades = list(trades)
    closed = stat_trades(trades)
    wins = [t for t in closed if t.get("result") in WIN_RESULTS]
    losses = [t for t in closed if t.get("result") in LOSS_RESULTS]
    breakeven = [t for t in closed if t.get("result") == "BREAK_EVEN"]
    pnl = [_num(t.get("net_pnl")) for t in closed if _num(t.get("net_pnl")) is not None]
    gross_profit = sum(p for p in pnl if p > 0)
    gross_loss = -sum(p for p in pnl if p < 0)
    win_pnl = [_num(t.get("net_pnl")) for t in wins if _num(t.get("net_pnl")) is not None]
    loss_pnl = [_num(t.get("net_pnl")) for t in losses if _num(t.get("net_pnl")) is not None]
    r_values = [_num(t.get("realised_r")) for t in closed if _num(t.get("realised_r")) is not None]
    with_pnl = [t for t in closed if _num(t.get("net_pnl")) is not None]
    best = max(with_pnl, key=lambda t: float(t["net_pnl"]), default=None)
    worst = min(with_pnl, key=lambda t: float(t["net_pnl"]), default=None)
    dd = drawdown(closed)
    dd_r = drawdown(closed, "realised_r")
    decided = len(wins) + len(losses) + len(breakeven)
    win_rate = (len(wins) / decided * 100) if decided else None
    loss_rate = (len(losses) / decided * 100) if decided else None
    operational = [t for t in trades if t.get("is_operational")]
    return {
        "sample": len(closed),
        "sample_warning": ("INSUFFICIENT_SAMPLE" if len(closed) < MIN_RELIABLE_SAMPLE else None),
        "total_trades": len(closed), "wins": len(wins), "losses": len(losses),
        "break_even": len(breakeven), "win_rate": _r(win_rate, 2), "loss_rate": _r(loss_rate, 2),
        "net_pnl": _r(sum(pnl), 8) if pnl else (0.0 if closed else None),
        "gross_profit": _r(gross_profit, 8), "gross_loss": _r(gross_loss, 8),
        "profit_factor": _r(gross_profit / gross_loss, 4) if gross_loss > 0 else (None if not gross_profit else "INF"),
        "avg_win": _r(_avg(win_pnl), 8), "avg_loss": _r(_avg(loss_pnl), 8),
        "avg_r": _r(_avg(r_values)), "total_r": _r(sum(r_values)) if r_values else None,
        "expectancy": _r(_avg(pnl), 8), "expectancy_r": _r(_avg(r_values)),
        "best_trade": ({"trade_ref": best["trade_ref"], "trade_id": best["trade_id"],
                        "net_pnl": _r(best["net_pnl"], 8), "symbol": best.get("symbol")} if best else None),
        "worst_trade": ({"trade_ref": worst["trade_ref"], "trade_id": worst["trade_id"],
                         "net_pnl": _r(worst["net_pnl"], 8), "symbol": worst.get("symbol")} if worst else None),
        "max_drawdown": dd["max_drawdown"] if pnl else None, "max_drawdown_at": dd["at"] if pnl else None,
        "max_drawdown_r": dd_r["max_drawdown"] if r_values else None,
        "avg_duration_s": _r(_avg(t.get("duration_s") for t in closed), 1),
        "total_fees": _r(sum(_num(t.get("fees_total")) or 0 for t in closed), 8),
        "total_funding": _r(sum(_num(t.get("funding_total")) or 0 for t in closed), 8),
        "avg_leverage": _r(_avg(t.get("leverage") for t in closed), 2),
        "avg_position_size": _r(_avg(t.get("notional_value") for t in closed), 4),
        "avg_quantity": _r(_avg(t.get("quantity") for t in closed), 8),
        "avg_risk_pct": _r(_avg(t.get("risk_pct") for t in closed)),
        "avg_planned_rr": _r(_avg(t.get("planned_rr") for t in closed)),
        "open_trades": sum(1 for t in trades if t.get("status") in ("OPEN", "PARTIALLY_CLOSED")),
        "pending_orders": sum(1 for t in trades if t.get("status") == "PENDING"),
        "operational_events": len(operational),
        "operational_breakdown": dict(Counter(t.get("result") for t in operational if t.get("result"))),
        "rule_violations": sum(1 for t in closed if t.get("rule_violation")),
    }


_COMPACT = ("sample", "sample_warning", "total_trades", "wins", "losses", "break_even", "win_rate",
            "net_pnl", "gross_profit", "gross_loss", "profit_factor", "avg_r", "expectancy",
            "expectancy_r", "max_drawdown", "avg_duration_s", "avg_leverage", "avg_risk_pct",
            "avg_planned_rr", "total_fees")


def _compact(m: dict) -> dict:
    return {k: m.get(k) for k in _COMPACT}


def group_by(trades: Iterable[dict], key: Callable[[dict], Optional[str]],
             label: Optional[Callable[[str, list], str]] = None, full: bool = False) -> list[dict]:
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        k = key(t)
        buckets[k if k not in (None, "") else "UNKNOWN"].append(t)
    rows = []
    for k, rows_in in buckets.items():
        m = metrics(rows_in)
        if m["total_trades"] == 0 and not full:
            continue
        rows.append({"key": k, "label": label(k, rows_in) if label else k,
                     **(m if full else _compact(m))})
    return sorted(rows, key=lambda r: (-(r["net_pnl"] or 0) if isinstance(r["net_pnl"], (int, float)) else 0,
                                       r["key"]))


def _rank_value(row: dict) -> tuple:
    """Rank by net P&L, then average R (for R-only rows)."""
    pnl = row.get("net_pnl")
    return (pnl if isinstance(pnl, (int, float)) else float("-inf"), row.get("avg_r") or float("-inf"))


def _best(rows: list[dict], *, worst: bool = False, min_trades: int = 1) -> Optional[dict]:
    eligible = [r for r in rows if (r.get("total_trades") or 0) >= min_trades]
    if not eligible:
        return None
    chosen = (min if worst else max)(eligible, key=_rank_value)
    return {"key": chosen["key"], "label": chosen["label"], "net_pnl": chosen.get("net_pnl"),
            "avg_r": chosen.get("avg_r"), "trades": chosen.get("total_trades"),
            "win_rate": chosen.get("win_rate")}


def strategy_key(t: dict) -> str:
    return t.get("strategy_name") or t.get("strategy_id") or "UNKNOWN"


def instance_key(t: dict) -> str:
    return t.get("instance_id") or t.get("lab_id") or t.get("bot_id") or "UNKNOWN"


def _instance_label(key: str, rows: list) -> str:
    named = next((r.get("instance_name") for r in rows if r.get("instance_name")), None)
    if named:
        return named
    lab = next((r.get("strategy_name") for r in rows if r.get("lab_id")), None)
    return lab or key


def session_key(t: dict) -> str:
    return t.get("entry_session") or "UNKNOWN"


def _session_label(key: str, _rows) -> str:
    return SESSION_LABELS.get(key, key.replace("_", " ").title())


def dashboard(trades: list[dict]) -> dict:
    """The summary cards above the journal table (respecting the filters)."""
    m = metrics(trades)
    closed = stat_trades(trades)
    strategies = group_by(closed, strategy_key)
    sessions = group_by(closed, session_key, _session_label)
    symbols = group_by(closed, lambda t: t.get("symbol"))
    return {
        "net_pnl": m["net_pnl"], "total_trades": m["total_trades"], "win_rate": m["win_rate"],
        "profit_factor": m["profit_factor"], "avg_r": m["avg_r"], "max_drawdown": m["max_drawdown"],
        "best_strategy": _best(strategies), "worst_strategy": _best(strategies, worst=True),
        "best_session": _best(sessions), "best_symbol": _best(symbols),
        "open_trades": m["open_trades"], "pending_orders": m["pending_orders"],
        "operational_events": m["operational_events"], "total_fees": m["total_fees"],
        "rule_violations": m["rule_violations"], "sample_warning": m["sample_warning"],
        "metrics": m,
    }


def strategy_performance(trades: list[dict], *, by: str = "strategy") -> list[dict]:
    key = {"strategy": strategy_key, "family": lambda t: t.get("strategy_family"),
           "instance": instance_key, "source": lambda t: t.get("trade_source")}.get(by, strategy_key)
    return group_by(stat_trades(trades) + [t for t in trades if not t.get("counts_in_stats")],
                    key, _instance_label if by == "instance" else None, full=True)


def strategy_comparison(trades: list[dict]) -> list[dict]:
    closed = stat_trades(trades)
    rows = []
    by_strategy: dict[str, list] = defaultdict(list)
    for t in closed:
        by_strategy[strategy_key(t)].append(t)
    for name, rows_in in by_strategy.items():
        m = metrics(rows_in)
        best_session = _best(group_by(rows_in, session_key, _session_label))
        best_symbol = _best(group_by(rows_in, lambda t: t.get("symbol")))
        families = Counter(t.get("strategy_family") for t in rows_in)
        modes = sorted({t.get("trading_mode") for t in rows_in if t.get("trading_mode")})
        rows.append({
            "strategy": name, "family": families.most_common(1)[0][0] if families else None,
            "modes": modes, "trades": m["total_trades"], "wins": m["wins"], "losses": m["losses"],
            "break_even": m["break_even"], "win_rate": m["win_rate"], "net_pnl": m["net_pnl"],
            "avg_planned_rr": m["avg_planned_rr"], "avg_realised_r": m["avg_r"],
            "expectancy": m["expectancy"], "expectancy_r": m["expectancy_r"],
            "profit_factor": m["profit_factor"], "max_drawdown": m["max_drawdown"],
            "avg_leverage": m["avg_leverage"], "avg_risk_pct": m["avg_risk_pct"],
            "total_fees": m["total_fees"],
            "best_session": best_session["label"] if best_session else None,
            "best_symbol": best_symbol["label"] if best_symbol else None,
            "sample_warning": m["sample_warning"],
            "verdict": _verdict(m),
        })
    return sorted(rows, key=lambda r: _rank_value({"net_pnl": r["net_pnl"], "avg_r": r["avg_realised_r"]}),
                  reverse=True)


def _verdict(m: dict) -> str:
    if not m["total_trades"]:
        return "NO_TRADES"
    exp = m.get("expectancy_r")
    if exp is None:
        exp = m.get("expectancy")
    label = "PROFITABLE" if (exp or 0) > 0 else "LOSING" if (exp or 0) < 0 else "FLAT"
    if m["total_trades"] < MIN_RELIABLE_SAMPLE:
        label += "_EARLY_SAMPLE"
    return label


def session_performance(trades: list[dict]) -> dict:
    closed = stat_trades(trades)
    rows = {r["key"]: r for r in group_by(closed, session_key, _session_label)}
    ordered = [rows.get(s) or {"key": s, "label": SESSION_LABELS[s], "total_trades": 0, "sample": 0}
               for s in SESSIONS]
    ordered += [r for k, r in rows.items() if k not in SESSIONS]
    by_strategy = {}
    for name in sorted({strategy_key(t) for t in closed}):
        subset = [t for t in closed if strategy_key(t) == name]
        by_strategy[name] = [r for r in group_by(subset, session_key, _session_label)]
        by_strategy[name].sort(key=lambda r: session_order([r["key"]])[0] if r["key"] in SESSIONS else "zz")
    preferred = [t for t in closed if t.get("in_preferred_session") is not None]
    return {
        "sessions": ordered,
        "by_strategy": by_strategy,
        "preferred_window": ({"inside": _compact(metrics([t for t in preferred if t["in_preferred_session"]])),
                              "outside": _compact(metrics([t for t in preferred if not t["in_preferred_session"]]))}
                             if preferred else None),
    }


def hour_performance(trades: list[dict]) -> list[dict]:
    closed = stat_trades(trades)
    out = []
    for hour in range(24):
        subset = [t for t in closed if t.get("entry_hour_london") == hour]
        m = metrics(subset)
        out.append({"hour": hour, "label": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
                    "trades": m["total_trades"], "net_pnl": m["net_pnl"], "win_rate": m["win_rate"],
                    "avg_r": m["avg_r"], "profit_factor": m["profit_factor"]})
    return out


def weekday_performance(trades: list[dict]) -> list[dict]:
    order = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    rows = {r["key"]: r for r in group_by(stat_trades(trades), lambda t: t.get("entry_weekday"))}
    return [rows[d] for d in order if d in rows]


def symbol_performance(trades: list[dict]) -> dict:
    closed = stat_trades(trades)
    by_strategy = {}
    for name in sorted({strategy_key(t) for t in closed}):
        by_strategy[name] = group_by([t for t in closed if strategy_key(t) == name], lambda t: t.get("symbol"))
    return {"overall": group_by(closed, lambda t: t.get("symbol")), "by_strategy": by_strategy}


def direction_performance(trades: list[dict]) -> dict:
    closed = stat_trades(trades)

    def split(rows_in):
        out = {}
        for side in ("LONG", "SHORT"):
            m = metrics([t for t in rows_in if t.get("direction") == side])
            out[side] = {"trades": m["total_trades"], "wins": m["wins"], "losses": m["losses"],
                         "win_rate": m["win_rate"], "net_pnl": m["net_pnl"], "avg_r": m["avg_r"],
                         "profit_factor": m["profit_factor"], "sample_warning": m["sample_warning"]}
        long_r, short_r = out["LONG"]["avg_r"], out["SHORT"]["avg_r"]
        out["stronger_side"] = (None if long_r is None or short_r is None or abs(long_r - short_r) < 0.1
                                else "LONG" if long_r > short_r else "SHORT")
        return out

    return {"overall": split(closed),
            "by_strategy": {name: split([t for t in closed if strategy_key(t) == name])
                            for name in sorted({strategy_key(t) for t in closed})}}


def leverage_performance(trades: list[dict]) -> list[dict]:
    closed = stat_trades(trades)
    buckets: dict[str, list] = defaultdict(list)
    for t in closed:
        lev = _num(t.get("leverage"))
        buckets[f"{lev:g}x" if lev is not None else "Not recorded"].append(t)
    rows = []
    for key, rows_in in buckets.items():
        m = metrics(rows_in)
        rows.append({
            "leverage": key, "trades": m["total_trades"], "win_rate": m["win_rate"],
            "net_pnl": m["net_pnl"], "avg_r": m["avg_r"], "avg_risk_pct": m["avg_risk_pct"],
            # leverage-neutral comparisons: R and return on margin, not raw P&L
            "avg_return_on_margin_pct": _r(_avg(t.get("return_on_margin_pct") for t in rows_in)),
            "avg_drawdown_r": _r(_avg(t.get("mae_r") for t in rows_in)),
            "avg_drawdown_amount": _r(_avg(t.get("mae_amount") for t in rows_in), 8),
            "max_drawdown": m["max_drawdown"], "sample_warning": m["sample_warning"],
        })
    return sorted(rows, key=lambda r: (r["leverage"] == "Not recorded",
                                       float(r["leverage"][:-1]) if r["leverage"].endswith("x") else 0))


def _reached_target(t: dict) -> bool:
    if t.get("exit_reason") == "TAKE_PROFIT":
        return True
    planned, gross = _num(t.get("planned_rr")), _num(t.get("gross_r"))
    return bool(planned and gross is not None and gross >= 0.98 * planned)


def rr_analysis(trades: list[dict]) -> dict:
    closed = [t for t in stat_trades(trades) if _num(t.get("planned_rr")) is not None]

    def block(rows_in):
        planned = _avg(t.get("planned_rr") for t in rows_in)
        realised = _avg(t.get("realised_r") for t in rows_in)
        winners = [t for t in rows_in if t.get("result") in WIN_RESULTS]
        realised_w = _avg(t.get("realised_r") for t in winners)
        reached = sum(1 for t in rows_in if _reached_target(t))
        stopped = sum(1 for t in rows_in if not _reached_target(t) and
                      t.get("exit_reason") in ("STOP_LOSS", "BREAK_EVEN_STOP", "TRAILING_STOP"))
        n = len(rows_in)
        return {
            "trades": n, "avg_planned_rr": _r(planned), "avg_realised_r": _r(realised),
            "avg_realised_r_winners": _r(realised_w),
            "difference_r": _r((realised - planned) if (planned is not None and realised is not None) else None),
            "winner_capture_pct": _r((realised_w / planned * 100) if (realised_w is not None and planned) else None, 2),
            "pct_full_target": _r(reached / n * 100 if n else None, 2),
            "pct_stopped_before_target": _r(stopped / n * 100 if n else None, 2),
            "pct_other_exit": _r((n - reached - stopped) / n * 100 if n else None, 2),
            "sample_warning": "INSUFFICIENT_SAMPLE" if n < MIN_RELIABLE_SAMPLE else None,
        }

    buckets = Counter()
    for t in stat_trades(trades):
        r = _num(t.get("realised_r"))
        if r is None:
            continue
        edge = max(-3, min(4, int(r // 1)))
        buckets[f"{edge}R to {edge + 1}R" if -3 < edge < 4 else ("≤ -3R" if edge <= -3 else "≥ 4R")] += 1
    return {"overall": block(closed),
            "by_strategy": {name: block([t for t in closed if strategy_key(t) == name])
                            for name in sorted({strategy_key(t) for t in closed})},
            "realised_r_distribution": dict(sorted(buckets.items()))}


def excursion_analysis(trades: list[dict]) -> dict:
    closed = stat_trades(trades)
    tracked = [t for t in closed if t.get("mfe_r") is not None or t.get("mae_r") is not None]
    winners = [t for t in tracked if t.get("result") in WIN_RESULTS and _num(t.get("mfe_r"))]
    losers = [t for t in tracked if t.get("result") in LOSS_RESULTS]
    capture = [float(t["realised_r"]) / float(t["mfe_r"]) for t in winners
               if _num(t.get("realised_r")) is not None and float(t["mfe_r"]) > 0]
    return {
        "coverage": {"tracked": len(tracked), "closed": len(closed),
                     "note": ("MFE/MAE are recorded only where the execution path tracked them; "
                              "untracked trades are excluded, never estimated.")},
        "avg_mfe_r": _r(_avg(t.get("mfe_r") for t in tracked)),
        "avg_mae_r": _r(_avg(t.get("mae_r") for t in tracked)),
        "avg_mfe_amount": _r(_avg(t.get("mfe_amount") for t in tracked), 8),
        "avg_mae_amount": _r(_avg(t.get("mae_amount") for t in tracked), 8),
        "avg_winner_mae_r": _r(_avg(t.get("mae_r") for t in winners)),
        "avg_loser_mfe_r": _r(_avg(t.get("mfe_r") for t in losers)),
        "losers_that_reached_1r": sum(1 for t in losers if (_num(t.get("mfe_r")) or 0) >= 1.0),
        "winner_capture_ratio": _r(_avg(capture)),
        "trades": [{"trade_ref": t["trade_ref"], "trade_id": t["trade_id"], "strategy": strategy_key(t),
                    "symbol": t.get("symbol"), "result": t.get("result"), "realised_r": _r(t.get("realised_r")),
                    "mfe_r": _r(t.get("mfe_r")), "mae_r": _r(t.get("mae_r")),
                    "mfe_amount": _r(t.get("mfe_amount"), 8), "mae_amount": _r(t.get("mae_amount"), 8)}
                   for t in sorted(tracked, key=_exit_key)[-200:]],
    }


def equity_curve(trades: list[dict]) -> list[dict]:
    total = 0.0
    total_r = 0.0
    out = []
    for t in sorted(stat_trades(trades), key=_exit_key):
        pnl, r = _num(t.get("net_pnl")), _num(t.get("realised_r"))
        total += pnl or 0.0
        total_r += r or 0.0
        out.append({"at": _exit_key(t), "trade_ref": t.get("trade_ref"), "net_pnl": _r(pnl, 8),
                    "cumulative_pnl": _r(total, 8), "cumulative_r": _r(total_r)})
    return out


def trend(trades: list[dict]) -> dict:
    """Is performance improving or deteriorating? Weekly buckets plus a
    recent-versus-prior comparison on average R, sample-gated."""
    closed = sorted(stat_trades(trades), key=_exit_key)
    weeks: dict[str, list] = defaultdict(list)
    for t in closed:
        weeks[iso_week_key(_exit_key(t)) or "UNKNOWN"].append(t)
    weekly = []
    cumulative = 0.0
    for week in sorted(weeks):
        m = metrics(weeks[week])
        cumulative += m["net_pnl"] or 0.0
        weekly.append({"week": week, "trades": m["total_trades"], "net_pnl": m["net_pnl"],
                       "win_rate": m["win_rate"], "avg_r": m["avg_r"], "cumulative_pnl": _r(cumulative, 8)})

    def direction(rows_in):
        n = len(rows_in)
        window = min(20, n // 2)
        if window < 5:
            return {"status": "INSUFFICIENT_DATA", "window": window, "detail":
                    f"{n} closed trades; at least 10 are needed to compare recent with prior performance."}
        recent, prior = rows_in[-window:], rows_in[-2 * window:-window]
        recent_r = _avg(t.get("realised_r") for t in recent)
        prior_r = _avg(t.get("realised_r") for t in prior)
        recent_m, prior_m = metrics(recent), metrics(prior)
        if recent_r is None or prior_r is None:
            metric, a, b = "net_pnl", recent_m["net_pnl"] or 0, prior_m["net_pnl"] or 0
            threshold = 0.0
        else:
            metric, a, b, threshold = "avg_r", recent_r, prior_r, 0.1
        status = ("IMPROVING" if a - b > threshold else "DETERIORATING" if b - a > threshold else "STABLE")
        return {"status": status, "window": window, "metric": metric,
                "recent": {"avg_r": _r(recent_r), "win_rate": recent_m["win_rate"], "net_pnl": recent_m["net_pnl"]},
                "prior": {"avg_r": _r(prior_r), "win_rate": prior_m["win_rate"], "net_pnl": prior_m["net_pnl"]},
                "detail": f"last {window} trades vs the {window} before them"}

    return {"weekly": weekly, "overall": direction(closed),
            "by_strategy": {name: direction([t for t in closed if strategy_key(t) == name])
                            for name in sorted({strategy_key(t) for t in closed})}}


def full_analytics(trades: list[dict]) -> dict:
    return {
        "summary": dashboard(trades),
        "comparison": strategy_comparison(trades),
        "strategies": strategy_performance(trades),
        "instances": strategy_performance(trades, by="instance"),
        "sessions": session_performance(trades),
        "hours": hour_performance(trades),
        "weekdays": weekday_performance(trades),
        "symbols": symbol_performance(trades),
        "directions": direction_performance(trades),
        "leverage": leverage_performance(trades),
        "rr": rr_analysis(trades),
        "excursions": excursion_analysis(trades),
        "trend": trend(trades),
        "equity_curve": equity_curve(trades),
    }


# ----------------------------------------------------------------- weekly review
def _pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _money(v) -> str:
    return "—" if not isinstance(v, (int, float)) else f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def weekly_review(week_trades: list[dict], prior_trades: list[dict], reviews: dict[str, dict],
                  *, week_key: str) -> dict:
    """Observations drawn only from the structured journal. It reports; it
    never changes a strategy, a risk setting or a trade."""
    closed = stat_trades(week_trades)
    observations: list[dict] = []

    def observe(category: str, text: str, *, strategy: Optional[str] = None, sample: int = 0,
                evidence: Optional[dict] = None) -> None:
        observations.append({"category": category, "strategy": strategy, "text": text, "sample": sample,
                             "confidence": ("LOW_SAMPLE" if sample < 10 else "MODERATE"
                                            if sample < MIN_RELIABLE_SAMPLE else "SUPPORTED"),
                             "evidence": evidence or {}})

    comparison = strategy_comparison(week_trades)
    for row in comparison:
        observe("profitability",
                f"{row['strategy']}: {row['trades']} trades, {_money(row['net_pnl'])}, "
                f"{_pct(row['win_rate'])} win rate, average {row['avg_realised_r'] or 0:+.2f}R"
                + (f", profit factor {row['profit_factor']}" if row['profit_factor'] is not None else "") + ".",
                strategy=row["strategy"], sample=row["trades"], evidence=row)
    if len(comparison) > 1:
        top, bottom = comparison[0], comparison[-1]
        observe("comparison", f"{top['strategy']} led the week ({_money(top['net_pnl'])}); "
                              f"{bottom['strategy']} trailed ({_money(bottom['net_pnl'])}).",
                sample=top["trades"] + bottom["trades"])

    by_strategy: dict[str, list] = defaultdict(list)
    for t in closed:
        by_strategy[strategy_key(t)].append(t)
    for name, rows_in in by_strategy.items():
        sessions = [r for r in group_by(rows_in, session_key, _session_label)
                    if r["total_trades"] >= MIN_OBSERVATION_SAMPLE and r["win_rate"] is not None]
        if len(sessions) >= 2:
            best = max(sessions, key=lambda r: r["win_rate"])
            worst = min(sessions, key=lambda r: r["win_rate"])
            if best["win_rate"] - worst["win_rate"] >= 15:
                observe("session", f"{name} had a {_pct(best['win_rate'])} win rate during the {best['label']} "
                                   f"session but only {_pct(worst['win_rate'])} during {worst['label']}.",
                        strategy=name, sample=best["total_trades"] + worst["total_trades"],
                        evidence={"best": best, "worst": worst})
        symbols = [r for r in group_by(rows_in, lambda t: t.get("symbol"))
                   if r["total_trades"] >= MIN_OBSERVATION_SAMPLE]
        positive = [r for r in symbols if (r["expectancy_r"] if r.get("expectancy_r") is not None
                                           else (r["expectancy"] or 0)) > 0]
        negative = [r for r in symbols if (r["expectancy_r"] if r.get("expectancy_r") is not None
                                           else (r["expectancy"] or 0)) < 0]
        if positive and negative:
            observe("symbol", f"{name} produced positive expectancy on {', '.join(r['key'] for r in positive)} "
                              f"but negative expectancy on {', '.join(r['key'] for r in negative)}.",
                    strategy=name, sample=sum(r["total_trades"] for r in positive + negative))
        sides = direction_performance(rows_in)["overall"]
        if (sides["LONG"]["trades"] >= MIN_OBSERVATION_SAMPLE and sides["SHORT"]["trades"] >= MIN_OBSERVATION_SAMPLE
                and sides["stronger_side"]):
            strong = sides["stronger_side"]
            weak = "SHORT" if strong == "LONG" else "LONG"
            observe("direction", f"{name} performed better {strong.lower()} ({sides[strong]['avg_r']:+.2f}R avg, "
                                 f"{_pct(sides[strong]['win_rate'])} win) than {weak.lower()} "
                                 f"({sides[weak]['avg_r']:+.2f}R, {_pct(sides[weak]['win_rate'])}).",
                    strategy=name, sample=sides["LONG"]["trades"] + sides["SHORT"]["trades"], evidence=sides)
        streak = longest = 0
        for t in sorted(rows_in, key=_exit_key):
            streak = streak + 1 if t.get("result") in LOSS_RESULTS else 0
            longest = max(longest, streak)
        if longest >= 3:
            observe("pattern", f"{name} had {longest} consecutive losses this week.", strategy=name,
                    sample=len(rows_in))

    rr = rr_analysis(week_trades)["overall"]
    if rr["trades"] and rr["avg_planned_rr"] is not None and rr["avg_realised_r_winners"] is not None:
        gap = rr["avg_planned_rr"] - rr["avg_realised_r_winners"]
        if gap > 0.25:
            observe("rr_execution", f"Average planned RR was {rr['avg_planned_rr']:.2f}R, but average realised "
                                    f"RR on winners was only {rr['avg_realised_r_winners']:.2f}R "
                                    f"({_pct(rr['pct_full_target'])} reached the full target).",
                    sample=rr["trades"], evidence=rr)
    m = metrics(week_trades)
    if m["max_drawdown"] is not None and m["max_drawdown"] < 0:
        observe("drawdown", f"Maximum drawdown this week was {_money(m['max_drawdown'])}"
                            + (f" ({m['max_drawdown_r']:.2f}R)" if m.get("max_drawdown_r") is not None else "") + ".",
                sample=m["total_trades"])
    lev = [r for r in leverage_performance(week_trades) if r["leverage"] != "Not recorded"]
    if len(lev) > 1:
        observe("leverage", "Leverage varied this week (" + ", ".join(
            f"{r['leverage']}: {r['trades']} trades, {r['avg_r'] or 0:+.2f}R avg" for r in lev)
            + "); compare strategies on R, not raw P&L.", sample=sum(r["trades"] for r in lev))
    violations = [t for t in closed if t.get("rule_violation")]
    if violations:
        observe("rule_violations", f"{len(violations)} trade(s) broke a rule: "
                                   + ", ".join(t["trade_ref"] for t in violations[:10]) + ".",
                sample=len(violations))
    mistakes = Counter()
    for t in closed:
        for mistake in (reviews.get(t["trade_id"]) or {}).get("mistakes") or []:
            mistakes[mistake] += 1
    for mistake, count in mistakes.most_common(5):
        if count >= 2:
            observe("repeated_failure", f"Repeated: “{mistake}” ({count} trades).", sample=count)
    operational = [t for t in week_trades if t.get("is_operational")]
    if operational:
        observe("operations", f"{len(operational)} order(s) ended as operational events "
                              f"({dict(Counter(t.get('result') for t in operational))}); "
                              "they are excluded from strategy statistics.", sample=len(operational))
    prior = metrics(prior_trades)
    if prior["total_trades"] and m["total_trades"]:
        observe("week_over_week", f"Net P&L {_money(m['net_pnl'])} vs {_money(prior['net_pnl'])} the week before; "
                                  f"average R {m['avg_r'] or 0:+.2f} vs {prior['avg_r'] or 0:+.2f}.",
                sample=m["total_trades"] + prior["total_trades"])
    if not closed:
        observe("activity", "No completed trades in this week for the selected modes.", sample=0)
    return {
        "week": week_key, "metrics": _compact(m), "comparison": comparison,
        "observations": observations,
        "guardrails": ["Observations only — strategy logic, risk settings and trades are never changed "
                       "by this review.",
                       f"Patterns under {MIN_RELIABLE_SAMPLE} trades are early signals, not evidence."],
    }
