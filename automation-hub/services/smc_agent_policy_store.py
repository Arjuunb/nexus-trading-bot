"""Where the agent's three optional rule-sets are saved.

Trade management, context vetoes and journal memory are all off by default
and all change how much and how often the agent trades. They are deliberate,
infrequent decisions -- made after a backtest, not toggled on a whim -- so
they persist in one small JSON file beside the journal rather than in the
session row, and a restart keeps them.

Reading is total: a missing file, a corrupt file or a key that no longer
exists all resolve to the safe default, which is off. A policy store that
could fail open would be a way to turn a trading behaviour on by breaking a
file, so every failure direction here lands on the defaults.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from services.smc_agent_context import ContextPolicy
from services.smc_agent_memory import MemoryPolicy
from services.smc_agent_trade_manager import TradeManagementPolicy

SECTIONS = {"trade_management": TradeManagementPolicy,
            "context": ContextPolicy,
            "memory": MemoryPolicy}


def _coerce(kind, payload: dict):
    """Build one policy from stored values, ignoring keys it does not know.

    A field removed in a later version must not stop the whole file loading,
    and an unknown field must not be silently accepted as meaningful.
    """
    allowed = {field for field in kind().__dataclass_fields__}
    kwargs = {key: value for key, value in (payload or {}).items() if key in allowed}
    if "allowed_hours_utc" in kwargs and kwargs["allowed_hours_utc"] is not None:
        kwargs["allowed_hours_utc"] = tuple(
            (int(start), int(end)) for start, end in kwargs["allowed_hours_utc"])
    return kind(**kwargs).validated()


class AgentPolicyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict:
        """The three policies. Defaults -- all off -- on any failure."""
        try:
            raw = json.loads(self.path.read_text())
        except Exception:
            raw = {}
        out = {}
        for name, kind in SECTIONS.items():
            try:
                out[name] = _coerce(kind, raw.get(name) or {})
            except Exception:
                # One unreadable section does not drag the others down, and
                # the one that failed falls back to off.
                out[name] = kind()
        return out

    def save(self, policies: dict) -> dict:
        """Write all three atomically, after validating every one.

        Validation happens before anything is written, so a rejected value
        cannot leave half the file updated -- and the temp-file rename means
        a crash mid-write cannot leave a truncated file that would read back
        as defaults without anyone noticing.
        """
        validated = {name: _coerce(SECTIONS[name], policies.get(name) or {})
                     for name in SECTIONS}
        payload = {name: asdict(policy) for name, policy in validated.items()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=str(self.path.parent),
                                                 suffix=".tmp")
            try:
                with os.fdopen(handle, "w") as stream:
                    json.dump(payload, stream, indent=2, sort_keys=True,
                              default=list)
                os.replace(temporary, self.path)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        return validated
