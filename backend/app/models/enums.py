"""
Enums shared across the domain model.

Kept in one module (rather than scattered per-class) since several classes
reference more than one of these, and it avoids import-order headaches.
"""

from enum import Enum


class Role(Enum):
    """A User's permission level. See README section 5/8 for who can do what."""
    ADMIN = "admin"
    USER = "user"


class IncidentStatus(Enum):
    """
    Simplified lifecycle (confirmed design decision): resolution reasons
    (Resolved / Not an Incident / By Design) are NOT separate statuses -
    they are all just "Closed" with a ResolutionType attached. See
    ResolutionType below and Incident.close() in incident.py.
    """
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class ResolutionType(Enum):
    """
    The reason an incident was closed. All three are set via the single
    Incident.close(actor, resolution_type, message) method and all three
    require a mandatory message - they differ only in the recorded reason
    and in who is allowed to set them (RESOLVED: user or admin;
    NOT_AN_INCIDENT / BY_DESIGN: admin only).
    """
    RESOLVED = "resolved"
    NOT_AN_INCIDENT = "not_an_incident"
    BY_DESIGN = "by_design"


class SeverityCategory(Enum):
    """
    Fixed set of fault severity categories - no others allowed.

    The numeric value doubles as the heapq priority key: lower value
    sorts first, so CRITICAL (1) is always popped before MAJOR (2) before
    MINOR (3). See app/queues/fault_queue.py.
    """
    CRITICAL = 1
    MAJOR = 2
    MINOR = 3


class SiteStatus(Enum):
    """
    Health of an organization site (the Sites & Mailing Lists subsystem -
    see app/models/site.py).

    UNKNOWN is the starting status: a freshly imported, created or restored
    site has not been checked yet. OPERATIONAL / DEGRADED / DOWN are what an
    availability check (or an admin's manual override) can set.
    MAINTENANCE is only ever set manually by an admin; automated checks
    skip sites in maintenance so planned work never raises an alarm.
    """
    UNKNOWN = "unknown"
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    DOWN = "down"
    MAINTENANCE = "maintenance"


class NotificationState(Enum):
    """
    Lifecycle of a Notification (see app/models/notification.py).

    Every site status change creates a notification that starts as DRAFT,
    or SKIPPED straight away when no active mailing list with members covers
    that site. An admin then either sends a draft (SENT) or discards it
    (DISMISSED). SENT, DISMISSED and SKIPPED are final - nothing moves a
    notification out of them.
    """
    DRAFT = "draft"
    SENT = "sent"
    DISMISSED = "dismissed"
    SKIPPED = "skipped"
