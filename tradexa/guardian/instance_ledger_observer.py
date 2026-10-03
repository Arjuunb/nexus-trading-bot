"""Change-only observation of isolated Trading Instance paper-ledger pairs."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent
from .lab_observer import _timestamp
from .store import GuardianStore

_COMPONENT = "instance_ledger"
_MAX_RESPONSE_BYTES = 1024 * 1024


class GuardianInstanceLedgerObserver:
    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 10.0,
                 fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or parsed.port != 8000 or
                parsed.path != "/guardian/instance-ledger" or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Guardian instance ledger URL must be internal")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("Guardian instance ledger requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self) -> dict:
        request = Request(self.url, headers={"X-Guardian-Observer-Key": self.key})
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("instance ledger returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("instance ledger response exceeds size limit")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("instance ledger response must be an object")
        return data

    def poll(self) -> int:
        view = self.fetch()
        if (not isinstance(view, dict) or view.get("schema_version") != 1 or
                view.get("scope") != "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY" or
                view.get("feed_health_verified") is not False or
                view.get("execution_integrity_verified") is not False):
            raise ValueError("instance paper-ledger contract is invalid")
        observed = _timestamp(view.get("observed_at"))
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None or \
                not -5 <= (now.astimezone(timezone.utc) - observed).total_seconds() <= 90:
            raise ValueError("instance paper-ledger observation is stale")
        snapshot = view.get("snapshot")
        if (not isinstance(snapshot, dict) or
                snapshot.get("scope") != "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY" or
                type(snapshot.get("atomic_snapshot")) is not bool or
                type(snapshot.get("source_coverage_verified")) is not bool or
                snapshot.get("broker_fill_verified") is not False or
                snapshot.get("live_exposure_verified") is not False or
                not isinstance(snapshot.get("instances"), list) or
                not isinstance(snapshot.get("findings"), list) or
                len(snapshot["instances"]) > 128 or len(snapshot["findings"]) > 128):
            raise ValueError("instance paper-ledger snapshot is invalid")
        if any(not isinstance(row, dict) for row in
               [*snapshot["instances"], *snapshot["findings"]]):
            raise ValueError("instance paper-ledger row is invalid")
        if snapshot["atomic_snapshot"] is False and snapshot["source_coverage_verified"] is True:
            raise ValueError("non-atomic source cannot claim complete coverage")
        owners = set()
        for row in snapshot["instances"]:
            owner = row.get("instance_id")
            if (not isinstance(owner, str) or not owner or owner in owners or
                    any(type(row.get(name)) is not int or
                        not 0 <= row[name] <= 64
                        for name in ("open_positions", "open_trades")) or
                    type(row.get("risk_complete")) is not bool):
                raise ValueError("instance paper-ledger summary is invalid")
            owners.add(owner)
            risk = row.get("risk_amount")
            if row["risk_complete"]:
                if (not snapshot["atomic_snapshot"] or
                        not snapshot["source_coverage_verified"] or
                        type(risk) not in (int, float) or
                        not math.isfinite(risk) or risk < 0):
                    raise ValueError("instance paper-ledger risk claim is unverified")
            elif risk is not None:
                raise ValueError("incomplete instance risk must be unknown")
        for row in snapshot["findings"]:
            codes = row.get("codes")
            state = row.get("pairing_state")
            if (row.get("instance_id") not in owners or
                    not isinstance(codes, list) or
                    any(not isinstance(code, str) or not code or len(code) > 80
                        for code in codes) or
                    state not in {"OBSERVED_MATCH", "UNVERIFIED"} or
                    (state == "OBSERVED_MATCH" and
                     (codes or not snapshot["atomic_snapshot"]))):
                raise ValueError("instance paper-ledger finding is invalid")
        material = json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                              allow_nan=False)
        digest = hashlib.sha256(material.encode()).hexdigest()
        abnormalities = [row for row in snapshot["findings"]
                         if row["pairing_state"] == "UNVERIFIED"]
        event = GuardianEvent(
            source_service="guardian_instance_ledger_probe",
            source_component=_COMPONENT,
            event_type="instance_paper_ledger_observed",
            timestamp=observed,
            severity="WATCH" if abnormalities else "INFO",
            reason=("PAPER_LEDGER_PAIRING_UNVERIFIED" if abnormalities else
                    "PAPER_LEDGER_SNAPSHOT_OBSERVED"),
            evidence={"material_digest": digest, **snapshot},
            metadata={"paper_only": True, "scope": snapshot["scope"]},
        )
        inserted = self.store.append_observed_snapshot(_COMPONENT, digest, event)
        self.store.record_heartbeat("guardian_instance_ledger_probe", "HEALTHY",
                                    observed_at=observed)
        return int(inserted)
