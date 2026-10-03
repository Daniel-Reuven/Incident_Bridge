"""
Integration tests for POST /incidents/maintenance/{id}/start (Task 2, step 5
patch): the Start button on the first maintenance task's own page, over real
HTTP. See tests/conftest.py for the admin_client / user_client fixtures.
"""


def _create_task(client, title):
    return client.post("/incidents/maintenance", json={"title": title, "description": "d"}).json()


def _start(client, task_id):
    return client.post(f"/incidents/maintenance/{task_id}/start")


def _queue(client):
    return client.get("/incidents/maintenance/queue").json()


def test_the_first_task_can_be_started_from_its_own_id(admin_client):
    a = _create_task(admin_client, "A")
    _create_task(admin_client, "B")

    response = _start(admin_client, a["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "in_progress"
    queue = _queue(admin_client)
    assert queue["current"]["id"] == a["id"]
    assert [t["title"] for t in queue["pending"]] == ["B"]


def test_starting_logs_a_status_change_comment(admin_client):
    a = _create_task(admin_client, "A")
    _start(admin_client, a["id"])
    log = admin_client.get(f"/incidents/{a['id']}").json()["comments"][-1]
    assert log["author"] == "admin"
    assert "Status changed from Open to In progress." in log["text"]


def test_a_task_that_is_not_first_cannot_be_started(admin_client):
    _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")

    response = _start(admin_client, b["id"])
    assert response.status_code == 409
    assert "first task in the queue" in response.json()["detail"]
    queue = _queue(admin_client)
    assert queue["current"] is None
    assert [t["title"] for t in queue["pending"]] == ["A", "B"]


def test_starting_while_another_task_is_in_progress_returns_409(admin_client):
    a = _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")
    _start(admin_client, a["id"])
    assert _start(admin_client, b["id"]).status_code == 409


def test_starting_the_same_task_twice_returns_409(admin_client):
    a = _create_task(admin_client, "A")
    assert _start(admin_client, a["id"]).status_code == 200
    assert _start(admin_client, a["id"]).status_code == 409


def test_starting_a_closed_task_returns_409(admin_client):
    a = _create_task(admin_client, "A")
    admin_client.post(
        f"/incidents/maintenance/{a['id']}/close", json={"message": "Not needed.", "resolution_type": "resolved"}
    )
    assert _start(admin_client, a["id"]).status_code == 409


def test_a_regular_user_can_start_the_first_task(admin_client, user_client):
    a = _create_task(admin_client, "A")
    assert _start(user_client, a["id"]).status_code == 200


def test_a_new_first_task_can_be_started_after_the_old_first_is_closed(admin_client):
    a = _create_task(admin_client, "A")
    b = _create_task(admin_client, "B")
    admin_client.post(
        f"/incidents/maintenance/{a['id']}/close", json={"message": "Not needed.", "resolution_type": "resolved"}
    )
    assert _start(admin_client, b["id"]).status_code == 200


def test_faults_cannot_be_started_this_way(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "F", "description": "d", "details": {}}
    ).json()
    assert _start(admin_client, fault["id"]).status_code == 400


def test_starting_an_unknown_id_returns_404(admin_client):
    assert _start(admin_client, "00000000-0000-0000-0000-000000000000").status_code == 404


def test_starting_publishes_a_started_event(admin_client, monkeypatch):
    from app.api.app import app

    events = []
    monkeypatch.setattr(
        app.state.broadcaster, "publish", lambda event, exclude_client_id=None: events.append(event)
    )
    a = _create_task(admin_client, "A")
    events.clear()

    _start(admin_client, a["id"])

    assert events[-1]["action"] == "started"
    assert events[-1]["id"] == a["id"]