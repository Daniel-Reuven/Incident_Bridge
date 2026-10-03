"""
Integration tests for the admin-only site portal API (app/api/sites.py),
over real HTTP via FastAPI's TestClient. See tests/conftest.py for the
admin_client / user_client fixtures.

Each test swaps the app's SiteDirectory for a small, known one with a
FakeChecker (no network) and an OutboxNotifier (no email) - see the
`portal` fixture.

Run from backend/:  python -m pytest tests/test_api/test_sites_api.py -v
"""

import pytest

from app.models import MailingList, Site
from app.services import FakeChecker, OutboxNotifier
from app.sites import SiteDirectory

PORTAL, STORE = "https://portal.example.com", "https://store.example.com"


@pytest.fixture
def portal(admin_client):
    """admin_client, with the app's site directory replaced by a known, memory-only one."""
    from app.api.app import app
    directory = SiteDirectory(checker=FakeChecker({STORE: [None]}), notifier=OutboxNotifier(log=None))
    directory.add_site(Site(1001, "Portal", PORTAL))
    directory.add_site(Site(1002, "Store", STORE))
    directory.add_mailing_list(MailingList("ops", "Ops", ["ops@example.com"], [1001, 1002]))
    app.state.incident_bridge.sites = directory
    return admin_client


# --- access control ---------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("get", "/sites"), ("get", "/sites/report"), ("get", "/sites/1001"), ("post", "/sites/check-all"),
    ("get", "/sites/mailing-lists"), ("get", "/sites/notifications"), ("post", "/sites/1001/archive"),
])
def test_regular_users_get_403_everywhere(portal, user_client, method, path):
    response = getattr(user_client, method)(path)
    assert response.status_code == 403
    assert response.json()["detail"] == "The site portal is available to admins only."


def test_anonymous_callers_get_401(app_client):
    assert app_client.get("/sites").status_code == 401


# --- sites ---------------------------------------------------------------------

def test_list_sites_includes_lists_incidents_and_check_state(portal):
    portal.post("/incidents/maintenance", json={"title": "Patch Site 1001", "description": "d"})
    sites = portal.get("/sites").json()
    assert [s["site_id"] for s in sites] == [1001, 1002]
    first = sites[0]
    assert first["status"] == "unknown" and first["list_ids"] == ["ops"]
    assert len(first["incident_ids"]) == 1 and first["checking"] is False and first["last_check"] is None


def test_create_edit_and_validation_errors(portal):
    created = portal.post("/sites", json={"site_name": "Docs", "site_url": "https://docs.example.com",
                                          "site_publish_date": "2024-02-01"})
    assert created.status_code == 200 and created.json()["site_id"] == 1003
    edited = portal.patch("/sites/1003", json={"site_name": "Developer docs"})
    assert edited.json()["site_name"] == "Developer docs" and edited.json()["site_publish_date"] == "2024-02-01"
    cleared = portal.patch("/sites/1003", json={"site_publish_date": None})
    assert cleared.json()["site_publish_date"] is None
    bad = portal.post("/sites", json={"site_name": "X", "site_url": "ftp://x"})
    assert bad.status_code == 400 and "site_url" in bad.json()["detail"]
    dup = portal.post("/sites", json={"site_name": "X", "site_url": PORTAL})
    assert dup.status_code == 400 and "already uses" in dup.json()["detail"]


def test_site_detail(portal):
    portal.post("/incidents/maintenance", json={"title": "Site 1001 slow", "description": "d"})
    portal.post("/sites/1001/status", json={"status": "down", "reason": "outage"})
    detail = portal.get("/sites/1001").json()
    assert detail["incidents"][0]["title"] == "Site 1001 slow"
    assert detail["status_history"][0]["new_status"] == "down"
    assert detail["notifications"][0]["state"] == "draft"


def test_unknown_site_is_404(portal):
    assert portal.get("/sites/9999").status_code == 404


def test_manual_status_change_drafts_a_notification(portal):
    body = portal.post("/sites/1001/status", json={"status": "down", "reason": "Customers report errors"}).json()
    assert body["site"]["status"] == "down"
    assert body["change"]["reason"] == "Customers report errors"
    assert body["notification"]["recipients"] == ["ops@example.com"]
    blank = portal.post("/sites/1001/status", json={"status": "operational", "reason": " "})
    assert blank.status_code == 400


