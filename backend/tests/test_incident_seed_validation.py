"""
Tests for the hardened incident JSONL import (IncidentRepository in
app/repository.py, Incident/Fault/MaintenanceTask.from_dict, and
SeverityScorer.validate_details): every kind of bad record is skipped with a
reason - none of them crashes the import.

Run from backend/:  python -m pytest tests/test_incident_seed_validation.py -v
"""

import json

import pytest

from app.models import Role, User
from app.repository import IncidentRepository, UserStore
from app.services import SeverityScorer


@pytest.fixture
def users():
    store = UserStore()
    store.add(User("admin", Role.ADMIN, "Passw0rd1"))
    return store


def fault(**overrides):
    record = {"id": "f-1", "kind": "fault", "title": "API down", "description": "503s", "created_by": "admin",
              "details": {"system_unavailable": True}}
    record.update(overrides)
    return json.dumps(record)


@pytest.mark.parametrize("line, reason", [
    (fault(details={"affected_users_percent": "lots"}), "0 to 100"),
    (fault(details={"affected_users_percent": 150}), "0 to 100"),
    (fault(details={"affected_users_percent": True}), "0 to 100"),
    (fault(details={"system_unavailable": "yes"}), "true or false"),
    (fault(details={"system_unavailble": True}), "unknown 'details' field"),
    (fault(details=[1, 2]), "JSON object"),
    (fault(id=None), "'id'"),
    (fault(id="   "), "'id'"),
    (fault(title=42), "title"),
    (fault(description="x" * 10_001), "description"),
    (json.dumps([1, 2]), "JSON object"),
    (json.dumps("just a string"), "JSON object"),
])
def test_bad_records_are_skipped_with_a_reason_not_a_crash(users, line, reason):
    result = IncidentRepository().load_from_jsonl_lines([line], users)
    assert result.created == 0
    assert len(result.skipped_invalid) == 1 and reason in result.skipped_invalid[0]


def test_a_record_without_id_is_rejected(users):
    record = json.loads(fault())
    del record["id"]
    result = IncidentRepository().load_from_jsonl_lines([json.dumps(record)], users)
    assert "missing a valid 'id'" in result.skipped_invalid[0]


def test_good_records_around_bad_ones_still_load(users):
    lines = [fault(id="a"), fault(id="b", details={"affected_users_percent": "lots"}), fault(id="c")]
    result = IncidentRepository().load_from_jsonl_lines(lines, users)
    assert result.created_ids == ["a", "c"] and len(result.skipped_invalid) == 1


def test_numeric_ids_are_accepted_as_text(users):
    result = IncidentRepository().load_from_jsonl_lines([fault(id=101)], users)
    assert result.created_ids == ["101"]


def test_the_bundled_sample_data_has_valid_details_and_ids():
    """Every record in data/sample_data_1.jsonl passes the stricter field checks (users aside)."""
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "data" / "sample_data_1.jsonl"
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                assert record.get("id"), record
                SeverityScorer.validate_details(record.get("details"))


# --- SeverityScorer.validate_details directly ------------------------------------

def test_valid_details_pass_unchanged():
    details = {"system_unavailable": False, "affected_users_percent": 25.5, "cosmetic_only": True}
    assert SeverityScorer.validate_details(details) is details
    assert SeverityScorer.validate_details(None) == {}


def test_score_rejects_bad_details_instead_of_crashing():
    with pytest.raises(ValueError, match="0 to 100"):
        SeverityScorer.score({"affected_users_percent": "lots"})
