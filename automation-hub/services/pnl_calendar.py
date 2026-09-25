"""App-wide realized P&L calendar: one read-only view over every trading ledger.

The rules -- which ledgers count, how a realization is identified and
de-duplicated, gross versus net, currency, date assignment, time-of-day
buckets and drawdown -- are written down in docs/PNL_CALENDAR.md. This module
is the only place those rules are implemented; the API and the UI read its
results and never recompute them.

Nothing here writes. Each collector reads one source through that source's own
interface and turns what it finds into ``Realization`` rows: one per realized
amount of P&L, with its settlement currency, the time it was realized and as
much attribution as the source recorded (and ``None`` for what it did not).
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZERO = Decimal(0)
_Q8 = Decimal("0.00000001")

SOURCES: dict[str, str] = {
    "trading_instance": "Trading Instance",
    "pa_lab": "Price Action Lab",
    "smc_lab": "SMC Lab",
    "adaptive_lab": "Adaptive MTF Lab",
    "paper_trading": "Paper Trading",
}

# The one definition of the time-of-day buckets (local hours, end exclusive).
# Night wraps midnight: for a given date it is 00:00-06:59 and 22:00-23:59.
TIME_OF_DAY: tuple[tuple[str, str, int, int], ...] = (
    ("morning", "Morning", 7, 12),
    ("afternoon", "Afternoon", 12, 17),
    ("evening", "Evening", 17, 22),
    ("night", "Night", 22, 7),
)

# Quote assets recognised at the end of a venue symbol, longest first, so that
# "BTCFDUSD" is FDUSD rather than USD.
_QUOTE_ASSETS = tuple(sorted(("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USD", "EUR", "GBP",
                              "JPY", "TRY", "BRL", "BTC", "ETH", "BNB"), key=len, reverse=True))


class CalendarError(ValueError):
    """A request the calendar cannot answer as asked (bad date, zone, filter)."""


# ------------------------------------------------------------------ helpers
def dec(value) -> Optional[Decimal]:
    """A Decimal from a stored value (via its string form), or None."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def amount(d: Optional[Decimal]) -> Optional[str]:
    """An exact, exponent-free string with eight decimal places."""
    if d is None:
        return None
    return format(d.quantize(_Q8, rounding=ROUND_HALF_UP), "f")


