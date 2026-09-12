"""A hung request must be diagnosable from inside the running process.

When the Price Action status route stopped returning, nginx cut it off at
ninety seconds and the app log stayed completely clean, because nothing was
raised. There was no way to ask the process where it was stuck. This endpoint
answers that: it names the frame every live thread is sitting in.
"""
import threading

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    # webhook_api first: routers.engine imports it, and importing the router
    # directly trips the circular import the rest of the suite avoids the
    # same way.
    import webhook_api  # noqa: F401
    from routers.engine import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_reports_every_live_thread_and_its_current_frame(client):
    response = client.get("/system/threads")
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert payload["thread_count"] >= 1
    assert payload["observed_at"]
    for row in payload["threads"]:
        assert row["name"]
        assert isinstance(row["stack"], list) and row["stack"], row
        # Innermost frame last, so the final line is where the thread is.
        assert all(isinstance(entry, str) for entry in row["stack"])


def test_a_blocked_thread_is_visible_by_name(client):
    """The point of the endpoint: find the thread that is stuck, by name."""
    release = threading.Event()
    entered = threading.Event()

    def parked():
        entered.set()
        release.wait(30)

    thread = threading.Thread(target=parked, name="deliberately-stuck", daemon=True)
    thread.start()
    assert entered.wait(5)
    try:
        payload = client.get("/system/threads").json()
        names = [row["name"] for row in payload["threads"]]
        assert "deliberately-stuck" in names, names
        stuck = next(row for row in payload["threads"]
                     if row["name"] == "deliberately-stuck")
        # It is parked in a wait, and the stack says so rather than guessing.
        assert any("wait" in entry for entry in stuck["stack"]), stuck["stack"]
    finally:
        release.set()
        thread.join(5)


def test_frame_depth_is_bounded(client):
    payload = client.get("/system/threads?frames=2").json()
    assert all(len(row["stack"]) <= 2 for row in payload["threads"])


def test_frame_depth_is_validated(client):
    assert client.get("/system/threads?frames=0").status_code == 422
    assert client.get("/system/threads?frames=500").status_code == 422
