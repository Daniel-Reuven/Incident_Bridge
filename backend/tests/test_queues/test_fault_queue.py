"""
Unit tests for app.queues.fault_queue.FaultPriorityQueue - the single
shared priority queue for faults of every severity (Critical, Major, and
Minor all share one heap - see README.md section 3).
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


def test_new_queue_is_empty():
    queue = FaultPriorityQueue()
    assert len(queue) == 0


def test_pop_most_severe_on_an_empty_queue_raises_index_error():
    queue = FaultPriorityQueue()
    with pytest.raises(IndexError):
        queue.pop_most_severe()


def test_peek_on_an_empty_queue_raises_index_error():
    queue = FaultPriorityQueue()
    with pytest.raises(IndexError):
        queue.peek()


def test_critical_pops_before_major_and_minor_regardless_of_insertion_order(reporter):
    """The core guarantee of this class: severity decides order, never arrival time."""
    queue = FaultPriorityQueue()
    minor = make_fault(reporter, "Minor issue", SeverityCategory.MINOR)
    major = make_fault(reporter, "Major issue", SeverityCategory.MAJOR)
    critical = make_fault(reporter, "Critical issue", SeverityCategory.CRITICAL)

    # Pushed in the "wrong" order on purpose - Minor first, Critical last.
    queue.push(minor)
    queue.push(major)
    queue.push(critical)

    assert queue.pop_most_severe() is critical
    assert queue.pop_most_severe() is major
    assert queue.pop_most_severe() is minor


def test_equal_severity_faults_are_popped_in_arrival_order(reporter):
    """When two faults share a severity, whichever was pushed first must come out first (the FIFO tiebreak)."""
    queue = FaultPriorityQueue()
    first = make_fault(reporter, "First critical", SeverityCategory.CRITICAL)
    second = make_fault(reporter, "Second critical", SeverityCategory.CRITICAL)
    queue.push(first)
    queue.push(second)

    assert queue.pop_most_severe() is first
    assert queue.pop_most_severe() is second


def test_peek_does_not_remove_the_fault(reporter):
    queue = FaultPriorityQueue()
    fault = make_fault(reporter, "x", SeverityCategory.CRITICAL)
    queue.push(fault)
    assert queue.peek() is fault
    assert len(queue) == 1  # still there
    assert queue.pop_most_severe() is fault  # can still be popped afterwards


def test_iter_by_severity_does_not_remove_anything(reporter):
    queue = FaultPriorityQueue()
    queue.push(make_fault(reporter, "Minor", SeverityCategory.MINOR))
    queue.push(make_fault(reporter, "Critical", SeverityCategory.CRITICAL))

    titles_in_order = [f.title for f in queue.iter_by_severity()]
    assert titles_in_order == ["Critical", "Minor"]
    assert len(queue) == 2  # nothing was removed by iterating


def test_dunder_iter_matches_iter_by_severity(reporter):
    queue = FaultPriorityQueue()
    queue.push(make_fault(reporter, "Minor", SeverityCategory.MINOR))
    queue.push(make_fault(reporter, "Critical", SeverityCategory.CRITICAL))
    assert [f.title for f in queue] == ["Critical", "Minor"]


def test_remove_an_existing_fault_returns_true_and_removes_it(reporter):
    queue = FaultPriorityQueue()
    fault = make_fault(reporter, "x", SeverityCategory.CRITICAL)
    queue.push(fault)
    assert queue.remove(fault) is True
    assert len(queue) == 0


def test_remove_a_fault_not_in_the_queue_returns_false(reporter):
    queue = FaultPriorityQueue()
    fault = make_fault(reporter, "x", SeverityCategory.CRITICAL)
    # Never pushed - must be a safe no-op, not an error.
    assert queue.remove(fault) is False


def test_reprioritize_moves_a_queued_fault_to_its_new_severity_position(reporter):
    """
    Mirrors what the API's PATCH /incidents/faults/{id}/severity endpoint
    does: change the fault's own .severity attribute (as an admin would
    via Fault.change_severity), then call reprioritize() so the heap
    reflects that change - heapq itself has no in-place "update priority"
    operation, which is exactly why this method exists.
    """
    queue = FaultPriorityQueue()
    escalate_me = make_fault(reporter, "Will be escalated", SeverityCategory.MINOR)
    already_major = make_fault(reporter, "Already major", SeverityCategory.MAJOR)
    queue.push(escalate_me)
    queue.push(already_major)

    escalate_me.severity = SeverityCategory.CRITICAL
    assert queue.reprioritize(escalate_me) is True

    assert queue.pop_most_severe() is escalate_me  # now pops first, ahead of Major


def test_reprioritize_on_a_fault_not_in_the_queue_returns_false(reporter):
    queue = FaultPriorityQueue()
    fault = make_fault(reporter, "Not queued", SeverityCategory.CRITICAL)
    assert queue.reprioritize(fault) is False

def test_dunder_str(reporter):
    queue = FaultPriorityQueue()
    # Test empty queue string
    assert str(queue) == "FaultPriorityQueue(0 faults: [])"

    # Push out of order to ensure the string representation applies priority sorting
    queue.push(make_fault(reporter, "Minor issue", SeverityCategory.MINOR))
    queue.push(make_fault(reporter, "Critical issue", SeverityCategory.CRITICAL))

    # Should show 2 faults, with Critical sorting before Minor
    assert str(queue) == "FaultPriorityQueue(2 faults: ['Critical issue', 'Minor issue'])"