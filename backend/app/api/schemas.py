"""
Pydantic request bodies for the API layer.

Response bodies are plain dicts built by app/api/serializers.py, not
Pydantic models - FastAPI's jsonable_encoder already handles Enum and
datetime encoding for plain dicts, so a parallel set of response models
would just duplicate the fields defined on the domain classes themselves.
"""

from pydantic import BaseModel, Field

from app.models.enums import ResolutionType, SeverityCategory


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
    message: str
    resolution_type: ResolutionType = ResolutionType.RESOLVED


class CreateFaultRequest(BaseModel):
    title: str
    description: str
    details: dict = Field(default_factory=dict)


class CloseFaultRequest(BaseModel):
    resolution_type: ResolutionType
    message: str


class ChangeSeverityRequest(BaseModel):
    severity: SeverityCategory


class AddCommentRequest(BaseModel):
    text: str
