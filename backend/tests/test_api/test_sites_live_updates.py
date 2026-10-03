"""
Integration tests: the site portal endpoints (app/api/sites.py) publish
admin-only live-update events with scope "sites", and never to regular
users' tabs. Subscribes directly on the app's broadcaster (as the SSE
endpoint would) rather than holding a real SSE stream open.

Run from backend/:  python -m pytest tests/test_api/test_sites_live_updates.py -v
"""

import time

import pytest

from app.models import MailingList, Site
from app.services import FakeChecker, OutboxNotifier
from app.sites import SiteDirectory


@pytest.fixture
def portal(admin_client):
    from app.api.app import app
    directory = SiteDirectory(checker=FakeChecker({"https://down.example.com": [None]}),
                              notifier=OutboxNotifier(log=None))
    directory.add_site(Site(1001, "Portal", "https://down.example.com"))
    directory.add_mailing_list(MailingList("ops", "Ops", ["ops@example.com"], [1001]))
    app.state.incident_bridge.sites = directory
    return admin_client


def drain(queue, wait=1.0):
    """Collect what arrived on a broadcaster queue (events are delivered by the server's loop thread)."""
    deadline = time.time() + wait
    while queue.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.05)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_site_events_reach_other_admin_tabs_but_not_users(portal):
    from app.api.app import app
    broadcaster = app.state.broadcaster
    other_admin = broadcaster.subscribe("other-admin-tab", role="admin")
    regular_user = broadcaster.subscribe("user-tab", role="user")
    acting_tab = broadcaster.subscribe("acting-tab", role="admin")

    response = portal.post("/sites/1001/status", json={"status": "down", "reason": "outage"},
                           headers={"X-Client-Id": "acting-tab"})
    assert response.status_code == 200

    (event,) = drain(other_admin)
    assert event["scope"] == "sites" and event["action"] == "site_status_changed"
    assert event["subject"] == "Site 1001 (Portal)" and event["status"] == "down" and event["notification"] == "draft"
    assert event["actor"] == "admin"
    assert drain(regular_user, wait=0.2) == []
    assert drain(acting_tab, wait=0.2) == []


def test_check_and_notification_events(portal):
    from app.api.app import app
    tab = app.state.broadcaster.subscribe("watcher", role="admin")
    portal.post("/sites/1001/check")
    portal.post("/sites/1001/check")
    events = drain(tab)
    assert [e["action"] for e in events] == ["site_checked", "site_checked"]
    assert events[-1]["status"] == "down" and events[-1]["changed"] is True

    draft = portal.get("/sites/notifications?state=draft").json()[0]
    portal.post(f"/sites/notifications/{draft['id']}/send", json={})
    (sent,) = drain(tab)
    assert sent["action"] == "notification_sent" and sent["site_id"] == 1001


def test_incident_events_still_reach_everyone(portal, user_client):
    from app.api.app import app
    user_tab = app.state.broadcaster.subscribe("user-tab-2", role="user")
    portal.post("/incidents/maintenance", json={"title": "Patch", "description": "d"})
    (event,) = drain(user_tab)
    assert "scope" not in event and event["action"] == "created"