def test_check_one_and_check_all(portal):
    one = portal.post("/sites/1002/check").json()
    assert one["check"]["observed_status"] == "down" and one["site"]["status"] == "degraded"
    assert one["notification"]["state"] == "draft"
    everything = portal.post("/sites/check-all").json()
    assert everything["checked"] == 2
    statuses = {r["site"]["site_id"]: r["site"]["status"] for r in everything["results"]}
    assert statuses == {1001: "operational", 1002: "down"}


def test_archive_and_restore_site(portal):
    archived = portal.post("/sites/1002/archive").json()
    assert archived["site"]["is_archived"] is True and archived["unlinked_list_ids"] == ["ops"]
    assert [s["site_id"] for s in portal.get("/sites").json()] == [1001]
    assert len(portal.get("/sites?include_archived=true").json()) == 2
    assert portal.post("/sites/1002/check").status_code == 409
    restored = portal.post("/sites/1002/restore").json()
    assert restored["relinked_list_ids"] == ["ops"] and restored["site"]["status"] == "unknown"
    assert portal.post("/sites/1002/restore").status_code == 409


# --- mailing lists ----------------------------------------------------------

def test_mailing_list_crud(portal):
    created = portal.post("/sites/mailing-lists", json={"list_id": "mgmt", "name": "Management",
                                                         "members": ["cto@example.com"], "site_ids": [1001]})
    assert created.status_code == 200 and created.json()["member_count"] == 1
    edited = portal.patch("/sites/mailing-lists/mgmt", json={"members": ["a@example.com", "b@example.com"]}).json()
    assert edited["members"] == ["a@example.com", "b@example.com"] and edited["site_ids"] == [1001]
    assert portal.patch("/sites/mailing-lists/mgmt", json={"site_ids": [9999]}).status_code == 400
    assert portal.post("/sites/mailing-lists/mgmt/archive").json()["is_archived"] is True
    assert [ml["list_id"] for ml in portal.get("/sites/mailing-lists").json()] == ["ops"]
    assert portal.post("/sites/mailing-lists/mgmt/restore").json()["is_archived"] is False
    assert portal.post("/sites/mailing-lists", json={"list_id": "Bad Id", "name": "x"}).status_code == 400
    assert portal.patch("/sites/mailing-lists/nope", json={"name": "x"}).status_code == 404


# --- notifications ----------------------------------------------------------

def test_send_and_dismiss_notifications(portal):
    from app.api.app import app
    first = portal.post("/sites/1001/status", json={"status": "down", "reason": "x"}).json()["notification"]
    second = portal.post("/sites/1002/status", json={"status": "down", "reason": "x"}).json()["notification"]
    assert {n["id"] for n in portal.get("/sites/notifications?state=draft").json()} == {first["id"], second["id"]}

    sent = portal.post(f"/sites/notifications/{first['id']}/send", json={"message": "Portal is down."}).json()
    assert sent["state"] == "sent" and sent["message"] == "Portal is down."
    assert app.state.incident_bridge.sites._notifier.outbox[0].body == "Portal is down."
    assert portal.post(f"/sites/notifications/{first['id']}/send", json={}).status_code == 409

    dismissed = portal.post(f"/sites/notifications/{second['id']}/dismiss", json={"reason": "false alarm"}).json()
    assert dismissed["state"] == "dismissed" and dismissed["dismiss_reason"] == "false alarm"
    assert portal.get("/sites/notifications?state=draft").json() == []
    assert portal.get("/sites/notifications?state=bogus").status_code == 422
    assert portal.post("/sites/notifications/nope/dismiss", json={}).status_code == 404


# --- report -------------------------------------------------------------------

def test_report(portal):
    portal.post("/incidents/faults", json={"title": "Site 1001 and Site 4242 broken", "description": "d"})
    portal.post("/sites/1002/status", json={"status": "down", "reason": "x"})
    report = portal.get("/sites/report").json()
    assert report["most_urgent"] == 1002
    assert report["unreported_outages"] == [1002]
    assert report["unknown_references"].keys() == {"4242"}
    assert report["rows"][0]["site_id"] == 1002
