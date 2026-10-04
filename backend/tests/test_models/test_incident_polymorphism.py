"""
Tests for the polymorphic Incident interface (app/models/incident.py):
the abstract `kind` property and `extra_fields()` method, their
implementations in MaintenanceTask and Fault, and that code handling
incidents (the API serializer, persistence) works through them instead of
checking which class an object is.

Run from backend/:  python -m pytest tests/test_models/test_incident_polymorphism.py -v
Uses the shared admin_user fixture from tests/conftest.py.
"""

import pytest

from app.api.serializers import incident_to_dict
from app.models import Fault, Incident, MaintenanceTask, SeverityCategory
from app.persistence import SqliteIncidentStore
from app.repository import UserStore


def make_fault(user) -> Fault:
    return Fault("API down", "503s", user, SeverityCategory.CRITICAL, 1.0, details={"system_unavailable": True})


def test_incident_is_abstract(admin_user):
    with pytest.raises(TypeError):
        Incident("t", "d", admin_user)


def test_a_subclass_missing_an_abstract_member_cannot_be_created(admin_user):
    class HalfDone(Incident):
        @property
        def kind(self):
            return "half"
        # extra_fields() deliberately not implemented

    with pytest.raises(TypeError, match="extra_fields"):
        HalfDone("t", "d", admin_user)


def test_each_type_reports_its_own_kind_and_fields(admin_user):
    task = MaintenanceTask("Patch", "d", admin_user)
    task.queue_position = 2
    fault = make_fault(admin_user)
    assert (task.kind, task.extra_fields()) == ("maintenance", {"queue_position": 2})
    assert fault.kind == "fault" and fault.kind == Fault.KIND
    assert fault.extra_fields() == {"severity": 1, "severity_score": 1.0, "details": {"system_unavailable": True}}


def test_the_serializer_uses_the_polymorphic_members(admin_user):
    task_data = incident_to_dict(MaintenanceTask("Patch", "d", admin_user))
    fault_data = incident_to_dict(make_fault(admin_user))
    assert task_data["type"] == "maintenance" and task_data["queue_position"] is None and "severity" not in task_data
    assert fault_data["type"] == "fault" and fault_data["severity"] == 1 and "queue_position" not in fault_data


def test_a_new_incident_type_needs_no_change_to_the_serializer(admin_user):
    """Open/Closed in practice: a third type plugs in just by implementing the two members."""
    class AuditTask(Incident):
        @property
        def kind(self):
            return "audit"

        def extra_fields(self):
            return {"auditor": "external"}

    data = incident_to_dict(AuditTask("Yearly audit", "d", admin_user))
    assert data["type"] == "audit" and data["auditor"] == "external"


def test_persistence_round_trip_still_works_for_both_types(admin_user):
    users = UserStore()
    users.add(admin_user)
    store = SqliteIncidentStore(":memory:")
    task = MaintenanceTask("Patch", "d", admin_user)
    task.queue_position = 3
    fault = make_fault(admin_user)
    store.save_incident(task)
    store.save_incident(fault)
    loaded = {i.id: i for i in store.load_all(users)}
    assert loaded[task.id].kind == "maintenance" and loaded[task.id].queue_position == 3
    assert loaded[fault.id].kind == "fault" and loaded[fault.id].severity == SeverityCategory.CRITICAL
    assert loaded[fault.id].details == {"system_unavailable": True}
