"""Integration tests for GET /incidents/maintenance/pressing (lazy pipeline - see app/iterators.py)."""

from datetime import timedelta


def _create(client, title):
    return client.post("/incidents/maintenance", json={"title": title, "description": "d"}).json()


def _backdate(client, incident_id, days):
    """Make an incident look `days` old - the API itself can't create a past-dated one."""
    state = client.app.state.incident_bridge
    state.incidents.get(incident_id).created_at -= timedelta(days=days)


def _setup(client):
    a = _create(client, "A - will be in progress")
    b = _create(client, "B - old and waiting")
    _create(client, "C - fresh")
    d = _create(client, "D - old and waiting")
    _backdate(client, a["id"], 5)
    _backdate(client, b["id"], 6)
    _backdate(client, d["id"], 4)
    client.post("/incidents/maintenance/start-next")  # FIFO: A goes in progress
    return b, d


def test_pressing_requires_login(app_client):
    assert app_client.get("/incidents/maintenance/pressing").status_code == 401


def test_pressing_lists_old_open_calls_and_skips_in_progress_and_fresh(admin_client):
    b, d = _setup(admin_client)
    body = admin_client.get("/incidents/maintenance/pressing").json()
    assert [r["id"] for r in body["results"]] == [b["id"], d["id"]]
    assert body["results"][0]["days_open"] == 6
    assert body["examined"] == 4 and body["total_incidents"] == 4
    assert body["stopped_early"] is False


def test_pressing_with_a_limit_stops_early(admin_client):
    b, _ = _setup(admin_client)
    body = admin_client.get("/incidents/maintenance/pressing", params={"limit": 1}).json()
    assert [r["id"] for r in body["results"]] == [b["id"]]
    assert body["examined"] == 2          # A (in progress, skipped) and B (the result) - C and D never looked at
    assert body["stopped_early"] is True


def test_pressing_with_nothing_old_returns_empty(admin_client):
    _create(admin_client, "Fresh")
    body = admin_client.get("/incidents/maintenance/pressing").json()
    assert body["results"] == [] and body["stopped_early"] is False


def test_pressing_rejects_an_invalid_limit(admin_client):
    assert admin_client.get("/incidents/maintenance/pressing", params={"limit": 0}).status_code == 422
