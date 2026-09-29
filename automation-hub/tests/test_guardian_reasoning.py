"""Guardian Phase 6: the reasoning layer answers from evidence, cites it, and
is checked.

The model is replaced by a stub that records the exact request and returns a
chosen reply -- no request leaves the test. Everything around it is real: a
real Guardian with a real incident, the real evidence pack and redaction, and
the real audit record.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import services.guardian_reasoning as reasoning
from services.guardian import evidence
from services.guardian.store import GuardianStore
from tests.test_guardian_incidents import _rows, _svc


class _Stub:
    """Stands in for ``anthropic.Anthropic()``; records every request."""

    def __init__(self, reply=None, *, stop_reason="end_turn", error=None):
        self.calls: list[dict] = []
        self.reply, self.stop_reason, self.error = reply, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        content = [SimpleNamespace(type="text", text=json.dumps(self.reply))] if self.reply else []
        return SimpleNamespace(stop_reason=self.stop_reason, content=content, model=kwargs["model"],
                               stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None)

    def __call__(self):
        return self


@pytest.fixture
def svc(monkeypatch):
    """A Guardian holding one real, open incident: a feed that went stale."""
    monkeypatch.setenv("HUB_LLM_API_KEY", "test-key-not-real-000000")
    store = GuardianStore()
    rows = {"rows": _rows("healthy", "healthy")}
    service, bus = _svc(store, rows, incident_verify_s=0)
    service.cycle()
    rows["rows"] = _rows("stale", "healthy")
    for _ in range(3):
        service.cycle()
        bus.flush()
    assert service.incidents.list(state="active")
    return service


def _incident_id(service) -> str:
    return f"incident:{service.incidents.list(state='active')[0]['id']}"


# ----------------------------------------------------- what the model sees
def test_the_pack_is_guardians_findings_with_ids_and_no_secrets(svc):
    from config import settings
    svc._publish("collector_failed", source_component="guardian.test", severity="WARNING",
                 reason=f"connect failed with key {settings.admin_key}")
    svc.bus.flush()
    pack = evidence.build(svc, research=svc.research)
    blob = json.dumps(pack)
    assert settings.admin_key not in blob
    assert _incident_id(svc) in evidence.ids(pack)
    assert {"platform", "incidents", "integrity", "strategies", "almost_trades", "hypotheses",
            "recent_warnings", "omitted", "boundary"} <= set(pack)
    assert evidence.digest(pack) == evidence.digest(json.loads(blob))    # stable hash


def test_it_is_off_without_a_key_and_nothing_is_sent(svc, monkeypatch):
    monkeypatch.delenv("HUB_LLM_API_KEY")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    stub = _Stub({"answer": "x", "confidence": "CONFIRMED", "citations": [], "limitations": []})
    out = reasoning.ask(svc, "why is the feed down?", client_factory=stub)
    assert out["available"] is False and stub.calls == []


def test_the_request_carries_the_pack_the_rules_and_a_json_schema(svc):
    from config import settings
    stub = _Stub({"answer": "The first feed is stale.", "confidence": "HIGH CONFIDENCE",
                  "citations": [_incident_id(svc)], "limitations": []})
    reasoning.ask(svc, f"why is it down? my key is {settings.admin_key}", client_factory=stub)
    [call] = stub.calls
    assert call["model"] == reasoning.MODEL
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": reasoning.SCHEMA}
    assert "correlation is not a proven improvement" in call["system"]
    assert "Never recommend changing a production strategy" in call["system"]
    sent = call["messages"][0]["content"]
    assert "EVIDENCE PACK" in sent and _incident_id(svc) in sent
    assert settings.admin_key not in sent                     # the owner's own words are scrubbed


# -------------------------------------------------------- what is believed
def test_citations_are_checked_against_the_pack(svc):
    real, fake = _incident_id(svc), "incident:999999"
    stub = _Stub({"answer": "Feed i0 is stale.", "confidence": "HIGH CONFIDENCE",
                  "citations": [real, fake], "limitations": ["one consumer"]})
    out = reasoning.ask(svc, "what is wrong?", client_factory=stub)
    assert out["outcome"] == "ANSWERED" and out["confidence"] == "HIGH CONFIDENCE"
    assert out["citations"] == [real] and out["unverified_citations"] == [fake]


def test_an_answer_citing_nothing_real_is_unknown_whatever_it_claims(svc):
    stub = _Stub({"answer": "Everything is proven.", "confidence": "CONFIRMED",
                  "citations": ["incident:424242"], "limitations": []})
    out = reasoning.ask(svc, "is it fixed?", client_factory=stub)
    assert out["confidence"] == "UNKNOWN" and out["claimed_confidence"] == "CONFIRMED"


def test_refusals_and_failures_are_recorded_never_raised(svc):
    refused = reasoning.ask(svc, "q1", client_factory=_Stub(stop_reason="refusal"))
    failed = reasoning.ask(svc, "q2", client_factory=_Stub(error=RuntimeError("network down")))
    assert refused["outcome"] == "REFUSED" and "cyber" in refused["reason"]
    assert failed["outcome"] == "FAILED" and failed["answer"] is None
    audit = [a for a in svc.store.actions(20) if a["action"] == "EVIDENCE_REASONING"]
    assert [a["result"] for a in audit] == ["FAILED", "REFUSED"]
    assert all(a["evidence"]["pack_sha256"] for a in audit)   # what the model was shown, provably


def test_an_answer_is_advice_the_platform_is_unchanged(svc):
    before = svc.store.components()
    stub = _Stub({"answer": "Restart everything and raise risk.", "confidence": "POSSIBLE",
                  "citations": [_incident_id(svc)], "limitations": []})
    reasoning.ask(svc, "what now?", client_factory=stub)
    assert svc.store.components() == before
    assert [a["action"] for a in svc.store.actions(5)] == ["EVIDENCE_REASONING"]


def test_the_ask_endpoint_needs_the_control_credential(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    monkeypatch.delenv("HUB_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    assert client.post("/guardian/reasoning/ask", json={"question": "hi"}).status_code == 401
    key = {"x-webhook-secret": webhook_api.settings.admin_key}
    body = client.post("/guardian/reasoning/ask", json={"question": "hi"}, headers=key).json()
    assert body["available"] is False and "Nothing was sent" in body["reason"]
    assert client.get("/guardian/reasoning").json()["available"] is False
