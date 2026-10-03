"""
Restart round trips for a maintenance task reopened at a chosen position
(Task 2, step 6): the placement must survive AppState.create() rebuilding the
queue from SQLite. The steps below mirror what the reopen endpoint does.
"""

from app.models import IncidentStatus, MaintenanceTask
from app.state import AppState


def _reopen(state, queue, task, admin, status, position):
    """Same sequence as POST /incidents/maintenance/{id}/reopen."""
    queue.check_placement(status, position)
    task.reopen(admin, status, "Reopening for test.", detail=f"Queue position: {position}")
    queue.reinsert(task, position)
    state.incidents.save_all(queue.all_tasks())


def _setup(tmp_path, monkeypatch, name):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / name))
    state = AppState.create()
    admin = state.users.get("admin")
    queue = state.maintenance.get_queue()
    tasks = {}
    for title in ("A", "B", "C"):
        task = MaintenanceTask(title=title, description="d", created_by=admin)
        state.incidents.add(task)
        queue.enqueue(task)
        tasks[title] = task
    state.incidents.save_all(queue.all_tasks())
    # Close B from the middle, persisting like the close endpoint does.
    queue.close_task(tasks["B"], admin, "Not needed.")
    state.incidents.save(tasks["B"])
    state.incidents.save_all(queue.all_tasks())
    return state, admin, queue, tasks


def test_a_task_reopened_at_the_start_keeps_its_place_after_a_restart(tmp_path, monkeypatch):
    state, admin, queue, tasks = _setup(tmp_path, monkeypatch, "reopen_start.db")
    _reopen(state, queue, tasks["B"], admin, IncidentStatus.OPEN, 1)

    restored = AppState.create().maintenance.get_queue()
    assert [(t.title, t.queue_position) for t in restored.all_tasks()] == [("B", 1), ("A", 2), ("C", 3)]


def test_a_task_reopened_as_in_progress_is_restored_as_the_current_task(tmp_path, monkeypatch):
    state, admin, queue, tasks = _setup(tmp_path, monkeypatch, "reopen_current.db")
    _reopen(state, queue, tasks["B"], admin, IncidentStatus.IN_PROGRESS, 1)

    restored = AppState.create().maintenance.get_queue()
    assert restored.current_task.title == "B"
    assert [(t.title, t.queue_position) for t in restored] == [("A", 2), ("C", 3)]