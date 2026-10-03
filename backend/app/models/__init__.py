"""
Domain model package. Re-exports the public classes/enums so other
packages can do `from app.models import User, Fault, ...` instead of
reaching into individual submodules.

Import order matters here: enums first (nothing depends on it), then user
(needed by comment/incident type hints), then comment, then the Incident
hierarchy. This mirrors the actual dependency graph between the files.

The Sites & Mailing Lists subsystem (group-of-four extension) comes last:
site and mailing_list depend only on enums/user, and notification depends
on both of them. _permissions.py is internal to this package and is
deliberately not re-exported.
"""

from app.models.enums import (
    IncidentStatus, NotificationState, ResolutionType, Role, SeverityCategory, SiteStatus,
)
from app.models.user import User
from app.models.comment import Comment
from app.models.incident import Incident
from app.models.maintenance_task import MaintenanceTask
from app.models.fault import Fault
from app.models.site import FAILURES_BEFORE_DOWN, SYSTEM_ACTOR, AvailabilityCheck, Site, SiteStatusChange
from app.models.mailing_list import MailingList
from app.models.notification import Notification

__all__ = [
    "Role", "IncidentStatus", "ResolutionType", "SeverityCategory",
    "SiteStatus", "NotificationState",
    "User", "Comment", "Incident", "MaintenanceTask", "Fault",
    "Site", "SiteStatusChange", "SYSTEM_ACTOR", "AvailabilityCheck", "FAILURES_BEFORE_DOWN", "MailingList", "Notification",
]