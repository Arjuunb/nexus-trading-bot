"""Automation Hub overview page — the main dashboard.

Layout (matches the spec):
  KPI bar: Running Bots | Paper Bots | Today's P&L | Alerts
  Live market chart
  Active Bots (name + state)
  Risk Center (daily loss / consecutive losses / exposure)
  Recent Activity
"""
from __future__ import annotations

from config import settings
from dashboard import widgets as w
from dashboard.alerts import recent_activity
from data.market_data import get_bars


def _consecutive_losses(trades: list) -> int:
    streak = 0
    for t in reversed(trades):
        if t.get("pnl", 0) < 0:
            streak += 1
        else:
            break
    return streak


def render_overview(manager, user: str = "") -> str:
    bots = manager.list()
    s = manager.summary()
    cur = settings.currency

    # ---- KPI bar ----
    pnl_cls = "pos" if s["pnl_today"] >= 0 else "neg"
    kpis = (
        '<div class="kpis">'
        + w.kpi("Running Bots", str(s["running"]))
        + w.kpi("Paper Bots", str(s["paper"]))
        + w.kpi("Today's P&L", f"{cur}{s['pnl_today']:,.2f}", pnl_cls)
        + w.kpi("Alerts", str(s["alerts"]), "neg" if s["alerts"] else "dim")
        + "</div>"
    )

    # ---- Live market chart (default symbol) ----
    bars, source = get_bars(settings.default_symbol, n=500)
    chart = (f'<div class="card"><h2>Live Market — {w.esc(settings.default_symbol)} '
             f'<span class="dim">({w.esc(source)})</span></h2>'
             f'<div class="chart">{w.candles(bars)}</div></div>')

    # ---- Active bots ----
    if bots:
        rows = "".join(
            f"<tr><td><b>{w.esc(b.config.name)}</b><div class='dim'>"
            f"{w.esc(b.config.strategy.upper())} · {w.esc(b.config.symbol)} · "
            f"{w.esc(b.config.timeframe)}</div>"
            + (f"<div class='neg' style='font-size:11px'>⛔ {w.esc(b.runtime.halt_reason)}</div>"
               if b.runtime.halt_reason else "")
            + "</td>"
            f"<td>{w.state_badge(b.runtime.state.value)}</td>"
            f"<td class='{'pos' if b.runtime.pnl_today >= 0 else 'neg'}'>"
            f"{cur}{b.runtime.pnl_today:,.2f}</td>"
            f"<td>{_row_controls(b)}</td></tr>"
            for b in bots
        )
        active = (f'<div class="card"><h2>Active Bots</h2><table><thead><tr>'
                  f'<th>Bot</th><th>State</th><th>P&L today</th><th></th></tr></thead>'
                  f'<tbody>{rows}</tbody></table></div>')
    else:
        active = ('<div class="card"><h2>Active Bots</h2>'
                  '<div class="empty">No bots yet — '
                  '<a class="pos" href="/bots/new">create one</a>.</div></div>')

    # ---- Risk Center ----
    daily_loss = -sum(min(0.0, b.runtime.pnl_today) for b in bots)
    limit = settings.max_daily_loss_pct * settings.starting_cash
    consec = max((_consecutive_losses(b.runtime.trades) for b in bots), default=0)
    used_pct = min(daily_loss / limit, 1.0) if limit else 0.0
    risk = (
        '<div class="card"><h2>Risk Center</h2>'
        f'<div>Daily Loss: <b>{cur}{daily_loss:,.0f}</b> / {cur}{limit:,.0f}'
        f'<div class="bar"><span style="width:{used_pct*100:.0f}%"></span></div></div>'
        f'<div style="margin-top:10px">Consecutive Losses: '
        f'<b class="{"neg" if consec >= 3 else ""}">{consec}</b></div>'
        f'<div style="margin-top:6px">Exposure: <b>{_exposure(bots):.0f}%</b></div>'
        '</div>'
    )

    # ---- Recent activity ----
    acts = recent_activity(bots)
    if acts:
        items = "".join(
            f"<li><b>{w.esc(a['label'])}</b> — {w.esc(a['bot'])} "
            f"<span class='dim'>{w.esc(a['detail'])}</span></li>"
            for a in acts
        )
        activity = f'<div class="card"><h2>Recent Activity</h2><ul class="activity">{items}</ul></div>'
    else:
        activity = ('<div class="card"><h2>Recent Activity</h2>'
                    '<div class="empty">No activity yet. Start a bot to populate the feed.</div></div>')

    from dashboard.stream import LIVE_FEED_CARD
    from services import csp
    # The live feed's script is ours; it carries this response's CSP nonce.
    LIVE_FEED_CARD = LIVE_FEED_CARD.replace("<script>", f'<script nonce="{csp.nonce()}">', 1)

    estop = ('<form class="inline" method="post" action="/emergency-stop">'
             '<button class="btn btn-danger" type="submit">■ Emergency Stop</button></form>')
    body = (w.topbar(settings.app_name, estop) + kpis + chart + active
            + LIVE_FEED_CARD + risk + activity)
    return w.page(title="Overview", active="overview", body=body,
                  app_name=settings.app_name, user=user)


def _exposure(bots) -> float:
    from database.models import BotState
    active = [b for b in bots if b.runtime.state in (BotState.RUNNING, BotState.PAPER)]
    if not bots:
        return 0.0
    # Phase 1 proxy: share of bots actively in-market.
    return 100.0 * len(active) / max(len(bots), 1)


def _row_controls(bot) -> str:
    from database.models import BotState
    st = bot.runtime.state
    bid = bot.id
    btns = []
    if st in (BotState.CREATED, BotState.STOPPED, BotState.PAUSED):
        btns.append(_form(f"/bots/{bid}/start", "Paper", "btn"))
        btns.append(_form(f"/bots/{bid}/go-live", "Go Live", "btn btn-warn"))
    if st in (BotState.RUNNING, BotState.PAPER):
        btns.append(_form(f"/bots/{bid}/pause", "Pause", "btn btn-warn"))
    btns.append(_form(f"/bots/{bid}/stop", "Stop", "btn btn-ghost"))
    return f'<div class="rowbtns">{"".join(btns)}</div>'


def _form(action: str, label: str, cls: str) -> str:
    return (f'<form class="inline" method="post" action="{action}">'
            f'<button class="{cls}" type="submit">{w.esc(label)}</button></form>')