def parse_ts(value) -> Optional[datetime]:
    """An aware UTC datetime from an ISO string or epoch seconds/ms."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        dt = datetime.fromtimestamp(seconds, timezone.utc)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def quote_currency(symbol: Optional[str]) -> str:
    """Settlement currency of a linear contract or spot pair: its quote asset.

    ``ETH/USDT`` names its quote outright. A run-together symbol can end in
    two known quotes at once (``BTCUSD`` ends in ``TUSD`` as well as ``USD``);
    the longest one that leaves a base of at least three letters wins, so
    ``BTCTUSD`` is TUSD and ``BTCUSD`` is USD."""
    text = str(symbol or "").upper()
    parts = [p for p in re.split(r"[^A-Z0-9]+", text) if p]
    if len(parts) > 1:
        tail = parts[-1] if parts[-1] != "PERP" else (parts[-2] if len(parts) > 2 else "")
        if tail in _QUOTE_ASSETS:
            return tail
    s = "".join(parts).removesuffix("PERP")
    matches = [q for q in _QUOTE_ASSETS if s.endswith(q) and len(s) > len(q)]
    for quote in matches:
        if len(s) - len(quote) >= 3:
            return quote
    return matches[0] if matches else "UNKNOWN"


def resolve_zone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        raise CalendarError(f"Unknown timezone {name!r}.") from None


def time_bucket(hour: int) -> str:
    for key, _label, start, end in TIME_OF_DAY:
        if (start <= hour < end) if start < end else (hour >= start or hour < end):
            return key
    raise AssertionError(hour)  # the buckets cover all 24 hours


def _side(value) -> Optional[str]:
    v = str(value or "").lower()
    return "long" if v in ("long", "buy", "bullish") else "short" if v in ("short", "sell", "bearish") else None


# ----------------------------------------------------------- the row type
@dataclass
class Realization:
    id: str
    trade_id: str
    source: str
    account: str
    currency: str
    closed_at: datetime
    gross: Decimal
    fees: Decimal
    funding: Decimal
    net: Decimal
    pnl_basis: str
    symbol: Optional[str] = None
    side: Optional[str] = None
    instance_id: Optional[str] = None
    instance_name: Optional[str] = None
    strategy: Optional[str] = None
    timeframe: Optional[str] = None
    opened_at: Optional[datetime] = None
    entry_price: Optional[Decimal] = None
    exit_price: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    rr: Optional[Decimal] = None
    rr_basis: Optional[str] = None
    exit_reason: Optional[str] = None
    final: bool = True
    missing: list[str] = field(default_factory=list)

    def public(self, tz: ZoneInfo) -> dict:
        row = asdict(self)
        for key in ("gross", "fees", "funding", "net", "entry_price", "exit_price", "quantity", "rr"):
            row[key] = amount(row[key])
        for key in ("closed_at", "opened_at"):
            value = getattr(self, key)
            row[key] = value.astimezone(tz).isoformat() if value else None
        row["source_label"] = SOURCES.get(self.source, self.source)
        row["duration_s"] = (int((self.closed_at - self.opened_at).total_seconds())
                             if self.opened_at else None)
        row["partial"] = not self.final
        return row


def _missing(**fields) -> list[str]:
    return [name for name, value in fields.items() if value in (None, "")]


# -------------------------------------------------------------- collectors
def collect_ledger(rows: Iterable[dict], *, scope: str, default_source: str,
                   instances: Optional[dict] = None,
                   journal: Optional[Callable[[str], Optional[dict]]] = None) -> tuple[list[Realization], dict]:
    """Closed ``paper_trades`` rows from one ledger. The stored P&L is already
    net of the round-trip fee, so it is used as is and never charged again."""
    instances = instances or {}
    out, skipped = [], defaultdict(int)
    open_positions = 0
    for t in rows:
        status = str(t.get("status") or "").lower()
        if status == "open":
            open_positions += 1
            continue
        if status != "closed":
            continue
        dataset = str(t.get("source") or "paper").lower()
        if dataset not in ("paper", "live"):
            skipped[f"not_trading:{dataset}"] += 1
            continue
        closed_at = parse_ts(t.get("closed_at"))
        net = dec(t.get("realized_pnl"))
        if net is None:
            net = dec(t.get("pnl"))
        if closed_at is None or net is None:
            skipped["no_close_time_or_pnl"] += 1
            continue
        fees = dec(t.get("fees")) or ZERO
        instance_id = str(t.get("instance_id") or "") or None
        source = "trading_instance" if instance_id and default_source == "paper_trading" else default_source
        meta = instances.get(instance_id) if instance_id else None
        strategy_id = str(t.get("strategy_id") or "") or None
        entry = journal(str(t.get("id"))) if journal else None
        # Attribution recorded with the trade wins over any current configuration.
        strategy = (entry or {}).get("strategy_name") or strategy_id
        if not (entry or {}).get("strategy_name") and strategy_id and meta and \
                strategy_id.split(":", 1)[0] == meta.get("strategy_key"):
            strategy = meta.get("strategy_label") or strategy_id
        sections = (entry or {}).get("sections") or {}
        exit_decision = sections.get("exit_decision") if isinstance(sections, dict) else None
        exit_reason = (exit_decision or {}).get("exit_reason") if isinstance(exit_decision, dict) else None
        timeframe = (entry or {}).get("timeframe") or None
        stop = dec(t.get("stop"))
        rr = dec(t.get("rr")) if stop not in (None, ZERO) else None
        instance_name = None
        if instance_id:
            instance_name = ((entry or {}).get("instance_name") or (meta.get("name") if meta else None)
                             or "Deleted instance")
        symbol = t.get("symbol")
        out.append(Realization(
            id=f"{scope}:{t.get('id')}", trade_id=f"{scope}:{t.get('id')}", source=source,
            account=scope, currency=quote_currency(symbol), closed_at=closed_at,
            gross=net + fees, fees=fees, funding=ZERO, net=net, pnl_basis="ledger_net",
            symbol=symbol, side=_side(t.get("side")), instance_id=instance_id,
            instance_name=instance_name, strategy=strategy, timeframe=timeframe,
            opened_at=parse_ts(t.get("opened_at")), entry_price=dec(t.get("entry")),
            exit_price=dec(t.get("exit")), quantity=dec(t.get("size")), rr=rr,
            rr_basis="gross price R" if rr is not None else None, exit_reason=exit_reason,
            missing=_missing(strategy=strategy, timeframe=timeframe, exit_reason=exit_reason,
                             **({"instance": instance_name} if source == "trading_instance" else {}))))
    return out, {"skipped": dict(skipped), "open_positions": open_positions}


def read_paper_trades(ledger, *, page: int = 1000) -> list[dict]:
    """Every ``paper_trades`` row of a ledger, read-only.

    SQLite returns them all in one query. A Supabase (PostgREST) select is
    capped by the server's max-rows setting (1,000 by default), so a plain
    ``get_paper_trades()`` would silently drop the oldest trades; that backend
    is read page by page, ordered by id, until a page comes back empty. A
    ledger in read-only degraded mode holds nothing real, so it is an error
    rather than an empty history."""
    if getattr(ledger, "read_only_degraded", False):
        raise RuntimeError(f"primary ledger unavailable: {getattr(ledger, 'degraded_reason', '')}")
    from data.ledger import SupabaseLedger, remote_call_with_retry
    if not isinstance(ledger, SupabaseLedger):
        return list(ledger.get_paper_trades())
    rows: list[dict] = []
    while True:
        start = len(rows)
        chunk = remote_call_with_retry(
            lambda: ledger._t("paper_trades").select("*").order("id")
            .range(start, start + page - 1).execute()).data or []
        if not chunk:
            return rows
        rows.extend(chunk)


_LAB_TABLES = {
    # source: (funding table, order-metadata columns, order-metadata table)
    "pa_lab": ("pa_funding_events", "order_id,strategy_id,direction,setup_id,proposal_id", "pa_order_meta"),
    "smc_lab": ("smc_funding_events",
                "order_id,ownership,model_id,model_version,direction,setup_id,proposal_id", "smc_order_meta"),
}


def v2_lab_history(account, *, source: str, currency: str) -> dict:
    """Read a PaperBrokerV2 lab's full history without changing the lab.

    The lab modules are under a source-hash freeze (data/pr6_real_paper_freeze.json),
    so this adapter lives here: it uses the lab's public ``sessions()``,
    ``session()`` and ``broker.export_state()``, and plain SELECTs on its own
    metadata tables through the lab's connection and lock. The live broker is
    authoritative for the active session; ended sessions come from the broker
    snapshot each one saved when it ended."""
    funding_table, meta_columns, meta_table = _LAB_TABLES[source]
    lock = getattr(account, "_lock", None) or threading.RLock()
    with lock:
        current_id = (account.session() or {}).get("id")
        live_fills = account.broker.export_state()["fills"]
        sessions = []
        for s in account.sessions():
            sid = s["id"]
            fills = live_fills if sid == current_id else (s.get("state") or {}).get("fills", [])
            funding = [dict(r) for r in account._db.execute(
                f"SELECT symbol,funding_time,amount,applied FROM {funding_table} "
                "WHERE session_id=? AND applied=1", (sid,))]
            meta = {r["order_id"]: dict(r) for r in account._db.execute(
                f"SELECT {meta_columns} FROM {meta_table} WHERE session_id=?", (sid,))}
            sessions.append({"session_id": sid, "symbol": s.get("symbol"), "timeframe": s.get("timeframe"),
                             "strategy": s.get("model_id") or s.get("strategy_id"), "fills": fills,
                             "funding": funding, "order_meta": meta})
    return {"currency": currency, "sessions": sessions}


def collect_v2_lab(history: dict, *, source: str) -> tuple[list[Realization], dict]:
    """Reducing fills from a PaperBrokerV2 lab, grouped into position episodes.

    Each fill that reduces a position realizes its own gross P&L (as booked by
    the broker). Net = gross - its own fee - the episode's entry fees and
    funding, shared out in proportion to the quantity each exit closes."""
    currency = str(history.get("currency") or "UNKNOWN").upper()
    out: list[Realization] = []
    open_positions = 0
    for session in history.get("sessions") or []:
        session_id = str(session.get("session_id") or "")
        order_meta = session.get("order_meta") or {}
        fills = sorted((f for f in session.get("fills") or [] if f.get("id")),
                       key=lambda f: (str(f.get("fill_timestamp") or f.get("timestamp") or ""), str(f["id"])))
        funding = [f for f in session.get("funding") or [] if f.get("applied", 1)]
        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for f in fills:
            by_symbol[str(f.get("symbol") or "").upper()].append(f)
        for symbol, symbol_fills in by_symbol.items():
            episodes = _episodes(symbol_fills)
            for ep in episodes:
                if ep["open_qty"] > 0:
                    open_positions += 1
                out.extend(_episode_realizations(
                    ep, source=source, session=session, session_id=session_id, symbol=symbol,
                    currency=currency, order_meta=order_meta,
                    funding=[f for f in funding if str(f.get("symbol") or "").upper() == symbol]))
    return out, {"open_positions": open_positions}


def _episodes(fills: list[dict]) -> list[dict]:
    episodes, current, position = [], None, ZERO
    for f in fills:
        qty = dec(f.get("quantity")) or ZERO
        if qty <= 0:
            continue
        direction = Decimal(1) if str(f.get("side")).lower() == "buy" else Decimal(-1)
        fee = dec(f.get("fee")) or ZERO
        if position == 0 or (position > 0) == (direction > 0):
            if current is None or position == 0:
                current = {"entries": [], "exits": [], "side": "long" if direction > 0 else "short",
                           "first_fill": f, "entry_qty": ZERO, "open_qty": ZERO}
                episodes.append(current)
            current["entries"].append({"fill": f, "qty": qty, "fee": fee})
            current["entry_qty"] += qty
            position += direction * qty
            current["open_qty"] = abs(position)
            continue
        close_qty = min(qty, abs(position))
        exit_fee = fee * close_qty / qty
        current["exits"].append({"fill": f, "qty": close_qty, "fee": exit_fee,
                                 "gross": dec(f.get("realized_pnl")) or ZERO})
        position += direction * close_qty
        current["open_qty"] = abs(position)
        remainder = qty - close_qty
        if remainder > 0:   # one fill closed the position and opened the other way
            current = {"entries": [{"fill": f, "qty": remainder, "fee": fee - exit_fee}],
                       "exits": [], "side": "long" if direction > 0 else "short", "first_fill": f,
                       "entry_qty": remainder, "open_qty": remainder}
            episodes.append(current)
            position = direction * remainder
    return episodes


def _episode_realizations(ep: dict, *, source: str, session: dict, session_id: str, symbol: str,
                          currency: str, order_meta: dict, funding: list[dict]) -> list[Realization]:
    if not ep["exits"]:
        return []
    entry_qty = ep["entry_qty"] or Decimal(1)
    entry_fees = sum((e["fee"] for e in ep["entries"]), ZERO)
    priced = [e for e in ep["entries"] if dec(e["fill"].get("price")) is not None]
    entry_cost = sum((dec(e["fill"].get("price")) * e["qty"] for e in priced), ZERO)
    priced_qty = sum((e["qty"] for e in priced), ZERO)
    avg_entry = entry_cost / priced_qty if priced_qty else None
    opened_at = parse_ts(ep["first_fill"].get("fill_timestamp") or ep["first_fill"].get("timestamp"))
    last_exit = parse_ts(ep["exits"][-1]["fill"].get("fill_timestamp") or ep["exits"][-1]["fill"].get("timestamp"))
    funding_total = sum((dec(f.get("amount")) or ZERO for f in funding
                         if opened_at and last_exit and
                         opened_at <= (parse_ts(f.get("funding_time") or f.get("funding_timestamp")) or opened_at)
                         <= last_exit), ZERO)
    entry_fill = ep["entries"][0]["fill"]
    meta = order_meta.get(str(entry_fill.get("order_id"))) or {}
    strategy = (meta.get("strategy_id") or
                (f"{meta['model_id']} {meta.get('model_version') or ''}".strip() if meta.get("model_id") else None) or
                entry_fill.get("strategy") or session.get("strategy") or None)
    timeframe = entry_fill.get("timeframe") or session.get("timeframe") or None
    stop = dec(entry_fill.get("stop_loss"))
    target = dec(entry_fill.get("take_profit"))
    out, closed_so_far = [], ZERO
    for i, x in enumerate(ep["exits"]):
        f = x["fill"]
        share = x["qty"] / entry_qty
        entry_fee_share = entry_fees * share
        funding_share = funding_total * share
        net = x["gross"] - x["fee"] - entry_fee_share - funding_share
        closed_so_far += x["qty"]
        final = i == len(ep["exits"]) - 1 and ep["open_qty"] == 0
        exit_price = dec(f.get("price"))
        rr = None
        if stop is not None and avg_entry is not None and exit_price is not None and avg_entry != stop:
            move = (exit_price - avg_entry) if ep["side"] == "long" else (avg_entry - exit_price)
            rr = move / abs(avg_entry - stop)
        closed_at = parse_ts(f.get("fill_timestamp") or f.get("timestamp"))
        if closed_at is None:
            continue
        exit_reason = _v2_exit_reason(f, order_meta, stop=stop, target=target)
        out.append(Realization(
            id=f"{source}:fill:{f['id']}", trade_id=f"{source}:{ep['first_fill']['id']}", source=source,
            account=f"{source}:{session_id}" if session_id else source, currency=currency,
            closed_at=closed_at, gross=x["gross"], fees=x["fee"] + entry_fee_share, funding=funding_share,
            net=net, pnl_basis="fills_gross_minus_costs", symbol=symbol, side=ep["side"],
            strategy=strategy, timeframe=timeframe, opened_at=opened_at, entry_price=avg_entry,
            exit_price=exit_price, quantity=x["qty"], rr=rr,
            rr_basis="gross price R" if rr is not None else None, exit_reason=exit_reason, final=final,
            missing=_missing(strategy=strategy, timeframe=timeframe, exit_reason=exit_reason)))
    return out


def _v2_exit_reason(fill: dict, order_meta: dict, *, stop, target) -> Optional[str]:
    meta = order_meta.get(str(fill.get("order_id"))) or {}
    if meta.get("ownership"):
        return str(meta["ownership"]).replace("_", " ")
    if meta.get("reason"):
        return str(meta["reason"])
    if str(fill.get("order_id") or "").startswith("protective-"):
        price = dec(fill.get("price"))
        if price is not None and stop is not None and target is not None:
            return "Stop loss" if abs(price - stop) <= abs(price - target) else "Take profit"
        return "Protective exit"
    return None


# ------------------------------------------------------------- aggregation
@dataclass(frozen=True)
class Filters:
    source: Optional[str] = None
    instance_id: Optional[str] = None
    strategy: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None

    def __post_init__(self):
        if self.source and self.source not in SOURCES:
            raise CalendarError(f"Unknown source {self.source!r}. Choose one of: {', '.join(SOURCES)}.")

    def match(self, r: Realization) -> bool:
        return ((not self.source or r.source == self.source) and
                (not self.instance_id or r.instance_id == self.instance_id) and
                (not self.strategy or r.strategy == self.strategy) and
                (not self.symbol or (r.symbol or "").upper() == self.symbol.upper()) and
                (not self.timeframe or r.timeframe == self.timeframe))

    def active(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


def _drawdown(rows: list[Realization]) -> Decimal:
    level = peak = worst = ZERO
    for r in sorted(rows, key=lambda r: (r.closed_at, r.id)):
        level += r.net
        peak = max(peak, level)
        worst = max(worst, peak - level)
    return worst


def _trade_totals(all_rows: Iterable[Realization]) -> dict[str, Decimal]:
    """Each trade's total net P&L across all its realizations, computed once per
    view (a trade can span several days, so no single window holds all of it)."""
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in all_rows:
        totals[r.trade_id] += r.net
    return totals


def _trade_outcomes(window: list[Realization], totals: dict[str, Decimal]) -> dict[str, str]:
    """win / loss / breakeven per trade whose final realization is in window,
    judged on the trade's total net P&L across all its realizations."""
    finals = {r.trade_id for r in window if r.final}
    return {tid: ("win" if totals[tid] > 0 else "loss" if totals[tid] < 0 else "breakeven") for tid in finals}


