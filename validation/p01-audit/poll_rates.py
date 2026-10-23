# Per-open-tab request rates from the static poller inventory (deployed lineage).
# useLive pollers dedupe by path at the minimum interval; raw setInterval pollers do not dedupe.
shell = {"/instances": [2500, 4000, 4000], "/research/price-action/bot-status": [4000],
         "/research/smc/bot-status": [4000], "/paper/account": [8000], "/risk/summary": [8000],
         "/system/status": [8000], "/strategy/performance": [12000], "/ledger/alerts?limit=25": [12000],
         "/instances/options": [30000]}
dashboard = {"/instances": [5000, 2000, 2000, 2000], "/research/price-action/bot-status": [4000],
             "/research/smc/bot-status": [4000], "/paper/equity-curve": [3000], "/strategy/performance": [4000, 4000],
             "/bots/live": [2500], "/ledger/logs?limit=12": [2500], "/risk/portfolio": [5000], "/risk/recovery": [6000],
             "/risk/summary": [2500], "/ledger/alerts?limit=8": [4000], "/system/status": [4000], "/markets/watchlist": [20000]}
pa_raw = {"/research/price-action/session": 5000, "/research/price-action/paper": 5000,
          "/research/price-action/live-chart(800 bars)": 3000, "/research/price-action/journal": 8000,
          "/research/price-action/learning/analysis": 8000, "/research/price-action/learning/candidates": 8000}
pa_live = {"/research/event-guard/price_action": [30000], "/research/observatory/status": [5000],
           "/research/observatory/comparison": [10000]}
ivl_raw = {"/research/instance-visual/state": 4000, "/research/instance-visual/timeline": 4000,
           "/research/instance-visual/features": 15000}
smc_live = {"/research/smc/session": [5000], "/research/smc/paper": [5000], "/research/smc/journal": [5000],
            "/research/smc/sessions": [15000], "/research/smc/chart(live)": [2500],
            "/research/event-guard/smc": [30000], "/research/observatory/status": [5000],
            "/research/observatory/comparison": [10000]}

def merge(*maps):
    out = {}
    for m in maps:
        for k, v in m.items():
            out.setdefault(k, []).extend(v)
    return {k: 60000 / min(v) for k, v in out.items()}

def show(name, rates, raw=None):
    total = sum(rates.values()) + sum(60000 / v for v in (raw or {}).values())
    lab = sum(r for k, r in rates.items() if "bot-status" in k)
    print(f"{name:42s} {total:6.1f} req/min  ({total/60:4.2f}/s)  lab bot-status share: {lab:4.1f}/min")

show("Shell only (any page)", merge(shell))
show("Dashboard", merge(shell, dashboard))
show("Price Action Lab (live mode)", merge(shell, pa_live), pa_raw)
show("SMC Strategy Lab (live)", merge(shell, smc_live))
show("Instance Visual Lab (+candles, not counted)", merge(shell), ivl_raw)

# Scenario for plan step P2: footer ticker polls lab status only on lab pages, and
# every /instances poller is raised to a 5 s minimum. (PA Lab still present.)
def p2(m):
    out = {}
    for k, v in m.items():
        if k.startswith("/research/") and "bot-status" in k:
            continue
        out[k] = [max(5000, i) for i in v] if k == "/instances" else v
    return out

print("\nP2 scenario (ticker lab polls only on lab pages, /instances >= 5 s):")
show("Shell only (any page)", merge(p2(shell)))
dash = {k: v for k, v in dashboard.items()}
show("Dashboard (its own lab cards still poll)", merge(p2(shell), {k: ([max(5000, i) for i in v] if k == "/instances" else v) for k, v in dash.items()}))
