"""
Integration tests for POST /incidents/maintenance/{id}/reopen and
GET /incidents/maintenance/reopen-options (Task 2, step 6), over real HTTP.
See tests/conftest.py for the admin_client / user_client fixtures.
"""


def _create_task(client, title):
    return client.post("/incidents/maintenance", json={"title": title, "description": "d"}).json()


def _close(client, task_id, message="Fixed."):
    return client.post(
        f"/incidents/maintenance/{task_id}/close", json={"message": message, "resolution_type": "resolved"}
    )


def _reopen(client, task_id, status="open", reason="Regression found", position=None):
    body = {"status": status, "reason": reason}
    if position is not None:
        body["position"] = position
    return client.post(f"/incidents/maintenance/{task_id}/reopen", json=body)


def _queue(client):
    return client.get("/incidents/maintenance/queue").json()


def _layout(client):
    queue = _queue(client)
    current = [(queue["current"]["title"], queue["current"]["queue_position"])] if queue["current"] else []
    return current + [(t["title"], t["queue_position"]) for t in queue["pending"]]


def test_reopen_as_open_goes_to_the_back_when_no_position_is_given(admin_client):
    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    _close(admin_client, a["id"])

    response = _reopen(admin_client, a["id"])
    assert response.status_code == 200
    assert _layout(admin_client) == [("B", 1), ("A", 2)]


def test_reopen_at_the_start(admin_client):
    _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")
    _close(admin_client, b["id"])

    _reopen(admin_client, b["id"], position=1)
    assert _layout(admin_client) == [("B", 1), ("A", 2)]


def test_reopen_at_a_specific_middle_position(admin_client):
    for title in "ABC":
        _create_task(admin_client, title)
    d = _create_task(admin_client, "D")
    _close(admin_client, d["id"])

    _reopen(admin_client, d["id"], position=2)
    assert _layout(admin_client) == [("A", 1), ("D", 2), ("B", 3), ("C", 4)]


def test_reopen_as_in_progress_at_position_one(admin_client):
    _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")
    _close(admin_client, b["id"])

    response = _reopen(admin_client, b["id"], status="in_progress", position=1)
    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    queue = _queue(admin_client)
    assert queue["current"]["title"] == "B"
    assert [(t["title"], t["queue_position"]) for t in queue["pending"]] == [("A", 2)]


def test_in_progress_is_rejected_while_another_task_is_in_progress_with_409(admin_client):
    a = _create_task(admin_client, "A")
    c = _create_task(admin_client, "C")
    _close(admin_client, c["id"])
    admin_client.post(f"/incidents/maintenance/{a['id']}/start")

    response = _reopen(admin_client, c["id"], status="in_progress", position=1)
    assert response.status_code == 409
    assert admin_client.get(f"/incidents/{c['id']}").json()["status"] == "closed"


def test_in_progress_at_a_position_other_than_one_is_rejected_with_400(admin_client):
    _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    c = _create_task(admin_client, "C")
    _close(admin_client, c["id"])

    response = _reopen(admin_client, c["id"], status="in_progress", position=2)
    assert response.status_code == 400
    assert admin_client.get(f"/incidents/{c['id']}").json()["status"] == "closed"


def test_a_position_out_of_range_is_rejected_with_400(admin_client):
    _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")
    _close(admin_client, b["id"])

    for bad_position in (0, 99):
        response = _reopen(admin_client, b["id"], position=bad_position)
        assert response.status_code == 400
    assert admin_client.get(f"/incidents/{b['id']}").json()["status"] == "closed"


def test_position_one_is_not_available_for_open_while_a_task_is_in_progress(admin_client):
    a = _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")
    admin_client.post(f"/incidents/maintenance/{a['id']}/start")
    _close(admin_client, b["id"])

    response = _reopen(admin_client, b["id"], position=1)
    assert response.status_code == 400
    assert "between 2 and 2" in response.json()["detail"]