def _mean(values: list[Decimal]) -> Optional[Decimal]:
    return sum(values, ZERO) / len(values) if values else None


def _money_summary(rows: list[Realization], totals: dict[str, Decimal]) -> dict[str, dict]:
    by_currency: dict[str, list[Realization]] = defaultdict(list)
    for r in rows:
        by_currency[r.currency].append(r)
    out = {}
    for currency, items in sorted(by_currency.items()):
        outcomes = _trade_outcomes(items, totals)
        closed = len(outcomes)
        wins = sum(1 for v in outcomes.values() if v == "win")
        losses = sum(1 for v in outcomes.values() if v == "loss")
        net = sum((r.net for r in items), ZERO)
        gross_profit = sum((r.net for r in items if r.net > 0), ZERO)
        gross_loss = -sum((r.net for r in items if r.net < 0), ZERO)
        # Per-trade figures use each closed trade's total net over all its exits.
        trade_nets = [totals[tid] for tid in outcomes]
        won = [t for t in trade_nets if t > 0]
        lost = [-t for t in trade_nets if t < 0]
        out[currency] = {
            "net": amount(net),
            "gross_profit": amount(gross_profit),
            "gross_loss": amount(gross_loss),
            "fees": amount(sum((r.fees for r in items), ZERO)),
            "funding": amount(sum((r.funding for r in items), ZERO)),
            "closed_trades": closed, "realizations": len(items),
            "wins": wins, "losses": losses, "breakeven": closed - wins - losses,
            "win_rate": (amount(Decimal(wins) * 100 / Decimal(closed)) if closed else None),
            "max_drawdown": amount(_drawdown(items)),
            "state": "profit" if net > 0 else "loss" if net < 0 else "breakeven",
            # None when undefined: no losses means no profit factor, not infinity.
            "profit_factor": amount(gross_profit / gross_loss) if gross_loss > 0 else None,
            "avg_win": amount(_mean(won)),
            "avg_loss": amount(_mean(lost)),              # positive magnitude
            "largest_win": amount(max(won)) if won else None,
            "largest_loss": amount(max(lost)) if lost else None,   # positive magnitude
            "expectancy": amount(_mean(trade_nets)),
        }
    return out


