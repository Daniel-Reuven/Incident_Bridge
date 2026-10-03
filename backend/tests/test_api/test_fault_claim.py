"""
Integration tests for claiming a specific fault from its incident page
(Task 2, step 4): POST /incidents/faults/{id}/claim and
GET /incidents/faults/{id}/claim-status, over real HTTP. See
tests/conftest.py for the admin_client / user_client fixtures.
"""

CRITICAL = {"system_unavailable": True}
MAJOR = {"performance_degraded": True}


def _create_fault(client, title, details=None):
    return client.post(
        "/incidents/faults", json={"title": title, "description": "d", "details": details or {}}
    ).json()


def _claim(client, fault_id):
    return client.post(f"/incidents/faults/{fault_id}/claim")


def _status(client, fault_id):
    return client.get(f"/incidents/faults/{fault_id}/claim-status")


def test_a_top_severity_fault_can_be_claimed_even_if_it_was_not_first_to_arrive(user_client):
    first = _create_fault(user_client, "Crit 1", CRITICAL)
    second = _create_fault(user_client, "Crit 2", CRITICAL)
    _create_fault(user_client, "Maj", MAJOR)

    response = _claim(user_client, second["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["assigned_to"] == "tech1"

    queue = user_client.get("/incidents/faults/queue").json()
    assert [f["title"] for f in queue] == ["Crit 1", "Maj"]
    assert first["id"] == queue[0]["id"]


def test_claiming_a_lower_severity_fault_is_rejected_and_reports_the_count(user_client):
    _create_fault(user_client, "Crit 1", CRITICAL)
    _create_fault(user_client, "Crit 2", CRITICAL)
    major = _create_fault(user_client, "Maj", MAJOR)

    response = _claim(user_client, major["id"])
    assert response.status_code == 409
    assert "2 higher-priority faults are still unclaimed" in response.json()["detail"]
    # The rejected claim changed nothing: still queued, still open, unassigned.
    assert len(user_client.get("/incidents/faults/queue").json()) == 3
    detail = user_client.get(f"/incidents/{major['id']}").json()
    assert detail["status"] == "open"
    assert detail["assigned_to"] is None


def test_claim_status_reports_the_number_of_higher_priority_faults(user_client):
    critical = _create_fault(user_client, "Crit", CRITICAL)
    _create_fault(user_client, "Crit 2", CRITICAL)
    major = _create_fault(user_client, "Maj", MAJOR)

    for_major = _status(user_client, major["id"]).json()
    assert for_major == {"queued": True, "claimable": False, "higher_priority_unclaimed": 2}

    for_critical = _status(user_client, critical["id"]).json()
    assert for_critical == {"queued": True, "claimable": True, "higher_priority_unclaimed": 0}


def test_claim_status_count_drops_as_higher_priority_faults_are_claimed(user_client):
    crit_a = _create_fault(user_client, "Crit A", CRITICAL)
    crit_b = _create_fault(user_client, "Crit B", CRITICAL)
    major = _create_fault(user_client, "Maj", MAJOR)

    _claim(user_client, crit_a["id"])
    assert _status(user_client, major["id"]).json()["higher_priority_unclaimed"] == 1

    _claim(user_client, crit_b["id"])
    after = _status(user_client, major["id"]).json()
    assert after["claimable"] is True
    assert after["higher_priority_unclaimed"] == 0


def test_a_new_higher_priority_fault_makes_a_waiting_fault_unclaimable(user_client):
    major = _create_fault(user_client, "Maj", MAJOR)
    assert _status(user_client, major["id"]).json()["claimable"] is True

    _create_fault(user_client, "New critical", CRITICAL)
    after = _status(user_client, major["id"]).json()
    assert after["claimable"] is False
    assert after["higher_priority_unclaimed"] == 1


def test_closing_the_higher_priority_fault_makes_the_lower_one_claimable(user_client):
    critical = _create_fault(user_client, "Crit", CRITICAL)
    major = _create_fault(user_client, "Maj", MAJOR)
    assert _claim(user_client, major["id"]).status_code == 409

    user_client.post(
        f"/incidents/faults/{critical['id']}/close", json={"resolution_type": "resolved", "message": "Dup."}
    )
    assert _claim(user_client, major["id"]).status_code == 200


def test_claim_status_of_a_fault_that_is_already_claimed(user_client):
    fault = _create_fault(user_client, "Crit", CRITICAL)
    _claim(user_client, fault["id"])
    assert _status(user_client, fault["id"]).json() == {
        "queued": False, "claimable": False, "higher_priority_unclaimed": 0,
    }


def test_claim_status_of_a_closed_fault(user_client):
    fault = _create_fault(user_client, "Crit", CRITICAL)
    user_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Dup."}
    )
    assert _status(user_client, fault["id"]).json() == {
        "queued": False, "claimable": False, "higher_priority_unclaimed": 0,
    }


def test_claiming_an_already_claimed_fault_returns_409(user_client):
    fault = _create_fault(user_client, "Crit", CRITICAL)
    assert _claim(user_client, fault["id"]).status_code == 200
    assert _claim(user_client, fault["id"]).status_code == 409


def test_claiming_a_closed_fault_returns_409(user_client):
    fault = _create_fault(user_client, "Crit", CRITICAL)
    user_client.post(
        f"/incidents/faults/{fault['id']}/close", json={"resolution_type": "resolved", "message": "Dup."}
    )
    assert _claim(user_client, fault["id"]).status_code == 409


def test_claim_logs_a_status_change_comment(user_client):
    fault = _create_fault(user_client, "Crit", CRITICAL)
    _claim(user_client, fault["id"])
    log = user_client.get(f"/incidents/{fault['id']}").json()["comments"][-1]
    assert log["author"] == "tech1"
    assert "Status changed from Open to In progress." in log["text"]
    assert "Assigned to tech1." in log["text"]


def test_fault_claim_endpoints_reject_maintenance_tasks(user_client):
    task = user_client.post("/incidents/maintenance", json={"title": "A", "description": "d"}).json()
    assert _claim(user_client, task["id"]).status_code == 400
    assert _status(user_client, task["id"]).status_code == 400


def test_fault_claim_endpoints_return_404_for_unknown_ids(user_client):
    unknown = "00000000-0000-0000-0000-000000000000"
    assert _claim(user_client, unknown).status_code == 404
    assert _status(user_client, unknown).status_code == 404