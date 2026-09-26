"""
Domain model package. Re-exports the public classes/enums so other
packages can do `from app.models import User, Fault, ...` instead of
reaching into individual submodules.

Import order matters here: enums first (nothing depends on it), then user
(needed by comment/incident type hints), then comment, then the Incident
hierarchy. This mirrors the actual dependency graph between the files.
"""

from app.models.enums import IncidentStatus, ResolutionType, Role, SeverityCategory
from app.models.user import User
from app.models.comment import Comment
from app.models.incident import Incident
from app.models.maintenance_task import MaintenanceTask
from app.models.fault import Fault

__all__ = [
    "Role", "IncidentStatus", "ResolutionType", "SeverityCategory",
    "User", "Comment", "Incident", "MaintenanceTask", "Fault",
]
