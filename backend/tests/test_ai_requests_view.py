"""
The site portal's AI requests view (app/api/ai_requests.py,
GET /analysis-requests) and the Fake AI's findings line
(app/ai/providers/fake_provider.py): every request with its state, fault,
findings and archive date; archived ones apart; admins only; the history
still shown with the AI feature off.

Run from backend/:  python -m pytest tests/test_ai_requests_view.py -v
"""

import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.jobs import AnalysisJob
from app.ai.providers.fake_provider import canned_findings
from tests.install_helper import finish_setup


def login(client, username="admin", password="Demo#Pass1"):
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def wait_for(client, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get("/analysis-requests").json()
        if condition(data):
            return data
        time.sleep(0.05)
    raise AssertionError("the AI request did not reach the expected state in time")


@pytest.fixture
def ai_app(monkeypatch, tmp_path):
    """The app on a database file, AI analysis on (fake provider, no delay)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ib.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AI_PROVIDER", "fake")
    monkeypatch.setenv("AI_REQUEST_DELAY_SECONDS", "0")
    from app.api.app import app
    return app


def request_analysis_of_new_fault(client, title="Site 1007 logins failing"):
    fault = client.post("/incidents/faults", json={"title": title, "description": "Also slow: site #1002.",
                                                   "details": {"performance_degraded": True}}).json()
    assert client.post(f"/incidents/faults/{fault['id']}/analysis").status_code == 200
    return fault


def test_a_finished_request_with_its_fault_and_findings(ai_app):
    with TestClient(ai_app) as client:
        finish_setup(client)
        login(client)
        fault = request_analysis_of_new_fault(client)
        data = wait_for(client, lambda d: d["requests"] and d["requests"][0]["state"] == "success")
        [row] = data["requests"]
        assert data["enabled"] is True and data["archive_days"] == 30
        assert row["incident_id"] == fault["id"] and row["incident_title"] == "Site 1007 logins failing"
        assert row["requested_by"] == "admin" and row["provider"] == "Fake AI" and row["model"] == "fake-model"
        assert row["findings"]["affected_site_ids"] == [1002, 1007]      # the sites the fault mentions
        assert row["finished_at"] and row["archive_at"]
        assert client.get("/analysis-requests?archived=true").json()["requests"] == []


def test_queued_requests_and_the_archive(ai_app):
    with TestClient(ai_app) as client:
        finish_setup(client)
        login(client)
        request_analysis_of_new_fault(client)
        wait_for(client, lambda d: d["requests"] and d["requests"][0]["state"] == "success")
        worker = ai_app.state.analysis_worker
        worker.history.record_requested(AnalysisJob(incident_id="seed-x", requested_by="tech1"), "Fake AI")
        live = client.get("/analysis-requests").json()["requests"]
        assert [r["state"] for r in live] == ["queued", "success"]       # newest first; not finished: queued
        assert live[0]["incident_title"] is None                         # a fault that does not exist
        ai_app.state.incident_bridge.request_archive_after = timedelta(microseconds=1)   # everything finished is old
        assert [r["state"] for r in client.get("/analysis-requests").json()["requests"]] == ["queued"]
        archived = client.get("/analysis-requests?archived=true").json()["requests"]
        assert [r["state"] for r in archived] == ["success"]             # an unfinished one is never archived


def test_only_admins(user_client, app_client):
    assert user_client.get("/analysis-requests").status_code == 403
    assert TestClient(app_client.app).get("/analysis-requests").status_code == 401


def test_with_the_ai_off_the_history_is_still_shown(ai_app, monkeypatch):
    with TestClient(ai_app) as client:
        finish_setup(client)
        login(client)
        request_analysis_of_new_fault(client)
        wait_for(client, lambda d: d["requests"] and d["requests"][0]["state"] == "success")
        assert client.put("/server/ai", json={"provider": "off"}).status_code == 200
    with TestClient(ai_app) as client:                                  # a restart with the feature off
        login(client)
        data = client.get("/analysis-requests").json()
        assert data["enabled"] is False and [r["state"] for r in data["requests"]] == ["success"]


@pytest.mark.parametrize("incident, comments, expected_ids, severity", [
    ({"title": "Site 1007 down", "description": "and site #1002", "severity": "Critical"}, [], [1002, 1007],
     "critical"),
    ({"title": "Slow", "description": "nothing named", "severity": "Minor"}, [{"text": "see Site-1009"}], [1009],
     "minor"),
    ({"title": "No sites", "description": "-", "severity": "?"}, [], [], "major"),
])
def test_the_fake_ai_names_the_sites_the_fault_mentions(incident, comments, expected_ids, severity):
    findings = canned_findings({"incident": incident, "comments": comments})
    assert findings == {"suggested_severity": severity, "affected_site_ids": expected_ids, "confidence": 0.5}
    json.dumps(findings)                                               # one valid JSON line
