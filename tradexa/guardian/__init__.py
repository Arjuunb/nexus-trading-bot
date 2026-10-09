"""Independent, read-only Guardian evidence primitives.

Importing this package never imports or starts a trading runtime.
"""

from .events import GuardianEvent, GuardianEventError
from .health import component_health
from .store import GuardianStore

__all__ = ["GuardianEvent", "GuardianEventError", "GuardianStore", "component_health"]
