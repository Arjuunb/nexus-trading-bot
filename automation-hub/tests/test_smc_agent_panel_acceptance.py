"""The agent panel: routed, in the sidebar, and computing nothing itself.

The agent had no UI at all -- deliberately, because the dashboard was parked
-- and the cost showed up twice: the operator went looking for it, found
nothing, and switched the session to Automatic paper to get autonomy, which is
the one mode where the agent stands down entirely. A page that cannot be
found is not a feature that exists.
"""
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2] / "automation-hub-dashboard" / "src"


def _sidebar_items() -> str:
    """Only the NAV_GROUPS block -- what the sidebar actually renders."""
    nav = (DASHBOARD / "app-context.ts").read_text()
    start = nav.index("export const NAV_GROUPS")
    return nav[start:nav.index("];", start)]


def test_the_agent_page_is_routed_and_in_the_sidebar():
    app = (DASHBOARD / "App.tsx").read_text()

    assert 'case "SMC Agent": return <SMCAgentPage' in app, "the page has no route"
    assert '"SMC Agent"' in _sidebar_items(), "the page is unreachable from the sidebar"


def test_it_sits_beside_the_lab_it_reports_on():
    """Research, next to the SMC Strategy Lab. An agent panel filed away from
    the lab whose decisions it records is a panel nobody opens."""
    research = _sidebar_items()
    block = research[research.index('"Research"'):]
    block = block[:block.index("]")]

    assert block.index('"SMC Agent"') > block.index('"SMC Strategy Lab"')


def test_the_panel_can_configure_rules_but_never_touch_an_order():
    """It reports on a system that places orders, and it may change the rules
    that system trades by -- that is the point of the rule panel. What it
    must never do is reach the order path itself: no approving, placing,
    cancelling or resetting. The one write it makes is the policy save, which
    is the single protected call on the agent surface.
    """
    page = (DASHBOARD / "pages" / "SMCAgent.tsx").read_text()

    assert "/research/smc/agent" in page
    writes = [line for line in page.splitlines() if "apiPostJson" in line
              and "import" not in line]
    assert len(writes) == 1, f"more than one write from the panel: {writes}"
    assert "/research/smc/agent/policy" in writes[0]

    for forbidden in ("approve_candidate", "approve-candidate", "/paper/reset",
                      "cancel_order", "/sessions/current/end", "submit_order"):
        assert forbidden not in page, f"the panel reached the order path: {forbidden}"


def test_the_panel_computes_no_trading_decision_of_its_own():
    """Same rule as every other surface: the page displays what the agent
    decided. It does not re-derive reward-to-risk, re-check a gate, or form a
    view about a setup -- a panel that disagreed with the journal would make
    the journal untrustworthy rather than the panel wrong."""
    page = (DASHBOARD / "pages" / "SMCAgent.tsx").read_text()

    for forbidden in ("ENTRY_READY ?", "computeRR", "reward / risk",
                      "entry - stop", "Math.abs(entry"):
        assert forbidden not in page, f"the panel computed a decision: {forbidden}"


def test_every_journal_outcome_is_named_on_the_page():
    """The four are not interchangeable, and collapsing them into
    traded/not-traded is what made the agent invisible in the first place."""
    from services.smc_agent_journal import DECISION_OUTCOMES

    page = (DASHBOARD / "pages" / "SMCAgent.tsx").read_text()

    for outcome in DECISION_OUTCOMES:
        assert outcome in page, f"{outcome} is not shown anywhere"


def test_the_page_explains_the_stood_down_case():
    """The exact confusion that prompted this page: an attached agent judging
    nothing because the session is in Automatic paper. If the panel cannot
    say that, someone will go looking for the agent again."""
    page = (DASHBOARD / "pages" / "SMCAgent.tsx").read_text()

    assert "STOOD DOWN" in page
    assert "Automatic paper" in page
