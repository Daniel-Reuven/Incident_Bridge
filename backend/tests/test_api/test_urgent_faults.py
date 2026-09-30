"""Integration tests for GET /incidents/faults/urgent (lazy pipeline - see app/iterators.py)."""


def _fault(client, title, details):
    return client.post("/incidents/faults", json={"title": title, "description": "d", "details": details})


def test_urgent_requires_login(app_client):
    assert app_client.get("/incidents/faults/urgent").status_code == 401


def test_urgent_stops_early_and_reports_what_it_examined(admin_client):
    _fault(admin_client, "Typo", {"cosmetic_only": True})                      # 1 minor - skipped
    _fault(admin_client, "DB down", {"system_unavailable": True})              # 2 result 1
    _fault(admin_client, "Slow checkout", {"performance_degraded": True})      # 3 result 2
    _fault(admin_client, "Login broken", {"security_breach": True})            # 4 never needed

    body = admin_client.get("/incidents/faults/urgent", params={"limit": 2}).json()
    assert len(body["results"]) == 2
    assert body["results"][0]["summary"].startswith("[CRITICAL] DB down")
    assert body["results"][0]["id"]
    assert body["examined"] == 3
    assert body["total_incidents"] == 4
    assert body["stopped_early"] is True


def test_urgent_with_no_matching_faults_returns_empty(admin_client):
    _fault(admin_client, "Typo", {"cosmetic_only": True})
    body = admin_client.get("/incidents/faults/urgent").json()
    assert body["results"] == []
    assert body["stopped_early"] is False


def test_urgent_rejects_an_invalid_limit(admin_client):
    assert admin_client.get("/incidents/faults/urgent", params={"limit": 0}).status_code == 422
