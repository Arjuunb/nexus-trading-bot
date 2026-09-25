"""Hashed build assets are cacheable for a year; misses are not."""
import pytest


def test_hashed_assets_are_immutable_and_misses_are_not_cached(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app as hub_app
    (tmp_path / "index-abc123.js").write_text("console.log(1)")
    api = FastAPI()
    api.mount("/assets", hub_app._HashedAssets(directory=str(tmp_path)), name="a")
    c = TestClient(api)
    hit = c.get("/assets/index-abc123.js")
    assert hit.status_code == 200 and hit.headers["cache-control"] == "public, max-age=31536000, immutable"
    miss = c.get("/assets/missing.js")
    assert miss.status_code == 404 and "immutable" not in miss.headers.get("cache-control", "")
