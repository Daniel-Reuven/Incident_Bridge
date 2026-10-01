"""Incident base class: the shared lifecycle for MaintenanceTask and Fault."""

import uuid
from abc import ABC
from datetime import datetime, timezone
from typing import List, Optional

from app.models.comment import Comment
from app.models.enums import IncidentStatus, ResolutionType, Role
from app.models.user import User

# Only these two resolution types are restricted to admins. Kept as a
# module-level constant (rather than hardcoded in close()) so the rule is
# visible in one obvious place.
_ADMIN_ONLY_RESOLUTIONS = {ResolutionType.NOT_AN_INCIDENT, ResolutionType.BY_DESIGN}


class Incident(ABC):
    """
    Abstract base class shared by MaintenanceTask and Fault.

    Holds the lifecycle (status, resolution, timestamps) and the comment
    thread that both concrete incident types inherit unchanged; each
    subclass adds only what's specific to it (see maintenance_task.py and
    fault.py). This keeps the model open for a third incident type later
    without touching this class (Open/Closed principle - README section 4).
    """

    def __init__(self, title: str, description: str, created_by: User,
                 assigned_to: Optional[User] = None):
        if self.__class__ is Incident:
            raise TypeError("Incident is abstract and cannot be instantiated directly.")
        self.id = str(uuid.uuid4())
        self.title = title
        self.description = description
        self.status = IncidentStatus.OPEN
        self.resolution_type: Optional[ResolutionType] = None
        self.resolution_message: Optional[str] = None
        self.created_by = created_by
        self.assigned_to = assigned_to
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.comments: List[Comment] = []

    @classmethod
    def from_dict(cls, data: dict, created_by: User, assigned_to: Optional[User] = None) -> "Incident":
        """
        Alternate constructor: builds an incident from a plain dict shaped
        like one line of data/sample_data.jsonl (see
        IncidentRepository.load_from_jsonl() in app/repository.py, which is
        the only caller). `created_by`/`assigned_to` are already-resolved
        User objects, not usernames - resolving a username string to a
        User is the loader's job (it has the UserStore), not this method's.

        This base implementation only validates the two fields every
        incident type needs (title, description) and refuses to build
        anything itself, since Incident can't be instantiated directly
        (see __init__ above). MaintenanceTask.from_dict and Fault.from_dict
        each OVERRIDE this method to add their own subclass-specific
        fields (queue_position has nothing to seed; severity/details do),
        calling `super().from_dict(data, ...)` first purely for this
        shared validation - its return value is unused by them.

        Raises:
            TypeError: if called on Incident itself rather than a concrete subclass.
            ValueError: if 'title' or 'description' is missing or blank.
        """
        if cls is Incident:
            raise TypeError("Incident.from_dict must be called on a concrete subclass (MaintenanceTask or Fault).")
        title = data.get("title")
        description = data.get("description")
        if not title or not str(title).strip():
            raise ValueError(f"record {data.get('id', '?')!r} is missing a required 'title'")
        if not description or not str(description).strip():
            raise ValueError(f"record {data.get('id', '?')!r} is missing a required 'description'")
        return None  # subclasses only use this call for its validation side effect, not this return value

    def add_comment(self, author: User, text: str) -> Comment:
        """Any authenticated user may add a comment/update."""
        comment = Comment(author=author, text=text)
        self.comments.append(comment)
        self.updated_at = datetime.now(timezone.utc)
        return comment

    def start_progress(self) -> None:
        """Move Open -> In Progress. No-op if already in progress or closed."""
        if self.status == IncidentStatus.OPEN:
            self.status = IncidentStatus.IN_PROGRESS
            self.updated_at = datetime.now(timezone.utc)

    def close(self, actor: User, resolution_type: ResolutionType, message: str) -> None:
        """
        Close the incident. This single method covers all three resolution
        types (confirmed design decision - README sections 4 and 8):
        Resolved, Not an Incident, and By Design are not separate code
        paths, just this one method with a required ResolutionType and a
        mandatory, non-empty message.

        Raises:
            ValueError: if message is empty/blank.
            PermissionError: if resolution_type is admin-only
                (Not an Incident / By Design) and actor is not an admin.
        """
        if not message or not message.strip():
            raise ValueError("A closing message is required for every resolution type.")
        if resolution_type in _ADMIN_ONLY_RESOLUTIONS and actor.role != Role.ADMIN:
            raise PermissionError(f"Only an admin may close an incident as {resolution_type.name}.")

        self.status = IncidentStatus.CLOSED
        self.resolution_type = resolution_type
        self.resolution_message = message.strip()
        self.updated_at = datetime.now(timezone.utc)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.id[:8]}, title={self.title!r}, status={self.status.name})"

    def __str__(self) -> str:
        status_name = self.status.name if hasattr(self.status, "name") else str(self.status)
        return f"[{self.__class__.__name__} - {status_name}] {self.title}"