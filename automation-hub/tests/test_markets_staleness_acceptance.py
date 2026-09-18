"""The Markets scanner must show the age of the candles it ranked.

The scanner reads a local candle cache that nothing refreshes on a schedule.
It was measured 15.8h behind the venue for BTCUSDT and holding nothing at all
for BNBUSDT, while the page presented the result as "live setups" with a last
price. These pin the parts of that fix a later edit is most likely to undo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2] / "automation-hub-dashboard" / "src"
MARKETS = DASHBOARD / "pages" / "Markets.tsx"


@pytest.fixture(scope="module")
def page() -> str:
    return MARKETS.read_text()


@pytest.fixture(scope="module")
def code(page) -> str:
    """The page with comments stripped.

    Scanning raw source for a banned phrase matches the comment that explains
    why the phrase is banned. That has cost this file two false failures
    already; every content assertion below reads this instead.
    """
    without_block = re.sub(r"/\*.*?\*/", "", page, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.M)


def test_the_stale_note_renders_on_the_empty_result_too(code):
    """The failure worth guarding: a scan over day-old candles and a scan that
    genuinely found nothing render as the same empty box. The note has to
    appear in BOTH branches, not only where there are cards to decorate."""
    assert code.count("<StaleNote data={data} />") >= 2, (
        "StaleNote must render for the empty result as well as the populated "
        "grid, or 'no setups' hides 'no recent data'")


def test_the_page_does_not_assert_a_currency_it_has_not_checked(code):
    """'live setups' / 'firing right now' are claims about the present tense,
    and the page has no basis for either until the server says FRESH."""
    scanner = code[code.index("function OpportunityScanner"):]
    for claim in ("live setups", "firing right now"):
        assert claim not in scanner, f"page still claims {claim!r}"


def test_the_page_never_decides_freshness_for_itself(code):
    """Freshness has exactly one authority and it is server-side. A threshold
    compiled into the page is a second opinion that will drift from the first,
    and it is how a UI ends up showing FRESH over stale candles.

    Reading the server's verdict into a local name is not that, so this looks
    at where the value CAME FROM rather than what it is called. Rendering an
    age with Date.now() is not that either -- displaying how old something is
    is the entire point; deciding stale/fresh from a local comparison is the
    banned thing.
    """
    decisions = re.findall(r"(?:const|let|var)\s+\w*(?:[sS]tale|[fF]resh)\w*\s*=\s*([^;\n]+)",
                           code)
    assert decisions, "expected the page to bind the server's staleness verdict"
    for rhs in decisions:
        assert not re.search(r"[<>]=?|Date\.now", rhs), (
            f"page decides staleness from a local comparison: {rhs.strip()!r}")
        assert "stale" in rhs or "fresh" in rhs, (
            f"staleness must come from the server payload, got {rhs.strip()!r}")
    assert "data.stale_symbols" in code and "o.stale" in code


def test_the_note_tells_the_operator_what_to_actually_do(code):
    """A warning with no remedy trains people to ignore warnings. The cache is
    refreshed by hand, so the page has to say so."""
    assert "/data/sync" in code
    assert "nothing does it on a schedule" in code
