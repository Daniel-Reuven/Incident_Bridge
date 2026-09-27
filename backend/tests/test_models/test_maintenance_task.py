"""
Unit tests specific to MaintenanceTask.

Most of MaintenanceTask's behavior is inherited from Incident and is
already covered by test_incident.py - this file only tests what
MaintenanceTask itself adds on top of that shared base.
"""

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
