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
