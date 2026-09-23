"""Delete on a Trading Instance: available, or explained.

The button used to render only for a deletable state. A running or paused
instance therefore had no Delete button at all, and nothing anywhere said
why -- which is indistinguishable from the feature being broken, and is
exactly how it was reported.

The refusal itself was never wrong, so none of these tests relax it. They
pin that the reason a user sees matches the reason the backend would give,
and that the two lists cannot drift apart.
"""
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2] / "automation-hub-dashboard" / "src"
PAGE = DASHBOARD / "pages" / "TradingInstances.tsx"

#: What TradingInstanceManager.delete accepts: anything whose worker is not
#: alive. Mirrored here so a change on either side fails a test rather than
#: silently hiding or offering the button.
DELETABLE = ["created", "stopped", "error", "degraded"]


def test_the_delete_button_is_always_rendered():
    """A missing control cannot be reasoned about. Whatever the state, the
    button is on the page -- disabled and explained when it cannot be used."""
    page = PAGE.read_text()
    clicks = [line for line in page.splitlines() if "void remove(instance)" in line]
    assert len(clicks) == 1, clicks

    # The element spans several lines, so read it rather than one line of it:
    # from the button tag that owns the click back to its opening bracket.
    start = page.rindex("<button", 0, page.index("void remove(instance)"))
    element = page[start:page.index("</button>", start)]

    assert "DELETABLE_STATES.includes" not in element, \
        "the button is conditional on state again"
    assert "disabled={rowBusy || Boolean(deleteBlocker)}" in element


def test_an_undeletable_state_carries_a_reason():
    page = PAGE.read_text()

    assert "const deleteBlocker" in page
    assert 'title={deleteBlocker ??' in page
    # And the reason is shown, not only offered as a tooltip: a hover hint is
    # not an explanation on a touch device.
    assert "{deleteBlocker ? <small" in page


def test_the_ui_and_the_backend_agree_on_what_is_deletable():
    """The one way this fix could rot: the page offering Delete for a state
    the manager refuses, putting the dead end back behind a live button."""
    page = PAGE.read_text()
    listed = page[page.index("const DELETABLE_STATES"):]
    listed = listed[:listed.index("]")]

    for state in DELETABLE:
        assert f'"{state}"' in listed, f"{state} is deletable but not offered"
    for refused in ("running", "ready", "paused", "starting"):
        assert f'"{refused}"' not in listed, f"{refused} is offered but will be refused"


def test_pause_is_explained_as_a_live_worker_not_a_stop():
    """The least obvious refusal. Pause reads like a stop, and the manager
    keeps the worker alive on purpose, so the message has to say so."""
    page = PAGE.read_text()

    assert "Pause only closes the entry gate" in page
    assert "worker stays alive" in page


def test_a_reboot_in_progress_is_named_as_its_own_blocker():
    """_assert_reboot_idle refuses a delete during a Full Bot Reboot. Saying
    'stop the instance' there would send someone to do something they have
    already done."""
    page = PAGE.read_text()

    assert "Full Bot Reboot is in progress" in page


@pytest.mark.parametrize("state", DELETABLE)
def test_every_deletable_state_is_a_state_the_backend_knows(state):
    """Guards against a typo making a state permanently undeletable."""
    manager = (Path(__file__).resolve().parents[1] / "services"
               / "trading_instances.py").read_text()

    assert f'"{state}"' in manager, f"{state} is not a state the manager uses"
