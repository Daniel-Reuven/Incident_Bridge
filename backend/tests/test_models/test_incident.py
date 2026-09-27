"""
Unit tests for app.models.incident.Incident - the shared base class behind
both MaintenanceTask and Fault.

Incident can't be instantiated directly (it enforces this itself inside
__init__, not via Python's abc.abstractmethod machinery), so these tests
use MaintenanceTask as a stand-in concrete subclass. Every behavior
tested here (add_comment, start_progress, close) is inherited UNCHANGED
by both MaintenanceTask and Fault - see test_maintenance_task.py and
test_fault.py for the small amount of behavior that's actually specific
to each subclass.
"""

import pytest

from app.models.enums import IncidentStatus, ResolutionType, Role
from app.models.incident import Incident
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


def test_incident_cannot_be_instantiated_directly(reporter):
    with pytest.raises(TypeError):
        Incident(title="x", description="y", created_by=reporter)


def test_new_incident_starts_open(task):
    assert task.status == IncidentStatus.OPEN
    assert task.resolution_type is None
    assert task.resolution_message is None


def test_add_comment_appends_and_returns_the_comment(task, reporter):
    comment = task.add_comment(reporter, "Looking into it")
    assert comment in task.comments
    assert task.comments[-1].text == "Looking into it"


def test_start_progress_moves_open_to_in_progress(task):
    task.start_progress()
    assert task.status == IncidentStatus.IN_PROGRESS


def test_start_progress_is_a_no_op_once_already_in_progress(task):
    task.start_progress()
    assert task.status == IncidentStatus.IN_PROGRESS
    task.start_progress()  # calling again must not raise or change anything
    assert task.status == IncidentStatus.IN_PROGRESS


def test_close_requires_a_non_empty_message(task, reporter):
    with pytest.raises(ValueError, match="closing message is required"):
        task.close(reporter, ResolutionType.RESOLVED, "")


def test_close_requires_a_non_whitespace_message(task, reporter):
    with pytest.raises(ValueError, match="closing message is required"):
        task.close(reporter, ResolutionType.RESOLVED, "   ")


def test_resolved_can_be_set_by_a_regular_user(task, reporter):
    task.close(reporter, ResolutionType.RESOLVED, "Fixed it.")
    assert task.status == IncidentStatus.CLOSED
    assert task.resolution_type == ResolutionType.RESOLVED
    assert task.resolution_message == "Fixed it."


def test_not_an_incident_by_a_regular_user_is_rejected(task, reporter):
    with pytest.raises(PermissionError):
        task.close(reporter, ResolutionType.NOT_AN_INCIDENT, "Not really a bug.")


def test_by_design_by_a_regular_user_is_rejected(task, reporter):
    with pytest.raises(PermissionError):
        task.close(reporter, ResolutionType.BY_DESIGN, "Working as intended.")


def test_not_an_incident_by_an_admin_succeeds(task, admin):
    task.close(admin, ResolutionType.NOT_AN_INCIDENT, "Not really a bug.")
    assert task.resolution_type == ResolutionType.NOT_AN_INCIDENT


def test_by_design_by_an_admin_succeeds(task, admin):
    task.close(admin, ResolutionType.BY_DESIGN, "Working as intended.")
    assert task.resolution_type == ResolutionType.BY_DESIGN


def test_empty_message_check_happens_before_the_permission_check(task, reporter):
    """
    Important edge case: a non-admin user closing with an admin-only
    resolution type AND an empty message should get a ValueError (about
    the missing message), not a PermissionError - the code checks the
    message first (see Incident.close()'s source order). Getting this
    backwards would mean a user only learns their message was empty AFTER
    already being told "permission denied", which is a confusing order
    for the frontend to show.
    """
    with pytest.raises(ValueError):
        task.close(reporter, ResolutionType.NOT_AN_INCIDENT, "")