def _day_state(money: dict[str, dict]) -> str:
    states = {m["state"] for m in money.values()}
    if not states:
        return "none"
    return states.pop() if len(states) == 1 else "mixed"


def _streaks(nets: list[Decimal]) -> tuple[int, int]:
    """Longest run of consecutive winning and of losing trading days (days
    without trades neither extend nor break a run)."""
    best_win = best_loss = run_win = run_loss = 0
    for n in nets:
        run_win = run_win + 1 if n > 0 else 0
        run_loss = run_loss + 1 if n < 0 else 0
        best_win, best_loss = max(best_win, run_win), max(best_loss, run_loss)
    return best_win, best_loss


def month_view(rows: list[Realization], *, year: int, month: int, tz: ZoneInfo) -> dict:
    if not (1 <= month <= 12) or not (1970 <= year <= 2200):
        raise CalendarError("month must be 1-12 and year a four-digit year.")
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    totals = _trade_totals(rows)
    by_day: dict[date, list[Realization]] = defaultdict(list)
    in_month = []
    for r in rows:
        d = r.closed_at.astimezone(tz).date()
        if first <= d <= last:
            by_day[d].append(r)
            in_month.append(r)
    days = []
    for n in range(last.day):
        d = first + timedelta(days=n)
        money = _money_summary(by_day.get(d, []), totals)
        days.append({"date": d.isoformat(), "state": _day_state(money), "by_currency": money,
                     "closed_trades": sum(m["closed_trades"] for m in money.values()),
                     "realizations": sum(m["realizations"] for m in money.values())})

    # Monday-first weeks of the grid; a week's total counts only its days that
    # fall inside this month.
    weeks = []
    start = first - timedelta(days=first.weekday())
    while start <= last:
        lo, hi = max(start, first), min(start + timedelta(days=6), last)
        group = [r for n in range((hi - lo).days + 1) for r in by_day.get(lo + timedelta(days=n), [])]
        money = _money_summary(group, totals)
        weeks.append({"start": start.isoformat(), "from": lo.isoformat(), "to": hi.isoformat(),
                      "state": _day_state(money), "by_currency": money,
                      "closed_trades": sum(m["closed_trades"] for m in money.values()),
                      "realizations": len(group)})
        start += timedelta(days=7)

    summary = _money_summary(in_month, totals)
    for currency, block in summary.items():
        nets = [(d["date"], Decimal(d["by_currency"][currency]["net"]))
                for d in days if currency in d["by_currency"]]
        best = max(nets, key=lambda x: x[1]) if nets else None
        worst = min(nets, key=lambda x: x[1]) if nets else None
        block["best_day"] = {"date": best[0], "net": amount(best[1])} if best else None
        block["worst_day"] = {"date": worst[0], "net": amount(worst[1])} if worst else None
        block["trading_days"] = len(nets)
        block["winning_days"] = sum(1 for _, n in nets if n > 0)
        block["losing_days"] = sum(1 for _, n in nets if n < 0)
        block["breakeven_days"] = sum(1 for _, n in nets if n == 0)
        block["longest_winning_streak"], block["longest_losing_streak"] = _streaks([n for _, n in nets])
        running = ZERO
        curve = []
        for day_iso, n in nets:
            running += n
            curve.append({"date": day_iso, "net": amount(n), "cumulative": amount(running)})
        block["cumulative"] = curve
    return {"year": year, "month": month, "days": days, "weeks": weeks, "summary": summary,
            "currencies": sorted(summary)}


