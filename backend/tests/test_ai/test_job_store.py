"""
Tests for the persisted queue (app/ai/job_store.py).

Run from backend/:  python -m pytest tests/test_ai/test_job_store.py -v
"""

from datetime import datetime, timedelta, timezone

from app.ai.job_store import SqliteAnalysisJobStore
from app.ai.jobs import AnalysisJob


def test_jobs_come_back_oldest_first_after_reopening_the_file(tmp_path):
    path = str(tmp_path / "jobs.db")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = SqliteAnalysisJobStore(path)
    for minutes, incident_id in [(2, "c"), (0, "a"), (1, "b")]:
        store.add(AnalysisJob(incident_id=incident_id, requested_by="tech1",
                              requested_at=start + timedelta(minutes=minutes)))

    reopened = SqliteAnalysisJobStore(path)
    assert [job.incident_id for job in reopened.load_all()] == ["a", "b", "c"]


def test_removing_a_job_and_removing_it_twice(tmp_path):
    store = SqliteAnalysisJobStore(str(tmp_path / "jobs.db"))
    job = AnalysisJob(incident_id="a", requested_by="tech1")
    store.add(job)
    store.remove(job.id)
    store.remove(job.id)                      # not an error
    assert store.load_all() == []
