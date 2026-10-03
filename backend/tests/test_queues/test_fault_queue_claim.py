"""
Unit tests for the claim-eligibility helpers FaultPriorityQueue gained in
Task 2, step 4: __contains__, higher_priority_count() and
ensure_claimable(). The queue's ordering behavior itself is covered in
test_fault_queue.py.
"""

import pytest

from app.models.enums import Role, SeverityCategory
from app.models.fault import Fault
from app.models.user import User
from app.queues.fault_queue import FaultPriorityQueue


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_fault(reporter, title, severity):
    return Fault(
        title=title, description="desc", created_by=reporter,
        severity=severity, severity_score=float(severity.value), details={},
    )


def test_contains_is_true_only_while_the_fault_is_queued(reporter):
    queue = FaultPriorityQueue()
    fault = make_fault(reporter, "x", SeverityCategory.MAJOR)
    assert fault not in queue
    queue.push(fault)
    assert fault in queue
    queue.remove(fault)
    assert fault not in queue


def test_higher_priority_count_only_counts_strictly_more_severe_faults(reporter):
    queue = FaultPriorityQueue()
    critical_1 = make_fault(reporter, "C1", SeverityCategory.CRITICAL)
    critical_2 = make_fault(reporter, "C2", SeverityCategory.CRITICAL)
    major = make_fault(reporter, "Maj", SeverityCategory.MAJOR)
    minor = make_fault(reporter, "Min", SeverityCategory.MINOR)
    for fault in (critical_1, critical_2, major, minor):
        queue.push(fault)

    assert queue.higher_priority_count(critical_1) == 0
    assert queue.higher_priority_count(critical_2) == 0
    assert queue.higher_priority_count(major) == 2
    assert queue.higher_priority_count(minor) == 3


def test_faults_of_equal_severity_do_not_block_each_other(reporter):
    queue = FaultPriorityQueue()
    first = make_fault(reporter, "First", SeverityCategory.MAJOR)
    second = make_fault(reporter, "Second", SeverityCategory.MAJOR)
    queue.push(first)
    queue.push(second)
    assert queue.higher_priority_count(second) == 0


def test_any_fault_in_the_top_severity_group_is_claimable_not_just_the_first(reporter):
    queue = FaultPriorityQueue()
    first = make_fault(reporter, "First critical", SeverityCategory.CRITICAL)
    second = make_fault(reporter, "Second critical", SeverityCategory.CRITICAL)
    queue.push(first)
    queue.push(second)
    queue.ensure_claimable(first)   # neither call may raise
    queue.ensure_claimable(second)


def test_ensure_claimable_reports_how_many_higher_priority_faults_remain(reporter):
    queue = FaultPriorityQueue()
    for title in ("C1", "C2"):
        queue.push(make_fault(reporter, title, SeverityCategory.CRITICAL))
    major = make_fault(reporter, "Maj", SeverityCategory.MAJOR)
    queue.push(major)

    with pytest.raises(RuntimeError, match="2 higher-priority faults are still unclaimed"):
        queue.ensure_claimable(major)


def test_ensure_claimable_uses_singular_wording_for_one_blocker(reporter):
    queue = FaultPriorityQueue()
    queue.push(make_fault(reporter, "C1", SeverityCategory.CRITICAL))
    major = make_fault(reporter, "Maj", SeverityCategory.MAJOR)
    queue.push(major)

    with pytest.raises(RuntimeError, match="1 higher-priority fault is still unclaimed"):
        queue.ensure_claimable(major)


def test_ensure_claimable_rejects_a_fault_that_is_not_queued(reporter):
    queue = FaultPriorityQueue()
    fault = make_fault(reporter, "Never queued", SeverityCategory.CRITICAL)
    with pytest.raises(RuntimeError, match="not waiting in the queue"):
        queue.ensure_claimable(fault)


def test_a_fault_becomes_claimable_once_the_higher_priority_ones_are_gone(reporter):
    queue = FaultPriorityQueue()
    critical = make_fault(reporter, "C1", SeverityCategory.CRITICAL)
    major = make_fault(reporter, "Maj", SeverityCategory.MAJOR)
    queue.push(critical)
    queue.push(major)
    with pytest.raises(RuntimeError):
        queue.ensure_claimable(major)

    queue.remove(critical)
    queue.ensure_claimable(major)  # no longer blocked
    assert queue.higher_priority_count(major) == 0