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
