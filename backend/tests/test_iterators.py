"""Unit tests for app.iterators - the lazy pressing-maintenance-calls pipeline."""

from datetime import datetime, timedelta, timezone

import pytest

from app.iterators import ExaminedCounter, first_pressing_calls, pressing_maintenance_calls
from app.models import Fault, MaintenanceTask, ResolutionType, Role, SeverityCategory, User

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_task(reporter, title, days_old):
    task = MaintenanceTask(title=title, description="d", created_by=reporter)
    task.created_at = NOW - timedelta(days=days_old)
    return task


@pytest.fixture
def incidents(reporter):
    in_progress = make_task(reporter, "Being worked on", 6)
    in_progress.start_progress()
    closed = make_task(reporter, "Already closed", 7)
    closed.close(reporter, ResolutionType.RESOLVED, "done")
    old_fault = Fault(title="Old fault", description="d", created_by=reporter,
                      severity=SeverityCategory.CRITICAL, severity_score=1.0)
    old_fault.created_at = NOW - timedelta(days=8)
    return [
        make_task(reporter, "Fresh", 1),            # 1 - filtered out (too recent)
        in_progress,                                 # 2 - filtered out (in progress)
        make_task(reporter, "Old open A", 5),       # 3 - result 1
        old_fault,                                   # 4 - filtered out (not a maintenance call)
        make_task(reporter, "Exactly 3 days", 3),   # 5 - filtered out (not OVER 3 days)
        closed,                                      # 6 - filtered out (closed)
        make_task(reporter, "Old open B", 4),       # 7 - result 2
        make_task(reporter, "Oldest open", 10),     # 8 - never needed when limit=2
        make_task(reporter, "Fresh 2", 0),          # 9 - never needed when limit=2
    ]


def test_pipeline_is_lazy_until_consumed(incidents):
    counter = ExaminedCounter()
    pressing_maintenance_calls(incidents, counter, NOW)  # built, not started
    assert counter.count == 0


def test_only_open_calls_older_than_three_days_come_out(incidents):
    results = list(pressing_maintenance_calls(incidents, now=NOW))
    assert [r["title"] for r in results] == ["Old open A", "Old open B", "Oldest open"]


def test_record_contains_id_summary_and_days_open(incidents):
    first = next(pressing_maintenance_calls(incidents, now=NOW))
    assert first["days_open"] == 5
    assert first["created_by"] == "reporter"
    assert first["summary"] == f"Old open A - open for 5 days ({first['id'][:8]})"


def test_stopping_after_two_results_leaves_the_rest_unexamined(incidents):
    calls, examined = first_pressing_calls(incidents, limit=2, now=NOW)
    assert len(calls) == 2
    assert examined == 7          # items 8 and 9 were never pulled from the source
    assert examined < len(incidents)


def test_no_limit_consumes_everything(incidents):
    calls, examined = first_pressing_calls(incidents, now=NOW)
    assert len(calls) == 3 and examined == len(incidents)


def test_generator_continues_where_it_stopped(incidents):
    gen = pressing_maintenance_calls(incidents, now=NOW)
    first = next(gen)
    rest = list(gen)
    assert first["title"] == "Old open A" and len(rest) == 2


def test_exhausted_generator_cannot_be_reused_but_a_new_one_can(incidents):
    gen = pressing_maintenance_calls(incidents, now=NOW)
    assert len(list(gen)) == 3
    assert list(gen) == []                                                  # exhausted
    assert len(list(pressing_maintenance_calls(incidents, now=NOW))) == 3   # fresh generator starts over


def test_no_matches_returns_empty(reporter):
    assert first_pressing_calls([make_task(reporter, "Fresh", 1)], now=NOW)[0] == []