def test_reopening_into_closed_is_rejected_with_400(admin_client):
    a = _create_task(admin_client, "A")
    _close(admin_client, a["id"])
    assert _reopen(admin_client, a["id"], status="closed").status_code == 400


def test_a_regular_user_cannot_reopen_a_task(admin_client, user_client):
    a = _create_task(admin_client, "A")
    _close(admin_client, a["id"])

    response = _reopen(user_client, a["id"])
    assert response.status_code == 403
    assert admin_client.get(f"/incidents/{a['id']}").json()["status"] == "closed"
    assert _layout(admin_client) == []


def test_a_blank_reason_is_rejected_with_400(admin_client):
    a = _create_task(admin_client, "A")
    _close(admin_client, a["id"])
    assert _reopen(admin_client, a["id"], reason="   ").status_code == 400
    assert admin_client.get(f"/incidents/{a['id']}").json()["status"] == "closed"


def test_reopening_a_task_that_is_not_closed_returns_409(admin_client):
    a = _create_task(admin_client, "A")
    assert _reopen(admin_client, a["id"]).status_code == 409
    assert _layout(admin_client) == [("A", 1)]  # untouched, not duplicated


def test_reopen_logs_the_reason_previous_resolution_and_position(admin_client):
    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    _close(admin_client, a["id"], "Fixed.")
    _reopen(admin_client, a["id"], reason="Regression found")

    log = admin_client.get(f"/incidents/{a['id']}").json()["comments"][-1]
    assert log["author"] == "admin"
    assert "Status changed from Closed to Open." in log["text"]
    assert "Reason: Regression found." in log["text"]
    assert "Previous resolution: Resolved - Fixed." in log["text"]
    assert "Queue position: 2." in log["text"]


def test_reopen_clears_the_resolution(admin_client):
    a = _create_task(admin_client, "A")
    _close(admin_client, a["id"])
    body = _reopen(admin_client, a["id"]).json()
    assert body["status"] == "open"
    assert body["resolution_type"] is None
    assert body["resolution_message"] is None


def test_faults_cannot_be_reopened_through_the_maintenance_endpoint(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "F", "description": "d", "details": {}}
    ).json()
    assert _reopen(admin_client, fault["id"]).status_code == 400


def test_reopening_an_unknown_id_returns_404(admin_client):
    assert _reopen(admin_client, "00000000-0000-0000-0000-000000000000").status_code == 404


def test_reopen_options_describe_the_queue_as_it_changes(admin_client):
    assert admin_client.get("/incidents/maintenance/reopen-options").json() == {
        "has_current": False, "first_position": 1, "last_position": 1, "can_start": True, "queue_empty": True,
    }

    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    assert admin_client.get("/incidents/maintenance/reopen-options").json() == {
        "has_current": False, "first_position": 1, "last_position": 3, "can_start": True, "queue_empty": False,
    }

    admin_client.post(f"/incidents/maintenance/{a['id']}/start")
    assert admin_client.get("/incidents/maintenance/reopen-options").json() == {
        "has_current": True, "first_position": 2, "last_position": 3, "can_start": False, "queue_empty": False,
    }


def test_reopening_publishes_the_ids_of_the_tasks_that_moved_down(admin_client, monkeypatch):
    from app.api.app import app

    events = []
    monkeypatch.setattr(
        app.state.broadcaster, "publish", lambda event, exclude_client_id=None: events.append(event)
    )
    a, b, c, d = (_create_task(admin_client, title) for title in "ABCD")
    _close(admin_client, c["id"])
    events.clear()

    _reopen(admin_client, c["id"], position=1)

    assert events[-1]["action"] == "reopened"
    assert events[-1]["id"] == c["id"]
    assert events[-1]["affected_ids"] == [a["id"], b["id"], d["id"]]


def test_a_task_reopened_at_the_front_is_the_one_start_next_starts(admin_client):
    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")
    _close(admin_client, a["id"])
    _reopen(admin_client, a["id"], position=1)

    started = admin_client.post("/incidents/maintenance/start-next")
    assert started.status_code == 200
    assert started.json()["title"] == "A"