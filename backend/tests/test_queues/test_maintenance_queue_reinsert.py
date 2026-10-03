"""
Unit tests for MaintenanceQueue.placement_bounds(), check_placement() and
reinsert() (Task 2, step 6): putting a reopened task back into the FIFO at a
chosen position. Closing and basic FIFO behavior are covered in
test_maintenance_queue_close.py and test_maintenance_queue.py.
"""

import pytest

from app.models.enums import ResolutionType, Role, IncidentStatus
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User
from app.queues.maintenance_queue import MaintenanceQueue


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_task(reporter, title):
    return MaintenanceTask(title=title, description="desc", created_by=reporter)


def make_queue(reporter, titles):
    queue = MaintenanceQueue()
    tasks = [make_task(reporter, title) for title in titles]
    for task in tasks:
        queue.enqueue(task)
    return queue, tasks


def positions(queue):
    return [(task.title, task.queue_position) for task in queue.all_tasks()]


def test_bounds_of_an_empty_queue(reporter):
    assert MaintenanceQueue().placement_bounds() == (1, 1)


def test_bounds_with_only_pending_tasks(reporter):
    queue, _ = make_queue(reporter, "ABC")
    assert queue.placement_bounds() == (1, 4)


def test_bounds_with_a_task_in_progress_start_at_two(reporter):
    queue, _ = make_queue(reporter, "ABC")
    queue.start_next()
    assert queue.placement_bounds() == (2, 4)


def test_reinsert_open_at_the_end(reporter):
    queue, _ = make_queue(reporter, "ABC")
    x = make_task(reporter, "X")
    queue.reinsert(x, 4)
    assert positions(queue) == [("A", 1), ("B", 2), ("C", 3), ("X", 4)]


def test_reinsert_open_at_the_start_when_nothing_is_in_progress(reporter):
    queue, _ = make_queue(reporter, "AB")
    x = make_task(reporter, "X")
    queue.reinsert(x, 1)
    assert positions(queue) == [("X", 1), ("A", 2), ("B", 3)]
    assert queue.current_task is None  # Open, so it is only first in line - not started


def test_reinsert_open_in_the_middle(reporter):
    queue, _ = make_queue(reporter, "ABC")
    x = make_task(reporter, "X")
    queue.reinsert(x, 2)
    assert positions(queue) == [("A", 1), ("X", 2), ("B", 3), ("C", 4)]


def test_reinsert_open_while_a_task_is_in_progress_starts_at_position_two(reporter):
    queue, _ = make_queue(reporter, "ABC")
    queue.start_next()  # A is current, B and C pending
    x = make_task(reporter, "X")
    queue.reinsert(x, 2)
    assert queue.current_task.title == "A"
    assert positions(queue) == [("A", 1), ("X", 2), ("B", 3), ("C", 4)]


def test_reinsert_in_progress_at_position_one_becomes_the_current_task(reporter):
    queue, _ = make_queue(reporter, "AB")
    x = make_task(reporter, "X")
    x.start_progress()  # a reopened-as-in-progress task
    queue.reinsert(x, 1)
    assert queue.current_task is x
    assert positions(queue) == [("X", 1), ("A", 2), ("B", 3)]


def test_reinsert_in_progress_into_an_empty_queue(reporter):
    queue = MaintenanceQueue()
    x = make_task(reporter, "X")
    x.start_progress()
    queue.reinsert(x, 1)
    assert queue.current_task is x
    assert x.queue_position == 1


def test_in_progress_is_rejected_while_another_task_is_in_progress(reporter):
    queue, _ = make_queue(reporter, "AB")
    queue.start_next()
    x = make_task(reporter, "X")
    x.start_progress()
    with pytest.raises(RuntimeError, match="already in progress"):
        queue.reinsert(x, 1)
    assert x not in queue
    assert queue.current_task.title == "A"


def test_in_progress_is_rejected_at_any_position_other_than_one(reporter):
    queue, _ = make_queue(reporter, "AB")
    x = make_task(reporter, "X")
    x.start_progress()
    with pytest.raises(ValueError, match="Only position 1"):
        queue.reinsert(x, 2)
    assert x not in queue


@pytest.mark.parametrize("bad_position", [0, 5])
def test_a_position_outside_the_bounds_is_rejected(reporter, bad_position):
    queue, _ = make_queue(reporter, "ABC")  # valid positions: 1 to 4
    x = make_task(reporter, "X")
    with pytest.raises(ValueError, match="between 1 and 4"):
        queue.reinsert(x, bad_position)
    assert x not in queue
    assert len(queue) == 3


def test_reinserting_a_task_that_is_already_queued_raises(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    with pytest.raises(RuntimeError, match="already in the queue"):
        queue.reinsert(a, 1)


def test_a_closed_task_cannot_be_placed_in_the_queue(reporter):
    queue, _ = make_queue(reporter, "A")
    x = make_task(reporter, "X")
    x.close(reporter, ResolutionType.RESOLVED, "Done.")
    with pytest.raises(ValueError, match="Open or In progress"):
        queue.reinsert(x, 1)
    assert x.status == IncidentStatus.CLOSED
    assert x not in queue


def test_fifo_order_is_preserved_after_a_reinsert(reporter):
    queue, _ = make_queue(reporter, "ABC")
    queue.reinsert(make_task(reporter, "X"), 2)
    started = []
    for _ in range(4):
        started.append(queue.start_next().title)
        queue.complete_current(reporter, "Done.")
    assert started == ["A", "X", "B", "C"]


def test_check_placement_changes_nothing(reporter):
    queue, _ = make_queue(reporter, "A")
    queue.check_placement(IncidentStatus.OPEN, 2)
    assert positions(queue) == [("A", 1)]
    assert len(queue) == 1