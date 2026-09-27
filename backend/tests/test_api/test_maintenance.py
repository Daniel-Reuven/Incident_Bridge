"""
Integration tests for the maintenance (strict FIFO) endpoints in
app/api/incidents.py, over real HTTP. See tests/conftest.py for the
admin_client / user_client fixtures used below.
"""


def test_maintenance_queue_starts_empty(admin_client):
    response = admin_client.get("/incidents/maintenance/queue")
    assert response.status_code == 200
    body = response.json()
    assert body["current"] is None
    assert body["pending"] == []


def test_create_maintenance_task(admin_client):
    response = admin_client.post(
        "/incidents/maintenance", json={"title": "Patch A", "description": "monthly patch"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "maintenance"
    assert body["status"] == "open"
    assert body["title"] == "Patch A"


def test_any_authenticated_role_can_create_a_maintenance_task(user_client):
    """Creating a task has no role restriction - both admins and regular users can do it."""
    response = user_client.post(
        "/incidents/maintenance", json={"title": "Rotate logs", "description": "weekly job"}
    )
    assert response.status_code == 200


def test_start_next_follows_fifo_order(admin_client):
    admin_client.post("/incidents/maintenance", json={"title": "Patch A", "description": "d"})
    admin_client.post("/incidents/maintenance", json={"title": "Rotate backups", "description": "d"})

    first = admin_client.post("/incidents/maintenance/start-next")
    assert first.status_code == 200
    assert first.json()["title"] == "Patch A"


def test_start_next_with_nothing_pending_returns_404(admin_client):
    response = admin_client.post("/incidents/maintenance/start-next")
    assert response.status_code == 404


def test_start_next_while_one_is_already_in_progress_returns_409(admin_client):
    admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"})
    admin_client.post("/incidents/maintenance", json={"title": "B", "description": "d"})
    admin_client.post("/incidents/maintenance/start-next")

    second_attempt = admin_client.post("/incidents/maintenance/start-next")
    assert second_attempt.status_code == 409


def test_complete_current_closes_the_task_and_unblocks_the_next_one(admin_client):
    admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"})
    admin_client.post("/incidents/maintenance", json={"title": "B", "description": "d"})
    admin_client.post("/incidents/maintenance/start-next")

    completed = admin_client.post(
        "/incidents/maintenance/complete-current",
        json={"message": "Done.", "resolution_type": "resolved"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "closed"

    started_next = admin_client.post("/incidents/maintenance/start-next")
    assert started_next.status_code == 200
    assert started_next.json()["title"] == "B"


def test_complete_current_with_nothing_in_progress_returns_409(admin_client):
    response = admin_client.post(
        "/incidents/maintenance/complete-current", json={"message": "Done.", "resolution_type": "resolved"}
    )
    assert response.status_code == 409


def test_maintenance_task_cannot_be_closed_via_the_fault_close_endpoint(admin_client):
    """
    /incidents/faults/{id}/close only works on Fault objects - a
    maintenance task's id must be rejected with 400, since maintenance
    tasks are only ever closed via /incidents/maintenance/complete-current
    (see app/api/incidents.py's close_fault(), and backend/README.md's
    API surface table).
    """
    admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"})
    admin_client.post("/incidents/maintenance/start-next")
    queue = admin_client.get("/incidents/maintenance/queue").json()
    task_id = queue["current"]["id"]

    response = admin_client.post(
        f"/incidents/faults/{task_id}/close", json={"resolution_type": "resolved", "message": "n/a"}
    )
    assert response.status_code == 400
