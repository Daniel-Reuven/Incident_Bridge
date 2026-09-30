"""
Unit tests for app.repository.IncidentRepository.load_from_jsonl - the
seed-data loader that reads data/sample_data.jsonl-shaped files (one JSON
object per line) and converts each valid, non-duplicate record into a
real, saved Incident via MaintenanceTask.from_dict / Fault.from_dict.

No HTTP, no FastAPI involved here - this is a unit test of the repository
and loader in isolation, using a real temporary file on disk (pytest's
built-in tmp_path fixture) rather than a real data/sample_data.jsonl, so
these tests never depend on - or modify - the project's actual seed file.
"""

import json

import pytest

from app.models import Role, User
from app.repository import IncidentRepository, UserStore


@pytest.fixture
def users():
    store = UserStore()
    store.add(User(username="admin", role=Role.ADMIN, password="Passw0rd1"))
    store.add(User(username="tech1", role=Role.USER, password="Passw0rd2"))
    return store


@pytest.fixture
def repo():
    return IncidentRepository()  # no SqliteIncidentStore - memory-only, nothing touches disk


def write_jsonl(tmp_path, lines):
    """Writes each item in `lines` as its own line (dicts are JSON-encoded; strings are written as-is)."""
    path = tmp_path / "sample_data.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")
    return str(path)


def test_loads_valid_maintenance_and_fault_records(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "title": "Patch server", "description": "d", "created_by": "admin"},
        {"id": "seed-2", "kind": "fault", "title": "Payment down", "description": "d", "created_by": "tech1",
         "details": {"system_unavailable": True}},
    ])

    result = repo.load_from_jsonl(path, users)

    assert result.created == 2
    assert result.skipped_duplicate_ids == []
    assert result.skipped_invalid == []
    assert repo.get("seed-1").title == "Patch server"
    assert repo.get("seed-2").severity.value == 1  # CRITICAL, auto-scored


def test_blank_lines_are_skipped_silently(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        "",
        {"id": "seed-1", "kind": "maintenance", "title": "A", "description": "d", "created_by": "admin"},
        "   ",
    ])
    result = repo.load_from_jsonl(path, users)
    assert result.created == 1
    assert result.skipped_invalid == []


def test_duplicate_id_is_skipped_not_overwritten(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "title": "Original", "description": "d", "created_by": "admin"},
        {"id": "seed-1", "kind": "maintenance", "title": "Different title", "description": "d", "created_by": "admin"},
    ])

    result = repo.load_from_jsonl(path, users)

    assert result.created == 1
    assert result.skipped_duplicate_ids == ["seed-1"]
    assert repo.get("seed-1").title == "Original"  # the second record never overwrote the first


def test_loading_the_same_file_twice_is_idempotent(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "title": "A", "description": "d", "created_by": "admin"},
    ])
    first = repo.load_from_jsonl(path, users)
    second = repo.load_from_jsonl(path, users)
    assert first.created == 1
    assert second.created == 0
    assert second.skipped_duplicate_ids == ["seed-1"]


def test_missing_kind_is_reported_as_invalid_and_does_not_stop_the_rest_of_the_file(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "title": "No kind field", "description": "d", "created_by": "admin"},
        {"id": "seed-2", "kind": "maintenance", "title": "Still loads", "description": "d", "created_by": "admin"},
    ])

    result = repo.load_from_jsonl(path, users)

    assert result.created == 1
    assert len(result.skipped_invalid) == 1
    assert "line 1" in result.skipped_invalid[0]
    assert "kind" in result.skipped_invalid[0]


def test_missing_title_is_reported_as_invalid(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "description": "d", "created_by": "admin"},
    ])
    result = repo.load_from_jsonl(path, users)
    assert result.created == 0
    assert "title" in result.skipped_invalid[0]


def test_unknown_created_by_username_is_reported_as_invalid(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "title": "A", "description": "d", "created_by": "nobody"},
    ])
    result = repo.load_from_jsonl(path, users)
    assert result.created == 0
    assert "nobody" in result.skipped_invalid[0]


def test_unknown_assigned_to_username_is_reported_as_invalid(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "title": "A", "description": "d",
         "created_by": "admin", "assigned_to": "nobody"},
    ])
    result = repo.load_from_jsonl(path, users)
    assert result.created == 0
    assert "assigned_to" in result.skipped_invalid[0]


def test_malformed_json_line_is_reported_as_invalid_and_does_not_stop_the_rest_of_the_file(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        "{not valid json",
        {"id": "seed-2", "kind": "maintenance", "title": "Still loads", "description": "d", "created_by": "admin"},
    ])

    result = repo.load_from_jsonl(path, users)

    assert result.created == 1
    assert len(result.skipped_invalid) == 1
    assert "line 1" in result.skipped_invalid[0]


def test_valid_assigned_to_is_resolved_to_a_real_user(tmp_path, repo, users):
    path = write_jsonl(tmp_path, [
        {"id": "seed-1", "kind": "maintenance", "title": "A", "description": "d",
         "created_by": "admin", "assigned_to": "tech1"},
    ])
    repo.load_from_jsonl(path, users)
    assert repo.get("seed-1").assigned_to.username == "tech1"