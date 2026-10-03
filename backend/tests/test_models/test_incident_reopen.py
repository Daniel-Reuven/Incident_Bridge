"""
Unit tests for the manual status-change foundation on
app.models.incident.Incident: log_status_change() and reopen().

Kept in its own file (rather than appended to test_incident.py) so the
manual-status-change feature's tests stay together as it grows over the
next steps. As in test_incident.py, MaintenanceTask is used as the
stand-in concrete subclass, since Incident itself cannot be instantiated;
one test uses Fault to show the behavior is inherited by both types.
"""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role, SeverityCategory
from app.models.fault import Fault
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


@pytest.fixture
def closed_task(task, admin):
    task.close(admin, ResolutionType.RESOLVED, "Fixed it.")
    return task


# --- log_status_change() ---

def test_log_status_change_adds_a_comment_authored_by_the_actor(task, reporter):
    comment = task.log_status_change(
        reporter, IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS, reason="Starting now"
    )
    assert task.comments[-1] is comment
    assert comment.author is reporter
    assert "Status changed from Open to In progress." in comment.text
    assert "Reason: Starting now." in comment.text


def test_log_status_change_without_a_reason_omits_the_reason_part(task, reporter):
    comment = task.log_status_change(reporter, IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS)
    assert comment.text == "Status changed from Open to In progress."


def test_log_status_change_appends_optional_detail(task, reporter):
    comment = task.log_status_change(
        reporter, IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS,
        reason="Starting now", detail="Queue position: 1",
    )
    assert "Queue position: 1." in comment.text


def test_log_status_change_does_not_change_the_status_itself(task, reporter):
    """It only writes the log entry - performing the change is the caller's job."""
    task.log_status_change(reporter, IncidentStatus.OPEN, IncidentStatus.CLOSED)
    assert task.status == IncidentStatus.OPEN


# --- reopen() ---

def test_reopen_by_an_admin_to_open_clears_the_resolution(closed_task, admin):
    closed_task.reopen(admin, IncidentStatus.OPEN, "Regression found")
    assert closed_task.status == IncidentStatus.OPEN
    assert closed_task.resolution_type is None
    assert closed_task.resolution_message is None


def test_reopen_by_an_admin_to_in_progress(closed_task, admin):
    closed_task.reopen(admin, IncidentStatus.IN_PROGRESS, "Picking this back up")
    assert closed_task.status == IncidentStatus.IN_PROGRESS
    assert closed_task.resolution_type is None


def test_reopen_logs_the_reason_and_the_previous_resolution(closed_task, admin):
    closed_task.reopen(admin, IncidentStatus.OPEN, "Regression found")
    comment = closed_task.comments[-1]
    assert comment.author is admin
    assert "Status changed from Closed to Open." in comment.text
    assert "Reason: Regression found." in comment.text
    assert "Previous resolution: Resolved - Fixed it." in comment.text


def test_reopen_by_a_regular_user_is_rejected_and_changes_nothing(closed_task, reporter):
    comments_before = len(closed_task.comments)  # close() itself already logged one
    with pytest.raises(PermissionError):
        closed_task.reopen(reporter, IncidentStatus.OPEN, "Please reopen")
    assert closed_task.status == IncidentStatus.CLOSED
    assert closed_task.resolution_type == ResolutionType.RESOLVED
    assert len(closed_task.comments) == comments_before


@pytest.mark.parametrize("blank_reason", ["", "   ", None])
def test_reopen_with_a_blank_reason_is_rejected(closed_task, admin, blank_reason):
    with pytest.raises(ValueError, match="reason is required"):
        closed_task.reopen(admin, IncidentStatus.OPEN, blank_reason)
    assert closed_task.status == IncidentStatus.CLOSED


def test_blank_reason_is_checked_before_permission(closed_task, reporter):
    """
    Same ordering as Incident.close(): a regular user who sends a blank
    reason should learn about the missing reason (ValueError) rather than
    only being told "permission denied" first.
    """
    with pytest.raises(ValueError):
        closed_task.reopen(reporter, IncidentStatus.OPEN, "")


@pytest.mark.parametrize("not_closed_status", [IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS])
def test_reopening_an_incident_that_is_not_closed_raises_runtime_error(task, admin, not_closed_status):
    if not_closed_status == IncidentStatus.IN_PROGRESS:
        task.start_progress()
    with pytest.raises(RuntimeError, match="closed"):
        task.reopen(admin, IncidentStatus.OPEN, "Reason")


def test_reopening_into_closed_is_rejected(closed_task, admin):
    with pytest.raises(ValueError, match="Open or In progress"):
        closed_task.reopen(admin, IncidentStatus.CLOSED, "Reason")
    assert closed_task.status == IncidentStatus.CLOSED


def test_a_reopened_incident_can_be_closed_again(closed_task, admin):
    closed_task.reopen(admin, IncidentStatus.OPEN, "Regression found")
    closed_task.close(admin, ResolutionType.RESOLVED, "Fixed properly this time.")
    assert closed_task.status == IncidentStatus.CLOSED
    assert closed_task.resolution_message == "Fixed properly this time."


def test_reopen_is_inherited_by_faults_too(admin):
    fault = Fault(
        title="Payment API down", description="Checkout failing", created_by=admin,
        severity=SeverityCategory.CRITICAL, severity_score=1.0, details={"system_unavailable": True},
    )
    fault.close(admin, ResolutionType.RESOLVED, "Gateway restarted.")
    fault.reopen(admin, IncidentStatus.OPEN, "It is down again")
    assert fault.status == IncidentStatus.OPEN
    assert fault.resolution_type is None
    assert "Previous resolution: Resolved - Gateway restarted." in fault.comments[-1].text
