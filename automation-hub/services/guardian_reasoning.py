"""Guardian's reasoning layer (PRD Phase 6, §20, §27, §43).

The owner asks a question; Claude answers from Guardian's evidence pack and
nothing else (``services/guardian/evidence.py``). The answer must cite the
pack's ids and carry one of Guardian's confidence levels. Every citation is
checked against the pack: an answer citing nothing that exists is marked
UNKNOWN, whatever the model claimed.

Boundaries:

* The model sees the pack only -- no database, no ledger, no credentials.
  The pack is stripped of secrets before it is built.
* The answer is advice. Nothing here can change a strategy, a risk limit, an
  order or the paper/live mode; the reply is stored, not executed.
* Every query is recorded in Guardian's append-only action audit (§39) with
  the pack's hash, so what the model was shown can be verified later. The
  conversation is never Guardian's database (§27).
* Disabled until an API key is configured (``HUB_LLM_API_KEY`` or
  ``ANTHROPIC_API_KEY``, as the Strategy Studio uses). With no key nothing is
  sent anywhere.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from services.guardian import evidence
from services.redaction import scrub_text

_KEY_ENVS = ("HUB_LLM_API_KEY", "ANTHROPIC_API_KEY")
MODEL = os.environ.get("HUB_GUARDIAN_LLM_MODEL", "claude-opus-5-5")
CONFIDENCE = ["CONFIRMED", "HIGH CONFIDENCE", "PROBABLE", "POSSIBLE", "UNKNOWN"]

SYSTEM = """You are the reasoning layer of Guardian, the read-only observer of a paper-trading platform.
You answer the owner's question using ONLY the evidence pack in the user message.

Rules:
- Every claim must rest on items in the pack. Cite the exact "id" of each item you rely on in "citations".
- Choose "confidence" by the evidence: CONFIRMED only when a component reported the fact about itself;
  HIGH CONFIDENCE when independent items agree; PROBABLE or POSSIBLE when one item fits several causes;
  UNKNOWN when the pack does not contain enough to answer.
- An observed correlation is not a proven improvement. Never present a hypothesis or an almost-trade as
  evidence that a rule is wrong.
- Never recommend changing a production strategy, a risk limit, leverage, stop-loss/take-profit rules,
  RR requirements, positions, credentials, or switching paper to live. You may recommend investigation,
  research, backtests, or owner review.
- If the pack says items were omitted, say what you could not see when it matters.
- List in "limitations" anything that limits the answer."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "string", "enum": CONFIDENCE},
        "citations": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "confidence", "citations", "limitations"],
    "additionalProperties": False,
}


def api_key() -> Optional[str]:
    for env in _KEY_ENVS:
        value = os.environ.get(env, "").strip()
        if value:
            return value
    return None


def available() -> bool:
    return api_key() is not None


def status(service) -> dict:
    """What /guardian/reasoning shows: on or off, and the questions asked."""
    return {"available": available(), "model": MODEL,
            "history": [a for a in service.store.actions(200) if a["action"] == "EVIDENCE_REASONING"][:30]}


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=api_key(), timeout=120.0)


def ask(service, question: str, *, research=None, client_factory: Callable[[], Any] = _client) -> dict:
    """Answer one question from the evidence pack. Never raises for an API
    failure: the failure is the recorded result."""
    # The owner's own words are scrubbed too: a pasted key never leaves.
    question = scrub_text(str(question or "").strip()[:2000])
    if not question:
        raise ValueError("a question is required")
    if not available():
        return {"available": False, "answer": None,
                "reason": "Guardian's reasoning layer is off: set HUB_LLM_API_KEY (or ANTHROPIC_API_KEY). "
                          "Nothing was sent."}
    pack = evidence.build(service, research=research)
    known = evidence.ids(pack)
    digest = evidence.digest(pack)
    result: dict[str, Any] = {"available": True, "model": MODEL, "pack_sha256": digest,
                              "question": question}
    try:
        response = client_factory().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": (
                "EVIDENCE PACK (JSON):\n" + json.dumps(pack, sort_keys=True, default=str)
                + "\n\nQUESTION:\n" + question)}],
        )
        result["served_by"] = getattr(response, "model", MODEL)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            result.update(outcome="REFUSED", answer=None,
                          reason=f"the model declined ({getattr(details, 'category', None) or 'no category'})")
        elif response.stop_reason == "max_tokens":
            result.update(outcome="INCOMPLETE", answer=None, reason="the answer hit its length limit")
        else:
            text = next(b.text for b in response.content if b.type == "text")
            body = json.loads(text)
            cited = [c for c in body.get("citations", []) if isinstance(c, str)]
            verified = [c for c in cited if c in known]
            unknown = [c for c in cited if c not in known]
            confidence = body.get("confidence") if body.get("confidence") in CONFIDENCE else "UNKNOWN"
            if not verified:
                confidence = "UNKNOWN"          # nothing it cited exists: not evidence-backed
            result.update(outcome="ANSWERED", answer=body.get("answer"), confidence=confidence,
                          claimed_confidence=body.get("confidence"), citations=verified,
                          unverified_citations=unknown, limitations=body.get("limitations") or [])
    except Exception as exc:  # noqa: BLE001 -- the failure is the result, never a crash
        from services.strategy_agent import describe_llm_error
        result.update(outcome="FAILED", answer=None, reason=describe_llm_error(exc))
    service.store.record_action(
        "EVIDENCE_REASONING", reason=question[:300], policy="OWNER_REQUEST",
        result=result["outcome"],
        evidence={k: result.get(k) for k in ("model", "served_by", "pack_sha256", "confidence",
                                             "citations", "unverified_citations", "reason")}
        | {"answer": (result.get("answer") or "")[:3000]})
    return result
