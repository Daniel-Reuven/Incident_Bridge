"""
Tests for the optional "closed" block of a seed record
(IncidentRepository._apply_seed_closure in app/repository.py), and that the
import endpoint keeps records seeded as closed out of the live queues.

Run from backend/:  python -m pytest tests/test_seed_closed.py -v
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import IncidentStatus, ResolutionType, Role, User
from app.repository import IncidentRepository, UserStore


@pytest.fixture
def users():
    store = UserStore()
    store.add(User("admin", Role.ADMIN, "Passw0rd!1"))
    store.add(User("tech1", Role.USER, "Passw0rd!2"))
    return store


def record(kind="fault", **extra):
    data = {"id": "r-1", "kind": kind, "title": "API down", "description": "503s", "created_by": "admin"}
    if kind == "fault":
        data["details"] = {"system_unavailable": True}
    data.update(extra)
    return json.dumps(data)


def load(users, line):
    repo = IncidentRepository()
    return repo, repo.load_from_jsonl_lines([line], users)


def minutes_before_now(moment):
    return (datetime.now(timezone.utc) - moment) / timedelta(minutes=1)


@pytest.mark.parametrize("kind", ["fault", "maintenance"])
def test_a_closed_record_is_loaded_closed_with_its_resolution_and_time(users, kind):
    repo, result = load(users, record(kind, minutes_ago=600, closed={
        "by": "tech1", "resolution": "resolved", "message": "Fixed the config.", "minutes_ago": 60}))
    incident = repo.get("r-1")
    assert result.created == 1
    assert incident.status == IncidentStatus.CLOSED
    assert incident.resolution_type == ResolutionType.RESOLVED
    assert incident.resolution_message == "Fixed the config."
    log = incident.comments[-1]
    assert log.author.username == "tech1" and "Status changed from Open to Closed" in log.text
    assert 59 < minutes_before_now(log.created_at) < 61
    assert incident.updated_at == log.created_at


def test_without_minutes_ago_it_is_closed_at_import_time(users):
    repo, _ = load(users, record(closed={"by": "admin", "resolution": "by_design", "message": "Works as designed."}))
    assert minutes_before_now(repo.get("r-1").updated_at) < 1


@pytest.mark.parametrize("closed, reason", [
    ("yes", "'closed' must be an object"),
    ({"by": "ghost", "resolution": "resolved", "message": "m"}, "closed 'by' 'ghost' is not an account here"),
    ({"by": "admin", "resolution": "done", "message": "m"}, "closed 'resolution' must be one of"),
    ({"by": "admin", "resolution": "resolved"}, "closed needs a 'message' string"),
    ({"by": "admin", "resolution": "resolved", "message": "   "}, "A closing message is required"),
    ({"by": "tech1", "resolution": "not_an_incident", "message": "m"}, "closed: Only an admin"),
    ({"by": "admin", "resolution": "resolved", "message": "m", "minutes_ago": -5}, "closed 'minutes_ago' must be"),
    ({"by": "admin", "resolution": "resolved", "message": "m", "minutes_ago": 700}, "older than the incident"),
    ({"by": "admin", "resolution": "resolved", "message": "m", "closed_at": "2000-01-01T00:00:00Z"},
     "older than the incident"),
    ({"by": "admin", "resolution": "resolved", "message": "m", "closed_at": "2999-01-01T00:00:00Z"},
     "is in the future"),
])
def test_an_invalid_closed_block_skips_the_record_with_a_reason(users, closed, reason):
    repo, result = load(users, record(minutes_ago=600, closed=closed))
    assert result.created == 0 and reason in result.skipped_invalid[0]


def test_import_keeps_closed_records_out_of_the_queues(admin_client):
    closed = {"by": "admin", "resolution": "resolved", "message": "Done."}
    content = "\n".join([
        record("maintenance", id="m-open"), record("maintenance", id="m-closed", closed=closed),
        record("fault", id="f-open"), record("fault", id="f-closed", closed=closed),
    ])
    assert admin_client.post("/incidents/import-jsonl", json={"content": content}).json()["created"] == 4
    pending = [t["id"] for t in admin_client.get("/incidents/maintenance/queue").json()["pending"]]
    faults = [f["id"] for f in admin_client.get("/incidents/faults/queue").json()]
    assert "m-open" in pending and "m-closed" not in pending
    assert "f-open" in faults and "f-closed" not in faults
    assert admin_client.get("/incidents/f-closed").json()["status"] == "closed"


def test_the_bundled_sample_data_has_open_and_closed_incidents_of_both_kinds(users):
    path = Path(__file__).resolve().parents[2] / "data" / "sample_data.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kinds = {"maintenance": [r for r in records if r["kind"] == "maintenance"],
             "fault": [r for r in records if r["kind"] == "fault"]}
    assert 10 <= len(kinds["maintenance"]) <= 15 and len(kinds["fault"]) == 20
    for group in kinds.values():
        assert any("closed" in r for r in group) and any("closed" not in r for r in group)
