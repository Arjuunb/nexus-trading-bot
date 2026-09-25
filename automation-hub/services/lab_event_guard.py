"""News blackout for the Price Action, SMC and Adaptive research labs.

The same opt-in gate Trading Instances have (services/instance_event_guard.py):
off by default, one switch per lab; when on, no new strategy entry from 30
minutes before a high-impact release until 15 minutes after it. Open
positions, their stops and targets, and manual orders are untouched.

Nothing here reaches a strategy. The Price Action and SMC lab modules are
frozen (data/pr6_real_paper_freeze.json, data/smc_decision_path_freeze.json),
so the gate sits in subclasses of their paper accounts, at the one call each
lab already routes every new strategy entry through:

* Price Action -- ``_runtime_control()``, the entry-pause control that
  ``_place_proposal`` consults before anything else. During a blackout it
  reports the pause, so the lab rejects the proposal with the blackout as
  the recorded reason, exactly as it rejects one for a pending readiness
  recheck. Nothing is written to the lab's control table.
* SMC -- ``submit_order`` for strategy-owned orders. It raises the
  ``ValueError`` the lab already turns into an audited
  "automatic paper placement rejected" (and the agent path into "No order
  was placed").
* Adaptive -- its bot is a Trading Instance under its own manager, so it
  takes the instance gate itself, pointed at the lab's one switch.

Labs have no half-size step: SMC orders can carry the agent's own committed
size, and halving it could break the venue's quantity step. Outside the
blackout window a lab trades as it always has.
"""
from __future__ import annotations

from services.price_action_lab import PriceActionPaperAccount
from services.smc_agent_runtime import AgentGatedSMCPaperAccount

LABS = {"price_action": "Price Action Lab", "smc": "SMC Lab", "adaptive": "Adaptive Lab"}


def lab_key(lab: str) -> str:
    if lab not in LABS:
        raise KeyError(lab)
    return f"lab:{lab}"


def _block(guard, lab: str) -> str | None:
    if guard is None:
        return None
    try:
        return guard.entry_block(lab_key(lab))
    except Exception:  # noqa: BLE001 -- an unreadable calendar must not stop a lab
        return None


class EventGuardedPriceActionPaperAccount(PriceActionPaperAccount):
    #: services.instance_event_guard.InstanceEventGuard, attached by the server.
    event_guard = None

    def _runtime_control(self) -> dict:
        control = super()._runtime_control()
        if control.get("readiness_recheck_required"):
            return control                      # already paused for its own reason
        reason = _block(self.event_guard, "price_action")
        if reason:
            return {**control, "readiness_recheck_required": True,
                    "entry_pause_reason": reason, "news_blackout": True}
        return control


class EventGuardedSMCPaperAccount(AgentGatedSMCPaperAccount):
    #: services.instance_event_guard.InstanceEventGuard, attached by the server.
    event_guard = None

    def submit_order(self, **kwargs):
        if kwargs.get("ownership") == "strategy":
            reason = _block(self.event_guard, "smc")
            if reason:
                raise ValueError(reason)
        return super().submit_order(**kwargs)


class LabKeyedEventGuard:
    """Lets every bot of one manager answer to one lab switch.

    The Adaptive lab's bot is a Trading Instance whose id changes when the
    lab is reconfigured; the owner's choice belongs to the lab, not to one
    bot, so it outlives each of them."""

    def __init__(self, guard, lab: str):
        self.guard = guard
        self.key = lab_key(lab)

    @property
    def after_min(self) -> int:
        return self.guard.after_min

    def events_for(self, _instance_id: str):
        return self.guard.events_for(self.key)

    def enabled(self, _instance_id: str) -> bool:
        return self.guard.enabled(self.key)

    def state(self, _instance_id: str) -> dict:
        return self.guard.state(self.key)

    def forget(self, _instance_id: str) -> None:
        """Deleting one bot does not clear the lab's switch."""
