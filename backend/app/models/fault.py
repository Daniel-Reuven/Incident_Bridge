"""Fault: the fault/issue incident subclass."""

from typing import Optional

from app.models.enums import Role, SeverityCategory
from app.models.incident import Incident
from app.models.user import User


class Fault(Incident):
    """
    A fault/issue incident. Severity is normally assigned once by
    SeverityScorer at creation time (see app/services/severity_scoring.py)
    and can only be changed afterwards by an admin, since it affects
    FaultPriorityQueue's ordering.
    """

    def __init__(self, title: str, description: str, created_by: User,
                 severity: SeverityCategory, severity_score: float, details: Optional[dict] = None,
                 assigned_to: Optional[User] = None):
        super().__init__(title, description, created_by, assigned_to)
        self.severity = severity
        self.severity_score = severity_score
        self.details = details or {}

    def __str__(self) -> str:
        # Falls back to string conversion if severity isn't a standard Enum
        severity_name = self.severity.name if hasattr(self.severity, "name") else str(self.severity)
        return f"[Fault - {severity_name}] {self.title}"

    def change_severity(self, actor: User, new_severity: SeverityCategory) -> None:
        """Admin-only: reclassifying severity changes this fault's position in the priority queue."""
        if actor.role != Role.ADMIN:
            raise PermissionError("Only an admin may change a fault's severity.")
        self.severity = new_severity

    @classmethod
    def from_dict(cls, data: dict, created_by: User, assigned_to: Optional[User] = None) -> "Fault":
        """
        Overrides Incident.from_dict: unlike MaintenanceTask, a Fault has
        extra fields to seed - 'details' (the raw signal dict, e.g.
        {"system_unavailable": true}) and its derived severity. Severity
        is auto-scored from those details via SeverityScorer, exactly like
        POST /incidents/faults does at the API layer (see
        app/api/incidents.py's create_fault) - a seed record never
        specifies severity directly, so the two entry points can't
        disagree about how a given set of details maps to a category.

        The import is local (not at module top) to avoid a domain model
        depending on app.services at import time, matching the existing
        pattern in app/models/user.py's set_password().
        """
        from app.services.severity_scoring import SeverityScorer

        super().from_dict(data, created_by, assigned_to)
        details = data.get("details") or {}
        severity = SeverityScorer.score(details)
        fault = cls(title=data["title"], description=data["description"], created_by=created_by,
                    severity=severity, severity_score=float(severity.value), details=details,
                    assigned_to=assigned_to)
        if "id" in data:
            fault.id = data["id"]
        return fault