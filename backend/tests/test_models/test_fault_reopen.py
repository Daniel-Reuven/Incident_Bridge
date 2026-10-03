"""
Unit tests for Fault.reopen() (Task 2, step 3): the Fault-specific part of
reopening - who the fault is assigned to afterwards. The shared rules
(admin-only, reason required, closed-only, log comment) are covered in
test_incident_reopen.py against the base class.
"""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role, SeverityCategory
from app.models.fault import Fault
from app.models.user import User


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


@pytest.fixture
def admin():
    return User(username="admin-actor", role=Role.ADMIN, password="Passw0rd1")


@pytest.fixture
def closed_fault(reporter, admin):
    fault = Fault(
        title="Payment API down", description="Checkout failing", created_by=reporter,
        severity=SeverityCategory.CRITICAL, severity_score=1.0, details={"system_unavailable": True},
        assigned_to=reporter,
    )
    fault.close(admin, ResolutionType.RESOLVED, "Fixed.")
    return fault


def test_reopen_as_in_progress_assigns_the_fault_to_the_admin(closed_fault, admin):
    closed_fault.reopen(admin, IncidentStatus.IN_PROGRESS, "Needs more work")
    assert closed_fault.status == IncidentStatus.IN_PROGRESS
    assert closed_fault.assigned_to is admin


def test_reopen_as_open_clears_the_previous_assignee(closed_fault, admin):
    assert closed_fault.assigned_to is not None  # fixture starts it assigned to the reporter
    closed_fault.reopen(admin, IncidentStatus.OPEN, "Back in the queue")
    assert closed_fault.status == IncidentStatus.OPEN
    assert closed_fault.assigned_to is None


def test_rejected_reopen_leaves_the_assignee_and_status_alone(closed_fault, reporter):
    with pytest.raises(PermissionError):
        closed_fault.reopen(reporter, IncidentStatus.IN_PROGRESS, "Please reopen")
    assert closed_fault.status == IncidentStatus.CLOSED
    assert closed_fault.assigned_to is reporter