"""
Integration tests for the status-change log comments added in Task 2,
step 7 (the dashboard's Start next and Claim next), plus end-to-end
comment-trail checks across the whole lifecycle of each incident type.
See tests/conftest.py for the admin_client / user_client fixtures.
"""


def test_start_next_logs_a_status_change_comment(user_client):
    task = user_client.post("/incidents/maintenance", json={"title": "A", "description": "d"}).json()
    user_client.post("/incidents/maintenance/start-next")

    log = user_client.get(f"/incidents/{task['id']}").json()["comments"][-1]
    assert log["author"] == "tech1"
    assert "Status changed from Open to In progress." in log["text"]


def test_claim_next_logs_a_status_change_comment_with_the_assignee(user_client):
    fault = user_client.post(
        "/incidents/faults", json={"title": "F", "description": "d", "details": {"system_unavailable": True}}
    ).json()
    user_client.post("/incidents/faults/claim-next")

    log = user_client.get(f"/incidents/{fault['id']}").json()["comments"][-1]
    assert log["author"] == "tech1"
    assert "Status changed from Open to In progress." in log["text"]
    assert "Assigned to tech1." in log["text"]


def test_a_faults_comment_trail_records_every_status_change_in_order(admin_client):
    fault = admin_client.post(
        "/incidents/faults", json={"title": "F", "description": "d", "details": {}}
    ).json()
    admin_client.post("/incidents/faults/claim-next")
    admin_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Fixed."}
    )
    admin_client.post(
        f"/incidents/faults/{fault['id']}/reopen", json={"status": "open", "reason": "Came back"}
    )

    comments = admin_client.get(f"/incidents/{fault['id']}").json()["comments"]
    assert len(comments) == 3
    assert "Open to In progress" in comments[0]["text"]
    assert "In progress to Closed" in comments[1]["text"]
    assert "Closed to Open" in comments[2]["text"]
    assert all(c["author"] == "admin" for c in comments)


def test_a_maintenance_tasks_comment_trail_records_every_status_change_in_order(admin_client):
    task = admin_client.post("/incidents/maintenance", json={"title": "A", "description": "d"}).json()
    admin_client.post("/incidents/maintenance/start-next")
    admin_client.post(
        "/incidents/maintenance/complete-current", json={"message": "Done.", "resolution_type": "resolved"}
    )
    admin_client.post(
        f"/incidents/maintenance/{task['id']}/reopen", json={"status": "open", "reason": "Needs redoing"}
    )

    comments = admin_client.get(f"/incidents/{task['id']}").json()["comments"]
    assert len(comments) == 3
    assert "Open to In progress" in comments[0]["text"]
    assert "In progress to Closed" in comments[1]["text"]
    assert "Closed to Open" in comments[2]["text"]