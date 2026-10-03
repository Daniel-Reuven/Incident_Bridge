"""
Integration tests for POST /incidents/maintenance/{id}/close (Task 2,
step 5): closing a maintenance task from any queue position, over real
HTTP. See tests/conftest.py for the admin_client / user_client fixtures.
"""


def _create_task(client, title):
    return client.post("/incidents/maintenance", json={"title": title, "description": "d"}).json()


def _close(client, task_id, message="Not needed.", resolution_type="resolved"):
    return client.post(
        f"/incidents/maintenance/{task_id}/close",
        json={"message": message, "resolution_type": resolution_type},
    )


def _queue(client):
    return client.get("/incidents/maintenance/queue").json()


def test_closing_a_middle_pending_task_moves_the_later_ones_up(admin_client):
    a, b, c = (_create_task(admin_client, title) for title in "ABC")
    response = _close(admin_client, b["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "closed"

    pending = _queue(admin_client)["pending"]
    assert [(t["title"], t["queue_position"]) for t in pending] == [("A", 1), ("C", 2)]


def test_closing_the_first_pending_task_promotes_the_next(admin_client):
    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    _close(admin_client, a["id"])

    pending = _queue(admin_client)["pending"]
    assert [(t["title"], t["queue_position"]) for t in pending] == [("B", 1)]


def test_closing_the_current_task_leaves_the_next_one_open_until_start_next(admin_client):
    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    admin_client.post("/incidents/maintenance/start-next")

    assert _close(admin_client, a["id"]).status_code == 200

    queue = _queue(admin_client)
    assert queue["current"] is None
    assert [(t["title"], t["status"]) for t in queue["pending"]] == [("B", "open")]

    started = admin_client.post("/incidents/maintenance/start-next")
    assert started.status_code == 200
    assert started.json()["title"] == "B"


def test_close_logs_the_reason_and_resolution(admin_client):
    task = _create_task(admin_client, "A")
    _close(admin_client, task["id"], "No longer needed.")

    log = admin_client.get(f"/incidents/{task['id']}").json()["comments"][-1]
    assert log["author"] == "admin"
    assert "Status changed from Open to Closed." in log["text"]
    assert "Reason: No longer needed." in log["text"]
    assert "Resolution: Resolved." in log["text"]


def test_closing_the_in_progress_task_logs_the_old_status(admin_client):
    task = _create_task(admin_client, "A")
    admin_client.post("/incidents/maintenance/start-next")
    _close(admin_client, task["id"], "Finished.")

    log = admin_client.get(f"/incidents/{task['id']}").json()["comments"][-1]
    assert "Status changed from In progress to Closed." in log["text"]


def test_a_regular_user_can_close_a_pending_task_as_resolved(admin_client, user_client):
    task = _create_task(admin_client, "A")
    response = _close(user_client, task["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "closed"


def test_a_regular_user_cannot_use_admin_only_resolutions_and_the_task_stays_queued(admin_client, user_client):
    task = _create_task(admin_client, "A")
    response = _close(user_client, task["id"], resolution_type="not_an_incident")
    assert response.status_code == 403
    assert [t["title"] for t in _queue(admin_client)["pending"]] == ["A"]


def test_an_admin_can_close_as_not_an_incident(admin_client):
    task = _create_task(admin_client, "A")
    response = _close(admin_client, task["id"], resolution_type="not_an_incident")
    assert response.status_code == 200
    assert response.json()["resolution_type"] == "not_an_incident"


def test_a_blank_reason_is_rejected_and_the_task_stays_queued(admin_client):
    task = _create_task(admin_client, "A")
    response = _close(admin_client, task["id"], message="   ")
    assert response.status_code == 400
    assert [t["title"] for t in _queue(admin_client)["pending"]] == ["A"]


def test_closing_an_already_closed_task_returns_409(admin_client):
    task = _create_task(admin_client, "A")
    assert _close(admin_client, task["id"]).status_code == 200
    assert _close(admin_client, task["id"]).status_code == 409


def test_a_closed_task_has_no_queue_position(admin_client):
    task = _create_task(admin_client, "A")
    _close(admin_client, task["id"])
    assert admin_client.get(f"/incidents/{task['id']}").json()["queue_position"] is None


def test_faults_cannot_be_closed_through_the_maintenance_endpoint(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "F", "description": "d", "details": {}}
    ).json()
    assert _close(admin_client, fault["id"]).status_code == 400


def test_closing_an_unknown_id_returns_404(admin_client):
    assert _close(admin_client, "00000000-0000-0000-0000-000000000000").status_code == 404


def test_closing_from_the_middle_publishes_the_shifted_task_ids(admin_client, monkeypatch):
    from app.api.app import app

    events = []
    monkeypatch.setattr(
        app.state.broadcaster, "publish", lambda event, exclude_client_id=None: events.append(event)
    )
    a, b, c, d = (_create_task(admin_client, title) for title in "ABCD")
    events.clear()

    _close(admin_client, b["id"])

    assert events[-1]["action"] == "closed"
    assert events[-1]["id"] == b["id"]
    assert events[-1]["affected_ids"] == [c["id"], d["id"]]


def test_closing_the_last_task_shifts_nobody(admin_client, monkeypatch):
    from app.api.app import app

    events = []
    monkeypatch.setattr(
        app.state.broadcaster, "publish", lambda event, exclude_client_id=None: events.append(event)
    )
    a, b = (_create_task(admin_client, title) for title in "AB")
    events.clear()

    _close(admin_client, b["id"])

    assert events[-1].get("affected_ids", []) == []