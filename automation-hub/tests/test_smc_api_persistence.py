"""The SMC lab must report a contended SQLite read the way the PA lab does.

Both labs hydrate through services/lab_read_view.py, so both meet the same
``sqlite3.OperationalError`` when a writer holds the database. The Price Action
router converted that into a retryable 503; this router did not, so one
condition produced two different status codes depending on which lab an
operator happened to ask, and a 500 gave no hint that retrying was correct.
"""
import sqlite3

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import webhook_api
from routers.native_smc import router


def test_smc_read_endpoints_surface_persistence_block_instead_of_500(monkeypatch):
    class Paper:
        # ":memory:" makes lab_read_view yield the account itself, so the
        # error surfaces from the read rather than from opening a file.
        path = ":memory:"

        @staticmethod
        def session():
            raise sqlite3.OperationalError("database is locked")

    class Runtime:
        account = Paper()

        @staticmethod
        def bot_status():
            raise sqlite3.OperationalError("interrupted")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    monkeypatch.setattr(webhook_api, "smc_paper", Paper(), raising=False)
    monkeypatch.setattr(webhook_api, "smc_runtime", Runtime(), raising=False)

    for path in ("/research/smc/session", "/research/smc/bot-status"):
        response = client.get(path)
        assert response.status_code == 503, path
        detail = response.json()["detail"]
        assert detail["state"] == "PERSISTENCE_BLOCKED"
        assert detail["retryable"] is True
        assert detail["real_execution_allowed"] is False


def test_smc_status_does_not_mask_a_genuine_programming_error(monkeypatch):
    """Only a contended read becomes a 503. Everything else still surfaces."""
    class Account:
        path = ":memory:"

    class Runtime:
        account = Account()

        @staticmethod
        def bot_status():
            raise sqlite3.OperationalError("no such table: smc_sessions")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(webhook_api, "smc_runtime", Runtime(), raising=False)

    response = client.get("/research/smc/bot-status")
    assert response.status_code == 500
