"""
Unit tests specific to MaintenanceTask.

Most of MaintenanceTask's behavior is inherited from Incident and is
already covered by test_incident.py - this file only tests what
MaintenanceTask itself adds on top of that shared base.
"""

import pytest

from app.models.enums import Role
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User


def test_new_maintenance_task_has_no_queue_position_yet():
    """
    queue_position starts as None and is only ever set once the task is
    actually placed into a MaintenanceQueue (see
    tests/test_queues/test_maintenance_queue.py) - a task that exists but
    hasn't been enqueued anywhere yet has no position to report.
    """
    reporter = User(username="reporter", role=Role.USER, password="Passw0rd1")
    task = MaintenanceTask(title="Patch server", description="Apply patches", created_by=reporter)
    assert task.queue_position is None


def test_from_dict_builds_a_task_with_the_given_fields():
    reporter = User(username="reporter", role=Role.USER, password="Passw0rd1")
    record = {"id": "seed-task-1", "kind": "maintenance", "title": "Patch server", "description": "Apply patches"}

    task = MaintenanceTask.from_dict(record, created_by=reporter)

    assert task.id == "seed-task-1"
    assert task.title == "Patch server"
    assert task.description == "Apply patches"
    assert task.created_by is reporter
    assert task.queue_position is None  # not enqueued anywhere yet


def test_from_dict_without_an_id_is_rejected():
    """
    Explicit decision (Incident.from_dict): every seed record must carry an id,
    because the id is what makes re-importing idempotent - a record without
    one is rejected rather than silently given a random uuid.
    """
    reporter = User(username="reporter", role=Role.USER, password="Passw0rd1")
    record = {"kind": "maintenance", "title": "Patch server", "description": "Apply patches"}

    with pytest.raises(ValueError, match="'id'"):
        MaintenanceTask.from_dict(record, created_by=reporter)


def test_from_dict_accepts_a_numeric_id_as_text():
    reporter = User(username="reporter", role=Role.USER, password="Passw0rd1")
    record = {"id": 101, "kind": "maintenance", "title": "Patch server", "description": "Apply patches"}
    assert MaintenanceTask.from_dict(record, created_by=reporter).id == "101"


def test_str_representation_when_unqueued():
    reporter = User(username="reporter", role=Role.USER, password="Passw0rd1")
    task = MaintenanceTask(title="Patch server", description="Apply patches", created_by=reporter)
    assert str(task) == "[MaintenanceTask - Unqueued] Patch server"


def test_str_representation_when_queued():
    reporter = User(username="reporter", role=Role.USER, password="Passw0rd1")
    task = MaintenanceTask(title="Patch server", description="Apply patches", created_by=reporter)
    task.queue_position = 5
    assert str(task) == "[MaintenanceTask - Pos 5] Patch server"