def day_view(rows: list[Realization], *, day: date, tz: ZoneInfo) -> dict:
    items = sorted((r for r in rows if r.closed_at.astimezone(tz).date() == day),
                   key=lambda r: (r.closed_at, r.id))
    totals = _trade_totals(rows)
    summary = _money_summary(items, totals)
    outcomes = _trade_outcomes(items, totals)

    def breakdown(key: Callable[[Realization], tuple]) -> list[dict]:
        groups: dict[tuple, list[Realization]] = defaultdict(list)
        for r in items:
            groups[key(r)].append(r)
        result = []
        for k, group in groups.items():
            money = _money_summary(group, totals)
            rrs = [r.rr for r in group if r.rr is not None]
            result.append({"key": list(k), "by_currency": money,
                           "closed_trades": sum(m["closed_trades"] for m in money.values()),
                           "realizations": len(group),
                           "avg_rr": amount(sum(rrs, ZERO) / len(rrs)) if rrs else None})
        return sorted(result, key=lambda g: -sum(abs(Decimal(m["net"])) for m in g["by_currency"].values()))

    sources = breakdown(lambda r: (r.source, SOURCES.get(r.source, r.source), r.instance_id or "",
                                   r.instance_name or ""))
    strategies = breakdown(lambda r: (r.strategy or "",))
    buckets = []
    for key, label, start, end in TIME_OF_DAY:
        group = [r for r in items if time_bucket(r.closed_at.astimezone(tz).hour) == key]
        buckets.append({"key": key, "label": label, "start": f"{start:02d}:00",
                        "end": f"{(end - 1) % 24:02d}:59", "realizations": len(group),
                        "by_currency": _money_summary(group, totals)})
    hourly = []
    for hour in range(24):
        group = [r for r in items if r.closed_at.astimezone(tz).hour == hour]
        if group:
            hourly.append({"hour": hour, "bucket": time_bucket(hour), "realizations": len(group),
                           "by_currency": _money_summary(group, totals)})
    trades = []
    for r in items:
        row = r.public(tz)
        row["outcome"] = outcomes.get(r.trade_id) if r.final else None
        trades.append(row)
    return {"date": day.isoformat(), "summary": summary, "currencies": sorted(summary),
            "state": _day_state(summary), "sources": sources, "strategies": strategies,
            "time_of_day": buckets, "hourly": hourly, "trades": trades}


