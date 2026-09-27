"""
Unit tests for app.queues.maintenance_queue.MaintenanceQueue and
MaintenanceQueueManager - the strict, un-reorderable FIFO queue.
"""

import pytest

from app.models.enums import Role
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User
from app.queues.maintenance_queue import MaintenanceQueue, MaintenanceQueueManager


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_task(reporter, title="Task"):
    return MaintenanceTask(title=title, description="desc", created_by=reporter)


def test_new_queue_is_empty():
    queue = MaintenanceQueue()
    assert len(queue) == 0
    assert queue.current_task is None
    assert list(queue) == []


def test_start_next_on_an_empty_queue_raises_index_error():
    queue = MaintenanceQueue()
    with pytest.raises(IndexError):
        queue.start_next()


def test_enqueue_then_start_next_returns_that_task(reporter):
    queue = MaintenanceQueue()
    task = make_task(reporter, "Only task")
    queue.enqueue(task)
    started = queue.start_next()
    assert started is task
    assert queue.current_task is task


def test_fifo_order_is_preserved(reporter):
    """The FIFO guarantee this whole class exists for: tasks start in the exact order they were enqueued."""
    queue = MaintenanceQueue()
    task_a = make_task(reporter, "A")
    task_b = make_task(reporter, "B")
    task_c = make_task(reporter, "C")
    queue.enqueue(task_a)
    queue.enqueue(task_b)
    queue.enqueue(task_c)

    first = queue.start_next()
    queue.complete_current(actor=reporter, message="done")
    second = queue.start_next()
    queue.complete_current(actor=reporter, message="done")
    third = queue.start_next()

    assert [first.title, second.title, third.title] == ["A", "B", "C"]


def test_start_next_while_one_is_already_in_progress_raises_runtime_error(reporter):
    queue = MaintenanceQueue()
    queue.enqueue(make_task(reporter, "A"))
    queue.enqueue(make_task(reporter, "B"))
    queue.start_next()
    with pytest.raises(RuntimeError):
        queue.start_next()


def test_complete_current_with_nothing_in_progress_raises_runtime_error(reporter):
    queue = MaintenanceQueue()
    with pytest.raises(RuntimeError):
        queue.complete_current(actor=reporter, message="done")


def test_complete_current_closes_the_task_and_frees_the_queue(reporter):
    queue = MaintenanceQueue()
    queue.enqueue(make_task(reporter, "A"))
    queue.start_next()
    completed = queue.complete_current(actor=reporter, message="All done")
    assert completed.status.value == "closed"
    assert completed.resolution_message == "All done"
    assert queue.current_task is None


def test_queue_position_is_kept_in_sync_while_pending(reporter):
    queue = MaintenanceQueue()
    task_a = make_task(reporter, "A")
    task_b = make_task(reporter, "B")
    queue.enqueue(task_a)
    queue.enqueue(task_b)
    queue.start_next()  # A becomes current
    assert task_a.queue_position == 1
    assert task_b.queue_position == 2  # first pending position is 2 while something is current


def test_queue_position_is_renumbered_immediately_after_complete_current(reporter):
    """
    complete_current() must call _renumber() right after clearing
    self._current, so a still-pending task's queue_position updates
    immediately - it must NOT sit stale until the next enqueue() or
    start_next() call happens to trigger a renumber. (This used to be a
    real bug: queue_position stayed at its old value, e.g. 2, until the
    next queue operation - fixed by adding the missing _renumber() call.)
    """
    queue = MaintenanceQueue()
    task_a = make_task(reporter, "A")
    task_b = make_task(reporter, "B")
    queue.enqueue(task_a)
    queue.enqueue(task_b)
    queue.start_next()
    assert task_b.queue_position == 2  # A is current, B is next in line
    queue.complete_current(actor=reporter, message="done")
    assert task_b.queue_position == 1  # renumbered immediately - nothing is current anymore


def test_restore_current_sets_current_without_going_through_start_next(reporter):
    queue = MaintenanceQueue()
    task = make_task(reporter, "Restored task")
    queue.restore_current(task)
    assert queue.current_task is task
    assert task.queue_position == 1


def test_restore_current_raises_if_a_current_task_already_exists(reporter):
    queue = MaintenanceQueue()
    queue.restore_current(make_task(reporter, "First"))
    with pytest.raises(RuntimeError):
        queue.restore_current(make_task(reporter, "Second"))


def test_iteration_only_covers_pending_tasks_not_the_current_one(reporter):
    queue = MaintenanceQueue()
    queue.enqueue(make_task(reporter, "A"))
    queue.enqueue(make_task(reporter, "B"))
    queue.start_next()  # A is now current, not pending
    pending_titles = [t.title for t in queue]
    assert pending_titles == ["B"]


def test_len_counts_the_current_task_plus_pending_ones(reporter):
    queue = MaintenanceQueue()
    queue.enqueue(make_task(reporter, "A"))
    queue.enqueue(make_task(reporter, "B"))
    assert len(queue) == 2
    queue.start_next()
    assert len(queue) == 2  # one current + one pending


def test_manager_has_a_default_queue_from_construction():
    manager = MaintenanceQueueManager()
    assert "default" in list(manager.queue_names())
    assert isinstance(manager.get_queue(), MaintenanceQueue)


def test_manager_creates_named_queues_on_first_request():
    manager = MaintenanceQueueManager()
    team_queue = manager.get_queue("team-b")
    assert team_queue.name == "team-b"
    assert "team-b" in list(manager.queue_names())


def test_manager_returns_the_same_queue_instance_on_repeated_calls():
    manager = MaintenanceQueueManager()
    first_call = manager.get_queue("team-b")
    second_call = manager.get_queue("team-b")
    assert first_call is second_call
