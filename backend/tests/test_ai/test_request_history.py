"""
Tests for RequestHistoryStore (app/ai/request_history.py) itself - the
schema, recording a request and updating it when it ends.

Run from backend/:  python -m pytest tests/test_ai/test_request_history.py -v
"""

from datetime import datetime, timezone

import pytest

from app.ai.request_history import OUTCOMES, RequestHistoryStore


def test_a_requested_row_starts_with_no_outcome(request_history):
    requested_at = datetime.now(timezone.utc)
    request_history.record_requested("job-1", "seed-001", "tech1", requested_at)

    [row] = request_history.all_requests()
    assert row["id"] == "job-1"
    assert row["incident_id"] == "seed-001"
    assert row["requested_by"] == "tech1"
    assert row["requested_at"] == requested_at.isoformat()
    assert row["finished_at"] is None
    assert row["outcome"] is None
    assert row["provider"] is None
    assert row["model"] is None
    assert row["findings_json"] is None


def test_record_finished_fills_in_the_rest(request_history):
    request_history.record_requested("job-1", "seed-001", "tech1", datetime.now(timezone.utc))
    request_history.record_finished("job-1", "success", provider="Gemini", model="gemini-pro",
                                     findings_json='{"suggested_severity": "major"}')

    [row] = request_history.all_requests()
    assert row["outcome"] == "success"
    assert row["provider"] == "Gemini"
    assert row["model"] == "gemini-pro"
    assert row["findings_json"] == '{"suggested_severity": "major"}'
    assert row["finished_at"] is not None


def test_record_finished_rejects_an_unknown_outcome(request_history):
    request_history.record_requested("job-1", "seed-001", "tech1", datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="unknown outcome"):
        request_history.record_finished("job-1", "done")


def test_every_outcome_is_accepted(request_history):
    for i, outcome in enumerate(OUTCOMES):
        job_id = f"job-{i}"
        request_history.record_requested(job_id, "seed-001", "tech1", datetime.now(timezone.utc))
        request_history.record_finished(job_id, outcome)
    assert [row["outcome"] for row in request_history.all_requests()] == list(OUTCOMES)


def test_for_incident_filters_by_incident_id(request_history):
    request_history.record_requested("job-1", "seed-001", "tech1", datetime.now(timezone.utc))
    request_history.record_requested("job-2", "seed-002", "tech1", datetime.now(timezone.utc))

    rows = request_history.for_incident("seed-001")
    assert [row["id"] for row in rows] == ["job-1"]


def test_rows_survive_a_new_store_on_the_same_file(db_path):
    RequestHistoryStore(db_path).record_requested("job-1", "seed-001", "tech1", datetime.now(timezone.utc))
    assert [row["id"] for row in RequestHistoryStore(db_path).all_requests()] == ["job-1"]
