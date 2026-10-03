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

# The only statuses a closed incident may be reopened into. CLOSED is
# deliberately absent: "reopening" into Closed would be meaningless.
_REOPEN_TARGETS = {IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS}

# Upper bounds for the validated text fields (see the title/description
# properties). Generous on purpose: they only stop absurd input (e.g. a
# pasted log file as a title), never a normal incident.
_MAX_TITLE_LENGTH = 200
_MAX_DESCRIPTION_LENGTH = 10_000


def _validated_text(value, field_name: str, max_length: int) -> str:
    """
    Shared rule for the title and description properties: `value` must be a
    string that is not blank after trimming, and at most `max_length`
    characters long. Returns the trimmed text; raises ValueError (HTTP 400 at
    the API layer) naming the field otherwise.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"An incident {field_name} is required and cannot be blank.")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"An incident {field_name} must be at most {max_length} characters (got {len(value)}).")
    return value


def _label(member) -> str:
    """
    Human-readable label for a status/resolution enum member, derived from
    its value: 'in_progress' -> 'In progress', 'not_an_incident' -> 'Not an
    incident'. Used only for the text of automatic log comments.
    """
    return member.value.replace("_", " ").capitalize()


def _as_sentence(text: str) -> str:
    """Trim `text` and make sure it ends in terminal punctuation, so log-comment segments read cleanly."""
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


class Incident(ABC):
    """
    Abstract base class shared by MaintenanceTask and Fault.

    Holds the lifecycle (status, resolution, timestamps) and the comment
    thread that both concrete incident types inherit unchanged; each
    subclass adds only what's specific to it (see maintenance_task.py and
    fault.py). This keeps the model open for a third incident type later
    without touching this class (Open/Closed principle - README section 4).

    Validation: `title` and `description` are properties whose setters
    reject blank or over-long text with ValueError, so an incident can never
    hold an empty title or description - not at creation (the constructor
    assigns through the setters), not when rebuilt from stored data, and not
    if code edits the field later. An invalid assignment leaves the old value
    in place.

    Manual status changes (see log_status_change() and reopen() below) only
    change the incident's OWN fields and its comment thread. They never
    touch a queue: placing a reopened incident back into the right queue
    is the API layer's job (see app/api/incidents.py), the same way
    start_progress() and close() have always left queue bookkeeping to
    MaintenanceQueue / FaultPriorityQueue.
    """

    def __init__(self, title: str, description: str, created_by: User,
                 assigned_to: Optional[User] = None):
        if self.__class__ is Incident:
            raise TypeError("Incident is abstract and cannot be instantiated directly.")
        self.id = str(uuid.uuid4())
        self.title = title              # validated by the property setter below
        self.description = description  # validated by the property setter below
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
        incident type needs (title, description) - an early check whose
        message names the record id; the properties re-check the same rule
        when the object is built - and refuses to build anything itself, since Incident can't be instantiated directly
        (see __init__ above). MaintenanceTask.from_dict and Fault.from_dict
        each OVERRIDE this method to add their own subclass-specific
        fields (queue_position has nothing to seed; severity/details do),
        calling `super().from_dict(data, ...)` first purely for this
        shared validation - its return value is unused by them.

        Explicit decision: every seed record MUST carry an 'id' (a non-blank
        string, or a whole number, which is stored as its text). The id is
        what makes re-importing the same file idempotent (an existing id is
        skipped, not duplicated) and what other data refers to, so a record
        without one is rejected rather than given a random id. Use
        incident_id_from() to read it.

        Raises:
            TypeError: if called on Incident itself rather than a concrete subclass.
            ValueError: if 'id' is missing or invalid, or 'title' or
                'description' is missing or blank.
        """
        if cls is Incident:
            raise TypeError("Incident.from_dict must be called on a concrete subclass (MaintenanceTask or Fault).")
        cls.incident_id_from(data)
        title = data.get("title")
        description = data.get("description")
        if not title or not str(title).strip():
            raise ValueError(f"record {data.get('id', '?')!r} is missing a required 'title'")
        if not description or not str(description).strip():
            raise ValueError(f"record {data.get('id', '?')!r} is missing a required 'description'")
        return None  # subclasses only use this call for its validation side effect, not this return value

    @staticmethod
    def incident_id_from(data: dict) -> str:
        """
        The record's 'id' as text. Accepts a non-blank string or a whole
        number (e.g. 101 -> "101"); raises ValueError for anything else,
        including a missing id (see from_dict's explicit decision).
        """
        raw = data.get("id")
        if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).strip():
            raise ValueError(f"record is missing a valid 'id' (a non-blank string or a whole number), got {raw!r}")
        return str(raw).strip()

    # ------------------------------------------------------------------
    # Validated text fields
    # ------------------------------------------------------------------

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value) -> None:
        self._title = _validated_text(value, "title", _MAX_TITLE_LENGTH)

    @property
    def description(self) -> str:
        return self._description

    @description.setter
    def description(self, value) -> None:
        self._description = _validated_text(value, "description", _MAX_DESCRIPTION_LENGTH)

    def add_comment(self, author: User, text: str) -> Comment:
        """Any authenticated user may add a comment/update."""
        comment = Comment(author=author, text=text)
        self.comments.append(comment)
        self.updated_at = datetime.now(timezone.utc)
        return comment

    def log_status_change(self, actor: User, old_status: IncidentStatus, new_status: IncidentStatus,
                          reason: Optional[str] = None, detail: Optional[str] = None) -> Comment:
        """
        Record a status change in the comment thread, authored by `actor`
        (so the thread shows who did it), e.g.:

            "Status changed from Closed to Open. Reason: Regression found.
             Previous resolution: Resolved - Fixed it."

        This only WRITES the log entry - it does not change self.status
        itself and does not validate the transition; callers (reopen()
        below, and later the queue/API operations) decide whether a change
        is allowed and perform it, then call this to leave the audit trail.

        `reason` and `detail` are both optional here because not every
        logged change has a user-supplied reason (e.g. logging an
        automatic Start next later on). Operations that REQUIRE a reason,
        such as reopen(), enforce that themselves before calling this.
        `detail` is a free-form extra sentence, e.g. "Queue position: 3."

        Returns the Comment that was added.
        """
        text = f"Status changed from {_label(old_status)} to {_label(new_status)}."
        if reason and reason.strip():
            text += f" Reason: {_as_sentence(reason)}"
        if detail and detail.strip():
            text += f" {_as_sentence(detail)}"
        return self.add_comment(actor, text)

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

        The message doubles as the required REASON for the status change:
        on success a log comment authored by `actor` is added, e.g.
        "Status changed from Open to Closed. Reason: <message>.
        Resolution: Resolved." (see log_status_change()). Because the
        logging lives here, every close of every incident type - a fault
        closed before ever being claimed, a claimed fault, a completed
        maintenance task - leaves the same audit trail automatically.

        Checks run in this order (nothing is changed or logged if any of
        them fails): message, then permission, then current state.

        Raises:
            ValueError: if message is empty/blank.
            PermissionError: if resolution_type is admin-only
                (Not an Incident / By Design) and actor is not an admin.
            RuntimeError: if the incident is already Closed (the API layer
                maps this to HTTP 409). Re-closing would silently overwrite
                the earlier resolution and its audit trail.
        """
        if not message or not message.strip():
            raise ValueError("A closing message is required for every resolution type.")
        if resolution_type in _ADMIN_ONLY_RESOLUTIONS and actor.role != Role.ADMIN:
            raise PermissionError(f"Only an admin may close an incident as {resolution_type.name}.")
        if self.status == IncidentStatus.CLOSED:
            raise RuntimeError("This incident is already closed.")

        old_status = self.status
        self.status = IncidentStatus.CLOSED
        self.resolution_type = resolution_type
        self.resolution_message = message.strip()
        self.updated_at = datetime.now(timezone.utc)
        self.log_status_change(
            actor, old_status, IncidentStatus.CLOSED,
            reason=self.resolution_message, detail=f"Resolution: {_label(resolution_type)}",
        )

    def reopen(self, actor: User, new_status: IncidentStatus, reason: str,
               detail: Optional[str] = None) -> None:
        """
        Move a CLOSED incident back to Open or In progress. Admin-only
        (confirmed design decision), and a non-blank `reason` is mandatory.

        What it does: clears resolution_type and resolution_message (so
        "closed" always means "has a resolution"), sets the new status, and
        logs a comment authored by `actor` containing the reason AND the
        resolution that was just cleared, so nothing about the earlier
        closure is lost. An optional `detail` sentence (e.g. "Queue
        position: 3") is appended after the previous-resolution text.

        What it deliberately does NOT do: touch queue_position or any
        queue. Putting the incident back in the right queue is the API
        layer's job - same split of responsibility as close() above.
        assigned_to is also left alone here; subclasses decide what a
        reopen means for the assignee (Fault.reopen assigns an In-progress
        reopen to the actor and clears the assignee on an Open one).

        Checks run in this order, mirroring close(): input validation
        first (so a user always learns about an empty reason or invalid
        target before being told "permission denied"), then permission,
        then the incident's current state.

        Raises:
            ValueError: if reason is empty/blank, or new_status is not
                Open / In progress.
            PermissionError: if actor is not an admin.
            RuntimeError: if the incident is not currently Closed (the
                API layer maps this to HTTP 409).
        """
        if not reason or not reason.strip():
            raise ValueError("A reason is required to reopen an incident.")
        if new_status not in _REOPEN_TARGETS:
            raise ValueError("A reopened incident must become Open or In progress.")
        if actor.role != Role.ADMIN:
            raise PermissionError("Only an admin may reopen a closed incident.")
        if self.status != IncidentStatus.CLOSED:
            raise RuntimeError("Only a closed incident can be reopened.")

        previous_resolution = None
        if self.resolution_type is not None:
            previous_resolution = f"Previous resolution: {_label(self.resolution_type)}"
            if self.resolution_message:
                previous_resolution += f" - {self.resolution_message}"

        old_status = self.status
        self.status = new_status
        self.resolution_type = None
        self.resolution_message = None
        self.updated_at = datetime.now(timezone.utc)

        detail_sentences = [_as_sentence(part) for part in (previous_resolution, detail) if part and part.strip()]
        self.log_status_change(
            actor, old_status, new_status, reason=reason, detail=" ".join(detail_sentences) or None
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.id[:8]}, title={self.title!r}, status={self.status.name})"

    def __str__(self) -> str:
        status_name = self.status.name if hasattr(self.status, "name") else str(self.status)
        return f"[{self.__class__.__name__} - {status_name}] {self.title}"