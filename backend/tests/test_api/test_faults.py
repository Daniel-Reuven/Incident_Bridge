"""
Integration tests for the fault (single shared priority queue) endpoints
in app/api/incidents.py, over real HTTP. See tests/conftest.py for the
admin_client / user_client fixtures used below.
"""


def test_fault_queue_starts_empty(admin_client):
    response = admin_client.get("/incidents/faults/queue")
    assert response.status_code == 200
    assert response.json() == []


def test_creating_a_fault_auto_scores_its_severity(user_client):
    response = user_client.post(
        "/incidents/faults",
        json={"title": "Payment API down", "description": "d", "details": {"system_unavailable": True}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "fault"
    assert body["severity"] == 1  # SeverityCategory.CRITICAL


def test_fault_queue_is_priority_ordered_regardless_of_creation_order(user_client):
    user_client.post(
        "/incidents/faults",
        json={"title": "Typo on invoice page", "description": "d", "details": {"cosmetic_only": True}},
    )
    user_client.post(
        "/incidents/faults",
        json={"title": "Checkout slow", "description": "d", "details": {"performance_degraded": True}},
    )
    user_client.post(
        "/incidents/faults",
        json={"title": "Payment API down", "description": "d", "details": {"system_unavailable": True}},
    )

    queue = user_client.get("/incidents/faults/queue").json()
    severities = [f["severity"] for f in queue]
    assert severities == [1, 2, 3]
    assert queue[0]["title"] == "Payment API down"


def test_claim_next_assigns_the_most_severe_fault_to_the_caller(user_client):
    user_client.post(
        "/incidents/faults", json={"title": "Minor bug", "description": "d", "details": {"cosmetic_only": True}}
    )
    user_client.post(
        "/incidents/faults",
        json={"title": "Payment API down", "description": "d", "details": {"system_unavailable": True}},
    )

    response = user_client.post("/incidents/faults/claim-next")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Payment API down"
    assert body["assigned_to"] == "tech1"
    assert body["status"] == "in_progress"

    # It should no longer be sitting in the queue.
    remaining = user_client.get("/incidents/faults/queue").json()
    assert all(f["title"] != "Payment API down" for f in remaining)


def test_claim_next_on_an_empty_queue_returns_404(user_client):
    response = user_client.post("/incidents/faults/claim-next")
    assert response.status_code == 404


def test_change_severity_is_admin_only(admin_client, user_client):
    created = admin_client.post(
        "/incidents/faults", json={"title": "x", "description": "d", "details": {}}
    ).json()

    rejected = user_client.patch(f"/incidents/faults/{created['id']}/severity", json={"severity": 1})
    assert rejected.status_code == 403

    allowed = admin_client.patch(f"/incidents/faults/{created['id']}/severity", json={"severity": 1})
    assert allowed.status_code == 200
    assert allowed.json()["severity"] == 1


def test_escalating_severity_repositions_the_fault_in_the_queue(admin_client):
    admin_client.post(
        "/incidents/faults",
        json={"title": "Will stay major", "description": "d", "details": {"performance_degraded": True}},
    )
    minor = admin_client.post(
        "/incidents/faults", json={"title": "Will be escalated", "description": "d", "details": {"cosmetic_only": True}}
    ).json()

    admin_client.patch(f"/incidents/faults/{minor['id']}/severity", json={"severity": 1})

    queue = admin_client.get("/incidents/faults/queue").json()
    assert queue[0]["title"] == "Will be escalated"  # now Critical, pops ahead of the still-Major fault


def test_resolved_can_be_set_by_a_regular_user(user_client):
    fault = user_client.post(
        "/incidents/faults", json={"title": "x", "description": "d", "details": {}}
    ).json()
    response = user_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Fixed."}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "closed"


def test_not_an_incident_by_a_regular_user_is_rejected_with_403(user_client):
    fault = user_client.post(
        "/incidents/faults", json={"title": "x", "description": "d", "details": {}}
    ).json()
    response = user_client.post(
        f"/incidents/faults/{fault['id']}/close",
        json={"resolution_type": "not_an_incident", "message": "Expected behavior."},
    )
    assert response.status_code == 403


def test_not_an_incident_by_an_admin_succeeds(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "x", "description": "d", "details": {}}
    ).json()
    response = admin_client.post(
        f"/incidents/faults/{fault['id']}/close",
        json={"resolution_type": "not_an_incident", "message": "Expected behavior."},
    )
    assert response.status_code == 200
    assert response.json()["resolution_type"] == "not_an_incident"


def test_closing_with_an_empty_message_is_rejected(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "x", "description": "d", "details": {}}
    ).json()
    response = admin_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": ""}
    )
    assert response.status_code == 400


def _create_fault(client, title, details=None):
    return client.post(
        "/incidents/faults", json={"title": title, "description": "d", "details": details or {}}
    ).json()


def test_closing_an_unclaimed_fault_removes_it_from_the_priority_queue(admin_client):
    critical = _create_fault(admin_client, "Outage", {"system_unavailable": True})
    _create_fault(admin_client, "Typo", {"cosmetic_only": True})

    response = admin_client.post(
        f"/incidents/faults/{critical['id']}/close", json={"resolution_type": "resolved", "message": "Fixed."}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "closed"

    queue = admin_client.get("/incidents/faults/queue").json()
    assert [f["title"] for f in queue] == ["Typo"]


def test_claim_next_never_returns_a_closed_fault(admin_client):
    fault = _create_fault(admin_client, "Only fault")
    admin_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Duplicate."}
    )
    response = admin_client.post("/incidents/faults/claim-next")
    assert response.status_code == 404


def test_closing_an_unclaimed_fault_logs_a_comment_with_the_reason(user_client):
    fault = _create_fault(user_client, "Dup report")
    user_client.post(
        f"/incidents/faults/{fault['id']}/close",
        json={"resolution_type": "resolved", "message": "Duplicate of ticket 42."},
    )
    detail = user_client.get(f"/incidents/{fault['id']}").json()
    log = detail["comments"][-1]
    assert log["author"] == "tech1"
    assert "Status changed from Open to Closed." in log["text"]
    assert "Reason: Duplicate of ticket 42." in log["text"]
    assert "Resolution: Resolved." in log["text"]


def test_closing_a_claimed_fault_also_logs_a_comment(user_client):
    _create_fault(user_client, "Claim me")
    claimed = user_client.post("/incidents/faults/claim-next").json()
    user_client.post(
        f"/incidents/faults/{claimed['id']}/close", json={"resolution_type": "resolved", "message": "Patched."}
    )
    detail = user_client.get(f"/incidents/{claimed['id']}").json()
    assert "Status changed from In progress to Closed." in detail["comments"][-1]["text"]


def test_closing_an_already_closed_fault_returns_409_and_keeps_the_first_resolution(admin_client):
    fault = _create_fault(admin_client, "Close twice")
    url = f"/incidents/faults/{fault['id']}/close"
    admin_client.post(url, json={"resolution_type": "resolved", "message": "First."})

    second = admin_client.post(url, json={"resolution_type": "resolved", "message": "Second."})
    assert second.status_code == 409
    assert admin_client.get(f"/incidents/{fault['id']}").json()["resolution_message"] == "First."


def test_a_rejected_close_leaves_the_fault_in_the_queue(user_client):
    fault = _create_fault(user_client, "Stay queued")
    response = user_client.post(
        f"/incidents/faults/{fault['id']}/close",
        json={"resolution_type": "not_an_incident", "message": "Expected."},
    )
    assert response.status_code == 403
    queue = user_client.get("/incidents/faults/queue").json()
    assert [f["title"] for f in queue] == ["Stay queued"]
