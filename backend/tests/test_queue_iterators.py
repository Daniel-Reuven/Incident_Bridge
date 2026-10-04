"""
Tests for the Iterable / Iterator split (Stage 1, Part D item 1):
MaintenanceQueue and FaultPriorityQueue are the Iterables, FifoQueueIterator
and SeverityOrderIterator (app/iterators.py) are their Iterators.

Shows: every iter() returns a new iterator, two iterators advance
independently, __next__ raises StopIteration at the end (and keeps doing
so), an iterator is its own iterable, iteration works on a snapshot, the
traversal order is FIFO / severity, and iterating never changes the queue.

Run from backend/:  python -m pytest tests/test_queue_iterators.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import pytest

from app.iterators import FifoQueueIterator, SeverityOrderIterator, SnapshotIterator
from app.models import Fault, MaintenanceTask, SeverityCategory
from app.queues import FaultPriorityQueue, MaintenanceQueue


@pytest.fixture
def queue(admin_user) -> MaintenanceQueue:
    q = MaintenanceQueue()
    for title in ("first", "second", "third"):
        q.enqueue(MaintenanceTask(title, "d", admin_user))
    return q


def fault(user, title, severity) -> Fault:
    return Fault(title, "d", user, severity, float(severity.value))


@pytest.fixture
def faults(admin_user) -> FaultPriorityQueue:
    q = FaultPriorityQueue()
    # Arrival order deliberately differs from severity order.
    q.push(fault(admin_user, "minor typo", SeverityCategory.MINOR))
    q.push(fault(admin_user, "major slow", SeverityCategory.MAJOR))
    q.push(fault(admin_user, "critical down", SeverityCategory.CRITICAL))
    q.push(fault(admin_user, "major errors", SeverityCategory.MAJOR))
    return q


def titles(items):
    return [item.title for item in items]


# --- the protocol ---------------------------------------------------------------

def test_iter_returns_the_dedicated_iterator_classes(queue, faults):
    assert isinstance(iter(queue), FifoQueueIterator)
    assert isinstance(iter(faults), SeverityOrderIterator)
    assert issubclass(FifoQueueIterator, SnapshotIterator) and issubclass(SeverityOrderIterator, SnapshotIterator)


def test_every_iter_call_returns_a_new_iterator(queue):
    assert iter(queue) is not iter(queue)


def test_an_iterator_is_its_own_iterable(queue):
    it = iter(queue)
    assert iter(it) is it


def test_next_then_stop_iteration_forever(queue):
    it = iter(queue)
    assert [next(it).title, next(it).title, next(it).title] == ["first", "second", "third"]
    with pytest.raises(StopIteration):
        next(it)
    with pytest.raises(StopIteration):     # still exhausted on later calls
        next(it)


def test_a_for_loop_continues_from_the_current_position(queue):
    it = iter(queue)
    next(it)
    assert titles(it) == ["second", "third"]
    assert titles(it) == []                 # exhausted - a new iter(queue) is needed to start again
    assert titles(queue) == ["first", "second", "third"]


def test_two_iterators_advance_independently(queue):
    a, b = iter(queue), iter(queue)
    next(a)
    next(a)
    assert next(b).title == "first"          # b did not move when a did
    assert next(a).title == "third"
    assert (a.remaining, b.remaining) == (0, 2)


def test_empty_queues_stop_immediately():
    with pytest.raises(StopIteration):
        next(iter(MaintenanceQueue()))
    assert list(FaultPriorityQueue()) == []


# --- traversal rules -----------------------------------------------------------------

def test_fifo_order_and_the_in_progress_task_is_left_out(queue):
    queue.start_next()                       # "first" is now in progress
    assert titles(queue) == ["second", "third"]


def test_severity_order_with_ties_by_arrival(faults):
    assert titles(faults) == ["critical down", "major slow", "major errors", "minor typo"]


def test_heap_storage_is_not_already_in_order(faults):
    """The reason SeverityOrderIterator sorts: the heap's internal list is not in priority order."""
    internal = [f.title for _, _, f in faults._heap]
    assert internal != titles(faults)


# --- snapshot and read-only behavior --------------------------------------------------

def test_changing_the_queue_while_iterating_is_safe(queue, admin_user):
    it = iter(queue)
    next(it)
    queue.enqueue(MaintenanceTask("added later", "d", admin_user))   # would break a plain deque iteration
    assert titles(it) == ["second", "third"]                        # works on the snapshot taken at iter()
    assert "added later" in titles(queue)                           # a new iterator sees the new task


def test_iterating_does_not_change_the_queues(queue, faults):
    list(queue)
    list(faults)
    assert len(queue) == 3 and len(faults) == 4
    assert faults.peek().title == "critical down"


def test_str_and_repr(queue):
    it = iter(queue)
    next(it)
    assert str(it) == "FifoQueueIterator(item 1 of 3)"
    assert repr(it) == "FifoQueueIterator(position=1, size=3)"