# Spreadsheet columns of the export, in order. Amounts stay exact strings.
EXPORT_COLUMNS = (
    "closed_at", "opened_at", "source", "source_label", "account", "instance_id", "instance_name",
    "strategy", "timeframe", "symbol", "side", "quantity", "entry_price", "exit_price", "currency",
    "gross", "fees", "funding", "net", "pnl_basis", "rr", "rr_basis", "exit_reason", "partial",
    "outcome", "trade_id", "id", "missing",
)
# Free-text columns that could start with a formula character; numbers never are.
_TEXT_COLUMNS = {"source_label", "account", "instance_id", "instance_name", "strategy", "timeframe",
                 "symbol", "exit_reason", "trade_id", "id"}


def _cell(column: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    text = str(value)
    # A spreadsheet runs a cell starting with = + - @ as a formula; quote it.
    if column in _TEXT_COLUMNS and text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def export_rows(rows: list[Realization], *, start: date, end: date, tz: ZoneInfo) -> list[list[str]]:
    """Every realization closed between start and end (calendar dates in tz,
    inclusive), oldest first, as spreadsheet rows under EXPORT_COLUMNS."""
    totals = _trade_totals(rows)
    items = sorted((r for r in rows if start <= r.closed_at.astimezone(tz).date() <= end),
                   key=lambda r: (r.closed_at, r.id))
    outcomes = _trade_outcomes(items, totals)
    out = []
    for r in items:
        row = r.public(tz)
        row["outcome"] = outcomes.get(r.trade_id) if r.final else None
        out.append([_cell(c, row.get(c)) for c in EXPORT_COLUMNS])
    return out


def filter_options(rows: list[Realization]) -> dict:
    def distinct(values):
        return sorted({v for v in values if v})
    instances = {}
    for r in rows:
        if r.instance_id:
            instances[r.instance_id] = {"id": r.instance_id, "name": r.instance_name,
                                        "source": r.source}
    return {"sources": [{"key": k, "label": v, "trades": sum(1 for r in rows if r.source == k)}
                        for k, v in SOURCES.items()],
            "instances": sorted(instances.values(), key=lambda x: (x["name"] or "", x["id"])),
            "strategies": distinct(r.strategy for r in rows),
            "symbols": distinct((r.symbol or "").upper() for r in rows),
            "timeframes": distinct(r.timeframe for r in rows)}


# ------------------------------------------------------------------ service
Collector = Callable[[], tuple[list[Realization], dict]]


class PnlCalendar:
    """Collects every source (cached briefly) and answers month/day queries."""

    def __init__(self, collectors: dict[str, Collector], *, ttl_s: float = 15.0,
                 clock: Callable[[], float] = time.monotonic):
        self.collectors = collectors
        self.ttl_s = ttl_s
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: Optional[tuple[float, list[Realization], dict]] = None

    def collect(self, *, fresh: bool = False) -> tuple[list[Realization], dict]:
        with self._lock:
            if not fresh and self._cache and self.clock() - self._cache[0] < self.ttl_s:
                return self._cache[1], self._cache[2]
            rows: dict[str, Realization] = {}
            status, duplicates = {}, 0
            for name, collect in self.collectors.items():
                try:
                    found, info = collect()
                except Exception as exc:  # noqa: BLE001 -- one broken source must not hide the rest
                    status[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                    continue
                kept = 0
                for r in found:
                    if r.id in rows:
                        duplicates += 1
                        continue
                    rows[r.id] = r
                    kept += 1
                status[name] = {"ok": True, "realizations": kept, **info}
            diagnostics = {"sources": status, "duplicates_dropped": duplicates,
                           "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._cache = (self.clock(), list(rows.values()), diagnostics)
            return self._cache[1], self._cache[2]

    def _filtered(self, filters: Filters, fresh: bool) -> tuple[list[Realization], dict]:
        rows, diagnostics = self.collect(fresh=fresh)
        return [r for r in rows if filters.match(r)], diagnostics

    def month(self, *, year: int, month: int, tz: ZoneInfo, filters: Filters = Filters(),
              fresh: bool = False) -> dict:
        rows, diagnostics = self._filtered(filters, fresh)
        return {**month_view(rows, year=year, month=month, tz=tz), "timezone": tz.key,
                "filters": filters.active(), "diagnostics": diagnostics}

    def day(self, *, day: date, tz: ZoneInfo, filters: Filters = Filters(), fresh: bool = False) -> dict:
        rows, diagnostics = self._filtered(filters, fresh)
        return {**day_view(rows, day=day, tz=tz), "timezone": tz.key, "filters": filters.active(),
                "diagnostics": diagnostics}

    def export(self, *, start: date, end: date, tz: ZoneInfo, filters: Filters = Filters(),
               fresh: bool = False) -> list[list[str]]:
        if end < start or (end - start).days > 400:
            raise CalendarError("Export a range of at most 400 days, start before end.")
        rows, _ = self._filtered(filters, fresh)
        return export_rows(rows, start=start, end=end, tz=tz)

    def options(self) -> dict:
        rows, diagnostics = self.collect()
        return {**filter_options(rows), "diagnostics": diagnostics}
