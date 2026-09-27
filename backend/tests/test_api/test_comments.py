"""
Integration tests for the shared comment endpoint
(POST /incidents/{id}/comments) and the general listing/detail endpoints
in app/api/incidents.py. See tests/conftest.py for the admin_client /
user_client fixtures used below.
"""


def test_any_authenticated_user_can_comment(admin_client, user_client):
    task = admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"}).json()

    response = user_client.post(f"/incidents/{task['id']}/comments", json={"text": "On it."})
    assert response.status_code == 200
    comments = response.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["text"] == "On it."
    assert comments[0]["author"] == "tech1"


def test_commenting_on_a_closed_incident_still_works(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "x", "description": "d", "details": {}}
    ).json()
    admin_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Fixed."}
    )
    response = admin_client.post(f"/incidents/{fault['id']}/comments", json={"text": "Follow-up note."})
    assert response.status_code == 200


def test_empty_comment_text_is_rejected(admin_client):
    task = admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"}).json()
    response = admin_client.post(f"/incidents/{task['id']}/comments", json={"text": "   "})
    assert response.status_code == 400


def test_getting_an_unknown_incident_returns_404(admin_client):
    response = admin_client.get("/incidents/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_list_incidents_can_be_filtered_by_type(admin_client):
    admin_client.post("/incidents/maintenance", json={"title": "Task", "description": "d"})
    admin_client.post("/incidents/faults", json={"title": "Fault", "description": "d", "details": {}})

    only_faults = admin_client.get("/incidents", params={"type": "fault"}).json()
    assert len(only_faults) == 1
    assert only_faults[0]["title"] == "Fault"


def test_list_incidents_can_be_filtered_by_status(admin_client):
    admin_client.post("/incidents/maintenance", json={"title": "Still open", "description": "d"})
    closed_fault = admin_client.post(
        "/incidents/faults", json={"title": "Will close", "description": "d", "details": {}}
    ).json()
    admin_client.post(
        f"/incidents/faults/{closed_fault['id']}/close", json={"resolution_type": "resolved", "message": "done"}
    )

    open_items = admin_client.get("/incidents", params={"status": "open"}).json()
    closed_items = admin_client.get("/incidents", params={"status": "closed"}).json()
    assert all(i["status"] == "open" for i in open_items)
    assert all(i["status"] == "closed" for i in closed_items)
    assert any(i["title"] == "Will close" for i in closed_items)
