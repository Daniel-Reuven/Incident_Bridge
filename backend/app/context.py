"""
IncidentWorkSession: a context manager bracketing a period of active work
on an incident (Stage 1 syllabus: context managers).

There's no file or network resource to guard yet at this layer - the
"resource" being protected here is the incident's audit trail: whatever
happens inside the `with` block (success or exception), the comment
thread ends up with an accurate record of what occurred. This is the same
pattern that will later guard a real external-service connection or a
log file (Stage 2+), just applied to something this layer already has.
"""

from datetime import datetime, timezone
from typing import Optional

from app.models.incident import Incident
from app.models.user import User


class IncidentWorkSession:
    """
    Usage:
        with IncidentWorkSession(incident, actor=technician):
            ... do the actual work ...
        # incident.comments now has an automatic "work session" entry,
        # whether or not the block raised an exception.

    On enter: moves an Open incident to In Progress and records a start
    time (no-op on the status change if it's already In Progress).
    On a clean exit: appends a comment noting how long the session lasted.
    On an exception: appends a comment noting the interruption, then lets
    the exception propagate - it is never swallowed, but the audit trail
    always reflects what happened even if the caller's code failed
    partway through.
    """

    def __init__(self, incident: Incident, actor: User):
        self.incident = incident
        self.actor = actor
        self._start: Optional[datetime] = None

    def __enter__(self) -> "IncidentWorkSession":
        self.incident.start_progress()
        self._start = datetime.now(timezone.utc)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        duration = (datetime.now(timezone.utc) - self._start).total_seconds()
        if exc_type is None:
            self.incident.add_comment(self.actor, f"Work session completed after {duration:.1f}s.")
        else:
            self.incident.add_comment(
                self.actor,
                f"Work session interrupted after {duration:.1f}s by {exc_type.__name__}: {exc_val}",
            )
        return False  # never suppress the exception

    def __str__(self) -> str:
        state = "Active" if self._start else "Pending"
        return f"IncidentWorkSession(incident={self.incident}, actor={self.actor}, state={state})"