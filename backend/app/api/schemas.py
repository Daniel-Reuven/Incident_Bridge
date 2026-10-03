"""
Pydantic request bodies for the API layer.

Response bodies are plain dicts built by app/api/serializers.py, not
Pydantic models - FastAPI's jsonable_encoder already handles Enum and
datetime encoding for plain dicts, so a parallel set of response models
would just duplicate the fields defined on the domain classes themselves.
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import IncidentStatus, ResolutionType, SeverityCategory


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateMaintenanceTaskRequest(BaseModel):
    title: str
    description: str


class CompleteMaintenanceRequest(BaseModel):
    """
    Body for POST /incidents/maintenance/complete-current and
    POST /incidents/maintenance/{id}/close. `message` is the required reason.
    """
    message: str
    resolution_type: ResolutionType = ResolutionType.RESOLVED


class ReopenMaintenanceRequest(BaseModel):
    """
    Body for POST /incidents/maintenance/{id}/reopen (admin-only).
    `status` is "open" or "in_progress" (the domain layer rejects "closed"
    with a 400). `reason` is mandatory. `position` is the queue position to
    put the task at, numbered the way the incident page displays it (the
    in-progress task is position 1); omit it to put the task at the back.
    """
    status: IncidentStatus
    reason: str
    position: Optional[int] = None


class CreateFaultRequest(BaseModel):
    title: str
    description: str
    details: dict = Field(default_factory=dict)


class CloseFaultRequest(BaseModel):
    resolution_type: ResolutionType
    message: str


class ReopenFaultRequest(BaseModel):
    """
    Body for POST /incidents/faults/{id}/reopen (admin-only). `status` is
    the status to reopen into ("open" or "in_progress"; the domain layer
    rejects "closed" with a 400), and `reason` is mandatory - the domain
    layer rejects a blank one with a 400 and records it in the incident's
    log comment.
    """
    status: IncidentStatus
    reason: str


class ChangeSeverityRequest(BaseModel):
    severity: SeverityCategory


class AddCommentRequest(BaseModel):
    text: str


class ImportJsonlRequest(BaseModel):
    content: str