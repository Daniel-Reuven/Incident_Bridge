"""
Unit tests specific to Fault.

Fault's shared lifecycle behavior (close, add_comment, start_progress) is
inherited from Incident and already covered by test_incident.py - this
file only tests what's specific to Fault: severity, severity_score,
details, and the admin-only change_severity() rule.
"""

import pytest

from app.models.enums import Role, SeverityCategory
from app.models.fault import Fault
from app.models.user import User


@pytest.fixture
def reporter():
    return User(username="reporter", role=Role.USER, password="Passw0rd1")


@pytest.fixture
def admin():
    return User(username="admin-actor", role=Role.ADMIN, password="Passw0rd1")


@pytest.fixture
def fault(reporter):
    return Fault(
        title="Payment API down",
        description="Checkout is failing",
        created_by=reporter,
        severity=SeverityCategory.CRITICAL,
        severity_score=1.0,
        details={"system_unavailable": True},
    )


def test_fault_stores_severity_score_and_details(fault):
    assert fault.severity == SeverityCategory.CRITICAL
    assert fault.severity_score == 1.0
    assert fault.details == {"system_unavailable": True}


def test_fault_defaults_details_to_an_empty_dict_when_none(reporter):
    fault = Fault(
        title="x", description="y", created_by=reporter,
        severity=SeverityCategory.MINOR, severity_score=3.0, details=None,
    )
    assert fault.details == {}


def test_change_severity_by_a_regular_user_is_rejected(fault, reporter):
    with pytest.raises(PermissionError):
        fault.change_severity(reporter, SeverityCategory.MINOR)
    # Severity must remain unchanged after a rejected attempt.
    assert fault.severity == SeverityCategory.CRITICAL


def test_change_severity_by_an_admin_succeeds(fault, admin):
    fault.change_severity(admin, SeverityCategory.MAJOR)
    assert fault.severity == SeverityCategory.MAJOR


def test_from_dict_auto_scores_severity_from_details(reporter):
    """
    from_dict never accepts severity directly - it's always derived from
    'details' via SeverityScorer, the same as POST /incidents/faults at
    the API layer, so a seed record and an API request can never disagree
    about what a given set of details scores as.
    """
    record = {
        "id": "seed-fault-1", "kind": "fault", "title": "Payment API down",
        "description": "Checkout is failing", "details": {"system_unavailable": True},
    }

    fault = Fault.from_dict(record, created_by=reporter)

    assert fault.id == "seed-fault-1"
    assert fault.severity == SeverityCategory.CRITICAL
    assert fault.severity_score == 1.0
    assert fault.details == {"system_unavailable": True}


def test_from_dict_defaults_to_minor_when_details_is_missing(reporter):
    record = {"id": "seed-fault-2", "kind": "fault", "title": "Typo", "description": "Small text issue"}
    fault = Fault.from_dict(record, created_by=reporter)
    assert fault.severity == SeverityCategory.MINOR
    assert fault.details == {}