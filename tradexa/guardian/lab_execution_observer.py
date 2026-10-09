"""Independent, change-only PA/SMC paper observations and freshness-gated view."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .events import GuardianEvent
from .health import component_health
from .lab_execution_integrity import reconcile_lab_paper
from .lab_observer import _timestamp
from .store import GuardianStore

COMPONENTS = {"PRICE_ACTION": "pa_paper_execution", "SMC": "smc_paper_execution"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an observer credential to another path or host.
        return None


class GuardianLabExecutionObserver:
    def __init__(self, store, url, key, lab, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"}
                or parsed.port != 8000 or parsed.path != "/guardian/lab-execution"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Lab paper observer URL must be internal")
        if lab not in COMPONENTS or not isinstance(key, str) or len(key) < 24:
            raise ValueError("Lab observer requires a lab and independent long key")
        self.store, self.url, self.key, self.lab = store, url, key, lab
        self.component = COMPONENTS[lab]
        self.probe = "guardian_" + self.component + "_probe"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fetch = fetch or self._fetch

    def _fetch(self):
        request = Request(self.url + "?lab=" + self.lab,
                          headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if response.status != 200:
                raise ValueError("Lab execution source unavailable")
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Lab execution response exceeds bound")
        return json.loads(raw)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            # A failed poll immediately invalidates previously fresh risk.
            self.store.record_heartbeat(self.probe, "FAILED", observed_at=self.clock(),
                                        reason="LAB_EXECUTION_EVIDENCE_UNAVAILABLE")
            raise

    def _poll(self):
        page = self.fetch()
        if (not isinstance(page, dict) or page.get("schema_version") != 1 or
                page.get("scope") != "ISOLATED_LAB_PAPER_BROKER" or
                page.get("execution_integrity_verified") is not False):
            raise ValueError("Invalid lab paper evidence contract")
        observed, now = _timestamp(page.get("observed_at")), self.clock()
        if now.utcoffset() is None or not -5 <= (now - observed).total_seconds() <= 90:
            raise ValueError("Lab paper observation is stale")
        snapshot = page.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("lab") != self.lab:
            raise ValueError("Lab paper identity mismatch")
        evidence = reconcile_lab_paper(snapshot)
        material = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(material.encode()).hexdigest()
        event = GuardianEvent(
            source_service=self.probe, source_component=self.component,
            event_type="lab_paper_execution_observed", timestamp=observed,
            lab_id=self.lab, severity="WATCH" if evidence["findings"] else "INFO",
            reason="PAPER_EXECUTION_RECORDS_OBSERVED",
            evidence=evidence, metadata={"paper_only": True, "material_digest": digest})
        inserted = self.store.append_observed_snapshot(self.component, digest, event)
        self.store.record_heartbeat(self.probe, "HEALTHY", observed_at=observed)
        return int(inserted)


def lab_execution_view(store: GuardianStore, *, now: datetime | None = None) -> dict:
    labs = []
    probes = tuple("guardian_" + name + "_probe" for name in COMPONENTS.values())
    health = component_health(store.heartbeats(), probes, now=now)["components"]
    for lab, component in COMPONENTS.items():
        event = store.observed_snapshot(component)
        probe = health["guardian_" + component + "_probe"]
        current = bool(event) and probe["state"] == "HEALTHY"
        evidence = (event or {}).get("evidence", {})
        labs.append({**evidence, "lab": lab,
                     "observation_state": "CURRENT" if current else "UNKNOWN",
                     "observation_age_seconds": probe.get("age_seconds"),
                     "probe_state": probe["state"], "probe_reason": probe["reason"],
                     "event_id": (event or {}).get("event_id"),
                     "material_evidence_at": (event or {}).get("timestamp"),
                     "positions": [{**row, "entry_to_stop_amount": row["entry_to_stop_amount"]
                                    if current else None} for row in evidence.get("positions", [])]})
    return {"scope": "SEPARATE_PA_AND_SMC_PAPER_ACCOUNTS", "labs": labs,
            "cross_lab_atomic": False, "global_risk_amount": None,
            "currency_verified": False, "live_exposure_verified": False}
