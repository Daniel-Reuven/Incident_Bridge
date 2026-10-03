"""
Unit tests for MaintenanceQueue.start_task() (Task 2, step 5 patch): starting
one SPECIFIC task, which is only allowed for the first pending task while
nothing is in progress. start_next() itself is covered in
test_maintenance_queue.py.
"""

import pytest

from app.models.enums import IncidentStatus, Role
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User
from app.queues.maintenance_queue import MaintenanceQueue


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_queue(reporter, titles):
    queue = MaintenanceQueue()
    tasks = [MaintenanceTask(title=title, description="d", created_by=reporter) for title in titles]
    for task in tasks:
        queue.enqueue(task)
    return queue, tasks


def test_start_task_starts_the_first_pending_task(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    started = queue.start_task(a)
    assert started is a
    assert a.status == IncidentStatus.IN_PROGRESS
    assert queue.current_task is a
    assert [(task.title, task.queue_position) for task in queue.all_tasks()] == [("A", 1), ("B", 2)]


def test_start_task_rejects_a_task_that_is_not_first(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    with pytest.raises(RuntimeError, match="first task in the queue"):
        queue.start_task(b)
    assert queue.current_task is None
    assert a.status == IncidentStatus.OPEN and b.status == IncidentStatus.OPEN


def test_start_task_rejects_starting_while_another_task_is_in_progress(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    queue.start_next()  # A is current
    with pytest.raises(RuntimeError, match="already in progress"):
        queue.start_task(b)
    assert b.status == IncidentStatus.OPEN


def test_start_task_rejects_a_task_that_is_not_in_the_queue(reporter):
    queue, _ = make_queue(reporter, "A")
    outsider = MaintenanceTask(title="X", description="d", created_by=reporter)
    with pytest.raises(RuntimeError):
        queue.start_task(outsider)


def test_fifo_continues_normally_after_start_task(reporter):
    queue, (a, b) = make_queue(reporter, "AB")
    queue.start_task(a)
    queue.complete_current(reporter, "Done.")
    assert queue.start_task(b) is b