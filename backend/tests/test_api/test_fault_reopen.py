"""
Integration tests for POST /incidents/faults/{id}/reopen (Task 2, step 3),
over real HTTP. See tests/conftest.py for the admin_client / user_client
fixtures; both share one running app, so an incident one creates is
visible to the other.
"""


def _create_fault(client, title, details=None):
    return client.post(
        "/incidents/faults", json={"title": title, "description": "d", "details": details or {}}
    ).json()


def _close(client, fault_id, message="Fixed."):
    return client.post(
        f"/incidents/faults/{fault_id}/close", json={"resolution_type": "resolved", "message": message}
    )


def _reopen(client, fault_id, status="open", reason="Regression found"):
    return client.post(f"/incidents/faults/{fault_id}/reopen", json={"status": status, "reason": reason})


def test_admin_can_reopen_a_closed_fault_as_open(admin_client):
    fault = _create_fault(admin_client, "Outage", {"system_unavailable": True})
    _close(admin_client, fault["id"])

    response = _reopen(admin_client, fault["id"], "open")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "open"
    assert body["resolution_type"] is None
    assert body["resolution_message"] is None
    queue = admin_client.get("/incidents/faults/queue").json()
    assert [f["id"] for f in queue] == [fault["id"]]


def test_reopen_logs_a_comment_with_the_reason_and_previous_resolution(admin_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"], "Fixed.")
    _reopen(admin_client, fault["id"], "open", "Regression found")

    log = admin_client.get(f"/incidents/{fault['id']}").json()["comments"][-1]
    assert log["author"] == "admin"
    assert "Status changed from Closed to Open." in log["text"]
    assert "Reason: Regression found." in log["text"]
    assert "Previous resolution: Resolved - Fixed." in log["text"]


def test_a_fault_reopened_as_open_goes_to_the_back_of_its_severity_group(admin_client):
    a = _create_fault(admin_client, "Critical A", {"system_unavailable": True})
    _create_fault(admin_client, "Critical B", {"system_unavailable": True})
    _close(admin_client, a["id"])
    _reopen(admin_client, a["id"], "open")

    queue = admin_client.get("/incidents/faults/queue").json()
    assert [f["title"] for f in queue] == ["Critical B", "Critical A"]


def test_a_fault_reopened_as_in_progress_is_assigned_to_the_admin_and_not_queued(admin_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"])

    response = _reopen(admin_client, fault["id"], "in_progress", "Picking this back up")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["assigned_to"] == "admin"
    assert admin_client.get("/incidents/faults/queue").json() == []


def test_reopening_as_open_clears_who_it_was_previously_claimed_by(admin_client, user_client):
    _create_fault(user_client, "Claimed then closed")
    claimed = user_client.post("/incidents/faults/claim-next").json()
    assert claimed["assigned_to"] == "tech1"
    _close(user_client, claimed["id"])

    body = _reopen(admin_client, claimed["id"], "open").json()
    assert body["assigned_to"] is None


def test_a_fault_reopened_as_open_can_be_claimed_again(admin_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"])
    _reopen(admin_client, fault["id"], "open")

    claimed = admin_client.post("/incidents/faults/claim-next")
    assert claimed.status_code == 200
    assert claimed.json()["id"] == fault["id"]


def test_a_regular_user_cannot_reopen_a_fault(admin_client, user_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"])

    response = _reopen(user_client, fault["id"], "open")
    assert response.status_code == 403
    assert admin_client.get(f"/incidents/{fault['id']}").json()["status"] == "closed"


def test_reopening_with_a_blank_reason_is_rejected(admin_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"])

    response = _reopen(admin_client, fault["id"], "open", "   ")
    assert response.status_code == 400
    assert admin_client.get(f"/incidents/{fault['id']}").json()["status"] == "closed"


def test_reopening_a_fault_that_is_not_closed_returns_409(admin_client):
    fault = _create_fault(admin_client, "Still open")
    response = _reopen(admin_client, fault["id"], "open")
    assert response.status_code == 409


def test_reopening_into_closed_is_rejected_with_400(admin_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"])
    response = _reopen(admin_client, fault["id"], "closed")
    assert response.status_code == 400


def test_an_unknown_status_value_is_rejected_with_422(admin_client):
    fault = _create_fault(admin_client, "Outage")
    _close(admin_client, fault["id"])
    response = _reopen(admin_client, fault["id"], "bogus")
    assert response.status_code == 422


def test_reopening_an_unknown_id_returns_404(admin_client):
    response = _reopen(admin_client, "00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_a_maintenance_task_cannot_be_reopened_through_the_fault_endpoint(admin_client):
    task = admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"}).json()
    response = _reopen(admin_client, task["id"], "open")
    assert response.status_code == 400