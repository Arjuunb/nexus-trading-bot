"""Build e2e/fixtures/calendar.json, the mocked /calendar/* responses.

The responses are not written by hand: the real calendar service runs over
trades produced by the real PaperExecutionEngine and PaperBrokerV2, with only
their timestamps pinned so the month is fixed (September 2026). Regenerate
after changing the calendar API:

    cd automation-hub && python ../automation-hub-dashboard/e2e/fixtures/generate_calendar_fixture.py
"""
import json, sys, tempfile
from datetime import date
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "automation-hub"))
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from execution.paper_broker_v2 import PaperBrokerV2
from services.fill_model import PerfectFill
from services import pnl_calendar as cal

tmp = Path(tempfile.mkdtemp())
class Fees(PerfectFill):
    def fee_pct(self, *, maker=False): return 0.0004
ledger = SqliteLedger(str(tmp / "ledger.db"))
eng = PaperExecutionEngine(ledger, 100_000, fill_model=Fees())
INST = "a3f9c2d1e8b74c0f"
plan = [  # symbol, side, entry, exit, size, stop, opened, closed, instance, strategy
    ("BTCUSDT", "long", 60000, 60450, 0.2, 59700, "2026-09-02T08:05:00+00:00", "2026-09-02T09:40:00+00:00", INST, "native_smc:1.4"),
    ("ETHUSDT", "short", 2500, 2512, 3, 2520, "2026-09-02T13:00:00+00:00", "2026-09-02T14:10:00+00:00", INST, "native_smc:1.4"),
    ("BTCUSDT", "long", 61000, 60800, 0.2, 60700, "2026-09-08T10:00:00+00:00", "2026-09-08T11:30:00+00:00", INST, "native_smc:1.4"),
    ("SOLUSDT", "long", 140, 140, 10, 138, "2026-09-10T18:00:00+00:00", "2026-09-10T19:00:00+00:00", "", ""),
    ("EURGBP", "short", 0.8600, 0.8550, 5000, 0.8650, "2026-09-10T07:10:00+00:00", "2026-09-10T15:45:00+00:00", INST, "price_action_v1:0.1"),
    ("BTCUSDT", "short", 62000, 61400, 0.1, 62400, "2026-09-16T21:30:00+00:00", "2026-09-17T00:20:00+00:00", INST, "native_smc:1.4"),
]
for sym, side, e, x, size, stop, opened, closed, inst, strat in plan:
    eng.open(symbol=sym, side=side, size=size, entry=e, stop=stop, strategy_id=strat)
    eng.close(symbol=sym, exit_price=x)
    tid = ledger.get_paper_trades()[0]["id"]
    ledger._c.execute("UPDATE paper_trades SET opened_at=?, closed_at=?, instance_id=? WHERE id=?", (opened, closed, inst, tid))
    ledger._c.commit()
instances = {INST: {"name": "Native SMC · BTCUSDT", "strategy_key": "native_smc", "strategy_label": "Native SMC", "mode": "trading"}}
journal = {}
rows, _ = cal.collect_ledger(ledger.get_paper_trades(), scope="ledger", default_source="paper_trading",
                             instances=instances, journal=lambda tid: None)

def broker(name):
    return PaperBrokerV2(tmp / name, starting_balance=100_000, leverage=5, fee_rate=0.0004,
                         spread_bps=0, slippage_bps=0, participation_rate=1)
def bar(p): return {"open": p, "high": p, "low": p, "close": p, "volume": 1000}
def fills(b, sym, side, qty, entry, exits, stamps, strategy, tf):
    b.submit(symbol=sym, side=side, order_type="market", quantity=qty, strategy=strategy, timeframe=tf)
    b.process_candle(sym, bar(entry))
    for part, price in exits:
        b.submit(symbol=sym, side="sell" if side == "buy" else "buy", order_type="market", quantity=part, reduce_only=True)
        b.process_candle(sym, bar(price))
    fs = sorted(b.export_state()["fills"], key=lambda f: f["timestamp"])[-(1 + len(exits)):]
    for f, ts in zip(fs, stamps):
        f["timestamp"] = f["fill_timestamp"] = ts
    return fs
pa = broker("pa.db")
pa_f = fills(pa, "BTCUSDT", "buy", 0.4, 60500, [(0.2, 60900), (0.2, 61200)],
             ["2026-09-16T22:05:00+00:00", "2026-09-16T23:10:00+00:00", "2026-09-17T08:30:00+00:00"], "pa_rulebook_v0_1", "15m")
pa_hist = {"currency": "USDT", "sessions": [{"session_id": "pa-5d21e0a4", "symbol": "BTCUSDT", "timeframe": "15m", "fills": pa_f,
           "funding": [{"symbol": "BTCUSDT", "funding_time": "2026-09-17T00:00:00+00:00", "amount": 1.25, "applied": 1}],
           "order_meta": {}}]}
smc = broker("smc.db")
smc_f = fills(smc, "ETHUSDT", "sell", 2, 2480, [(2, 2502)],
              ["2026-09-08T14:00:00+00:00", "2026-09-08T16:45:00+00:00"], "smc_ladder", "5m")
smc_hist = {"currency": "USDT", "sessions": [{"session_id": "smc-91be77c2", "symbol": "ETHUSDT", "timeframe": "5m", "fills": smc_f,
            "funding": [], "order_meta": {}}]}
pa_rows, _ = cal.collect_v2_lab(pa_hist, source="pa_lab")
smc_rows, _ = cal.collect_v2_lab(smc_hist, source="smc_lab")
calendar = cal.PnlCalendar({
    "ledger": lambda: (rows, {"open_positions": 1, "skipped": {}}),
    "pa_lab": lambda: (pa_rows, {"open_positions": 0}),
    "smc_lab": lambda: (smc_rows, {"open_positions": 0}),
    "adaptive_lab": lambda: ([], {"open_positions": 0, "skipped": {}}),
})
tz = cal.resolve_zone("Europe/London")
conv = lambda cs: {"display_currency": "USDT", "needed": any(c != "USDT" for c in cs), "available": False,
                   "unconverted": [c for c in cs if c != "USDT"],
                   "note": "" if all(c == "USDT" for c in cs) else "No exchange-rate source is configured, so amounts in GBP are shown in their settlement currency and never added to USDT."}
month = calendar.month(year=2026, month=9, tz=tz)
month["conversion"] = conv(month["currencies"])
days = {}
for d in ("2026-09-02", "2026-09-08", "2026-09-10", "2026-09-16", "2026-09-17", "2026-09-20"):
    v = calendar.day(day=date.fromisoformat(d), tz=tz)
    v["conversion"] = conv(v["currencies"])
    days[d] = v
opts = {**calendar.options(), "timezone": "Europe/London", "display_currency": "USDT"}
by_source = {}
for key in cal.SOURCES:
    v = calendar.month(year=2026, month=9, tz=tz, filters=cal.Filters(source=key))
    v["conversion"] = conv(v["currencies"])
    by_source[key] = v
out = {"month": month, "month_by_source": by_source, "days": days, "options": opts}
Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "calendar.json").write_text(json.dumps(out, indent=1, default=str))
print("ok", month["currencies"], {k: v["net"] for k, v in month["summary"].items()}, len(pa_rows), len(smc_rows), len(rows))
