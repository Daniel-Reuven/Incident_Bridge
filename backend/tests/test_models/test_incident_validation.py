"""
Unit tests for the validated Incident.title / Incident.description
properties (app/models/incident.py). They apply to both incident types,
because MaintenanceTask and Fault build themselves through Incident.__init__.

Run from backend/:  python -m pytest tests/test_models/test_incident_validation.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import pytest

from app.models import Fault, MaintenanceTask, SeverityCategory


def make_task(user, title="Patch server", description="Apply patches"):
    return MaintenanceTask(title=title, description=description, created_by=user)


def make_fault(user, title="API down", description="503 everywhere"):
    return Fault(title, description, user, SeverityCategory.CRITICAL, 1.0)


@pytest.mark.parametrize("factory", [make_task, make_fault])
@pytest.mark.parametrize("bad", ["", "   ", None, 42, "x" * 201])
def test_invalid_title_is_rejected_on_creation(admin_user, factory, bad):
    with pytest.raises(ValueError, match="title"):
        factory(admin_user, title=bad)


@pytest.mark.parametrize("factory", [make_task, make_fault])
@pytest.mark.parametrize("bad", ["", "\n\t ", None, ["text"], "x" * 10_001])
def test_invalid_description_is_rejected_on_creation(admin_user, factory, bad):
    with pytest.raises(ValueError, match="description"):
        factory(admin_user, description=bad)


def test_text_is_trimmed(admin_user):
    task = make_task(admin_user, title="  Patch server  ", description="\n Apply patches \n")
    assert (task.title, task.description) == ("Patch server", "Apply patches")


def test_limits_are_inclusive(admin_user):
    task = make_task(admin_user, title="x" * 200, description="y" * 10_000)
    assert len(task.title) == 200 and len(task.description) == 10_000


def test_an_invalid_edit_keeps_the_old_value(admin_user):
    task = make_task(admin_user)
    with pytest.raises(ValueError, match="title"):
        task.title = "   "
    with pytest.raises(ValueError, match="description"):
        task.description = ""
    assert (task.title, task.description) == ("Patch server", "Apply patches")


def test_a_valid_edit_is_applied(admin_user):
    task = make_task(admin_user)
    task.title = "Patch web server"
    assert task.title == "Patch web server"
