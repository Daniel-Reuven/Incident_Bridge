"""Fault: the fault/issue incident subclass."""

from datetime import datetime, timezone
from typing import Optional

from app.models.enums import IncidentStatus, Role, SeverityCategory
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

    # Type name - see Incident.kind (also a class attribute: Fault.KIND).
    KIND = "fault"

    @property
    def kind(self) -> str:
        """Implements Incident.kind."""
        return self.KIND

    def extra_fields(self) -> dict:
        """Implements Incident.extra_fields(): severity (its numeric value), severity score and the scoring details."""
        return {"severity": self.severity.value, "severity_score": self.severity_score, "details": self.details}

    def __str__(self) -> str:
        # Falls back to string conversion if severity isn't a standard Enum
        severity_name = self.severity.name if hasattr(self.severity, "name") else str(self.severity)
        return f"[Fault - {severity_name}] {self.title}"

    def change_severity(self, actor: User, new_severity: SeverityCategory) -> None:
        """
        Admin-only: reclassifying severity changes this fault's position in the priority queue.

        Keeps severity_score in sync with the new category (it is always
        float(severity.value), exactly as at creation time - previously it
        was left stale) and bumps updated_at, so the change is visible to
        anything that compares timestamps (e.g. the dashboard's row diffing).
        Does not touch the queue itself: the API layer calls
        FaultPriorityQueue.reprioritize() afterwards.

        Raises PermissionError if actor is not an admin; nothing is changed in that case.
        """
        if actor.role != Role.ADMIN:
            raise PermissionError("Only an admin may change a fault's severity.")
        self.severity = new_severity
        self.severity_score = float(new_severity.value)
        self.updated_at = datetime.now(timezone.utc)

    def reopen(self, actor: User, new_status: IncidentStatus, reason: str,
               detail: Optional[str] = None) -> None:
        """
        Reopen a closed fault (admin-only, reason required - all validation
        and the log comment come from Incident.reopen()), then settle who it
        is assigned to, which is the one thing that differs for a fault:

          - reopened as IN_PROGRESS: assigned to `actor`, the admin who
            reopened it - it is being worked on right now, outside the
            priority queue (it is not taken from the queue, so the
            "highest severity first" claim rule does not apply to it).
          - reopened as OPEN: assigned_to is cleared, because an Open fault
            is by definition unclaimed and waiting in the priority queue.

        `detail` is an optional extra sentence for the log comment (same as
        Incident.reopen); faults currently don't use it.

        If Incident.reopen() raises (blank reason, invalid target status,
        not an admin, not closed), nothing is changed - the assignee is
        only touched after it succeeds. Putting an Open fault back into
        FaultPriorityQueue is the API layer's job (see
        app/api/incidents.py's reopen_fault).
        """
        super().reopen(actor, new_status, reason, detail)
        self.assigned_to = actor if new_status == IncidentStatus.IN_PROGRESS else None

    def claim(self, actor: User) -> None:
        """
        Claim an OPEN fault: Open -> In progress, assigned to `actor`, with a
        log comment ("Status changed from Open to In progress. Assigned to
        <username>."). Whether this fault is ALLOWED to be claimed right now
        (the priority rule) is not this method's concern - that is
        FaultPriorityQueue.ensure_claimable(), and removing the fault from
        the queue afterwards is the API layer's job (see app/api/incidents.py's
        claim_fault).

        Raises:
            RuntimeError: if the fault is not Open (the API layer maps this
                to HTTP 409). Nothing is changed in that case.
        """
        if self.status != IncidentStatus.OPEN:
            raise RuntimeError("Only an open fault can be claimed.")
        old_status = self.status
        self.start_progress()
        self.assigned_to = actor
        self.log_status_change(actor, old_status, self.status, detail=f"Assigned to {actor.username}")

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

        'details' is validated by SeverityScorer.validate_details() (via
        score()): it must be a JSON object with only known keys, true/false
        flags and a 0-100 affected_users_percent - anything else raises
        ValueError, so the loader skips the record instead of crashing.
        The record's required 'id' replaces the freshly-generated one.

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
        fault.id = cls.incident_id_from(data)
        return fault