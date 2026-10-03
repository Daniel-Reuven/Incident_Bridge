"""
Shared permission helper for the Sites & Mailing Lists subsystem: its
models (site.py, mailing_list.py, notification.py) and the admin
create/edit operations in app/sites.py (SiteDirectory).

Keeps the "admin only" rule in exactly one function, so every model method
that needs it raises the same PermissionError with the same wording. The
API layer already maps PermissionError to HTTP 403 (see app/api/app.py),
so no extra wiring is needed there.

The leading underscore marks this as internal to the domain layer (app.models
and app.sites) - it is not re-exported from app/models/__init__.py, and the
API layer never calls it directly (it relies on the domain raising).
"""

from app.models.enums import Role
from app.models.user import User


def require_admin(actor: User, action: str) -> None:
    """
    Raise PermissionError unless `actor` is an admin.

    `action` completes the sentence "Only an admin may ...", e.g.
    require_admin(user, "archive a site") ->
    "Only an admin may archive a site."
    """
    if actor is None or actor.role != Role.ADMIN:
        raise PermissionError(f"Only an admin may {action}.")
