"""Secret redaction at the serializer and in stored text (services/redaction.py)."""
import asyncio
import json

import pytest

from services import redaction
from services.redaction import REDACTED, RedactionMiddleware, redact, redact_json_bytes, scrub_text

LIVE_SECRET = "live-exchange-secret-4f9a1c2b"


@pytest.fixture(autouse=True)
def _known_secret(monkeypatch):
    monkeypatch.setenv("HUB_EXCHANGE_API_SECRET", LIVE_SECRET)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "public-anon-key-shipped-to-browsers")
    redaction.refresh_known_secrets()
    yield
    monkeypatch.delenv("HUB_EXCHANGE_API_SECRET", raising=False)
    redaction.refresh_known_secrets()


def test_secret_named_strings_are_redacted_in_any_case_style():
    doc = {"api_key": "abc123abc123", "apiSecret": "zzz", "X-Webhook-Secret": "s3cr3t",
           "nested": [{"password": "hunter2", "access_token": "tok"}]}
    out = redact(doc)
    assert out["api_key"] == REDACTED
    assert out["apiSecret"] == REDACTED
    assert out["X-Webhook-Secret"] == REDACTED
    assert out["nested"][0] == {"password": REDACTED, "access_token": REDACTED}


def test_status_fields_counters_and_non_strings_survive():
    doc = {"api_key_set": True, "webhook_secret_set": False, "input_tokens": 1200,
           "max_tokens": 4096, "api_key": None, "secret": "", "password": 42}
    assert redact(doc) == doc


def test_live_secret_values_are_removed_under_any_key_and_inside_text():
    doc = {"note": f"connected with {LIVE_SECRET} ok", "rows": [LIVE_SECRET]}
    out = redact(doc)
    assert LIVE_SECRET not in json.dumps(out)
    assert out["note"] == f"connected with {REDACTED} ok"


def test_public_anon_key_is_not_treated_as_a_secret():
    assert "public-anon-key-shipped-to-browsers" not in redaction.known_secrets()


def test_credential_shapes_are_scrubbed_from_free_text():
    text = ("auth Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig and "
            "sk-ant-api03-abcdefghijklmnop and https://x/cb?token=abcdef123456&x=1")
    out = scrub_text(text)
    assert "eyJhbGci" not in out and "sk-ant-api03" not in out and "abcdef123456" not in out
    assert "&x=1" in out  # the rest of the URL is kept


def test_untouched_json_is_returned_as_the_same_object():
    body = json.dumps({"candles": [[1, 2, 3, 4]], "symbol": "BTCUSDT"}).encode()
    assert redact_json_bytes(body) is body


def test_by_name_off_still_removes_live_values():
    body = json.dumps({"token": "issued-to-owner", "echo": LIVE_SECRET}).encode()
    out = json.loads(redact_json_bytes(body, by_name=False))
    assert out["token"] == "issued-to-owner"
    assert out["echo"] == REDACTED


def _run(app, path="/x"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app({"type": "http", "path": path, "method": "GET", "headers": []}, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start, body


def _json_app(payload, ctype=b"application/json", chunks=1):
    raw = json.dumps(payload).encode()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(raw)).encode())]})
        step = max(1, len(raw) // chunks)
        parts = [raw[i:i + step] for i in range(0, len(raw), step)]
        for i, part in enumerate(parts):
            await send({"type": "http.response.body", "body": part, "more_body": i < len(parts) - 1})
    return app


def test_middleware_redacts_chunked_json_and_fixes_content_length():
    mw = RedactionMiddleware(_json_app({"api_secret": "abcdefabcdef", "ok": True}, chunks=4))
    start, body = _run(mw)
    assert json.loads(body) == {"api_secret": REDACTED, "ok": True}
    assert dict(start["headers"])[b"content-length"] == str(len(body)).encode()


def test_middleware_leaves_html_alone_and_honours_credential_paths():
    html = RedactionMiddleware(_json_app({"password": "x" * 12}, ctype=b"text/html"))
    _, body = _run(html)
    assert json.loads(body) == {"password": "x" * 12}

    mw = RedactionMiddleware(_json_app({"token": "jwt-for-owner"}), credential_paths={"/auth/login"})
    _, body = _run(mw, path="/auth/login")
    assert json.loads(body) == {"token": "jwt-for-owner"}
    _, body = _run(mw, path="/elsewhere")
    assert json.loads(body) == {"token": REDACTED}
