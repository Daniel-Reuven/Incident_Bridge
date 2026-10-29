"""
API tests for AI analysis (app/api/analysis.py), with the fake provider
(AI_PROVIDER=fake - no key, no network).

Run from backend/:  python -m pytest tests/test_api/test_analysis_api.py -v
"""


import pytest
from fastapi.testclient import TestClient

from tests.install_helper import finish_setup

from app.ai.providers.fake_provider import FakeProvider
from tests.test_ai.conftest import wait_until

AI_VARIABLES = ["AI_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "AI_REQUEST_DELAY_SECONDS"]


def login(client, username="admin", password="Demo#Pass1"):
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def create_fault(client):
    return client.post("/incidents/faults", json={"title": "Checkout failing", "description": "Payments fail.",
                                                 "details": {"performance_degraded": True}}).json()


@pytest.fixture
def ai_client(monkeypatch):
    """A logged-in admin client with AI analysis on (fake provider, no delay)."""
    for name in AI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_PROVIDER", "fake")
    monkeypatch.setenv("AI_REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        login(client)
        yield client


@pytest.fixture
def off_client(monkeypatch):
    """A logged-in admin client with AI analysis off (the default provider, Gemini, has no key)."""
    for name in AI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    from app.api.app import app
    with TestClient(app) as client:
        finish_setup(client)
        login(client)
        yield client


def comments(client, incident_id):
    return client.get(f"/incidents/{incident_id}").json()["comments"]


def test_status_when_the_feature_is_off(off_client):
    fault = create_fault(off_client)
    assert off_client.get(f"/incidents/faults/{fault['id']}/analysis").json()["enabled"] is False
    response = off_client.post(f"/incidents/faults/{fault['id']}/analysis")
    assert response.status_code == 503 and "not configured" in response.json()["detail"]


def test_requesting_an_analysis_end_to_end(ai_client):
    fault = create_fault(ai_client)
    status = ai_client.get(f"/incidents/faults/{fault['id']}/analysis").json()
    assert status == {"enabled": True, "provider": "Fake AI", "model": "fake-model", "models": ["fake-model"],
                      "state": "idle", "position": None, "last_analysis": None, "blocked": False,
                      "reason": None, "can_force": False, "force_wait_seconds": 0}

    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis").status_code == 200
    wait_until(lambda: len(comments(ai_client, fault["id"])) == 2)
    first, second = comments(ai_client, fault["id"])
    assert (first["author"], first["text"]) == ("admin", "admin requested Fake AI analysis.")
    assert second["author"] == "ai-assistant" and second["text"].startswith("AI analysis by Fake AI")


def test_the_status_comes_from_the_settings_until_the_first_request(ai_client):
    """v0.4.5: the page works before the worker thread starts, and asking for the status starts nothing."""
    worker = ai_client.app.state.analysis_worker
    fault = create_fault(ai_client)
    status = ai_client.get(f"/incidents/faults/{fault['id']}/analysis").json()
    assert (status["provider"], status["models"]) == ("Fake AI", ["fake-model"])
    assert not worker.started and not worker.provider_loaded
    ai_client.post(f"/incidents/faults/{fault['id']}/analysis")
    assert worker.started
    wait_until(lambda: worker.provider_loaded)


def test_a_duplicate_request_and_cancelling(ai_client):
    worker = ai_client.app.state.analysis_worker
    worker.provider = FakeProvider(delay_seconds=0.5)        # keep the first job busy for a moment
    fault, other = create_fault(ai_client), create_fault(ai_client)
    ai_client.post(f"/incidents/faults/{fault['id']}/analysis")
    ai_client.post(f"/incidents/faults/{other['id']}/analysis")

    duplicate = ai_client.post(f"/incidents/faults/{other['id']}/analysis")
    assert duplicate.status_code == 409 and "already pending" in duplicate.json()["detail"]
    assert ai_client.get(f"/incidents/faults/{other['id']}/analysis").json()["state"] == "queued"

    cancelled = ai_client.post(f"/incidents/faults/{other['id']}/analysis/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "idle"
    assert comments(ai_client, other["id"])[-1]["text"] == "admin requested to cancel Fake AI analysis."
    assert ai_client.post(f"/incidents/faults/{other['id']}/analysis/cancel").status_code == 409


def test_a_regular_user_can_request_and_cancel(ai_client):
    ai_client.app.state.analysis_worker.provider = FakeProvider(delay_seconds=0.5)
    fault = create_fault(ai_client)
    ai_client.post("/auth/logout")
    login(ai_client, "tech1", "Demo#Pass2")
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis").status_code == 200
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis/cancel").status_code == 200


def test_closed_faults_and_maintenance_tasks_are_refused(ai_client):
    fault = create_fault(ai_client)
    ai_client.post(f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Fixed."})
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis").status_code == 409

    task = ai_client.post("/incidents/maintenance", json={"title": "Patch", "description": "d"}).json()
    assert ai_client.post(f"/incidents/faults/{task['id']}/analysis").status_code == 400
    assert ai_client.get("/incidents/faults/nope/analysis").status_code == 404


def test_login_is_required(ai_client):
    fault = create_fault(ai_client)
    ai_client.post("/auth/logout")
    assert ai_client.get(f"/incidents/faults/{fault['id']}/analysis").status_code == 401
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis").status_code == 401


def test_the_reanalysis_rule_over_http(ai_client, monkeypatch):
    fault = create_fault(ai_client)
    ai_client.post(f"/incidents/faults/{fault['id']}/analysis")
    wait_until(lambda: len(comments(ai_client, fault["id"])) == 2)
    wait_until(lambda: ai_client.get(f"/incidents/faults/{fault['id']}/analysis").json()["state"] == "idle")

    status = ai_client.get(f"/incidents/faults/{fault['id']}/analysis").json()
    assert status["blocked"] and status["last_analysis"]["provider"] == "Fake AI"
    assert status["can_force"] is False and status["force_wait_seconds"] > 0     # admin, within the cooldown

    again = ai_client.post(f"/incidents/faults/{fault['id']}/analysis")
    assert again.status_code == 409 and "nothing has changed since" in again.json()["detail"]
    forced = ai_client.post(f"/incidents/faults/{fault['id']}/analysis?force=true")
    assert forced.status_code == 409 and "5-minute cooldown" in forced.json()["detail"]

    ai_client.app.state.analysis_worker.cooldown_seconds = 0                     # the cooldown has passed
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis?force=true").status_code == 200


def test_a_user_cannot_force_and_a_comment_unblocks_it(ai_client):
    fault = create_fault(ai_client)
    ai_client.post(f"/incidents/faults/{fault['id']}/analysis")
    wait_until(lambda: len(comments(ai_client, fault["id"])) == 2)
    wait_until(lambda: ai_client.get(f"/incidents/faults/{fault['id']}/analysis").json()["state"] == "idle")
    ai_client.post("/auth/logout")
    login(ai_client, "tech1", "Demo#Pass2")
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis?force=true").status_code == 403
    ai_client.post(f"/incidents/{fault['id']}/comments", json={"text": "Still failing after the restart."})
    assert ai_client.get(f"/incidents/faults/{fault['id']}/analysis").json()["blocked"] is False
    assert ai_client.post(f"/incidents/faults/{fault['id']}/analysis").status_code == 200
