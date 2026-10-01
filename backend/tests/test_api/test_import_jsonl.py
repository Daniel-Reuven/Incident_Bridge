"""
Integration tests for POST /incidents/import-jsonl - the admin-only bulk
import endpoint that runs IncidentRepository.load_from_jsonl_lines()
inside the live app and then enqueues + broadcasts each newly-created
incident, so it appears without a server restart. See
tests/test_repository.py for the loader's own unit tests (duplicate/
invalid-record handling); these tests focus on what's specific to going
through the live HTTP endpoint - permissions, and that imported incidents
actually land in the real, live queues.
"""


def test_import_is_admin_only(user_client):
    response = user_client.post("/incidents/import-jsonl", json={"content": ""})
    assert response.status_code == 403


def test_import_creates_incidents_and_they_are_immediately_queryable(admin_client):
    content = (
        '{"id": "seed-1", "kind": "maintenance", "title": "Patch server", '
        '"description": "d", "created_by": "admin"}\n'
        '{"id": "seed-2", "kind": "fault", "title": "Payment down", '
        '"description": "d", "created_by": "admin", "details": {"system_unavailable": true}}\n'
    )
    response = admin_client.post("/incidents/import-jsonl", json={"content": content})
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert set(body["created_ids"]) == {"seed-1", "seed-2"}
    assert body["skipped_duplicate_ids"] == []
    assert body["skipped_invalid"] == []


def test_imported_maintenance_task_lands_in_the_live_maintenance_queue(admin_client):
    """
    This is the behavior the live-update fix is actually about: no
    restart, no separate AppState - the incident is enqueued into the
    SAME running app's queue this same request handles, so the very next
    GET sees it.
    """
    content = '{"id": "seed-1", "kind": "maintenance", "title": "Patch server", "description": "d", "created_by": "admin"}\n'
    admin_client.post("/incidents/import-jsonl", json={"content": content})

    queue = admin_client.get("/incidents/maintenance/queue").json()
    assert any(t["id"] == "seed-1" for t in queue["pending"])


def test_imported_fault_lands_in_the_live_priority_queue_at_the_right_position(admin_client):
    content = (
        '{"id": "seed-minor", "kind": "fault", "title": "Typo", "description": "d", '
        '"created_by": "admin", "details": {"cosmetic_only": true}}\n'
        '{"id": "seed-critical", "kind": "fault", "title": "Outage", "description": "d", '
        '"created_by": "admin", "details": {"system_unavailable": true}}\n'
    )
    admin_client.post("/incidents/import-jsonl", json={"content": content})

    queue = admin_client.get("/incidents/faults/queue").json()
    assert queue[0]["id"] == "seed-critical"  # Critical pops first, regardless of line order


def test_duplicate_and_invalid_records_are_reported_and_not_queued(admin_client):
    content = (
        '{"id": "seed-1", "kind": "maintenance", "title": "First", "description": "d", "created_by": "admin"}\n'
        '{"id": "seed-1", "kind": "maintenance", "title": "Duplicate", "description": "d", "created_by": "admin"}\n'
        '{"id": "seed-2", "kind": "maintenance", "title": "Missing description", "created_by": "admin"}\n'
    )
    response = admin_client.post("/incidents/import-jsonl", json={"content": content})
    body = response.json()

    assert body["created"] == 1
    assert body["skipped_duplicate_ids"] == ["seed-1"]
    assert len(body["skipped_invalid"]) == 1

    queue = admin_client.get("/incidents/maintenance/queue").json()
    assert len(queue["pending"]) == 1  # the duplicate and the invalid record never got queued