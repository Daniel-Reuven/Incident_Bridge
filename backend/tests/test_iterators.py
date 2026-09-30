"""Unit tests for app.iterators - the lazy urgent-fault pipeline."""

import pytest

from app.iterators import ExaminedCounter, first_urgent_faults, urgent_fault_summaries
from app.models import Fault, MaintenanceTask, Role, SeverityCategory, User


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_fault(reporter, title, severity):
    return Fault(title=title, description="d", created_by=reporter,
                 severity=severity, severity_score=float(severity.value))


@pytest.fixture
def incidents(reporter):
    closed = make_fault(reporter, "Already closed", SeverityCategory.CRITICAL)
    closed.start_progress()
    from app.models import ResolutionType
    closed.close(reporter, ResolutionType.RESOLVED, "done")
    return [
        make_fault(reporter, "Typo", SeverityCategory.MINOR),                 # 1 - filtered out (minor)
        MaintenanceTask(title="Patch", description="d", created_by=reporter),  # 2 - filtered out (not a fault)
        make_fault(reporter, "DB down", SeverityCategory.CRITICAL),            # 3 - result 1
        closed,                                                                 # 4 - filtered out (closed)
        make_fault(reporter, "Slow checkout", SeverityCategory.MAJOR),         # 5 - result 2
        make_fault(reporter, "Login broken", SeverityCategory.CRITICAL),       # 6 - never needed
        make_fault(reporter, "Footer glitch", SeverityCategory.MINOR),         # 7 - never needed
    ]


def test_pipeline_is_lazy_until_consumed(incidents):
    counter = ExaminedCounter()
    urgent_fault_summaries(incidents, counter)  # built, not started
    assert counter.count == 0


def test_only_open_critical_or_major_faults_come_out(incidents):
    results = list(urgent_fault_summaries(incidents))
    assert len(results) == 3
    assert all(r["summary"].startswith(("[CRITICAL]", "[MAJOR]")) for r in results)
    assert not any(r["title"] in ("Typo", "Patch", "Already closed") for r in results)


def test_summary_contains_severity_title_and_short_id(incidents):
    first = next(urgent_fault_summaries(incidents))
    assert first["summary"] == f"[CRITICAL] DB down ({first['id'][:8]})"
    assert first["severity"] == "CRITICAL" and first["title"] == "DB down"


def test_stopping_after_two_results_leaves_the_rest_unexamined(incidents):
    summaries, examined = first_urgent_faults(incidents, limit=2)
    assert len(summaries) == 2
    assert examined == 5          # items 6 and 7 were never pulled from the source
    assert examined < len(incidents)


def test_generator_continues_where_it_stopped(incidents):
    gen = urgent_fault_summaries(incidents)
    first = next(gen)
    rest = list(gen)
    assert first["title"] == "DB down" and len(rest) == 2


def test_exhausted_generator_cannot_be_reused_but_a_new_one_can(incidents):
    gen = urgent_fault_summaries(incidents)
    assert len(list(gen)) == 3
    assert list(gen) == []                                  # exhausted
    assert len(list(urgent_fault_summaries(incidents))) == 3  # fresh generator starts over


def test_no_matches_returns_empty(reporter):
    assert first_urgent_faults([make_fault(reporter, "x", SeverityCategory.MINOR)])[0] == []
