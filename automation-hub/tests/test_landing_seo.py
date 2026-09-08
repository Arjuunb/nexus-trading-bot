"""The public landing routes serve their build-time pre-rendered documents."""

from pathlib import Path
from types import SimpleNamespace

import app as app_module


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_public_route_uses_its_prerendered_document(monkeypatch, tmp_path):
    _write(tmp_path / "index.html", "<head></head><body>generic shell</body>")
    _write(tmp_path / "seo" / "features.html", "<head></head><body><h1>Features</h1></body>")
    monkeypatch.setattr(app_module, "_LANDING", tmp_path)
    app_module._landing_document.cache_clear()
    try:
        html = app_module._landing_document("/features")
        assert "<h1>Features</h1>" in html
        assert "generic shell" not in html
    finally:
        app_module._landing_document.cache_clear()


def test_private_route_and_missing_prerender_fall_back_to_shell(monkeypatch, tmp_path):
    _write(tmp_path / "index.html", "<head></head><body>generic shell</body>")
    monkeypatch.setattr(app_module, "_LANDING", tmp_path)
    app_module._landing_document.cache_clear()
    try:
        assert "generic shell" in app_module._landing_document("/settings/profile")
        assert "generic shell" in app_module._landing_document("/features")
    finally:
        app_module._landing_document.cache_clear()


def test_public_and_private_landing_headers(monkeypatch, tmp_path):
    _write(tmp_path / "index.html", "<head></head><body>generic shell</body>")
    _write(tmp_path / "seo" / "features.html", "<head></head><body>features</body>")
    monkeypatch.setattr(app_module, "_LANDING", tmp_path)
    app_module._landing_document.cache_clear()
    try:
        public_request = SimpleNamespace(url=SimpleNamespace(path="/features"))
        private_request = SimpleNamespace(url=SimpleNamespace(path="/settings/account"))
        public = app_module._serve_landing(public_request)
        private = app_module._serve_landing(private_request)
        assert public.headers["cache-control"].startswith("public")
        assert "x-robots-tag" not in public.headers
        assert private.headers["cache-control"] == "no-store"
        assert private.headers["x-robots-tag"] == "noindex, nofollow"
    finally:
        app_module._landing_document.cache_clear()
