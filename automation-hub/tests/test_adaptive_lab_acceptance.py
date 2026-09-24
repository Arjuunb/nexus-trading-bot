"""The Adaptive MTF lab page: routed, beside the SMC lab, and no second chart.

The Instance Visual Lab rule is that every instance-path strategy shares one
chart implementation, so the same drawing never exists five times and
disagrees with itself (tests/test_instance_visual_lab_acceptance.py). This
lab is a separate paper bot with its own account -- a clone of the SMC lab --
and it keeps that rule by drawing with the SMC lab's own live chart component
(closed candles, the forming candle, bid/ask/mark) instead of a copy.
"""
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2] / "automation-hub-dashboard" / "src"
PAGE = DASHBOARD / "pages" / "AdaptiveLab.tsx"


def _sidebar_items() -> str:
    nav = (DASHBOARD / "app-context.ts").read_text()
    start = nav.index("export const NAV_GROUPS")
    return nav[start:nav.index("];", start)]


def test_the_lab_is_routed_and_sits_beside_the_smc_lab():
    app = (DASHBOARD / "App.tsx").read_text()
    assert 'case "Adaptive MTF Lab": return <AdaptiveLabPage' in app
    research = _sidebar_items()
    block = research[research.index('"Research"'):]
    block = block[:block.index("]")]
    assert block.index('"Adaptive MTF Lab"') > block.index('"SMC Strategy Lab"')


def test_it_draws_with_the_shared_chart_and_owns_no_chart_of_its_own():
    page = PAGE.read_text()
    assert 'from "../components/chart/NativeSMCChartOverlay"' in page
    assert "<NativeSMCChartOverlay " in page
    for forbidden in ("<svg", "function Chart", "viewBox"):
        assert forbidden not in page, f"the lab grew its own chart: {forbidden}"


def test_the_chart_is_fed_by_the_bots_own_live_feed_and_journal():
    page = PAGE.read_text()
    # The bot's hub snapshot (closed candles + forming candle + quote), not a
    # second market-data source, and the append-only per-candle journal.
    assert '"/research/adaptive-lab/live-chart' in page
    assert '"/research/adaptive-lab/journal' in page
    for forbidden in ("fapi.binance", "wss://", "/research/smc/"):
        assert forbidden not in page, f"the lab reads market data around its bot: {forbidden}"
    # SMC objects are drawn only when a strategy publishes them; this one does not.
    assert "NO_SMC_LAYERS" in page and "pivots: [], events: []" in page


def test_its_one_write_is_the_labs_configuration():
    page = PAGE.read_text()
    writes = [line.strip() for line in page.splitlines()
              if "apiPost" in line and "import" not in line]
    assert len(writes) == 1 and "/research/adaptive-lab/configuration" in writes[0]
    for forbidden in ("/orders", "/approve", "/close", "/reset", "/instances/"):
        assert forbidden not in page, f"the lab page reached another write path: {forbidden}"
