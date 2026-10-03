"""
Tests for startup queue rebuilding in app/state.py (Task 2, step 5):
the pure ordering rule order_pending_maintenance(), and real "restart"
round trips through AppState.create() against a throwaway SQLite file.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MaintenanceTask, Role, User
from app.state import AppState, order_pending_maintenance


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


def make_task(reporter, title, minutes, position):
    """A task with a controlled creation time and saved queue position."""
    task = MaintenanceTask(title=title, description="d", created_by=reporter)
    task.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    task.queue_position = position
    return task


def titles(tasks):
    return [task.title for task in tasks]


def test_contiguous_saved_positions_are_trusted_over_creation_order(reporter):
    tasks = [make_task(reporter, "A", 0, 3), make_task(reporter, "B", 1, 1), make_task(reporter, "C", 2, 2)]
    assert titles(order_pending_maintenance(tasks, has_current=False)) == ["B", "C", "A"]


def test_with_a_current_task_the_expected_positions_start_at_two(reporter):
    tasks = [make_task(reporter, "A", 0, 4), make_task(reporter, "B", 1, 2), make_task(reporter, "C", 2, 3)]
    assert titles(order_pending_maintenance(tasks, has_current=True)) == ["B", "C", "A"]


@pytest.mark.parametrize("saved", [[3, 1], [1, 1], [None, 1]])
def test_untrustworthy_positions_fall_back_to_creation_order(reporter, saved):
    tasks = [make_task(reporter, "A", 0, saved[0]), make_task(reporter, "B", 1, saved[1])]
    assert titles(order_pending_maintenance(tasks, has_current=False)) == ["A", "B"]


def test_no_pending_tasks_gives_an_empty_order():
    assert order_pending_maintenance([], has_current=False) == []


def test_queue_order_and_positions_survive_a_restart_after_a_mid_queue_close(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "restart.db"))
    state = AppState.create()
    admin = state.users.get("admin")
    queue = state.maintenance.get_queue()

    tasks = []
    for title in ("A", "B", "C", "D"):
        task = MaintenanceTask(title=title, description="d", created_by=admin)
        state.incidents.add(task)
        queue.enqueue(task)
        tasks.append(task)
    state.incidents.save_all(queue.all_tasks())

    # Close B from the middle, persisting exactly like the API endpoint does.
    queue.close_task(tasks[1], admin, "Not needed.")
    state.incidents.save(tasks[1])
    state.incidents.save_all(queue.all_tasks())

    restarted = AppState.create()
    restored = restarted.maintenance.get_queue()
    assert [task.title for task in restored] == ["A", "C", "D"]
    assert [task.queue_position for task in restored] == [1, 2, 3]


def test_a_restart_restores_the_current_task_and_the_pending_order(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "restart_current.db"))
    state = AppState.create()
    admin = state.users.get("admin")
    queue = state.maintenance.get_queue()

    for title in ("A", "B", "C"):
        task = MaintenanceTask(title=title, description="d", created_by=admin)
        state.incidents.add(task)
        queue.enqueue(task)
    queue.start_next()  # A becomes current
    state.incidents.save_all(queue.all_tasks())

    restarted = AppState.create()
    restored = restarted.maintenance.get_queue()
    assert restored.current_task.title == "A"
    assert [(task.title, task.queue_position) for task in restored] == [("B", 2), ("C", 3)]