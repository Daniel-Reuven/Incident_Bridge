"""
Integration tests for GET /incidents/work/stale - the endpoint behind the
dashboard's "Check stale incidents" button. The selection logic itself
(the generator in app/iterators.py) is covered in tests/test_stale_work_generator.py;
these tests check that the endpoint wires it up correctly.
"""

from datetime import timedelta


def _create_task(client, title):
    return client.post("/incidents/maintenance", json={"title": title, "description": "d"}).json()


def _create_fault(client, title):
    return client.post("/incidents/faults", json={"title": title, "description": "d", "details": {}}).json()


def _idle_for(client, incident_id, minutes):
    """Make an incident look like it had no update for `minutes` - the API can't backdate itself."""
    state = client.app.state.incident_bridge
    state.incidents.get(incident_id).updated_at -= timedelta(minutes=minutes)


def test_stale_requires_login(app_client):
    assert app_client.get("/incidents/work/stale").status_code == 401


def test_nothing_stale_when_there_is_no_in_progress_work(admin_client):
    _create_task(admin_client, "Still waiting")
    body = admin_client.get("/incidents/work/stale").json()
    assert body["count"] == 0 and body["results"] == []
    assert body["stale_after_minutes"] == 240


def test_an_in_progress_task_idle_for_hours_is_reported_as_stale(admin_client):
    task = _create_task(admin_client, "Stuck patch")
    admin_client.post("/incidents/maintenance/start-next")
    _idle_for(admin_client, task["id"], 300)

    body = admin_client.get("/incidents/work/stale").json()
    assert body["count"] == 1
    result = body["results"][0]
    assert result["id"] == task["id"] and result["type"] == "maintenance"
    assert result["minutes_since_update"] >= 300


def test_a_freshly_started_task_is_not_stale(admin_client):
    _create_task(admin_client, "Just started")
    admin_client.post("/incidents/maintenance/start-next")
    assert admin_client.get("/incidents/work/stale").json()["count"] == 0


def test_a_waiting_task_is_never_stale_however_old_it_is(admin_client):
    task = _create_task(admin_client, "Waiting in the queue")
    _idle_for(admin_client, task["id"], 5000)
    assert admin_client.get("/incidents/work/stale").json()["count"] == 0


def test_a_claimed_fault_idle_for_hours_is_reported_as_stale(admin_client):
    fault = _create_fault(admin_client, "Claimed and forgotten")
    admin_client.post("/incidents/faults/claim-next")
    _idle_for(admin_client, fault["id"], 600)

    body = admin_client.get("/incidents/work/stale").json()
    assert [r["id"] for r in body["results"]] == [fault["id"]]
    assert body["results"][0]["type"] == "fault"
    assert body["results"][0]["assigned_to"] == "admin"


def test_the_threshold_can_be_lowered_for_a_demo(admin_client):
    task = _create_task(admin_client, "Idle a few minutes")
    admin_client.post("/incidents/maintenance/start-next")
    _idle_for(admin_client, task["id"], 5)

    assert admin_client.get("/incidents/work/stale").json()["count"] == 0  # default is 4 hours
    lowered = admin_client.get("/incidents/work/stale", params={"stale_after_minutes": 1}).json()
    assert lowered["count"] == 1 and lowered["stale_after_minutes"] == 1


def test_an_invalid_threshold_is_rejected(admin_client):
    assert admin_client.get("/incidents/work/stale", params={"stale_after_minutes": 0}).status_code == 422
