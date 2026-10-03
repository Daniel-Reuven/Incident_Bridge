"""
Unit tests for MaintenanceQueue.close_task(), all_tasks(), __contains__,
the cleared queue_position on close, and MaintenanceQueueManager.queue_of()
(Task 2, step 5). The queue's basic FIFO behavior is covered in
test_maintenance_queue.py.
"""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User
from app.queues.maintenance_queue import MaintenanceQueue, MaintenanceQueueManager


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_task(reporter, title):
    return MaintenanceTask(title=title, description="desc", created_by=reporter)


def make_queue(reporter, titles):
    """A queue with one pending task per character in `titles`, returned with the tasks in that order."""
    queue = MaintenanceQueue()
    tasks = [make_task(reporter, title) for title in titles]
    for task in tasks:
        queue.enqueue(task)
    return queue, tasks


def positions(queue):
    return [(task.title, task.queue_position) for task in queue.all_tasks()]


def test_contains_covers_current_and_pending_tasks(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    outsider = make_task(reporter, "X")
    queue.start_next()
    assert a in queue and b in queue
    assert outsider not in queue


def test_all_tasks_lists_the_current_task_first_then_the_pending_ones(reporter):
    queue, _ = make_queue(reporter, "ABC")
    queue.start_next()
    assert [task.title for task in queue.all_tasks()] == ["A", "B", "C"]


def test_closing_a_middle_pending_task_moves_the_later_ones_up(reporter):
    queue, (a, b, c, d) = make_queue(reporter, "ABCD")
    queue.close_task(c, reporter, "Not needed.")
    assert c.status == IncidentStatus.CLOSED
    assert positions(queue) == [("A", 1), ("B", 2), ("D", 3)]


def test_closing_the_first_pending_task_promotes_the_next(reporter):
    queue, (a, b, c) = make_queue(reporter, "ABC")
    queue.close_task(a, reporter, "Duplicate.")
    assert positions(queue) == [("B", 1), ("C", 2)]


def test_closing_a_pending_task_while_another_is_in_progress(reporter):
    queue, (a, b, c, d) = make_queue(reporter, "ABCD")
    queue.start_next()  # A is current
    queue.close_task(c, reporter, "Not needed.")
    assert queue.current_task is a
    assert positions(queue) == [("A", 1), ("B", 2), ("D", 3)]


def test_closing_the_current_task_frees_the_queue_and_keeps_the_next_one_pending(reporter):
    queue, (a, b, c) = make_queue(reporter, "ABC")
    queue.start_next()
    queue.close_task(a, reporter, "Done.")
    assert queue.current_task is None
    assert b.status == IncidentStatus.OPEN  # NOT auto-started
    assert positions(queue) == [("B", 1), ("C", 2)]


def test_close_task_clears_the_closed_tasks_queue_position(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    queue.close_task(b, reporter, "Not needed.")
    assert b.queue_position is None


def test_complete_current_also_clears_the_closed_tasks_queue_position(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    queue.start_next()
    queue.complete_current(reporter, "Done.")
    assert a.queue_position is None


def test_fifo_order_of_the_remaining_tasks_is_preserved_after_a_removal(reporter):
    queue, (a, b, c, d) = make_queue(reporter, "ABCD")
    queue.close_task(b, reporter, "Not needed.")
    started = []
    for _ in range(3):
        started.append(queue.start_next().title)
        queue.complete_current(reporter, "Done.")
    assert started == ["A", "C", "D"]


@pytest.mark.parametrize("case", ["admin_only_resolution_by_regular_user", "blank_reason"])
def test_a_rejected_close_leaves_the_queue_untouched(reporter, case):
    queue, (a, b) = make_queue(reporter, "AB")
    if case == "admin_only_resolution_by_regular_user":
        with pytest.raises(PermissionError):
            queue.close_task(b, reporter, "Expected.", ResolutionType.NOT_AN_INCIDENT)
    else:
        with pytest.raises(ValueError):
            queue.close_task(b, reporter, "   ")
    assert b.status == IncidentStatus.OPEN
    assert b in queue
    assert positions(queue) == [("A", 1), ("B", 2)]


def test_closing_a_task_that_is_not_in_the_queue_raises_runtime_error(reporter):
    queue, _ = make_queue(reporter, "A")
    outsider = make_task(reporter, "X")
    with pytest.raises(RuntimeError, match="not in this queue"):
        queue.close_task(outsider, reporter, "Whatever.")


def test_manager_queue_of_finds_the_owning_queue(reporter):
    manager = MaintenanceQueueManager()
    task = make_task(reporter, "A")
    manager.get_queue().enqueue(task)
    assert manager.queue_of(task) is manager.get_queue()


def test_manager_queue_of_returns_none_for_a_task_in_no_queue(reporter):
    manager = MaintenanceQueueManager()
    assert manager.queue_of(make_task(reporter, "A")) is None