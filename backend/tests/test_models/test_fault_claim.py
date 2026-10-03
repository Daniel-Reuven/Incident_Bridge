"""Unit tests for Fault.claim() (Task 2, step 4)."""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role, SeverityCategory
from app.models.fault import Fault
from app.models.user import User


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


@pytest.fixture
def technician():
    return User(username="tech", role=Role.USER, password="Passw0rd1")


@pytest.fixture
def fault(reporter):
    return Fault(
        title="Payment API down", description="Checkout failing", created_by=reporter,
        severity=SeverityCategory.CRITICAL, severity_score=1.0, details={"system_unavailable": True},
    )


def test_claim_moves_the_fault_to_in_progress_and_assigns_the_actor(fault, technician):
    fault.claim(technician)
    assert fault.status == IncidentStatus.IN_PROGRESS
    assert fault.assigned_to is technician


def test_claim_logs_a_status_change_comment_authored_by_the_actor(fault, technician):
    fault.claim(technician)
    comment = fault.comments[-1]
    assert comment.author is technician
    assert "Status changed from Open to In progress." in comment.text
    assert "Assigned to tech." in comment.text


@pytest.mark.parametrize("make_not_open", ["in_progress", "closed"])
def test_claiming_a_fault_that_is_not_open_raises_and_changes_nothing(fault, technician, make_not_open):
    if make_not_open == "in_progress":
        fault.start_progress()
    else:
        fault.close(technician, ResolutionType.RESOLVED, "Done.")
    comments_before = len(fault.comments)
    status_before = fault.status

    with pytest.raises(RuntimeError, match="open fault"):
        fault.claim(technician)

    assert fault.status == status_before
    assert fault.assigned_to is None
    assert len(fault.comments) == comments_before