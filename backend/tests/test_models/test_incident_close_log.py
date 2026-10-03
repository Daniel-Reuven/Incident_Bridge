"""
Unit tests for the logging and already-closed guard that
Incident.close() gained in Task 2, step 2. MaintenanceTask is the
stand-in concrete subclass (Incident itself cannot be instantiated).
"""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


@pytest.fixture
def admin():
    return User(username="admin-actor", role=Role.ADMIN, password="Passw0rd1")


@pytest.fixture
def task(reporter):
    return MaintenanceTask(title="Patch server", description="Apply patches", created_by=reporter)


def test_close_logs_a_status_change_comment_from_open(task, reporter):
    task.close(reporter, ResolutionType.RESOLVED, "Fixed it.")
    assert len(task.comments) == 1
    comment = task.comments[0]
    assert comment.author is reporter
    assert "Status changed from Open to Closed." in comment.text
    assert "Reason: Fixed it." in comment.text
    assert "Resolution: Resolved." in comment.text


def test_close_from_in_progress_logs_the_old_status(task, reporter):
    task.start_progress()
    task.close(reporter, ResolutionType.RESOLVED, "Done.")
    assert "Status changed from In progress to Closed." in task.comments[-1].text


def test_close_logs_the_label_of_an_admin_only_resolution(task, admin):
    task.close(admin, ResolutionType.NOT_AN_INCIDENT, "Expected behavior.")
    assert "Resolution: Not an incident." in task.comments[-1].text


def test_a_rejected_close_changes_and_logs_nothing(task, reporter):
    with pytest.raises(PermissionError):
        task.close(reporter, ResolutionType.NOT_AN_INCIDENT, "Not really a bug.")
    with pytest.raises(ValueError):
        task.close(reporter, ResolutionType.RESOLVED, "   ")
    assert task.status == IncidentStatus.OPEN
    assert task.resolution_type is None
    assert task.comments == []


def test_closing_an_already_closed_incident_raises_runtime_error(task, reporter):
    task.close(reporter, ResolutionType.RESOLVED, "First closure.")
    with pytest.raises(RuntimeError, match="already closed"):
        task.close(reporter, ResolutionType.RESOLVED, "Second closure.")
    # The first resolution and its single log entry must be untouched.
    assert task.resolution_message == "First closure."
    assert len(task.comments) == 1