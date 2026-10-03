"""Unit tests for the optional `detail` argument of Incident.reopen() / Fault.reopen() (Task 2, step 6)."""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role, SeverityCategory
from app.models.fault import Fault
from app.models.maintenance_task import MaintenanceTask
from app.models.user import User


@pytest.fixture
def admin():
    return User(username="admin-actor", role=Role.ADMIN, password="Passw0rd1")


@pytest.fixture
def closed_task(admin):
    task = MaintenanceTask(title="Patch server", description="d", created_by=admin)
    task.close(admin, ResolutionType.RESOLVED, "Fixed it.")
    return task


def test_detail_is_appended_after_the_previous_resolution(closed_task, admin):
    closed_task.reopen(admin, IncidentStatus.OPEN, "Regression found", detail="Queue position: 3")
    text = closed_task.comments[-1].text
    assert "Reason: Regression found." in text
    assert "Previous resolution: Resolved - Fixed it. Queue position: 3." in text


def test_without_detail_the_comment_is_unchanged(closed_task, admin):
    closed_task.reopen(admin, IncidentStatus.OPEN, "Regression found")
    text = closed_task.comments[-1].text
    assert "Previous resolution: Resolved - Fixed it." in text
    assert "Queue position" not in text


def test_fault_reopen_accepts_and_logs_detail(admin):
    fault = Fault(
        title="Payment API down", description="d", created_by=admin,
        severity=SeverityCategory.CRITICAL, severity_score=1.0, details={},
    )
    fault.close(admin, ResolutionType.RESOLVED, "Fixed.")
    fault.reopen(admin, IncidentStatus.OPEN, "Down again", detail="Extra note")
    assert "Extra note." in fault.comments[-1].text