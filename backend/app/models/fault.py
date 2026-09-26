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

    def change_severity(self, actor: User, new_severity: SeverityCategory) -> None:
        """Admin-only: reclassifying severity changes this fault's position in the priority queue."""
        if actor.role != Role.ADMIN:
            raise PermissionError("Only an admin may change a fault's severity.")
        self.severity = new_severity
